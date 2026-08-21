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


# ── L0 ────────────────────────────────────────────────────────────────────
# UID 前綴表刻意以通用措辭描述：它是可設定的對照表（rules.UID_PREFIX），
# 換一個組織換一組前綴，文件不用重寫。
add("L0-AA", "L0 統編解析", "財政部外國機構統編配賦（格式規則，不帶名冊）", "統一編號",
    "符合 %s（不分大小寫）" % R.UID_SPECIAL_PATTERN,
    R.UID_SPECIAL_GROUP, R.UID_SPECIAL_SUB, "high",
    "使領館與駐台辦事處無公司登記，另配賦 AA＋3 碼特種統編。命中即終結")
for _prefix, _group in sorted(R.UID_PREFIX.items()):
    add("L0-%s" % _prefix, "L0 統編解析", "輸入值本身", "統一編號首碼",
        "= %s（不分大小寫）" % _prefix, _group, "（空白）", "high",
        "命中即終結，不再進任何名冊／稅籍／名稱層")
add("L0-N", "L0 統編解析", "本地例外表（exceptions/local_exceptions.json）", "官方名稱",
    "統編空白／N-A，且命中無統編歸戶表（筆數依本機例外檔，--doctor 查看）",
    "（依表值）", "（依表值）", "high", "名稱完全比對。內容不進版控；查無則歸「無法分類」")
add("L0-X", "L0 統編解析", "輸入值本身", "統一編號",
    "非 8 碼數字，且首碼不在 UID 前綴表", R.UNCLASSIFIED[0], "（空白）", "low",
    "統編備註標「%s」；8 碼數字才續走 L1" % R.UID_FORMAT_NOTE)

# ── L1 ────────────────────────────────────────────────────────────────────
# 本地例外表（L1-1／L1-3／L1-4／L1-5）的**內容**刻意不寫進這份文件——
# 那些是針對特定機構的人工裁決，理由常涉及業務關係，不該進版控的文件。
# 這裡只記「有這一層、目前幾筆」；內容由使用者自己在
# exceptions/local_exceptions.json 維護，格式見 exceptions/README.md。
add("L1-1", "L1 特殊規則", "本地例外表（exceptions/local_exceptions.json）", "統一編號",
    "命中統編歸戶表（筆數依本機例外檔，--doctor 查看）",
    "（改用歸戶統編查詢後續各層）", "—", "high",
    "查詢重導向，**不改寫輸出的統一編號**（輸出保留輸入原值，否則 join 不回原始報表）；"
    "統編備註記「統編歸戶：查詢採 …」。內容不進版控")
for tid, (name, peri_group) in sorted(X.PERIPHERAL_BUILTIN.items()):
    add("L1-2", "L1 特殊規則", "內建白名單（core/exceptions.py）",
        "統一編號", "= %s" % tid, peri_group, "周邊單位", "high", name)
add("L1-2", "L1 特殊規則", "本地例外表（exceptions/local_exceptions.json）", "統一編號",
    "命中本地追加周邊單位（筆數依本機例外檔，--doctor 查看）",
    "（依所屬大類）", "周邊單位", "high",
    "組織自行認定的周邊單位（公協會等），值＝[名稱, 所屬大類]。內容不進版控")
add("L1-3", "L1 特殊規則", "本地例外表（exceptions/local_exceptions.json）", "統一編號",
    "命中已裁決例外表（筆數依本機例外檔，--doctor 查看）",
    "（依裁決值）", "（依裁決值）", "high",
    "名冊或稅籍碼判定與事實不符時的人工裁決，每筆附理由與日期。內容不進版控")
add("L1-3R", "L1 特殊規則", "同上", "裁決值形態",
    "大分類填 A–S 十九大類或「%s」（行業軌值形態）" % R.UNMAPPED_SECTION,
    "一般企業", "未細分", "high",
    "單軌直接採裁決值（子分類不受 SUBGROUPS 限制），身分軌降為一般企業／未細分。"
    "僅應用於官方來源全查無的戶——對有稅籍者填會蓋掉稅籍，見 exceptions/README.md 警語。"
    "登記狀態為解散類時仍由 L3-D 覆寫")

# ── L2 ────────────────────────────────────────────────────────────────────
for reg in R.REGISTRIES:
    gives = reg["gives"].split("／")
    add(reg["id"], "L2 權威名冊", reg["source"], reg["key"],
        "命中名冊（%s 列）" % f"{reg['rows']:,}",
        gives[0], gives[1] if len(gives) > 1 else "（依名冊值）", "high", reg["note"])

# ── L3-A ──────────────────────────────────────────────────────────────────
_MATCH_LABEL = {"contains": "名稱含", "contains_all": "名稱同時含", "endswith": "名稱以…結尾",
                "contains_ext": "名稱含（校詞之後須帶延續詞）"}
add("L3-A 前置排除", "L3-A 制度性名稱", "官方正式名稱", "名稱含（整層跳過）",
    "／".join(R.CORP_PREFIX), "—", "—", "—",
    "法人與公協會交給 L2-4 非營利名冊與 L3-6 法人前綴，L3-A 不介入")
