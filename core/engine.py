# -*- coding: utf-8 -*-
r"""判定引擎 — 執行 rules.py 的規則瀑布（本地端與 API 端共用同一份邏輯）

設計要點
────────
本檔**不碰任何檔案、不打任何網路**。所有資料存取都經由 provider 物件，
由 local/provider.py（讀全檔）或 api/provider.py（打單筆 API）提供。
兩種模式共用同一個引擎，是「同一份輸入必得同一份結果」的結構保證——
邏輯只有一份，不會出現本地端與 API 端判斷不同的情況。

provider 必須提供的介面（見 core/provider_base.py）
    tax(tax_id)        -> {"name","org","codes":[(6碼,名稱),...]} 或 None
    authority(tax_id)  -> [名冊列 dict, ...]（可空）
    gov(tax_id)        -> 機關名稱 或 None
    school(tax_id)     -> 學校名稱 或 None
    nonprofit(tax_id)  -> 團體名稱 或 None
    gcis_items(tax_id) -> {營業項目代碼, ...} 或 None（僅 L3-9 觸發時才呼叫）

判定順序（首次命中即定案）
    L1 特殊規則   → L1-2 周邊單位白名單 → L1-3 已裁決例外
    L2 權威名冊   → L2-1 金管會（＋L2-1R 其他金融細分）→ L2-2 機關 → L2-3 學校 → L2-4 非營利
    L3 稅籍碼表   → L3-1 醫療碼 → L3-2 六碼精確 → L3-3 金融特許四碼
                   → L3-5 教育法人二碼 → L3-6 法人名稱前綴
    L3-9 GCIS     → 名稱含訊號詞且稅籍非金融碼時，查所營事業有無債權收買
    L3-7 大類映射 → 稅籍主碼前二碼 → 一般企業子分類
    L3-8 名稱關鍵字（僅稅籍查無時）
    L4 兜底       → 一般企業／未細分
"""
import re
import sys

if __package__ in (None, ""):
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import rules as R
    import exceptions as X
else:
    from . import rules as R
    from . import exceptions as X

# 兼營多列時的主分類優先序（數字小者優先；同序按子分類字典序）
GROUP_PRIORITY = {"金控與銀行": 0, "證券期貨": 1, "保險": 2, "其他金融": 3, "電支支付": 4}
GROUP_PRIORITY_DEFAULT = 9


def normalize_tax_id(raw):
    """統編正規化：去空白／引號／全形空白，純數字不足 8 碼補前導零。

    來源系統常見兩種損壞：Excel 把統編當數字讀掉前導零、以及 16 碼折半重複。
    此處只處理前導零；折半損壞需在資料清理階段處理（見 docs/03_資料來源.md）。
    """
    t = (raw or "").strip().strip('"').replace("　", "").replace(" ", "")
    if t.isdigit() and 0 < len(t) < 8:
        t = t.zfill(8)
    return t


def resolve_name(tax_id, provider, fallback_name=""):
    """回傳 (官方正式名稱, 名稱來源)。與分類判定完全獨立。

    優先序：稅籍 → 機關名冊 → 學校名冊 → 非營利名冊 → L1-4 存量凍結表
            → 金管會名冊 → GCIS 公司登記 → 傳入的備用名稱（通常是帳務系統名）

    GCIS 排在倒數第二，用途是補「稅籍查無」的兩種現實情況：已解散或廢止的公司、
    以及外商在台（無台灣稅籍登記）。兩種模式都走同一順序，故結果一致；
    本地端如指定 offline，GCIS 這步會回 None，該筆改用備用名稱並標記。
    """
    t = provider.tax(tax_id)
    if t and t.get("name"):
        return t["name"], "稅籍(財政部)"
    for getter, label in ((provider.gov, "機關名冊"),
                          (provider.school, "學校名冊"),
                          (provider.nonprofit, "非營利名冊")):
        nm = getter(tax_id)
        if nm:
            return nm, label
    if tax_id in X.FROZEN_NAMES:
        return X.FROZEN_NAMES[tax_id]
    rows = provider.authority(tax_id)
    if rows:
        nm = (rows[0].get("customer_name") or "").strip()
        if nm:
            return nm, "金管會名冊"
    comp = provider.company(tax_id)
    if comp and comp.get("name"):
        return comp["name"], "GCIS商工登記"
    return fallback_name, "帳務名(待補官方全銜)"


def _pick_authority_row(rows):
    """兼營多列 → 取主分類單列（穩定排序，不受檔案列序影響）"""
    return sorted(rows, key=lambda r: (GROUP_PRIORITY.get(r.get("industry_group"),
                                                          GROUP_PRIORITY_DEFAULT),
                                       r.get("industry_detail", "")))[0]


