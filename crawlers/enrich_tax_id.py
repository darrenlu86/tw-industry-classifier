# -*- coding: utf-8 -*-
r"""
enrich_tax_id.py — 統編補全模組（交付版，供 build_authority_master 匯入使用）

＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝
★★ 警語：請勿直接執行本檔的 main() ★★

    本檔的 main() 會用三張凍結列源名冊「從零」重建 tax_id_lookup.csv 並整檔覆寫。
    現行的 tax_id_lookup.csv 裡有 `query_pending_taxids.py` 透過 GCIS 查詢、以及人工
    確認後寫回的解析成果（那些統編在三張名冊裡是留白的）——一旦重建，這些成果會被
    整批清空，且無法從名冊還原。**這是資料破壞，不是重跑。**

    正常使用方式只有一種：由 `build_authority_master.py` 匯入本檔、呼叫
    `load_lookup()`（只讀既有快取，檔案不存在時才建置一次）。

    真的需要重建（例如快取檔遺失、或三張名冊剛做過大幅更新）才執行本檔，
    並須明確帶上旗標：

        python enrich_tax_id.py --i-understand-this-overwrites-lookup

    不帶旗標會印出本警語並以 exit code 1 結束。
＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝

本檔為交付版：萃取邏輯、collision 解法、輸出欄位與排序與產線版逐字相同，僅把路徑改走
`_paths` 模組、把對原產線 etl 的跨目錄 import 換成本地副本 `_taxid`，因此可從任何目錄
直接匯入或執行，不依賴 原產線的目錄層數。

職責：
    1. 從三張既有「純外部事實」列源名冊萃取「官方名稱 → tax_id + tax_id_source」快取，
       寫成 crawlers/masters/tax_id_lookup.csv（**事實承接，不重查**）。
    2. 把查無統編的名稱另存 crawlers/masters/tax_id_pending.csv，留待日後補查。
    3. 提供查詢介面 lookup_tax_id()（讀快取）與 query_external()（外網查詢佔位，
       本模組**不打外網**；外網補查由 query_pending_taxids.py 負責）。

列源名冊（凍結，只讀）：
    crawlers/masters/financial_institutions_master.csv  490 列（金融機構）
    crawlers/masters/insurance_brokers_master.csv       542 列（保經代）
    crawlers/masters/farmers_credit_master.csv          311 列（農漁會）

已知覆蓋率限制（如實揭露）：
    三張名冊自身的 tax_id 欄本來就有留白（主要是保經代與少數非公司登記機構），
    本模組**只承接名冊裡既有的事實、不猜也不補**。留白者一律進 tax_id_pending.csv，
    要補統編得另跑 query_pending_taxids.py（打 GCIS）或人工查證後寫回快取。

設計原則：
    - 輸出 deterministic：依 official_name 排序寫出，同輸入同輸出。
    - 含中文一律以 utf-8-sig 讀寫。
"""

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths

from _taxid import normalize_tax_id

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ── 路徑 ─────────────────────────────────────────────────────
LOOKUP_CSV = _paths.TAX_ID_LOOKUP
PENDING_CSV = _paths.TAX_ID_PENDING

# 三張列源名冊（依固定順序處理，確保 collision 解法 deterministic）。
# 檔名同時是 lookup／pending 的 source_master 欄值，下游（query_pending_taxids）會依此篩選，
# 故沿用原檔名字串當邏輯鍵，實際位置查 MASTER_PATHS。
SOURCE_MASTERS = [
    "financial_institutions_master.csv",
    "insurance_brokers_master.csv",
    "farmers_credit_master.csv",
]

MASTER_PATHS = {
    "financial_institutions_master.csv": _paths.FIM,
    "insurance_brokers_master.csv": _paths.BROKERS,
    "farmers_credit_master.csv": _paths.FARMERS,
}

# 外網查詢端點（僅供文件與日後實作參考；本模組不呼叫）
EXTERNAL_ENDPOINTS = {
    "TWSE-t187ap03": "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",  # 上市公司基本資料（含統編）
    "GCIS": "https://data.gcis.nat.gov.tw/od/data/api/...",                  # 經濟部商工登記公示
}

OVERWRITE_FLAG = "--i-understand-this-overwrites-lookup"


def _read_master(name):
    """讀單一列源名冊（utf-8-sig）→ list[dict]。"""
    path = MASTER_PATHS[name]
    if not os.path.exists(path):
        raise FileNotFoundError(f"列源名冊不存在: {path}")
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def build_lookup():
    """從三張名冊建「官方名稱 → tax_id + tax_id_source」快取。

    回傳 (lookup, pending, collisions)：
        lookup     dict[official_name] = {tax_id, tax_id_source, source_master}
        pending    list[dict]（tax_id 為空、待日後補查的名稱）
        collisions list[dict]（同名但不同 tax_id，保留首見、記錄衝突）
    """
    lookup = {}
    pending = []
    collisions = []

    for master_name in SOURCE_MASTERS:
        for row in _read_master(master_name):
            name = (row.get("official_name") or "").strip()
            if not name:
                continue
            tax_id = normalize_tax_id(row.get("tax_id"))
            tax_id_source = (row.get("tax_id_source") or "").strip()
            segment = (row.get("segment") or "").strip()

            if not tax_id:
                pending.append({
                    "official_name": name,
                    "segment": segment,
                    "source_master": master_name,
                    "reason": "來源主檔統編留白（待 TWSE OpenAPI / GCIS 補查）",
                })
                continue

            if name in lookup:
                if lookup[name]["tax_id"] != tax_id:
                    collisions.append({
                        "official_name": name,
                        "kept_tax_id": lookup[name]["tax_id"],
                        "kept_source": lookup[name]["source_master"],
                        "dropped_tax_id": tax_id,
                        "dropped_source": master_name,
                    })
                # 同名同統編：略過（保留首見）
                continue

            lookup[name] = {
                "tax_id": tax_id,
                "tax_id_source": tax_id_source,
                "source_master": master_name,
            }

    return lookup, pending, collisions


