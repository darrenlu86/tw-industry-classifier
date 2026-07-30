# -*- coding: utf-8 -*-
r"""
query_pending_taxids.py — 批次補齊 tax_id_pending.csv 待查統編（交付版，可獨立執行、可重跑）

本檔為交付版：GCIS endpoint、`$filter` 語法、名稱正規化與容差比對、檢查碼驗證、速率控制
與產線版逐字相同，僅把路徑改走 `_paths` 模組、把寫死的查詢日期改成 `--run-date` 參數，
因此可從任何目錄直接執行，不依賴 原產線的目錄層數。

任務背景：權威來源主檔（authority_master.csv）曾有 241 列統編留白（240 家保經代 ＋
中國輸出入銀行）。統編是客戶 join 的唯一鍵，空值會讓保經代市場無法對應客戶。本腳本只動：
    crawlers/masters/tax_id_lookup.csv   （新增解析成功列）
    crawlers/masters/tax_id_pending.csv  （移除已解析列；未解析列更新 reason）
不動 authority_master.csv、不動三張凍結列源名冊。

資料來源（GCIS 商工行政資料開放平臺「公司登記關鍵字查詢」）：
    https://data.gcis.nat.gov.tw/od/data/api/6BBA2268-1367-4B42-9CCA-BC17499EBE8C
    $filter = "Company_Name like {name} and Company_Status eq {status}"（單一狀態碼，非清單）
    Company_Status=01 通常涵蓋「核准設立」／外國公司在台分公司「核准登記」

比對規則：
    1. 名稱正規化（NFKC 全半形 + 去括號註記 + 股份有限公司/有限公司/台灣分公司 尾綴容差）完全比對優先。
    2. 單一命中且（名稱正規化後一致 或 名稱含「保險經紀」/「保險代理」）→ 採用；
       但若命中候選之登記狀態為廢止/解散/撤銷/歇業/清算 等終止類狀態 → 視為與在冊保經代狀態矛盾，
       **不自動採用**，留 pending 並附候選統編＋狀態說明，待人工確認。
    3. 多重命中／模糊命中 → 留 pending，reason 附全部候選（統編＋登記名＋狀態）。
    4. 每個採用的統編皆過檢查碼驗證（權重 1,2,1,2,1,2,4,1，積數各位相加，總和 %5==0；
       第7碼為7時 +1 亦可視為通過）。
    5. 速率控制：每次查詢間 sleep；連線失敗採指數退避重試，重試仍失敗則如實記錄為「查詢受阻」
       並保留在 pending（不計入已解析、不計入名稱不符的未解析，另立一類方便回頭補查）。

已知覆蓋率限制（如實揭露，非本腳本 bug）：
    GCIS 只收公司登記，非公司登記的機構（例：中國輸出入銀行為財政部所屬銀行）查不到，
    只能靠 MANUAL_OVERRIDES 以人工查證的事實補入。名稱寫法差異過大、多家同名、或登記狀態
    與在冊身分矛盾者，本腳本**一律留 pending 不猜**，reason 會寫清候選供人工判斷。

輸出：
    更新 crawlers/masters/tax_id_lookup.csv / tax_id_pending.csv（deterministic 排序，utf-8-sig）
    報告 crawlers/build/taxid_enrich_report.md

用法：
    python query_pending_taxids.py                      # 查詢日＝今天
    python query_pending_taxids.py --run-date 2026-07-30
"""

import argparse
import csv
import datetime
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

LOOKUP_CSV = _paths.TAX_ID_LOOKUP
PENDING_CSV = _paths.TAX_ID_PENDING
REPORT_MD = os.path.join(_paths.BUILD_DIR, "taxid_enrich_report.md")

API = "https://data.gcis.nat.gov.tw/od/data/api/6BBA2268-1367-4B42-9CCA-BC17499EBE8C"
HEADERS = {"User-Agent": "Mozilla/5.0"}

STATUS_PRIMARY = "01"
STATUS_FALLBACK = ["02", "03", "04", "05", "06", "07", "08", "09", "10",
                    "11", "12", "13", "14", "15", "16", "17", "18", "19", "20"]

SLEEP_BETWEEN = 0.25          # 請求間隔（速率控制）
MAX_RETRIES = 3               # 單一請求失敗重試上限

# 查詢日，寫進 reason／tax_id_source／報告，用來標示「這筆是哪天向 GCIS 查證的」。
# 預設今天，由 --run-date 覆寫（main() 會重設本全域）。
QUERY_DATE = datetime.date.today().isoformat()

