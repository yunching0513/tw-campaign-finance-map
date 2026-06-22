#!/usr/bin/env bash
# =============================================================================
# deploy_github_pages.sh — 一鍵把「臺灣縣市政治獻金地圖」上架到 GitHub Pages
#
# 用法：
#   bash deploy_github_pages.sh [repo名稱] [public|private]
#   例： bash deploy_github_pages.sh tw-campaign-finance-map public
#
# 前置需求：
#   - git
#   - GitHub CLI (gh)：brew install gh
#   - 已登入：gh auth login   （需勾選 repo 與 workflow 權限）
#
# 公開內容：只有 docs/（index.html + 彙總後的 map_data.js）與程式碼、README。
# 原始 130MB 明細（data/）已由 .gitignore 排除，不會上傳。
# =============================================================================
set -euo pipefail

REPO_NAME="${1:-tw-campaign-finance-map}"
VISIBILITY="${2:-public}"

cd "$(dirname "$0")"
echo "▶ 專案目錄：$(pwd)"

# 0) 前置檢查 ----------------------------------------------------------------
command -v git >/dev/null || { echo "✗ 需要 git"; exit 1; }
command -v gh  >/dev/null || { echo "✗ 需要 GitHub CLI：brew install gh"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "✗ 請先登入：gh auth login"; exit 1; }

# 1) 網站資料夾：GitHub Pages 由 /docs 提供 -----------------------------------
if [ -d web ] && [ ! -d docs ]; then
  echo "▶ 將 web/ 改名為 docs/（GitHub Pages 由 /docs 提供）"
  mv web docs
fi
[ -f docs/index.html ]  || { echo "✗ 找不到 docs/index.html"; exit 1; }
if [ ! -f docs/map_data.js ]; then
  echo "▶ 找不到 docs/map_data.js，重新產生…"
  python3 build_map_data.py --year 111 --out docs/map_data.js
fi

# 2) 不上傳的 .nojekyll（避免 GitHub Pages 的 Jekyll 處理掉檔案）-------------
touch docs/.nojekyll

# 3) git init + commit -------------------------------------------------------
[ -d .git ] || { echo "▶ git init"; git init -b main >/dev/null; }
git add .gitignore ardata_scraper.py build_map_data.py requirements.txt \
        README.md docs/ deploy_github_pages.sh 2>/dev/null || true
git commit -m "政治獻金與牠們的產地：臺灣縣市政治獻金地圖 (2022) + 下載/正規化工具" \
  >/dev/null 2>&1 && echo "▶ 已建立 commit" || echo "▶ (無新變更可提交)"

# 4) 建立 remote 並 push -----------------------------------------------------
if git remote get-url origin >/dev/null 2>&1; then
  echo "▶ 推送到既有 origin"
  git push -u origin main
else
  echo "▶ 建立 GitHub repo「$REPO_NAME」($VISIBILITY) 並推送"
  gh repo create "$REPO_NAME" --"$VISIBILITY" --source=. --push
fi

# 5) 啟用 GitHub Pages：main 分支 /docs --------------------------------------
OWNER=$(gh api user -q .login)
echo "▶ 啟用 GitHub Pages (main /docs)…"
gh api -X POST "repos/$OWNER/$REPO_NAME/pages" \
     -f "source[branch]=main" -f "source[path]=/docs" >/dev/null 2>&1 \
  || gh api -X PUT "repos/$OWNER/$REPO_NAME/pages" \
       -f "source[branch]=main" -f "source[path]=/docs" >/dev/null 2>&1 \
  || echo "  (若失敗，請到 repo Settings ▸ Pages 手動設定 Source = main / docs)"

# 6) 顯示網址 ----------------------------------------------------------------
echo ""
echo "✅ 完成！GitHub Pages 建置約需 1–2 分鐘後可開啟："
URL=$(gh api "repos/$OWNER/$REPO_NAME/pages" -q .html_url 2>/dev/null || true)
echo "   ${URL:-https://$OWNER.github.io/$REPO_NAME/}"
echo ""
echo "Repo： https://github.com/$OWNER/$REPO_NAME"
