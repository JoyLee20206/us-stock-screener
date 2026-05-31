# 🇺🇸 美股量化選股終端機

基於 **CAN SLIM + Minervini + O'Neil** 三派經典方法論打造的美股量化選股系統。
整合大盤健康度檢測、IBD 風格 RS Rating、VCP 收縮、Stage 2 突破、持倉警示與績效回測。

資料來源為 **yfinance**，標的池為 **S&P 500 + NASDAQ 100 + S&P MidCap 400 + 費城半導體 (SOX)** 四大指數成分股的聯集（去重後約 900 檔）。S&P MidCap 400 是 Minervini 風格的甜蜜點，市值 $40-150 億最容易出現 VCP 突破飆股。

---

## ✨ 核心功能

### 1. 大盤狀態紅綠燈（CAN SLIM 的 M）
進場前先看大盤臉色，避免在派發階段硬選股。

- 🟢 **多頭健康**：站上 200MA + 站上 50MA + 50MA 上揚 + 近 25 日派發日 < 4
- 🟡 **警示**：站上 200MA 但派發日累積（建議降低持倉）
- 🔴 **空頭/弱勢**：跌破 200MA 或派發過多（建議空手觀望）

同時顯示 SPY 距 200MA 距離、50MA 走勢、VIX 波動率。

### 2. 三種交易心法

| 模式 | 邏輯 | 適合 |
|------|------|------|
| 🚀 **BREAKOUT** | 突破近 20 日新高 + 量增 1.5 倍 + 短期 RS 為正 + 乖離 < 15% | 動能交易者 |
| 🎣 **PULLBACK** | 站上 MA20 + 月線正乖離 < 5% + MA20 上揚 + 短期 RS 為正 | 順勢回檔買點 |
| 📊 **COMBO** | 突破或回檔擇一成立 + 加權計分過閾值 | 全面掃描 |

### 3. 九項加分指標（滿分 9 分）

| 大類 | 指標 | 說明 |
|------|------|------|
| **型態** | sig_setup | 突破或回檔成立 |
| **量價** | sig_vol | 連續兩日量增 + 紅 K |
| **強勢** | sig_rs | 21 日相對大盤強度為正 |
| **乖離** | sig_cool | 月線正乖離 < 15%（避免追高） |
| **籌碼** | RS Rating | IBD 風格 1-99 百分位排名（加權 63/126/189/252 日） |
| **籌碼** | U/D Ratio | 近 50 日上漲日總量 / 下跌日總量 ≥ 1.25 = 機構吃貨 |
| **型態** | VCP 收縮 | 近 2 週區間 < 近 4 週區間 × 0.6（Minervini 經典） |
| **型態** | Stage 2 | 近 30 日盤整 < 15% 後突破新高 |
| **量價** | Power Day | 近 5 日有 ≥5% 跳空 + 量爆 2 倍 |

### 4. 品質硬過濾（第一波）
不符就直接剔除，不進入計分流程。

- 距 52 週新高 ≤ 25%
- 多頭排列 (MA5 > MA20 > MA60)
- 季線 (MA60) 上揚
- 強制站上季線
- 最低股價、最低均量、最低日成交額

### 5. 可執行性計算（下單參數）

- **建議停損** = max(現價 - ATR × 倍數, MA20 × 0.99)
- **建議目標** = 現價 + 風險金額 × R/R 倍數
- **風險%**、**獲利空間%**、**ATR%**、**日成交額**

### 6. 持倉管理（D + E）
新增持倉後自動評估目前狀態並產生警示：

- 🔴 **嚴重警示**：觸發停損、跌破 MA50
- 🟡 **一般警示**：RS<50、量大長黑、8 週未動
- 🟢 **正向訊號**：創 30 日新高、達目標價

**Minervini 鐵則**：實際生效停損 = max(進場價 × 0.93, 你設定的停損)，比 -7% 寬鬆的停損會被自動緊縮。

### 7. 績效回測（F）
讀取 `scans/` 歷史掃描快照，分析訊號的 1 週 / 4 週 / 12 週後表現：

- 總體勝率、中位數獲利、平均獲利、最大回撤
- 訊號別表現（VCP / Stage2 / PowerDay / 突破 / 回檔）
- 分數分組表現（1-3 / 4-5 / 6-7 / 8-9 分）

### 8. 雲端持倉備份
側邊欄提供：

- 📥 **下載持倉 JSON**（部署前先備份）
- 📤 **還原持倉 JSON**（重開 App 時還原）
- 📤 **上傳歷史掃描快照**（跨工作階段回測）

