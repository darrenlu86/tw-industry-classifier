# -*- coding: utf-8 -*-
r"""例外表載入層 — 把「與特定機構有關的人工裁決」與「通用規則」分開

為什麼要分開
────────────
`rules.py` 放的是**通用規則**：稅籍碼表、值域、名稱關鍵字。這些對任何人都適用，
可以公開、可以共用。

但實務上一定會有「這家公司我們就是要歸在某一類」的人工裁決——理由往往涉及
業務往來關係、對特定公司的內部研究、或帳務系統的歷史包袱。這類內容：

  * 不該混在通用規則裡（會讓規則檔看起來像一份客戶清單）
  * 不該進版控（統編出現在例外表裡，等於間接揭露那是誰的處理對象）
  * 換一個組織使用時本來就該重新檢視，不該照抄

所以它們放在外部檔 `exceptions/local_exceptions.json`，本檔負責載入。
檔案不存在時四張表都是空的，分類器照樣跑——只是少了那些人工裁決。

四張表的用途
────────────
  TAX_ID_FIX     統編修正：帳務系統的佔位碼或已知錯碼 → 正確統編
  PERIPHERAL     周邊單位白名單（**本檔內建，因為都是法定公開機構**）；
                 v4 起值為 (官方名稱, 所屬大類)，判定結果＝所屬大類／周邊單位
  OVERRIDE       已裁決例外：名冊或稅籍碼判定與事實不符者
  FROZEN_NAMES   存量凍結官方名稱：四個名冊都查無者的名稱（人工查證一次）
  FROZEN_STATUS  存量凍結登記狀態：已知終止登記者

格式與維護方式見 `exceptions/README.md`；範本見 `exceptions/local_exceptions.example.json`。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOCAL_FILE = os.path.join(ROOT, "exceptions", "local_exceptions.json")

# ══════════════════════════════════════════════════════════════════════════
# 周邊單位白名單（內建）
#
# 這幾家的共同點：職能是法定金融市場基礎設施，但登記型態各異
# （股份有限公司與財團法人並存），稅籍行業代號也分散在證券輔助、票據交換所、
# 其他金融輔助等不同碼——沒有任何單一規則能涵蓋，只能用統編認定。
#
# 它們都是公開機構，把它列在這裡不涉及任何組織的內部資訊，
# 而且任何做台灣金融機構分類的人都會遇到同一個問題，所以內建共用。
#
# v4 起值為 (官方名稱, 所屬大類)：周邊單位不再歸政府機關，改掛所服務的
# 金融市場大類（金控與銀行／證券期貨／保險），子分類一律「周邊單位」。
# 組織自行擴充（公協會等業務視角認定）放本地例外檔 peripheral_extra。
# ══════════════════════════════════════════════════════════════════════════
PERIPHERAL_BUILTIN = {
    "03559508": ("臺灣證券交易所股份有限公司", "證券期貨"),
    "23474232": ("臺灣集中保管結算所股份有限公司", "證券期貨"),
    "16092130": ("臺灣期貨交易所股份有限公司", "證券期貨"),
    "92002238": ("財團法人中華民國證券櫃檯買賣中心", "證券期貨"),
    "29188566": ("臺灣碳權交易所股份有限公司", "證券期貨"),
    "15639870": ("財團法人台灣票據交換所", "金控與銀行"),
    "00999340": ("財團法人金融聯合徵信中心", "金控與銀行"),
}
# 引擎實際查的表＝內建＋本地追加。文件與規則計數只認內建
# （本地追加屬各組織裁決，不進版控文件——見 emit_rule_table.py）。
PERIPHERAL = dict(PERIPHERAL_BUILTIN)

# ── 以下四張表預設為空，由本地例外檔填入 ──────────────────────────────────
TAX_ID_FIX = {}
OVERRIDE = {}
FROZEN_NAMES = {}
FROZEN_STATUS = {}

_loaded_from = ""


def _coerce_override(raw):
    """OVERRIDE 的值是 [大分類, 子分類, 理由, 裁決日]，容忍缺裁決日。"""
    out = {}
    for tid, v in (raw or {}).items():
        if isinstance(v, (list, tuple)) and len(v) >= 3:
            group, sub, why = v[0], v[1], v[2]
            when = v[3] if len(v) > 3 else ""
            out[str(tid)] = (group, sub, why, when)
    return out


def _coerce_pairs(raw):
    """TAX_ID_FIX 與 FROZEN_NAMES 的值都是 [值, 說明]。"""
    out = {}
    for tid, v in (raw or {}).items():
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            out[str(tid)] = (v[0], v[1])
    return out


def load(path=None):
    """載入本地例外檔。回傳實際載入的路徑（沒有檔案回空字串）。"""
    global TAX_ID_FIX, OVERRIDE, FROZEN_NAMES, FROZEN_STATUS, _loaded_from
    PERIPHERAL.clear()
    PERIPHERAL.update(PERIPHERAL_BUILTIN)
    p = path or LOCAL_FILE
    if not os.path.exists(p):
        _loaded_from = ""
        return ""
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    TAX_ID_FIX = _coerce_pairs(data.get("tax_id_fix"))
    OVERRIDE = _coerce_override(data.get("override"))
    FROZEN_NAMES = _coerce_pairs(data.get("frozen_names"))
    FROZEN_STATUS = {str(k): v for k, v in (data.get("frozen_status") or {}).items()}
    # 本地檔也可以追加周邊單位（例如你的組織把某個公協會視為周邊單位）。
    # 值格式 [官方名稱, 所屬大類]；所屬大類限 金控與銀行／證券期貨／保險。
    for tid, v in (data.get("peripheral_extra") or {}).items():
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            PERIPHERAL[str(tid)] = (v[0], v[1])
    _loaded_from = p
    return p


def summary():
    """給 --doctor 用的一行摘要。"""
    return {
        "本地例外檔": _loaded_from or "（未提供，四張例外表為空）",
        "統編修正": len(TAX_ID_FIX),
        "周邊單位白名單": "%d（內建 %d＋本地追加 %d）" % (
            len(PERIPHERAL), len(PERIPHERAL_BUILTIN),
            len(PERIPHERAL) - len(PERIPHERAL_BUILTIN)),
        "已裁決例外": len(OVERRIDE),
        "凍結名稱": len(FROZEN_NAMES),
        "凍結登記狀態": len(FROZEN_STATUS),
    }


load()
