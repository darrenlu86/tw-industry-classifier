# -*- coding: utf-8 -*-
r"""
build_authority_master.py — 權威來源主檔建置器（交付版，可獨立執行）

一列 ＝ 一筆「某機構出現在某份主管機關名冊上」的外部事實。兼營者多列，不做取捨。

本檔為交付版：組裝邏輯、source 欄口徑、schema 驗證、排序與守門與產線版逐字相同，
僅把路徑改走 `_paths` 模組、把對原產線 etl 的跨目錄 import 換成本地副本
（`_taxid` / `_taxonomy`），因此可從任何目錄直接執行，不依賴 原產線的目錄層數。

列來源（三張凍結名冊，只讀）
------------------------------------------------------------------
(A) 金融機構 490 列 → crawlers/masters/financial_institutions_master.csv
(B) 保經代   542 列 → crawlers/masters/insurance_brokers_master.csv
(C) 農漁會   311 列 → crawlers/masters/farmers_credit_master.csv

驗證層（覆蓋率報告用，只讀）
------------------------------------------------------------------
(D) crawlers/baseline/ 底下的名冊 CSV 快照（各 fetcher 的 diff 基準，**全部無 tax_id 欄**）
    ——22 份主管機關名冊 ＋ leasing.csv（租賃公會名錄）。

為何以 (A)(B)(C) 三張 reconciled 名冊為列來源、而非直接堆 baseline 名冊？
------------------------------------------------------------------
baseline 是「抓取當下、尚未歸併」的原始快照：
  - 過量計數兼營（raw sfb_sica 投顧 166 vs 名冊專營投顧 89；raw 期貨 54+51=105 vs 名冊專營期貨商 16）。
  - **完全沒有** 郵政儲匯（手動列），融資租賃另在 baseline/leasing.csv 且 schema 不同，
    而規格要求「融資租賃 source 標『租賃公會名錄（非主管機關）』」「總列數含 490」。
  - 沒有 renamed/merged/exited 沿革，也沒有統編。
三張名冊正是「raw → 歸併 + 統編補全 + 沿革承接」後的 reconciled 事實層（純外部、無內部數據），
已內含兼營雙列（彰銀/法巴）、滙豐兩法人、42+10 沿革列與統編。故以名冊為列來源可精確命中驗收，
baseline 則作為 **provenance／enrich 覆蓋率驗證層**：對每份 baseline 以官方名稱查 enrich 快取
補統編，輸出解析率報告，示範「raw 無統編 → enrich 補」機制，未解析者即「待日後 TWSE/GCIS 補查」。

已知覆蓋率限制（如實揭露，非本腳本 bug）
------------------------------------------------------------------
baseline 名冊只有官方名稱、沒有統編，統編一律靠 enrich 快取（tax_id_lookup.csv）以名稱回查，
名稱寫法不同（全半形、括號註記、簡稱）就查不到。覆蓋率報告中「未解析」的筆數即為此類，
需 TWSE/GCIS 或人工補查（見 query_pending_taxids.py），本腳本**不猜、不臆造統編**。

統編回填
------------------------------------------------------------------
列源名冊 (A)(B) 自身的 tax_id 欄若為空，改以 enrich 快取（crawlers/masters/tax_id_lookup.csv，
依 official_name 查）回填 tax_id + tax_id_source；查無快取則維持空白（留 tax_id_pending.csv 待補）。
只呼叫 `enrich_tax_id.load_lookup()`（只讀既有快取，檔案不存在才建置一次），
**絕不呼叫 `build_lookup()+write_lookup()`**——後者會用三張凍結名冊重新「從零」建快取，
把 `query_pending_taxids.py` 已透過 GCIS／人工解析、寫回 tax_id_lookup.csv 的成果整批覆蓋掉。

fetched_at 為何預設是 2026-07-03（凍結日，不是執行日）
------------------------------------------------------------------
`fetched_at` 的語意是「這筆外部事實是哪一天抓到的」。三張列源名冊與 baseline 名冊皆註明
抓取於 2026-07-03，本產線只做「事實承接、不重查」，所以預設沿用真實抓取日而非執行日——
寫執行日會謊稱資料比實際更新，也會讓輸出不再 deterministic（吃系統時間）。
只有在列源名冊本身被重新抓取更新後，才該用 `--fetched-at YYYY-MM-DD` 覆寫成新的抓取日。

輸出：crawlers/build/authority_master.csv（10 欄；utf-8-sig；deterministic）
可用 `--out <path>` 導向臨時路徑做 dry-run（驗證回填邏輯，不覆寫正式檔）。

用法：
  python build_authority_master.py
  python build_authority_master.py --out build/authority_master_dryrun.csv
  python build_authority_master.py --fetched-at 2026-08-15   # 列源名冊已重抓才用
"""

