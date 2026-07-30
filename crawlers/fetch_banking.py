# -*- coding: utf-8 -*-
"""
fetch_banking.py — 金管會銀行局名冊 fetcher（交付版，可獨立執行、可重跑）

本檔為 industry_classifier 交付版：抓取／解析邏輯與來源 URL 與原產線腳本
逐字相同，只把路徑改走 `_paths` 模組、把寫死的抓取日期改成 `--run-date` 參數。

資料來源（7 份名冊，皆走同一清單頁樣板，差異僅 type 參數）：
  https://www.banking.gov.tw/ch/home.jsp?id=606&parentpath=0,590,604
    &mcustomize=FscSearch_BankType.jsp&type=<type>

  - 本國銀行             type=1
  - 外國銀行在臺分行     type=3
  - 大陸地區銀行在臺分行 type=T
  - 信用合作社           type=5
  - 票券金融公司         type=81
  - 金融控股公司         type=F
  - 專營電子支付機構     type=H1

  頁面本身即為完整 server-rendered HTML（非需要瀏覽器執行 JS 才會出現的內容）；
  分頁透過表單 `bankingform` 的 JS function `list(page)` 送出，實際效果只是
  對同一 URL 補 `page=<n>` 欄位做 POST，故用 requests 的 GET(首頁)+POST(各頁)
  即可還原，不需要 Playwright。此結論已於 2026-07-14 用 requests 實測驗證
  （見輸出報告開頭的方法說明），與早期快照的 fetched_at_note
  「Playwright render + JS list(page) pagination POST」相比，
  是同一套分頁機制、換了較輕量的抓取方式，非改變資料來源。

已知覆蓋率限制（誠實說明，勿當成完整金融機構名冊）：
  - 既有 baseline 的 bb_foreign_bank.csv 底部混入了 3 筆「大陸地區銀行在臺分行」
    （type=T，來源標記卻誤寫 type=3 URL）。本檔修正為獨立輸出 bb_china_bank.csv、
    source_url 如實標 type=T，與其餘 6 份一致走清單頁（而非逐一查詢已知 bank_no
    的機構詳情頁）——理由：type=T 為真正的官方清單頁，會隨名冊異動自動反映
    新增／減少的機構，不必仰賴人工預先窮舉 bank_no，較符合「可重跑、可回溯」
    的產線目標。
  - 因此 baseline 沒有獨立的 bb_china_bank.csv；diff 時會回退去
    baseline/bb_foreign_bank.csv 內按 segment 欄篩出對應 3 列，避免誤報
    「無既有快照」。
  - 本來源僅涵蓋銀行局監管的 7 類機構，不含證期局（券商／投信投顧／期貨）、
    保險局（保險公司／保經代）、農業金融署（農漁會信用部）等名冊，
    須由其他 fetcher 各自補齊。
  - 清單頁只提供機構代碼與官方名稱，**不含統一編號**；統編需另行由
    masters/tax_id_lookup.csv 之類的對照表補上。

輸出（不覆蓋 baseline/ 既有快照）：
  raw/<run-date>/bb_domestic_bank.csv 等 7 份
  raw/<run-date>/fetch_banking_report.md

diff 比對基準：
  baseline/bb_*.csv（bb_china_bank.csv 回退至 baseline/bb_foreign_bank.csv）

用法：
  py -3.12 -X utf8 fetch_banking.py [--run-date YYYY-MM-DD]
"""

import argparse
import csv
import os
import re
import sys
import time
from datetime import date

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths

sys.stdout.reconfigure(encoding="utf-8")

LIST_URL_TMPL = (
    "https://www.banking.gov.tw/ch/home.jsp?id=606&parentpath=0,590,604"
    "&mcustomize=FscSearch_BankType.jsp&type={type_code}"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
        "industry_classifier-fetch_banking/1.0"
    )
}

# 清單頁每頁固定 15 筆（實測 type=1 三頁為 15/15/8）；不足 15 即最後一頁
PAGE_SIZE = 15
MAX_PAGES = 10  # 安全上限（現行最大名冊 38 家 / 15 ≈ 3 頁，10 頁已留充裕餘裕）

ROW_RE = re.compile(
    r'fcode_con">([^<]*)</div>.*?forganization_name_con">\s*<a[^>]*title="([^"]*)"',
    re.S,
)

# (輸出檔名(對齊既有 bb_*.csv), segment 顯示名, type 參數, 預期家數量級註記)
SEGMENTS = [
    ("bb_domestic_bank.csv", "本國銀行", "1", 38),
    ("bb_foreign_bank.csv", "外國銀行在臺分行", "3", 28),
    ("bb_china_bank.csv", "大陸地區銀行在臺分行", "T", 3),
    ("bb_coop.csv", "信用合作社", "5", 23),
    ("bb_bills.csv", "票券金融公司", "81", 8),
    ("bb_fhc.csv", "金融控股公司", "F", 14),
    ("bb_epay.csv", "專營電子支付機構", "H1", 10),
]


