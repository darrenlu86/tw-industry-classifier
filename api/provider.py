# -*- coding: utf-8 -*-
r"""API Provider — 打單筆查詢 API，免下載 320 MB 全檔

實測可用的單筆端點（皆免金鑰、免 Referer、免 IP 白名單，2026-07-30 驗證）
──────────────────────────────────────────────────────────────────────────
  稅籍（主力，唯一同時給名稱與行業標準分類碼）
      GET https://eip.fia.gov.tw/OAI/api/businessRegistration/{ban}
      → businessNm、businessType、industryCd／industryNm（主碼）、industryCd1~3（副碼）
      查無＝HTTP 404
  非營利事業機關團體（社團／財團／協會／基金會的扣繳單位）
      GET https://eip.fia.gov.tw/OAI/api/nonBusinessUnit/{ban}   → unitNm
  各級學校
      GET https://eip.fia.gov.tw/OAI/api/schoolBanData/{ban}     → unitNm
  GCIS 公司登記（稅籍查無時補名稱，例如已解散公司、外商在台）
      應用一 5F64D864 → Company_Name、Company_Status_Desc
      查無＝HTTP 200 但空 body（與稅籍的 404 語意不同，要分開處理）
      （v4 起引擎不再查所營事業——原 L3-9 用的應用三 236EE382 已退場）

仍須隨包附的小檔（政府機關唯一來源，無任何單筆 API）
──────────────────────────────────────────────────────────────────────────
  gov_central.csv        42 KB   行政院所屬機關（data.gov.tw #44806）
  gov_local.csv          94 KB   地方政府機關（#166161）
  authority_master.csv  446 KB   金管會權威名冊（非公開檔，見 crawlers/）
  合計約 580 KB —— 相較本地端模式的 320 MB，縮減 99.8%。
  （v5 選用）listed_master.csv＋listed_industry_map.csv —— L2-5 上市櫃名冊，
  缺檔即跳層，見 crawlers/fetch_listed.py。

實測依據：財政部 OAS（eip.fia.gov.tw/OAI/v2/api-docs）24 個 path 全數檢視，
無任何政府機關端點；財政部 03732303、勞保局 03769808 在 businessRegistration
與 nonBusinessUnit 皆回 404，GCIS 統編查類型也三項全 N。

正確性守則
──────────────────────────────────────────────────────────────────────────
網路錯誤**不會**被當成「查無」。若重試後仍失敗，預設直接拋出 ApiUnavailable，
讓整批停下來，而不是讓某幾筆因為當下網路狀況被判成別的分類。
要容忍降級請明確傳 strict=False（CLI 對應 classify.py --tolerate-api-error），
屆時該筆會標記 confidence=low 並列入 self.degraded。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "core"))

from provider_base import ProviderBase  # noqa: E402

FIA = "https://eip.fia.gov.tw/OAI/api"
GCIS = "https://data.gcis.nat.gov.tw/od/data/api"
GCIS_COMPANY = GCIS + "/5F64D864-61CB-4D0D-8AD9-492047CC1EA6"
UA = {"User-Agent": "Mozilla/5.0 (industry_classifier/1.0)", "Accept": "application/json"}

MISS = object()          # 明確的「查無此統編」標記，與 None（尚未查）區分


class ApiUnavailable(RuntimeError):
    """外部 API 重試後仍失敗。刻意讓它中斷，避免把網路問題誤判成查無。"""


class ApiProvider(ProviderBase):
    mode_name = "API 單筆查詢"

    def __init__(self, data_dir, offline=False, strict=True,
                 cache_path=None, sleep=0.15, retries=3, timeout=25):
        super().__init__(data_dir)
        if offline:
            raise ValueError("API 模式無法離線執行；請改用 --mode local")
        self.strict = strict
        self.sleep = sleep
        self.retries = retries
        self.timeout = timeout
        self.calls = 0
        self.degraded = []                     # 記錄降級（strict=False 時）的統編
        self._cache_path = cache_path or os.path.join(data_dir, "_api_cache.json")
        self._cache = (json.load(open(self._cache_path, encoding="utf-8"))
                       if os.path.exists(self._cache_path) else {})
        self.data_version = "API 即時查詢（%s）" % time.strftime("%Y-%m-%d")

    # ── 只載入沒有單筆 API 的名冊 ────────────────────────────────────────
    def load_registries(self, require_authority=True):
        self._authority = self._load_authority("authority_master.csv", require_authority)
        self._gov = {}
        for fn in ("gov_central.csv", "gov_local.csv"):
            self._gov.update(self._load_pairs(fn, required=True))
        self._listed = self._load_listed()             # L2-5：兩檔皆為小檔，缺檔即跳層
        return self

    def registry_summary(self):
        return {"authority_master.csv": len(self._authority),
                "機關名冊（central＋local）": len(self._gov),
                "上市櫃名冊": len(self._listed) or "（未提供，L2-5 跳過）",
                "學校／非營利": "改由單筆 API 查詢"}

    # ── HTTP ─────────────────────────────────────────────────────────────
    def _get(self, url, expect_json=True):
        """回傳 (payload, 狀態)。狀態為 'ok'／'miss'／'error'。"""
        last = ""
        for attempt in range(1, self.retries + 1):
            try:
                self.calls += 1
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read().decode("utf-8", "replace")
                if self.sleep:
                    time.sleep(self.sleep)
                if not raw.strip():                       # GCIS 查無：200 + 空 body
                    return None, "miss"
                if raw.lstrip().startswith("非授權"):       # GCIS IP 未授權會回純文字
                    return None, "error"
                return (json.loads(raw) if expect_json else raw), "ok"
            except urllib.error.HTTPError as e:
                if e.code == 404:                          # 稅籍查無
                    return None, "miss"
                last = "HTTP %s" % e.code
            except Exception as e:                         # noqa: BLE001
                last = "%s: %s" % (type(e).__name__, e)
            if attempt < self.retries:
                time.sleep(min(2 ** attempt, 8))
        return last, "error"

    def _cached(self, key, fetch):
        """快取包裝。'error' 不寫入快取，以免把暫時性失敗變成永久結果。"""
        if key in self._cache:
            v = self._cache[key]
            return MISS if v == "__MISS__" else v
        payload, state = fetch()
        if state == "error":
            if self.strict:
                raise ApiUnavailable(
                    "查詢 %s 失敗（%s）。已重試 %d 次仍不通。\n"
                    "外部 API 不通時本工具刻意中斷，避免把網路問題誤判成「查無此統編」。\n"
                    "要容忍降級請加 --tolerate-api-error，或改用 --mode local。"
                    % (key, payload, self.retries))
            tid = key.split(":", 1)[-1]          # 對外只留統編（去端點前綴、去重）
            if tid not in self.degraded:
                self.degraded.append(tid)
            return MISS
        value = "__MISS__" if state == "miss" else payload
        self._cache[key] = value
        return MISS if state == "miss" else payload

    # ── 引擎介面 ─────────────────────────────────────────────────────────
    def tax(self, tax_id):
        rec = self._cached("tax:" + tax_id,
                           lambda: self._get("%s/businessRegistration/%s" % (FIA, tax_id)))
        if rec is MISS or not rec:
            return None
        codes = []
        for key_cd, key_nm in (("industryCd", "industryNm"), ("industryCd1", "industryNm1"),
                               ("industryCd2", "industryNm2"), ("industryCd3", "industryNm3")):
            cd = (rec.get(key_cd) or "").strip()
            if cd:
                codes.append((cd, (rec.get(key_nm) or "").strip()))
        return {"name": (rec.get("businessNm") or "").strip(),
                "org": (rec.get("businessType") or "").strip(),
                "codes": codes}

    def school(self, tax_id):
        rec = self._cached("school:" + tax_id,
                           lambda: self._get("%s/schoolBanData/%s" % (FIA, tax_id)))
        if rec is MISS or not rec:
            return None
        return (rec.get("unitNm") or "").strip() or None

    def nonprofit(self, tax_id):
        rec = self._cached("nonprofit:" + tax_id,
                           lambda: self._get("%s/nonBusinessUnit/%s" % (FIA, tax_id)))
        if rec is MISS or not rec:
            return None
        return (rec.get("unitNm") or "").strip() or None

    def company(self, tax_id):
        """GCIS 公司登記：稅籍查無時補名稱與登記狀態（已解散、外商在台等）。"""
        url = GCIS_COMPANY + "?$format=json&$filter=" + urllib.parse.quote(
            "Business_Accounting_NO eq " + tax_id)
        rec = self._cached("company:" + tax_id, lambda: self._get(url))
        if rec is MISS or not rec:
            return None
        row = rec[0] if isinstance(rec, list) else rec
        return {"name": (row.get("Company_Name") or "").strip(),
                "status": (row.get("Company_Status_Desc") or "").strip()}

    def save_gcis_cache(self):
        """存快取（沿用 local provider 的方法名，供 CLI 統一呼叫）。"""
        with open(self._cache_path, "w", encoding="utf-8") as f:
            json.dump(self._cache, f, ensure_ascii=False, indent=1)
