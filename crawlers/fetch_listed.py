#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fetch_listed.py — 上市（TWSE）／上櫃（TPEx）公司名冊 fetcher（v5 L2-5 資料線專屬）

目的
----
v5 新增 L2-5「上市櫃名冊」作為行業軌 fallback：無稅籍主行業碼可用的 KY／控股／
上市櫃公司，改用 TWSE／TPEx 官方公司基本資料的「產業別」欄位反推行業軌 A–S 大類。
本檔只負責「抓＋落地＋官方代碼表核對」，不碰 core/、classify.py、tests/。

資料來源（皆免 key、原始 JSON）
--------------------------------
1. TWSE 上市公司基本資料 t187ap03_L：
   https://openapi.twse.com.tw/v1/opendata/t187ap03_L
   關鍵欄位（2026-08-21 實測，逐字對照官方回傳鍵名，未憑記憶）：
     公司代號／公司名稱／產業別（2 碼數字代碼）／營利事業統一編號
2. TPEx 上櫃公司基本資料 mopsfin_t187ap03_O：
   https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O
   關鍵欄位（英文鍵名，2026-08-21 實測）：
     SecuritiesCompanyCode／CompanyName／SecuritiesIndustryCode／`UnifiedBusinessNo.`
     （欄位名本身含句點，官方原始鍵名如此，非本檔誤植）

兩份 API 皆只回傳「產業別代碼」（如 "01"），不回傳代碼對應的中文名稱。
官方也沒有單一頁面直接列出「代碼→名稱」對照表（MOPS 的下拉選單頁面
mops.twse.com.tw/mops/web/t51sb01 等對直連請求回傳「安全性考量拒絕」）。

代碼→名稱表的取得方法（誠實揭露，非憑記憶／常識）
--------------------------------------------------
改用 TWSE 官方 ISIN 登錄查詢系統（isin.twse.com.tw，臺灣證券交易所自建服務）：
    上市：https://isin.twse.com.tw/isin/C_public.jsp?strMode=2
    上櫃：https://isin.twse.com.tw/isin/C_public.jsp?strMode=4
該系統逐檔列出每家公司的「產業別」文字欄位（如「水泥工業」），但不含數字代碼。
本檔以「公司代號」為 join key，把 ISIN 登錄的文字欄位與 t187ap03_L／_O 的數字代碼
逐家比對：若某代碼底下所有公司在 ISIN 登錄查得的文字完全一致，視為該代碼的官方名稱；
若比對不到、或同一代碼下出現一種以上文字，一律標記「未確認」，不猜測、不用常識填。
2026-08-21 實測：TWSE 34 個代碼中 33 個做到 1:1 零衝突（1095 家全數比對成功）；
代碼 91（10 家台灣存託憑證 TDR 掛牌公司）在 ISIN 登錄的產業別欄位留白，故標未確認。
TPEx 28 個代碼全數 1:1 零衝突（890 家全數比對成功）。

FALLBACK_*_CODE_NAMES 為 2026-08-21 本次交叉比對的凍結快照，僅在 ISIN 登錄頁
未來抓取失敗時作為退路（同 fetch_sfb.py 的 FALLBACK_FSC_URLS 慣例）；
一律以當次動態解析結果優先，凍結快照過期不會被誤用去覆蓋新資料。

輸出
----
- `data/listed_master.csv`（UTF-8-SIG）：統一編號,公司代號,公司名稱,市場別,產業別代碼,產業別名稱
  市場別 ∈ {TWSE, TPEx}；統一編號一律 8 碼字串（zfill，沿用 `_taxid.normalize_tax_id`）；
  無統編或統編正規化後非 8 碼數字的列跳過並計數，不寫入。
- `data/listed_industry_map.csv`（UTF-8-SIG）：
  市場別,產業別代碼,產業別名稱,行業軌大類代碼,行業軌大類名稱,映射理由
  行業軌大類代碼/名稱逐字取自 core/rules.py 的 SECTION_RANGES（2026-08-21 核對複製，
  見本檔 SECTION_LABELS 常數註解）；「產業別名稱→A–S」為本檔作者的分類判斷，
  每碼皆附一行映射理由與信心（高/中/低），多解的碼在理由中明說取捨依據，
  查無官方代碼名稱或性質上無法對應單一大類者，行業軌欄位留空並註記。

用法
----
  py -3.12 -X utf8 fetch_listed.py
