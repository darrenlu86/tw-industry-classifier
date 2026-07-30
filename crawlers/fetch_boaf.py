#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fetch_boaf.py — 農業部農業金融署（BOAF）機構名冊 fetcher（交付版，可獨立執行、可重跑）

本檔為 industry_classifier 交付版：抓取／解析邏輯與來源 URL 皆與原產線版逐字相同，
僅把路徑改走 `_paths` 模組、把寫死的抓取日期改成 `--run-date` 參數。

來源（兩個獨立頁面，皆為農業部農業金融署官網 www.afna.gov.tw，server-rendered 靜態表格）：

  1) 農漁會信用部及其分部基本資料查詢（一手權威來源，決定名單範圍與 inst_code）
     https://www.afna.gov.tw/list.php?theme=credit_department&subtheme=
     逐頁 drlPageSize=200 抓取（實測現況 6 頁、約 1,146～1,148 筆含分部/辦事處），
     依「總機構代號」去重，僅保留機構名稱不含「分部」「辦事處」之列 = 信用部總部（法人）。
     依名稱含「漁會」/「農會」拆成 fishers_credit_dept / farmers_credit_dept 兩個 segment。

  2) 全國農業金庫（單一機構，農金署未在自身頁面公告金融機構代號，inst_code 依既有慣例留空）
     https://www.afna.gov.tw/list.php?theme=web_structure&subtheme=78
     （農業金融機構財務資訊揭露列表，逐月揭露「全國農業金庫財業務資料」）
     僅驗證頁面仍揭露「全國農業金庫」字樣（機構仍在運作、名稱未變），不做表格解析。

輸出 schema（5 欄，與 baseline 快照完全相同）：
  segment, inst_code, official_name, source_url, fetched_at_note

寫出位置（`_paths.raw_dir(run_date)`，絕不覆蓋 baseline 快照）：
  crawlers/raw/<run-date>/boaf_farmers.csv
  crawlers/raw/<run-date>/boaf_fishers.csv
  crawlers/raw/<run-date>/boaf_agri_bank.csv
  crawlers/raw/<run-date>/boaf_diff_report.txt   （與 baseline 快照逐筆 diff）

Diff 比對基準（baseline，`_paths.baseline(...)`）：
  crawlers/baseline/boaf_farmers.csv
  crawlers/baseline/boaf_fishers.csv
  crawlers/baseline/boaf_agri_bank.csv

已知覆蓋率限制（誠實說明，勿當成完整名冊）：
  - 本來源只到「信用部總部（法人層級）」：頁面上的分部／辦事處列已刻意排除，
    故本檔輸出不含分支機構，家數必然少於官網原始列數。
  - 全國農業金庫的 `inst_code` **留空**：農業金融署官網未在自身頁面公告其「金融機構代號」，
    不採用維基百科／財金公司等第三方來源湊碼。機構全名依官網揭露頁面標題用字「全國農業金庫」。
  - 本來源不提供統一編號，統編需另行由 tax_id 對照流程補齊。
  - Fallback 原則：requests 抓不到 → 改用 Playwright 渲染同一 URL 後再套同一套解析邏輯
    （此頁面實測為 server-rendered 靜態表格，非 JS 動態產生，Playwright 僅作備援手段）。
    requests 與 Playwright 皆失敗 → 如實記錄失敗原因至 issues / diff report，
    **不假裝成功、不產生假資料**，並提示人工至官網核對的路徑（manual fallback）。

用法：
  py -3.12 fetch_boaf.py
  py -3.12 fetch_boaf.py --run-date 2026-07-30
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths

import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------------------
# 常數
# ---------------------------------------------------------------------------

CREDIT_DEPT_PAGE_URL_TMPL = (
    "https://www.afna.gov.tw/list.php?theme=credit_department&subtheme="
    "&drlPageSize=200&page={page}"
)
CREDIT_DEPT_BASE_URL = "https://www.afna.gov.tw/list.php?theme=credit_department&subtheme="
AGRI_BANK_LIST_URL = "https://www.afna.gov.tw/list.php?theme=web_structure&subtheme=78"
AGRI_BANK_VIEW_URL_EXAMPLE = "https://www.afna.gov.tw/view.php?theme=web_structure&id=5800"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; industry-classifier-fetch-boaf/1.0)"}

