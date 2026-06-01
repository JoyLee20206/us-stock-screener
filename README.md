# 🇺🇸 美股量化選股終端機

基於 **CAN SLIM + Minervini + O'Neil** 三派經典方法論打造的美股量化選股系統,並擴充**純買方視角的期權工具**(新手指南、期權鏈瀏覽、智能標籤、Greeks、What-if 模擬器、損益圖、IV/RV Rank 雙軌、財報日警示、期權持倉追蹤)。

資料來源為 **yfinance**,標的池為 **S&P 500 + NASDAQ 100 + S&P MidCap 400 + 費城半導體 (SOX) + 常用 ETF(~80 檔)** 的聯集(去重後約 1,000 檔)。S&P MidCap 400 是 Minervini 風格的甜蜜點,市值 $40-150 億最容易出現 VCP 突破飆股。ETF 池涵蓋大盤/板塊/主題/國際/債券/商品/槓桿/加密,方便持倉管理與大盤分析。

部署架構為 **GitHub Actions 自動排程 + GitHub Releases 託管資料 + Streamlit Community Cloud 跑 UI**,**完全免費**。

> ⚠️ **雲端期權瀏覽說明**:期權鏈資料無免費雲端 API(Tradier/MarketData/Finnhub/Polygon 全部付費鎖),雲端版只能靠 yfinance 但 Streamlit Cloud 共用 IP 常被 Yahoo 限流。已內建磁碟快取 + 智能冷卻 + 過期 fallback 等強化機制(詳見「雲端期權瀏覽限制」章節),**最穩定的做法是本機執行**。其他功能(選股、回測、持倉等)在雲端完全正常。

---

## 📑 介面總覽

```
┌─ 主畫面(永遠顯示)─────────────────────────────────────┐
│ 🇺🇸 標題 ｜ 🚦 大盤紅綠燈(SPY / vs 50MA / 派發日 / VIX) │
│ 📅 快取更新時間 + 共 N 檔 + [🔄 刷新雲端資料]            │
│ [按 🚀 執行量化掃描(在側邊欄)→ 結果出現在這]            │
│ [📈 對掃描結果產生期權買方建議]                          │
├─ 4 個頁籤(Excel sheet 風格)──────────────────────────┤
│ 📌 我的持倉 ｜ 📈 績效回測 ｜ 📚 期權新手教學 ｜ 🎯 期權瀏覽 │
└─────────────────────────────────────────────────────┘

側邊欄:策略控制中心
├─ 🎯 選擇交易心法(BREAKOUT / PULLBACK / COMBO)
├─ 品質硬過濾 + 籌碼計分 + 下單參數
├─ 🚀 執行量化掃描(藍底大按鈕)
└─ 💾 持倉備份(下載/還原 JSON)
```

---

## ✨ 選股功能

### 1. 大盤狀態紅綠燈(CAN SLIM 的 M)
進場前先看大盤臉色,避免在派發階段硬選股。

- 🟢 **多頭健康**:站上 200MA + 站上 50MA + 50MA 上揚 + 近 25 日派發日 < 4
- 🟡 **警示**:站上 200MA 但派發日累積(建議降低持倉)
- 🔴 **空頭/弱勢**:跌破 200MA 或派發過多(建議空手觀望)

同時顯示 SPY 距 200MA 距離、50MA 走勢、VIX 波動率。

### 2. 三種交易心法

| 模式 | 邏輯 | 適合 |
|------|------|------|
| 🚀 **BREAKOUT** | 突破近 20 日新高 + 量增 1.5 倍 + 短期 RS 為正 + 乖離 < 15% | 動能交易者 |
| 🎣 **PULLBACK** | 站上 MA20 + 月線正乖離 < 5% + MA20 上揚 + 短期 RS 為正 | 順勢回檔買點 |
| 📊 **COMBO** | 突破或回檔擇一成立 + 加權計分過閾值 | 全面掃描 |

### 3. 九項加分指標(滿分 9 分)

