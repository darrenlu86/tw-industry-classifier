# -*- coding: utf-8 -*-
r"""v5 改版驗收：L0 統編解析 ＋ L3-A 制度性名稱 ＋ 單軌 fallback ＋ 新值域

v5 的核心宣稱是「任何輸入都有明確輸出與可稽核依據」。這句話要能被驗證，
所以本檔的重點不是既有規則的迴歸（那由 test_v4_golden.py 顧），而是三件新事：

  1. L0 首碼命中即終結——**絕不**再往下走名稱關鍵字層
     （v4 實際發生過：F 碼的「○○銀行東京分行」被 L3-8 的 N12「銀行」判成本國銀行）
  2. L3-A 排在稅籍層之前，且公司型後綴防呆有效
     （長庚大學稅籍主碼 68 不動產，v4 判成不動產業；大學光學科技必須維持批發零售）
  3. 單軌 fallback（L2-5 上市櫃名冊、L3-D GCIS 登記狀態）在稅籍查無時才作用

規則層與值域測試用**合成 provider 與合成名冊**，完全不依賴 data/ 下的真檔——
規則的正確性不該取決於某台機器上有沒有下載過 63 MB 的稅籍檔。
只有標了 HAVE_LOCAL_DATA 的個案測試會用真資料，缺檔時跳過。

測試實體一律是公開個體（上市公司、公立私立學校），F／P 案例用合成值。

用法：
    py -3.12 -m pytest tests/test_v5_golden.py -q
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "core"))

import engine                                          # noqa: E402
import rules as R                                      # noqa: E402
import exceptions as X                                 # noqa: E402
from provider_base import ProviderBase                 # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DATA = os.path.join(ROOT, "data")
HAVE_LOCAL_DATA = (os.path.exists(os.path.join(DATA, "BGMOPEN1.zip"))
                   and os.path.exists(os.path.join(DATA, "authority_master.csv")))


class FakeProvider(ProviderBase):
    """合成 provider：所有名冊預設空的，測哪一層就只餵那一層的資料。

    刻意不繼承 Local／ApiProvider——那兩個會碰檔案與網路，規則測試不該有那種相依。
    """

    mode_name = "測試合成"
    data_version = "fixture"

    def __init__(self, tax=None, listed=None, company=None, offline=False, **registries):
        super().__init__(data_dir=os.path.join(HERE, "_fixtures"))
        self._tax = tax or {}
        self._listed = listed or {}
        self._company = company or {}
        self.offline = offline
        for key, value in registries.items():          # gov／school／nonprofit／authority
            setattr(self, "_" + key, value)

    def tax(self, tax_id):
        return self._tax.get(tax_id)

    def company(self, tax_id):
        # 與 LocalProvider 同語意：offline 且無快取時不查 GCIS。
        # （真的 provider 有快取時 offline 仍會回值，本 fake 不模擬快取。）
        return None if self.offline else self._company.get(tax_id)


def verdict_of(name, tax_id="12345678", **kwargs):
    """跑一次完整 query，回 (產業大類, 產業子類, 分類依據層) 三元組。"""
    rec = engine.query(tax_id, FakeProvider(**kwargs), fallback_name=name)
    return rec["產業大類"], rec["產業子類"], rec["分類依據層"]


# ══════════════════════════════════════════════════════════════════════════
# 值域
# ══════════════════════════════════════════════════════════════════════════
def test_v5_groups_domain():
    for g in ("執行業務者（非營業人）", "境外法人（無台灣登記）", "個人戶", "無法分類"):
        assert g in R.GROUPS, "v5 應有「%s」大類" % g
        assert g in R.SUBGROUPS, "%s 缺子分類值域" % g
    assert "陸銀在臺分行" in R.SUBGROUPS["金控與銀行"]
    # 三類允許子分類空白：空字串必須是合法值域，否則值域自我檢查會誤報越界
    for g in ("境外法人（無台灣登記）", "個人戶", "無法分類"):
        assert "" in R.SUBGROUPS[g], "%s 應允許子分類空白" % g
    assert R.SUBGROUPS["執行業務者（非營業人）"][0] == "會計師事務所"


def test_registry_sub_normalize():
    """農會／漁會信用部併回農漁會信用部，且併過去的值本身在值域內。"""
    assert R.REGISTRY_SUB_NORMALIZE["農會信用部"] == "農漁會信用部"
    assert R.REGISTRY_SUB_NORMALIZE["漁會信用部"] == "農漁會信用部"
    for target in set(R.REGISTRY_SUB_NORMALIZE.values()):
        assert target in R.SUBGROUPS["金控與銀行"]
    p = FakeProvider(authority={"11111111": [{"industry_group": "金控與銀行",
                                              "industry_detail": "農會信用部",
                                              "source": "金管會名冊"}]})
    rec = engine.query("11111111", p)
    assert rec["子分類"] == "農漁會信用部", "名冊值未正規化，實得 %s" % rec["子分類"]


def test_medical_section_derived():
    """L3-A 醫療的單軌值必須由既有行業軌表組出，不是另寫的字串。"""
    assert R.MEDICAL_SECTION == ("Q 醫療保健及社會工作服務業", "86 醫療保健服務")


# ══════════════════════════════════════════════════════════════════════════
# L0 統編解析
# ══════════════════════════════════════════════════════════════════════════
def test_l0_prefix_terminates():
    """F／P 首碼命中即終結，且大小寫皆可。"""
    assert verdict_of("", tax_id="F000001") == ("境外法人（無台灣登記）", "", "L0 統編解析")
    assert verdict_of("", tax_id="P000001") == ("個人戶", "", "L0 統編解析")
    assert verdict_of("", tax_id="f000002")[0] == "境外法人（無台灣登記）"


def test_l0_f_code_never_reaches_name_rules():
    """v4 迴歸：F 碼帶「銀行」的名稱不得再被 L3-8 的 N12 判成本國銀行。"""
    got = verdict_of("測試國際銀行東京分行", tax_id="F000138")
    assert got == ("境外法人（無台灣登記）", "", "L0 統編解析"), \
        "F 碼進了名稱關鍵字層，實得 %s／%s（層 %s）" % got


def test_l0_special_foreign_mission_code():
    """AA＋3 碼＝外國駐台機構的特種統編（財政部配賦），不分大小寫，命中即終結。"""
    for uid in ("AA104", "aa104", "AA001", "AA136"):
        rec = engine.query(uid, FakeProvider(), fallback_name="某駐台辦事處")
        assert (rec["大分類"], rec["子分類"]) == ("境外法人（無台灣登記）", "外國駐台機構"), \
            "%s → %s／%s" % (uid, rec["大分類"], rec["子分類"])
        assert rec["分類依據層"] == "L0 統編解析"
        assert rec["信心"] == "high"
        assert rec["子分類"] in R.SUBGROUPS[rec["大分類"]], "值域自我檢查要通過"
    # 格式不符者不得誤中
    for uid in ("A1234567", "AA12", "AA1234", "AAB01", "22099131"):
        rec = engine.query(uid, FakeProvider(offline=True))
        assert rec["子分類"] != "外國駐台機構", "%s 不該判成外國駐台機構" % uid


def test_l0_blank_without_table():
    """統編空白且歸戶表查無 → 無法分類（不是靜默丟掉，也不是兜底一般企業）。"""
    for blank in ("", "  ", "N/A", "-"):
        got = verdict_of("某某未登記單位", tax_id=blank)
        assert got == ("無法分類", "", "L0 統編解析"), "%r → %s" % (blank, got)


def test_l0_blank_with_account_table(tmp_path):
    """無統編歸戶表命中時依表值；載入接口在 core/exceptions.py。"""
    import json                                        # noqa: PLC0415
    p = tmp_path / "local_exceptions.json"
    p.write_text(json.dumps({"no_taxid_accounts": {
        "某某縣政府教育處": ["政府機關", "政府機關", "無統編分支單位，2026-08-21 裁決"]}},
        ensure_ascii=False), encoding="utf-8")
    try:
        X.load(str(p))
        assert len(X.NO_TAXID_ACCOUNTS) == 1
        got = verdict_of("某某縣政府教育處", tax_id="")
        assert got == ("政府機關", "政府機關", "L0 統編解析"), got
        # 表裡沒有的名稱仍歸無法分類
        assert verdict_of("不在表裡的單位", tax_id="")[0] == "無法分類"
    finally:
        X.load()                                       # 還原成專案本地例外檔


def test_l0_format_error():
    """非 8 碼數字、非已知前綴 → 無法分類，且統編備註如實標記。"""
    rec = engine.query("2209913199", FakeProvider(), fallback_name="格式壞掉的一筆")
    assert rec["產業大類"] == "無法分類"
    assert rec["統編備註"] == R.UID_FORMAT_NOTE
    assert rec["判定規則"].startswith("L0-X")


# ── L1-1 統編歸戶＝查詢重導向（使用者 2026-08-21 語意變更）────────────────
def _with_tax_id_fix(alias, target, why):
    saved = dict(X.TAX_ID_FIX)
    X.TAX_ID_FIX[alias] = (target, why)
    return saved


def test_tax_id_fix_redirects_lookup_but_keeps_key():
    """別名 A→B：查詢用 B，輸出的統一編號保留 A，備註寫明歸戶。

    改寫輸出鍵會讓分類結果 join 不回原始報表（原鍵直接消失），
    同實體雙統編時戶鍵由呼叫端決定，本工具不替它決定。
    """
    tax = {"87654321": {"name": "測試電子股份有限公司", "org": "",
                        "codes": [("271000", "電腦電子產品製造")]}}
    saved = _with_tax_id_fix("11112222", "87654321",
                             "舊統編，帳務端沿用開單；現行為 87654321；2026-08-21 核定")
    try:
        rec = engine.query("11112222", FakeProvider(tax=tax), fallback_name="測試電子")
        assert rec["統一編號"] == "11112222", "輸出鍵必須是輸入原值，實得 %s" % rec["統一編號"]
        assert rec["官方正式名稱"] == "測試電子股份有限公司", "名稱應取自歸戶後的統編"
        assert rec["產業大類"] == "C 製造業", "分類應取自歸戶後的統編"
        assert rec["統編備註"].startswith("統編歸戶：查詢採 87654321（"), rec["統編備註"]
        assert "舊統編，帳務端沿用開單" in rec["統編備註"], "備註要帶理由第一段"
        assert "；" not in rec["統編備註"].split("（", 1)[1][:-1], "理由只取第一段"
    finally:
        X.TAX_ID_FIX.clear()
        X.TAX_ID_FIX.update(saved)


def test_tax_id_fix_both_keys_survive_batch():
    """批次同時含別名 A 與目標 B：兩列都在、鍵不重複也不消失。"""
    tax = {"87654321": {"name": "測試電子股份有限公司", "org": "",
                        "codes": [("271000", "電腦電子產品製造")]}}
    saved = _with_tax_id_fix("11112222", "87654321", "舊統編；2026-08-21 核定")
    try:
        p = FakeProvider(tax=tax)
        rows = [engine.query(t, p) for t in ("11112222", "87654321")]
        keys = [r["統一編號"] for r in rows]
        assert keys == ["11112222", "87654321"], "兩個鍵都要在且不互相取代，實得 %s" % keys
        assert len(set(keys)) == 2, "鍵不得重複"
        # 兩列的分類結果相同（同一實體），但只有別名列帶歸戶備註
        assert rows[0]["產業大類"] == rows[1]["產業大類"] == "C 製造業"
        assert rows[0]["統編備註"].startswith("統編歸戶：")
        assert rows[1]["統編備註"] == ""
    finally:
        X.TAX_ID_FIX.clear()
        X.TAX_ID_FIX.update(saved)


def test_lookup_tax_id_helper():
    """歸戶查表本身：命中回修正值，未命中回原值。引擎與 emit 共用同一份邏輯。"""
    saved = _with_tax_id_fix("11112222", "87654321", "測試")
    try:
        assert engine.lookup_tax_id("11112222") == "87654321"
        assert engine.lookup_tax_id("87654321") == "87654321"
        assert engine.lookup_tax_id("22099131") == "22099131"
    finally:
        X.TAX_ID_FIX.clear()
        X.TAX_ID_FIX.update(saved)


def test_l0_zero_pad_still_works():
    """normalize_tax_id 的補零行為不變：不足 8 碼的純數字補完後續走 L1，不進 L0-X。"""
    rec = engine.query("999340", FakeProvider())
    assert rec["統一編號"] == "00999340"
    assert not rec["判定規則"].startswith("L0"), "補零後應續走 L1，實得 %s" % rec["判定規則"]


# ══════════════════════════════════════════════════════════════════════════
# L3-A 制度性名稱
# ══════════════════════════════════════════════════════════════════════════
# (名稱, 期望大類, 期望子類, 說明)。稅籍碼一律餵 68（不動產經營）——
# 這正是長庚大學在稅籍檔的實際主碼，L3-A 若沒排在稅籍前，全部會判成不動產業。
INSTITUTIONAL_CASES = [
    ("國立測試大學", "教育與法人", "學校", "A3a 詞尾錨定"),
    ("測試科技大學附設醫院", "一般企業", "醫療", "A1 早於 A3：附設醫院歸醫療"),
    ("測試市立聯合醫院", "一般企業", "醫療", "A1 醫院"),
    ("測試家庭醫學科診所", "一般企業", "醫療", "A2 診所詞尾"),
    ("測試醫事檢驗所", "一般企業", "醫療", "A2 醫事檢驗所"),
    ("測試大學進修推廣學院", "教育與法人", "學校", "A3a 學院詞尾"),
    ("測試大學校友會館", "教育與法人", "學校", "A3b 含大學、無公司後綴"),
    ("測試聯合會計師事務所", "執行業務者（非營業人）", "會計師事務所", "A4"),
    ("測試法律事務所", "執行業務者（非營業人）", "律師事務所", "A4"),
    ("測試建築師事務所", "執行業務者（非營業人）", "建築師與技師事務所", "A4"),
    ("測試記帳士聯合事務所", "執行業務者（非營業人）", "記帳士與報稅代理", "A4 記帳士優先於事務所尾"),
    ("測試國際專利商標聯合事務所", "執行業務者（非營業人）", "其他執行業務者", "A4 專利＋事務所"),
    # 泛用「事務所」尾綴已於 2026-08-21 修正輪移到稅籍之後，
    # 改由 test_generic_office_suffix_moved_after_tax 驗
]


def test_l3a_cases():
    tax = {"12345678": {"name": "", "org": "",
                        "codes": [("681100", "不動產經營")]}}
    for name, want_group, want_sub, why in INSTITUTIONAL_CASES:
        rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name=name)
        got = (rec["大分類"], rec["子分類"])
        assert got == (want_group, want_sub), \
            "%s（%s）：期望 %s／%s，實得 %s／%s" % (name, why, want_group, want_sub, *got)
        assert rec["分類依據層"] == "L3-A 制度性名稱", \
            "%s 應由 L3-A 判定，實得 %s" % (name, rec["分類依據層"])


def test_l3a_medical_maps_to_section_q():
    """醫療命中時單軌走 Q／86，不受稅籍主碼（此處 68 不動產）影響。"""
    tax = {"12345678": {"name": "", "org": "", "codes": [("681100", "不動產經營")]}}
    rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name="測試市立聯合醫院")
    assert (rec["產業大類"], rec["產業子類"]) == R.MEDICAL_SECTION, \
        "實得 %s／%s" % (rec["產業大類"], rec["產業子類"])


def test_l3a_corp_suffix_guard():
    """公司／店家型後綴防呆：帶這些後綴者不套 A1–A3，改走稅籍。

    後四個是 2026-08-21 稅籍全檔實掃撈出來的真實命名（已改成合成名稱）。
    """
    tax = {"12345678": {"name": "", "org": "", "codes": [("474100", "零售業")]}}
    for name in ("大學光學科技股份有限公司", "測試醫院管理顧問有限公司",
                 "測試學院文創工作室", "測試尚一品餐館海洋大學",
                 "大學雙剖檳榔測試店", "測試大學小吃部", "測試學院餐廳"):
        rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name=name)
        assert rec["分類依據層"] == "L3 稅籍碼表", \
            "%s 不應被 L3-A 收走，實得 %s" % (name, rec["分類依據層"])
        assert rec["產業大類"] == "G 批發及零售業"


# ── R1 修正輪（2026-08-21）：三群實測誤收必須歸零 ────────────────────────
def test_l3a_excludes_veterinary():
    """動物醫院／家畜診所屬稅籍 75 獸醫服務，不是人的醫療（實掃 291 筆最大誤收群）。"""
    tax = {"12345678": {"name": "", "org": "", "codes": [("750000", "獸醫服務")]}}
    for name in ("測試動物醫院", "測試家畜診所", "測試寵物醫院", "測試獸醫院"):
        rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name=name)
        assert rec["子分類"] != "醫療", "%s 不應判成醫療" % name
        assert rec["分類依據層"] == "L3 稅籍碼表", \
            "%s 應落回稅籍層，實得 %s" % (name, rec["分類依據層"])
        assert rec["產業大類"] == "M 專業、科學及技術服務業"


def test_l3a_skips_juridical_persons():
    """名稱含法人／公協會字樣者整層跳過 L3-A，交給 L2-4 與 L3-6（實掃 35 筆）。"""
    # 稅籍查無：走 L3-6 法人名稱前綴需要稅籍碼，故此處用稅籍 85 教育服務業
    tax = {"12345678": {"name": "", "org": "", "codes": [("850000", "教育服務業")]}}
    for name in ("測試市記帳士公會", "測試市地政士公會", "社團法人測試報稅代理人協會"):
        rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name=name)
        assert rec["大分類"] == "教育與法人", \
            "%s 應歸教育與法人，實得 %s／%s" % (name, rec["大分類"], rec["子分類"])
        assert not rec["分類依據層"].startswith("L3-A"), \
            "%s 不應由 L3-A 判定" % name
    # 醫療財團法人也一併跳過 L3-A，由稅籍 86 的 L3-1 接手（結果仍是醫療）
    tax86 = {"12345678": {"name": "", "org": "", "codes": [("861000", "醫療保健服務")]}}
    rec = engine.query("12345678", FakeProvider(tax=tax86), fallback_name="醫療財團法人測試紀念醫院")
    assert (rec["大分類"], rec["子分類"]) == ("一般企業", "醫療")
    assert rec["判定規則"].startswith("L3-1")


def test_l3a_excludes_campus_district_placename():
    """G2：「大學城／大學口」是校門口商圈的地名，整條街的店都會踩到 A3 的「大學」。"""
    tax = {"12345678": {"name": "", "org": "", "codes": [("474100", "零售業")]}}
    for name in ("大學城書局", "大學城體育用品社", "大學口湯圓", "大學城撞球場",
                 "小富翁大學城社區管理委員會"):
        rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name=name)
        assert rec["子分類"] != "學校", "%s 不應判成學校" % name
        assert rec["分類依據層"] == "L3 稅籍碼表", \
            "%s 應落回稅籍層，實得 %s" % (name, rec["分類依據層"])


def test_l3a_excludes_canine_feline_clinic():
    """G3：獸醫院用「犬貓」不用「動物」，第一輪的排除詞攔不到。"""
    tax = {"12345678": {"name": "", "org": "", "codes": [("750000", "獸醫服務")]}}
    for name in ("測試犬貓專科醫院", "測試犬貓急診醫院", "測試犬貓診所"):
        rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name=name)
        assert rec["子分類"] != "醫療", "%s 不應判成人的醫療" % name
        assert rec["產業大類"] == "M 專業、科學及技術服務業"


def test_l3a_excludes_shops_inside_campus_and_hospital():
    """G5：醫院裡的咖啡屋、學校裡的洗衣部是店，不是醫院或學校本體。"""
    tax = {"12345678": {"name": "", "org": "", "codes": [("960000", "其他個人服務")]}}
    for name in ("測試市立聯合醫院有何不可咖啡屋", "測試醫院理髮部", "測試大學洗衣部",
                 "測試大學員工福利社理髮部", "衣學院服裝坊", "測試學成造型名店",
                 "鮮烘豆咖啡學院", "測試咖啡甜食小賣所國軍醫院店"):
        rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name=name)
        assert rec["大分類"] == "一般企業", \
            "%s 應落回稅籍層，實得 %s／%s" % (name, rec["大分類"], rec["子分類"])
        assert rec["產業大類"] == "S 其他服務業"
    # 對照組：不帶店家業態詞的校內單位仍維持學校（G1 附設場館，指揮官裁示維持）
    rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name="測試大學圖書館")
    assert rec["子分類"] == "學校"


# ── A3(b) 延續詞白名單（2026-08-21 第三輪，取代逐一列舉店家業態的打地鼠做法）──
def test_a3b_extension_whitelist_hit():
    """白名單命中：校詞之後帶得出校務單位語意 → 判學校。"""
    tax = {"12345678": {"name": "", "org": "", "codes": [("474100", "零售業")]}}
    for name in ("測試大學附設山地實驗農場", "測試科技大學楠梓校區", "測試大學綜合體育館",
                 "測試大學汽車臨時停車場", "測試大學員生消費合作社", "測試大學總務處",
                 "測試大學場地管理委員會", "測試大學圖書館", "測試大學創新育成中心"):
        rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name=name)
        assert (rec["大分類"], rec["子分類"]) == ("教育與法人", "學校"), \
            "%s 應判學校，實得 %s／%s" % (name, rec["大分類"], rec["子分類"])
        assert rec["判定規則"].startswith("L3-A/A3b"), rec["判定規則"]


def test_a3b_extension_whitelist_miss():
    """白名單落空：校詞之後只是業態詞 → 跳過 A3b，落回稅籍層。

    這一群就是逐一加黑名單補不完的校門口商圈（實掃 16 筆）。
    """
    tax = {"12345678": {"name": "", "org": "", "codes": [("474100", "零售業")]}}
    for name in ("大學書局", "大學眼鏡行", "大學水電行", "大學影印社", "大學便當",
                 "大學鐘錶刻印行", "測試ＰＩＺＺＡ亞洲大學店", "測試大學後門水果店",
                 "測試大學紀念品專戶"):
        rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name=name)
        assert rec["子分類"] != "學校", "%s 不應判成學校" % name
        assert rec["分類依據層"] == "L3 稅籍碼表", \
            "%s 應落回稅籍層，實得 %s" % (name, rec["分類依據層"])


def test_a3b_extension_blocklist():
    """住宅社區管委會：延續字串雖含「管理委員會」，全名命中黑名單即否決。

    「管理委員會」本身不能當排除詞——實掃顯示它會一次誤殺 12 筆校內單位
    （○○大學場地／車輛／大樓管理委員會），所以只能從住宅端的字樣下手。
    """
    tax = {"12345678": {"name": "", "org": "", "codes": [("681100", "不動產經營")]}}
    for name in ("測試大學銀行社區管理委員會", "測試大學公寓大廈管理委員會",
                 "台大學人福邸管理委員會", "測試大學劍橋社區管理委員會"):
        rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name=name)
        assert rec["子分類"] != "學校", "%s 不應判成學校" % name
        assert rec["產業大類"] == "L 不動產業"
    # 對照組：校內的管理委員會不受黑名單影響
    rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name="測試大學場地管理委員會")
    assert rec["子分類"] == "學校"


def test_l3a_excludes_livelihood_academy():
    """A3(a) 學院尾綴的生活業態黑名單（養生／甲睫／芳香／歌唱，各一筆實證）。"""
    tax = {"12345678": {"name": "", "org": "", "codes": [("960000", "其他個人服務")]}}
    for name in ("辟穀回春養生學院", "測試眉甲睫學院", "測試芳香學院", "測試卡拉ＯＫ歌唱學院"):
        rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name=name)
        assert rec["子分類"] != "學校", "%s 不應判成學校" % name
        assert rec["產業大類"] == "S 其他服務業"


def test_l3a_excludes_aquatic_hospital():
    """A1／A2 動物診療排除詞補「水產」：「○○水產專科醫院」不是人的醫療。"""
    tax = {"12345678": {"name": "", "org": "", "codes": [("750000", "獸醫服務")]}}
    rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name="測試水產專科醫院")
    assert rec["子分類"] != "醫療"
    assert rec["產業大類"] == "M 專業、科學及技術服務業"


def test_l3a_excludes_hospital_retail_counter():
    """院內商店靠「醫院」踩到 A1；「販賣部」是業態詞，落回稅籍層（實掃 2 筆）。"""
    tax = {"12345678": {"name": "", "org": "", "codes": [("474100", "零售業")]}}
    for name in ("測試醫院販賣部", "測試大學醫學院附設醫院測試分院販賣部"):
        rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name=name)
        assert rec["子分類"] != "醫療", "%s 不應判成醫療" % name
        assert rec["產業大類"] == "G 批發及零售業", \
            "%s 應落回稅籍層，實得 %s" % (name, rec["產業大類"])


def test_l3a_excludes_beauty_academy():
    """「○○美學院」是美容補習業，不是學校（實掃 19 筆）。"""
    tax = {"12345678": {"name": "", "org": "", "codes": [("960000", "其他個人服務")]}}
    for name in ("測試時尚美學院", "測試髮學院"):
        rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name=name)
        assert rec["子分類"] != "學校", "%s 不應判成學校" % name
        assert rec["產業大類"] == "S 其他服務業"


def test_generic_office_suffix_moved_after_tax():
    """泛用「事務所」尾綴移到稅籍之後：有稅籍的店家不得被標成非營業人。"""
    tax = {"12345678": {"name": "", "org": "", "codes": [("960000", "其他個人服務")]}}
    for name in ("裱框事務所", "測試婚禮事務所", "測試股份有限公司台北事務所"):
        rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name=name)
        assert rec["大分類"] == "一般企業", \
            "%s 有稅籍，不應歸執行業務者，實得 %s" % (name, rec["大分類"])
        assert rec["分類依據層"] == "L3 稅籍碼表"
    # 稅籍查無時才由 N15 接手
    rec = engine.query("12345678", FakeProvider(), fallback_name="測試聯合事務所")
    assert (rec["大分類"], rec["子分類"]) == ("執行業務者（非營業人）", "其他執行業務者")
    assert rec["判定規則"].startswith("L3-8 N15"), rec["判定規則"]


def test_professional_office_stays_before_tax():
    """迴歸：專業別事務所仍在稅籍之前——副業稅籍碼不得蓋掉本業。"""
    tax = {"12345678": {"name": "", "org": "", "codes": [("681100", "不動產經營")]}}
    for name, want_sub in (("測試聯合會計師事務所", "會計師事務所"),
                           ("測試法律事務所", "律師事務所"),
                           ("測試建築師事務所", "建築師與技師事務所"),
                           ("測試國際專利商標聯合事務所", "其他執行業務者")):
        rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name=name)
        assert (rec["大分類"], rec["子分類"]) == ("執行業務者（非營業人）", want_sub), \
            "%s：實得 %s／%s" % (name, rec["大分類"], rec["子分類"])
        assert rec["分類依據層"] == "L3-A 制度性名稱"
    # 稅籍查無的會計師事務所（規格原案）也不變
    rec = engine.query("12345678", FakeProvider(), fallback_name="測試聯合會計師事務所")
    assert (rec["大分類"], rec["子分類"]) == ("執行業務者（非營業人）", "會計師事務所")


def test_l3a_before_tax_after_registry():
    """順序：L2 名冊仍優先於 L3-A；L3-A 優先於 L3-B 稅籍。"""
    tax = {"12345678": {"name": "", "org": "", "codes": [("681100", "不動產經營")]}}
    # 名冊命中 → 由 L2 決定，L3-A 不介入
    p = FakeProvider(tax=tax, school={"12345678": "測試大學"})
    assert engine.query("12345678", p, fallback_name="測試大學")["分類依據層"] == "L2 權威名冊"
    # 名冊查無 → L3-A 介入，不落到稅籍的不動產
    p = FakeProvider(tax=tax)
    assert engine.query("12345678", p, fallback_name="測試大學")["產業大類"] == "教育與法人"


# ══════════════════════════════════════════════════════════════════════════
# L3-C：N3 辦事處排除 ＋ A4′
# ══════════════════════════════════════════════════════════════════════════
def test_n3_excludes_representative_office():
    """名稱以「辦事處」結尾者不判政府機關（稅籍查無母體）。"""
    assert verdict_of("測試貿易促進會台北辦事處")[0] != "政府機關"
    # 其他「處」尾字不受影響
    assert verdict_of("測試縣稅捐稽徵處")[0] == "政府機關"


def test_a4_prime_low_confidence_accountant():
    """A4′：名稱以「會計」／「會計師」結尾 → 執行業務者，且只在稅籍查無時作用。"""
    rec = engine.query("12345678", FakeProvider(), fallback_name="測試聯合會計")
    assert (rec["大分類"], rec["子分類"]) == ("執行業務者（非營業人）", "會計師事務所")
    assert rec["分類依據層"] == "L3 名稱關鍵字"
    # 有稅籍的「○○會計顧問股份有限公司」必須先被 L3-B 收走
    tax = {"12345678": {"name": "", "org": "", "codes": [("702000", "管理顧問")]}}
    rec = engine.query("12345678", FakeProvider(tax=tax), fallback_name="測試會計顧問股份有限公司")
    assert rec["大分類"] == "一般企業", "有稅籍者不應落 A4′，實得 %s" % rec["大分類"]


# ══════════════════════════════════════════════════════════════════════════
# 單軌 fallback：L2-5 上市櫃名冊 ／ L3-D GCIS 登記狀態
# ══════════════════════════════════════════════════════════════════════════
LISTED_FIXTURE = {"87654321": {"market": "TWSE", "code": "9999", "name": "測試控股股份有限公司",
                               "ind_code": "24", "ind_name": "半導體業",
                               "section": "C 製造業"}}
# 官方對照表沒有 A–S 歸屬的產業別代碼（其他業／綠能環保／存託憑證）：section 留空
LISTED_UNMAPPED = {"87654321": {"market": "TWSE", "code": "5871", "name": "測試控股股份有限公司",
                                "ind_code": "20", "ind_name": "其他業", "section": ""}}


def test_l2_5_listed_fallback():
    """稅籍查無但在上市櫃名冊 → 依名冊給行業軌值，依據詞帶市場別與代碼。

    依據層與判定規則必須一起換成 L2-5：只換值的話，信心 high 的列會配上
    L4 兜底那句「各層皆未命中…標 confidence=low」，輸出自相矛盾。
    """
    rec = engine.query("87654321", FakeProvider(listed=LISTED_FIXTURE, offline=True),
                       fallback_name="測試控股股份有限公司")
    assert rec["產業大類"] == "C 製造業"
    assert rec["產業子類"] == "半導體業"
    assert rec["分類依據詞"] == "上市櫃名冊：TWSE 24 半導體業"
    assert rec["信心"] == "high"
    assert rec["分類依據層"] == "L2-5 上市櫃名冊", rec["分類依據層"]
    assert rec["判定規則"].startswith("L2-5 上市櫃名冊（公司代號 9999"), rec["判定規則"]
    assert "各層皆未命中" not in rec["判定規則"]


def test_l2_5_supplies_official_name():
    """上市櫃名冊帶官方全銜，排在 GCIS 之前——不該讓這些戶停在「帳務名」。"""
    rec = engine.query("87654321", FakeProvider(listed=LISTED_FIXTURE, offline=True),
                       fallback_name="帳務系統簡稱")
    assert rec["官方正式名稱"] == "測試控股股份有限公司"
    assert rec["名稱來源"] == "上市櫃名冊"


def test_l2_5_unmapped_industry_code():
    """名冊命中但官方無 A–S 對應 → 產業大類「其他」＋名冊產業別原值，信心 medium。

    上市櫃身分是官方證實的，產業歸屬不是——不硬塞一個猜出來的大類。
    """
    rec = engine.query("87654321", FakeProvider(listed=LISTED_UNMAPPED, offline=True),
                       fallback_name="測試控股股份有限公司")
    assert rec["產業大類"] == R.UNMAPPED_SECTION, rec["產業大類"]
    assert rec["產業子類"] == "其他業"
    assert rec["分類依據層"] == "L2-5 上市櫃名冊"
    assert rec["分類依據詞"] == "上市櫃名冊：TWSE 20 其他業"
    assert rec["信心"] == "medium", "官方未給產業歸屬，不應標 high"


def test_dissolved_beats_unmapped_listed():
    """優先序定版：解散判定壓過 L2-5——已解散的前上市公司標歷史戶，不是「其他」。"""
    comp = {"87654321": {"name": "測試控股股份有限公司", "status": "解散"}}
    rec = engine.query("87654321", FakeProvider(listed=LISTED_UNMAPPED, company=comp))
    assert rec["產業大類"] == R.DISSOLVED_SECTION, rec["產業大類"]
    assert rec["產業子類"] == ""
    assert rec["分類依據層"] == "L3-D GCIS 登記狀態"


def test_single_track_sections_declared():
    """三個單軌專屬值集中宣告，供 emit_rule_table 與文件引用，不各寫一份。"""
    assert R.SINGLE_TRACK_SECTIONS == (R.TAX_MISSING_SECTION, R.UNMAPPED_SECTION,
                                       R.DISSOLVED_SECTION)
    for s in R.SINGLE_TRACK_SECTIONS:
        assert s not in R.GROUPS, "%s 是單軌專屬值，不該混進身分軌值域" % s


def test_l2_5_does_not_override_tax():
    """稅籍查得到時 L2-5 不介入——稅籍主碼仍是第一順位。"""
    tax = {"87654321": {"name": "", "org": "", "codes": [("474100", "零售業")]}}
    rec = engine.query("87654321", FakeProvider(tax=tax, listed=LISTED_FIXTURE, offline=True))
    assert rec["產業大類"] == "G 批發及零售業"


def test_l2_5_missing_file_skips_layer():
    """兩個名冊檔缺席時整層跳過，維持「未登記（稅籍查無）」。"""
    rec = engine.query("87654321", FakeProvider(offline=True), fallback_name="測試控股")
    assert rec["產業大類"] == R.TAX_MISSING_SECTION


def test_l3_d_dissolved_status():
    """GCIS 登記狀態含解散字樣 → 已解散（歷史戶），身分軌維持一般企業。"""
    comp = {"87654321": {"name": "測試已解散股份有限公司", "status": "解散"}}
    rec = engine.query("87654321", FakeProvider(company=comp))
    assert rec["產業大類"] == R.DISSOLVED_SECTION
    assert rec["產業子類"] == ""
    assert rec["大分類"] == "一般企業", "身分軌應維持一般企業，實得 %s" % rec["大分類"]
    assert rec["分類依據詞"] == "GCIS 登記狀態：解散"
    assert rec["登記狀態"] == "解散"
    assert rec["信心"] == "low", "非核准登記狀態應降級進複核"
    assert rec["分類依據層"] == "L3-D GCIS 登記狀態", rec["分類依據層"]
    assert rec["判定規則"].startswith("L3-D 登記狀態「解散」"), rec["判定規則"]
    assert "各層皆未命中" not in rec["判定規則"]


def test_l3_d_liquidation_and_bankruptcy_status():
    """終局狀態不含「解散」四詞的兩種實例（重整完成暨清算完結／破產）也要判歷史戶。

    2026-08-22 實資料補列：歌林＝「重整完成暨清算完結」、佳晶＝「破產」，
    原四詞（解散/廢止/撤銷/撤回）都比對不到，曾漏留在未登記。
    """
    for status in ("重整完成暨清算完結", "破產"):
        comp = {"87654321": {"name": "測試終局狀態股份有限公司", "status": status}}
        rec = engine.query("87654321", FakeProvider(company=comp))
        assert rec["產業大類"] == R.DISSOLVED_SECTION, (status, rec["產業大類"])
        assert rec["登記狀態"] == status
        assert rec["信心"] == "low"


def test_named_group_dissolved_status():
    """具名大類（銀行等）＋解散狀態 → 單軌改標歷史戶，身分軌保留原群組。

    使用者 2026-08-21 裁示：非存續戶不分身分軌群組，一律標「已解散（歷史戶）」。
    身分軌回答「它是什麼」（證券期貨／金控與銀行…），單軌的產業大類回答
    「它還在不在」——兩個問題，所以兩欄不同值是對的，不是矛盾。

    v5 初版在這條路徑上把 resolve_name() 已經拿到的狀態整個丟掉了，結果已廢止的
    外商銀行分行輸出「登記狀態空白＋medium」，看起來像正常存續戶。
    """
    comp = {"87654321": {"name": "測試商測試銀行股份有限公司", "status": "廢止登記"}}
    rec = engine.query("87654321", FakeProvider(company=comp))
    assert rec["大分類"] == "金控與銀行", "身分軌應保留原群組，實得 %s" % rec["大分類"]
    assert rec["子分類"] == "本國銀行"
    assert rec["產業大類"] == R.DISSOLVED_SECTION, \
        "非存續戶單軌應標歷史戶，實得 %s" % rec["產業大類"]
    assert rec["產業子類"] == ""
    assert rec["登記狀態"] == "廢止登記", "登記狀態被丟掉了"
    assert rec["信心"] == "low", "非核准登記狀態應降級進複核"
    assert rec["名稱來源"] == "GCIS商工登記"
    assert rec["分類依據層"] == "L3-D GCIS 登記狀態", rec["分類依據層"]
    assert "GCIS 商工登記" in rec["判定規則"], rec["判定規則"]


# ── L1-3 override 的兩種值形態（使用者 2026-08-21 放寬行業軌覆寫）─────────
def _with_override(tax_id, value):
    """暫時塞一筆 override，用完還原。"""
    saved = dict(X.OVERRIDE)
    X.OVERRIDE[tax_id] = value
    return saved


def test_override_industry_track_value():
    """override 填 A–S 行業軌值：單軌直接採用，身分軌降為一般企業／未細分。

    用途是替「官方來源全查無」的戶填空白——稅籍、各名冊、上市櫃、GCIS 都答不出來時，
    人工查證的結果總得有地方填。
    """
    saved = _with_override("87654321",
                           ("C 製造業", "26 電子零組件製造",
                            "人工查證：實際從事印刷電路板製造；官方各來源皆查無", "2026-08-21"))
    try:
        rec = engine.query("87654321", FakeProvider(offline=True), fallback_name="測試電子廠")
        assert (rec["產業大類"], rec["產業子類"]) == ("C 製造業", "26 電子零組件製造"), \
            "單軌應採 override 值，實得 %s／%s" % (rec["產業大類"], rec["產業子類"])
        assert (rec["大分類"], rec["子分類"]) == ("一般企業", "未細分"), \
            "身分軌應降為一般企業／未細分，實得 %s／%s" % (rec["大分類"], rec["子分類"])
        assert rec["分類依據層"] == "L1 特殊規則"
        assert rec["判定規則"].startswith("L1-3")
        assert rec["分類依據詞"].startswith("人工裁決：")
        assert rec["信心"] == "high"
        # 值域自我檢查：身分軌形態必須合法
        assert rec["子分類"] in R.SUBGROUPS[rec["大分類"]]
    finally:
        X.OVERRIDE.clear()
        X.OVERRIDE.update(saved)


def test_override_unmapped_value_and_free_text_sub():
    """override 也可填單軌「其他」，子分類不受身分軌值域限制（可任意描述或空白）。"""
    saved = _with_override("87654321",
                           ("其他", "宗教及類似組織", "人工查證：宗教財團法人附屬事業", "2026-08-21"))
    try:
        rec = engine.query("87654321", FakeProvider(offline=True), fallback_name="測試單位")
        assert (rec["產業大類"], rec["產業子類"]) == ("其他", "宗教及類似組織")
        assert (rec["大分類"], rec["子分類"]) == ("一般企業", "未細分")
        assert rec["子分類"] in R.SUBGROUPS[rec["大分類"]], "值域自我檢查要通過"
    finally:
        X.OVERRIDE.clear()
        X.OVERRIDE.update(saved)
    # 子分類留空也合法
    saved = _with_override("87654321", ("N 支援服務業", "", "人工查證：人力派遣", "2026-08-21"))
    try:
        rec = engine.query("87654321", FakeProvider(offline=True), fallback_name="測試單位")
        assert (rec["產業大類"], rec["產業子類"]) == ("N 支援服務業", "")
    finally:
        X.OVERRIDE.clear()
        X.OVERRIDE.update(saved)


def test_dissolved_beats_industry_track_override():
    """L3-D 解散覆寫優先於 L1-3 行業軌值（指揮官 2026-08-21 裁定，全域單一語意）。

    「已解散」回答的是「這家還在不在」，與那一戶的產業是人工填的還是引擎判的無關。
    形態 (A) 身分軌值本來就是這個行為，形態 (B) 對齊。
    """
    comp = {"87654321": {"name": "測試電子廠股份有限公司", "status": "解散"}}
    saved = _with_override("87654321",
                           ("C 製造業", "26 電子零組件製造",
                            "人工查證：實際從事印刷電路板製造", "2026-08-21"))
    try:
        rec = engine.query("87654321", FakeProvider(company=comp))
        assert rec["產業大類"] == R.DISSOLVED_SECTION, \
            "解散應覆寫人工填的行業軌值，實得 %s" % rec["產業大類"]
        assert rec["產業子類"] == ""
        assert rec["分類依據層"] == "L3-D GCIS 登記狀態"
        assert rec["登記狀態"] == "解散"
        assert rec["信心"] == "low", "由 L3-D 接手後降級照常套用"
    finally:
        X.OVERRIDE.clear()
        X.OVERRIDE.update(saved)


def test_override_identity_track_value_unchanged():
    """迴歸：override 填身分軌值時行為完全不變（單軌沿用身分軌，不降級）。"""
    saved = _with_override("87654321",
                           ("證券期貨", "證券商", "測試裁決：業務往來主體為證券子公司", "2026-08-21"))
    try:
        rec = engine.query("87654321", FakeProvider(offline=True), fallback_name="測試投資公司")
        assert (rec["大分類"], rec["子分類"]) == ("證券期貨", "證券商")
        assert (rec["產業大類"], rec["產業子類"]) == ("證券期貨", "證券商"), \
            "身分軌命中者單軌應沿用，實得 %s／%s" % (rec["產業大類"], rec["產業子類"])
        assert rec["分類依據層"] == "L1 特殊規則"
    finally:
        X.OVERRIDE.clear()
        X.OVERRIDE.update(saved)


def test_override_industry_values_are_verbatim_sections():
    """行業軌 override 白名單必須逐字取自既有 SECTION_RANGES，不另造字串。"""
    assert "C 製造業" in R.OVERRIDE_INDUSTRY_VALUES
    assert R.UNMAPPED_SECTION in R.OVERRIDE_INDUSTRY_VALUES
    assert len(R.OVERRIDE_INDUSTRY_VALUES) == len(R.SECTION_RANGES) + 1
    # 引擎判出來的狀態值不開放人工填
    assert R.TAX_MISSING_SECTION not in R.OVERRIDE_INDUSTRY_VALUES
    assert R.DISSOLVED_SECTION not in R.OVERRIDE_INDUSTRY_VALUES
    # 錯字不會被當成行業軌值——會落回身分軌形態，由值域自我檢查抓出來
    assert "C製造業" not in R.OVERRIDE_INDUSTRY_VALUES


def test_duplicate_taxid_does_not_flip_live_agency():
    """統編重號守門：GCIS 同號記錄的名稱對不上官方名冊名稱時，不採用其狀態。

    實例：某分署的統編在 GCIS 是一家已解散的企業社。沒有守門的話，
    現存機關會被那家公司的「解散」狀態改標成歷史戶。
    """
    comp = {"87654321": {"name": "測試乙企業有限公司", "status": "解散"}}
    p = FakeProvider(company=comp, gov={"87654321": "測試部測試發展署測試分署"})
    rec = engine.query("87654321", p)
    assert (rec["大分類"], rec["子分類"]) == ("政府機關", "政府機關")
    assert rec["產業大類"] == "政府機關", "重號的解散狀態不該改標，實得 %s" % rec["產業大類"]
    assert rec["登記狀態"] == "", "不採用的狀態不該寫進登記狀態欄"
    assert rec["信心"] == "high", "不採用就不該連帶降級"
    assert "名稱不符" in rec["統編備註"] and "測試乙企業有限公司" in rec["統編備註"], \
        "看到的東西要如實記下來，實得 %r" % rec["統編備註"]


def test_registry_name_matches_gcis_still_flips():
    """守門不誤殺：GCIS 名稱與名冊名稱一致時，解散狀態照樣採用。"""
    comp = {"87654321": {"name": "測試金融控股股份有限公司", "status": "合併解散"}}
    p = FakeProvider(company=comp,
                     authority={"87654321": [{"industry_group": "金控與銀行",
                                              "industry_detail": "金融控股",
                                              "source": "金管會名冊",
                                              "customer_name": "測試金融控股股份有限公司"}]})
    rec = engine.query("87654321", p)
    assert rec["名稱來源"] == "金管會名冊"
    assert rec["產業大類"] == R.DISSOLVED_SECTION, rec["產業大類"]
    assert rec["大分類"] == "金控與銀行", "身分軌保留原群組"
    assert rec["登記狀態"] == "合併解散"
    assert rec["信心"] == "low"
    assert "名稱不符" not in rec["統編備註"]


def test_names_match_tolerates_abbreviation():
    """名稱比對：去空白後相等或一方包含另一方即算同一實體。"""
    assert engine.names_match("測試金融控股股份有限公司", "測試金融控股股份有限公司")
    assert engine.names_match("測試金融控股股份有限公司", "測試金融控股")   # 全銜 vs 簡稱
    assert engine.names_match("測試 企業 有限公司", "測試企業有限公司")     # 空白差異
    assert not engine.names_match("測試乙企業有限公司", "測試部測試發展署測試分署")
    assert not engine.names_match("", "測試企業有限公司")                  # 無從比對＝不算符合


def test_suspension_is_information_only():
    """停業＝純資訊透出：登記狀態如實寫，但分類與信心完全不受影響。

    停業戶還在，只是暫時不營業——不是解散，不該觸發 L3-D，也不該降級。
    """
    tax = {"87654321": {"name": "測試工程股份有限公司", "org": "",
                        "codes": [("271000", "電腦電子產品製造")]}}
    comp = {"87654321": {"name": "測試工程股份有限公司", "status": "核准設立",
                         "case_status": "停業", "sus_beg": "1141130", "sus_end": "1151129"}}
    # 稅籍查得到 → 根本不問 GCIS，登記狀態留空（現行語意：還在稅籍＝還在營業）
    rec = engine.query("87654321", FakeProvider(tax=tax, company=comp))
    assert rec["登記狀態"] == ""
    assert rec["產業大類"] == "C 製造業"
    # 稅籍查無 → 透出停業，但分類與信心不動
    rec = engine.query("87654321", FakeProvider(company=comp), fallback_name="測試工程")
    assert rec["登記狀態"] == "停業（迄 1151129）", rec["登記狀態"]
    assert rec["產業大類"] != R.DISSOLVED_SECTION, "停業不是解散，不得觸發 L3-D"
    assert rec["產業大類"] == R.TAX_MISSING_SECTION
    assert not rec["分類依據層"].startswith("L3-D")
    # 「不降級」＝與沒有停業資訊的同一筆相比，信心與分類完全一樣
    # （這一筆本來就是 L4 兜底的 low，那是兜底造成的，不是停業造成的）
    plain = {"87654321": {"name": "測試工程股份有限公司", "status": "核准設立"}}
    base = engine.query("87654321", FakeProvider(company=plain), fallback_name="測試工程")
    assert (rec["信心"], rec["產業大類"], rec["分類依據層"]) == \
           (base["信心"], base["產業大類"], base["分類依據層"]), "停業改變了判定"
    # 沒有迄日時只寫「停業」
    comp2 = {"87654321": {"name": "測試工程股份有限公司", "status": "核准設立",
                          "case_status": "停業", "sus_beg": "", "sus_end": ""}}
    rec = engine.query("87654321", FakeProvider(company=comp2), fallback_name="測試工程")
    assert rec["登記狀態"] == "停業"


def test_suspension_respects_name_gate():
    """統編重號守門對停業資訊同樣適用：名稱不符就不採用。"""
    comp = {"87654321": {"name": "測試乙企業有限公司", "status": "核准設立",
                         "case_status": "停業", "sus_beg": "1141130", "sus_end": "1151129"}}
    p = FakeProvider(company=comp, gov={"87654321": "測試部測試發展署測試分署"})
    rec = engine.query("87654321", p)
    assert rec["登記狀態"] == "", "重號記錄的停業資訊不該採用"
    assert "名稱不符" in rec["統編備註"]
    assert rec["大分類"] == "政府機關"


def test_legacy_gcis_cache_without_suspension_fields():
    """舊快取只有 name／status：缺欄位視為空，不炸也不重打 API。"""
    comp = {"87654321": {"name": "測試控股股份有限公司", "status": "核准設立"}}
    rec = engine.query("87654321", FakeProvider(company=comp), fallback_name="測試控股")
    assert rec["登記狀態"] == "核准設立"
    assert rec["產業大類"] == R.TAX_MISSING_SECTION


def test_frozen_status_dissolved_offline():
    """L1-5 凍結表命中同樣改標歷史戶，且不需連線；判定規則要標明來源是凍結表。"""
    saved = dict(X.FROZEN_STATUS)
    try:
        X.FROZEN_STATUS["87654321"] = "合併解散"
        rec = engine.query("87654321", FakeProvider(offline=True), fallback_name="測試已併購公司")
        assert rec["產業大類"] == R.DISSOLVED_SECTION
        assert rec["登記狀態"] == "合併解散"
        assert rec["信心"] == "low"
        assert "L1-5 凍結表" in rec["判定規則"], rec["判定規則"]
    finally:
        X.FROZEN_STATUS.clear()
        X.FROZEN_STATUS.update(saved)


def test_override_row_dissolved_keeps_identity():
    """L1-3 人工裁決命中的戶若同時已解散：身分軌依裁決，單軌仍標歷史戶。"""
    saved_o, saved_s = dict(X.OVERRIDE), dict(X.FROZEN_STATUS)
    try:
        X.OVERRIDE["87654321"] = ("證券期貨", "證券商", "測試裁決：業務往來主體為證券子公司", "2026-08-21")
        X.FROZEN_STATUS["87654321"] = "解散"
        rec = engine.query("87654321", FakeProvider(offline=True), fallback_name="測試投資股份有限公司")
        assert (rec["大分類"], rec["子分類"]) == ("證券期貨", "證券商"), \
            "身分軌應依 override，實得 %s／%s" % (rec["大分類"], rec["子分類"])
        assert rec["產業大類"] == R.DISSOLVED_SECTION
        assert rec["信心"] == "low"
    finally:
        X.OVERRIDE.clear()
        X.OVERRIDE.update(saved_o)
        X.FROZEN_STATUS.clear()
        X.FROZEN_STATUS.update(saved_s)


def test_l3_d_skipped_offline():
    """--offline 時不打 GCIS，統編備註如實標記，不假裝這層跑過了。"""
    comp = {"87654321": {"name": "測試已解散股份有限公司", "status": "解散"}}
    rec = engine.query("87654321", FakeProvider(company=comp, offline=True),
                       fallback_name="測試已解散股份有限公司")
    assert rec["產業大類"] == R.TAX_MISSING_SECTION
    assert "離線" in rec["統編備註"], "應標記離線未查，實得 %r" % rec["統編備註"]


# ══════════════════════════════════════════════════════════════════════════
# 值域自我檢查：所有新路徑的輸出都必須落在合法值域
# ══════════════════════════════════════════════════════════════════════════
def test_all_new_paths_in_domain():
    cases = [("F000001", ""), ("P000001", ""), ("", "查無此名稱的單位"),
             ("2209913199", "格式壞掉"), ("12345678", "測試聯合會計師事務所"),
             ("12345678", "測試市立聯合醫院"), ("12345678", "國立測試大學")]
    for tid, name in cases:
        rec = engine.query(tid, FakeProvider(offline=True), fallback_name=name)
        assert rec["子分類"] in R.SUBGROUPS.get(rec["大分類"], []), \
            "%r／%r → %s／%s 越界" % (tid, name, rec["大分類"], rec["子分類"])


def test_batch_reader_keeps_nameless_taxid_rows(tmp_path):
    """classify.read_taxids：統編空但有名稱的列要保留，全空列才丟。"""
    from classify import read_taxids                   # noqa: PLC0415
    p = tmp_path / "in.csv"
    p.write_text("統一編號,備用名稱\n22099131,台積電\n,某某無統編單位\n,\nF000001,某境外法人\n",
                 encoding="utf-8-sig")
    got = read_taxids(str(p))
    assert got == [("22099131", "台積電"), ("", "某某無統編單位"), ("F000001", "某境外法人")], got


# ══════════════════════════════════════════════════════════════════════════
# 真資料個案（缺 data/ 時跳過）
# ══════════════════════════════════════════════════════════════════════════
# (統編, 期望產業大類, 期望產業子類, 說明) —— 全為公開個體
REAL_GOLDEN = [
    ("02612701", "教育與法人", "學校", "長庚大學：稅籍主碼 68 不動產，v4 誤判不動產業"),
    ("84926791", "G 批發及零售業", "47 零售業（綜合商品）",
     "大學光學科技：公司型後綴防呆對照組，必須維持批發零售"),
    ("22099131", "C 製造業", "26 電子零組件製造", "台積電：行業軌與 v4 相同"),
]


def test_real_golden_cases():
    if not HAVE_LOCAL_DATA:
        print("    －（缺本地資料，跳過真資料個案）")
        return
    from classify import build_provider                # noqa: PLC0415
    _, provider = build_provider("local", offline=True)
    for tid, want_group, want_sub, why in REAL_GOLDEN:
        rec = engine.query(tid, provider)
        got = (rec["產業大類"], rec["產業子類"])
        assert got == (want_group, want_sub), \
            "%s（%s）：期望 %s／%s，實得 %s／%s" % (tid, why, want_group, want_sub, *got)
