#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fetch_leasing.py — 台北市租賃商業同業公會會員名冊 fetcher（交付版，可獨立執行、可重跑）

本檔為交付版：抓取邏輯、解析邏輯與來源 URL 與產線版逐字相同，僅把路徑改走 `_paths`
模組、把寫死的抓取日期改成 `--run-date` 參數，因此可從任何目錄直接執行，不依賴
原產線的目錄層數。

融資租賃業無主管機關名冊，本來源為「租賃公會名錄（非主管機關）」，下游收錄權威主檔時
須如實標註來源性質。

兩個來源頁面（皆為 tpeleasing.com 官網、server-rendered 靜態 HTML，非 JS 動態渲染）：

  來源 A - member_info1.html（金保法納管名單專頁，信心度最高，公會自行整理的 4 份序號式清單）
    https://tpeleasing.com/member_info1.html
    4 個區段：金保法第一階段納管會員公司 / 金保法第二階段納管會員公司 /
              有簽署BNPL自律規範會員公司 / 承作中古車輛交易業務會員公司
    HTML 結構為 `<td colspan="2" class="t11Gray">區段名&lt;&lt;</td>` 後接
    `<td width="20%">序號 公司名稱<br>...</td>`，逐行 `N 公司名稱` 純文字、可正規解析。

  來源 B - member_list.htm（完整會員名錄頁，約 38 個會員資訊區塊）
    https://tpeleasing.com/member_list.htm
    僅擷取明文標示「公司名稱 : 」欄位者（約 14 筆）；其餘因公司名稱以 logo 圖檔呈現而
    無法自動辨識，如實記錄「未能自動辨識區塊數」於報告，不臆造名稱。

**本來源已知的覆蓋率限制（如實揭露，非腳本 bug）**：
  台北市租賃商業同業公會官網並非機關名冊，其「會員名錄」頁面（member_list.htm）約 38 個
  會員資訊區塊中，公司名稱多數以 logo 圖檔（無 ALT 文字）呈現，僅約 14 個區塊有明文
  「公司名稱：」標籤可供文字解析；其餘需人工（或 OCR）比對地址/負責人/資本額等特徵反查
  GCIS，此非本腳本範圍（那是 baseline 建置時的一次性人工研究工作，非「可重跑的自動化
  抓取」能承擔的步驟）。本腳本僅做「抓取→解析可自動辨識部分→寫 raw 快照→與 baseline
  diff」，**不做 tax_id 補全**（統編補全由 tax_id 補全流程負責）。

輸出：
  raw 快照     crawlers/raw/<run-date>/leasing.csv
  異動報告     crawlers/raw/<run-date>/leasing_fetch_report.md
  raw 快照的 schema 為抓取層中繼格式（6 欄）：
  segment,list_seq,official_name,inst_code,source_url,fetched_at_note

baseline：
  crawlers/baseline/leasing.csv（38 列人工彙整快照，10 欄，含已核實的 tax_id 與
  authority/status/notes 等欄位）。**baseline 的 schema 與本腳本輸出不同（10 欄 vs 6 欄）**，
  兩者無法逐欄比對，故 diff 只做 `official_name` 的集合比對（相符／baseline 有但抓不到／
  抓到但 baseline 沒有）。**本腳本絕不覆蓋 baseline**。

用法：
  python fetch_leasing.py                      # 抓取日＝今天
  python fetch_leasing.py --run-date 2026-07-30
"""

from __future__ import annotations

import argparse
import csv
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths

import requests

sys.stdout.reconfigure(encoding="utf-8")

BASELINE_FILENAME = "leasing.csv"

SOURCE_INFO1_URL = "https://tpeleasing.com/member_info1.html"
SOURCE_LIST_URL = "https://tpeleasing.com/member_list.htm"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 industry_classifier-fetch_leasing/1.0"
    )
}

SCHEMA = ["segment", "list_seq", "official_name", "inst_code", "source_url", "fetched_at_note"]


# ---------------------------------------------------------------------------
# 抓取（requests 優先，Playwright 為 fallback；兩者皆失敗才回報 manual fallback）
# ---------------------------------------------------------------------------
def _get_via_requests(url: str, timeout: int = 20) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def _get_via_playwright(url: str, timeout_ms: int = 20000) -> str:
    """requests 失敗時的 fallback：Playwright 開瀏覽器渲染後取 HTML
    （此頁面實測為 server-rendered 靜態 HTML，Playwright 僅作備援手段，
    不代表頁面本身需要 JS 才能取得內容）。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, timeout=timeout_ms)
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            html = page.content()
        finally:
            browser.close()
    return html


