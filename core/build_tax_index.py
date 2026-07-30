# -*- coding: utf-8 -*-
r"""把稅籍全檔（BGMOPEN1.zip，171 萬列）建成 sqlite 索引，讓本地端單筆查詢即時回應

不建索引也能跑——本地端 provider 會退回掃描整個 zip（單筆約 10–20 秒）。
建了索引之後單筆查詢是毫秒級，適合給人做臨時查詢或接成內部服務。

用法：
    py -3.12 core/build_tax_index.py            # 建立／重建索引
    py -3.12 core/build_tax_index.py --check    # 只檢查索引是否比稅籍檔新

產出：data/tax_index.sqlite（約 250–350 MB，可刪，刪了只是變慢）
"""
import argparse
import csv
import io
import json
import os
import sqlite3
import sys
import time
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
ZIP_PATH = os.path.join(DATA, "BGMOPEN1.zip")
IDX_PATH = os.path.join(DATA, "tax_index.sqlite")

COL_TAX_ID, COL_NAME, COL_ORG = 1, 3, 6
CODE_PAIRS = (8, 10, 12, 14)


def build():
    if not os.path.exists(ZIP_PATH):
        sys.exit("缺少 %s，請先執行 core/fetch_bulk_data.py" % ZIP_PATH)
    tmp = IDX_PATH + ".part"
    for p in (tmp, tmp + "-journal"):
        if os.path.exists(p):
            os.remove(p)

    conn = sqlite3.connect(tmp)
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("CREATE TABLE tax (tax_id TEXT PRIMARY KEY, name TEXT, org TEXT, codes TEXT)")

    t0 = time.time()
    z = zipfile.ZipFile(ZIP_PATH)
    member = z.namelist()[0]
    total, dup, batch = 0, 0, []
    seen = set()
    with z.open(member) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig",
                                            errors="replace", newline=""))
        next(reader, None)
        for row in reader:
            if len(row) <= COL_ORG:
                continue
            tid = row[COL_TAX_ID].strip()
            if not tid:
                continue
            if tid in seen:                       # 同統編多列（分支等）只留第一列，與引擎一致
                dup += 1
                continue
            seen.add(tid)
            codes = [[row[i].strip(), row[i + 1].strip() if len(row) > i + 1 else ""]
                     for i in CODE_PAIRS if len(row) > i and row[i].strip()]
            batch.append((tid, row[COL_NAME].strip(), row[COL_ORG].strip(),
                          json.dumps(codes, ensure_ascii=False)))
            total += 1
            if len(batch) >= 20000:
                conn.executemany("INSERT INTO tax VALUES (?,?,?,?)", batch)
                batch.clear()
                print("  已寫入 %s 列…" % f"{total:,}", end="\r")
    if batch:
        conn.executemany("INSERT INTO tax VALUES (?,?,?,?)", batch)
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    conn.close()
    os.replace(tmp, IDX_PATH)
    print("  索引完成：%s 列（跳過重複統編 %s 列），%.1f 秒，%.0f MB"
          % (f"{total:,}", f"{dup:,}", time.time() - t0, os.path.getsize(IDX_PATH) / 1048576))


def check():
    if not os.path.exists(IDX_PATH):
        print("索引不存在（本地端仍可執行，單筆查詢會掃全檔約 10–20 秒）")
        return 1
    if not os.path.exists(ZIP_PATH):
        print("索引存在，但稅籍檔不存在——無法判斷新舊")
        return 1
    idx_m, zip_m = os.path.getmtime(IDX_PATH), os.path.getmtime(ZIP_PATH)
    conn = sqlite3.connect(IDX_PATH)
    n = conn.execute("SELECT COUNT(*) FROM tax").fetchone()[0]
    conn.close()
    fresh = idx_m >= zip_m
    print("索引 %s 列，%.0f MB，%s" % (f"{n:,}", os.path.getsize(IDX_PATH) / 1048576,
                                      "比稅籍檔新（可用）" if fresh else "**比稅籍檔舊，請重建**"))
    return 0 if fresh else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="建立稅籍 sqlite 索引")
    ap.add_argument("--check", action="store_true", help="只檢查索引狀態")
    args = ap.parse_args()
    sys.exit(check() if args.check else (build() or 0))