def write_lookup(lookup, pending):
    """把快取與待查名單 deterministic 寫出（依 official_name 排序，utf-8-sig）。

    ★ 整檔覆寫。呼叫前請先確認不會蓋掉 query_pending_taxids.py 的解析成果。
    """
    os.makedirs(os.path.dirname(LOOKUP_CSV), exist_ok=True)
    with open(LOOKUP_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["official_name", "tax_id", "tax_id_source", "source_master"])
        for name in sorted(lookup):
            e = lookup[name]
            w.writerow([name, e["tax_id"], e["tax_id_source"], e["source_master"]])

    with open(PENDING_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["official_name", "segment", "source_master", "reason"])
        for r in sorted(pending, key=lambda r: (r["source_master"], r["segment"], r["official_name"])):
            w.writerow([r["official_name"], r["segment"], r["source_master"], r["reason"]])


def load_lookup():
    """讀 tax_id_lookup.csv → dict[official_name] = {tax_id, tax_id_source, source_master}。

    快取不存在 → 先 build 一次再讀（讓 build_authority_master 可獨立跑）。
    快取已存在 → **只讀不寫**，不會覆蓋既有解析成果。
    """
    if not os.path.exists(LOOKUP_CSV):
        lookup, pending, _ = build_lookup()
        write_lookup(lookup, pending)
    out = {}
    with open(LOOKUP_CSV, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            out[row["official_name"]] = {
                "tax_id": row["tax_id"],
                "tax_id_source": row["tax_id_source"],
                "source_master": row["source_master"],
            }
    return out


def lookup_tax_id(name, cache=None):
    """以官方名稱查快取 → (tax_id, tax_id_source)。查無回 ("", "")。"""
    if cache is None:
        cache = load_lookup()
    e = cache.get((name or "").strip())
    if not e:
        return "", ""
    return e["tax_id"], e["tax_id_source"]


def query_external(name, allow_network=False):
    """外網統編查詢介面（規劃：TWSE OpenAPI t187ap03 → GCIS 商工登記）。

    本模組**不打外網**（外網補查請走 query_pending_taxids.py）：
        - allow_network=False（預設）→ 回 ("", "", "offline-stub") 不連線。
        - allow_network=True         → raise NotImplementedError（尚未實作，避免誤觸外網）。
    """
    if not allow_network:
        return "", "", "offline-stub（本模組不打外網；外網補查請走 query_pending_taxids.py）"
    raise NotImplementedError(
        "本模組未實作外網統編查詢（只建快取與介面）；"
        f"外網補查請執行 query_pending_taxids.py。日後端點：{EXTERNAL_ENDPOINTS}"
    )


def _print_overwrite_warning():
    print("=" * 74)
    print("已中止：直接執行本檔會整檔重建並覆寫 tax_id_lookup.csv。")
    print("=" * 74)
    print(f"  目標檔：{LOOKUP_CSV}")
    print(f"          {PENDING_CSV}")
    print("")
    print("  重建是用三張凍結列源名冊「從零」產生快取。現行快取內含 query_pending_taxids.py")
    print("  透過 GCIS 查詢、以及人工確認後寫回的統編（那些統編在名冊裡是留白的），")
    print("  重建會把這些成果整批清空且無法從名冊還原。")
    print("")
    print("  一般用途不需要執行本檔——build_authority_master.py 會匯入本檔並呼叫")
    print("  load_lookup()（只讀既有快取）。")
    print("")
    print("  確定要重建（快取檔遺失、或三張名冊剛做過大幅更新）才加旗標：")
    print(f"      python enrich_tax_id.py {OVERWRITE_FLAG}")
    print("=" * 74)


def parse_args():
    p = argparse.ArgumentParser(
        description="重建統編快取（危險操作：會整檔覆寫 tax_id_lookup.csv / tax_id_pending.csv）")
    p.add_argument(OVERWRITE_FLAG, dest="confirm_overwrite", action="store_true",
                    help="確認理解本操作會覆寫既有快取（含 GCIS／人工解析成果）；不帶此旗標不執行")
    return p.parse_args()


def main():
    args = parse_args()
    if not args.confirm_overwrite:
        _print_overwrite_warning()
        sys.exit(1)

    lookup, pending, collisions = build_lookup()
    write_lookup(lookup, pending)
    print("[enrich_tax_id] 快取重建完成（既有快取已被覆寫）")
    print(f"  列源名冊       : {', '.join(SOURCE_MASTERS)}")
    print(f"  已解析名稱     : {len(lookup)} 筆 -> {LOOKUP_CSV}")
    print(f"  待補查名稱     : {len(pending)} 筆 -> {PENDING_CSV}")
    if collisions:
        print(f"  同名不同統編   : {len(collisions)} 筆（保留首見）")
        for c in collisions[:10]:
            print(f"    - {c['official_name']}: 保留 {c['kept_tax_id']}({c['kept_source']}) "
                  f"／丟棄 {c['dropped_tax_id']}({c['dropped_source']})")
    else:
        print("  同名不同統編   : 0 筆")
    print("  提醒：若原快取含 GCIS／人工解析成果，請重跑 query_pending_taxids.py 補回。")


if __name__ == "__main__":
    main()
