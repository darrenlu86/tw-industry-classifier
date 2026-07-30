# crawlers/masters/ — 列源名冊（不在版控裡）

`build_authority_master.py` 的唯一輸入。這裡的檔案**不進版控**——它們是把各主管機關
原始名冊經過歸併、統編補全、沿革承接之後的成果，屬於彙整資產。

需要五個檔：

| 檔案 | 列數 | 內容 |
|---|---|---|
| `financial_institutions_master.csv` | 490 | 銀行、證券、期貨、投信、投顧、保險本業、電支等 20 個 segment |
| `insurance_brokers_master.csv` | 542 | 保險經紀人公司＋保險代理人公司 |
| `farmers_credit_master.csv` | 311 | 農會信用部 283＋漁會信用部 28 |
| `tax_id_lookup.csv` | 1,344 | 機構全銜 → 統編的解析快取 |
| `tax_id_pending.csv` | 0＋表頭 | 待補統編的清單 |

## 欄位

三張列源名冊共用同一組欄位：

```
authority, segment, inst_code, official_name, short_name, aliases,
tax_id, tax_id_source, status, notes
```

- `segment` 只能用 `crawlers/_taxonomy.py` 的 `SEGMENT_TO_GROUP` 定義的 24 個值，
  用別的值 builder 會直接報錯
- `status` 只能是 `active` / `renamed` / `merged` / `exited`
- `source`（在輸出的 authority_master 裡）會標記名冊性質，例如融資租賃那批標的是
  「租賃公會名錄（非主管機關）」——因為融資租賃不是特許業，台灣沒有主管機關名單

## 怎麼取得

1. **向提供者索取**：最快，而且 `tax_id_lookup.csv` 是不可重現的資產
   （GCIS 查詢結果＋人工提供的統編），從零重建會很痛
2. **自己建**：跑 `crawlers/run_all.py` 抓六個官方名冊，再依 `docs/名冊維護與爬蟲.md`
   的步驟 2–3 人工歸併。這是一次性的工，之後每季只需維護異動

## 兩個地雷

> ⚠️ **不要直接執行 `enrich_tax_id.py` 的 main()**。它會用三張列源名冊從零重建
> `tax_id_lookup.csv`，覆蓋掉既有的 GCIS 查詢成果。該檔已加旗標保護，但還是別碰。

> ⚠️ **builder 有列數守門**：`SOURCE_INPUTS` 硬編期望列數（490／542／311），
> 你更新名冊後列數會變，builder 會停下來報錯。這是刻意的防呆——確認是刻意更新後
> 再改那個常數。
