# -*- coding: utf-8 -*-
r"""名冊 segment 值域與對映（獨立副本，切斷對原產線 etl/process_industry.py 的依賴）

來源：原產線 etl/process_industry.py 的 GROUP_ORDER 與 SEGMENT_TO_GROUP，逐字複製。
為什麼複製而不 import：原檔 import 時有副作用（會 reconfigure stdout、
在原產線的 data 目錄下建目錄），且它自己還 import pandas。builder 其實只需要這兩個常數。

★ 值域原與 原產線的 etl/process_industry.py 對齊。**2026-08-11 v4 改版起刻意分岔**：
  本檔依使用者核定把「其他金融」改為「租賃」，原產線仍用「其他金融」——
  原產線的值域同步屬另案，未定案前兩邊不一致是已知狀態（見 docs/已知限制.md）。

注意：GROUP_ORDER 是「名冊事實層」的九大類值域，含「醫療生技」；
分類器 core/rules.py 的 GROUPS 是「查詢輸出」的八大類（醫療生技已併入一般企業）。
兩者刻意不同——名冊守門用前者，對外分類用後者。
"""

# 名冊事實層的九大類（authority_master.csv 的 industry_group 值域）
GROUP_ORDER = [
    "金控與銀行", "證券期貨", "保險", "電支支付",
    "租賃", "政府機關", "醫療生技", "教育與法人", "一般企業",
]
VALID_GROUPS = set(GROUP_ORDER)

VALID_STATUS = {"active", "renamed", "merged", "exited"}
VALID_SOURCE_PREFIXES = ("authority:", "legacy:")

# authority_master.csv 的 industry_detail（segment）→ group 對應表（24 segment）
SEGMENT_TO_GROUP = {
    # 金控與銀行
    "本國銀行": "金控與銀行",
    "外國銀行在臺分行": "金控與銀行",
    "陸銀在臺分行": "金控與銀行",
    "信用合作社": "金控與銀行",
    "農業金庫": "金控與銀行",
    "農會信用部": "金控與銀行",
    "漁會信用部": "金控與銀行",
    "郵政儲匯（兼營簡易壽險）": "金控與銀行",
    "票券金融": "金控與銀行",
    "金融控股": "金控與銀行",
    # 證券期貨
    "證券商": "證券期貨",
    "期貨商": "證券期貨",
    "投信": "證券期貨",
    "投顧": "證券期貨",
    "證券金融": "證券期貨",
    # 保險
    "人身保險": "保險",
    "財產保險": "保險",
    "再保險": "保險",
    "外商保險在臺分公司": "保險",
    "保險經紀人公司": "保險",
    "保險代理人公司": "保險",
    # 電支支付
    "專營電子支付": "電支支付",
    "第三方支付": "電支支付",
    # 租賃（v4：原「其他金融」大類移除，租賃公會名錄會員獨立成大類）
    "融資租賃資融": "租賃",
}