CHECKSUM_WEIGHTS = [1, 2, 1, 2, 1, 2, 4, 1]

RISKY_STATUS_KEYWORDS = ("廢止", "解散", "撤銷", "歇業", "清算")

BRANCH_SUFFIX = "台灣分公司"
CORP_SUFFIXES = ("股份有限公司", "有限合夥", "有限公司")

# 人工查證的既有事實（非 GCIS 可查者）：中國輸出入銀行，檢查碼已驗過 PASS。
MANUAL_OVERRIDES = {
    "中國輸出入銀行": {
        "tax_id": "12211183",
        "tax_id_source": "manual（使用者提供，2026-07-14；檢查碼已驗過 PASS）",
    },
}


# ── 統編檢查碼 ───────────────────────────────────────────────
def validate_checksum(tax_id):
    """台灣統一編號檢查碼：權重 1,2,1,2,1,2,4,1 相乘→個位十位相加→總和 %5==0；
    第7碼為7時 +1 亦可視為通過。"""
    if not (tax_id and tax_id.isdigit() and len(tax_id) == 8):
        return False
    total = 0
    for d, w in zip(tax_id, CHECKSUM_WEIGHTS):
        p = int(d) * w
        total += p // 10 + p % 10
    if total % 5 == 0:
        return True
    if tax_id[6] == "7" and (total + 1) % 5 == 0:
        return True
    return False


# ── 名稱正規化與容差比對 ─────────────────────────────────────
def normalize_name(s):
    """NFKC 全半形正規化 + 去括號註記（如「(原：XXX)」）+ strip。"""
    s = unicodedata.normalize("NFKC", (s or "").strip())
    s = re.sub(r"[（(].*?[）)]", "", s).strip()
    return s


def core_name(s):
    """遞迴去除 台灣分公司／股份有限公司／有限公司／有限合夥 尾綴，得「核心名」供容差比對。"""
    s = normalize_name(s)
    changed = True
    while changed:
        changed = False
        for suf in (BRANCH_SUFFIX,) + CORP_SUFFIXES:
            if s.endswith(suf) and len(s) > len(suf):
                s = s[: -len(suf)]
                changed = True
                break
    return s


def core_equal(a, b):
    return core_name(a) == core_name(b)


def is_risky_status(status_desc):
    return any(k in (status_desc or "") for k in RISKY_STATUS_KEYWORDS)


# ── GCIS 查詢（含重試/退避）──────────────────────────────────
def _http_get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    r = urllib.request.urlopen(req, timeout=20)
    data = r.read()
    return json.loads(data.decode("utf-8")) if data else []


def query_gcis(name, status):
    """查一次 (name, status)；失敗重試 MAX_RETRIES 次（指數退避）。
    回傳 list（可能空）或 None（重試仍失敗＝查詢受阻）。"""
    q = f"Company_Name like {name} and Company_Status eq {status}"
    url = API + "?$format=json&$filter=" + urllib.parse.quote(q) + "&$top=20"
    for attempt in range(MAX_RETRIES):
        try:
            return _http_get(url)
        except Exception as e:
            wait = 1.0 * (2 ** attempt)
            print(f"    [retry {attempt + 1}/{MAX_RETRIES}] {name} status={status}: {e} -> sleep {wait:.1f}s",
                  file=sys.stderr)
            time.sleep(wait)
    return None


def query_name_all_status(query_name):
    """依 name 跑 01 → fallback 狀態清單，命中即停止。回傳 (candidates, blocked)。"""
    r = query_gcis(query_name, STATUS_PRIMARY)
    if r is None:
        return None, True
    time.sleep(SLEEP_BETWEEN)
    if r:
        return r, False
    for st in STATUS_FALLBACK:
        r = query_gcis(query_name, st)
        if r is None:
            return None, True
        time.sleep(SLEEP_BETWEEN)
        if r:
            return r, False
    return [], False


def query_variants(pending_name):
    """查詢字串候選（依序嘗試，命中即停）：
        v1 = 全形正規化＋去括號註記（原樣，含台灣分公司字尾）
        v2 = 若為外國公司在台分公司（字尾「台灣分公司」），額外去掉該字尾再試一次
             （GCIS 商工登記關鍵字查詢對外國分公司之 Company_Name 通常不含「台灣分公司」，
             實測如「三井物產泛立迅」「安宏」皆須去尾綴才查得到）。
    """
    v1 = normalize_name(pending_name)
    seen = {v1}
    yield v1
    if v1.endswith(BRANCH_SUFFIX) and len(v1) > len(BRANCH_SUFFIX):
        v2 = v1[: -len(BRANCH_SUFFIX)]
        if v2 not in seen:
            yield v2


