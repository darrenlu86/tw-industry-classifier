# crawlers/baseline/ — 異動比對基準（不在版控裡）

各支 fetcher 的 diff 基準線：抓到最新名冊後，跟這裡的既有快照比對，
產出「多了誰、少了誰」的異動報告。**不進版控**（是彙整快照，非公開原始檔）。

需要 23 個 CSV：

| 來源 | 檔案 |
|---|---|
| 銀行局（6） | `bb_domestic_bank.csv`、`bb_foreign_bank.csv`、`bb_coop.csv`、`bb_bills.csv`、`bb_fhc.csv`、`bb_epay.csv` |
| 證期局（6） | `sfb_broker.csv`、`sfb_futures.csv`、`sfb_futures_misc.csv`、`sfb_sitc.csv`、`sfb_sica.csv`、`sfb_sfc.csv` |
| 保險局（5） | `ib_life.csv`、`ib_nonlife.csv`、`ib_reinsurer.csv`、`ib_agents.csv`、`ib_brokers.csv` |
| 農業金融署（3） | `boaf_farmers.csv`、`boaf_fishers.csv`、`boaf_agri_bank.csv` |
| 數位產業署（2） | `moda_tpp_aml.csv`、`moda_tpp_registry.csv` |
| 租賃公會（1） | `leasing.csv` |

## 缺檔不會壞

每支 fetcher 遇到 baseline 不存在時，會在報告寫「無既有快照可比對」然後照常抓取，
**不會 crash**。所以第一次執行可以完全沒有 baseline——抓到的結果就是你的第一份基準。

把 `crawlers/raw/<日期>/` 的輸出複製過來，就成為下一次的比對基準。

## 兩個 schema 特例

- `leasing.csv` 是 10 欄的人工彙整版（含已核實的 `tax_id`），而 `fetch_leasing.py`
  的輸出是 6 欄抓取層中繼格式。兩者無法逐欄比對，所以 diff 只做 `official_name`
  的集合比對，而且**腳本絕不覆寫 baseline**。
- `bb_china_bank.csv` 不存在。陸銀在臺分行的 baseline 是從 `bb_foreign_bank.csv`
  依 `segment` 欄篩出來的（`fetch_banking.py` 有 fallback 處理），這是刻意設計。