| 大類 | 指標 | 說明 |
|------|------|------|
| **型態** | sig_setup | 突破或回檔成立 |
| **量價** | sig_vol | 連續兩日量增 + 紅 K |
| **強勢** | sig_rs | 21 日相對大盤強度為正 |
| **乖離** | sig_cool | 月線正乖離 < 15%(避免追高) |
| **籌碼** | RS Rating | IBD 風格 1-99 百分位排名(加權 63/126/189/252 日) |
| **籌碼** | U/D Ratio | 近 50 日上漲日總量 / 下跌日總量 ≥ 1.25 = 機構吃貨 |
| **型態** | VCP 收縮 | 近 2 週區間 < 近 4 週區間 × 0.6(Minervini 經典) |
| **型態** | Stage 2 | 近 30 日盤整 < 15% 後突破新高 |
| **量價** | Power Day | 近 5 日有 ≥5% 跳空 + 量爆 2 倍 |

### 4. 品質硬過濾
不符就直接剔除,不進入計分流程。

- 距 52 週新高 ≤ 25%、多頭排列 (MA5>MA20>MA60)、季線 (MA60) 上揚
- 強制站上季線、最低股價、最低均量、最低日成交額

### 5. 可執行性計算

- **建議停損** = max(現價 - ATR × 倍數, MA20 × 0.99)
- **建議目標** = 現價 + 風險金額 × R/R 倍數
- **風險%**、**獲利空間%**、**ATR%**、**日成交額**

### 6. 持倉管理(雙分頁:股票 + 期權)

#### 📈 股票持倉
- 新增持倉後自動評估、報酬、停損、警示
- 🔴 **嚴重警示**:觸發停損、跌破 MA50
- 🟡 **一般警示**:RS<50、量大長黑、8 週未動
- 🟢 **正向訊號**:創 30 日新高、達目標價
- **Minervini 鐵則**:實際生效停損 = max(進場價 × 0.93, 你輸入的停損),比 -7% 寬鬆會被自動緊縮

#### 🎯 期權持倉
- 每口合約即時抓取 yfinance Bid/Ask,計算當前損益、Greeks、DTE
- 警示:🚨 DTE≤7 / 🔴 -50% 停損 / 🟢 +100% 達標 / 💎 已 ITM / ⚡ 時間價值將燒完 / 📅 跨財報

### 7. 績效回測
讀取 `scans/` 歷史掃描快照,分析訊號的 1 週 / 4 週 / 12 週後表現:

- 總體勝率、中位數獲利、平均獲利、最大回撤
- 訊號別表現(VCP / Stage2 / PowerDay / 突破 / 回檔)
- 分數分組表現(1-3 / 4-5 / 6-7 / 8-9 分)

---

## 🎯 期權工具系統(純買方視角)

### 1. 📚 期權新手教學
App 內直接渲染 `期權新手指南.md`,10 章涵蓋:什麼是 Call/Put、ITM/ATM/OTM、Greeks、IV、新手 7 大死亡陷阱、實務細節、智能標籤對照、雙軌設計、財報警示。

### 2. 🎯 期權瀏覽(核心)

#### 操作流程
1. 輸入代號(預設帶入剛掃描結果的第一檔,可改)
2. 選到期日(系統自動挑最接近 30 天的甜蜜點)
3. 按 🔍 查詢

#### 顯示內容
- **頂部摘要列 6 欄**:現價 / 到期日 / 剩餘天數 / Call/Put 筆數 / **IV 或 RV Rank** / **📅 下次財報日**
- **⭐ 推薦合約卡**:Call 和 Put 各一張,挑出 Delta 0.45-0.65 + DTE 21-45 的甜蜜點
- **跨財報時頂端紅色橫條**警告 IV crush 風險
- **Call 鏈 / Put 鏈分頁表格**:含智能標籤、行權價、Bid/Ask、Δ、Θ、IV%、BE 等

#### 雲端強化(2026-06-01 新增)
雲端版的期權鏈額外包了一層**磁碟快取 + 智能冷卻 + 過期 fallback** 機制(詳見「雲端期權瀏覽限制」章節),查詢區下方會顯示:
- ✅ 所有資料源待命中 / ⏳ 冷卻中:yfinance 287s
- [🔄 強制刷新] 按鈕:清磁碟快取 + 解除所有 source 冷卻
- 若顯示過期資料,頂部會出現黃色橫條提醒「資料 X 小時前」

