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
    L0 統編解析   → L0-P／L0-F 首碼命中 UID 前綴表 → L0-N 無統編歸戶表
                   → L0-X 格式異常（8 碼數字才續走 L1）
    L1 特殊規則   → L1-2 周邊單位白名單（帶所屬大類）→ L1-3 已裁決例外
    L2 權威名冊   → L2-1 金管會＋租賃公會 → L2-2 機關 → L2-3 學校 → L2-4 非營利
    L3-A 制度性名稱 → A1／A2 醫院診所 → A3 學校 → A4 事務所（v5，早於稅籍層）
    L3-B 稅籍碼表 → L3-1 醫療碼 → L3-2 六碼精確 → L3-3 金融特許四碼
                   → L3-5 教育法人二碼 → L3-6 法人名稱前綴
    L3-7 大類映射 → 稅籍主碼前二碼 → 一般企業子分類
    L3-C 名稱關鍵字 L3-8（僅稅籍查無時）
    L4 兜底       → 一般企業／未細分

輸出的「產業大類／產業子類」為定版單軌（2026-08-11，v5 擴充）：身分軌命中者沿用
大分類／子分類；一般企業改由行業軌回答，順序為
    稅籍主行業代號（A–S 十九大類，見 industry_track()）
    → L2-5 上市櫃名冊 → L3-D GCIS 登記狀態（已解散）→ 未登記（稅籍查無）
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
    """回傳 (官方正式名稱, 名稱來源, 登記狀態)。與分類判定完全獨立。

    優先序：稅籍 → 機關名冊 → 學校名冊 → 非營利名冊 → L1-4 存量凍結表
            → 金管會名冊 → L2-5 上市櫃名冊 → GCIS 公司登記
            → 傳入的備用名稱（通常是帳務系統名）

    GCIS 排在最後一個官方來源，用途是補「稅籍查無」的兩種現實情況：已解散或廢止的
    公司、以及外商在台（無台灣稅籍登記）。兩種模式都走同一順序，故結果一致；
    本地端如指定 offline，GCIS 這步會回 None，該筆改用備用名稱並標記。

    登記狀態只有 GCIS 這一步給得出來，且**呼叫過就一定帶回**——不能因為呼叫端
    當下只想要名稱就把它丟掉。丟掉的後果是已廢止的機構在具名大類（銀行、保險等）
    輸出「登記狀態空白＋信心 medium」，看起來像正常存續戶。
    """
    t = provider.tax(tax_id)
    if t and t.get("name"):
        return t["name"], "稅籍(財政部)", ""
    for getter, label in ((provider.gov, "機關名冊"),
                          (provider.school, "學校名冊"),
                          (provider.nonprofit, "非營利名冊")):
        nm = getter(tax_id)
        if nm:
            return nm, label, ""
    if tax_id in X.FROZEN_NAMES:
        nm, src = X.FROZEN_NAMES[tax_id]
        return nm, src, ""
    rows = provider.authority(tax_id)
    if rows:
        nm = (rows[0].get("customer_name") or "").strip()
        if nm:
            return nm, "金管會名冊", ""
    lst = provider.listed(tax_id)                     # 上市櫃名冊帶官方全銜，早於 GCIS
    if lst and lst.get("name"):
        return lst["name"], "上市櫃名冊", ""
    comp = provider.company(tax_id)
    if comp and comp.get("name"):
        return comp["name"], "GCIS商工登記", (comp.get("status") or "").strip()
    return fallback_name, "帳務名(待補官方全銜)", ""


def _pick_authority_row(rows):
    """兼營多列 → 取主分類單列（穩定排序，不受檔案列序影響）"""
    return sorted(rows, key=lambda r: (GROUP_PRIORITY.get(r.get("industry_group"),
                                                          GROUP_PRIORITY_DEFAULT),
                                       r.get("industry_detail", "")))[0]


def _match_name_rules(name):
    """L3-8：名稱關鍵字（僅稅籍查無時使用）。回 (大類, 子類, 說明, 命中詞) 或 None。"""
    for rid, group, sub, keywords in R.NAME_RULES:
        if any(name.endswith(s) for s in R.NAME_RULE_SUFFIX_EXCLUDE.get(rid, ())):
            continue                                  # 例：N3 的「處$」不收「○○辦事處」
        for kw in keywords:
            hit = re.search(kw, name) if kw.endswith("$") else (kw in name)
            if hit:
                shown = kw.rstrip("$").replace("(股份)?", "")
                return group, sub, "%s 名稱關鍵字「%s」" % (rid, shown), shown
    return None


