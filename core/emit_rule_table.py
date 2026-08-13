# -*- coding: utf-8 -*-
r"""把 rules.py 匯出成「分類邏輯表」（CSV ＋ Markdown）

CSV 供程式／Excel 直接消費；Markdown 供人閱讀與季度複核。
兩者都由 rules.py 生成，所以文件與程式永不漂移——這是刻意的設計：
規則一改，重跑本檔，文件就跟著對。不要手動維護規則文件。

用法：
    py -3.12 core/emit_rule_table.py
產出：docs/分類邏輯表.csv、docs/分類邏輯表.md
"""
import csv
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import rules as R  # noqa: E402
import exceptions as X  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DOCS = os.path.join(ROOT, "docs")
rows, seq = [], 0


def add(rid, layer, source, key, cond, group, sub, conf, note=""):
    global seq
    seq += 1
    rows.append({"優先序": seq, "規則編號": rid, "判定層": layer, "比對鍵": key, "條件": cond,
                 "大分類": group, "子分類": sub, "信心": conf, "資料來源": source, "備註": note})


# ── L1 ────────────────────────────────────────────────────────────────────
# 本地例外表（L1-1／L1-3／L1-4／L1-5）的**內容**刻意不寫進這份文件——
# 那些是針對特定機構的人工裁決，理由常涉及業務關係，不該進版控的文件。
# 這裡只記「有這一層、目前幾筆」；內容由使用者自己在
# exceptions/local_exceptions.json 維護，格式見 exceptions/README.md。
# 需要含內容的完整版給內部看時，加 --include-local（輸出檔已在 .gitignore）。
add("L1-1", "L1 特殊規則", "本地例外表（exceptions/local_exceptions.json）", "統一編號",
    "命中統編修正表（目前 %d 筆）" % len(X.TAX_ID_FIX),
    "（先改判統編後重跑）", "—", "high",
    "帳務系統的佔位碼或已知錯碼 → 正確統編。內容不進版控")
for tid, (name, peri_group) in sorted(X.PERIPHERAL_BUILTIN.items()):
    add("L1-2", "L1 特殊規則", "內建白名單（core/exceptions.py）",
        "統一編號", "= %s" % tid, peri_group, "周邊單位", "high", name)
add("L1-2", "L1 特殊規則", "本地例外表（exceptions/local_exceptions.json）", "統一編號",
    "命中本地追加周邊單位（目前 %d 筆）" % (len(X.PERIPHERAL) - len(X.PERIPHERAL_BUILTIN)),
    "（依所屬大類）", "周邊單位", "high",
    "組織自行認定的周邊單位（公協會等），值＝[名稱, 所屬大類]。內容不進版控")
add("L1-3", "L1 特殊規則", "本地例外表（exceptions/local_exceptions.json）", "統一編號",
    "命中已裁決例外表（目前 %d 筆）" % len(X.OVERRIDE),
    "（依裁決值）", "（依裁決值）", "high",
    "名冊或稅籍碼判定與事實不符時的人工裁決，每筆附理由與日期。內容不進版控")

# ── L2 ────────────────────────────────────────────────────────────────────
for reg in R.REGISTRIES:
    gives = reg["gives"].split("／")
    add(reg["id"], "L2 權威名冊", reg["source"], reg["key"],
        "命中名冊（%s 列）" % f"{reg['rows']:,}",
        gives[0], gives[1] if len(gives) > 1 else "（依名冊值）", "high", reg["note"])

# ── L3 ────────────────────────────────────────────────────────────────────
for c2, label in R.MEDICAL2.items():
    add("L3-1", "L3 稅籍碼表", "財政部稅籍", "主行業代號前2碼", "= %s（%s）" % (c2, label),
        "一般企業", "醫療", "high", "須早於 L3-6 與 L2-4，否則財團法人醫院會被判為法人")
for c6, (group, sub, label) in R.FIN6.items():
    add("L3-2", "L3 稅籍碼表", "財政部稅籍", "主行業代號6碼", "= %s（%s）" % (c6, label),
        group, sub, "high", "6 碼精確比對，避免「其他XX」尾碼誤收")
for c4, (group, sub, label) in R.FIN4.items():
    add("L3-3", "L3 稅籍碼表", "財政部稅籍", "主行業代號前4碼", "= %s（%s）" % (c4, label),
        group, sub, "high", "金融特許業")