### 3. 🏷️ 智能標籤(9 種,每口合約都有明確分類)

#### ✅ 可考慮的合約
| 標籤 | 條件 | 適合誰 |
|------|------|--------|
| ⭐ **推薦** | Δ 0.45-0.65 + DTE 21-45 天 | 新手首選 |
| 💎 **ITM 穩** | Δ > 0.70 | 想替代買股票 |
| 🟠 **略 ITM** | Δ 0.65-0.70 | 保守派 |
| ⏱️ **DTE 不對** | Δ 對了但 DTE 不對 | Delta OK 但時間軸要重挑 |
| 🟡 **略 OTM** | Δ 0.30-0.45 | 性價比中等 |

#### ❌ 應避開
| 標籤 | 條件 |
|------|------|
| ⚠️ **太 OTM** | Δ < 0.30,90% 歸零 |
| 🔥 **高 IV** | IV > 50%,財報前常見 IV crush 風險 |
| 💀 **Theta 黑洞** | DTE < 14 天,時間損耗 5-10%/天 |
| ❓ **流動性差** | OI < 100 **且** 今日量 < 20(2026-06-01 修正:單看 OI 易因 yfinance lag 誤判) |
| 🚨 **跨財報**(獨立欄位)| IV crush 風險 |

### 4. 🔍 合約分析(風險清單 + 模擬器 + 損益圖)

#### ✅ 進場前 7 項風險檢查(紅黃綠燈 + 整體判定 GO/CAUTION/STOP)
到期天數 / Delta / IV / OI / Vol / Bid-Ask 價差 / 跨財報日
- 第 4 項 OI 也加 volume fallback:OI 低但今日量 ≥50 時降一格警示,不再直接判紅燈

#### 💡 名詞解釋
Δ / Θ / IV / BE / OI / Bid-Ask 完整定義(可展開)

#### 🎮 What-if 模擬器
3 軸 slider:股價變動 / 天數經過 / IV 變動 → 即時 BS 重算合約現值、損益、報酬率

#### 📈 到期日損益曲線(Plotly 互動圖)
自動標出 🟠 現價 / 🔵 行權價 / 🟢 BE / 🔴 最大虧損
附「📖 怎麼看這張圖?」完整圖解(含動態帶入數字的常見 Q&A)

### 5. 📈 對選股結果產生期權建議
掃描完成後,下方提供「對 Top N 名產生期權建議」批次按鈕,一次看 N 檔的 ⭐ 推薦合約。

### 6. 📊 雙軌 IV/RV Rank 設計

| 指標 | 何時顯示 | 含義 |
|------|---------|------|
| **IV Rank** | IV 歷史累積 ≥ 30 天 | 真實 IV 在歷史區間的百分位(最準)|
| **RV Rank(估算)** | IV 累積 < 30 天時 | 用「實現波動率」算的近似百分位(立即可用) |

判讀:≥ 70% 🔥 偏貴｜30-70% 中等｜< 30% ✅ 便宜

### 7. 📅 財報日警示
- 每檔標的抓取下次財報日(yfinance)
- 跨財報合約自動標 🚨、頂部紅色橫條警告
- 風險清單第 7 項判定 ✅/⚠️/❌
- 期權持倉自動警示「🚨 財報倒數 N 天,建議平倉避 IV crush」

### 8. 📈 財報前 IV 突增偵測(Earnings Drift)
財報前 1-3 週,市場買期權避險推升 IV,這個「事件溢價」可量化為 IV/RV 比例:

| IV/RV 比 | 等級 | 含義 |
|---------|------|------|
| < 1.20 | ✓ normal | 進場時機 OK |
| 1.20-1.40 | 📈 elevated | 已有事件溢價,買方需小心 |
| > 1.40 | 🔥 strong | IV 暴衝,買方強烈不建議進場 |

摘要列下方永久顯示 IV/RV 比例與當前 IV / RV 數值。**只有當「IV/RV > 1.2 且財報在 30 天內」時**才會在頂部出現警告橫條(earnings drift 確認)。

