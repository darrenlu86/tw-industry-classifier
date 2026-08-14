# -*- coding: utf-8 -*-
r"""爬蟲產線的路徑定義（唯一來源）

原始腳本用 `Path(__file__).parents[3]` 之類的相對層數推路徑，
搬到任何其他目錄深度就會指錯。本檔改成一律以本檔位置為錨點，搬家不會斷。

目錄配置
    crawlers/
    ├── masters/    三張列源名冊（凍結、只讀）＋ tax_id_lookup／pending
    ├── baseline/   23 份既有名冊快照，各 fetcher 的 diff 比對基準
    ├── raw/<日期>/ 每次抓取的原始快照與異動報告（fetcher 輸出）
    └── build/      builder 產出（authority_master.csv 與報告）

分類器讀的資料層（data/）不在這裡定義：爬蟲本身不寫這裡（builder 輸出到 BUILD_DIR，
要不要覆蓋到 data/ 是部署決定，見 docs/名冊維護與爬蟲.md 步驟 5）。
"""
import os
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))

MASTERS_DIR = os.path.join(HERE, "masters")
BASELINE_DIR = os.path.join(HERE, "baseline")
RAW_DIR = os.path.join(HERE, "raw")
BUILD_DIR = os.path.join(HERE, "build")

# 三張凍結列源名冊（build_authority_master 的唯一輸入）
FIM = os.path.join(MASTERS_DIR, "financial_institutions_master.csv")
BROKERS = os.path.join(MASTERS_DIR, "insurance_brokers_master.csv")
FARMERS = os.path.join(MASTERS_DIR, "farmers_credit_master.csv")
TAX_ID_LOOKUP = os.path.join(MASTERS_DIR, "tax_id_lookup.csv")
TAX_ID_PENDING = os.path.join(MASTERS_DIR, "tax_id_pending.csv")

# builder 產出（同時也是分類器 data/ 要用的檔）
AUTHORITY_MASTER = os.path.join(BUILD_DIR, "authority_master.csv")


def raw_dir(run_date=None, create=True):
    """回傳 raw/<日期>/，預設用今天。抓取日一律取系統日期，不寫死常數。"""
    d = run_date or date.today().isoformat()
    p = os.path.join(RAW_DIR, d)
    if create:
        os.makedirs(p, exist_ok=True)
    return p


def baseline(filename):
    return os.path.join(BASELINE_DIR, filename)