def _match_school_extension(name, school_words):
    """A3(b) 延續詞白名單：名稱含校詞，且第一個校詞之後帶得出校務單位語意才算命中。

    回「校詞＋延續詞」的說明字串，或 None。

    校內單位的命名是公式化的（○○大學＋附設／校區／中心／總務處…），
    校門口的店家不是（大學書局、大學便當、○○大學店）。用延續字串問這個問題，
    比一條一條加店家業態黑名單收斂得多——後者補完一群就浮出下一群。
    """
    if any(b in name for b in R.SCHOOL_EXTENSION_BLOCK):
        return None                                   # 住宅社區管委會：校詞是建案名
    found = [(name.find(kw), kw) for kw in school_words if kw in name]
    if not found:
        return None
    i, kw = min(found)                                # 只看第一個校詞之後的部分
    tail = name[i + len(kw):]
    for w in R.SCHOOL_EXTENSION_WORDS:
        if w in tail:
            return "%s…%s" % (kw, w)
    return None


def _match_institutional(name):
    """L3-A：制度性非營業人名稱規則。回 (大類, 子類, 規則編號, 比對方式, 命中詞, 信心) 或 None。

    排在稅籍層之前是刻意的：醫院、學校的稅籍主碼常是校舍出租、附設餐飲之類的
    附屬營業碼，先問稅籍會把本業蓋掉。詳見 rules.py 的 INSTITUTIONAL_RULES 註解。
    """
    if not name:
        return None
    if any(k in name for k in R.CORP_PREFIX):
        return None                                   # 法人／公協會整層跳過，交給 L2-4 與 L3-6
    has_corp = any(s in name for s in R.CORP_SUFFIX_EXCLUDE)
    for rid, group, sub, mode, keywords, conf in R.INSTITUTIONAL_RULES:
        if has_corp and rid in R.CORP_SUFFIX_GUARD_RULES:
            continue                                  # 帶公司／店家型後綴＝營業人，落回稅籍層
        if any(k in name for k in R.INSTITUTIONAL_KEYWORD_EXCLUDE.get(rid, ())):
            continue                                  # 例：A1 的「醫院」不收動物醫院
        if mode == "contains_all":
            if all(kw in name for kw in keywords):
                return group, sub, rid, mode, "＋".join(keywords), conf
            continue
        if mode == "contains_ext":
            hit = _match_school_extension(name, keywords)
            if hit:
                return group, sub, rid, mode, hit, conf
            continue
        for kw in keywords:
            hit = name.endswith(kw) if mode == "endswith" else (kw in name)
            if hit:
                return group, sub, rid, mode, kw, conf
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


def classify_uid(raw_uid, name):
    """L0：統編解析。回 (Verdict, 統編備註)；(None, "") 代表是 8 碼統編，續走 L1。

    這一層存在的理由是「任何輸入都要有明確輸出」。首碼命中 UID 前綴表者**在此終結**，
    絕不往下走名冊、稅籍或名稱規則——F 碼的「○○銀行東京分行」若走到 L3-8 的 N12，
    會被判成本國銀行（v4 實際發生過的錯）。
    """
    prefix = raw_uid[:1].upper()                      # 首碼一律轉大寫比對；真統編全是數字
    if prefix in R.UID_PREFIX:
        group = R.UID_PREFIX[prefix]
        return Verdict(group, "", "L0 統編解析",
                       "L0-%s 統編首碼 %s → %s" % (prefix, prefix, group),
                       "統編首碼 %s：%s" % (prefix, group), "high"), ""
    if raw_uid.strip().upper() in R.UID_BLANK_TOKENS:
        hit = X.NO_TAXID_ACCOUNTS.get((name or "").strip())
        if hit:
            group, sub, why = hit
            return Verdict(group, sub, "L0 統編解析",
                           "L0-N 無統編歸戶表：%s" % why,
                           "無統編歸戶：%s" % why[:40], "high"), ""
        return Verdict(R.UNCLASSIFIED[0], R.UNCLASSIFIED[1], "L0 統編解析",
                       "L0-N 無統一編號，且無統編歸戶表查無此名稱",
                       "無統一編號且歸戶表查無", "low"), ""
    if raw_uid.isdigit() and len(raw_uid) == 8:
        return None, ""                               # 正常 8 碼統編：續走 L1
    return Verdict(R.UNCLASSIFIED[0], R.UNCLASSIFIED[1], "L0 統編解析",
                   "L0-X 統編格式異常（非 8 碼數字，且首碼不在 UID 前綴表）",
                   "統編格式異常", "low"), R.UID_FORMAT_NOTE


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
        sub = R.REGISTRY_SUB_NORMALIZE.get(sub, sub)   # 例：農會／漁會信用部 → 農漁會信用部
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

    # ── L3-A 制度性非營業人名稱規則（早於稅籍層）──────────────────────────
    inst = _match_institutional(name)
    if inst:
        group, sub, rid, mode, kw, conf = inst
        how = "名稱以「%s」結尾" % kw if mode == "endswith" else "名稱含「%s」" % kw
        return Verdict(group, sub, "L3-A 制度性名稱",
                       "L3-A/%s %s" % (rid, how), how, conf)

    # ── L3-B 稅籍碼表（特定碼優先於大類映射）────────────────────────────
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


