# -*- coding: utf-8 -*-
r"""統編正規化（獨立副本，切斷對原產線 etl/shared.py 的依賴）

來源：原產線 etl/shared.py 的 normalize_tax_id，逐字複製。
為什麼複製而不 import：原檔模組層 `import pandas as pd`，
為了一支純字串函式而讓交付包被迫依賴 pandas 不合理。

★ 若 原產線那側修改了此函式，本檔需同步——兩邊必須同口徑，
  否則同一份來源資料會被正規化成不同統編，join 結果就會分歧。
"""


def normalize_tax_id(raw):
    """正規化統編：去空白／去尾 .0／純數字且 <=8 碼補前導 0 至 8 碼；其餘原樣。

    台灣統編固定 8 碼。另含一條保守清洗規則：儲存格內同一數字統編重複多次
    （如「85840188  85840188」，來源＝營管年費報表匯出端損毀）→ 取單一值再正規化；
    token 彼此不同時絕不猜、維持原樣（下游歸「未分類」，不冒認錯戶的風險）。
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if s.endswith(".0"):
        s = s[:-2]
    if s == "" or s.lower() == "nan":
        return ""
    tokens = s.split()
    if len(tokens) > 1 and len(set(tokens)) == 1 and tokens[0].isdigit():
        s = tokens[0]
    if s.isdigit() and len(s) <= 8:
        return s.zfill(8)
    return s