### 9. 📆 自動推薦「財報後到期」合約(避開 IV Crush)

**應用一:期權瀏覽快速切換**
跨財報的合約頂部會額外出現一鍵按鈕:
```
🚨 跨財報警示:下次財報 2026-08-27(5 天後)...    [📆 改選 2026-09-18]
                                                     ↑ 一鍵切到財報後 14 天最接近的到期
```

**應用二:批次推薦的「避開財報」開關**
掃描結果下方的批次查詢區提供 toggle:開啟後對每檔自動挑「財報後 ~14 天」的到期日,避開 IV crush。沒財報的標的(如 ETF)自動退回一般 30 天邏輯。

---

## ☁️ 雲端期權瀏覽限制(2026-06-01 新增)

### 為什麼這是個問題
Streamlit Community Cloud 用**共用 IP pool**,Yahoo Finance 對這些 IP 限流嚴重(`YFRateLimitError: Too Many Requests`)。我們系統性嘗試過**所有免費期權 API**:

| 供應商 | 期權鏈免費版? | 嘗試結果 |
|--------|-------------|---------|
| Yahoo (yfinance) | ✅ 但雲端共用 IP 被限流 | 雲端常 429 |
| MarketData.app | ❌ Trader 等級 $100/月 才解鎖 | HTTP 402 Payment required |
| Finnhub | ❌ Premium $25+/月 才解鎖 | HTTP 403 You don't have access |
| Polygon.io | ❌ Options $29/月 | 沒實測 |
| Tradier Sandbox | ❌ 需先開美國證券戶(KYC + Foreign Diligence) | 申請流程過於繁瑣 |

期權資料供應商把這個當吸金產品,免費 + 雲端 + 期權鏈幾乎不存在。

### 雲端應對策略(已內建)
即便如此,還是想盡辦法榨乾 yfinance 的偶發成功機會:

| 機制 | 做什麼 | 效果 |
|------|--------|------|
| **6 小時磁碟快取** | 成功抓到的鏈存到 `cache/options/{ticker}_{exp}.parquet` | 同代號 6 小時內幾乎不打 API |
| **智能冷卻** | 403/402 鎖 1 小時;429 限流鎖 5 分鐘 | 不對已知失敗的端點狂打浪費 quota |
| **yfinance 自動重試** | 失敗時等 3s 再試 1 次 | 偶發網路抖動可吸收 |
| **過期 fallback** | 全失敗時用磁碟舊資料 + 黃色橫條警示 | yfinance 全擋時還能看舊鏈 |
| **強制刷新按鈕** | 清磁碟快取 + 解除所有冷卻 | 用戶主動繞過所有快取 |
| **curl_cffi Chrome 偽裝** | yfinance 用偽裝 TLS 指紋的 session | 對 IP 限流無效,但對 UA 限流有用 |
| **冷卻狀態 caption** | UI 顯示「⏳ 冷卻中:yfinance 287s」 | 透明,好除錯 |
| **降級提示** | 三來源都掛時提示「請本機執行」並附指令 | 友善 UX |

### 本機跑就完全沒問題
家用 IP 不在 Yahoo 限流名單,本機跑 yfinance 期權鏈一切正常。

```powershell
cd "你的專案目錄"
streamlit run us_screener_ui.py
```

### 雲端 vs 本機功能對照

| 功能 | 雲端 | 本機 |
|------|------|------|
| 選股、大盤紅綠燈 | ✅ | ✅ |
| 績效回測、持倉管理 | ✅ | ✅ |
| 期權新手教學 | ✅ | ✅ |
| **期權瀏覽 / 持倉評估** | 🎲 看 yfinance 限流狀況(有磁碟快取緩衝) | ✅ 穩定 |
| 每日股價自動更新 | ✅ GitHub Actions | — |

---

## 📁 檔案結構