---

## 📁 檔案結構

```
美股選股/
│
├── us_screener_ui.py            # 主程式（Streamlit UI，雲端部署入口）
├── us_screener.py               # 舊版精簡版（已被 us_screener_ui.py 取代，保留參考）
├── fetch_cache_us.py            # 美股每日資料抓取（GitHub Actions 每天自動執行）
├── options_data.py              # 期權鏈抓取 + Black-Scholes Greeks + 智能標籤
├── 期權新手指南.md              # 給沒接觸過期權的人看的入門教學（App 內可讀取）
├── requirements.txt             # Python 套件清單
├── README.md                    # 本檔案
├── .gitignore                   # Git 排除清單
│
├── .github/
│   └── workflows/
│       └── fetch.yml            # GitHub Actions：每日 21:30 UTC（台灣 05:30）抓資料
│                                #   → 上傳到 GitHub Releases (tag: data-cache)
│
├── .streamlit/
│   └── secrets.toml             # 本機秘鑰範本（不上傳，雲端在 App 設定貼入）
│                                #   PARQUET_URL = GitHub Releases 的 parquet 下載連結
│
├── cache/                       # 自動產生：每日股價快取（.gitignore 排除）
│   └── us_daily.parquet         #   ├─ 約 600 檔 × 1 年 = ~15 MB
│                                #   └─ 增量更新 + 週六全量刷新
│
├── scans/                       # 自動產生：每次掃描結果快照（.gitignore 排除）
│   └── YYYY-MM-DD_HHMMSS_MODE.parquet
│                                # 提供「績效回測」模組分析訊號後續表現
│
├── positions/                   # 自動產生：本機持倉（.gitignore 排除）
│   └── positions.json           # 雲端模式請用「下載/還原持倉 JSON」備份
│
└── fetch_us_cache說明.doc       # 抓取程式說明文件（原名保留）
    fetch_us_cache說明.odt
```

### 主要程式檔案職責

| 檔案 | 行數 | 職責 |
|------|------|------|
| `us_screener_ui.py` | ~1000 | Streamlit 主程式：UI、選股、持倉、回測、期權教學/瀏覽 |
| `fetch_cache_us.py` | ~210 | 抓取成分股清單 + yfinance 增量下載 + 寫入 parquet |
| `options_data.py` | ~250 | 期權鏈抓取 + Black-Scholes Greeks + 智能標籤邏輯 |
| `期權新手指南.md` | ~250 | 給新手讀的期權入門文件，App 內透過 markdown 渲染顯示 |
| `.github/workflows/fetch.yml` | ~40 | 每日排程：跑 fetch_cache_us.py + 上傳 Releases |

---

## 🚀 部署架構（GitHub + Streamlit Community Cloud，完全免費）

```
┌──────────────────────────────────────────────────────────────┐
│  GitHub Actions（免費 Cron）                                  │
│  每天 05:30 (台灣) 執行 fetch_cache_us.py                     │
│       │                                                       │
│       ▼                                                       │
│  GitHub Releases (tag: data-cache)                            │
│  └─ us_daily.parquet  ←── 對外公開、直接 URL 下載             │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│  Streamlit Community Cloud（免費）                            │
│  └─ us_screener_ui.py                                         │
│     │                                                         │
│     ├─ 啟動時從 PARQUET_URL 下載 parquet（cache 1 小時）      │
│     ├─ 持倉透過 session_state + 手動 JSON 備份                │
│     └─ 掃描快照存在當次工作階段（可下載 Excel 報表）          │
└──────────────────────────────────────────────────────────────┘
```

---

## 📦 部署步驟

