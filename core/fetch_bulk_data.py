# -*- coding: utf-8 -*-
r"""下載本地端模式所需的五個公開資料源（全檔）

本地端模式（local/）需要這五個檔案，全部是政府開放資料、免金鑰：

  BGMOPEN1.zip     財政部稅籍登記全量        ~63 MB   每日更新
  BGMOPEN99.csv    非營利事業機關團體名冊    ~12 MB   每月更新
  BGMOPEN99X.csv   全國各級學校統一編號      ~1 MB    每月更新
  gov_central.csv  行政院所屬機關統一編號    ~42 KB   每月更新
  gov_local.csv    地方政府機關統一編號      ~94 KB   每月更新

第六個資料源 authority_master.csv（金管會權威名冊 1,343 列）**不是公開下載檔**，
是由 crawlers/ 的六支爬蟲抓官方名冊後組裝而成，見 crawlers/README.md。

用法：
    py -3.12 core/fetch_bulk_data.py              # 只補缺檔或過期檔
    py -3.12 core/fetch_bulk_data.py --force      # 全部重抓
    py -3.12 core/fetch_bulk_data.py --check      # 只檢查來源可達與新舊，不下載
    py -3.12 core/fetch_bulk_data.py --max-age 7  # 超過 7 天才重抓（預設 30）

設計原則（沿用原產線 fetcher 慣例）：
  * 抓不到不覆寫既有檔案，如實回報並保留舊快照（離線可用比資料新鮮更重要）
  * 每個檔案抓完後驗證：非空、可解壓／可解析、列數在合理範圍
  * 下載結果寫入 data/_fetch_log.json，供文件與稽核使用
"""
import argparse
import csv
import io
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
UA = {"User-Agent": "Mozilla/5.0 (industry_classifier/1.0)"}
DGT_API = "https://data.gov.tw/api/v2/rest/dataset/%s"

# ── 資料源定義 ────────────────────────────────────────────────────────────
# direct_url：財資中心固定網址（穩定，優先用）
# dataset_id：data.gov.tw 資料集編號（direct_url 失效時改由 API 解析下載網址）
SOURCES = [
    {
        "key": "tax_registry",
        "filename": "BGMOPEN1.zip",
        "title": "財政部稅籍登記全量",
        "dataset_id": "9400",
        "direct_url": "https://eip.fia.gov.tw/data/BGMOPEN1.zip",
        "cadence": "每日",
        "min_bytes": 40_000_000,
        "expect_rows": (1_400_000, 2_200_000),
        "kind": "zip",
    },
    {
        "key": "nonprofit",
        "filename": "BGMOPEN99.csv",
        "title": "非營利事業機關團體名冊",
        "dataset_id": "34147",
        "direct_url": "https://eip.fia.gov.tw/data/BGMOPEN99.csv",
        "cadence": "每月",
        "min_bytes": 5_000_000,
        "expect_rows": (60_000, 200_000),
        "kind": "csv",
    },
    {
        "key": "schools",
        "filename": "BGMOPEN99X.csv",
        "title": "全國各級學校統一編號",
        "dataset_id": "75136",
        "direct_url": "https://eip.fia.gov.tw/data/BGMOPEN99X.csv",
        "cadence": "每月",
        "min_bytes": 300_000,
        "expect_rows": (8_000, 20_000),
        "kind": "csv",
    },
    {
        "key": "gov_central",
        "filename": "gov_central.csv",
        "title": "行政院所屬各機關同意開放統一編號",
        "dataset_id": "44806",
        "direct_url": None,
        "cadence": "每月",
        "min_bytes": 10_000,
        "expect_rows": (300, 3_000),
        "kind": "csv",
    },
    {
        "key": "gov_local",
        "filename": "gov_local.csv",
        "title": "地方政府各機關同意開放統一編號",
        "dataset_id": "166161",
        "direct_url": None,
        "cadence": "每月",
        "min_bytes": 20_000,
        "expect_rows": (500, 6_000),
        "kind": "csv",
    },
]


def http_get(url, timeout=180, retries=3):
    """GET 位元組內容，指數退避重試。失敗回 (None, 錯誤訊息)。"""
    ctx = ssl.create_default_context()
    last = ""
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read(), ""
        except Exception as e:                       # noqa: BLE001 — 對外網路，任何錯都要收
            last = "%s: %s" % (type(e).__name__, e)
            if attempt < retries:
                time.sleep(2 ** attempt)
    return None, last


def resolve_urls(src):
    """回傳候選下載網址清單：direct_url 優先，其次 data.gov.tw API 解析出的網址。"""
    urls = []
    if src["direct_url"]:
        urls.append(src["direct_url"])
    raw, err = http_get(DGT_API % src["dataset_id"], timeout=60)
    if raw:
        try:
            meta = json.loads(raw.decode("utf-8", "replace"))
            for d in (meta.get("result", {}) or {}).get("distribution", []) or []:
                u = d.get("resourceDownloadUrl")
                if u and u not in urls:
                    urls.append(u)
        except Exception:                            # noqa: BLE001
            pass
    return urls