```
美股選股/
│
├── us_screener_ui.py           # 主程式(Streamlit UI,雲端部署入口,~1750 行)
├── fetch_cache_us.py           # 美股每日股價快取(含 ETF 池 + Releases 同步)
├── fetch_iv_history.py         # 每日 IV snapshot 抓取(用於 IV Rank 累積)
├── options_data.py             # 期權鏈 + BS Greeks + 智能標籤 + 風險清單 +
│                               #   What-if 模擬 + 損益曲線 + 部位評估 +
│                               #   IV/RV Rank + 財報日抓取 + 批次推薦 +
│                               #   磁碟快取/冷卻/重試/IV 反推/Finnhub/MarketData
├── 期權新手指南.md             # 新手教學(App 內 markdown 渲染)
├── requirements.txt
├── README.md                   # 本檔案
├── .gitignore
│
├── .github/
│   └── workflows/
│       ├── fetch.yml           # 股價快取:每日 UTC 21:30(台灣 05:30)
│       │                       #   2026-06-01 新增 env: PARQUET_URL
│       └── fetch_iv.yml        # IV snapshot:每日 UTC 21:45(台灣 05:45)
│                               # 兩者都上傳到 GitHub Releases (tag: data-cache)
│
├── .streamlit/
│   └── secrets.toml            # 雲端在 App 設定 Secrets 貼入 PARQUET_URL
│
├── cache/                      # 自動生成(.gitignore 排除)
│   ├── us_daily.parquet        # 股價快取 ~1,000 檔
│   ├── iv_history.parquet      # IV 歷史累積(觀察清單 51 檔)
│   └── options/                # 期權鏈磁碟快取(2026-06-01 新增,6 小時 TTL)
│       └── {ticker}_{exp}.parquet
│
├── scans/                      # 掃描結果快照(.gitignore 排除)
└── positions/                  # 本機持倉 JSON(.gitignore 排除)
```

### 主要程式檔案職責

| 檔案 | 行數 | 職責 |
|------|------|------|
| `us_screener_ui.py` | ~1850 | Streamlit 主程式:UI、選股、持倉、回測、期權教學/瀏覽/分析、強制刷新按鈕、冷卻狀態 |
| `options_data.py` | ~1400 | BS 定價/Greeks、智能標籤、風險清單、What-if 模擬、IV/RV Rank、財報抓取、IV drift 偵測、財報後到期挑選、批次推薦、**磁碟快取、智能冷卻、yfinance 重試、IV 反推(Newton)、Finnhub/MarketData client、curl_cffi session** |
| `fetch_cache_us.py` | ~310 | 抓 SP500/NDX/SP400/SOX/ETF 成分股 + yfinance 增量下載 + **從 Releases 同步最新 parquet** |
| `fetch_iv_history.py` | ~130 | 每日 IV snapshot 抓取,累積到 iv_history.parquet |
| `期權新手指南.md` | ~280 | 新手讀的完整入門文件 |

---

## 🚀 部署架構(GitHub + Streamlit Community Cloud,完全免費)

```
┌──────────────────────────────────────────────────────────────┐
│  GitHub Actions(公開 repo Cron 無限免費)                    │
│  ├─ 每天 05:30 (台灣) 跑 fetch_cache_us.py                    │
│  │   ↳ 2026-06-01 新增:執行前先從 Releases 拉最新 parquet   │
│  │      (修「每次都 18 天未更新強制全量」bug)               │
│  └─ 每天 05:45 (台灣) 跑 fetch_iv_history.py                  │
│       │                                                       │
│       ▼                                                       │
│  GitHub Releases (tag: data-cache)                            │
│  ├─ us_daily.parquet      ~1,000 檔股價 / 1 年                │
│  └─ iv_history.parquet    51 檔 IV 累積(每日 +1 筆)          │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│  Streamlit Community Cloud(免費)                            │
│  └─ us_screener_ui.py                                         │
│     │                                                         │
│     ├─ 啟動時優先從 PARQUET_URL 下載最新 parquet              │
│     ├─ 持倉透過 session_state + 手動 JSON 備份                │
│     ├─ 期權鏈:磁碟快取 → yfinance(curl_cffi) → 過期 fallback│
│     └─ 「🔄 刷新雲端資料」/「🔄 強制刷新」按鈕可強制重抓     │
└──────────────────────────────────────────────────────────────┘
```

