# -*- coding: utf-8 -*-
r"""Provider 介面定義與共用的名冊載入工具

Provider 是引擎與資料源之間的唯一介面。實作有兩個：
    local/provider.py  LocalProvider  — 讀已下載的全檔（可完全離線）
    api/provider.py    ApiProvider    — 打單筆查詢 API（免下載 63MB 稅籍檔）

只要兩個實作都遵守同一介面，引擎邏輯就只有一份，本地端與 API 端不會判出不同結果。
"""
import csv
import os
import sys
from collections import defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def normalize_tax_id(raw):
    """統編正規化：去空白／引號／全形空白，純數字不足 8 碼補前導零。

    來源系統常見兩種損壞：Excel 把統編當數字讀掉前導零、以及 16 碼折半重複。
    此處只處理前導零；折半損壞需在資料清理階段處理（見 docs/資料來源與更新.md）。
    """
    t = (raw or "").strip().strip('"').replace("　", "").replace(" ", "")
    if t.isdigit() and 0 < len(t) < 8:
        t = t.zfill(8)
    return t


class ProviderBase:
    """所有 provider 的共同父類。子類必須實作 tax()。

    authority／gov／school／nonprofit 四個名冊都是小檔（合計約 13 MB），
    兩種模式都直接載入記憶體，故實作放在此父類共用。
    """

    mode_name = "base"
    data_version = ""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self._authority = defaultdict(list)
        self._gov = {}
        self._school = {}
        self._nonprofit = {}
        self._listed = {}
        self._missing = []

    # ── 名冊載入 ─────────────────────────────────────────────────────────
    def load_registries(self, require_authority=True):
        """載入四個名冊。缺檔記入 self._missing，由呼叫端決定是否致命。"""
        self._authority = self._load_authority("authority_master.csv", require_authority)
        self._gov = {}
        for fn in ("gov_central.csv", "gov_local.csv"):
            self._gov.update(self._load_pairs(fn, required=False))
        self._school = self._load_pairs("BGMOPEN99X.csv", required=False)
        self._nonprofit = self._load_pairs("BGMOPEN99.csv", required=False)
        self._listed = self._load_listed()
        return self

    def _path(self, filename):
        return os.path.join(self.data_dir, filename)

    def _load_authority(self, filename, required):
        p = self._path(filename)
        out = defaultdict(list)
        if not os.path.exists(p):
            self._missing.append((filename, "金管會權威名冊（金融四類分類的事實來源）"))
            if required:
                raise FileNotFoundError(
                    "缺少 %s。此檔非公開下載，請執行 crawlers/build_authority_master.py "
                    "或向提供者索取最新快照。" % p)
            return out
        with open(p, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                tid = normalize_tax_id(row.get("tax_id", ""))
                if tid:
                    out[tid].append(row)
        return out

    def _load_pairs(self, filename, required=True, tax_col=0, name_col=1):
        """讀「第一欄統編、第二欄名稱」格式的名冊 CSV。"""
        p = self._path(filename)
        out = {}
        if not os.path.exists(p):
            self._missing.append((filename, "名冊"))
            if required:
                raise FileNotFoundError("缺少 %s，請先執行 core/fetch_bulk_data.py" % p)
            return out
        with open(p, encoding="utf-8-sig", errors="replace", newline="") as f:
            reader = csv.reader(f)
            next(reader, None)                       # 表頭
            for row in reader:
                if len(row) > max(tax_col, name_col):
                    tid = normalize_tax_id(row[tax_col])
                    if tid:
                        out.setdefault(tid, row[name_col].strip())
        return out

    def _load_listed(self):
        """L2-5 上市櫃名冊：兩個檔在載入時就 join 好，引擎拿到的是一筆完整記錄。

        缺任一檔＝這一層不存在（回空 dict），引擎會直接跳過——名冊由
        crawlers/fetch_listed.py 產生，不是每個使用者都會跑。
        欄位契約見 docs/資料來源與更新.md，兩檔皆 UTF-8-SIG。
        """
        master = self._path("listed_master.csv")
        mapping = self._path("listed_industry_map.csv")
        if not (os.path.exists(master) and os.path.exists(mapping)):
            return {}
        sections = {}
        with open(mapping, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                sections[(row.get("市場別", "").strip(), row.get("產業別代碼", "").strip())] = (
                    row.get("行業軌大類代碼", "").strip(), row.get("行業軌大類名稱", "").strip())
        out = {}
        with open(master, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                tid = normalize_tax_id(row.get("統一編號", ""))
                if not tid:
                    continue
                market = row.get("市場別", "").strip()
                ind_code = row.get("產業別代碼", "").strip()
                # 對不到 A–S 大類的仍然收進來，section 留空——名冊本身已證實
                # 上市櫃身分，只是官方沒給得出對應的行業歸屬（其他業／綠能環保等）。
                # 由引擎決定怎麼標，這裡不替它決定「寧缺勿錯」而整列丟掉。
                sec = sections.get((market, ind_code)) or ("", "")
                out.setdefault(tid, {
                    "market": market, "code": row.get("公司代號", "").strip(),
                    "name": row.get("公司名稱", "").strip(),
                    "ind_code": ind_code, "ind_name": row.get("產業別名稱", "").strip(),
                    "section": ("%s %s" % sec) if sec[1] else "",
                })
        return out

    # ── 引擎呼叫的介面 ───────────────────────────────────────────────────
    def authority(self, tax_id):
        return self._authority.get(tax_id, [])

    def gov(self, tax_id):
        return self._gov.get(tax_id)

    def school(self, tax_id):
        return self._school.get(tax_id)

    def nonprofit(self, tax_id):
        return self._nonprofit.get(tax_id)

    def listed(self, tax_id):
        """L2-5 上市櫃名冊：回 {"market","code","name","ind_code","ind_name","section"} 或 None。"""
        return self._listed.get(tax_id)

    def tax(self, tax_id):
        raise NotImplementedError

    def company(self, tax_id):
        """GCIS 公司登記：稅籍查無時補名稱與登記狀態。回 {"name","status"} 或 None。

        預設回 None（例如離線時），子類可覆寫。
        """
        return None

    # ── 診斷 ─────────────────────────────────────────────────────────────
    @property
    def missing_files(self):
        return list(self._missing)

    def registry_summary(self):
        return {
            "authority_master.csv": len(self._authority),
            "機關名冊（central＋local）": len(self._gov),
            "學校名冊": len(self._school),
            "非營利名冊": len(self._nonprofit),
            "上市櫃名冊": len(self._listed) or "（未提供，L2-5 跳過）",
        }
