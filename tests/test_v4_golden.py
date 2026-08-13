# -*- coding: utf-8 -*-
r"""v4 改版驗收：值域結構 ＋ 指標個案（golden）

test_consistency.py 只驗「自洽性」（重現性、兩模式一致、值域不越界），
對「這次改版有沒有把個案分到正確的新大類」沒有驗證能力——本檔補這一塊。

只用公開個體當 golden（法定基礎設施與租賃公會名錄會員，名錄本身公開），
不揭露任何組織的處理對象。本地例外檔（peripheral_extra）的追加戶不在此驗，
由各組織自行以新舊輸出 diff 複核。

用法：
    py -3.12 tests/test_v4_golden.py        # 亦相容 pytest
缺本地資料（BGMOPEN1.zip／authority_master.csv）時，引擎個案測試會跳過，
值域結構測試照跑。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "core"))

import rules as R       # noqa: E402
import exceptions as X  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DATA = os.path.join(ROOT, "data")
HAVE_LOCAL_DATA = (os.path.exists(os.path.join(DATA, "BGMOPEN1.zip"))
                   and os.path.exists(os.path.join(DATA, "authority_master.csv")))

# (統編, 期望大類, 期望子類, 說明) —— 全為公開個體
GOLDEN = [
    ("03559508", "證券期貨", "周邊單位", "臺灣證券交易所（內建白名單）"),
    ("00999340", "金控與銀行", "周邊單位", "金融聯合徵信中心（內建白名單）"),
    ("15639870", "金控與銀行", "周邊單位", "台灣票據交換所（內建白名單）"),
    ("05072925", "租賃", "融資租賃資融", "中租迪和（租賃公會名錄會員）"),
    # 遠智不在租賃名冊（在證期局券商名冊）→ 稅籍 6611 規則自判，無需人工裁決
    # （2026-08-11 使用者裁示移除其 override）
    ("29039617", "證券期貨", "證券商", "遠智證券（稅籍 661100，非租賃名冊會員）"),
]


def test_groups_domain():
    assert "租賃" in R.GROUPS, "v4 應有「租賃」大類"
    assert "其他金融" not in R.GROUPS, "v4 應已移除「其他金融」大類"
    assert R.SUBGROUPS["租賃"] == ["融資租賃資融"]
    for g in ("金控與銀行", "證券期貨", "保險"):
        assert "周邊單位" in R.SUBGROUPS[g], "%s 應含「周邊單位」子類" % g
    assert "周邊單位" not in R.SUBGROUPS["政府機關"], "周邊單位不再歸政府機關"


def test_removed_rules():
    assert not hasattr(R, "OTHERFIN_REFINE"), "L2-1R 細分應已移除"
    assert not hasattr(R, "AUTO4"), "AUTO4 汽機車碼表應已移除"
    assert not hasattr(R, "GCIS_TRIGGER_WORDS"), "L3-9 GCIS 層應已移除"
    assert "649100" not in R.FIN6 and "649699" not in R.FIN6, \
        "649100／649699 應自 FIN6 移除（非名錄會員走稅籍分類）"
    assert not any(rid == "N5" for rid, _, _, _ in R.NAME_RULES), "N5 資融規則應已移除"
    assert not any(g == "其他金融" for _, g, _, _ in R.NAME_RULES)


def test_peripheral_structure():
    assert len(X.PERIPHERAL) >= 7
    for tid, val in X.PERIPHERAL.items():
        assert isinstance(val, tuple) and len(val) == 2, "PERIPHERAL[%s] 應為 (名稱, 大類)" % tid
        name, group = val
        assert group in ("金控與銀行", "證券期貨", "保險"), \
            "PERIPHERAL[%s] 大類 %r 越界" % (tid, group)
        assert "周邊單位" in R.SUBGROUPS[group]


def test_section_track():
    assert len(R.SECTION_BY_MAJOR2) == 90, "A–S 中類展開應為 90 個 2 碼"
    assert R.SECTION_BY_MAJOR2["64"][0] == "K"
    assert R.SECTION_BY_MAJOR2["86"][0] == "Q"
    for empty in ("04", "07", "40", "44", "57", "89"):
        assert empty not in R.SECTION_BY_MAJOR2, "官方空碼 %s 不應在對照表" % empty


def test_golden_cases():
    if not HAVE_LOCAL_DATA:
        print("    －（缺本地資料，跳過引擎個案）")
        return
    import engine                                     # noqa: PLC0415
    from classify import build_provider               # noqa: PLC0415
    _, provider = build_provider("local", offline=True)
    for tid, want_group, want_sub, why in GOLDEN:
        rec = engine.query(tid, provider)
        got = (rec["大分類"], rec["子分類"])
        assert got == (want_group, want_sub), \
            "%s（%s）：期望 %s/%s，實得 %s/%s" % (tid, why, want_group, want_sub, *got)


def test_single_track_merge():
    """定版單軌（產業大類／產業子類）：身分軌沿用、一般企業走行業軌。"""
    if not HAVE_LOCAL_DATA:
        print("    －（缺本地資料，跳過引擎個案）")
        return
    import engine                                     # noqa: PLC0415
    from classify import build_provider               # noqa: PLC0415
    _, provider = build_provider("local", offline=True)
    # 身分軌命中者：產業大類／產業子類＝大分類／子分類（證交所，內建白名單）
    rec = engine.query("03559508", provider)
    assert (rec["產業大類"], rec["產業子類"]) == (rec["大分類"], rec["子分類"]), \
        "身分軌命中者單軌應沿用身分分類，實得 %s/%s" % (rec["產業大類"], rec["產業子類"])
    # 一般企業走行業軌（台積電為公開個體）：稅籍主碼 26 → C 製造業
    rec = engine.query("22099131", provider)
    assert rec["大分類"] == "一般企業", "台積電身分軌應為一般企業，實得 %s" % rec["大分類"]
    assert rec["產業大類"] == "C 製造業", \
        "台積電產業大類應為 C 製造業，實得 %s" % rec["產業大類"]
    assert rec["產業子類"].startswith("26 "), \
        "台積電產業子類應為 26 開頭中類，實得 %s" % rec["產業子類"]


def main():
    checks = [test_groups_domain, test_removed_rules, test_peripheral_structure,
              test_section_track, test_golden_cases, test_single_track_merge]
    failures = []
    for fn in checks:
        try:
            fn()
            print("  ✓ %s" % fn.__name__)
        except AssertionError as e:
            failures.append("%s：%s" % (fn.__name__, e))
            print("  ✗ %s：%s" % (fn.__name__, e))
    print("─" * 62)
    if failures:
        print("v4 golden 未通過（%d 項）" % len(failures))
        sys.exit(1)
    print("v4 golden 全部通過。")
    sys.exit(0)


if __name__ == "__main__":
    main()
