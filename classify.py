# -*- coding: utf-8 -*-
r"""產業分類查詢 — 統一入口（本地端與 API 端共用）

    輸入統一編號 → 官方正式名稱 ＋ 產業大類 ＋ 產業子類 ＋ 分類依據

產業大類／產業子類為定版單軌（2026-08-11）：身分軌命中者沿用八大分類；
一般企業改由行業軌（稅籍主行業代號 → A–S 十九大類）回答。
輸出同時保留身分軌原值（大分類／子分類）供稽核。

兩種查詢模式，同一套規則、同一個引擎，結果一致：

    local   讀已下載的全檔（可完全離線）。需先跑 core/fetch_bulk_data.py。
            適合：整批跑名單、內部服務、無外網環境。
    api     打單筆查詢 API（免下載 63 MB 稅籍檔）。
            適合：臨時查幾筆、不想維護大檔的人。

用法
────
  # 單筆查詢
  py -3.12 classify.py 22099131
  py -3.12 classify.py --mode api 22099131

  # 整批跑名單（CSV，第一欄統編；有表頭會自動略過）
  py -3.12 classify.py --input input/taxids.csv --output output/result.csv

  # 完全離線（跳過 GCIS 那層，該類統編改由稅籍大類判定）
  py -3.12 classify.py --mode local --offline --input input/taxids.csv

  # 檢查資料就位狀況
  py -3.12 classify.py --doctor

輸出欄位見 core/engine.py 的 OUTPUT_COLUMNS。
"""
import argparse
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "core"))

import engine                                        # noqa: E402
import rules as R                                    # noqa: E402
import exceptions as X                               # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DATA = os.path.join(HERE, "data")
OUTPUT = os.path.join(HERE, "output")
RULES_VERSION = "v4"


def build_provider(mode, offline):
    """建立 provider。mode=auto 時：資料齊全走 local，否則走 api。"""
    have_tax = os.path.exists(os.path.join(DATA, "BGMOPEN1.zip"))
    have_auth = os.path.exists(os.path.join(DATA, "authority_master.csv"))
    if mode == "auto":
        mode = "local" if (have_tax and have_auth) else "api"
    if mode == "local":
        sys.path.insert(0, os.path.join(HERE, "local"))
        from provider import LocalProvider           # noqa: PLC0415
        p = LocalProvider(DATA, offline=offline)
    else:
        sys.path.insert(0, os.path.join(HERE, "api"))
        from provider import ApiProvider             # noqa: PLC0415
        p = ApiProvider(DATA, offline=offline)
    p.load_registries(require_authority=True)
    return mode, p


def read_taxids(path):
    """讀第一欄統編；自動略過表頭與空列。回 [(統編, 備用名稱)]。"""
    out = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.reader(f)):
            if not row or not row[0].strip():
                continue
            first = row[0].strip()
            if i == 0 and not any(ch.isdigit() for ch in first):
                continue                              # 表頭
            name = row[1].strip() if len(row) > 1 else ""
            out.append((first, name))
    return out


def doctor():
    """檢查兩種模式各自的資料就位狀況。"""
    print("資料目錄：%s\n" % DATA)
    need = [("BGMOPEN1.zip", "稅籍全檔", "local 必需", "core/fetch_bulk_data.py"),
            ("tax_index.sqlite", "稅籍索引（可選，加速單筆查詢）", "local 選用",
             "core/build_tax_index.py"),
            ("authority_master.csv", "金管會權威名冊", "兩種模式皆必需",
             "crawlers/build_authority_master.py"),
            ("BGMOPEN99.csv", "非營利團體名冊", "兩種模式皆需", "core/fetch_bulk_data.py"),
            ("BGMOPEN99X.csv", "學校名冊", "兩種模式皆需", "core/fetch_bulk_data.py"),
            ("gov_central.csv", "行政院機關名冊", "兩種模式皆需", "core/fetch_bulk_data.py"),
            ("gov_local.csv", "地方機關名冊", "兩種模式皆需", "core/fetch_bulk_data.py")]
    ok = True
    for fn, desc, when, how in need:
        p = os.path.join(DATA, fn)
        if os.path.exists(p):
            print("  [有] %-22s %-28s %.1f MB" % (fn, desc, os.path.getsize(p) / 1048576))
        else:
            flag = "選用" in when
            print("  [%s] %-22s %-28s ← %s" % ("缺" if not flag else "無", fn, desc, how))
            ok = ok and flag
    print()
    print("local 模式：%s" % ("可用" if os.path.exists(os.path.join(DATA, "BGMOPEN1.zip"))
                             else "不可用（缺稅籍全檔）"))
    print("api   模式：可用（僅需名冊小檔＋外網）")
    print()
    print("規則版本 %s：通用規則 %d 條（core/rules.py）" % (RULES_VERSION, count_generic_rules()))
    for k, v in X.summary().items():
        print("  %-16s %s" % (k, v))
    return 0 if ok else 1


