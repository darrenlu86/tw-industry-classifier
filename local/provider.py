# -*- coding: utf-8 -*-
r"""本地端 Provider — 讀已下載的全檔，可完全離線執行

稅籍全檔（BGMOPEN1.zip，63 MB、171 萬列）有三種讀法，依情境自動選：

  1. sqlite 索引（最快，建議）
     先跑 core/build_tax_index.py 建 data/tax_index.sqlite，之後單筆查詢即時回應。
  2. 批次預載（跑名單時最快）
     preload(統編集合) 掃一次 zip，只留名單內的統編。171 萬列約 20–40 秒。
  3. 逐筆掃描（沒索引又只查一筆時的退路）
     掃 zip 並在命中後提早結束，平均約 10–20 秒。結果會快取在記憶體。

GCIS 在本地端模式僅剩「名稱解析」一種用途（company()，補稅籍查無者的名稱），
仍需連外；--offline 時該筆改用備用名稱並標記。
（v4 起引擎不再查 GCIS 所營事業——原 L3-9 層已移除。）
"""
import csv
import io
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "core"))

from provider_base import ProviderBase, normalize_tax_id  # noqa: E402

TAX_ZIP = "BGMOPEN1.zip"
TAX_INDEX = "tax_index.sqlite"
GCIS_COMPANY = "https://data.gcis.nat.gov.tw/od/data/api/5F64D864-61CB-4D0D-8AD9-492047CC1EA6"
UA = {"User-Agent": "Mozilla/5.0 (industry_classifier/1.0)"}

# BGMOPEN1 欄位位置（表頭：營業地址,統一編號,總機構統一編號,營業人名稱,資本額,
#                     設立日期,組織別名稱,使用統一發票,行業代號,行業名稱,...）
COL_TAX_ID, COL_NAME, COL_ORG = 1, 3, 6
CODE_PAIRS = (8, 10, 12, 14)          # 1 主 3 副：(代號, 名稱) 成對出現


def parse_tax_row(row):
    """把稅籍一列轉成引擎要的 dict。"""
    codes = [(row[i].strip(), row[i + 1].strip() if len(row) > i + 1 else "")
             for i in CODE_PAIRS if len(row) > i and row[i].strip()]
    return {"name": row[COL_NAME].strip(), "org": row[COL_ORG].strip(), "codes": codes}


class LocalProvider(ProviderBase):
    mode_name = "本地端全檔"

    def __init__(self, data_dir, offline=False, gcis_cache_path=None):
        super().__init__(data_dir)
        self.offline = offline
        self._tax_cache = {}
        self._preloaded = False
        self._scanned_all = False
        self._conn = None
        self._gcis_cache_path = gcis_cache_path or os.path.join(data_dir, "_gcis_cache.json")
        self._gcis = (json.load(open(self._gcis_cache_path, encoding="utf-8"))
                      if os.path.exists(self._gcis_cache_path) else {})
        self.data_version = self._describe_versions()
        idx = self._path(TAX_INDEX)
        if os.path.exists(idx):
            self._conn = sqlite3.connect("file:%s?mode=ro" % idx.replace("\\", "/"), uri=True)

    # ── 資料版本（寫進每筆輸出，供稽核）────────────────────────────────
    def _describe_versions(self):
        parts = []
        for fn, label in ((TAX_ZIP, "稅籍"), ("BGMOPEN99.csv", "非營利"),
                          ("BGMOPEN99X.csv", "學校"), ("gov_central.csv", "機關中央"),
                          ("gov_local.csv", "機關地方"), ("authority_master.csv", "金管會")):
            p = self._path(fn)
            if os.path.exists(p):
                parts.append("%s=%s" % (label, time.strftime("%Y-%m-%d",
                                                             time.localtime(os.path.getmtime(p)))))
        return " ".join(parts)

    # ── 稅籍查詢 ─────────────────────────────────────────────────────────
    @property
    def wants_preload(self):
        """有 sqlite 索引時不需要預載——索引查詢已是毫秒級，掃全檔純屬浪費。"""
        return self._conn is None

    def preload(self, tax_ids):
        """批次模式：掃一次 zip，只留名單內的統編。"""
        if self._conn is not None:
            return self
        want = {normalize_tax_id(t) for t in tax_ids if normalize_tax_id(t)}
        if not want:
            return self
        found = {}
        for row in self._iter_tax_rows():
            tid = normalize_tax_id(row[COL_TAX_ID])
            if tid in want and tid not in found:
                found[tid] = parse_tax_row(row)
                if len(found) == len(want):
                    break
        self._tax_cache.update(found)
        self._preloaded = True
        return self

    def _iter_tax_rows(self):
        p = self._path(TAX_ZIP)
        if not os.path.exists(p):
            raise FileNotFoundError("缺少 %s，請先執行 core/fetch_bulk_data.py" % p)
        z = zipfile.ZipFile(p)
        with z.open(z.namelist()[0]) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig",
                                                 errors="replace", newline=""))
            next(reader, None)
            for row in reader:
                if len(row) > COL_ORG:
                    yield row

    def tax(self, tax_id):
        if tax_id in self._tax_cache:
            return self._tax_cache[tax_id]
        if self._conn is not None:                    # 路徑 1：sqlite 索引
            cur = self._conn.execute(
                "SELECT name, org, codes FROM tax WHERE tax_id = ?", (tax_id,))
            row = cur.fetchone()
            rec = None
            if row:
                rec = {"name": row[0], "org": row[1],
                       "codes": [tuple(c) for c in json.loads(row[2])]}
            self._tax_cache[tax_id] = rec
            return rec
        if self._preloaded or self._scanned_all:      # 批次已掃過 → 沒有就是真沒有
            self._tax_cache[tax_id] = None
            return None
        for row in self._iter_tax_rows():             # 路徑 3：逐筆掃描（提早結束）
            if normalize_tax_id(row[COL_TAX_ID]) == tax_id:
                rec = parse_tax_row(row)
                self._tax_cache[tax_id] = rec
                return rec
        self._tax_cache[tax_id] = None
        return None

    def company(self, tax_id):
        """GCIS 公司登記：補「稅籍查無」者的名稱（已解散、廢止、外商在台）。

        與 API 模式走同一個端點與同一個順序，兩種模式才會判出相同結果。
        offline 時回 None，該筆改用備用名稱並在輸出標記。
        """
        key = "company:" + tax_id
        if key in self._gcis:
            rec = self._gcis[key]
            return rec if rec and "error" not in rec else None
        if self.offline:
            return None
        url = GCIS_COMPANY + "?$format=json&$filter=" + urllib.parse.quote(
            "Business_Accounting_NO eq " + tax_id)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
                raw = r.read().decode("utf-8", "replace")
            data = json.loads(raw) if raw.strip() and not raw.startswith("非授權") else []
            rec = ({"name": (data[0].get("Company_Name") or "").strip(),
                    "status": (data[0].get("Company_Status_Desc") or "").strip()}
                   if data else {})
            self._gcis[key] = rec
            time.sleep(0.3)
            return rec or None
        except Exception as e:                        # noqa: BLE001
            self._gcis[key] = {"error": "%s: %s" % (type(e).__name__, e)}
            return None

    def save_gcis_cache(self):
        with open(self._gcis_cache_path, "w", encoding="utf-8") as f:
            json.dump(self._gcis, f, ensure_ascii=False, indent=1)

    def close(self):
        if self._conn is not None:
            self._conn.close()