import argparse
import csv
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths

from _taxid import normalize_tax_id
# 值域必須對齊名冊事實層的九大類（用本地副本，不自創類名）。
# VALID_GROUPS／VALID_STATUS 一併從 _taxonomy 取，不在本檔重新宣告——
# 重複宣告會在只改一邊時靜默漂移。
from _taxonomy import SEGMENT_TO_GROUP, GROUP_ORDER, VALID_GROUPS, VALID_STATUS

import enrich_tax_id

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ── 常數 ─────────────────────────────────────────────────────
# 輸出 10 欄（規格 §1.1 表列順序）。第 10 欄 short_name 併入 aliases（規格表末列「short_name / aliases」）。
COLUMNS = [
    "tax_id", "customer_name", "industry_group", "industry_detail", "source",
    "status", "fetched_at", "tax_id_source", "inst_code", "short_name",
]

# 預設 fetched_at ＝ 三張列源名冊的凍結抓取日（非執行日）；理由見檔頭。
DEFAULT_FETCHED_AT = "2026-07-03"

# segment → industry_group：以 _taxonomy 的 24 段為底，補三張獨立表的 4 段（值域仍在九大類內）
EXTRA_SEGMENT_TO_GROUP = {
    "保險經紀人公司": "保險",
    "保險代理人公司": "保險",
    "農會信用部": "金控與銀行",
    "漁會信用部": "金控與銀行",
}
SEG2GROUP = {**SEGMENT_TO_GROUP, **EXTRA_SEGMENT_TO_GROUP}

GROUP_RANK = {g: i for i, g in enumerate(GROUP_ORDER)}

# 檔名同時是輸出 source 欄的組裝依據與統計標籤，故沿用原檔名字串當邏輯鍵，實際位置查 MASTER_PATHS。
MASTER_PATHS = {
    "financial_institutions_master.csv": _paths.FIM,
    "insurance_brokers_master.csv": _paths.BROKERS,
    "farmers_credit_master.csv": _paths.FARMERS,
}

# 硬編的預期列數是**刻意的防呆**：列源名冊是凍結事實，列數變動代表名冊被改動過，
# 而非本腳本邏輯改變。若確認是刻意更新名冊，才連帶調整這裡的數字。
SOURCE_INPUTS = [
    ("financial_institutions_master.csv", 490),
    ("insurance_brokers_master.csv", 542),  # 542（非 544）：台新證保代／未來保代更名後的新舊名重複列已合併
    ("farmers_credit_master.csv", 311),
]

# 只讀不寫的列源名冊（md5 前後守門）。本產線僅讀這三張列源名冊。
FROZEN_FILES = [
    "financial_institutions_master.csv", "insurance_brokers_master.csv",
    "farmers_credit_master.csv",
]