def count_generic_rules():
    """通用規則條數（與 docs/分類邏輯表 的條數一致）。

    本地例外表另計——它的筆數會隨各組織自己的裁決而變，不算通用規則。
    """
    return (3                          # L1-1／L1-3／L1-2 本地追加，各以一條摘要列表示
            + len(X.PERIPHERAL_BUILTIN)  # L1-2 內建周邊單位白名單（法定公開機構）
            + len(R.REGISTRIES)
            + len(R.MEDICAL2) + len(R.FIN6) + len(R.FIN4) + len(R.EDU_CORP2)
            + 1                        # L3-6 法人名稱前綴
            + len(R.SUB_BY_MAJOR2) + len(R.NAME_RULES)
            + 1)                       # L4 兜底


def main():
    ap = argparse.ArgumentParser(
        description="統編 → 官方名稱與產業分類（產業大類＋產業子類）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tax_id", nargs="?", help="要查的統一編號（單筆查詢）")
    ap.add_argument("--mode", choices=("auto", "local", "api"), default="auto",
                    help="查詢模式（預設 auto：資料齊全走 local，否則走 api）")
    ap.add_argument("--input", help="統編清單 CSV（第一欄統編，可選第二欄備用名稱）")
    ap.add_argument("--output", help="輸出 CSV 路徑（預設 output/分類結果.csv）")
    ap.add_argument("--offline", action="store_true", help="不連外，跳過 GCIS 那層")
    ap.add_argument("--as-of", default="", help="寫入輸出的判定日（預設空白；填了才會出現）")
    ap.add_argument("--json", action="store_true", help="單筆查詢以 JSON 輸出")
    ap.add_argument("--doctor", action="store_true", help="檢查資料就位狀況後結束")
    args = ap.parse_args()

    if args.doctor:
        sys.exit(doctor())
    if not args.tax_id and not args.input:
        ap.error("請給一個統一編號，或用 --input 指定名單 CSV")

    try:
        mode, provider = build_provider(args.mode, args.offline)
    except FileNotFoundError as e:
        sys.exit("資料缺漏：%s\n（可先執行 py -3.12 classify.py --doctor 檢查）" % e)

    # ── 單筆 ─────────────────────────────────────────────────────────────
    if args.tax_id:
        rec = engine.query(args.tax_id, provider, as_of=args.as_of)
        if hasattr(provider, "save_gcis_cache"):
            provider.save_gcis_cache()
        if args.json:
            print(json.dumps(rec, ensure_ascii=False, indent=2))
        else:
            width = max(len(k) for k in rec)
            for k, v in rec.items():
                if v != "":
                    print("  %s：%s" % (k.ljust(width), v))
        return

    # ── 批次 ─────────────────────────────────────────────────────────────
    items = read_taxids(args.input)
    summary = "、".join(
        "%s %s" % (k, f"{v:,}" if isinstance(v, int) else v)
        for k, v in provider.registry_summary().items())
    print("模式 %s ｜ 輸入 %d 筆 ｜ 名冊：%s" % (mode, len(items), summary))
    if mode == "local" and getattr(provider, "wants_preload", False):
        print("預載稅籍（掃一次全檔，約 20–40 秒；建了 sqlite 索引可省略此步）…")
        provider.preload([t for t, _ in items])

    rows = [engine.query(t, provider, fallback_name=n, as_of=args.as_of) for t, n in items]
    if hasattr(provider, "save_gcis_cache"):
        provider.save_gcis_cache()

    os.makedirs(OUTPUT, exist_ok=True)
    out_path = args.output or os.path.join(OUTPUT, "分類結果.csv")
    order = {g: i for i, g in enumerate(R.GROUPS)}
    rows.sort(key=lambda r: (order.get(r["大分類"], 99), r["子分類"], r["統一編號"]))
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=engine.OUTPUT_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    from collections import Counter                   # noqa: PLC0415
    print()
    for g in R.GROUPS:
        subs = Counter(r["子分類"] for r in rows if r["大分類"] == g)
        if subs:
            print("  %-6s %3d ：%s" % (g, sum(subs.values()),
                                       "、".join("%s %d" % kv for kv in subs.most_common())))
    print()
    for k, v in sorted(Counter(r["分類依據層"] for r in rows).items()):
        print("  %-14s %3d (%.1f%%)" % (k, v, 100.0 * v / len(rows)))
    bad = [r for r in rows if r["子分類"] not in R.SUBGROUPS.get(r["大分類"], [])]
    print("\n值域自我檢查：%d 筆越界" % len(bad))
    print("寫出 %s（%d 列）" % (out_path, len(rows)))


if __name__ == "__main__":
    main()