def _refine_other_finance(tax_id, name, provider):
    """L2-1R：其他金融子分類細分。

    前提是已由 L2-1（租賃公會名錄）或 L1-3 判為其他金融——有此前提後，
    稅籍的汽機車碼才可安全使用；單獨用那些碼會掃到全國一萬多家汽車百貨。
    """
    t = provider.tax(tax_id)
    code = t["codes"][0][0] if (t and t.get("codes")) else ""
    if code[:6] == "649100":
        return "融資租賃", "稅籍 649100 金融租賃"
    if code[:6] == "649699":
        return "民間融資", "稅籍 649699 其他民間融資"
    if code[:4] in R.AUTO4:
        return "汽機車分期", "稅籍 %s 屬汽機車製造／批發／零售" % code
    if "租賃" in name:
        return "融資租賃", "名稱含「租賃」"
    return "其他融資", "稅籍碼與名稱皆無融資訊號"


def _match_name_rules(name):
    """L3-8：名稱關鍵字（僅稅籍查無時使用）。回 (大類, 子類, 說明, 命中詞) 或 None。"""
    for rid, group, sub, keywords in R.NAME_RULES:
        for kw in keywords:
            hit = re.search(kw, name) if kw.endswith("$") else (kw in name)
            if hit:
                shown = kw.rstrip("$").replace("(股份)?", "")
                return group, sub, "%s 名稱關鍵字「%s」" % (rid, shown), shown
    return None


class Verdict:
    """一次判定的完整結果。

    basis（分類依據詞）在判定點直接產生，不從中文句子反解——
    這樣規則措辭怎麼改，依據詞都不會靜默失準。
    """

    __slots__ = ("group", "sub", "layer", "rule", "basis", "confidence")

    def __init__(self, group, sub, layer, rule, basis, confidence):
        self.group, self.sub = group, sub
        self.layer, self.rule = layer, rule
        self.basis, self.confidence = basis, confidence


