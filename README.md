# 手機股票雷達 v0.3 — 雲端 / PWA 版

這一版的目的就是：**手機直接使用，不需要讓 PC 或筆電一直開著。**

## 架構
手機 Chrome / PWA → 雲端 Stock Radar → Playwright Chromium → Yahoo 股市

預設監控：
- `6770.TW` 力積電
- `2609.TW` 陽明
- `WTX&` 台指期近一
- 也可貼任何 `https://tw.stock.yahoo.com/...` 網址

## v0.3 改動
- 支援雲端平台的 `PORT` 環境變數
- Docker / Render 部署設定已附上
- Chromium 常駐共用，不再每抓一檔就重開瀏覽器
- 預設 3 檔平行抓取
- PWA 192 / 512 圖示
- Service Worker 更新與 API no-cache
- 手機可選擇每 60 秒自動更新
- 仍維持唯讀，沒有下單功能

---

# 最簡單：Render 雲端部署

你可以全程用手機完成，但需要一個 GitHub 帳號與 Render 帳號。

### 1. 把這個專案放到 GitHub
把 ZIP 解壓後，將整個資料夾上傳到一個 GitHub repository。

專案根目錄一定要看得到：
- `Dockerfile`
- `render.yaml`
- `requirements.txt`
- `server.py`
- `static/`

### 2. 在 Render 建立服務
Render → New → Web Service → 選剛才的 GitHub repository。

因為專案內已有 `Dockerfile`，Render 會使用 Docker 建置。

健康檢查網址：
`/health`

部署完成後會得到類似：
`https://mobile-stock-radar-xxxx.onrender.com`

### 3. 手機安裝成 App
Android Chrome 開啟上面的 HTTPS 網址：
右上角選單 → **安裝應用程式 / 加到主畫面**。

之後直接點桌面「股票雷達」即可，不需要 PC。

---

# 其他 Docker 平台
只要平台支援 Docker，基本上也可以使用此專案。
容器啟動命令已寫在 `Dockerfile`：

`uvicorn server:app --host 0.0.0.0 --port ${PORT}`

平台需提供 `PORT`；若沒有，預設為 `10000`。

---

# 本機測試（選用）

Windows：雙擊 `start.bat`

Linux/macOS：
`./start.sh`

本機預設：
`http://127.0.0.1:8765`

---

# API

健康狀態：
`GET /health`

預設清單：
`GET /api/watchlist`

抓單一預設標的：
`GET /api/quote?symbol=6770.TW`

抓 Yahoo 網址：
`GET /api/quote?url=https%3A%2F%2Ftw.stock.yahoo.com%2Fquote%2F6770.TW`

全部預設標的：
`GET /api/all`

## 安全限制
- `/api/quote` 只允許 `https://tw.stock.yahoo.com/...`
- 沒有券商登入、下單、密碼或 API Key
- Yahoo 網頁改版時，某些欄位可能回傳空值；程式不會猜行情
- Yahoo 網頁資料不等於交易所直連 Tick Feed，可能有延遲

## 雲端注意事項
免費雲端方案可能在長時間無人使用後休眠，第一次開啟可能需要較久才恢復。若要長時間盤中穩定使用，建議改用不休眠的付費 Web Service / VPS。