### 1. 建立 GitHub Repo
到 [github.com](https://github.com) → New Repository → **Public** → 名稱例如 `us-stock-screener`

### 2. 推上 GitHub
```powershell
cd "C:\Users\joyle\OneDrive\Documents\Claude\美股選股"
git init
git add us_screener_ui.py fetch_cache_us.py requirements.txt .github .streamlit .gitignore README.md
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_USERNAME/us-stock-screener.git
git branch -M main
git push -u origin main
```

### 3. 手動觸發第一次抓取
GitHub → Actions → **Daily US Stock Cache Update** → **Run workflow**
等 5 分鐘後到 Releases 頁面確認 `us_daily.parquet` 已上傳。

### 4. 取得 Parquet URL
複製 Releases 頁面上 `us_daily.parquet` 的下載連結：
```
https://github.com/YOUR_USERNAME/us-stock-screener/releases/download/data-cache/us_daily.parquet
```

### 5. Streamlit Cloud 部署
1. [share.streamlit.io](https://share.streamlit.io) → 用 GitHub 登入
2. **New app** → Repo: `us-stock-screener` / Branch: `main` / Main file: **`us_screener_ui.py`**
3. **Advanced settings → Secrets** 貼入：
   ```toml
   PARQUET_URL = "https://github.com/YOUR_USERNAME/us-stock-screener/releases/download/data-cache/us_daily.parquet"
   ```
4. **Deploy**

---

## 🛠️ 本機開發

```powershell
cd "C:\Users\joyle\OneDrive\Documents\Claude\美股選股"

# 第一次：建立 venv 並安裝套件
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 抓資料（首次約 5 分鐘，之後增量約 30 秒）
python fetch_cache_us.py

# 啟動 UI
streamlit run us_screener_ui.py
```

本機模式會優先讀取 `cache/us_daily.parquet`，不需要設定 `PARQUET_URL`。

---

## ⚙️ 參數調整指南

### 側邊欄參數對照

| 參數 | 預設 | 何時調整 |
|------|------|----------|
| 最低股價 | $10 | 想做小型股 → 調低到 $5 |
| 最低均量 (萬股) | 100 萬 | 流動性要求嚴格 → 調高到 300 萬 |
| 距 52 週新高 ≤ 25% | ON | 想抓底部反轉 → OFF |
| 多頭排列 | ON | 弱勢市場想找轉強 → OFF |
| 季線上揚 | ON | 想抓盤整突破 → OFF |
| RS Rating 加分閾值 | 80 | IBD 標準：80 算強，90 算頂級 |
| U/D Ratio 加分閾值 | 1.25 | < 1.0 為派發、≥ 1.5 為強力吃貨 |
| 最低日成交額 (M$) | 5 | Minervini 建議 > $1M，避免滑價 |
| R/R 目標倍數 | 2.0 | 賠 1 賺 2，保守可用 1.5，積極可用 3.0 |
| 停損 ATR 倍數 | 2.5 | 高波動股可調 3.0，低波動股可調 2.0 |
| 最低總分 | COMBO=5, 其他=1 | 大盤強勢可降到 3，弱勢拉高到 7 |

### 抓取程式參數 (`fetch_cache_us.py`)

| 參數 | 預設 | 說明 |
|------|------|------|
| `PERIOD` | `"1y"` | 首次/新標的下載期間 |
| `KEEP_DAYS` | 380 | 快取保留視窗（含週末緩衝） |
| `FULL_REFRESH_WEEKDAY` | 5 (週六) | 每週幾強制全量刷新 |
| `STALE_DAYS` | 7 | 快取超過幾天未更新強制全量 |

---

## 📊 資料更新機制

1. **每日增量** (週一到週五凌晨 5:30)：只下載最新一天的資料
2. **每週全量** (週六)：完整重抓 1 年資料，吸收當週分割/股息調整
3. **失效保護**：快取超過 7 天未更新自動強制全量
4. **指數成分股自動同步**：每次都從 Wikipedia 抓最新名單，已剔除的標的會從快取移除

---

## 🎓 方法論參考

- **William O'Neil — CAN SLIM**：大盤方向 + 強勢股 + 突破買點
- **Mark Minervini — SEPA**：VCP 收縮型態 + -7% 鐵則停損 + Stage 2 趨勢
- **IBD — RS Rating**：1-99 百分位相對強度排名，> 80 為強勢
- **Power Day**：跳空 5% + 量爆 2 倍 = 機構強力進場訊號

---

## 🔧 常見問題

**Q: 為什麼掃描結果重啟後消失？**
A: 雲端模式下，所有檔案存在臨時磁碟，App 休眠/重啟後消失。請：
- 持倉：用側邊欄「下載持倉 JSON」備份
- 掃描快照：用回測區塊的 Excel 報表存檔，或下載 `scans/*.parquet` 之後再上傳

**Q: 抓取失敗 / Wikipedia 503？**
A: Wikipedia 偶爾擋爬蟲。`fetch_cache_us.py` 有錯誤處理會自動 fallback；嚴重時隔幾分鐘手動觸發 workflow。

**Q: 想加自己的觀察清單？**
A: 編輯 `fetch_cache_us.py` 的 `get_sox_tickers()` 函式，加入自己的代號即可。

**Q: 為什麼有 `us_screener.py` 和 `us_screener_ui.py` 兩支？**
A: `us_screener.py` 是早期精簡版，`us_screener_ui.py` 是目前完整版。部署只用 `us_screener_ui.py`。