for c2, (group, sub, label) in R.EDU_CORP2.items():
    add("L3-5", "L3 稅籍碼表", "財政部稅籍", "主行業代號前2碼", "= %s（%s）" % (c2, label),
        group, sub, "medium", "")
add("L3-6", "L3 名稱關鍵字", "官方正式名稱", "名稱含", "／".join(R.CORP_PREFIX),
    "教育與法人", "財團法人與公協會", "medium",
    "稅籍主碼為營業碼但實體是法人時的糾偏（稅籍「組織別」欄不可靠，故用名稱）")
for c2 in sorted(R.SUB_BY_MAJOR2):
    add("L3-7", "L3 稅籍碼表", "財政部稅籍", "主行業代號前2碼",
        "= %s（%s）" % (c2, R.MAJOR2_LABEL.get(c2, "")),
        "一般企業", R.SUB_BY_MAJOR2[c2], "medium", "完整覆蓋 01–99，保證不落空")
for rid, group, sub, kws in R.NAME_RULES:
    add("L3-8/%s" % rid, "L3 名稱關鍵字", "官方正式名稱（僅稅籍查無時）", "名稱含",
        "／".join(k.rstrip("$").replace("(股份)?", "") + ("（尾字）" if k.endswith("$") else "")
                 for k in kws),
        group, sub, "medium", "順序即優先序")
add("L4", "L4 兜底", "—", "—", "以上全未命中", R.FALLBACK[0], R.FALLBACK[1], "low", R.FALLBACK[2])

os.makedirs(DOCS, exist_ok=True)
cols = ["優先序", "規則編號", "判定層", "比對鍵", "條件", "大分類", "子分類", "信心", "資料來源", "備註"]
with open(os.path.join(DOCS, "分類邏輯表.csv"), "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)

# ── Markdown ──────────────────────────────────────────────────────────────
L = []
A = L.append
A("# 分類邏輯表")
A("")
A("> 由 `core/rules.py` 自動生成，共 **%d** 條規則。**首次命中即定案**。" % len(rows))
A("> 不要手改本檔——改 `rules.py` 後重跑 `py -3.12 core/emit_rule_table.py`。")
A("")
A("## 值域")
A("")
A("| 大分類 | 子分類 |")
A("|---|---|")
for g in R.GROUPS:
    A("| %s | %s |" % (g, "、".join(R.SUBGROUPS[g])))
A("")
A("## 規則明細")
cur = None
for r in rows:
    if r["判定層"] != cur:
        cur = r["判定層"]
        A("")
        A("### %s" % cur)
        A("")
        A("| # | 規則 | 比對鍵 | 條件 | → 大分類 | → 子分類 | 信心 | 備註 |")
        A("|---|---|---|---|---|---|---|---|")
    A("| %d | %s | %s | %s | %s | %s | %s | %s |"
      % (r["優先序"], r["規則編號"], r["比對鍵"], r["條件"], r["大分類"], r["子分類"],
         r["信心"], r["備註"][:70]))
A("")
A("### L1-4／L1-5 存量凍結表")
A("")
A("| 表 | 用途 | 目前筆數 |")
A("|---|---|---|")
A("| L1-4 凍結官方名稱 | 四個名冊都查無名稱時（已解散公司、外商在台、"
  "機關名冊未收錄的機關），人工查證一次後凍結 | %d |" % len(X.FROZEN_NAMES))
A("| L1-5 凍結登記狀態 | 標記已知終止登記者；填了該筆信心降為 low，進季度複核 | %d |"
  % len(X.FROZEN_STATUS))
A("")
A("這兩張表與 L1-1／L1-3 一樣放在 `exceptions/local_exceptions.json`，")
A("**內容不寫進本文件**——那是針對特定機構的人工查證結果。格式見 `exceptions/README.md`。")
A("")
A("## 官方名稱解析順序（與分類獨立）")
A("")
A("| 優先序 | 來源 | 取哪個欄位 | 更新頻率 |")
A("|---|---|---|---|")
for i, (name, field, freq) in enumerate(R.NAME_SOURCE_ORDER, 1):
    A("| %d | %s | %s | %s |" % (i, name, field, freq))

with open(os.path.join(DOCS, "分類邏輯表.md"), "w", encoding="utf-8") as f:
    f.write("\n".join(L) + "\n")

print("寫出 docs/分類邏輯表.csv 與 .md（%d 條規則）" % len(rows))
for k, v in Counter(r["判定層"] for r in rows).most_common():
    print("  %-18s %3d 條" % (k, v))