def fetch_type_list(session, type_code, segment_label, run_date):
    """抓單一 type 的清單頁（含跨頁）。回傳 (rows, source_url, note, error)。
    rows: list[(inst_code, official_name)]；error 非 None 代表需人工介入(manual fallback)。
    """
    url = LIST_URL_TMPL.format(type_code=type_code)
    note = f"requests GET+POST pagination(list(page) 表單機制); fetched {run_date}"

    try:
        session.get(url, timeout=20, headers=HEADERS)
    except requests.RequestException as e:
        return [], url, note, f"GET 首頁失敗: {e!r}"

    rows = []
    for page in range(1, MAX_PAGES + 1):
        try:
            resp = session.post(
                url,
                data={"type": type_code, "page": str(page), "pagesize": ""},
                timeout=20,
                headers=HEADERS,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            return rows, url, note, f"第 {page} 頁 POST 失敗: {e!r}"

        page_rows = ROW_RE.findall(resp.text)
        if not page_rows:
            break
        rows.extend(page_rows)
        if len(page_rows) < PAGE_SIZE:
            break
    else:
        return rows, url, note, (
            f"分頁抓到 {MAX_PAGES} 頁上限仍未結束，可能未抓完整，需人工確認"
        )

    if not rows:
        return rows, url, note, "抓到 0 筆，頁面結構可能改版，需人工確認(manual fallback)"

    return rows, url, note, None


def write_csv(path, segment_label, rows, source_url, note):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["segment", "inst_code", "official_name", "source_url", "fetched_at_note"])
        for inst_code, official_name in rows:
            w.writerow([segment_label, inst_code, official_name, source_url, note])


# baseline 沒有獨立的 bb_china_bank.csv；「大陸地區銀行在臺分行」3 筆歷史上被
# 附加在 bb_foreign_bank.csv 檔尾（同檔內用 segment 欄區分）。為了 diff 時不誤報
# 「無既有快照」，回退去該檔內按 segment 篩出對應列。
BASELINE_FALLBACK = {
    "bb_china_bank.csv": ("bb_foreign_bank.csv", "大陸地區銀行在臺分行"),
}


def load_baseline(filename):
    """回傳 (baseline_rows_or_None, 實際讀取的來源檔名)。"""
    path = _paths.baseline(filename)
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            return list(csv.DictReader(f)), filename

    fallback = BASELINE_FALLBACK.get(filename)
    if fallback:
        fb_filename, segment_filter = fallback
        fb_path = _paths.baseline(fb_filename)
        if os.path.exists(fb_path):
            with open(fb_path, encoding="utf-8-sig") as f:
                rows = [r for r in csv.DictReader(f) if r.get("segment") == segment_filter]
            if rows:
                return rows, f"{fb_filename}(segment={segment_filter})"

    return None, None


def diff_rows(baseline, baseline_src, new_rows):
    """baseline: list[dict] (既有快照, 可能 None＝無快照); new_rows: list[(inst_code, official_name)]"""
    if baseline is None:
        return f"無既有快照可比對(此名冊為新建立；本次抓到 {len(new_rows)} 筆)"

    src_note = f"[baseline 來源: {baseline_src}] " if baseline_src else ""
    base_map = {r["inst_code"]: r["official_name"] for r in baseline}
    new_map = {code: name for code, name in new_rows}

    added = [c for c in new_map if c not in base_map]
    removed = [c for c in base_map if c not in new_map]
    renamed = [c for c in new_map if c in base_map and base_map[c] != new_map[c]]

    parts = [f"列數 {len(baseline)} -> {len(new_map)}"]
    if added:
        parts.append("新增: " + "; ".join(f"{c} {new_map[c]}" for c in added))
    if removed:
        parts.append("消失: " + "; ".join(f"{c} {base_map[c]}" for c in removed))
    if renamed:
        parts.append(
            "名稱變動: " + "; ".join(f"{c}: {base_map[c]} -> {new_map[c]}" for c in renamed)
        )
    if not (added or removed or renamed):
        parts.append("內容無差異")
    return src_note + "; ".join(parts)


def run(run_date=None):
    run_date = run_date or date.today().isoformat()
    out_dir = _paths.raw_dir(run_date)

    session = requests.Session()
    results = []

    for filename, label, type_code, expect in SEGMENTS:
        rows, url, note, err = fetch_type_list(session, type_code, label, run_date)
        out_path = os.path.join(out_dir, filename)
        full_note = note if not err else f"{note}; ERROR/manual-fallback: {err}"
        write_csv(out_path, label, rows, url, full_note)

        baseline, baseline_src = load_baseline(filename)
        d = diff_rows(baseline, baseline_src, rows)
        results.append(
            dict(label=label, filename=filename, rows=len(rows), path=out_path,
                 diff=d, error=err, expect=expect)
        )
        print(f"[{label}] rows={len(rows)} (預期約{expect}) error={err}")
        time.sleep(0.5)  # 對外部政府網站禮貌性節流

    # ---- 報告 ----
    report_path = os.path.join(out_dir, "fetch_banking_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# 金管會銀行局名冊 fetch 報告 — {run_date}\n\n")
        f.write(
            "方法：requests GET(首頁) + POST(逐頁，模擬 `bankingform` 的 JS "
            "`list(page)` 送出行為)。經 2026-07-14 實測，banking.gov.tw 名冊頁"
            "為 server-rendered HTML，不需要 Playwright 渲染即可完整取得資料"
            "（含分頁，7 個 segment 皆同一套 FscSearch_BankType.jsp 樣板，"
            "差異僅 type 參數）。\n\n"
        )
        for r in results:
            f.write(f"## {r['label']} ({r['filename']})\n\n")
            f.write(f"- rows: {r['rows']}（預期約 {r['expect']}）\n")
            f.write(f"- 輸出: {r['path']}\n")
            f.write(f"- 與既有快照 diff: {r['diff']}\n")
            if r["error"]:
                f.write(f"- **ERROR / manual fallback 需人工介入**: {r['error']}\n")
            f.write("\n")
    print(f"報告已寫入 {report_path}")

    return results


def main():
    ap = argparse.ArgumentParser(description="金管會銀行局 7 份名冊 fetcher（交付版）")
    ap.add_argument(
        "--run-date",
        default=date.today().isoformat(),
        help="抓取日期（YYYY-MM-DD），決定輸出目錄 raw/<日期>/ 與 fetched_at_note，預設今天",
    )
    args = ap.parse_args()
    run(args.run_date)


if __name__ == "__main__":
    main()