def _industry_fallback(provider, tax_id):
    """稅籍查無時的行業軌 fallback（v5）：L2-5 上市櫃名冊。回 Verdict 或 None。

    回整個 Verdict 而不只是值，是因為**依據層與判定規則也必須跟著換**。
    只換值會讓輸出自相矛盾：命中 L2-5 的列信心是 high，判定規則卻還寫著
    L4 兜底的「各層皆未命中；…標 confidence=low」。

    （L3-D 登記狀態不在這裡——它不分身分軌群組，改由 query() 集中後置判定。）
    """
    lst = provider.listed(tax_id)
    if not lst:
        return None
    where = "%s %s %s" % (lst["market"], lst["ind_code"], lst["ind_name"])
    rule = "L2-5 上市櫃名冊（公司代號 %s，%s）" % (lst["code"], where)
    basis = "上市櫃名冊：%s" % where
    if not lst["section"]:
        # 官方沒給 A–S 歸屬（其他業／綠能環保／存託憑證）。上市櫃身分是確定的，
        # 產業歸屬不是——所以值標「其他」、信心降 medium，不假裝知道。
        return Verdict(R.UNMAPPED_SECTION, lst["ind_name"], "L2-5 上市櫃名冊",
                       rule + "；官方無 A–S 對應", basis, "medium")
    return Verdict(lst["section"], lst["ind_name"], "L2-5 上市櫃名冊", rule, basis, "high")


def is_dissolved(status):
    """登記狀態是否代表非存續戶（解散／廢止／撤銷／撤回）。"""
    return bool(status) and any(w in status for w in R.DISSOLVED_STATUS_WORDS)


# 這些名稱來源與 GCIS 狀態出自同一筆記錄（或根本沒有官方名稱可比對），
# 不可能發生「名稱是甲、狀態是乙」的錯配，故不套名稱一致性守門。
STATUS_TRUSTED_NAME_SOURCES = ("GCIS商工登記", "帳務名(待補官方全銜)", "查無官方名稱")


def names_match(a, b):
    """兩個機構名稱是否指同一個實體：去空白後相等，或一方包含另一方。

    容忍全銜與簡稱的落差（金管會名冊的全銜對得上 GCIS 的同一全銜），
    但擋得住完全不同的名字——那是統編重號，不是同一家。
    """
    ca = re.sub(r"[\s　]", "", a or "")
    cb = re.sub(r"[\s　]", "", b or "")
    if not ca or not cb:
        return False
    return ca == cb or ca in cb or cb in ca


def registration_status(provider, tax_id, known="", resolved_name="", name_source=""):
    """回 (登記狀態, 統編備註)。稅籍查無時補問一次 GCIS，並做名稱一致性守門。

    為什麼還要補問：resolve_name 只有在走到最後一步才會碰 GCIS，
    名稱若由機關、金管會或上市櫃名冊解出就提早回傳了——那些戶一樣可能已經解散。
    稅籍查得到就不問：稅籍檔只收營業中資料，還在裡面就代表還在營業。
    tax() 與 company() 兩邊都有快取，這裡不會產生重複查詢。

    為什麼要守門：**統編會重號**。機關的統編可能與某家已解散的歷史公司撞號，
    此時 GCIS 回的是那家公司的狀態，套到機關身上就會把現存機關標成已解散
    （實例：某分署的統編在 GCIS 是一家已解散的企業社）。所以名稱來源若是
    別的官方名冊，就要求 GCIS 記錄的名稱對得上解析出來的名稱才採信；
    對不上則不採用該狀態，並在統編備註如實寫明看到了什麼。

    L1-5 凍結表（`known`）是人工查證值，不受此守門限制。
    """
    if known:
        return known, ""
    if provider.tax(tax_id):                          # 稅籍還在＝存續中
        return "", ""
    if getattr(provider, "offline", False):
        return "", ""
    comp = provider.company(tax_id) or {}
    status = (comp.get("status") or "").strip()
    if not status or name_source in STATUS_TRUSTED_NAME_SOURCES:
        return status, ""
    gcis_name = (comp.get("name") or "").strip()
    if names_match(gcis_name, resolved_name):
        return status, ""
    return "", "GCIS 同號記錄名稱不符（%s／%s），未採用其狀態" % (
        gcis_name or "無名稱", status)