# ── 單筆解析 ─────────────────────────────────────────────────
def resolve_one(pending_name):
    """回傳 dict：
        status='resolved' -> tax_id / gcis_name / status_desc
        status='pending'  -> reason（含候選說明，若有）
        status='blocked'  -> reason（GCIS 查詢受阻，剩餘未查）
    """
    candidates = []
    tried_variants = []
    for qname in query_variants(pending_name):
        tried_variants.append(qname)
        c, blocked = query_name_all_status(qname)
        if blocked:
            return {"status": "blocked", "reason": "GCIS 查詢逾時/連線失敗（已重試 3 次仍不通）"}
        if c:
            candidates = c
            break

    if not candidates:
        return {"status": "pending",
                "reason": f"GCIS 查無資料（已試查詢字串 {tried_variants}，各試 status=01 + "
                          f"{len(STATUS_FALLBACK)} 個備援狀態碼，{QUERY_DATE}）"}

    def _fmt(c):
        return f"{c.get('Business_Accounting_NO')}|{c.get('Company_Name')}|{c.get('Company_Status_Desc')}"

    if len(candidates) == 1:
        c = candidates[0]
        tid = (c.get("Business_Accounting_NO") or "").strip()
        cname = c.get("Company_Name") or ""
        status_desc = c.get("Company_Status_Desc") or ""
        name_ok = core_equal(cname, pending_name)
        kw_ok = ("保險經紀" in cname) or ("保險代理" in cname)

        if not (name_ok or kw_ok):
            return {"status": "pending",
                    "reason": f"單一命中但名稱不一致，人工確認候選：{_fmt(c)}（{QUERY_DATE}）"}

        if is_risky_status(status_desc):
            return {"status": "pending",
                    "reason": f"單一命中但登記狀態為終止類（與在冊保經代狀態矛盾），人工確認候選："
                               f"{_fmt(c)}（{QUERY_DATE}）"}

        if not validate_checksum(tid):
            return {"status": "pending",
                    "reason": f"單一命中但統編檢查碼未通過，人工確認候選：{_fmt(c)}（{QUERY_DATE}）"}

        return {"status": "resolved", "tax_id": tid, "gcis_name": cname, "status_desc": status_desc}

    # 多重命中：僅當「正規化核心名完全相同」且唯一 → 仍可視為高信心解
    exact = [c for c in candidates
             if core_equal(c.get("Company_Name") or "", pending_name)
             and not is_risky_status(c.get("Company_Status_Desc") or "")]
    if len(exact) == 1:
        c = exact[0]
        tid = (c.get("Business_Accounting_NO") or "").strip()
        if validate_checksum(tid):
            return {"status": "resolved", "tax_id": tid, "gcis_name": c.get("Company_Name"),
                     "status_desc": c.get("Company_Status_Desc") or ""}

    candidate_str = "; ".join(_fmt(c) for c in candidates[:10])
    more = f"（僅列前 10／共 {len(candidates)} 筆）" if len(candidates) > 10 else ""
    return {"status": "pending",
            "reason": f"多重命中 {len(candidates)} 筆，需人工判斷{more}：{candidate_str}（{QUERY_DATE}）"}