# ── source 欄組裝（機關＋出處；租賃如實標非主管機關；沿革列註明承接自何檔）──
def make_source(authority, segment, status, source_master):
    authority = (authority or "").strip()
    if segment == "融資租賃資融":
        base = "租賃公會名錄（非主管機關）"          # 非金融特許業，如實標註
    elif authority == "金管會銀行局":
        base = "金管會銀行局(banking.gov.tw)"
    elif authority == "金管會證期局":
        base = "金管會證期局(sfb.gov.tw)"
    elif authority == "金管會保險局":
        if source_master == "insurance_brokers_master.csv":
            base = "金管會保險局(保險輔助人名錄；GCIS＋data.gov.tw＋公會拼合)"
        else:
            base = "金管會保險局(ib.gov.tw)"
    elif authority == "農業部農業金融署":
        base = "農業部農業金融署(afna.gov.tw)"
    elif authority == "數位發展部數位產業署":
        base = "數位發展部數位產業署(moda.gov.tw；data.gov.tw#165372)"
    elif authority.startswith("交通部"):
        base = "交通部/中華郵政(郵政儲匯；GCIS核實，手動列)"
    elif authority.startswith("經濟部"):                # 融資租賃保險（多為 segment 已攔截）
        base = "租賃公會名錄（非主管機關）"
    else:
        raise ValueError(f"未知 authority，無法組裝 source：{authority!r}（segment={segment}）")

    if status in ("renamed", "merged", "exited"):
        base += f"；沿革承接自 {source_master}"
    return base


def _combine_short(short_name, aliases):
    parts = [p.strip() for p in (short_name, aliases) if (p or "").strip()]
    return "|".join(parts)


def reshape_master(name, tax_id_cache, fetched_at=DEFAULT_FETCHED_AT):
    """讀單張列源名冊 → authority_master 列（dict，10 欄）。

    名冊自身 tax_id 為空時，改以 tax_id_cache（tax_id_lookup.csv，依 official_name 查）回填；
    tax_id_source 一併取自 cache（不得混用名冊的空 tax_id_source）。
    回傳 (rows, backfilled_count)。
    """
    path = MASTER_PATHS[name]
    with open(path, encoding="utf-8-sig", newline="") as f:
        src_rows = list(csv.DictReader(f))

    out = []
    backfilled = 0
    for r in src_rows:
        segment = (r.get("segment") or "").strip()
        group = SEG2GROUP.get(segment)
        if group is None:
            raise ValueError(f"{name} segment 查無 industry_group 對映（不准自創類名）：{segment!r}")

        status = (r.get("status") or "").strip() or "active"   # 空狀態視為 active（在冊現役）
        authority = (r.get("authority") or "").strip()
        official_name = (r.get("official_name") or "").strip()

        tax_id = normalize_tax_id(r.get("tax_id"))
        tax_id_source = (r.get("tax_id_source") or "").strip()
        if not tax_id:
            cached = tax_id_cache.get(official_name)
            if cached and cached.get("tax_id"):
                tax_id = cached["tax_id"]
                tax_id_source = cached.get("tax_id_source", "")
                backfilled += 1

        out.append({
            "tax_id": tax_id,
            "customer_name": official_name,
            "industry_group": group,
            "industry_detail": segment,                        # 直接＝名冊 segment，不加工
            "source": make_source(authority, segment, status, name),
            "status": status,
            "fetched_at": fetched_at,
            "tax_id_source": tax_id_source,
            "inst_code": (r.get("inst_code") or "").strip(),
            "short_name": _combine_short(r.get("short_name"), r.get("aliases")),
            "_source_master": name,                            # 內部用，不輸出
        })
    return out, backfilled