def query(raw_tax_id, provider, fallback_name="", as_of=""):
    """單筆查詢的公開入口：統編 → 完整判定結果 dict。

    產業大類／產業子類＝定版單軌（2026-08-11，v5 擴充）：身分軌命中者直接沿用；
    一般企業（名冊查無執照）改走行業軌，由稅籍主行業代號歸 A–S 行業別，稅籍查無時
    再試 L2-5 上市櫃名冊與 L3-D GCIS 登記狀態。
    大分類／子分類保留身分軌原值，供稽核與八欄版並排比對；對外輸出時
    欄名標註「僅供參考」（2026-08-13，見 DISPLAY_RENAME／display()）。
    """
    t0 = normalize_tax_id(raw_tax_id)
    v, fix_note = classify_uid(t0, fallback_name)     # L0：非 8 碼統編在此終結
    if v is not None:
        # L0 終結者不查任何名冊／稅籍／GCIS：名稱只能用呼叫端帶進來的帳務名。
        tax_id, status = t0, ""
        name = fallback_name
        name_source = "帳務名(待補官方全銜)" if name else "查無官方名稱"
        ind_group, ind_sub = v.group, v.sub
        layer, rule, basis, confidence = v.layer, v.rule, v.basis, v.confidence
    else:
        tax_id, fix_note = ((X.TAX_ID_FIX[t0][0], X.TAX_ID_FIX[t0][1])
                            if t0 in X.TAX_ID_FIX else (t0, ""))
        name, name_source, gcis_status = resolve_name(tax_id, provider, fallback_name)
        v = classify(tax_id, name, provider)
        layer, rule, basis, confidence = v.layer, v.rule, v.basis, v.confidence
        # 凍結表優先於 GCIS 即時值（人工查證過）；GCIS 狀態不分大類一律如實帶出，
        # 具名大類（銀行、保險等）也要——已廢止的機構不能看起來像正常存續戶。
        frozen = X.FROZEN_STATUS.get(tax_id, "")
        status, status_note = registration_status(
            provider, tax_id, frozen or gcis_status, name, name_source)
        if status_note:                               # 統編重號：如實記，不靜默丟掉
            fix_note = "；".join(x for x in (fix_note, status_note) if x)
        if v.layer == "L3-A 制度性名稱" and v.sub == "醫療":
            ind_group, ind_sub = R.MEDICAL_SECTION    # 醫院／診所直接對到行業軌 Q／86
        elif v.group == "一般企業":                   # 定版單軌：一般企業走行業軌
            ind_group, ind_sub = industry_track(provider, tax_id)
            if ind_group == R.TAX_MISSING_SECTION:
                fb = _industry_fallback(provider, tax_id)
                if fb:                                # 值、依據層、判定規則要一起換
                    ind_group, ind_sub = fb.group, fb.sub
                    layer, rule = fb.layer, fb.rule
                    basis, confidence = fb.basis, fb.confidence
                elif not status and getattr(provider, "offline", False):
                    fix_note = "；".join(x for x in (fix_note, "離線：未查 GCIS 登記狀態") if x)
        else:
            ind_group, ind_sub = v.group, v.sub
        # ── L3-D 非存續戶（使用者 2026-08-21 裁示：不分身分軌群組）─────────
        # 已廢止的銀行與已解散的公司，在「這家還在不在」這個問題上是同一件事，
        # 所以單軌一律標歷史戶。身分軌（大分類／子分類）保留原群組值供稽核——
        # 那回答的是「它是什麼」，與「它還在不在」是兩個問題。
        if is_dissolved(status):
            ind_group, ind_sub = R.DISSOLVED_SECTION, ""
            layer = "L3-D GCIS 登記狀態"
            rule = "L3-D 登記狀態「%s」（%s，非存續戶）" % (
                status, "L1-5 凍結表" if frozen else "GCIS 商工登記")
            basis = "GCIS 登記狀態：%s" % status
        if status and status not in ("核准設立", "核准登記"):
            confidence = "low"
        if not name:                                 # 名稱完全查不到時誠實標記
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
        "分類依據詞": basis,
        "分類依據層": layer,
        "判定規則": rule,
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