def classify(tax_id, name, provider):
    """回傳 Verdict。首次命中即定案。"""
    tax = provider.tax(tax_id)
    code = tax["codes"][0][0] if (tax and tax.get("codes")) else ""
    code_label = tax["codes"][0][1] if (tax and tax.get("codes")) else ""
    c6, c4, c2 = code[:6], code[:4], code[:2]

    # ── L1 特殊規則 ──────────────────────────────────────────────────────
    if tax_id in X.PERIPHERAL:
        return Verdict("政府機關", "周邊單位", "L1 特殊規則",
                       "L1-2 周邊單位白名單（金融市場基礎設施）",
                       "白名單：政府周邊單位", "high")
    if tax_id in X.OVERRIDE:
        group, sub, why, when = X.OVERRIDE[tax_id]
        head = why.split("；")[0]
        if "：" in head and len(head.split("：")[0]) <= 18:
            head = head.split("：", 1)[1]
        return Verdict(group, sub, "L1 特殊規則",
                       "L1-3 已裁決例外（%s）：%s" % (when, why),
                       "人工裁決：" + head[:40], "high")

    # ── L2 權威名冊 ──────────────────────────────────────────────────────
    rows = provider.authority(tax_id)
    if rows:
        picked = _pick_authority_row(rows)
        group = picked.get("industry_group", "")
        sub = picked.get("industry_detail", "")
        src = picked.get("source", "")
        src_short = re.split(r"[（(]", src)[0].strip()
        if group == "其他金融":
            sub, why = _refine_other_finance(tax_id, name, provider)
            return Verdict(group, sub, "L2 權威名冊",
                           "L2-1 %s ＋ L2-1R 細分（%s）" % (src, why),
                           "名冊：%s　＋　%s" % (src_short, why), "high")
        return Verdict(group, sub, "L2 權威名冊", "L2-1 金管會名冊（%s）" % src,
                       "名冊：%s" % src_short, "high")
    if provider.gov(tax_id):
        return Verdict("政府機關", "政府機關", "L2 權威名冊",
                       "L2-2 行政院／地方機關名冊", "名冊：行政院／地方機關名冊", "high")
    if provider.school(tax_id):
        return Verdict("教育與法人", "學校", "L2 權威名冊",
                       "L2-3 全國各級學校名冊", "名冊：全國各級學校名冊", "high")
    if provider.nonprofit(tax_id) and c2 not in R.NONPROFIT_MEDICAL_EXCEPTION:
        return Verdict("教育與法人", "財團法人與公協會", "L2 權威名冊",
                       "L2-4 非營利事業機關團體名冊",
                       "名冊：非營利事業機關團體名冊", "high")

    # ── L3 稅籍碼表（特定碼優先於大類映射）──────────────────────────────
    if code:
        if c2 in R.MEDICAL2:
            return Verdict("一般企業", "醫療", "L3 稅籍碼表",
                           "L3-1 醫療碼 %s %s" % (code, code_label),
                           "稅籍主碼 %s %s" % (code, code_label), "high")
        if c6 in R.FIN6:
            group, sub, label = R.FIN6[c6]
            return Verdict(group, sub, "L3 稅籍碼表",
                           "L3-2 稅籍6碼精確 %s %s" % (code, label),
                           "稅籍主碼 %s %s" % (code, label), "high")
        if c4 in R.FIN4:
            group, sub, label = R.FIN4[c4]
            return Verdict(group, sub, "L3 稅籍碼表",
                           "L3-3 金融特許碼 %s %s" % (code, label),
                           "稅籍主碼 %s %s" % (code, label), "high")
        if c2 in R.EDU_CORP2:
            group, sub, label = R.EDU_CORP2[c2]
            return Verdict(group, sub, "L3 稅籍碼表",
                           "L3-5 稅籍大類%s %s" % (c2, label),
                           "稅籍大類 %s %s" % (c2, label), "medium")
        if any(k in name for k in R.CORP_PREFIX):
            return Verdict("教育與法人", "財團法人與公協會", "L3 名稱關鍵字",
                           "L3-6 法人名稱前綴（稅籍主碼 %s 為營業碼）" % code,
                           "名稱含法人前綴（財團法人／基金會／公會等）", "medium")

    # ── L3-9 GCIS 所營事業（條件觸發）────────────────────────────────────
    if any(w in name for w in R.GCIS_TRIGGER_WORDS):
        items = provider.gcis_items(tax_id)
        if items:
            has_debt = any(i in items for i in R.GCIS_DEBT_ITEMS)
            has_lease = R.GCIS_LEASE_ITEM in items
            if has_debt and has_lease:
                return Verdict("其他金融", "消費分期", "L3-9 GCIS所營事業",
                               "L3-9 所營事業含金融機構金錢債權收買＋租賃業",
                               "所營事業 HZ02010 債權收買＋JE01010 租賃業", "medium")
            if has_debt:
                return Verdict("其他金融", "債權媒合", "L3-9 GCIS所營事業",
                               "L3-9 所營事業含金融機構金錢債權收買（無租賃業）",
                               "所營事業 HZ02010 債權收買", "medium")

    # ── L3-7 稅籍大類映射 ────────────────────────────────────────────────
    if code:
        label = R.MAJOR2_LABEL.get(c2, code_label)
        return Verdict("一般企業", R.SUB_BY_MAJOR2.get(c2, "其他"), "L3 稅籍碼表",
                       "L3-7 稅籍大類%s %s" % (c2, label),
                       "稅籍大類 %s %s" % (c2, label), "medium")

    # ── L3-8 名稱關鍵字（稅籍查無）───────────────────────────────────────
    hit = _match_name_rules(name)
    if hit:
        group, sub, why, kw = hit
        return Verdict(group, sub, "L3 名稱關鍵字", "L3-8 " + why + "（稅籍查無）",
                       "名稱含「%s」" % kw, "medium")

    # ── L4 兜底 ──────────────────────────────────────────────────────────
    return Verdict(R.FALLBACK[0], R.FALLBACK[1], "L4 兜底", "L4 " + R.FALLBACK[2],
                   "無任何官方依據（兜底歸一般企業）", "low")


def query(raw_tax_id, provider, fallback_name="", as_of=""):
    """單筆查詢的公開入口：統編 → 完整判定結果 dict。"""
    t0 = normalize_tax_id(raw_tax_id)
    tax_id, fix_note = (X.TAX_ID_FIX[t0][0], X.TAX_ID_FIX[t0][1]) if t0 in X.TAX_ID_FIX else (t0, "")
    name, name_source = resolve_name(tax_id, provider, fallback_name)
    v = classify(tax_id, name, provider)
    confidence = v.confidence
    status = X.FROZEN_STATUS.get(tax_id, "")
    if status and status not in ("核准設立", "核准登記"):
        confidence = "low"
    if not name:                                     # 名稱完全查不到時誠實標記
        name_source = "查無官方名稱"
        confidence = "low"
    return {
        "統一編號": tax_id,
        "官方正式名稱": name,
        "大分類": v.group,
        "子分類": v.sub,
        "分類依據詞": v.basis,
        "分類依據層": v.layer,
        "判定規則": v.rule,
        "名稱來源": name_source,
        "登記狀態": status,
        "信心": confidence,
        "統編備註": fix_note,
        "查詢模式": provider.mode_name,
        "資料版本": provider.data_version,
        "判定日": as_of,
    }


OUTPUT_COLUMNS = ["統一編號", "官方正式名稱", "大分類", "子分類", "分類依據詞",
                  "分類依據層", "判定規則", "名稱來源", "登記狀態", "信心",
                  "統編備註", "查詢模式", "資料版本", "判定日"]
