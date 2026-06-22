# 政治獻金與牠們的產地 — ardata 下載/正規化工具

從 **監察院政治獻金公開查閱平臺**（<https://ardata.cy.gov.tw>）抓取各場選舉、各選舉區
候選人的政治獻金資料（含**捐贈來源明細**），正規化成統一 schema 的 CSV，
供開放資料視覺化平台使用。

> 主管機關是**監察院**（不是中選會）。平臺僅收錄 **107 年(2018)修法後**的「會計報告書」明細。

## 安裝

```bash
pip install -r requirements.txt        # 只需 requests
```

## 快速開始

```bash
# 1) 看平臺目前收錄哪些年度 / 選舉
python3 ardata_scraper.py --catalog

# 2) 抓 2022 九合一 (民國111) 與 2024 總統+立委 (民國113)
python3 ardata_scraper.py --years 111 113

# 也可用西元年
python3 ardata_scraper.py --years-ad 2022 2024
```

輸出（預設於 `data/`）：

```
data/
├── raw/                         # 官方原始 ZIP（依民國年分資料夾，可重複使用）
│   ├── 111/ election-*.zip
│   └── 113/ election-*.zip
└── normalized/
    ├── transactions_111.csv     # 2022 收入+支出「明細」（統一 schema）★核心
    ├── summary_111.csv          # 2022 每位候選人「收支結算表」彙總
    ├── transactions_113.csv     # 2024 明細
    ├── summary_113.csv          # 2024 彙總
    ├── transactions_all.csv     # 跨年度合併（指定多年度時）
    ├── summary_all.csv
    └── download_manifest.csv    # 下載清單與每檔列數、狀態
```

## 常用參數

| 參數 | 說明 |
|---|---|
| `--years 111 113` | 民國年（可多個） |
| `--years-ad 2022 2024` | 西元年（可多個） |
| `--catalog` | 只列出平臺收錄的年度/選舉，不下載 |
| `--name-filter "市長\|議員"` | 只處理選舉名稱符合 regex 的申報列 |
| `--out DIR` | 輸出根目錄（預設 `data`） |
| `--sleep 1.0` | 每次下載間隔秒數（對政府主機客氣） |
| `--refresh` | 強制重新下載（預設沿用已存在的 ZIP，可中斷續跑） |
| `--latest-only` | 去除完全重複的明細列 |

## 統一 schema — `transactions_*.csv`（明細，每列一筆收入或支出）

| 欄位 | 說明 |
|---|---|
| `election_year_roc` / `election_year_ad` | 選舉年度（民國 / 西元） |
| `election_name` | 選舉名稱（明細檔為各縣市別，如「111年臺北市市長選舉」） |
| `election_type` | 選舉類別（總統副總統 / 立法委員 / 直轄市長 / 縣市長 / 直轄市議員 / 縣市議員 / 鄉鎮市長 / 鄉鎮市民代表 / 村里長 / 原住民區長 / 原住民區民代表） |
| `electoral_district` | 選舉區（到縣市層級，如「臺北市」） |
| `candidate` | 擬參選人／政黨 |
| `report_serial` | 申報次別（首次申報 / 第N次賸餘 / 更正…） |
| `direction` | `income`（收入/捐贈）或 `expense`（支出） |
| `txn_date` / `txn_date_roc` | 交易日期（西元 ISO / 原民國） |
| `account_subject` | 收支科目（如 個人捐贈收入、營利事業捐贈收入…） |
| `donor_type` | 捐贈來源類別（個人 / 營利事業 / 政黨 / 人民團體 / 匿名 / 其他；僅收入） |
| `counterparty` | **捐贈者／支出對象**（姓名/公司名） |
| `counterparty_id` | 身分證／統一編號（公司統編多會顯示，個人多為隱碼/空白） |
| `amount` | 金額 |
| `method` | 捐贈方式（匯款 / 票據 / 現金…） |
| `is_monetary` | 金錢類 / 非金錢類 |
| `deposit_date` | 存入專戶日期（西元 ISO） |
| `returned_or_treasury` | 返還／繳庫 |
| `purpose` | 支出用途 |
| `address` / `phone` | 地址／電話（多為部分隱碼 `****` 或空白） |
| `disclosed_payee` / `relation` | 應揭露之支出對象／關係 |
| `correction_note` / `correction_date` | 更正註記／更正日期 |
| `source_zip` | 來源原始 ZIP 檔名（可回溯） |

`summary_*.csv` 沿用官方收支結算表欄名（中文），另加上 `election_year_*`、
`election_type`、`electoral_district`，並把日期欄補上 `_西元` 版本。

## 資料來源與 API（逆向整理）

- 平臺前端：Angular SPA；資料 API 為 `https://ardata.cy.gov.tw/api/v1`。
- 進站雖有「使用須知 + 驗證碼」，但**資料 API 不需驗證碼**（純前端門檻）。
- 本工具使用三支 API：
  1. `GET /search/data` — 收錄年度與選舉清單。
  2. `GET /search/elections?ElectionYear={民國年}&page=&pageSize=` — 該年度全部申報列，附下載連結。
  3. `GET /Search/download?...&SearchType=2&DownloadType=3` — 取得每個「選舉×選舉區×申報次別」的 ZIP。
- ZIP 內含 `incomes.csv`（收入明細）、`expenditures.csv`（支出明細）、收支結算表、
  `manifest.csv`、`schema_*.csv`（欄位字典）。

## 已知限制 / 注意事項

- **年度涵蓋**：平臺有 103(2014)、105(2016)、107(2018)、108、109(2020)、111(2022)、
  112、113(2024)、114(2025)。**93–107 年僅有收支結算表（不在本平臺）**，
  明細需至監察院臨櫃申請。
- **選舉區粒度**：立委、縣市議員等的 `electoral_district` 僅到「縣市」，
  不含更細的「選舉區編號」；候選人姓名仍完整。
- **隱碼**：個人捐贈者的身分證、完整地址、電話多被隱碼；公司統編多會顯示。
- **申報次別**：同一候選人可能有「首次申報」與「賸餘/更正申報」多列，預設全留並以
  `report_serial` 標記；`--latest-only` 僅去除完全重複列（不會臆測合併更正）。
- **2026 九合一**：選舉在 2026/11，會計報告書申報期限在**選後約 3 個月**，
  故完整資料約 **2027 年初**才會上平臺；屆時 `--years 115` 即可抓取。
- **使用限制**：依平臺使用須知，資料不得作營利、徵信或其他不正當用途；
  再利用時請標註來源（監察院政治獻金公開查閱平臺）。

## 授權

程式碼可自由使用。資料著作權與使用規範依監察院平臺規定。