SCHEMA = ["segment", "inst_code", "official_name", "source_url", "fetched_at_note"]


# ---------------------------------------------------------------------------
# 抓取（requests 優先，Playwright 為 fallback）
# ---------------------------------------------------------------------------


def _get_via_requests(url: str, timeout: int = 20) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def _get_via_playwright(url: str, timeout_ms: int = 20000) -> str:
    """requests 失敗時的 fallback：Playwright 開瀏覽器渲染後取 HTML。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, timeout=timeout_ms)
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            html = page.content()
        finally:
            browser.close()
    return html


def fetch_html(url: str, label: str) -> tuple[str, list[str]]:
    """requests 優先，失敗才 fallback 到 Playwright；兩者皆失敗則丟出例外。"""
    attempts: list[str] = []
    try:
        html = _get_via_requests(url)
        attempts.append(f"[OK] requests：{label}")
        return html, attempts
    except Exception as e:  # noqa: BLE001 - 有意寬鬆捕捉，記錄後嘗試下一手段
        attempts.append(f"[FAIL] requests：{label} — {e!r}")
    try:
        html = _get_via_playwright(url)
        attempts.append(f"[OK] playwright fallback：{label}")
        return html, attempts
    except Exception as e:  # noqa: BLE001
        attempts.append(f"[FAIL] playwright fallback：{label} — {e!r}")
    raise RuntimeError(
        f"fetch_html 兩種手段皆失敗（{label}）："
        + " ｜ ".join(attempts)
    )


# ---------------------------------------------------------------------------
# 解析：農漁會信用部及其分部基本資料查詢
# ---------------------------------------------------------------------------


def get_total_pages(html: str) -> int:
    m = re.search(r"共(\d+)頁", html)
    if not m:
        raise RuntimeError(
            "解析總頁數失敗（找不到「共N頁」字樣，頁面結構可能已變更，需人工至官網核對）"
        )
    return int(m.group(1))


def parse_credit_department_page(html: str) -> list[tuple[str, str, str]]:
    """解析單頁 rwd-table，回傳 [(official_name, inst_code, city), ...]（含分部/辦事處列）。"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="rwd-table")
    if table is None:
        raise RuntimeError(
            "找不到 class=rwd-table 的表格（頁面結構可能已變更，需人工至官網核對）"
        )
    tbody = table.find("tbody")
    rows: list[tuple[str, str, str]] = []
    for tr in (tbody or table).find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        a = tds[1].find("a")
        name = a.get_text(strip=True) if a else tds[1].get_text(strip=True)
        code = tds[2].get_text(strip=True)
        city = tds[3].get_text(strip=True).replace("\xa0", "").strip()
        if not name or not code:
            continue
        rows.append((name, code, city))
    return rows


def fetch_credit_department_roster(run_date: str) -> tuple[list[dict], list[dict], list[str]]:
    """抓取農漁會信用部總部名冊，回傳 (farmers_rows, fishers_rows, notes)。"""
    notes: list[str] = []

    first_html, attempts = fetch_html(
        CREDIT_DEPT_PAGE_URL_TMPL.format(page=1), "credit_department page=1"
    )
    notes.extend(attempts)
    total_pages = get_total_pages(first_html)
    notes.append(f"drlPageSize=200，共 {total_pages} 頁")

    all_rows = list(parse_credit_department_page(first_html))
    for page in range(2, total_pages + 1):
        html, attempts = fetch_html(
            CREDIT_DEPT_PAGE_URL_TMPL.format(page=page),
            f"credit_department page={page}",
        )
        notes.extend(attempts)
        all_rows.extend(parse_credit_department_page(html))

    notes.append(f"合計原始列數（含分部/辦事處）＝{len(all_rows)}")

    # 僅保留機構名稱不含「分部」「辦事處」之列 = 信用部總部（法人層級）
    hq_rows = [r for r in all_rows if ("分部" not in r[0] and "辦事處" not in r[0])]
    notes.append(f"去除分部/辦事處後（信用部總部，一法人一列）＝{len(hq_rows)}")

    # 以「總機構代號」去重完整性檢查（不應有重複）
    seen: dict[str, str] = {}
    dup_codes = []
    for name, code, _city in hq_rows:
        if code in seen and seen[code] != name:
            dup_codes.append((code, seen[code], name))
        seen[code] = name
    if dup_codes:
        notes.append(f"警告：總機構代號重複（同代號對到不同名稱）{len(dup_codes)} 筆：{dup_codes}")

    source_url_note = (
        f"{CREDIT_DEPT_BASE_URL} (共{total_pages}頁, drlPageSize=200 一次抓取{len(all_rows)}筆; "
        f"已排除機構名稱含「分部」或「辦事處」之分支機構列, 每一總機構代號僅保留1筆總部列)"
    )
    fetched_at_note = (
        f"{run_date} by requests+BeautifulSoup 靜態頁面抓取"
        f"(伺服器端渲染表格, 非JS動態); page=1..{total_pages} drlPageSize=200"
    )

    farmers_rows: list[dict] = []
    fishers_rows: list[dict] = []
    for name, code, _city in hq_rows:
        is_fishers = "漁會" in name
        row = {
            "segment": "fishers_credit_dept" if is_fishers else "farmers_credit_dept",
            "inst_code": code,
            "official_name": name,
            "source_url": source_url_note,
            "fetched_at_note": fetched_at_note,
        }
        (fishers_rows if is_fishers else farmers_rows).append(row)

    notes.append(f"分類結果：farmers_credit_dept={len(farmers_rows)}, fishers_credit_dept={len(fishers_rows)}")

    return farmers_rows, fishers_rows, notes