"""

from __future__ import annotations

import csv
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths
import _taxid

import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------------------
# 常數：資料來源
# ---------------------------------------------------------------------------

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; industry-classifier-fetch-listed/1.0)"}

TWSE_LISTED_API = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_LISTED_API = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
ISIN_TWSE_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"  # 本國上市證券
ISIN_TPEX_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"  # 本國上櫃證券

MASTER_SCHEMA = ["統一編號", "公司代號", "公司名稱", "市場別", "產業別代碼", "產業別名稱"]
MAP_SCHEMA = ["市場別", "產業別代碼", "產業別名稱", "行業軌大類代碼", "行業軌大類名稱", "映射理由"]

UNCONFIRMED = "未確認"

# 2026-08-21 交叉比對凍結快照（僅供 ISIN 登錄頁抓取失敗時的 fallback，見檔頭說明）。
# 代碼 91（TDR）刻意不列入——官方本就未提供名稱，凍結快照也不該替官方瞎補。
FALLBACK_TWSE_CODE_NAMES = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "14": "建材營造業",
    "15": "航運業", "16": "觀光餐旅", "17": "金融保險業", "18": "貿易百貨業",
    "20": "其他業", "21": "化學工業", "22": "生技醫療業", "23": "油電燃氣業",
    "24": "半導體業", "25": "電腦及週邊設備業", "26": "光電業", "27": "通信網路業",
    "28": "電子零組件業", "29": "電子通路業", "30": "資訊服務業", "31": "其他電子業",
    "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
}
FALLBACK_TPEX_CODE_NAMES = {
    "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維", "05": "電機機械",
    "06": "電器電纜", "10": "鋼鐵工業", "14": "建材營造業", "15": "航運業",
    "16": "觀光餐旅", "17": "金融保險業", "20": "其他業", "21": "化學工業",
    "22": "生技醫療業", "23": "油電燃氣業", "24": "半導體業", "25": "電腦及週邊設備業",
    "26": "光電業", "27": "通信網路業", "28": "電子零組件業", "29": "電子通路業",
    "30": "資訊服務業", "31": "其他電子業", "32": "文化創意業", "33": "農業科技業",
    "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
}

# ---------------------------------------------------------------------------
# 行業軌大類代碼/名稱 —— 逐字複製自 core/rules.py SECTION_RANGES（2026-08-21 核對，
# 該檔第 207–227 行）。只取 (letter, label)，不取中類 range（本檔不需要）。
# ★ core/rules.py 若修改此常數，本檔需同步；本檔不 import core，避免耦合到
#   core 模組其他相依（pandas／sqlite），做法比照 crawlers/_taxid.py 的獨立副本慣例。
# ---------------------------------------------------------------------------
SECTION_LABELS = {
    "A": "農、林、漁、牧業",
    "B": "礦業及土石採取業",
    "C": "製造業",
    "D": "電力及燃氣供應業",
    "E": "用水供應及污染整治業",
    "F": "營建工程業",
    "G": "批發及零售業",
    "H": "運輸及倉儲業",
    "I": "住宿及餐飲業",
    "J": "出版影音及資通訊業",
    "K": "金融及保險業",
    "L": "不動產業",
    "M": "專業、科學及技術服務業",
    "N": "支援服務業",
    "O": "公共行政及國防；強制性社會安全",
    "P": "教育業",
    "Q": "醫療保健及社會工作服務業",
    "R": "藝術、娛樂及休閒服務業",
    "S": "其他服務業",
}


def _section(letter: str) -> tuple[str, str]:
    return letter, SECTION_LABELS[letter]


# ---------------------------------------------------------------------------
# 「產業別名稱」→ 行業軌 A–S 映射（作者判斷，非官方對照表）
# 每筆 = (行業軌代碼或 None, 信心, 映射理由)；理由裡的抽樣數字來自 2026-08-21 實測。
# 名稱在 TWSE／TPEx 間語意一致（同屬 MOPS 上市櫃公司產業別分類），故用名稱而非
# (市場別, 代碼) 做 key；輸出時仍逐一寫回各市場各自的代碼列。
# ---------------------------------------------------------------------------
MAPPING_REASONS: dict[str, tuple[str | None, str, str]] = {
    "水泥工業": ("C", "高", "水泥製造屬稅務行業標準分類中類 23 窯業土石製品製造業，落於 C 製造業。"),
    "食品工業": ("C", "高", "食品製造業對應中類 08，落於 C 製造業。"),
    "塑膠工業": ("C", "高", "塑膠製品製造業對應中類 22，落於 C 製造業。"),
    "紡織纖維": ("C", "高", "紡織業對應中類 13，落於 C 製造業。"),
    "電機機械": ("C", "高", "機械設備製造業對應中類 29，落於 C 製造業。"),
    "電器電纜": ("C", "高", "電力設備製造業對應中類 28，落於 C 製造業。"),
    "玻璃陶瓷": ("C", "高", "窯業土石製品製造業對應中類 23，落於 C 製造業。"),
    "造紙工業": ("C", "高", "紙漿、紙及紙製品製造業對應中類 14，落於 C 製造業。"),
    "鋼鐵工業": ("C", "高", "基本金屬製造業對應中類 24，落於 C 製造業。"),
    "橡膠工業": ("C", "高", "橡膠製品製造業對應中類 21，落於 C 製造業。"),
    "汽車工業": ("C", "高",
                 "抽查 TWSE 該碼 44 家公司多為汽車與零組件製造商，對應中類 30 運輸工具製造業，"
                 "落於 C 製造業；未逐家排除少數經銷商可能性，故非完全窮舉驗證。"),
    "建材營造業": ("F", "中",
                   "抽查 TWSE 該碼 20 家清一色為『○○建設』型態公司（國泰建設、太子建設、冠德建設、"
                   "太平洋建設等），主業對應中類 41 住宅及大樓建築工程業，落於 F 營建工程業；"
                   "惟建設公司實務上常身兼營建與不動產開發兩種角色，L 不動產業（不動產開發）"
                   "亦為合理替代解讀，本碼組成不純粹，取 F 為多數解。"),
    "航運業": ("H", "高", "海運、空運及倉儲業對應中類 49–52，落於 H 運輸及倉儲業。"),
    "觀光餐旅": ("I", "中",
                 "抽查 TWSE 該碼 19 家以飯店（晶華、國賓、六福、雲品）與餐飲（王品、八方雲集、"
                 "三商餐飲）為主流，對應中類 55–56，落於 I 住宿及餐飲業；惟其中 2 家為旅行社"
                 "（雄獅、鳳凰），實際應屬 N 支援服務業中類 79 旅行及相關服務，占比小不影響整體取捨。"),
    "金融保險業": ("K", "高", "金融及保險業對應中類 64–66，落於 K，字面與內容皆無歧異。"),
    "貿易百貨業": ("G", "高", "貿易（批發）與百貨（零售）皆對應中類 45–48，落於 G 批發及零售業。"),
    "其他業": (None, "低",
               "抽查 TWSE 該碼 20 家組成極度分歧：投資控股（中租控股、勤益投資控股）、"
               "有線電視（大豐、台灣數位光訊，屬 J）、物流（立益物流開發，屬 H）、"
               "不動產開發（海悅國際開發，屬 L）、精密製造（經寶精密控股，屬 C）等並存。"
               "本碼性質為 TWSE 無法歸入其餘 19 類之 catch-all，不存在合理的單一 A–S 對應，"
               "故不填行業軌、明確標記無法對應（非查無官方代碼名稱，是名稱本身即為雜項分類）。"),
    "化學工業": ("C", "高", "化學材料與化學製品製造業對應中類 19–20，落於 C 製造業。"),
    "生技醫療業": ("C", "中",
                   "抽查 TWSE 該碼 15 家絕大多數為製藥／生技製造商（生達化學製藥、南光化學製藥、"
                   "美時化學製藥、台灣神隆等），對應中類 21 醫藥製造業，落於 C 製造業；"
                   "惟其中 2 家為投資控股公司（永信國際投資控股、中化控股），且不排除純研發服務"
                   "公司（可能屬 M 專業科學技術服務業），非全體一致。"),
    "油電燃氣業": ("D", "中",
                   "抽查 TWSE 該碼 8 家多數為天然氣／液化石油氣供應商（大台北區瓦斯、欣欣天然氣、"
                   "新海瓦斯、欣高石油氣），對應中類 35 電力及燃氣供應業，落於 D；惟其中台塑石化"
                   "為石油煉製（屬 C 製造業中類 19）、全國加油站為加油站零售（屬 G），D 為多數解"
                   "但非全部。"),
    "半導體業": ("C", "高", "半導體製造業對應中類 26，落於 C 製造業。"),
    "電腦及週邊設備業": ("C", "高", "電腦、電子產品及光學製品製造業對應中類 26，落於 C 製造業。"),
    "光電業": ("C", "高", "光電材料及元件製造業對應中類 26，落於 C 製造業。"),
    "通信網路業": ("C", "中",
                   "抽查 TWSE＋TPEx 該碼共 91 家，逾八成為通訊網路設備製造商（智邦、友訊、啟碁、"
                   "中磊、正文等），對應中類 26，落於 C 製造業；惟其中 3 家為電信服務業者"
                   "（中華電信、台灣大哥大、遠傳電信），實際應屬 J 出版影音及資通訊業中類 61 電信業，"
                   "另 1 家（神腦國際）為手機零售通路商屬 G——這是本碼組成中最明確可辨識的例外，"
                   "下游若命中這幾家指標性公司會被本表誤配，建議優先以稅籍碼層覆蓋。"),
    "電子零組件業": ("C", "高", "電子零組件製造業對應中類 26，落於 C 製造業。"),
    "電子通路業": ("G", "高",
                   "抽查 TWSE 該碼 21 家清一色為電子零組件／資訊產品經銷代理商（聯強國際、"
                   "大聯大控股、文曄科技、燦坤實業），對應中類 46–47 批發零售，落於 G。"),
    "資訊服務業": ("J", "高", "電腦系統整合、軟體開發服務對應中類 62，落於 J 出版影音及資通訊業。"),
    "其他電子業": ("C", "中",
                   "電子業 catch-all，未逐一抽查全部成員，以電子製造業慣例推定多數為 C 製造業，"
                   "信心不若半導體業／電子零組件業等碼明確（缺乏逐家驗證）。"),
    "文化創意業": ("J", "中",
                   "抽查 TPEx 該碼 26 家多數為數位遊戲開發（鈊象電子、宇峻奧汀、智冠科技、"
                   "遊戲橘子、昱泉國際）、出版（時報文化出版）、音樂（華研國際音樂），對應中類 "
                   "58/59/62，落於 J；惟含誠品生活（複合式書店零售，屬 G）與寬宏藝術經紀（藝術經紀，"
                   "屬 R 藝術娛樂休閒服務業）等少數例外。"),
    "農業科技業": ("C", "低",
                   "樣本僅 4 家（茂生農經、瑞基海洋生物科技、惠光、達邦蛋白生技），性質偏生技加工／"
                   "飼料生產，傾向歸 C 製造業；惟茂生農經涉飼料經銷、瑞基海洋生物科技涉水產養殖"
                   "研發，A 農、林、漁、牧業（若為直接養殖）與 M 專業科學技術服務業（若為研發服務）"
                   "皆為合理替代解，樣本過小不宜視為定論。"),
    "綠能環保": (None, "低",
                 "抽查 TWSE＋TPEx 該碼共 46 家，組成三分天下：風電／材料設備製造（世紀離岸風電設備、"
                 "上緯國際、聚恆科技、台鎔科技材料等）占最大宗、廢棄物清除處理（可寧衛、日友環保、"
                 "中聯資源、山林水環境工程，對應 E 用水供應及污染整治業中類 38）約 9–10 家、發電業"
                 "（雲豹能源、泓德能源、富威電力，對應 D 電力供應業）約 5–6 家。三種解讀皆成立且"
                 "無明顯多數，本碼信心為全表最低，故不填行業軌；下游命中此碼應優先以稅籍碼／"
                 "名稱關鍵字層覆蓋，不宜逕採本表。"),
    "數位雲端": ("J", "中",
                 "抽查 TWSE＋TPEx 該碼共 40 家多數為雲端／資安／軟體服務商（資拓宏宇、安碁資訊、"
                 "91APP、綠界科技），對應中類 62，落於 J；惟含電商零售平台（富邦媒體 momo、網路家庭 "
                 "PChome，性質應屬 G 無店面零售）與計程車叫車平台（台灣大車隊，性質可能屬 H）等例外。"),
    "運動休閒": ("C", "中",
                 "抽查 TWSE＋TPEx 該碼共 27 家多數為運動用品／器材製造代工商（寶成工業、豐泰企業"
                 "製鞋代工、美利達、巨大機械自行車、喬山健康科技健身器材），對應中類 29/30，落於 C；"
                 "惟含 2 家健身房營運商（世界健身、柏文健康事業），性質應屬 R 藝術、娛樂及休閒服務業。"),
    "居家生活": ("G", "低",
                 "抽查 TWSE＋TPEx 該碼共 35 家組成接近對半：家具家飾生活用品零售商（全家便利商店、"
                 "寶雅國際、詩肯、炎洲流通、振宇五金）與家用品製造商（橋椿金屬、台灣櫻花、台灣福興"
                 "工業、成霖企業、億豐綜合工業窗簾製造）並存，無明顯多數，本表取 G（零售端數量略多）"
                 "但信心偏低。"),
}

# ---------------------------------------------------------------------------
# TPEx 專屬覆寫（2026-08-21 修正輪）
# 起因：MAPPING_REASONS 以「產業別名稱」為 key，TWSE／TPEx 共用同一段理由文字，但兩市場
# 同代碼底下的實際公司完全不同——fresh 對抗審查抓到 TPEx 14/16/20/22/23/29 六列的理由誤引
# TWSE 公司（如「永信國際投資控股」根本不在 TPEx 名單）。以下六碼改用抽查 TPEx 自己
# `data/listed_master.csv` 實際公司名單得出的理由；映射值（letter/confidence）逐碼獨立重新
# 判斷，非照抄 TWSE 結論。只覆寫這六碼，其餘代碼（含這六碼在 TWSE 的列）仍走上面的
# MAPPING_REASONS，不受影響。
# ---------------------------------------------------------------------------
TPEX_CODE_OVERRIDES: dict[str, tuple[str | None, str, str]] = {
    "14": ("F", "中",
           "抽查 TPEx 該碼 33 家，組成比 TWSE 更純粹：清一色為「○○開發」「○○建設」「○○營造」"
           "「○○地產」型態公司（三圓建設、富宇地產、坤悅開發、永信建設開發、德昌營造、力麒建設、"
           "力泰建設企業、新潤興業等），對應中類 41 住宅及大樓建築工程業，落於 F 營建工程業；"
           "惟同 TWSE 版本的疑慮仍在——這類建設公司常身兼營建與不動產開發角色，L 不動產業"
           "（不動產開發）仍是合理替代解，故維持中信心。"),
    "16": ("I", "中",
           "抽查 TPEx 該碼 32 家：餐飲連鎖（安心食品服務、漢來美食、瓦城泰統、六角國際事業、"
           "王座國際餐飲、亞洲藏壽司、全家國際餐飲、築間餐飲事業、金色三麥餐飲等）約 14 家、"
           "飯店（富野大飯店、亞都麗緻大飯店、知本老爺大酒店、洛碁實業）4 家，合計約 18/32≈56%"
           "對應中類 55–56，落於 I 住宿及餐飲業；惟旅行社（台鋼燦星國際旅行社、易飛網國際旅行社、"
           "山富國際旅行社、五福旅行社、旅天下聯合國際旅行社）達 5 家，實際應屬 N 支援服務業"
           "中類 79 旅行及相關服務，另遊樂園／育樂（劍湖山世界、南仁湖育樂、力麗觀光開發）3 家"
           "應屬 R 藝術、娛樂及休閒服務業——TPEx 版本的旅行社／育樂占比（8/32≈25%）明顯高於"
           "TWSE 版本（2/19），非單純多數解，維持中信心並如實揭露此差異。"),
    "20": (None, "低",
           "抽查 TPEx 該碼 42 家，異質程度不亞於 TWSE：精密機械／材料製造（精剛精密科技、"
           "大甲永和機械工業、太普高精密影像、耀億工業、晟田科技工業、伯鑫工具、旭源包裝科技、"
           "花王企業、光隆實業、富堡工業、邦泰複合材料、國統國際、合騏工業、森鉅科技材料等）"
           "約 17 家占最大宗，但同時含殯葬服務（龍巖，屬 S 其他服務業）、保全服務（信實保全，"
           "屬 N 支援服務業）、室內裝修工程（潤德室內裝修設計工程，屬 F）、幼兒教育（大地幼教，"
           "屬 P 教育業或 S）、冷凍倉儲（裕國冷凍冷藏，屬 H）、系統整合（新鼎系統，屬 J）、"
           "投資控股（南良國際、能率亞洲資本）等至少 6 種不同性質。與 TWSE 版本結論一致："
           "本碼為 catch-all，不存在合理單一 A–S 對應，故不填行業軌。"),
    "22": ("C", "中",
           "抽查 TPEx 該碼 98 家（TPEx 樣本數最大的代碼），主體確為製藥／原料藥／生技研發／"
           "醫材製造商（台灣東洋藥品工業、晟德大藥廠、合一生技、台灣浩鼎生技、中裕新藥、"
           "智擎生技製藥、高端疫苗生物製劑、鐿鈦科技等），粗估逾 75 家、占比逾 75%，對應"
           "中類 21 醫藥製造業，落於 C 製造業；惟已知至少三類明確例外：\n"
           "  (a) 零售／通路（應屬 G 批發及零售業）：大樹醫藥股份有限公司（統編 12803476，"
           "藥局零售連鎖）、杏一醫療用品股份有限公司（統編 86649006，醫療用品零售連鎖）、"
           "大學光學科技股份有限公司（統編 84926791，眼鏡零售連鎖——**此統編即 v5 實作規格"
           "第 6 節明訂的防呆對照組，規格要求其行業軌『必須仍為 G 批發及零售業』，本碼若被"
           "當作行業軌來源會直接誤配成 C，驗證了防呆對照組存在的必要性**）、威健股份有限公司"
           "（統編 80158777，醫療器材代理商，性質偏批發）；\n"
           "  (b) 醫療服務連鎖（應屬 Q 醫療保健及社會工作服務業）：馬光保健控股股份有限公司"
           "（統編 28993894，中醫診所連鎖控股）；\n"
           "  (c) 投資控股，無單一實質產業（欣大健康投資控股 54154754、太景醫藥研發控股 "
           "31982484、合富醫療控股 09906168、共信醫藥科技控股 42486336）；\n"
           "  (d) 醫療資訊／AI 服務（較接近 J 出版影音及資通訊業或 M 專業科學技術服務業）："
           "商之器科技（統編 22743619）、長佳智能（統編 50824681）、醫影股份（統編 86700973）。\n"
           "**建議**：維持 C 製造業、信心中（多數仍達 75% 以上，且無其他單一替代解更合理，"
           "改用 G 或 Q 反而會誤配更多真正的製藥廠）；但上列 12 家已知例外請指揮官另行裁示"
           "是否需要 local_exceptions 個案覆蓋（尤其大學光學科技已是規格內定的防呆對照組，"
           "理論上會由稅籍碼或名冊其他層先攔截，不至於落到本層，但若稅籍查無仍會踩到此碼）。"),
    "23": ("D", "中",
           "抽查 TPEx 該碼僅 4 家：欣雄天然氣（統編 07861475）、欣泰石油氣（統編 22000004）、"
           "大園汽電共生（統編 84526079）3 家對應中類 35 電力及燃氣供應業，落於 D，與 TWSE 版本"
           "同類型（瓦斯／汽電共生）；惟北基國際股份有限公司（統編 23218091）業務性質從公司名稱"
           "無法確認——若其主業為石油產品之儲存、裝卸、轉運（物流倉儲）而非燃氣供應本身，"
           "應屬 H 運輸及倉儲業而非 D，但本檔查證範圍僅限官方 API／ISIN 登錄，未查證該公司"
           "實際營業項目，如實標記不確定，不用背景印象猜測；維持中信心（3/4 家明確落 D）。"),
    "29": ("G", "高",
           "抽查 TPEx 該碼 16 家：多數為典型電子零組件代理／經銷商命名型態（茂綸、光菱電子、"
           "巨虹電子、擎亞電子、倍微科技等），另含順發電腦股份有限公司（統編 89743949，3C"
           "消費性電子零售連鎖）——與純代理批發商性質不同，但零售與批發同屬 G 批發及零售業，"
           "不構成跨大類的例外，故信心不降反而更穩固：16 家全數落於 G，無需揭露跨類例外。"),
}


# ---------------------------------------------------------------------------
# 抓取：requests 優先；FortiGuard 疑似阻擋時改用 r.jina.ai 伺服器端渲染代理
# ---------------------------------------------------------------------------


def fetch_bytes(url: str, label: str) -> tuple[bytes, list[str]]:
    notes: list[str] = []
    # 2026-08-21 實測：TPEx openapi 對較大回應（約 1MB）偶發連線中斷（ChunkedEncodingError／
    # IncompleteRead），重試一次即可成功；非阻擋，純網路不穩，故直連本身重試最多 3 次。
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            notes.append(f"[OK] requests：{label}（{len(r.content)} bytes，第 {attempt + 1} 次嘗試）")
            return r.content, notes
        except Exception as e:  # noqa: BLE001
            notes.append(f"[FAIL] requests 第 {attempt + 1} 次嘗試：{label} — {e!r}")
    proxy_url = f"https://r.jina.ai/{url}"
    try:
        r = requests.get(proxy_url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        notes.append(f"[OK] r.jina.ai fallback：{label}（{len(r.content)} bytes）")
        return r.content, notes
    except Exception as e:  # noqa: BLE001
        notes.append(f"[FAIL] r.jina.ai fallback：{label} — {e!r}")
    raise RuntimeError(f"fetch_bytes 兩種手段皆失敗（{label}）：" + " ｜ ".join(notes))


def decode_isin_html(content: bytes) -> str:
    """ISIN 登錄頁宣稱 MS950（實為 cp950／Big5 系列）；直連走此編碼。
    若走 r.jina.ai 代理，內容已是 UTF-8 純文字，cp950 解碼會产生大量 U+FFFD，
    偵測到即改用 utf-8 重解。"""
    text = content.decode("cp950", errors="replace")
    if text.count("�") > len(text) * 0.05:
        text = content.decode("utf-8", errors="replace")
    return text


# ---------------------------------------------------------------------------
# 官方產業別代碼表：以 ISIN 登錄頁「產業別」文字欄位 × 公司代號 交叉比對
# ---------------------------------------------------------------------------


def resolve_industry_code_names(
    market: str,
    api_records: list[dict],
    company_code_field: str,
    industry_code_field: str,
    isin_url: str,
) -> tuple[dict[str, str], list[str]]:
    notes: list[str] = []
    try:
        content, fetch_notes = fetch_bytes(isin_url, f"ISIN 登錄頁（{market}）")
        notes.extend(fetch_notes)
        html = decode_isin_html(content)
    except Exception as e:  # noqa: BLE001
        notes.append(f"[FAIL] ISIN 登錄頁抓取失敗（{market}）：{e!r}——本次無法動態核對代碼表")
        return {}, notes

    # ISIN 登錄頁內含多個區塊（股票／創新板／臺灣存託憑證(TDR)／權證／ETF／ETN／特別股…），
    # 公司代號在「股票」與「創新板」兩區塊皆會出現且產業別文字一致（2026-08-21 實測驗證，
    # 見檔頭說明）；不做區塊篩選，讓後續「以公司代號 join」自然只取用得到的部分，
    # 篩區塊反而會誤刪創新板公司（實測：篩「股票」單一區塊會漏 30 家創新板公司）。
    soup = BeautifulSoup(html, "html.parser")
    code_to_text: dict[str, str] = {}
    section_seen: Counter = Counter()
    current_section = None
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) == 1:
            current_section = tds[0].get_text(strip=True)
            continue
        if len(tds) != 7:
            continue
        name_col = tds[0].get_text(strip=True)
        industry_text = tds[4].get_text(strip=True)
        company_code = name_col.split("　")[0].strip() if "　" in name_col else name_col.strip()
        if company_code:
            code_to_text[company_code] = industry_text
            section_seen[current_section] += 1
    notes.append(f"ISIN 登錄頁（{market}）解析出 {len(code_to_text)} 筆公司代號→產業別文字（跨全部區塊，逐區塊列數：{dict(section_seen)}）")

    pair_texts: dict[str, set] = defaultdict(set)
    pair_counts: Counter = Counter()
    unmatched = 0
    for rec in api_records:
        ccode = str(rec.get(company_code_field, "")).strip()
        icode = str(rec.get(industry_code_field, "")).strip()
        itext = code_to_text.get(ccode)
        if itext is None:
            unmatched += 1
            continue
        pair_texts[icode].add(itext)
        pair_counts[icode] += 1
    notes.append(f"與 {market} 名冊 {len(api_records)} 筆公司以公司代號 join：{unmatched} 筆查無 ISIN 對應列")

    all_codes = {str(rec.get(industry_code_field, "")).strip() for rec in api_records}
    resolved: dict[str, str] = {}
    unresolved: list[tuple[str, list[str]]] = []
    for icode in sorted(all_codes):
        texts = pair_texts.get(icode, set())
        non_empty = {t for t in texts if t}
        if len(non_empty) == 1:
            resolved[icode] = next(iter(non_empty))
        else:
            resolved[icode] = UNCONFIRMED
            unresolved.append((icode, sorted(texts)))
    if unresolved:
        notes.append(f"以下代碼交叉比對不一致或 ISIN 該欄位空白，標記未確認：{unresolved}")
    notes.append(
        f"{market} 代碼表：名冊中出現 {len(all_codes)} 種代碼，"
        f"{len(all_codes) - len(unresolved)} 碼確認、{len(unresolved)} 碼未確認"
    )
    return resolved, notes


# ---------------------------------------------------------------------------
# 名冊 API → master rows
# ---------------------------------------------------------------------------


def fetch_market_records(url: str, label: str) -> tuple[list[dict], list[str]]:
    import json

    content, notes = fetch_bytes(url, label)
    records = json.loads(content.decode("utf-8"))
    notes.append(f"{label} 回傳 {len(records)} 筆")
    return records, notes


def build_master_rows(
    market: str,
    records: list[dict],
    company_code_field: str,
    company_name_field: str,
    industry_code_field: str,
    tax_id_field: str,
    code_names: dict[str, str],
) -> tuple[list[dict], int, int]:
    rows = []
    kept, skipped_no_taxid = 0, 0
    for rec in records:
        raw_tax_id = rec.get(tax_id_field, "")
        tax_id = _taxid.normalize_tax_id(raw_tax_id)
        # "00000000" 是 TWSE 對「無中華民國統一編號」外國公司（多為 TDR 掛牌、產業別代碼 91）
        # 的官方佔位值，不是真統編——2026-08-21 對抗審查發現若不排除，8 家公司會共用同一把
        # join key，比對到 listed_master 時會互相污染。比照「無統編」語意計入跳過統計。
        if not (tax_id.isdigit() and len(tax_id) == 8) or tax_id == "00000000":
            skipped_no_taxid += 1
            continue
        icode = str(rec.get(industry_code_field, "")).strip()
        iname = code_names.get(icode, UNCONFIRMED)
        rows.append({
            "統一編號": tax_id,
            "公司代號": str(rec.get(company_code_field, "")).strip(),
            "公司名稱": str(rec.get(company_name_field, "")).strip(),
            "市場別": market,
            "產業別代碼": icode,
            "產業別名稱": iname,
        })
        kept += 1
    return rows, kept, skipped_no_taxid


def build_map_rows(market: str, code_names: dict[str, str]) -> list[dict]:
    rows = []
    for icode, iname in sorted(code_names.items()):
        if iname == UNCONFIRMED:
            rows.append({
                "市場別": market,
                "產業別代碼": icode,
                "產業別名稱": UNCONFIRMED,
                "行業軌大類代碼": "",
                "行業軌大類名稱": "",
                "映射理由": "未確認：ISIN 登錄查詢系統對此代碼下所有公司的『產業別』文字欄位為空白"
                            "或彼此不一致，官方未提供可信賴的單一名稱，故連代碼名稱本身都無法確認，"
                            "遑論對應行業軌 A–S；不猜測、不用常識填。",
            })
            continue
        if market == "TPEx" and icode in TPEX_CODE_OVERRIDES:
            letter, confidence, reason = TPEX_CODE_OVERRIDES[icode]
            if letter is None:
                rows.append({
                    "市場別": market, "產業別代碼": icode, "產業別名稱": iname,
                    "行業軌大類代碼": "", "行業軌大類名稱": "",
                    "映射理由": f"[信心：{confidence}][TPEx 獨立查核] {reason}",
                })
            else:
                code, label = _section(letter)
                rows.append({
                    "市場別": market, "產業別代碼": icode, "產業別名稱": iname,
                    "行業軌大類代碼": code, "行業軌大類名稱": label,
                    "映射理由": f"[信心：{confidence}][TPEx 獨立查核] {reason}",
                })
            continue
        mapping = MAPPING_REASONS.get(iname)
        if mapping is None:
            rows.append({
                "市場別": market,
                "產業別代碼": icode,
                "產業別名稱": iname,
                "行業軌大類代碼": "",
                "行業軌大類名稱": "",
                "映射理由": f"未確認：代碼名稱已由 ISIN 登錄確認為「{iname}」，"
                            "但本檔映射表（MAPPING_REASONS）未收錄此名稱，"
                            "需人工新增規則後才能對應行業軌，暫不填。",
            })
            continue
        letter, confidence, reason = mapping
        if letter is None:
            rows.append({
                "市場別": market, "產業別代碼": icode, "產業別名稱": iname,
                "行業軌大類代碼": "", "行業軌大類名稱": "",
                "映射理由": f"[信心：{confidence}] {reason}",
            })
        else:
            code, label = _section(letter)
            rows.append({
                "市場別": market, "產業別代碼": icode, "產業別名稱": iname,
                "行業軌大類代碼": code, "行業軌大類名稱": label,
                "映射理由": f"[信心：{confidence}] {reason}",
            })
    return rows


def write_csv(path: str, rows: list[dict], schema: list[str]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=schema)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in schema})


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    all_notes: list[str] = []
    issues: list[str] = []

    # 1) 抓兩份名冊 API
    twse_records, n1 = fetch_market_records(TWSE_LISTED_API, "TWSE t187ap03_L")
    all_notes.extend(n1)
    tpex_records, n2 = fetch_market_records(TPEX_LISTED_API, "TPEx mopsfin_t187ap03_O")
    all_notes.extend(n2)

    # 2) 動態核對代碼表；失敗才落回凍結快照
    twse_codes, n3 = resolve_industry_code_names(
        "TWSE", twse_records, "公司代號", "產業別", ISIN_TWSE_URL,
    )
    all_notes.extend(n3)
    if not twse_codes:
        twse_codes = dict(FALLBACK_TWSE_CODE_NAMES)
        issues.append("TWSE 代碼表動態核對失敗，已落回 2026-08-21 凍結快照 FALLBACK_TWSE_CODE_NAMES")

    tpex_codes, n4 = resolve_industry_code_names(
        "TPEx", tpex_records, "SecuritiesCompanyCode", "SecuritiesIndustryCode", ISIN_TPEX_URL,
    )
    all_notes.extend(n4)
    if not tpex_codes:
        tpex_codes = dict(FALLBACK_TPEX_CODE_NAMES)
        issues.append("TPEx 代碼表動態核對失敗，已落回 2026-08-21 凍結快照 FALLBACK_TPEX_CODE_NAMES")

    # 3) master rows
    twse_rows, twse_kept, twse_skipped = build_master_rows(
        "TWSE", twse_records, "公司代號", "公司名稱", "產業別", "營利事業統一編號", twse_codes,
    )
    tpex_rows, tpex_kept, tpex_skipped = build_master_rows(
        "TPEx", tpex_records, "SecuritiesCompanyCode", "CompanyName",
        "SecuritiesIndustryCode", "UnifiedBusinessNo.", tpex_codes,
    )
    all_notes.append(f"listed_master：TWSE 帶統編 {twse_kept} 筆／跳過無統編 {twse_skipped} 筆")
    all_notes.append(f"listed_master：TPEx 帶統編 {tpex_kept} 筆／跳過無統編 {tpex_skipped} 筆")

    master_path = os.path.join(_paths.HERE, "..", "data", "listed_master.csv")
    master_path = os.path.normpath(master_path)
    write_csv(master_path, twse_rows + tpex_rows, MASTER_SCHEMA)
    all_notes.append(f"[written] {master_path}（共 {len(twse_rows) + len(tpex_rows)} 列）")

    # 4) industry map rows
    map_rows = build_map_rows("TWSE", twse_codes) + build_map_rows("TPEx", tpex_codes)
    unconfirmed_count = sum(1 for r in map_rows if r["行業軌大類代碼"] == "")
    all_notes.append(f"listed_industry_map：共 {len(map_rows)} 列，其中 {unconfirmed_count} 列無法/尚未對應行業軌大類")

    map_path = os.path.join(_paths.HERE, "..", "data", "listed_industry_map.csv")
    map_path = os.path.normpath(map_path)
    write_csv(map_path, map_rows, MAP_SCHEMA)
    all_notes.append(f"[written] {map_path}")

    print("\n".join(f"- {n}" for n in all_notes))
    if issues:
        print("\n## 需人工注意")
        print("\n".join(f"- {i}" for i in issues))

    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
