# -*- coding: utf-8 -*-
"""fetch_insurance.py — 金管會保險局權威名冊 fetcher（交付版）

本檔為可獨立執行的交付版：抓取邏輯、解析邏輯與來源 URL 皆與原產線腳本逐字相同，
只把路徑改走 `_paths` 模組、把寫死的快照日期改成 `--run-date` 參數。
職責僅止於「抓取 → 解析 → 輸出與 baseline 同欄位結構的 CSV」，
**不做統編補全**（補全是另一支腳本的職責）。
本腳本**絕不寫入 `crawlers/baseline/`**——那是 diff 比對的基準線，只讀不寫。

涵蓋範圍：人身／財產／再保險（含外商在臺分公司）＋ 保經代 ＋ 保經（子集，best-effort）

來源（沿用既有 insurance_report.md、insurance_brokers_report.md 與 baseline 快照已記錄的來源）：

1. 人身保險（insurance_life，含外商人身保險在臺分公司）
   https://www.ib.gov.tw/ch/home.jsp?id=181&parentpath=0,9,179
2. 財產保險（insurance_nonlife，含外商財產保險在臺分公司）
   https://www.ib.gov.tw/ch/home.jsp?id=180&parentpath=0,9,179
3. 再保險（insurance_reinsurer，含外商再保險在臺分公司）
   https://www.ib.gov.tw/ch/home.jsp?id=206&parentpath=0,9,179
   （以上三頁為金管會保險局官網『機構名錄』區靜態 HTML，2026-07-14 實測 requests 即可讀到
   `<div class="page_content">` 內的公司清單，無需 Playwright 渲染；仍保留 Playwright fallback
   以防日後改版成 JS 渲染。）
4. 保經代（data.gov.tw dataset id）：
   - 15326  會員名錄_壽險會員公司（中華民國保險代理人商業同業公會）→ segment insurance_agent_life
   - 142715 會員名錄_產險會員公司（中華民國保險代理人商業同業公會）→ segment insurance_agent_nonlife
   實際下載連結（ciaa.org.tw / stat.fsc.gov.tw）透過 data.gov.tw REST API
   （https://data.gov.tw/api/v2/rest/dataset/<id>）即時解析 `resourceDownloadUrl`，
   不寫死轉址網址，避免下次改版失效。
5. 保經（保險經紀人公司）best-effort 附帶項目，見下方「已知覆蓋率限制」。

已知覆蓋率限制（誠實說明，勿當成全體名冊使用）：
- 保經（保險經紀人公司）**取不到全體名單**。保險局官網僅公開《得於我國經營再保險經紀業務之
  保險經紀人公司一覽表》PDF（segment `insurance_broker_reinsurance_permitted_subset`），
  這只是全體保經名單的一個子集；全體名單需登入 intermediary.ib.gov.tw，公開網站取不到
  （見 insurance_brokers_report.md 第二節）。本腳本盡力重抓＋解析該 PDF，
  解析失敗時標記 manual fallback，**不影響**前 4 個主要名冊的產出。
- 前 4 個名冊皆不含統編（`inst_code` 僅保經代的公會會員代碼），需另行補全。

【脆弱點警示】保險局那份 PDF 是**寫死的下載連結**（`BROKER_PDF_URL` 內含官方檔名
`business/202605271743300.pdf` 與日期字串 1150527）。官方換檔／改版即失效，屆時會出現
404 或解析 0 列，需人工到保險局網站找新連結並更新 `BROKER_PDF_URL`。
不想被這一項拖慢執行時可加 `--skip-broker-pdf` 略過。

執行方式：
    py -3.12 fetch_insurance.py [--run-date yyyy-mm-dd] [--skip-broker-pdf]

輸出（run_date 預設今天）：
    crawlers/raw/<run_date>/ib_life.csv
    crawlers/raw/<run_date>/ib_nonlife.csv
    crawlers/raw/<run_date>/ib_reinsurer.csv
    crawlers/raw/<run_date>/ib_agents.csv   (insurance_agent_life + insurance_agent_nonlife 合併一檔)
    crawlers/raw/<run_date>/ib_brokers.csv  (best-effort，可能為空或標記 manual fallback)
    crawlers/raw/<run_date>/fetch_insurance_report.md
    並在 stdout 印出一段 JSON 摘要（rosters/diff/issues），供上層彙整。

diff 基準線（只讀）：
    crawlers/baseline/ib_life.csv / ib_nonlife.csv / ib_reinsurer.csv / ib_agents.csv / ib_brokers.csv
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths

from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------------------
# 常數
# ---------------------------------------------------------------------------
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) industry-classifier-fetch_insurance/1.0"
FIELDS = ["segment", "inst_code", "official_name", "source_url", "fetched_at_note"]

IB_PAGES = [
    # (segment, id, 中文描述, baseline 檔名，供 diff 對照)
    ("insurance_life", 181, "人身保險（含外商人身保險在臺分公司）", "ib_life.csv"),
    ("insurance_nonlife", 180, "財產保險（含外商財產保險在臺分公司）", "ib_nonlife.csv"),
    ("insurance_reinsurer", 206, "再保險（含外商再保險在臺分公司）", "ib_reinsurer.csv"),
]

AGENT_DATASETS = [
    ("insurance_agent_life", 15326, "會員名錄_壽險會員公司（中華民國保險代理人商業同業公會）"),
    ("insurance_agent_nonlife", 142715, "會員名錄_產險會員公司（中華民國保險代理人商業同業公會）"),
]

# 【脆弱點】官方寫死檔名的下載連結，官方換檔即失效，見檔頭警示
BROKER_PDF_URL = (
    "https://www.ib.gov.tw/uploaddowndoc?file=business/202605271743300.pdf"
    "&filedisplay=%E5%86%8D%E4%BF%9D%E7%B6%93%E5%85%AC%E5%8F%B8%E4%B8%80%E8%A6%BD%E8%A1%A8-1150527.pdf"
    "&flag=doc"
)
BROKER_SEGMENT = "insurance_broker_reinsurance_permitted_subset"

STRIP_SUFFIXES = ["(停業清理中)", "（停業清理中）"]

FETCHED_AT = date.today().isoformat()  # 由 main() 依 --run-date 覆寫


def clean_name(s):
    """NFKC 正規化 + 去空白。

    保經 reinsurance-permitted 子集 PDF 實測發現：來源 PDF 內嵌字型對「利／連／聯／德／萬／禮／領／諾／林」
    等字使用了 CJK Compatibility Ideographs 區（U+F900–U+FAFF）的相容變體碼位而非對應的統一表意文字碼位，
    肉眼顯示完全相同但 `==` 比對會判定為不同字。NFKC 正規化可將這些相容變體碼位摺疊回標準碼位，
    大幅降低 diff 誤報（實測 15 家 → 3 家，其餘 3 家為全形/半形括號冒號或別名擷取不完整之個案，非真實名冊異動）。
    對 ib.gov.tw / data.gov.tw 來源目前未發現此問題，仍統一套用以求穩健。
    """
    return unicodedata.normalize("NFKC", s).strip()


class FetchError(Exception):
    """抓取或解析失敗；呼叫端需捕捉並記錄 issue，不得讓整支腳本因單一來源失敗而中止。"""


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), dict(r.headers)


# ---------------------------------------------------------------------------
# 1) 金管會保險局官網三頁（人身/財產/再保）
# ---------------------------------------------------------------------------
def fetch_ib_page_html(page_id):
    """requests 優先；HTML 內找不到名冊區塊才退回 Playwright 渲染。"""
    url = f"https://www.ib.gov.tw/ch/home.jsp?id={page_id}&parentpath=0,9,179"
    try:
        body, _ = http_get(url)
        text = body.decode("utf-8", errors="replace")
        if "page_content" in text:
            return text, url, "requests"
        raise FetchError(f"id={page_id}: HTML 未含 page_content 區塊，疑似需要 JS 渲染")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, FetchError) as e:
        # fallback: Playwright 渲染（JS/延遲載入情境；目前實測本站為靜態 HTML，此路徑為保險絲）
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise FetchError(f"id={page_id}: requests 失敗（{e}）且環境無 playwright，無法 fallback")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto(url, wait_until="networkidle", timeout=30000)
                html = page.content()
                browser.close()
            return html, url, "playwright(networkidle)"
        except Exception as e2:
            raise FetchError(f"id={page_id}: requests 與 Playwright 皆失敗（requests: {e}；playwright: {e2}）")


def parse_ib_page(html, segment, url, method):
    soup = BeautifulSoup(html, "html.parser")
    div = soup.find("div", class_="page_content")
    if div is None:
        raise FetchError(f"{segment}: 找不到 <div class=\"page_content\"> 區塊（頁面結構可能已改版）")
    anchors = div.find_all("a")
    if not anchors:
        raise FetchError(f"{segment}: page_content 區塊內無 <a> 名冊項目（頁面結構可能已改版）")

    updated_m = re.search(r"更新日期[：:]\s*([0-9\-]+)", str(soup))
    updated_note = f"官網標註更新日期:{updated_m.group(1)}" if updated_m else "官網未標註更新日期"

    rows = []
    seen = set()
    for a in anchors:
        raw_name = a.get_text(strip=True)
        if not raw_name:
            continue
        name = clean_name(raw_name)
        note_suffix = ""
        for suf in STRIP_SUFFIXES:
            if name.endswith(suf):
                name = name[: -len(suf)]
                note_suffix = f"；頁面標註{suf}"
                break
        if not name or name in seen:
            continue
        seen.add(name)
        rows.append({
            "segment": segment,
            "inst_code": "",
            "official_name": name,
            "source_url": url,
            "fetched_at_note": (
                f"{FETCHED_AT} {method} 讀取 www.ib.gov.tw"
                f"（<div class=\"page_content\"> 區塊逐一 <a> 標籤取公司名稱）"
                f"；{updated_note}{note_suffix}"
            ),
        })
    return rows


def fetch_ib_segment(segment, page_id, desc):
    html, url, method = fetch_ib_page_html(page_id)
    rows = parse_ib_page(html, segment, url, method)
    return rows


# ---------------------------------------------------------------------------
# 2) 保經代 — data.gov.tw dataset 15326 / 142715
# ---------------------------------------------------------------------------
def resolve_datagovtw_resource(dataset_id):
    """透過 data.gov.tw REST API 動態解析 resourceDownloadUrl（dataset id 穩定，實際轉址網址可能變動）。"""
    api_url = f"https://data.gov.tw/api/v2/rest/dataset/{dataset_id}"
    body, _ = http_get(api_url)
    j = json.loads(body.decode("utf-8", errors="replace"))
    if not j.get("success"):
        raise FetchError(f"dataset {dataset_id}: data.gov.tw API 回應 success=false")
    result = j.get("result") or {}
    dist = result.get("distribution") or []
    if not dist:
        raise FetchError(f"dataset {dataset_id}: API 回應無 distribution（資料集可能已下架/改版）")
    resource_url = dist[0].get("resourceDownloadUrl")
    if not resource_url:
        raise FetchError(f"dataset {dataset_id}: distribution 內無 resourceDownloadUrl")
    return resource_url, result.get("title", "")


def fetch_agent_dataset(segment, dataset_id, desc):
    resource_url, title = resolve_datagovtw_resource(dataset_id)
    body, _ = http_get(resource_url, timeout=40)
    text = body.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for r in reader:
        name = clean_name(r.get("會員名稱") or "")
        if not name:
            continue
        code = (r.get("會員代碼") or "").strip()
        status = (r.get("狀態") or "").strip() or "未標註"
        owner = (r.get("負責人") or "").strip() or "未標註"
        rows.append({
            "segment": segment,
            "inst_code": code,
            "official_name": name,
            "source_url": resource_url,
            "fetched_at_note": (
                f"{FETCHED_AT} requests 下載 data.gov.tw dataset {dataset_id}（{desc}）"
                f"；resourceDownloadUrl 由 data.gov.tw REST API 即時解析（非寫死轉址網址）"
                f"；狀態:{status}；負責人:{owner}"
            ),
        })
    if not rows:
        raise FetchError(f"dataset {dataset_id}: 下載內容可解析但 0 筆有效列（會員名稱皆空？請人工檢查原始檔）")
    return rows


# ---------------------------------------------------------------------------
# 3) 保經（reinsurance-permitted 子集）best-effort PDF 解析
# ---------------------------------------------------------------------------
def fetch_broker_pdf_subset(raw_dir):
    import pdfplumber

    body, _ = http_get(BROKER_PDF_URL, timeout=40)
    pdf_path = os.path.join(raw_dir, "_ib_brokers_reinsurance_subset.pdf")
    with open(pdf_path, "wb") as f:
        f.write(body)

    rows = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table:
                continue
            for r in table:
                if not r or not r[0] or r[0].strip() in ("No.", ""):
                    continue
                cells = (list(r) + [None] * 4)[:4]
                _no, name_raw, wenhao_raw, _note = cells
                if not name_raw:
                    continue
                name = clean_name(re.sub(r"\s+", "", name_raw)).replace("(股)公司", "股份有限公司")
                wenhao = re.sub(r"\s+", "", wenhao_raw or "")
                rows.append({
                    "segment": BROKER_SEGMENT,
                    "inst_code": "",
                    "official_name": name,
                    "source_url": BROKER_PDF_URL,
                    "fetched_at_note": (
                        f"{FETCHED_AT} requests 下載官網 PDF + pdfplumber 解析表格；核准文號:{wenhao}"
                        f"；【範圍警示】僅《得於我國經營再保險經紀業務之保險經紀人公司一覽表》子集，"
                        f"非全體保經名單（全體名單需登入 intermediary.ib.gov.tw，公開網站無法取得，"
                        f"見 insurance_brokers_report.md 第二節）"
                        f"；官方名稱使用「(股)公司」縮寫，已正規化為「股份有限公司」"
                    ),
                })
    if not rows:
        raise FetchError("broker PDF: 解析後 0 筆列，PDF 表格結構可能已改版")
    return rows


# ---------------------------------------------------------------------------
# 輸出與 diff
# ---------------------------------------------------------------------------
def write_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def load_existing(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def diff_names(existing_rows, new_rows):
    existing_names = {(r.get("official_name") or "").strip() for r in existing_rows}
    new_names = {(r.get("official_name") or "").strip() for r in new_rows}
    added = sorted(n for n in (new_names - existing_names) if n)
    removed = sorted(n for n in (existing_names - new_names) if n)
    return added, removed


def diff_summary_text(name, existing_rows, new_rows):
    added, removed = diff_names(existing_rows, new_rows)
    lines = [
        f"### {name}",
        f"- 既有快照列數: {len(existing_rows)}　新抓列數: {len(new_rows)}",
    ]
    if added:
        lines.append(f"- 新增 {len(added)} 家: " + "、".join(added))
    else:
        lines.append("- 新增: 無")
    if removed:
        lines.append(f"- 消失 {len(removed)} 家: " + "、".join(removed))
    else:
        lines.append("- 消失: 無")
    return "\n".join(lines), added, removed


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    global FETCHED_AT

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-date", default=date.today().isoformat(),
                    help="快照日期 yyyy-mm-dd，決定 raw/<日期>/ 輸出目錄與 note 標註（預設今天）")
    ap.add_argument("--skip-broker-pdf", action="store_true", help="略過 best-effort 的保經 PDF 子集抓取")
    args = ap.parse_args()

    FETCHED_AT = args.run_date
    raw_dir = _paths.raw_dir(FETCHED_AT)

    issues = []
    rosters = []  # for JSON summary
    report_lines = [f"# fetch_insurance 執行報告 — {FETCHED_AT}\n"]

    # ---- 1) 人身/財產/再保 三頁 ----
    for segment, page_id, desc, existing_filename in IB_PAGES:
        existing_path = _paths.baseline(existing_filename)
        out_path = os.path.join(raw_dir, existing_filename)
        try:
            rows = fetch_ib_segment(segment, page_id, desc)
            write_csv(out_path, rows)
            existing_rows = load_existing(existing_path)
            block, added, removed = diff_summary_text(f"{desc}（{segment}）", existing_rows, rows)
            report_lines.append(block + "\n")
            rosters.append({
                "name": segment,
                "rows": len(rows),
                "snapshot_path": out_path,
                "diff_vs_previous": f"+{len(added)}/-{len(removed)}（既有{len(existing_rows)}→新{len(rows)}）",
                "live_fetch_ok": True,
            })
        except FetchError as e:
            issues.append(f"[{segment}] 抓取失敗：{e}")
            write_csv(out_path, [])
            report_lines.append(f"### {desc}（{segment}）\n- **抓取失敗**：{e}\n- manual fallback：沿用既有快照 `{existing_filename}`，本次寫出 0 列\n")
            rosters.append({
                "name": segment,
                "rows": 0,
                "snapshot_path": out_path,
                "diff_vs_previous": "抓取失敗，無法 diff（manual fallback，沿用既有快照）",
                "live_fetch_ok": False,
            })

    # ---- 2) 保經代 data.gov.tw 15326 / 142715（合併寫入 ib_agents.csv） ----
    agent_existing_path = _paths.baseline("ib_agents.csv")
    agent_out_path = os.path.join(raw_dir, "ib_agents.csv")
    agent_rows_all = []
    agent_any_ok = False
    for segment, dataset_id, desc in AGENT_DATASETS:
        try:
            rows = fetch_agent_dataset(segment, dataset_id, desc)
            agent_rows_all.extend(rows)
            agent_any_ok = True
            report_lines.append(f"### {desc}（{segment}，dataset {dataset_id}）\n- 抓取成功：{len(rows)} 列\n")
        except FetchError as e:
            issues.append(f"[{segment}/dataset {dataset_id}] 抓取失敗：{e}")
            report_lines.append(f"### {desc}（{segment}，dataset {dataset_id}）\n- **抓取失敗**：{e}\n")

    write_csv(agent_out_path, agent_rows_all)
    existing_agent_rows = load_existing(agent_existing_path)
    block, added, removed = diff_summary_text("保經代合併（ib_agents.csv）", existing_agent_rows, agent_rows_all)
    report_lines.append(block + "\n")
    rosters.append({
        "name": "insurance_agent (15326+142715)",
        "rows": len(agent_rows_all),
        "snapshot_path": agent_out_path,
        "diff_vs_previous": (
            f"+{len(added)}/-{len(removed)}（既有{len(existing_agent_rows)}→新{len(agent_rows_all)}；"
            f"既有快照為空白樣板，此為首次填入）" if not existing_agent_rows
            else f"+{len(added)}/-{len(removed)}（既有{len(existing_agent_rows)}→新{len(agent_rows_all)}）"
        ),
        "live_fetch_ok": agent_any_ok,
    })

    # ---- 3) 保經 reinsurance-permitted 子集 best-effort ----
    broker_existing_path = _paths.baseline("ib_brokers.csv")
    broker_out_path = os.path.join(raw_dir, "ib_brokers.csv")
    if args.skip_broker_pdf:
        write_csv(broker_out_path, [])
        report_lines.append("### 保經 reinsurance-permitted 子集（ib_brokers.csv）\n- 依 --skip-broker-pdf 略過\n")
        rosters.append({
            "name": "insurance_broker_reinsurance_permitted_subset",
            "rows": 0,
            "snapshot_path": broker_out_path,
            "diff_vs_previous": "本次略過（--skip-broker-pdf）",
            "live_fetch_ok": False,
        })
    else:
        try:
            broker_rows = fetch_broker_pdf_subset(raw_dir)
            write_csv(broker_out_path, broker_rows)
            existing_broker_rows = load_existing(broker_existing_path)
            block, added, removed = diff_summary_text("保經 reinsurance-permitted 子集（ib_brokers.csv）", existing_broker_rows, broker_rows)
            report_lines.append(block + "\n")
            rosters.append({
                "name": "insurance_broker_reinsurance_permitted_subset",
                "rows": len(broker_rows),
                "snapshot_path": broker_out_path,
                "diff_vs_previous": f"+{len(added)}/-{len(removed)}（既有{len(existing_broker_rows)}→新{len(broker_rows)}）",
                "live_fetch_ok": True,
            })
        except FetchError as e:
            issues.append(f"[insurance_broker_reinsurance_permitted_subset] best-effort 抓取失敗：{e}（非核心目標，manual fallback 沿用既有快照，不影響前 4 個名冊）")
            write_csv(broker_out_path, [])
            report_lines.append(f"### 保經 reinsurance-permitted 子集（ib_brokers.csv）\n- **best-effort 抓取失敗**：{e}\n- manual fallback：沿用既有快照，本次寫出 0 列\n")
            rosters.append({
                "name": "insurance_broker_reinsurance_permitted_subset",
                "rows": 0,
                "snapshot_path": broker_out_path,
                "diff_vs_previous": "抓取失敗（best-effort 附帶項目），manual fallback 沿用既有快照",
                "live_fetch_ok": False,
            })

    if issues:
        report_lines.append("## 已知問題 / manual fallback\n" + "\n".join(f"- {i}" for i in issues) + "\n")

    report_path = os.path.join(raw_dir, "fetch_insurance_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))

    summary = {
        "source_key": "insurance",
        "fetched_at": FETCHED_AT,
        "live_fetch_ok": all(r["live_fetch_ok"] for r in rosters if r["name"] not in (
            "insurance_broker_reinsurance_permitted_subset",
        )),  # 核心 4 名冊(人身/財產/再保/保經代)皆成功才算 live_fetch_ok；best-effort 保經子集不列入判準
        "raw_dir": raw_dir,
        "report_path": report_path,
        "rosters": rosters,
        "issues": issues,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
