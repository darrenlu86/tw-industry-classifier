# -*- coding: utf-8 -*-
"""fetch_moda.py — 數位發展部數位產業署（moda ADI）第三方支付服務業
「洗錢防制及服務能量登錄」名冊抓取器（交付版）。

本檔為可獨立執行的交付版：抓取邏輯、解析邏輯與來源 URL 皆自 原產線
`scripts/crosswalk/fetch_authority/fetch_moda.py` 逐字沿用（已驗證可用的產線），
只把路徑與日期改成 `_paths` 模組與 `--run-date` 參數，不重寫任何取數邏輯。

只做「抓取 → 解析 → 寫 raw 快照 → 與 baseline 快照 diff」，不做統編補全
（本名冊兩份官方來源皆自帶統一編號欄位，無需 TWSE／GCIS 補全），
也不寫回 authority_master.csv（彙整由 build_authority_master.py 負責）。

兩份獨立來源（沿用原始調查結論，未憑記憶臆測任何 URL）：

  來源 A — data.gov.tw 資料集 165372
    資料集頁：https://data.gov.tw/dataset/165372
    CSV 直連：https://www-api.moda.gov.tw/OpenData/Files/19292
    baseline：crawlers/baseline/moda_tpp_aml.csv

  來源 B — moda.gov.tw 數位產業署 ADI 官網「能量登錄審核通過名單」頁（權威、較新）
    頁面：https://moda.gov.tw/ADI/services/apply-serivces/energy/5757
    ODS 附件連結為動態產生的亂碼 ID（曾見 5Qx8BuqRsedekKR，對應檔名
    「…審核通過名單_1150611.ods」），本腳本**不硬編這組 ID**——每次執行時重新抓取
    頁面 HTML、以錨點文字（含「審核通過名單」且副檔名為 ODS）動態找出目前有效的
    附件連結，避免官方換檔名／換連結後腳本失效。ODS 以標準庫 zipfile + xml.etree
    解析 table:table-cell，不引入 pandas。
    baseline：crawlers/baseline/moda_tpp_registry.csv

輸出
    raw 快照寫到 `_paths.raw_dir(run_date)`（＝ crawlers/raw/<yyyy-mm-dd>/），
    檔名沿用 moda_tpp_aml.csv／moda_tpp_registry.csv，欄位結構與 baseline 相同，
    另寫一份 moda_fetch_report.md 異動報告。**絕不覆寫 baseline/ 下的既有快照。**

已知覆蓋率限制（誠實說明，沿用原檔）
    - 來源 A 為序號式流水帳，不分「現生效／廢止」區段；廢止者僅在證書編號／登錄日期
      欄內以「廢止登錄資格」文字註記，需下游自行判讀，無獨立狀態欄位。
    - 來源 B 才有分段（現生效／已廢止業者／屆期失效業者），以 status 欄標記；
      兩份來源筆數與狀態口徑不同，屬正常，不可互相當作校驗基準。
    - 任一來源抓取失敗時不中斷另一來源：如實在報告中記錄失敗原因與已嘗試手段
      （requests → Playwright render 兩段式），標記 manual fallback 待人工處理；
      已成功的來源仍正常寫出快照與 diff。

用法
    py -3.12 fetch_moda.py                      # 抓取日＝今天
    py -3.12 fetch_moda.py --run-date 2026-07-30
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths

import requests

sys.stdout.reconfigure(encoding="utf-8")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 fetch_moda.py/1.0"

# ---------------------------------------------------------------------------
# 來源 A：data.gov.tw dataset 165372
# ---------------------------------------------------------------------------
SOURCE_A_DATASET_PAGE = "https://data.gov.tw/dataset/165372"
SOURCE_A_CSV_URL = "https://www-api.moda.gov.tw/OpenData/Files/19292"
AML_SNAPSHOT_NAME = "moda_tpp_aml.csv"
AML_SEGMENT_LABEL = "第三方支付服務業-洗防能量登錄審核通過清冊(data.gov.tw dataset 165372)"
AML_FIELDS = [
    "segment", "inst_code", "official_name", "reg_no",
    "reg_date", "valid_until", "source_url", "fetched_at_note",
]

# ---------------------------------------------------------------------------
# 來源 B：moda.gov.tw ADI 官網 能量登錄頁
# ---------------------------------------------------------------------------
SOURCE_B_PAGE_URL = "https://moda.gov.tw/ADI/services/apply-serivces/energy/5757"
REGISTRY_SNAPSHOT_NAME = "moda_tpp_registry.csv"
REGISTRY_SEGMENT_LABEL = "第三方支付服務業-能量登錄(moda數位產業署官網)"
REGISTRY_FIELDS = [
    "segment", "inst_code", "official_name", "reg_no", "status",
    "reg_date", "valid_until", "source_url", "fetched_at_note",
]

ODS_LINK_RE = re.compile(
    r'<a[^>]*href="(https://www-api\.moda\.gov\.tw/File/Get/[^"]+)"[^>]*>(.*?)</a>',
    re.S,
)


# ---------------------------------------------------------------------------
# 共用：抓取（requests 優先，失敗改 Playwright render 再抓一次）
# ---------------------------------------------------------------------------
def fetch_bytes(url: str, referer: str | None = None, timeout: int = 30):
    """回傳 (ok, content_bytes_or_None, method, error_message_or_None)。"""
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return True, r.content, "requests", None
    except Exception as e:  # noqa: BLE001 — 爬蟲對外部站台需寬容捕捉後改走 fallback
        requests_err = repr(e)

    # Playwright fallback（JS 渲染／被擋時再試一次；仍失敗才回報 manual fallback）
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(user_agent=UA)
                resp = page.goto(url, timeout=timeout * 1000)
                body = resp.body() if resp is not None else None
            finally:
                browser.close()
        if body:
            return True, body, "playwright", None
        return False, None, "playwright", (
            f"requests 失敗（{requests_err}）；Playwright 開啟後內容為空"
        )
    except Exception as e2:  # noqa: BLE001
        return False, None, "playwright", (
            f"requests 失敗（{requests_err}）；Playwright fallback 亦失敗（{repr(e2)}）"
        )


# ---------------------------------------------------------------------------
# 來源 A 解析
# ---------------------------------------------------------------------------
def fetch_source_a(fetched_at: str):
    ok, content, method, err = fetch_bytes(SOURCE_A_CSV_URL, referer=SOURCE_A_DATASET_PAGE)
    if not ok:
        return None, f"來源A（data.gov.tw 165372）抓取失敗，已嘗試 requests + Playwright：{err}"

    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    # 官方欄名可能微調（例如空白差異），寬鬆比對含關鍵字的欄名
    def col(row, keyword):
        for k in fieldnames:
            if keyword in k:
                return (row.get(k) or "").strip()
        return ""

    note = (
        f"{fetched_at} 以 {method}（UA=Mozilla）直接下載 data.gov.tw 165372 CSV 資源 "
        f"({SOURCE_A_CSV_URL})，python csv 模組讀取(utf-8-sig)；內容為序號式流水帳"
        "（不分現生效/廢止區段，廢止者於證書編號/登錄日期欄內以「廢止登錄資格」註記）"
    )

    rows = []
    for r in reader:
        rows.append({
            "segment": AML_SEGMENT_LABEL,
            "inst_code": col(r, "統一編號"),
            "official_name": col(r, "廠商名稱"),
            "reg_no": col(r, "證書編號"),
            "reg_date": col(r, "登錄日期"),
            "valid_until": col(r, "有效日期"),
            "source_url": f"{SOURCE_A_DATASET_PAGE} | CSV直連: {SOURCE_A_CSV_URL}",
            "fetched_at_note": note,
        })
    return rows, None


# ---------------------------------------------------------------------------
# 來源 B 解析
# ---------------------------------------------------------------------------
def discover_registry_ods_url(page_html: str):
    """從官網頁面 HTML 動態找出目前有效的 ODS 附件連結（不硬編亂碼 ID）。"""
    candidates = []
    for m in ODS_LINK_RE.finditer(page_html):
        href, raw_text = m.group(1), m.group(2)
        text = re.sub(r"<[^>]+>", "", raw_text).strip()
        text_compact = re.sub(r"\s+", "", text)
        if "審核通過名單" in text_compact and text_compact.upper().endswith("ODS"):
            candidates.append((href, text_compact))
    if not candidates:
        return None, "頁面中找不到符合「審核通過名單...ODS」錨點文字的附件連結"
    if len(candidates) > 1:
        # 取第一個並在 note 中如實記錄有多個候選（避免靜默選錯）
        return candidates[0], f"注意：頁面命中 {len(candidates)} 個候選連結，已取第一個：{candidates}"
    return candidates[0], None


def parse_registry_ods(content: bytes):
    ns = {
        "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
        "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    }
    z = zipfile.ZipFile(io.BytesIO(content))
    root = ET.fromstring(z.read("content.xml"))
    trows = root.findall(".//table:table-row", ns)

    def cell_text(cell):
        paras = cell.findall(".//text:p", ns)
        return "".join("".join(p.itertext()) for p in paras).strip()

    def row_values(row):
        vals = []
        for c in row:
            tag = c.tag.split("}")[-1]
            if tag in ("table-cell", "covered-table-cell"):
                vals.append(cell_text(c))
        return vals

    cert_re = re.compile(r"^(\d{3}[三四五六七八九十][0-9A-Za-z]{3,6}(?:（展延）|\(展延\))?)")
    date_re = re.compile(r"[（(](\d+/\d+/\d+)[）)]")

    status = "現生效"
    parsed = []
    for row in trows:
        vals = row_values(row)
        non_empty = [v for v in vals if v.strip()]
        if not non_empty:
            continue
        first = vals[0].strip() if vals else ""
        if first == "序號":
            continue
        if "已廢止業者" in first:
            status = "已廢止"
            continue
        if "屆期失效業者" in first:
            status = "屆期失效"
            continue
        if len(non_empty) <= 1:
            continue

        vals = (vals + [""] * 7)[:7]
        _seq, reg_no_raw, name, tax_id, date_raw, valid_until_raw, _contact = vals

        if status == "已廢止":
            m = cert_re.match(reg_no_raw)
            reg_no = m.group(1) if m else reg_no_raw
            dm = date_re.search(date_raw)
            reg_date = dm.group(1) if dm else date_raw
            valid_until = ""
        else:
            reg_no = reg_no_raw
            reg_date = date_raw
            valid_until = valid_until_raw

        parsed.append({
            "reg_no": reg_no.strip(),
            "official_name": name.strip(),
            "tax_id": tax_id.strip(),
            "reg_date": reg_date.strip(),
            "valid_until": valid_until.strip(),
            "status": status,
        })
    return parsed


def fetch_source_b(fetched_at: str):
    ok, html_bytes, method, err = fetch_bytes(SOURCE_B_PAGE_URL)
    if not ok:
        return None, f"來源B（moda.gov.tw ADI 官網頁面）抓取失敗，已嘗試 requests + Playwright：{err}"

    html = html_bytes.decode("utf-8", errors="replace")
    link_info, link_note = discover_registry_ods_url(html)
    if link_info is None:
        return None, f"來源B（moda.gov.tw ADI 官網）已連上頁面，但{link_note}；需人工確認頁面版面是否改版"

    ods_url, ods_filename_text = link_info
    ok2, ods_bytes, method2, err2 = fetch_bytes(ods_url, referer=SOURCE_B_PAGE_URL)
    if not ok2:
        return None, f"來源B ODS 附件抓取失敗（連結已找到：{ods_url}），已嘗試 requests + Playwright：{err2}"

    try:
        parsed_rows = parse_registry_ods(ods_bytes)
    except Exception as e:  # noqa: BLE001
        return None, f"來源B ODS 附件已下載但解析失敗（zipfile/xml.etree）：{repr(e)}"

    note = (
        f"{fetched_at} 以 {method2}（UA=Mozilla）下載 moda.gov.tw ADI 官網公告之 ODS 附件"
        f"（頁面錨點文字：{ods_filename_text}），以 python zipfile+xml.etree 解析"
        " table:table-cell；頁面分三段：現生效/已廢止業者/屆期失效業者，status 欄位標記區段"
    )
    if link_note:
        note += f"；{link_note}"

    rows = []
    for r in parsed_rows:
        rows.append({
            "segment": REGISTRY_SEGMENT_LABEL,
            "inst_code": r["tax_id"],
            "official_name": r["official_name"],
            "reg_no": r["reg_no"],
            "status": r["status"],
            "reg_date": r["reg_date"],
            "valid_until": r["valid_until"],
            "source_url": (
                f"{SOURCE_B_PAGE_URL} | 附件直連: {ods_url} ({ods_filename_text})"
            ),
            "fetched_at_note": note,
        })
    return rows, None


# ---------------------------------------------------------------------------
# 寫檔 / diff
# ---------------------------------------------------------------------------
def write_csv(rows, fields, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def load_existing(path: Path):
    if not path.exists():
        return None
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def diff_rosters(old_rows, new_rows, key_field="inst_code", name_field="official_name"):
    """回傳列數差異與新增/消失機構（以統一編號為 key）。"""
    if old_rows is None:
        return {
            "old_rows": None,
            "new_rows": len(new_rows),
            "added": [],
            "removed": [],
            "note": "無既有快照可比對（首次抓取或 baseline 缺失）",
        }
    old_index = {r.get(key_field, "").strip(): r.get(name_field, "").strip() for r in old_rows}
    new_index = {r.get(key_field, "").strip(): r.get(name_field, "").strip() for r in new_rows}
    old_keys, new_keys = set(old_index), set(new_index)
    added = sorted((k, new_index[k]) for k in (new_keys - old_keys))
    removed = sorted((k, old_index[k]) for k in (old_keys - new_keys))
    return {
        "old_rows": len(old_rows),
        "new_rows": len(new_rows),
        "added": added,
        "removed": removed,
        "note": None,
    }


def format_diff_md(title, diff):
    lines = [f"### {title}", ""]
    if diff["old_rows"] is None:
        lines.append(f"- 既有快照：無（{diff['note']}）")
        lines.append(f"- 本次筆數：{diff['new_rows']}")
    else:
        delta = diff["new_rows"] - diff["old_rows"]
        sign = "+" if delta >= 0 else ""
        lines.append(f"- 既有快照筆數：{diff['old_rows']}")
        lines.append(f"- 本次快照筆數：{diff['new_rows']}（{sign}{delta}）")
        if diff["added"]:
            lines.append(f"- 新增機構（{len(diff['added'])} 家）：")
            for tax_id, name in diff["added"]:
                lines.append(f"  - {tax_id} {name}")
        else:
            lines.append("- 新增機構：無")
        if diff["removed"]:
            lines.append(f"- 消失機構（{len(diff['removed'])} 家）：")
            for tax_id, name in diff["removed"]:
                lines.append(f"  - {tax_id} {name}")
        else:
            lines.append("- 消失機構：無")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(run_date: str | None = None):
    fetched_at = run_date or date.today().isoformat()
    raw_dir = Path(_paths.raw_dir(fetched_at))
    report_lines = [
        f"# fetch_moda.py 抓取報告 — {fetched_at}",
        "",
        "來源：數位發展部數位產業署第三方支付服務業洗防能量登錄名冊"
        "（data.gov.tw 165372 CSV 直連 + moda ADI 官網 ODS 附件，本腳本不憑記憶猜測端點）。",
        "",
    ]

    results = {}  # name -> dict(ok, rows, snapshot_path, diff, error)

    # ---- 來源 A ----
    rows_a, err_a = fetch_source_a(fetched_at)
    if rows_a is not None:
        out_a = raw_dir / AML_SNAPSHOT_NAME
        write_csv(rows_a, AML_FIELDS, out_a)
        old_a = load_existing(Path(_paths.baseline(AML_SNAPSHOT_NAME)))
        diff_a = diff_rosters(old_a, rows_a)
        results[AML_SNAPSHOT_NAME] = {
            "ok": True, "rows": len(rows_a), "path": str(out_a), "diff": diff_a, "error": None,
        }
        report_lines.append(f"## 來源A：{AML_SEGMENT_LABEL}")
        report_lines.append(f"- 狀態：成功；寫出 {out_a}")
        report_lines.append(format_diff_md(AML_SNAPSHOT_NAME, diff_a))
    else:
        results[AML_SNAPSHOT_NAME] = {
            "ok": False, "rows": 0, "path": None, "diff": None, "error": err_a,
        }
        report_lines.append(f"## 來源A：{AML_SEGMENT_LABEL}")
        report_lines.append(f"- 狀態：**失敗**（manual fallback 待人工處理）")
        report_lines.append(f"- 錯誤：{err_a}")
        report_lines.append("")

    # ---- 來源 B ----
    rows_b, err_b = fetch_source_b(fetched_at)
    if rows_b is not None:
        out_b = raw_dir / REGISTRY_SNAPSHOT_NAME
        write_csv(rows_b, REGISTRY_FIELDS, out_b)
        old_b = load_existing(Path(_paths.baseline(REGISTRY_SNAPSHOT_NAME)))
        diff_b = diff_rosters(old_b, rows_b)
        results[REGISTRY_SNAPSHOT_NAME] = {
            "ok": True, "rows": len(rows_b), "path": str(out_b), "diff": diff_b, "error": None,
        }
        report_lines.append(f"## 來源B：{REGISTRY_SEGMENT_LABEL}")
        report_lines.append(f"- 狀態：成功；寫出 {out_b}")
        report_lines.append(format_diff_md(REGISTRY_SNAPSHOT_NAME, diff_b))
    else:
        results[REGISTRY_SNAPSHOT_NAME] = {
            "ok": False, "rows": 0, "path": None, "diff": None, "error": err_b,
        }
        report_lines.append(f"## 來源B：{REGISTRY_SEGMENT_LABEL}")
        report_lines.append(f"- 狀態：**失敗**（manual fallback 待人工處理）")
        report_lines.append(f"- 錯誤：{err_b}")
        report_lines.append("")

    if any(v["ok"] for v in results.values()):
        raw_dir.mkdir(parents=True, exist_ok=True)
        report_path = raw_dir / "moda_fetch_report.md"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(report_lines))
        print(f"報告已寫出：{report_path}")
    else:
        print("兩份來源皆抓取失敗，未寫出任何快照檔（manual fallback）。")

    print("\n".join(report_lines))
    return results


def parse_args():
    ap = argparse.ArgumentParser(description="抓取 moda 數位產業署第三方支付登錄名冊（兩份來源）")
    ap.add_argument(
        "--run-date",
        default=date.today().isoformat(),
        help="抓取日（yyyy-mm-dd），決定 raw 輸出子目錄與快照註記；預設為今天",
    )
    return ap.parse_args()


if __name__ == "__main__":
    main(parse_args().run_date)
