# 擋掉中國 IP ＋ 擋機器人／限流（Cloudflare 設定指引）

策略：**只擋中國（CN）IP，其餘地區（含台灣與全球）照常開放**。
這樣不會發生「TW-only 硬閘誤把台灣人擋在外」的問題（行動網路 CGNAT 常被定位錯）。

GitHub Pages 是純靜態託管，**無法**依 IP 封鎖、限流或擋機器人。要真正做到，得在網站前面加一層
**Cloudflare（免費方案即可）**。本指引以你們的網域 `visionzero.tw` 開一個子網域為例。

> ⚠️ 重要前提：GitHub 的 `yunching0513.github.io` 原始網址**永遠公開**，就算前面擋了，
> 從中國直接打 github.io 仍進得去。要徹底，請見最後「徹底封鎖」一節（改用 Cloudflare Pages 託管，
> 或把 repo 設為私有）。此外，**底層是政府公開資料**，靜態公開檔案無法完全防抓——這些手段是
> 「降低濫用與大量抓取」，不是「保密」。

---

## 方案 A：自有網域 + Cloudflare 代理 → GitHub Pages（最快）

### 1. 把網域加進 Cloudflare
1. 註冊 [Cloudflare](https://dash.cloudflare.com/)（免費）。
2. Add a site → 輸入 `visionzero.tw` → 選 **Free** 方案。
3. 依指示把網域的 **Nameservers 改成 Cloudflare 給的兩組**（在你買網域的註冊商後台改）。
   等生效（數分鐘～數小時）。

### 2. 在 GitHub Pages 設定自訂網域
1. GitHub repo `tw-campaign-finance-map` → **Settings ▸ Pages**。
2. **Custom domain** 填 `money.visionzero.tw`（或你想要的子網域）→ Save。
   （這會在 repo 產生一個 `CNAME` 檔。）
3. 先**不要**勾 Enforce HTTPS，等 DNS 生效後再勾。

### 3. 在 Cloudflare 設 DNS（橘雲＝走 Cloudflare）
- DNS ▸ Records ▸ Add record：
  - Type `CNAME`，Name `money`，Target `yunching0513.github.io`，**Proxy status：Proxied（橘色雲）**。

### 4. 擋掉中國 IP（WAF 自訂規則）
Security ▸ **WAF ▸ Custom rules ▸ Create rule**：
- Rule name：`Block China`
- 條件（Edit expression）：
  ```
  (ip.geoip.country eq "CN")
  ```
  （若也要擋香港／澳門，改成 `(ip.geoip.country in {"CN" "HK" "MO"})`）
- Action：建議 **Managed Challenge**（跳驗證，誤判時真人按一下仍可進）；要更硬可選 **Block**。
- Deploy。
> 結果：**只有中國（CN）IP 會被擋／需驗證；台灣與其餘地區照常開放**，不會誤傷台灣使用者。

### 5. 擋機器人
Security ▸ **Bots ▸ Bot Fight Mode → 開啟**（免費，擋已知爬蟲）。

### 6. 限流（擋大量訪問）
Security ▸ **WAF ▸ Rate limiting rules ▸ Create**：
- 例如：同一 IP 在 `10 秒`內 `> 60` 次請求 → **Block** `60 秒`。
- 免費方案有一條基本限流規則可用。

完成後，對外請大家用 **`https://money.visionzero.tw`**。

---

## 方案 B：改用 Cloudflare Pages 託管（更乾淨）

把 `docs/` 直接部署到 **Cloudflare Pages**（連 GitHub repo 自動建置），同網域掛上 WAF 國別規則／
Bot Fight／限流，安全規則原生套用，且沒有 github.io 這個旁路出口（但 `*.pages.dev` 預設網址仍公開，
建議只對外公布自訂網域，並對 `*.pages.dev` 也加規則）。

---

## 徹底封鎖 github.io 旁路

只要還用 GitHub Pages，`yunching0513.github.io/...` 就一定打得開。若要徹底只走台灣：
1. **改方案 B**（Cloudflare Pages，不暴露 github.io）；或
2. 把 GitHub repo **設為 Private**（Pages 仍可發佈，但原始碼不公開；github.io 網址仍在，
   故仍建議搭配方案 B）；或
3. 接受「主要入口走 Cloudflare、github.io 為次要旁路」的現實——畢竟資料本為公開資料。

---

## 已在程式內做的（免 Cloudflare 也有的基本防護）
- `docs/robots.txt`：謝絕 GPTBot／ClaudeBot／CCBot／Google-Extended 等 AI 訓練爬蟲，並請一般爬蟲
  勿抓 `map_data.js`／`candidates.js`。（僅對守規矩的程式有效）

> 前端「台灣軟閘」已**移除**——免費前端 IP 定位對台灣（行動網路 CGNAT 多）誤判率高，會把真台灣
> 使用者擋在外。地理判斷一律改在 Cloudflare 做（定位準、可用 Managed Challenge 軟性處理、可一鍵調整）。
