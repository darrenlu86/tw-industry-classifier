# -*- coding: utf-8 -*-
r"""驗收測試：兩種模式一致性 ＋ 重現性

這兩件事是本工具的核心宣稱，必須可驗證，不能只寫在文件裡。

  test 1  同一份清單，local 與 api 兩種模式的判定結果逐欄相同
          （排除 查詢模式／資料版本 兩欄——它們本來就該不同）
  test 2  local 模式連續執行兩次，輸出檔 SHA-256 完全相同
  test 3  刪除快取後重跑（冷啟動），結果仍與前兩次相同
  test 4  輸入順序打亂後重跑，結果仍相同（輸出排序不依賴輸入順序）
  test 5  值域自我檢查：每筆子分類都在 rules.SUBGROUPS 的合法值域內

用法：
    py -3.12 tests/test_consistency.py                       # 用示範清單
    py -3.12 tests/test_consistency.py --input 你的清單.csv    # 用自己的清單
    py -3.12 tests/test_consistency.py --skip-api            # 只測本地端（無外網時）

離線注意：test 1 需要外網（api 模式必然連外）。無外網請加 --skip-api，
其餘四項仍會執行——但若清單中有統編觸發 L3-9（GCIS），本地端也需要外網，
此時請改用 --offline-local，該層會被跳過並在輸出如實標記。
"""
import argparse
import csv
import hashlib
import os
import random
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "core"))

import rules as R  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

TMP = os.path.join(HERE, "_tmp")
MODE_SPECIFIC = {"查詢模式", "資料版本"}      # 兩模式本來就不同的欄位


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 16), b""):
            h.update(blk)
    return h.hexdigest()


def load(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def run(mode, input_path, out_name, extra=()):
    out = os.path.join(TMP, out_name)
    cmd = [sys.executable, os.path.join(ROOT, "classify.py"),
           "--mode", mode, "--input", input_path, "--output", out, *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    if proc.returncode != 0:
        print("  執行失敗（%s）：\n%s" % (mode, (proc.stderr or proc.stdout)[-1200:]))
        return None
    return out


def diff_rows(a, b, label_a, label_b):
    """逐欄比對兩份結果（以統編為鍵）。回差異清單。"""
    ka = {r["統一編號"]: r for r in a}
    kb = {r["統一編號"]: r for r in b}
    diffs = []
    only_a = sorted(set(ka) - set(kb))
    only_b = sorted(set(kb) - set(ka))
    for t in only_a:
        diffs.append((t, "（整列）", "有", "無"))
    for t in only_b:
        diffs.append((t, "（整列）", "無", "有"))
    for t in sorted(set(ka) & set(kb)):
        for col in ka[t]:
            if col in MODE_SPECIFIC:
                continue
            va, vb = ka[t].get(col, ""), kb[t].get(col, "")
            if va != vb:
                diffs.append((t, col, va, vb))
    return diffs


def main():
    ap = argparse.ArgumentParser(description="兩模式一致性與重現性驗收")
    ap.add_argument("--input", default=os.path.join(ROOT, "input", "taxids_sample.csv"))
    ap.add_argument("--skip-api", action="store_true", help="不測 API 模式（無外網時）")
    ap.add_argument("--offline-local", action="store_true", help="本地端加 --offline 執行")
    args = ap.parse_args()

    if os.path.isdir(TMP):
        shutil.rmtree(TMP)
    os.makedirs(TMP)
    local_extra = ("--offline",) if args.offline_local else ()
    failures = []

    print("輸入清單：%s" % args.input)
    print("規則版本 v3（rules.py）")
    print()

    # ── test 2／3：重現性 ────────────────────────────────────────────────
    print("[1] 本地端重現性（連續兩次 ＋ 冷啟動一次）")
    h = []
    for i in (1, 2):
        p = run("local", args.input, "local_run%d.csv" % i, local_extra)
        if not p:
            failures.append("本地端執行失敗")
            break
        h.append(sha256(p))
        print("    第 %d 次 SHA-256 = %s" % (i, h[-1][:24]))
    cache = os.path.join(ROOT, "data", "_gcis_cache.json")
    bak = cache + ".testbak"
    had = os.path.exists(cache)
    if had:
        shutil.copy2(cache, bak)
        os.remove(cache)
    p3 = run("local", args.input, "local_run3.csv", local_extra)
    if had:
        shutil.move(bak, cache)
    if p3:
        h.append(sha256(p3))
        print("    冷啟動   SHA-256 = %s" % h[-1][:24])
    if len(h) == 3 and len(set(h)) == 1:
        print("    ✓ 三次完全相同")
    else:
        failures.append("重現性：三次雜湊不一致 %s" % [x[:12] for x in h])
        print("    ✗ 不一致")

    # ── test 4：輸入順序無關 ─────────────────────────────────────────────
    print()
    print("[2] 輸入順序無關性")
    rows = load(args.input) if False else None          # 以原始 CSV 逐列打亂
    with open(args.input, encoding="utf-8-sig", newline="") as f:
        raw = list(csv.reader(f))
    head, body = raw[0], raw[1:]
    random.Random(20260730).shuffle(body)
    shuffled = os.path.join(TMP, "shuffled_input.csv")
    with open(shuffled, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows([head] + body)
    p4 = run("local", shuffled, "local_shuffled.csv", local_extra)
    if p4 and h:
        if sha256(p4) == h[0]:
            print("    ✓ 打亂輸入順序後結果相同")
        else:
            d = diff_rows(load(p4), load(os.path.join(TMP, "local_run1.csv")), "亂序", "原序")
            failures.append("輸入順序影響結果（%d 處差異）" % len(d))
            print("    ✗ 有 %d 處差異" % len(d))

    # ── test 5：值域 ─────────────────────────────────────────────────────
    print()
    print("[3] 值域自我檢查")
    base = load(os.path.join(TMP, "local_run1.csv")) if h else []
    bad = [r for r in base if r["子分類"] not in R.SUBGROUPS.get(r["大分類"], [])]
    print("    %s 越界 %d 筆" % ("✓" if not bad else "✗", len(bad)))
    if bad:
        failures.append("值域越界 %d 筆：%s" % (len(bad), [(b["統一編號"], b["子分類"]) for b in bad[:5]]))

    # ── test 1：兩模式一致 ───────────────────────────────────────────────
    print()
    print("[4] 兩種模式一致性（local vs api）")
    if args.skip_api:
        print("    － 已用 --skip-api 跳過")
    else:
        pa = run("api", args.input, "api_run.csv")
        if not pa:
            failures.append("API 模式執行失敗")
        elif base:
            d = diff_rows(base, load(pa), "local", "api")
            if not d:
                print("    ✓ %d 筆逐欄相同（排除 查詢模式／資料版本）" % len(base))
            else:
                print("    ✗ %d 處差異：" % len(d))
                for t, col, va, vb in d[:12]:
                    print("        %s ／ %s：local=%r  api=%r" % (t, col, va, vb))
                failures.append("兩模式差異 %d 處" % len(d))

    print()
    print("─" * 62)
    if failures:
        print("驗收未通過（%d 項）：" % len(failures))
        for f in failures:
            print("  - %s" % f)
        sys.exit(1)
    print("全部通過。")
    sys.exit(0)


if __name__ == "__main__":
    main()