def fetch_html(url: str, label: str) -> tuple[str | None, list[str], str | None]:
    """回傳 (html_or_None, attempts, error_or_None)。requests 優先，失敗才試 Playwright。"""
    attempts: list[str] = []
    try:
        html = _get_via_requests(url)
        attempts.append(f"[OK] requests：{label}")
        return html, attempts, None
    except Exception as e:  # noqa: BLE001 — 對外部站台需寬容捕捉後改走 fallback
        attempts.append(f"[FAIL] requests：{label} — {e!r}")
    try:
        html = _get_via_playwright(url)
        attempts.append(f"[OK] playwright fallback：{label}")
        return html, attempts, None
    except Exception as e:  # noqa: BLE001
        attempts.append(f"[FAIL] playwright fallback：{label} — {e!r}")
    err = f"兩種手段皆失敗（requests + Playwright）：{label}"
    return None, attempts, err


# ---------------------------------------------------------------------------
# 解析：來源 A — member_info1.html（4 份序號式官方整理名單）
# ---------------------------------------------------------------------------
SECTION_RE = re.compile(
    r'<td colspan="2" class="t11Gray">([^<]+?)&lt;&lt;</td>.*?<td width="20%">(.*?)</td>',
    re.S,
)
LINE_RE = re.compile(r"^(\d+)\s+(.+?)(?:\s+V)?$")


def parse_member_info1(html: str, run_date: str) -> tuple[list[dict], list[str]]:
    """回傳 (rows, notes)。若找不到任何區段，notes 會如實記錄頁面可能已改版。"""
    notes: list[str] = []
    sections = SECTION_RE.findall(html)
    if not sections:
        notes.append(
            "找不到任何符合 `<td colspan=\"2\" class=\"t11Gray\">區段名&lt;&lt;</td>` "
            "樣板的區段，頁面結構可能已改版，需人工至官網核對"
        )
        return [], notes

    rows: list[dict] = []
    for label, content in sections:
        label = label.strip()
        lines = re.split(r"<br\s*/?>", content)
        n_in_section = 0
        for raw_line in lines:
            line = re.sub(r"<[^>]+>", "", raw_line).strip()
            line = line.replace("&nbsp;", "").strip()
            if not line:
                continue
            m = LINE_RE.match(line)
            if not m:
                notes.append(f"區段「{label}」有無法解析的行，原樣保留供人工核對：{line!r}")
                continue
            seq, name = m.group(1), m.group(2).strip()
            rows.append(
                {
                    "segment": label,
                    "list_seq": seq,
                    "official_name": name,
                    "inst_code": "",
                    "source_url": SOURCE_INFO1_URL,
                    "fetched_at_note": (
                        f"{run_date} 公會官網「金保法納管會員公司」專頁自整理名單，逐行"
                        "『序號 公司名稱』純文字解析（server-rendered 靜態 HTML）"
                    ),
                }
            )
            n_in_section += 1
        notes.append(f"區段「{label}」解析 {n_in_section} 筆")

    return rows, notes


# ---------------------------------------------------------------------------
# 解析：來源 B — member_list.htm（僅擷取明文「公司名稱：」標籤者）
# ---------------------------------------------------------------------------
NAME_LABEL_RE = re.compile(r"公司名稱\s*[:：]\s*([^<\r\n]+)")
PHONE_RE = re.compile(r"電\s*話\s*[:：]")


