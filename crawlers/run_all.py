# -*- coding: utf-8 -*-
r"""一次跑完六支名冊爬蟲，並匯總成一份異動總表

原始產線沒有 orchestrator（六支各自執行、各自產報告），這支補上那一段。

它做什麼：
  1. 依序執行六支 fetcher（各自獨立，一支失敗不影響其他支）
  2. 收集每支的 exit code、輸出檔、以及它自己產的異動報告
  3. 匯總成 crawlers/raw/<日期>/00_異動總表.md

它**不做**什麼（重要）：
  * 不會更新 crawlers/masters/ 的三張列源名冊
  * 不會重建 authority_master.csv
  兩者之間有一段人工歸併（判斷新增／更名／併購／退場、補統編、決定 segment），
  詳見 docs/名冊維護與爬蟲.md。這支只負責「抓到」與「告訴你差異」。

用法：
    py -3.12 crawlers/run_all.py                      # 抓今天的
    py -3.12 crawlers/run_all.py --run-date 2026-08-01
    py -3.12 crawlers/run_all.py --only banking,sfb   # 只跑指定幾支
    py -3.12 crawlers/run_all.py --skip-broker-pdf    # 略過保險局 PDF（沒裝 pdfplumber 時）
"""
import argparse
import glob
import os
import subprocess
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import _paths  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# (key, 腳本, 主管機關, 這支抓的 segment)
FETCHERS = [
    ("banking", "fetch_banking.py", "金管會銀行局",
     "本國銀行、外國銀行在臺分行、陸銀在臺分行、信用合作社、票券金融、金融控股、專營電子支付"),
    ("sfb", "fetch_sfb.py", "金管會證期局", "證券商、期貨商、投信、投顧、證券金融"),
    ("insurance", "fetch_insurance.py", "金管會保險局",
     "人身保險、財產保險、再保險、外商保險在臺分公司、保險經紀人／代理人公司"),
    ("boaf", "fetch_boaf.py", "農業部農業金融署", "農會信用部、漁會信用部、農業金庫"),
    ("moda", "fetch_moda.py", "數位發展部數位產業署", "第三方支付"),
    ("leasing", "fetch_leasing.py", "台北市租賃商業同業公會（非主管機關）", "融資租賃資融"),
]


def run_one(key, script, run_date, extra):
    path = os.path.join(HERE, script)
    if not os.path.exists(path):
        return {"key": key, "script": script, "rc": None, "note": "腳本不存在", "tail": ""}
    cmd = [sys.executable, path, "--run-date", run_date, *extra]
    print("  執行 %s …" % script, flush=True)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           cwd=HERE, timeout=900)
        out = (p.stdout or "") + (p.stderr or "")
        return {"key": key, "script": script, "rc": p.returncode,
                "note": "" if p.returncode == 0 else "回傳碼 %d" % p.returncode,
                "tail": "\n".join(out.strip().splitlines()[-12:])}
    except subprocess.TimeoutExpired:
        return {"key": key, "script": script, "rc": -1, "note": "逾時（15 分鐘）", "tail": ""}
    except Exception as e:                             # noqa: BLE001
        return {"key": key, "script": script, "rc": -1,
                "note": "%s: %s" % (type(e).__name__, e), "tail": ""}


def main():
    ap = argparse.ArgumentParser(description="一次跑完六支名冊爬蟲並匯總異動")
    ap.add_argument("--run-date", default=date.today().isoformat())
    ap.add_argument("--only", default="", help="只跑指定 key（逗號分隔）：%s"
                                              % ",".join(k for k, *_ in FETCHERS))
    ap.add_argument("--skip-broker-pdf", action="store_true",
                    help="傳給 fetch_insurance，略過保險局 PDF 子集")
    args = ap.parse_args()

    keys = {k.strip() for k in args.only.split(",") if k.strip()}
    todo = [f for f in FETCHERS if not keys or f[0] in keys]
    raw_dir = _paths.raw_dir(args.run_date)

    print("抓取日：%s" % args.run_date)
    print("輸出到：%s" % raw_dir)
    print("共 %d 支" % len(todo))
    print()

    results = []
    for key, script, authority, segments in todo:
        extra = ["--skip-broker-pdf"] if (key == "insurance" and args.skip_broker_pdf) else []
        r = run_one(key, script, args.run_date, extra)
        r.update(authority=authority, segments=segments)
        results.append(r)
        print("    %s%s" % ("完成" if r["rc"] == 0 else "**未完成**",
                            "（%s）" % r["note"] if r["note"] else ""))

    # ── 匯總 ──────────────────────────────────────────────────────────────
    produced = sorted(os.path.basename(p) for p in glob.glob(os.path.join(raw_dir, "*"))
                      if not os.path.basename(p).startswith("00_"))
    reports = [f for f in produced if f.endswith((".md", ".txt"))]

    L = []
    A = L.append
    A("# 名冊抓取異動總表")
    A("")
    A("- 抓取日：%s" % args.run_date)
    A("- 執行 %d 支，成功 %d 支" % (len(results), sum(1 for r in results if r["rc"] == 0)))
    A("- 輸出目錄：`%s`" % raw_dir)
    A("")
    A("## 執行結果")
    A("")
    A("| 主管機關 | 腳本 | 結果 | 涵蓋 segment |")
    A("|---|---|---|---|")
    for r in results:
        A("| %s | `%s` | %s | %s |" % (
            r["authority"], r["script"],
            "完成" if r["rc"] == 0 else "**未完成**（%s）" % (r["note"] or "見下方輸出"),
            r["segments"]))
    A("")
    A("## 各支的異動報告")
    A("")
    if reports:
        for f in reports:
            A("- `%s`" % f)
    else:
        A("（本次未產生任何報告檔——代表六支都沒成功，請看下方輸出尾段）")
    A("")
    A("## 下一步（人工）")
    A("")
    A("1. 逐份讀上列異動報告，對每筆異動決定：新增（`status=active`）／更名（`renamed`）")
    A("   ／併購消滅（`merged`，**不要刪列**）／退場（`exited`）")
    A("2. 更新 `crawlers/masters/` 的三張列源名冊；`segment` 只能用 `_taxonomy.py` 定義的 24 個值")
    A("3. 新增機構若無統編，跑 `py -3.12 crawlers/query_pending_taxids.py` 補查")
    A("4. 重建：`py -3.12 crawlers/build_authority_master.py`")
    A("   （列數守門會擋——確認是刻意更新後再改 `SOURCE_INPUTS` 的期望列數）")
    A("5. 部署：把 `crawlers/build/authority_master.csv` 複製到 `data/`，")
    A("   然後跑 `py -3.12 tests/test_consistency.py` 驗收")
    A("")
    A("> 這一段沒有自動化路徑，是刻意的：名冊異動涉及法人身分判斷（同一法人跨 segment、")
    A("> 兼營、更名 vs 新設），誤判會直接汙染分類事實層。詳見 `docs/名冊維護與爬蟲.md`。")

    failed = [r for r in results if r["rc"] != 0]
    if failed:
        A("")
        A("## 未完成的支（輸出尾段）")
        for r in failed:
            A("")
            A("### %s" % r["script"])
            A("")
            A("```")
            A(r["tail"] or "（無輸出）")
            A("```")

    out = os.path.join(raw_dir, "00_異動總表.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")

    print()
    print("匯總寫入 %s" % out)
    print("產生檔案 %d 個" % len(produced))
    print()
    print("※ 名冊異動需人工判讀後才會進 authority_master.csv——見總表的「下一步」一節。")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