# ── CSV I/O（沿用 enrich_tax_id 的欄位格式，deterministic 排序）──
def read_lookup():
    lookup = {}
    if os.path.exists(LOOKUP_CSV):
        with open(LOOKUP_CSV, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                lookup[row["official_name"]] = {
                    "tax_id": row["tax_id"],
                    "tax_id_source": row["tax_id_source"],
                    "source_master": row["source_master"],
                }
    return lookup


def read_pending():
    if not os.path.exists(PENDING_CSV):
        raise FileNotFoundError(
            f"待查名單不存在：{PENDING_CSV}\n"
            "  它由 enrich_tax_id 建置（列源名冊統編留白者）；沒有這張表就沒有待查對象。")
    with open(PENDING_CSV, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_lookup(lookup):
    os.makedirs(os.path.dirname(LOOKUP_CSV), exist_ok=True)
    with open(LOOKUP_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["official_name", "tax_id", "tax_id_source", "source_master"])
        for name in sorted(lookup):
            e = lookup[name]
            w.writerow([name, e["tax_id"], e["tax_id_source"], e["source_master"]])


def write_pending(pending):
    with open(PENDING_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["official_name", "segment", "source_master", "reason"])
        for r in sorted(pending, key=lambda r: (r["source_master"], r["segment"], r["official_name"])):
            w.writerow([r["official_name"], r["segment"], r["source_master"], r["reason"]])


# ── 主流程 ───────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="向 GCIS 批次補齊 tax_id_pending.csv 的待查統編")
    p.add_argument("--run-date", default=datetime.date.today().isoformat(),
                    help="查詢日（寫進 reason／tax_id_source／報告；預設今天）")
    return p.parse_args()


def main():
    global QUERY_DATE
    args = parse_args()
    QUERY_DATE = args.run_date

    lookup = read_lookup()
    pending = read_pending()

    results_log = []   # 給報告用的逐家結果

    # (1) 人工查證的既有事實（中國輸出入銀行）
    remaining = []
    for r in pending:
        name = r["official_name"]
        if name in MANUAL_OVERRIDES:
            ov = MANUAL_OVERRIDES[name]
            lookup[name] = {
                "tax_id": ov["tax_id"],
                "tax_id_source": ov["tax_id_source"],
                "source_master": r["source_master"],
            }
            results_log.append({"official_name": name, "segment": r["segment"],
                                 "outcome": "resolved-manual", "tax_id": ov["tax_id"],
                                 "detail": ov["tax_id_source"]})
            print(f"[manual] {name} -> {ov['tax_id']}")
        else:
            remaining.append(r)
    pending = remaining

    # (2) 保經代：GCIS 批次查詢（其餘 source_master 原樣保留、不查）
    to_query = [r for r in pending if r["source_master"] == "insurance_brokers_master.csv"]
    other_pending = [r for r in pending if r["source_master"] != "insurance_brokers_master.csv"]

    print(f"\n[GCIS 批次查詢] 待查 {len(to_query)} 家保經代（{QUERY_DATE}）")
    resolved_count = 0
    unresolved_count = 0
    blocked_count = 0
    new_pending = []

    for i, r in enumerate(to_query, 1):
        name = r["official_name"]
        res = resolve_one(name)
        if res["status"] == "resolved":
            lookup[name] = {
                "tax_id": res["tax_id"],
                "tax_id_source": f"GCIS-API（公司登記關鍵字查詢，登記名：{res['gcis_name']}，"
                                  f"狀態：{res['status_desc']}，{QUERY_DATE}查證）",
                "source_master": r["source_master"],
            }
            resolved_count += 1
            results_log.append({"official_name": name, "segment": r["segment"],
                                 "outcome": "resolved-gcis", "tax_id": res["tax_id"],
                                 "detail": f"{res['gcis_name']} / {res['status_desc']}"})
            print(f"  [{i}/{len(to_query)}] RESOLVED  {name} -> {res['tax_id']} ({res['gcis_name']})")
        elif res["status"] == "blocked":
            blocked_count += 1
            new_pending.append({**r, "reason": res["reason"]})
            results_log.append({"official_name": name, "segment": r["segment"],
                                 "outcome": "blocked", "tax_id": "", "detail": res["reason"]})
            print(f"  [{i}/{len(to_query)}] BLOCKED   {name}: {res['reason']}")
        else:
            unresolved_count += 1
            new_pending.append({**r, "reason": res["reason"]})
            results_log.append({"official_name": name, "segment": r["segment"],
                                 "outcome": "pending", "tax_id": "", "detail": res["reason"]})
            print(f"  [{i}/{len(to_query)}] PENDING   {name}: {res['reason']}")

    pending = other_pending + new_pending

    # (3) 寫出（deterministic）
    write_lookup(lookup)
    write_pending(pending)

    manual_n = sum(1 for r in results_log if r["outcome"] == "resolved-manual")

    print("\n" + "=" * 70)
    print(f"人工查證事實：{manual_n} 筆")
    print(f"GCIS 批次查詢：{len(to_query)} 家保經代")
    print(f"  已解析（resolved） : {resolved_count}")
    print(f"  未解析（pending）  : {unresolved_count}")
    print(f"  查詢受阻（blocked）: {blocked_count}")
    print(f"  解析＋未解析（含受阻）= {resolved_count + unresolved_count + blocked_count} "
          f"(應= {len(to_query)})")
    print(f"lookup 總筆數：{len(lookup)}；pending 剩餘：{len(pending)}")

    write_report(results_log, {
        "resolved_count": resolved_count,
        "unresolved_count": unresolved_count,
        "blocked_count": blocked_count,
        "manual_count": manual_n,
        "total_insurance": len(to_query),
        "lookup_total": len(lookup),
        "pending_remaining": len(pending),
    })
    print(f"報告已寫出：{REPORT_MD}")


# ── 報告 ─────────────────────────────────────────────────────
def write_report(results_log, stats):
    lines = []
    lines.append("# 統編待補清單 — 補齊結果報告\n")
    lines.append(f"查詢日期：{QUERY_DATE}\n")
    lines.append("")
    lines.append("## 方法論")
    lines.append("")
    lines.append("1. **中國輸出入銀行**（財政部所屬銀行，非一般公司登記）：統編 12211183 由使用者直接提供，"
                  "已用權重法（1,2,1,2,1,2,4,1，積數各位相加，總和 %5==0）驗證 PASS，`tax_id_source=manual`。")
    lines.append("2. **保經代**：批次查詢經濟部商業發展署 GCIS 商工行政資料開放平臺「公司登記關鍵字查詢」"
                  "（`https://data.gcis.nat.gov.tw/od/data/api/6BBA2268-1367-4B42-9CCA-BC17499EBE8C`，"
                  "`$filter=Company_Name like {name} and Company_Status eq {status}`）。"
                  "名稱先做 NFKC 全半形正規化＋去括號註記（如「(原：舊名)」），"
                  "查詢先試 `Company_Status=01`，查無再依序試 19 個備援狀態碼（02–20）。")
    lines.append("3. **比對規則**：")
    lines.append("   - 單一命中 + 名稱正規化後核心相同（容差 股份有限公司/有限公司/台灣分公司 尾綴）"
                  "或名稱含「保險經紀」/「保險代理」→ 採用；但登記狀態顯示「廢止／解散／撤銷／歇業／清算」"
                  "等終止類狀態，即使名稱吻合仍**不自動採用**（與在冊保經代身分矛盾），留 pending 待人工確認。")
    lines.append("   - 多重命中：僅當「核心名唯一完全相同且狀態非終止類」時視為高信心解，否則留 pending 並附全部候選（統編／登記名／狀態）。")
    lines.append("   - 查無資料、名稱不符、或連線受阻皆留 pending，如實記錄原因。")
    lines.append("4. **檢查碼**：每筆採用的統編皆過台灣統編檢查碼驗證（權重 1,2,1,2,1,2,4,1；積數各位相加；"
                  "總和 %5==0；第7碼為7時 +1 亦可通過），未過者即使命中也不採用。")
    lines.append("5. **速率控制**：每次請求間 sleep 0.25 秒；連線失敗採指數退避重試 3 次，仍失敗記為「查詢受阻」。")
    lines.append("")
    lines.append("## 統計")
    lines.append("")
    lines.append("| 項目 | 數量 |")
    lines.append("|---|---:|")
    lines.append(f"| 人工查證事實（MANUAL_OVERRIDES 命中） | {stats['manual_count']} |")
    lines.append(f"| 保經代待查總數 | {stats['total_insurance']} |")
    lines.append(f"| GCIS 解析成功（resolved） | {stats['resolved_count']} |")
    lines.append(f"| 未解析（pending，含名稱不符/查無/狀態矛盾/多重命中） | {stats['unresolved_count']} |")
    lines.append(f"| 查詢受阻（blocked，連線失敗） | {stats['blocked_count']} |")
    lines.append(f"| 解析 + 未解析（含受阻）核對 | {stats['resolved_count'] + stats['unresolved_count'] + stats['blocked_count']} "
                 f"（應 = {stats['total_insurance']}） |")
    lines.append(f"| tax_id_lookup.csv 總筆數（更新後） | {stats['lookup_total']} |")
    lines.append(f"| tax_id_pending.csv 剩餘筆數（更新後） | {stats['pending_remaining']} |")
    lines.append("")
    lines.append("## 逐家結果")
    lines.append("")
    lines.append("| # | 官方名稱 | segment | 結果 | 統編 | 說明 |")
    lines.append("|---:|---|---|---|---|---|")
    for i, r in enumerate(results_log, 1):
        detail = (r["detail"] or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {i} | {r['official_name']} | {r['segment']} | {r['outcome']} | {r['tax_id']} | {detail} |")
    lines.append("")

    os.makedirs(os.path.dirname(REPORT_MD), exist_ok=True)
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