---

## 📦 部署步驟(首次)

### 1. 建立 GitHub Repo(Public)
名稱例如 `us-stock-screener`。Public 才能用 Actions 免費額度。

### 2. 上傳檔案
網頁拖拉:`https://github.com/YOUR_USERNAME/us-stock-screener/upload/main`
必傳:`us_screener_ui.py`、`fetch_cache_us.py`、`fetch_iv_history.py`、`options_data.py`、`期權新手指南.md`、`requirements.txt`、`README.md`、`.gitignore`、`.github/workflows/fetch.yml`、`.github/workflows/fetch_iv.yml`

**`requirements.txt`** 內容(2026-06-01 新增 curl_cffi):
```
streamlit
pandas
yfinance
pyarrow
openpyxl
requests
plotly
curl_cffi
```

### 3. 手動觸發兩個 workflow
- Actions → "Daily US Stock Cache Update" → Run workflow
- Actions → "Daily IV History Snapshot" → Run workflow

等 5 分鐘後到 Releases 確認 `us_daily.parquet` 和 `iv_history.parquet` 都已上傳。

### 4. 取得 Parquet URL
複製:
```
https://github.com/YOUR_USERNAME/us-stock-screener/releases/download/data-cache/us_daily.parquet
```

### 5. Streamlit Cloud 部署
1. [share.streamlit.io](https://share.streamlit.io) → 用 GitHub 登入
2. **New app** → Repo / Branch: main / Main file: **`us_screener_ui.py`**
3. **Advanced settings → Secrets** 貼入:
   ```toml
   PARQUET_URL = "https://github.com/YOUR_USERNAME/us-stock-screener/releases/download/data-cache/us_daily.parquet"
   ```
4. **Deploy**

### 6. (2026-06-01 新增)workflow 也要設 PARQUET_URL
編輯 `.github/workflows/fetch.yml`,在「Run fetch_cache_us.py」步驟加 env:

```yaml
- name: Run fetch_cache_us.py
  env:
    PARQUET_URL: https://github.com/${{ github.repository }}/releases/download/data-cache/us_daily.parquet
  run: python fetch_cache_us.py
```

這樣 Actions 跑的時候才會從 Releases 拉最新 parquet,不會再用 repo 內 stale 的舊檔強制全量 90 秒。

---

## 🛠️ 本機開發

```powershell
cd "你的專案目錄"

# 第一次:建立 venv 並安裝套件
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 抓股價資料(首次約 5 分鐘)
python fetch_cache_us.py

# (選用)抓 IV snapshot
python fetch_iv_history.py

# 啟動 UI
streamlit run us_screener_ui.py
```

本機模式自動 fallback:未設定 `PARQUET_URL` 時用 `cache/us_daily.parquet`。

**雲端期權瀏覽不穩時的主要解法就是切到本機跑**,本機 IP 不被 Yahoo 限流。

---

## ⚙️ 參數調整指南

### 側邊欄參數

| 參數 | 預設 | 何時調整 |
|------|------|----------|
| 最低股價 | $10 | 想做小型股 → 調低到 $5 |
| 最低均量 (萬股) | 100 萬 | 流動性要求嚴格 → 調高到 300 萬 |
| 距 52 週新高 ≤ 25% | ON | 想抓底部反轉 → OFF |
| 多頭排列 | ON | 弱勢市場想找轉強 → OFF |
| 季線上揚 | ON | 想抓盤整突破 → OFF |
| RS Rating 加分閾值 | 80 | IBD 標準:80 算強,90 算頂級 |
| U/D Ratio 加分閾值 | 1.25 | < 1.0 為派發、≥ 1.5 為強力吃貨 |
| 最低日成交額 (M$) | 5 | Minervini 建議 > $1M |
| R/R 目標倍數 | 2.0 | 賠 1 賺 2,保守 1.5、積極 3.0 |
| 停損 ATR 倍數 | 2.5 | 高波動股 3.0、低波動股 2.0 |
| 最低總分 | COMBO=5, 其他=1 | 強勢可降到 3,弱勢拉到 7 |

### 抓取程式參數 (`fetch_cache_us.py`)

| 參數 | 預設 | 說明 |
|------|------|------|
| `PERIOD` | `"1y"` | 首次/新標的下載期間 |
| `KEEP_DAYS` | 380 | 快取保留視窗(含週末緩衝) |
| `FULL_REFRESH_WEEKDAY` | 5 (週六) | 強制全量刷新日 |
| `STALE_DAYS` | 7 | 快取超過幾天未更新強制全量 |
| `PARQUET_URL`(env) | - | (2026-06-01 新增)Releases 上最新 parquet URL,設了才會在執行前拉新版 |

### 期權雲端強化參數 (`options_data.py`,2026-06-01 新增)

| 參數 | 預設 | 說明 |
|------|------|------|
| `DISK_FRESH_HOURS` | 6 | 磁碟快取多久內視為新鮮(直接用不打 API) |
| `COOLDOWN_429_SEC` | 300 | 限流(429)後該 source 冷卻幾秒 |
| `COOLDOWN_PREMIUM_SEC` | 3600 | 付費鎖(403/402)後該 source 冷卻幾秒 |
| `FINNHUB_TOKEN`(env) | - | (選用)Finnhub API key,免費版期權鏈鎖死,留接口給未來付費 |
| `MARKETDATA_TOKEN`(env) | - | (選用)MarketData.app token,同上 |

### IV 觀察清單 (`fetch_iv_history.py`)

51 檔涵蓋:大盤 ETF / 板塊 ETF / Magnificent 7 / 半導體龍頭 / 雲端 SaaS / 金融 / 中概 / 熱門題材。可直接編輯 `WATCHLIST` 加入自己常看的代號。

---

## 📊 資料更新機制

| 機制 | 觸發時機 | 範圍 |
|------|---------|------|
| **每日增量** | 工作日凌晨 5:30 | 只下載最新一天的資料 |
| **每週全量** | 週六 | 完整重抓 1 年資料(吸收當週分割/股息)|
| **失效保護** | 超過 7 天未更新 | 自動強制全量 |
| **指數成分股同步** | 每次都從 Wikipedia 抓 | 已剔除的標的自動從快取移除 |
| **IV 累積** | 工作日凌晨 5:45 | 51 檔 ATM IV 各 +1 筆 |
| **Releases 同步**(2026-06-01) | 每次 workflow 跑前 | 從 Releases 拉最新 parquet,避免每次強制全量 |
| **期權磁碟快取**(2026-06-01) | 查詢成功時自動 | 同代號 6 小時內不再打 API |

---

## 🎓 方法論參考

- **William O'Neil — CAN SLIM**:大盤方向 + 強勢股 + 突破買點
- **Mark Minervini — SEPA**:VCP 收縮型態 + -7% 鐵則停損 + Stage 2 趨勢
- **IBD — RS Rating**:1-99 百分位相對強度排名,> 80 為強勢
- **Power Day**:跳空 5% + 量爆 2 倍 = 機構強力進場訊號
- **Black-Scholes**:期權理論定價與 Greeks 計算
- **IV Crush**:財報後 IV 暴跌,買方主要敵人之一
- **Newton 法 IV 反推**(2026-06-01 新增):從市價反推隱含波動率,API 缺 IV 時的補救

---

## 🔧 常見問題

**Q: 為什麼掃描結果 / 持倉 重啟後消失?**
A: 雲端模式下,所有檔案存在臨時磁碟,App 休眠/重啟後消失。請:
- 持倉:側邊欄「下載持倉 JSON」備份
- 掃描快照:用 Excel 報表存檔,或下載 `scans/*.parquet` 再上傳

**Q: 為什麼快取顯示舊資料 / 找不到新加的 ETF?**
A: 點 App 頂部「🔄 刷新雲端資料」按鈕強制重新下載。Streamlit 預設有 1 小時快取。

**Q: IV Rank 為什麼一直顯示「累積中」?**
A: IV 歷史 yfinance 不提供回填,必須每天自己抓累積。需要 30+ 天才有意義。期間顯示 RV Rank(用股價估算)。

**Q: 期權瀏覽找不到財報日?**
A: ETF(SPY、QQQ)沒財報是正常的。某些小型股 yfinance 可能拿不到,會顯示「無資料」。

**Q: IV/RV 比例突然飆到 1.5 是什麼意思?**
A: 表示市場已 pricing in 即將到來的事件(多半是財報)。買方在這時進場最容易踩 IV crush。建議:等財報後 IV 回穩再進、或用「📆 改選財報後到期」按鈕切換到財報之後的合約。

**Q: 「避開財報」開關打開後,沒財報的標的會怎樣?**
A: 自動退回一般 30 天目標到期日邏輯,跟關閉時一樣。所以同時掃描有財報股 + ETF 不會有問題。

**Q: 抓取失敗 / Wikipedia 503?**
A: Wikipedia 偶爾擋爬蟲。`fetch_cache_us.py` 有錯誤處理會自動 fallback;嚴重時隔幾分鐘手動觸發 workflow。

**Q: 想加自己的觀察清單到股價快取?**
A: 編輯 `fetch_cache_us.py` 的 `get_etf_tickers()` 函式(ETF)或 `get_sox_tickers()`(個股觀察清單),加入代號即可。

**Q: 想加 IV Rank 觀察清單?**
A: 編輯 `fetch_iv_history.py` 的 `WATCHLIST` 列表加入代號,下次 workflow 跑就會開始累積。

**Q: GitHub Actions 排程會自動跑嗎?**
A: ✅ 會。公開 repo 排程 workflow 完全免費無限制。每天清晨 5:30 + 5:45 自動執行。Repo 閒置 60 天會被暫停(手動觸發或 push 任何 commit 即可解除)。

**Q: (2026-06-01)為什麼以前 Actions log 每次都顯示「18 天未更新強制全量」?**
A: 是 bug。Actions checkout 拿到的是 repo 內手動上傳的舊 parquet(2026-05-14),腳本算 stale 一直算成 18 天 → 強制全量 90 秒。已修:`fetch_cache_us.py` 加 `sync_latest_from_releases()`,執行前先從 Releases 拉最新版,workflow 加 `env: PARQUET_URL`。現在改走增量下載。

**Q: (2026-06-01)為什麼期權頁所有 strike 都標「流動性差」?**
A: 是 bug。yfinance 的 `openInterest` 常 lag 一天或回 0,光看 OI < 100 會誤殺熱門合約。已修:改成「OI < 100 **且** 今日量 < 20」才標流動性差。

**Q: (2026-06-01)雲端期權瀏覽抓不到怎辦?**
A: yfinance 在 Streamlit Cloud 共用 IP 常被擋(YFRateLimitError)。已內建磁碟快取 + 智能冷卻 + 過期 fallback + 重試,但有時候還是會全失敗。處理順序:
1. 按「🔄 強制刷新」清快取重抓
2. 看冷卻 caption 顯示剩餘秒數,等冷卻結束再試
3. 若顯示「資料 X 小時前」橫條,代表用過期快取撐著,內容可參考但 Bid/Ask 可能不準
4. **最穩做法是本機跑** `streamlit run us_screener_ui.py`(家用 IP 不被擋)

**Q: (2026-06-01)為什麼免費版的 Finnhub / MarketData 一直 403/402?**
A: 期權鏈是這兩家的吸金產品,免費版完全不開放。Finnhub 要 Premium($25+/月)、MarketData 要 Trader($100/月)。我們程式碼留著 token 接口,有需要可以加值升級啟用。Streamlit Secrets 不設這兩個 token 完全不影響(只走 yfinance)。

---

## 🔒 風險警語

本系統提供的工具是**輔助判斷**,**不構成投資建議**。

**期權買方統計上約 70-80% 最終歸零**,主因:時間價值衰減 + IV crush。請務必:

1. 先用券商模擬帳戶練習至少 1-2 個月
2. 真實交易單筆風險 ≤ 總資金 2%
3. 進場前算清楚最大虧損你能不能接受
4. 設定停損並嚴格執行

切到「📚 期權新手教學」頁籤閱讀完整新手指南。