def count_rows(path, kind):
    """回傳資料列數（不含表頭）；無法解析回 None。"""
    try:
        if kind == "zip":
            z = zipfile.ZipFile(path)
            member = z.namelist()[0]
            n = 0
            with z.open(member) as raw:
                for _ in io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace"):
                    n += 1
            return max(0, n - 1)
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
            return max(0, sum(1 for _ in f) - 1)
    except Exception:                                # noqa: BLE001
        return None


def validate(path, src):
    """下載後驗證：大小、可解析、列數區間。回 (ok, 說明)。"""
    size = os.path.getsize(path)
    if size < src["min_bytes"]:
        return False, "檔案過小（%d bytes < 下限 %d）" % (size, src["min_bytes"])
    rows = count_rows(path, src["kind"])
    if rows is None:
        return False, "無法解析內容"
    lo, hi = src["expect_rows"]
    if not (lo <= rows <= hi):
        return False, "列數 %s 不在預期區間 %s–%s" % (f"{rows:,}", f"{lo:,}", f"{hi:,}")
    return True, "%.1f MB／%s 列" % (size / 1048576, f"{rows:,}")


def age_days(path):
    if not os.path.exists(path):
        return None
    return (time.time() - os.path.getmtime(path)) / 86400


def fetch_one(src, force, max_age, check_only):
    dest = os.path.join(DATA, src["filename"])
    rec = {"key": src["key"], "file": src["filename"], "title": src["title"],
           "cadence": src["cadence"], "action": "", "detail": "", "url": ""}
    a = age_days(dest)
    have = a is not None

    if check_only:
        urls = resolve_urls(src)
        rec["url"] = urls[0] if urls else ""
        rec["action"] = "check"
        rec["detail"] = ("本地已有，%.1f 天前" % a if have else "本地無此檔") + \
                        ("；可解析出 %d 個下載網址" % len(urls) if urls else "；**解析不到下載網址**")
        return rec, True

    if have and not force and a <= max_age:
        ok, info = validate(dest, src)
        rec["action"] = "skip"
        rec["detail"] = "沿用既有檔（%.1f 天前，%s）" % (a, info if ok else "驗證未過：" + info)
        return rec, ok

    urls = resolve_urls(src)
    if not urls:
        rec["action"] = "fail"
        rec["detail"] = "解析不到任何下載網址" + ("；保留既有舊檔" if have else "；且本地無舊檔")
        return rec, have

    tmp = dest + ".part"
    for u in urls:
        blob, err = http_get(u)
        if blob is None:
            rec["detail"] = "下載失敗 %s（%s）" % (u, err)
            continue
        with open(tmp, "wb") as f:
            f.write(blob)
        ok, info = validate(tmp, src)
        if not ok:
            os.remove(tmp)
            rec["detail"] = "下載成功但驗證未過 %s（%s）" % (u, info)
            continue
        os.replace(tmp, dest)
        rec.update(action="ok", detail=info, url=u)
        return rec, True

    rec["action"] = "fail"
    if have:
        rec["detail"] += "；**保留既有舊檔，本地端模式仍可執行**"
    return rec, have


def main():
    ap = argparse.ArgumentParser(description="下載本地端模式所需的公開資料源")
    ap.add_argument("--force", action="store_true", help="忽略檔案新舊，全部重抓")
    ap.add_argument("--check", action="store_true", help="只檢查來源可達與本地新舊，不下載")
    ap.add_argument("--max-age", type=int, default=30, help="超過幾天才重抓（預設 30）")
    ap.add_argument("--only", default="", help="只處理指定 key（逗號分隔）")
    args = ap.parse_args()

    os.makedirs(DATA, exist_ok=True)
    keys = {k.strip() for k in args.only.split(",") if k.strip()}
    todo = [s for s in SOURCES if not keys or s["key"] in keys]

    print("資料目錄：%s" % DATA)
    print("模式：%s，重抓門檻 %d 天" % ("檢查" if args.check else ("強制重抓" if args.force else "增量"),
                                       args.max_age))
    print()
    results, all_ok = [], True
    for s in todo:
        print("→ %s（%s，%s）" % (s["title"], s["filename"], s["cadence"]))
        rec, ok = fetch_one(s, args.force, args.max_age, args.check)
        results.append(rec)
        all_ok = all_ok and ok
        mark = {"ok": "  [完成]", "skip": "  [沿用]", "check": "  [檢查]", "fail": "  [失敗]"}[rec["action"]]
        print("%s %s" % (mark, rec["detail"]))
        print()

    log_path = os.path.join(DATA, "_fetch_log.json")
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                   "results": results}, f, ensure_ascii=False, indent=1)

    auth = os.path.join(DATA, "authority_master.csv")
    print("─" * 62)
    if not os.path.exists(auth):
        print("※ 尚缺 authority_master.csv（金管會權威名冊）——非公開下載檔，")
        print("  請執行 crawlers/build_authority_master.py 或向提供者索取最新快照。")
    else:
        print("authority_master.csv 已就位（%.1f 天前更新）" % age_days(auth))
    print("紀錄寫入 %s" % log_path)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