# ── baseline 名冊 × enrich 覆蓋率報告（示範「raw 無統編 → enrich 補」）──
def raw_enrich_coverage(cache):
    baseline_dir = Path(_paths.BASELINE_DIR)
    if not baseline_dir.exists():
        print(f"[warn] baseline 名冊目錄不存在：{baseline_dir}")
        return
    raw_csvs = sorted(baseline_dir.glob("*.csv"))
    print(f"\n[baseline × enrich 覆蓋率] 讀入 {len(raw_csvs)} 份名冊 CSV（皆無 tax_id 欄，靠 enrich 快取補）")
    total_names = total_hit = 0
    unresolved_samples = []
    for p in raw_csvs:
        with open(p, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        names = [(r.get("official_name") or "").strip() for r in rows]
        names = [n for n in names if n]
        hit = sum(1 for n in names if cache.get(n, {}).get("tax_id"))
        total_names += len(names)
        total_hit += hit
        for n in names:
            if not cache.get(n, {}).get("tax_id") and len(unresolved_samples) < 25:
                unresolved_samples.append(f"{p.name}: {n}")
        rate = f"{hit/len(names)*100:4.0f}%" if names else "  - "
        print(f"    {p.name:24s} names={len(names):4d} enriched={hit:4d} ({rate})")
    overall = f"{total_hit/total_names*100:.1f}%" if total_names else "n/a"
    print(f"  合計：名冊名稱 {total_names} 筆，enrich 命中 {total_hit} 筆（{overall}）")
    print(f"  未解析（名冊有、快取無 → 待日後 TWSE/GCIS 補查）：{total_names - total_hit} 筆；示例：")
    for s in unresolved_samples:
        print(f"    - {s}")


# ── schema 驗證（失敗 raise）──
def validate(rows):
    errs = []

    # 1. 欄位齊全
    for i, r in enumerate(rows):
        missing = [c for c in COLUMNS if c not in r]
        if missing:
            errs.append(f"列 {i} 缺欄位 {missing}")
            break

    # 2. tax_id：8 碼或空
    for r in rows:
        tid = r["tax_id"]
        if tid and not (tid.isdigit() and len(tid) == 8):
            errs.append(f"tax_id 非 8 碼或空：{tid!r}（{r['customer_name']}）")

    # 3. industry_group 在值域內
    for r in rows:
        if r["industry_group"] not in VALID_GROUPS:
            errs.append(f"industry_group 值域外：{r['industry_group']!r}（{r['customer_name']}）")

    # 4. status 在值域內
    for r in rows:
        if r["status"] not in VALID_STATUS:
            errs.append(f"status 值域外：{r['status']!r}（{r['customer_name']}）")

    # 5. 核心欄非空（customer_name / industry_detail / source）
    for r in rows:
        for c in ("customer_name", "industry_detail", "source"):
            if not r[c]:
                errs.append(f"核心欄 {c} 為空（{r.get('customer_name')!r} / {r['industry_detail']!r}）")

    # 6. 重複 (tax_id, industry_detail) 組合（空 tax_id 不參與）
    seen = {}
    for r in rows:
        if not r["tax_id"]:
            continue
        key = (r["tax_id"], r["industry_detail"])
        if key in seen:
            errs.append(f"重複 (tax_id, industry_detail)：{key}（{r['customer_name']} vs {seen[key]}）")
        else:
            seen[key] = r["customer_name"]

    # 7. 00000000 不得出現（那是客戶主檔的事）
    if any(r["tax_id"] == "00000000" for r in rows):
        errs.append("00000000 不得出現在權威來源主檔（屬客戶主檔範圍）")

    if errs:
        raise ValueError("schema 驗證失敗：\n  - " + "\n  - ".join(errs[:50]))


def sort_key(r):
    # deterministic：產業群序 → detail → tax_id（空排後）→ 名稱 → inst_code → status → source
    return (
        GROUP_RANK.get(r["industry_group"], 99),
        r["industry_detail"],
        (r["tax_id"] == "", r["tax_id"]),
        r["customer_name"],
        r["inst_code"],
        r["status"],
        r["source"],
        r["tax_id_source"],
        r["short_name"],
    )


def md5_of(path):
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def _pending_count():
    """直接讀 tax_id_pending.csv 現有列數（不重建、不覆寫），供統計輸出用。"""
    if not os.path.exists(enrich_tax_id.PENDING_CSV):
        return 0
    with open(enrich_tax_id.PENDING_CSV, encoding="utf-8-sig", newline="") as f:
        return sum(1 for _ in csv.DictReader(f))


def parse_args():
    p = argparse.ArgumentParser(description="建置權威來源主檔 authority_master.csv")
    p.add_argument("--out", default=_paths.AUTHORITY_MASTER,
                    help="輸出路徑（預設為正式 crawlers/build/authority_master.csv；dry-run 請指定臨時路徑）")
    p.add_argument("--fetched-at", default=DEFAULT_FETCHED_AT,
                    help=f"輸出 fetched_at 欄的抓取日（預設 {DEFAULT_FETCHED_AT}＝三張列源名冊的凍結抓取日，"
                          "非執行日；只有名冊被重新抓取更新後才該覆寫）")
    return p.parse_args()


def main():
    args = parse_args()
    out_path = Path(args.out)
    fetched_at = args.fetched_at

    print("=" * 70)
    print("build_authority_master — 權威來源主檔建置")
    print("=" * 70)
    print(f"fetched_at＝{fetched_at}" + ("（預設：列源名冊凍結抓取日）"
                                          if fetched_at == DEFAULT_FETCHED_AT else "（由 --fetched-at 指定）"))

    # 凍結檔 md5（前）
    frozen_before = {n: md5_of(MASTER_PATHS[n]) for n in FROZEN_FILES}

    # (1) 讀 enrich 快取（只讀既有 tax_id_lookup.csv；檔案不存在才建置一次；
    #     不呼叫 build_lookup()+write_lookup()，避免覆蓋 query_pending_taxids.py 的解析成果）
    cache = enrich_tax_id.load_lookup()
    pending_n = _pending_count()
    print(f"\n[enrich] 快取 {len(cache)} 筆（讀自既有 tax_id_lookup.csv）、待補查 {pending_n} 筆（tax_id_pending.csv）")

    # (2) baseline × enrich 覆蓋率報告
    raw_enrich_coverage(cache)

    # (3) 以三張 reconciled 名冊組裝列（含兼營雙列、滙豐兩列、沿革承接；自身統編留白者以快取回填）
    rows = []
    per_source = {}
    total_backfilled = 0
    for name, expect in SOURCE_INPUTS:
        part, backfilled = reshape_master(name, cache, fetched_at)
        per_source[name] = len(part)
        total_backfilled += backfilled
        if len(part) != expect:
            raise ValueError(
                f"列源名冊列數與預期不符：{name} 實際 {len(part)} 列、預期 {expect} 列。\n"
                f"  名冊路徑：{MASTER_PATHS[name]}\n"
                "  這代表名冊被改過（新增／刪除機構，或更名列合併方式變了）。\n"
                "  預期列數是刻意的防呆，不是要你改掉就好——請先確認名冊是刻意更新，\n"
                "  再回頭調整 build_authority_master.py 的 SOURCE_INPUTS 數字。"
            )
        rows.extend(part)

    # (4) schema 驗證（失敗 raise）
    validate(rows)

    # (5) deterministic 排序後寫出（10 欄，utf-8-sig）
    rows.sort(key=sort_key)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({c: r[c] for c in COLUMNS})

    # 凍結檔 md5（後）— 確認只讀未改
    frozen_after = {n: md5_of(MASTER_PATHS[n]) for n in FROZEN_FILES}
    frozen_ok = frozen_before == frozen_after

    print("\n" + "-" * 70)
    print(f"輸出：{out_path}")
    print(f"  統編回填（自身留白、由 tax_id_lookup.csv 補上）：{total_backfilled} 列")
    print(f"  總列數：{len(rows)}（{' + '.join(str(per_source[n]) for n, _ in SOURCE_INPUTS)}）")
    for name, _ in SOURCE_INPUTS:
        print(f"    {name}: {per_source[name]}")
    print(f"  凍結檔 md5 前後一致：{frozen_ok}")
    print(f"  輸出檔 md5：{md5_of(out_path)}")
    if not frozen_ok:
        changed = [n for n in FROZEN_FILES if frozen_before[n] != frozen_after[n]]
        raise RuntimeError(
            "列源名冊在本次執行期間被改動了（md5 前後不一致）："
            f"{', '.join(changed)}。\n"
            "  本腳本只該讀名冊、不該寫名冊；輸出結果不可信，請勿採用。\n"
            "  請確認是否有其他程式同時在寫這些檔，或名冊被誤當成輸出目標。"
        )
    print("完成。")


if __name__ == "__main__":
    main()