# ---------------------------------------------------------------------------
# 解析：全國農業金庫
# ---------------------------------------------------------------------------


def fetch_agri_bank_roster(run_date: str) -> tuple[list[dict], list[str]]:
    """核對全國農業金庫仍在官網揭露，回傳單列 (rows, notes)。"""
    notes: list[str] = []
    html, attempts = fetch_html(AGRI_BANK_LIST_URL, "agri_bank web_structure list")
    notes.extend(attempts)

    if "全國農業金庫" not in html:
        raise RuntimeError(
            "頁面已不含「全國農業金庫」字樣，需人工至官網核對機構是否更名/裁撤（不可假設仍存在）"
        )
    notes.append("已確認頁面仍揭露「全國農業金庫財業務資料」逐月揭露項目")

    row = {
        "segment": "agri_bank",
        "inst_code": "",
        "official_name": "全國農業金庫",
        "source_url": (
            f"{AGRI_BANK_LIST_URL} (農業金融機構財務資訊揭露列表；內含逐月「全國農業金庫財業務資料」揭露項目，"
            f"例 {AGRI_BANK_VIEW_URL_EXAMPLE})"
        ),
        "fetched_at_note": (
            f"{run_date} by requests+BeautifulSoup 頁面核對；農業金融署官網未在自身頁面公告全國農業金庫之"
            f"「金融機構代號」，故 inst_code 留空（不採用維基百科/財金公司等第三方來源湊碼）；"
            f"機構全名依官網揭露頁面標題用字「全國農業金庫」"
        ),
    }
    return [row], notes


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SCHEMA)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def read_existing_csv(path: Path) -> list[dict] | None:
    if not path.exists():
        return None
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Diff：本次快照 vs crawlers/baseline/ 既有快照
# ---------------------------------------------------------------------------