for rid, group, sub, mode, kws, conf in R.INSTITUTIONAL_RULES:
    note = "早於稅籍層：這類實體的稅籍主碼常是附屬營業碼，放稅籍後會被蓋掉"
    excl = R.INSTITUTIONAL_KEYWORD_EXCLUDE.get(rid, ())
    if excl:
        note = "排除 " + "／".join(excl) + "；" + note
    add("L3-A/%s" % rid, "L3-A 制度性名稱", "官方正式名稱",
        _MATCH_LABEL[mode] + ("（且無店家型後綴）" if rid in R.CORP_SUFFIX_GUARD_RULES else ""),
        "／".join(kws), group, sub, conf, note)
add("L3-A 排除", "L3-A 制度性名稱", "—",
    "公司／店家型後綴（%s 適用）" % "／".join(R.CORP_SUFFIX_GUARD_RULES),
    "／".join(R.CORP_SUFFIX_EXCLUDE), "—", "—", "—",
    "防呆：名稱含這些後綴者是營業人，避免收進「大學光學科技股份有限公司」這類真公司")
add("L3-A/A3b 白名單", "L3-A 制度性名稱", "官方正式名稱", "第一個校詞之後的字串須含",
    "／".join(R.SCHOOL_EXTENSION_WORDS), "（不含則跳過 A3b）", "—", "—",
    "校內單位命名公式化，店家不是——用延續詞問，比逐一列舉店家業態收斂")
add("L3-A/A3b 黑名單", "L3-A 制度性名稱", "官方正式名稱", "全名含（否決 A3b）",
    "／".join(R.SCHOOL_EXTENSION_BLOCK), "—", "—", "—",
    "住宅社區管委會：名稱裡的「大學」是建案名或地名，不是學校")

# ── L3-B ──────────────────────────────────────────────────────────────────
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
    excl = R.NAME_RULE_SUFFIX_EXCLUDE.get(rid, ())
    add("L3-8/%s" % rid, "L3 名稱關鍵字", "官方正式名稱（僅稅籍查無時）", "名稱含",
        "／".join(k.rstrip("$").replace("(股份)?", "") + ("（尾字）" if k.endswith("$") else "")
                 for k in kws),
        group, sub, "medium",
        "順序即優先序" + ("；排除尾綴 " + "／".join(excl) if excl else ""))
add("L4", "L4 兜底", "—", "—", "以上全未命中", R.FALLBACK[0], R.FALLBACK[1], "low", R.FALLBACK[2])

# ── 單軌合併（產業大類）──────────────────────────────────────────────────
# 這兩層不決定身分軌的大分類／子分類，只換單軌的產業大類／產業子類，
# 所以排在 L4 之後獨立成一節。
add("L2-5", "單軌合併（產業大類）",
    "證交所 t187ap03_L＋櫃買 mopsfin_t187ap03_O（data/listed_master.csv）", "統一編號",
    "身分軌＝一般企業且稅籍查無，命中上市櫃名冊",
    "（對照 listed_industry_map.csv 的 A–S 大類）", "（名冊產業別名稱）", "high",
    "缺檔即跳層；依據詞帶市場別（TWSE／TPEx）與產業別代碼")
add("L2-5R", "單軌合併（產業大類）", "同上", "產業別代碼",
    "命中名冊，但該代碼在對照表無 A–S 歸屬（其他業／綠能環保／存託憑證）",
    R.UNMAPPED_SECTION, "（名冊產業別名稱）", "medium",
    "上市櫃身分官方已證實、產業歸屬官方沒給——標「其他」而不硬塞一個猜出來的大類")
add("L3-D", "單軌合併（產業大類）", "GCIS 商工登記／L1-5 凍結登記狀態表", "登記狀態",
    "含 %s 任一者（**不分身分軌群組**）" % "／".join(R.DISSOLVED_STATUS_WORDS),
    R.DISSOLVED_SECTION, "（空白）", "high",
    "最後套用，覆蓋前面各層的產業大類；身分軌保留原群組值供稽核。"
    "凍結表命中不需連線；--offline 且無 GCIS 快取時本層跳過並在統編備註標記。信心降 low")

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
    A("| %s | %s |" % (g, "、".join(s or "（空白）" for s in R.SUBGROUPS[g])))
A("")
A("單軌（產業大類）另有三個專屬值，不屬於上表的身分軌值域"
  "（`classify.py` 的值域自我檢查驗的是身分軌，與這三個值無關）：")
A("")
A("| 單軌專屬值 | 何時出現 | 產業子類 |")
A("|---|---|---|")
A("| %s | 稅籍查無，且 L2-5 也沒命中 | 空白 |" % R.TAX_MISSING_SECTION)
A("| %s | L2-5 命中，但該產業別代碼官方無 A–S 對應 | 名冊產業別名稱原值 |"
  % R.UNMAPPED_SECTION)
A("| %s | 登記狀態含 %s（**最後套用，覆蓋前兩者**） | 空白 |"
  % (R.DISSOLVED_SECTION, "／".join(R.DISSOLVED_STATUS_WORDS)))
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
A("| 表 | 用途 | 筆數 |")
A("|---|---|---|")
A("| L1-4 凍結官方名稱 | 四個名冊都查無名稱時（已解散公司、外商在台、"
  "機關名冊未收錄的機關），人工查證一次後凍結 | 依本機例外檔（--doctor 查看） |")
A("| L1-5 凍結登記狀態 | 標記已知終止登記者；填了該筆信心降為 low，進季度複核 |"
  " 依本機例外檔（--doctor 查看） |")
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