def parse_member_list(html: str, run_date: str) -> tuple[list[dict], list[str]]:
    """回傳 (rows, notes)。僅擷取明文標示「公司名稱：」欄位的會員區塊。"""
    notes: list[str] = []

    total_blocks = len(PHONE_RE.findall(html))
    names = [m.group(1).strip() for m in NAME_LABEL_RE.finditer(html)]

    if total_blocks == 0:
        notes.append(
            "找不到任何「電話：」欄位樣板（會員資訊區塊判斷依據），"
            "頁面結構可能已改版，需人工至官網核對"
        )
    else:
        notes.append(f"頁面共偵測到約 {total_blocks} 個會員資訊區塊（以「電話：」欄位計數）")

    notes.append(
        f"其中 {len(names)} 個區塊有明文「公司名稱：」標籤可自動解析；"
        f"其餘約 {max(total_blocks - len(names), 0)} 個區塊之公司名稱以 logo 圖檔呈現"
        "（無 ALT 文字），本腳本不臆造名稱，需人工（或 OCR）比對地址/負責人/資本額"
        "等特徵反查 GCIS 確認身分"
    )

    rows = []
    for name in names:
        rows.append(
            {
                "segment": "會員名錄(官網公司名稱標籤可辨識者)",
                "list_seq": "",
                "official_name": name,
                "inst_code": "",
                "source_url": SOURCE_LIST_URL,
                "fetched_at_note": (
                    f"{run_date} 公會官網「會員名錄」頁面明文「公司名稱：」標籤解析"
                    "（server-rendered 靜態 HTML；多數會員區塊之公司名稱以 logo 圖檔呈現，"
                    "無法以此法解析，見報告未辨識數）"
                ),
            }
        )
    return rows, notes


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------
def write_csv(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SCHEMA)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def load_baseline_names(path: str) -> list[dict] | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Diff：本次自動抓取（去重後的公司名清單）vs baseline leasing.csv
# baseline 為 10 欄人工彙整 schema、本腳本輸出為 6 欄抓取層 schema，兩者不可逐欄比對，
# 故只做 official_name 的集合 diff。
# ---------------------------------------------------------------------------
def diff_against_baseline(new_rows: list[dict], baseline_rows: list[dict] | None, baseline_path: str) -> dict:
    new_names = sorted({r["official_name"] for r in new_rows})

    if baseline_rows is None:
        return {
            "baseline_rows": None,
            "new_distinct": len(new_names),
            "matched": [],
            "missing_from_fetch": [],
            "new_not_in_baseline": new_names,
            "text": (
                f"無 baseline 可比對（{baseline_path} 不存在）；"
                f"本次自動抓取去重後 {len(new_names)} 家"
            ),
        }

    baseline_names = sorted({r["official_name"] for r in baseline_rows})
    new_set, base_set = set(new_names), set(baseline_names)

    matched = sorted(new_set & base_set)
    missing_from_fetch = sorted(base_set - new_set)
    new_not_in_baseline = sorted(new_set - base_set)

    lines = [
        f"baseline（{baseline_path}，人工彙整 10 欄）：{len(baseline_names)} 家（distinct official_name）",
        f"本次自動抓取（去重後）：{len(new_names)} 家",
        f"兩者相符：{len(matched)} 家",
    ]
    if missing_from_fetch:
        lines.append(
            f"baseline 有但本次自動抓取抓不到（{len(missing_from_fetch)} 家，多為 logo 圖檔"
            "無法自動辨識者，或 baseline 另有 FSC 公告等非 tpeleasing.com 來源者，非機構真的消失）："
        )
        lines.extend(f"  - {n}" for n in missing_from_fetch)
    else:
        lines.append("baseline 有但本次抓不到：無")

    if new_not_in_baseline:
        lines.append(
            f"本次抓到但 baseline 沒有相同字串者（{len(new_not_in_baseline)} 筆，"
            "可能為官網名稱與 baseline 人工核實後正式名稱不同、或真正新增會員，需人工核對）："
        )
        lines.extend(f"  - {n}" for n in new_not_in_baseline)
    else:
        lines.append("本次抓到但 baseline 沒有相同字串者：無")

    return {
        "baseline_rows": len(baseline_names),
        "new_distinct": len(new_names),
        "matched": matched,
        "missing_from_fetch": missing_from_fetch,
        "new_not_in_baseline": new_not_in_baseline,
        "text": "\n".join(lines),
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="台北市租賃商業同業公會會員名冊 fetcher（交付版）")
    ap.add_argument(
        "--run-date",
        default=datetime.date.today().isoformat(),
        help="抓取日（yyyy-mm-dd），決定 raw 輸出目錄與 fetched_at_note；預設今天",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    fetched_at = args.run_date
    raw_dir = _paths.raw_dir(fetched_at)
    baseline_path = _paths.baseline(BASELINE_FILENAME)

    report_lines = [
        f"# fetch_leasing.py 抓取與 diff 報告 — {fetched_at}",
        "",
        "來源：台北市租賃商業同業公會官網（非主管機關名冊，下游收錄權威主檔時須如實標註"
        "「租賃公會名錄（非主管機關）」）。",
        "",
    ]

    all_new_rows: list[dict] = []
    issues: list[str] = []
    any_ok = False

    # ---- 來源 A：member_info1.html ----
    html_a, attempts_a, err_a = fetch_html(SOURCE_INFO1_URL, "member_info1.html")
    report_lines.append(f"## 來源A：{SOURCE_INFO1_URL}")
    report_lines.extend(f"- {a}" for a in attempts_a)
    if html_a is not None:
        rows_a, notes_a = parse_member_info1(html_a, fetched_at)
        report_lines.extend(f"- {n}" for n in notes_a)
        if rows_a:
            any_ok = True
            all_new_rows.extend(rows_a)
            report_lines.append(f"- 小計：{len(rows_a)} 列（4 區段，可含跨區段重複公司）")
        else:
            issues.append("來源A（member_info1.html）已連線但解析出 0 筆，需人工核對頁面版面")
    else:
        issues.append(f"來源A（member_info1.html）抓取失敗：{err_a}")
        report_lines.append(f"- **狀態：失敗（manual fallback 待人工處理）**：{err_a}")
    report_lines.append("")

    # ---- 來源 B：member_list.htm ----
    html_b, attempts_b, err_b = fetch_html(SOURCE_LIST_URL, "member_list.htm")
    report_lines.append(f"## 來源B：{SOURCE_LIST_URL}")
    report_lines.extend(f"- {a}" for a in attempts_b)
    if html_b is not None:
        rows_b, notes_b = parse_member_list(html_b, fetched_at)
        report_lines.extend(f"- {n}" for n in notes_b)
        if rows_b:
            any_ok = True
            all_new_rows.extend(rows_b)
        report_lines.append(f"- 小計：{len(rows_b)} 列（僅明文「公司名稱：」標籤者）")
    else:
        issues.append(f"來源B（member_list.htm）抓取失敗：{err_b}")
        report_lines.append(f"- **狀態：失敗（manual fallback 待人工處理）**：{err_b}")
    report_lines.append("")

    # ---- 寫 raw 快照 ----
    snapshot_path = None
    if all_new_rows:
        snapshot_path = os.path.join(raw_dir, "leasing.csv")
        write_csv(snapshot_path, all_new_rows)
        report_lines.append(f"## 輸出\n\n- raw 快照：{snapshot_path}（{len(all_new_rows)} 列，"
                             f"schema：{','.join(SCHEMA)}）\n")
    else:
        report_lines.append("## 輸出\n\n- 兩份來源皆未解析出任何列，未寫出 raw 快照（manual fallback）。\n")

    # ---- Diff vs baseline（只比 official_name 集合，schema 不同無法逐欄比） ----
    baseline_rows = load_baseline_names(baseline_path)
    diff = diff_against_baseline(all_new_rows, baseline_rows, baseline_path)
    report_lines.append(f"## Diff vs baseline（{baseline_path}；僅比對 official_name 集合）\n")
    report_lines.append(diff["text"])
    report_lines.append("")

    if issues:
        report_lines.append("## 失敗/需人工處理紀錄（manual fallback）")
        report_lines.extend(f"- {i}" for i in issues)
        report_lines.append("")

    report_text = "\n".join(report_lines)
    print(report_text)

    if any_ok:
        os.makedirs(raw_dir, exist_ok=True)
        report_path = os.path.join(raw_dir, "leasing_fetch_report.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_text)
        print(f"\n[written] {report_path}")

    return 0 if any_ok and not issues else (0 if any_ok else 1)


if __name__ == "__main__":
    sys.exit(main())
