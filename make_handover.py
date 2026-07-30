# -*- coding: utf-8 -*-
r"""打包成可交付的 zip

兩種包裝，看收到的人要哪種模式：

  --light （預設）約 2–3 MB
      不含大檔。收到的人可以立刻用 api 模式查詢；
      要用 local 模式就自己跑一次 core/fetch_bulk_data.py。
      **這種可以用 email 寄。**

  --full  約 322 MB
      含全部下載好的資料源與 sqlite 索引，解壓即可離線執行。
      太大不能寄信，放共用磁碟或外接碟。

  --no-crawlers
      不含 crawlers/（爬蟲產線與名冊建置資產）。
      給「只要查詢、不維護名冊」的人用，包會再小一半。

用法：
    py -3.12 make_handover.py                    # 輕量包
    py -3.12 make_handover.py --full             # 完整包
    py -3.12 make_handover.py --no-crawlers      # 只要查詢功能
    py -3.12 make_handover.py --out D:\交付\     # 指定輸出目錄
"""
import argparse
import os
import sys
import zipfile
from datetime import date

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.basename(ROOT)

# 一律排除
ALWAYS_SKIP_DIRS = {"__pycache__", ".git", "_tmp"}
ALWAYS_SKIP_FILES = {".gitignore"}
ALWAYS_SKIP_SUFFIX = (".pyc", ".part", ".testbak", ".bak")

# 輕量包額外排除（大檔與快取）
LIGHT_SKIP = {
    "data/BGMOPEN1.zip", "data/BGMOPEN99.csv", "data/BGMOPEN99X.csv",
    "data/tax_index.sqlite", "data/_gcis_cache.json", "data/_api_cache.json",
    "data/_fetch_log.json",
}
# 執行產物一律不進包
OUTPUT_PREFIXES = ("output/", "crawlers/raw/", "crawlers/build/", "tests/_tmp/")


def rel_posix(path):
    return os.path.relpath(path, ROOT).replace("\\", "/")


def should_include(rel, light, with_crawlers):
    if any(rel.startswith(p) for p in OUTPUT_PREFIXES):
        return False
    if not with_crawlers and rel.startswith("crawlers/"):
        return False
    if light and rel in LIGHT_SKIP:
        return False
    if os.path.basename(rel) in ALWAYS_SKIP_FILES:
        return False
    if rel.endswith(ALWAYS_SKIP_SUFFIX):
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description="打包 industry_classifier 交付檔")
    ap.add_argument("--full", action="store_true", help="含全部資料源（約 322 MB）")
    ap.add_argument("--no-crawlers", action="store_true", help="不含爬蟲產線")
    ap.add_argument("--out", default=os.path.dirname(ROOT), help="輸出目錄")
    args = ap.parse_args()

    light = not args.full
    with_crawlers = not args.no_crawlers
    tag = ("完整" if args.full else "輕量") + ("" if with_crawlers else "_無爬蟲")
    name = "%s_%s_%s.zip" % (PKG, tag, date.today().strftime("%Y%m%d"))
    out_path = os.path.join(args.out, name)

    files, total = [], 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in ALWAYS_SKIP_DIRS]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if full == out_path:
                continue
            rel = rel_posix(full)
            if should_include(rel, light, with_crawlers):
                files.append((full, rel))
                total += os.path.getsize(full)

    print("打包 %s" % name)
    print("  檔案 %d 個，未壓縮 %.1f MB" % (len(files), total / 1048576))
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for full, rel in files:
            z.write(full, arcname="%s/%s" % (PKG, rel))
        if light:
            z.writestr("%s/請先讀我.txt" % PKG, LIGHT_NOTE)
    print("  壓縮後 %.1f MB → %s" % (os.path.getsize(out_path) / 1048576, out_path))
    print()
    if light:
        print("這是輕量包：可立刻用 api 模式查詢。")
        print("要用 local 模式（離線／整批），解壓後執行：")
        print("    py -3.12 core/fetch_bulk_data.py")
        print("    py -3.12 core/build_tax_index.py")
    else:
        print("這是完整包：解壓即可離線執行 local 模式。")
    print("驗收：py -3.12 tests/test_consistency.py")


LIGHT_NOTE = """industry_classifier — 輕量交付包
=====================================

這包不含大型資料源（稅籍全檔等），所以可以用 email 寄。

【立刻可用】api 模式，不需下載任何東西
    py -3.12 classify.py --mode api 22099131
    py -3.12 classify.py --mode api --input input/taxids_sample.csv

【要用 local 模式】（離線、整批較快）先下載資料源約 320 MB
    py -3.12 core/fetch_bulk_data.py
    py -3.12 core/build_tax_index.py        （可選，加速單筆查詢）
    py -3.12 classify.py --doctor           （確認資料就位）

【驗收】
    py -3.12 tests/test_consistency.py      （無外網時加 --skip-api）

需要 Python 3.12。分類器本身零第三方套件。
只有 crawlers/（金管會名冊爬蟲）需要 pip install -r crawlers/requirements.txt。

詳細說明看 README.md，先讀「兩種查詢模式，選一個」那一節。
"""

if __name__ == "__main__":
    main()
