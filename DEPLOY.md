# 上架到 GitHub Pages

## 前置（只需一次）

```bash
# 安裝 GitHub CLI（若尚未安裝）
brew install gh
# 登入（瀏覽器授權，記得允許 repo 權限）
gh auth login
```

## 一鍵上架

```bash
cd "/Users/yunching0513/2026地方選舉專案/01-政治獻金與牠們的產地"
bash deploy_github_pages.sh           # 預設 repo 名稱 tw-campaign-finance-map（公開）
# 或自訂名稱：bash deploy_github_pages.sh 我的repo名稱 public
```

腳本會自動：
1. 把 `web/` 改名為 `docs/`（GitHub Pages 由 `/docs` 提供）
2. `git init` → commit（已用 `.gitignore` 排除 130MB 原始資料）
3. `gh repo create` 建立遠端並 push
4. 啟用 Pages（main 分支 `/docs`）
5. 印出網址（約 1–2 分鐘後生效）：`https://<你的帳號>.github.io/tw-campaign-finance-map/`

## 會公開什麼

| 會上傳 | 不會上傳 |
|---|---|
| `docs/`（index.html + 彙總後 map_data.js）、`*.py`、README | `data/raw/`、`data/normalized/*.csv`（130MB+ 原始明細） |

`map_data.js` 只含**各縣市彙總數字 + 地圖路徑**，不含個別捐贈者明細。

## 手動備援（無 gh 時）

1. 到 github.com 手動建立空 repo。
2.
   ```bash
   cd "01-政治獻金與牠們的產地"
   mv web docs && touch docs/.nojekyll
   git init -b main
   git add .gitignore *.py *.md requirements.txt docs/
   git commit -m "init"
   git remote add origin https://github.com/<帳號>/<repo>.git
   git push -u origin main
   ```
3. repo ▸ Settings ▸ Pages ▸ Source 設為 `main` / `/docs`。
