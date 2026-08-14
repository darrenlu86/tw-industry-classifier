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

判定順序（首次命中即定案；v4 起 L2-1R 細分與 L3-9 GCIS 層已移除）
    L1 特殊規則   → L1-2 周邊單位白名單（帶所屬大類）→ L1-3 已裁決例外
    L2 權威名冊   → L2-1 金管會＋租賃公會 → L2-2 機關 → L2-3 學校 → L2-4 非營利
    L3 稅籍碼表   → L3-1 醫療碼 → L3-2 六碼精確 → L3-3 金融特許四碼
                   → L3-5 教育法人二碼 → L3-6 法人名稱前綴
    L3-7 大類映射 → 稅籍主碼前二碼 → 一般企業子分類
    L3-8 名稱關鍵字（僅稅籍查無時）
    L4 兜底       → 一般企業／未細分

輸出的「產業大類／產業子類」為定版單軌（2026-08-11）：身分軌命中者沿用大分類／
子分類；一般企業改由行業軌（稅籍主行業代號 → A–S 十九大類）回答，見 industry_track()。
"""
import re
import sys

if __package__ in (None, ""):
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import rules as R
    import exceptions as X
    from provider_base import normalize_tax_id
else:
    from . import rules as R
    from . import exceptions as X
    from .provider_base import normalize_tax_id

# 兼營多列時的主分類優先序（數字小者優先；同序按子分類字典序）
GROUP_PRIORITY = {"金控與銀行": 0, "證券期貨": 1, "保險": 2, "租賃": 3, "電支支付": 4}
GROUP_PRIORITY_DEFAULT = 9


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
        _, peri_group = X.PERIPHERAL[tax_id]
        return Verdict(peri_group, "周邊單位", "L1 特殊規則",
                       "L1-2 周邊單位白名單（金融市場基礎設施與公協會）",
                       "白名單：%s周邊單位" % peri_group, "high")
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


def industry_track(provider, tax_id):
    """行業軌：稅籍主行業代號 → (行業大類, 行業中類)。查無 →（未登記, 空）。

    行業大類＝稅務行業標準分類第 9 次修訂 A–S 十九大類，行業中類＝2 碼＋名稱。
    只取主碼、禁止人工覆寫（2026-08-03 定案）；官方空碼區實資料不會出現，如實標記。
    """
    t = provider.tax(tax_id)
    code = t["codes"][0][0] if (t and t.get("codes")) else ""
    if not code:
        return R.TAX_MISSING_SECTION, ""
    c2 = code[:2]
    sec = R.SECTION_BY_MAJOR2.get(c2)
    if not sec:                                      # 官方空碼區，實資料不會出現；如實標記
        return "未對應（中類 %s）" % c2, "%s %s" % (c2, R.MAJOR2_LABEL.get(c2, ""))
    return "%s %s" % sec, "%s %s" % (c2, R.MAJOR2_LABEL.get(c2, ""))


def query(raw_tax_id, provider, fallback_name="", as_of=""):
    """單筆查詢的公開入口：統編 → 完整判定結果 dict。

    產業大類／產業子類＝定版單軌（2026-08-11）：身分軌命中者直接沿用；
    一般企業（名冊查無執照）改走行業軌，由稅籍主行業代號歸 A–S 行業別。
    大分類／子分類保留身分軌原值，供稽核與八欄版並排比對；對外輸出時
    欄名標註「僅供參考」（2026-08-13，見 DISPLAY_RENAME／display()）。
    """
    t0 = normalize_tax_id(raw_tax_id)
    tax_id, fix_note = (X.TAX_ID_FIX[t0][0], X.TAX_ID_FIX[t0][1]) if t0 in X.TAX_ID_FIX else (t0, "")
    name, name_source = resolve_name(tax_id, provider, fallback_name)
    v = classify(tax_id, name, provider)
    if v.group == "一般企業":                        # 定版單軌：一般企業走行業軌
        ind_group, ind_sub = industry_track(provider, tax_id)
    else:
        ind_group, ind_sub = v.group, v.sub
    confidence = v.confidence
    status = X.FROZEN_STATUS.get(tax_id, "")
    if status and status not in ("核准設立", "核准登記"):
        confidence = "low"
    if not name:                                     # 名稱完全查不到時誠實標記
        name_source = "查無官方名稱"
        confidence = "low"
    if any(k.split(":", 1)[-1] == tax_id             # strict=False 降級筆：
           for k in getattr(provider, "degraded", ())):  # 任一端點失敗＝判定基礎不完整
        confidence = "low"
    return {
        "統一編號": tax_id,
        "官方正式名稱": name,
        "產業大類": ind_group,
        "產業子類": ind_sub,
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


OUTPUT_COLUMNS = ["統一編號", "官方正式名稱", "產業大類", "產業子類",
                  "大分類", "子分類", "分類依據詞",
                  "分類依據層", "判定規則", "名稱來源", "登記狀態", "信心",
                  "統編備註", "查詢模式", "資料版本", "判定日"]

# 權威答案是單軌的產業大類／產業子類；身分軌舊欄位對外標註「僅供參考」
# （2026-08-13）。只改呈現層欄名，程式內部鍵名不變——讀取方仍用原名。
DISPLAY_RENAME = {"大分類": "大分類（僅供參考）", "子分類": "子分類（僅供參考）"}


def display(rec):
    """query() 結果 → 對外呈現用 dict（套 DISPLAY_RENAME 欄名，欄序不變）。"""
    return {DISPLAY_RENAME.get(k, k): v for k, v in rec.items()}