def diff_roster(name: str, new_rows: list[dict], baseline_path: Path) -> dict:
    baseline_rows = read_existing_csv(baseline_path)
    if baseline_rows is None:
        return {
            "name": name,
            "rows": len(new_rows),
            "baseline_rows": None,
            "text": f"{name}：無既有快照可比對（baseline 不存在：{baseline_path}）",
        }

    baseline_by_code = {r["inst_code"]: r["official_name"] for r in baseline_rows}
    new_by_code = {r["inst_code"]: r["official_name"] for r in new_rows}

    added = sorted(set(new_by_code) - set(baseline_by_code))
    removed = sorted(set(baseline_by_code) - set(new_by_code))
    renamed = [
        (code, baseline_by_code[code], new_by_code[code])
        for code in (set(new_by_code) & set(baseline_by_code))
        if baseline_by_code[code] != new_by_code[code]
    ]

    lines = [
        f"## {name}",
        f"既有快照 {len(baseline_rows)} 列 → 本次快照 {len(new_rows)} 列（差 {len(new_rows) - len(baseline_rows):+d}）",
    ]
    lines.append(
        "新增機構：無" if not added
        else f"新增機構 {len(added)} 筆：" + "; ".join(f"{c}={new_by_code[c]}" for c in added)
    )
    lines.append(
        "消失機構：無" if not removed
        else f"消失機構 {len(removed)} 筆：" + "; ".join(f"{c}={baseline_by_code[c]}" for c in removed)
    )
    lines.append(
        "名稱變更：無" if not renamed
        else f"名稱變更 {len(renamed)} 筆：" + "; ".join(f"{c}: {old}→{new}" for c, old, new in renamed)
    )

    return {
        "name": name,
        "rows": len(new_rows),
        "baseline_rows": len(baseline_rows),
        "text": "\n".join(lines),
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取農業部農業金融署（BOAF）農漁會信用部與全國農業金庫名冊"
    )
    parser.add_argument(
        "--run-date",
        default=date.today().isoformat(),
        help="抓取日（yyyy-mm-dd），決定輸出目錄 raw/<run-date>/ 與 fetched_at 註記；預設今日",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    fetched_at = args.run_date
    raw_dir = Path(_paths.raw_dir(fetched_at))

    results: list[tuple[str, list[dict], Path, Path]] = []
    issues: list[str] = []
    all_notes: list[str] = []

    # 1) 農漁會信用部總部名冊
    try:
        farmers_rows, fishers_rows, notes = fetch_credit_department_roster(fetched_at)
        all_notes.extend(notes)

        farmers_path = raw_dir / "boaf_farmers.csv"
        fishers_path = raw_dir / "boaf_fishers.csv"
        write_csv(farmers_path, farmers_rows)
        write_csv(fishers_path, fishers_rows)

        results.append(
            ("boaf_farmers", farmers_rows, farmers_path, Path(_paths.baseline("boaf_farmers.csv")))
        )
        results.append(
            ("boaf_fishers", fishers_rows, fishers_path, Path(_paths.baseline("boaf_fishers.csv")))
        )
    except Exception as e:  # noqa: BLE001 - 需要如實記錄失敗原因，不可中斷整支腳本
        issues.append(
            "農漁會信用部名冊抓取失敗："
            f"{e!r}（manual fallback：沿用 baseline 快照 "
            "crawlers/baseline/boaf_farmers.csv 與 boaf_fishers.csv；"
            "需人工至 https://www.afna.gov.tw/list.php?theme=credit_department&subtheme= 核對）"
        )

    # 2) 全國農業金庫
    try:
        agri_rows, notes = fetch_agri_bank_roster(fetched_at)
        all_notes.extend(notes)

        agri_path = raw_dir / "boaf_agri_bank.csv"
        write_csv(agri_path, agri_rows)
        results.append(
            ("boaf_agri_bank", agri_rows, agri_path, Path(_paths.baseline("boaf_agri_bank.csv")))
        )
    except Exception as e:  # noqa: BLE001
        issues.append(
            "全國農業金庫頁面核對失敗："
            f"{e!r}（manual fallback：沿用 baseline 快照 crawlers/baseline/boaf_agri_bank.csv；"
            "需人工至 https://www.afna.gov.tw/list.php?theme=web_structure&subtheme=78 核對）"
        )

    # Diff 報告
    report_lines = [f"# fetch_boaf 抓取與 diff 報告 — {fetched_at}", ""]
    report_lines.append("## 抓取過程紀錄")
    report_lines.extend(f"- {n}" for n in all_notes)
    report_lines.append("")

    for name, rows, _path, baseline_path in results:
        d = diff_roster(name, rows, baseline_path)
        report_lines.append(d["text"])
        report_lines.append("")

    if issues:
        report_lines.append("## 失敗紀錄（manual fallback）")
        report_lines.extend(f"- {i}" for i in issues)
        report_lines.append("")

    print("\n".join(report_lines))

    if results:
        raw_dir.mkdir(parents=True, exist_ok=True)
        diff_path = raw_dir / "boaf_diff_report.txt"
        with open(diff_path, "w", encoding="utf-8") as f:
            f.write("\n".join(report_lines))
        print(f"[written] {diff_path}")

    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
