import warnings
warnings.filterwarnings("ignore")

import streamlit as st
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import datetime
import io
import json
import requests

POSITIONS_FILE = Path("positions") / "positions.json"
SCANS_DIR = Path("scans")

st.set_page_config(page_title="美股量化選股終端機", page_icon="🇺🇸", layout="wide")
CACHE_DIR = Path("cache")

# 自定義美化 CSS
st.markdown("""<style>
    .main { background-color: #f8f9fa; }
    .stButton>button { background-color: #007bff; color: white; border-radius: 8px; font-weight: bold; }
    .stDownloadButton>button { background-color: #28a745 !important; color: white !important; }
</style>""", unsafe_allow_html=True)

@st.cache_data(ttl=3600)
def _read_parquet_cached(path_str: str, mtime_key: float):
    return pd.read_parquet(path_str)

@st.cache_data(ttl=3600, show_spinner="正在從雲端下載市場資料（~15 MB）...")
def _download_parquet_from_url(url: str):
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    return pd.read_parquet(io.BytesIO(resp.content))

def load_stock_data():
    local_file = CACHE_DIR / "us_daily.parquet"
    if local_file.exists():
        try:
            mtime_ts = local_file.stat().st_mtime
            df = _read_parquet_cached(str(local_file), mtime_ts)
            return df, datetime.fromtimestamp(mtime_ts)
        except Exception as e:
            st.error(f"⚠️ 快取檔損毀：{e}")
            return None, None
    # 雲端模式：從 GitHub Releases 下載
    try:
        parquet_url = st.secrets.get("PARQUET_URL", "")
    except Exception:
        parquet_url = ""
    if not parquet_url:
        st.error("⚠️ 找不到本機快取，也未設定 PARQUET_URL。"
                 "請執行 fetch_cache_us.py，或在 Streamlit Cloud Secrets 設定 PARQUET_URL。")
        return None, None
    try:
        df = _download_parquet_from_url(parquet_url)
        return df, datetime.now()
    except Exception as e:
        st.error(f"⚠️ 雲端資料下載失敗：{e}")
        return None, None

@st.cache_data(ttl=3600, show_spinner=False)
def compute_rs_ratings(_df_daily: pd.DataFrame, mtime_key: float) -> dict:
    """
    IBD 風格 RS Rating：1-99 百分位排名
    加權公式：63日(40%) + 126日(20%) + 189日(20%) + 252日(20%) 價格相對變化
    底線參數名告訴 streamlit 跳過 DataFrame hash，僅以 mtime_key 為 cache key
    """
    raw_scores = {}
    weights_periods = [(0.4, 63), (0.2, 126), (0.2, 189), (0.2, 252)]
    grouped = _df_daily.sort_values(['stock_id', 'date']).groupby('stock_id', sort=False)
    for sid, g in grouped:
        closes = g['close'].reset_index(drop=True)
        n = len(closes)
        if n < 63 or pd.isna(closes.iloc[-1]) or closes.iloc[-1] <= 0:
            continue
        price_now = float(closes.iloc[-1])
        score = 0.0; total_w = 0.0
        for w, p in weights_periods:
            if n > p:
                past = closes.iloc[-(p + 1)]
                if pd.notna(past) and past > 0:
                    score += w * (price_now / past)
                    total_w += w
        if total_w > 0:
            raw_scores[sid] = score / total_w
    if not raw_scores:
        return {}
    ranked = pd.Series(raw_scores).rank(pct=True) * 98 + 1
    return ranked.round().astype(int).to_dict()

@st.cache_data(ttl=3600, show_spinner=False)
def get_market_status():
    """
    CAN SLIM 中的 M (Market Direction) 檢測
    回傳大盤健康度紅綠燈 + 詳細指標
    """
    try:
        spy = yf.download("^GSPC", period="1y", progress=False)
        if isinstance(spy.columns, pd.MultiIndex):
            spy.columns = spy.columns.get_level_values(0)
        if spy.empty: return None

        spy_close = spy['Close']; spy_vol = spy['Volume']
        price_now = float(spy_close.iloc[-1])
        ma200 = float(spy_close.tail(200).mean()) if len(spy_close) >= 200 else float(spy_close.mean())
        ma50 = float(spy_close.tail(50).mean()) if len(spy_close) >= 50 else float(spy_close.mean())

        above_200ma = price_now > ma200
        above_50ma = price_now > ma50

        # Distribution Days：近 25 日 SPY 跌 >0.2% 且量 > 50 日均量
        pct_change = spy_close.pct_change()
        avg_vol_50 = spy_vol.rolling(50).mean()
        dist_mask = (pct_change < -0.002) & (spy_vol > avg_vol_50)
        dist_days = int(dist_mask.tail(25).sum())

        # MA50 是否上揚
        if len(spy_close) >= 70:
            ma50_20d_ago = float(spy_close.iloc[-70:-20].mean())
            ma50_up = ma50 > ma50_20d_ago
        else:
            ma50_up = True

        # VIX 波動度
        vix_now = None
        try:
            vix = yf.download("^VIX", period="1mo", progress=False)
            if isinstance(vix.columns, pd.MultiIndex):
                vix.columns = vix.columns.get_level_values(0)
            if not vix.empty:
                vix_now = float(vix['Close'].iloc[-1])
        except Exception:
            pass

        # 紅綠燈判定
        if above_200ma and above_50ma and dist_days < 4 and ma50_up:
            light = "🟢"; label = "多頭健康"; msg = "市況良好，可積極選股建倉"
        elif above_200ma and dist_days < 6:
            light = "🟡"; label = "警示"; msg = "派發日累積中，建議降低持倉、嚴選滿分標的"
        else:
            light = "🔴"; label = "空頭/弱勢"; msg = "大盤弱勢，建議空手觀望，僅做最頂級設定"

        return {
            "light": light, "label": label, "msg": msg,
            "spy_price": price_now, "ma200": ma200, "ma50": ma50,
            "above_200ma": above_200ma, "above_50ma": above_50ma,
            "dist_days": dist_days, "ma50_up": ma50_up, "vix": vix_now,
        }
    except Exception:
        return None

# ============================================================
# D+E. 持倉管理
# ============================================================
def load_positions():
    if "positions" not in st.session_state:
        if POSITIONS_FILE.exists():
            try:
                st.session_state.positions = json.loads(POSITIONS_FILE.read_text(encoding='utf-8'))
            except Exception:
                st.session_state.positions = []
        else:
            st.session_state.positions = []
    return st.session_state.positions

def save_positions(positions):
    st.session_state.positions = positions
    try:  # 本機同時寫檔；雲端環境失敗則忽略
        POSITIONS_FILE.parent.mkdir(exist_ok=True)
        POSITIONS_FILE.write_text(json.dumps(positions, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    except Exception:
        pass

def evaluate_position(pos, df_daily, rs_ratings):
    """評估單筆持倉的目前狀態 + 出場警示"""
    sid = pos['sid']
    g = df_daily[df_daily['stock_id'] == sid].sort_values('date').reset_index(drop=True)
    if g.empty:
        return None

    closes = g['close']; opens = g['open']; vols = g['Trading_Volume']
    price_now = float(closes.iloc[-1])
    entry_price = float(pos['entry_price'])
    entry_date_str = pos['entry_date']
    last_date_str = str(g['date'].iloc[-1])

    ret_pct = (price_now / entry_price - 1) * 100

    try:
        days_held = (pd.Timestamp(last_date_str) - pd.Timestamp(entry_date_str)).days
    except Exception:
        days_held = 0

    ma50 = float(closes.tail(50).mean()) if len(g) >= 50 else 0.0
    avg_vol_20 = float(vols.tail(20).mean()) if len(g) >= 20 else 0.0
    vol_now = float(vols.iloc[-1]) if pd.notna(vols.iloc[-1]) else 0.0

    # 有效停損 = max(-7% 鐵則停損, 使用者設定停損)
    stop_7pct = entry_price * 0.93
    stop_custom = float(pos.get('stop') or stop_7pct)
    effective_stop = max(stop_7pct, stop_custom)
    stop_dist_pct = (price_now / effective_stop - 1) * 100

    rs_rating = int(rs_ratings.get(sid, 0)) if rs_ratings else 0

    # === 出場警示 ===
    alerts = []
    if price_now <= effective_stop:
        alerts.append("🔴 觸發停損")
    if ma50 > 0 and price_now < ma50:
        alerts.append("🔴 跌破MA50")
    if rs_rating > 0 and rs_rating < 50:
        alerts.append("🟡 RS<50")
    if avg_vol_20 > 0 and vol_now > avg_vol_20 * 2 and closes.iloc[-1] < opens.iloc[-1]:
        alerts.append("🟡 量大長黑")
    if days_held > 56 and ret_pct < 5:
        alerts.append("🟡 8週未動")
    # 加碼訊號
    if len(g) >= 30:
        high_30 = float(closes.iloc[:-1].tail(30).max())
        if price_now >= high_30 * 0.995 and avg_vol_20 > 0 and vol_now > avg_vol_20:
            alerts.append("🟢 創30日新高")
    target = pos.get('target')
    if target and price_now >= float(target):
        alerts.append("🟢 達目標價")

    return {
        "代號": sid,
        "進場日": entry_date_str,
        "持有日": days_held,
        "進場價": round(entry_price, 2),
        "現價": round(price_now, 2),
        "報酬%": round(ret_pct, 2),
        "停損": round(effective_stop, 2),
        "距停損%": round(stop_dist_pct, 2),
        "MA50": round(ma50, 2),
        "RS Rating": rs_rating,
        "警示": " ".join(alerts) if alerts else "✓ 正常",
    }

# ============================================================
# F. 績效回測
# ============================================================
@st.cache_data(ttl=3600, show_spinner=False)
def run_backtest(_df_daily, mtime_key: float):
    """讀取 scans/ 歷史快照，與當前快取的後續價格比對，計算 1/4/12 週報酬"""
    if not SCANS_DIR.exists():
        return None
    scan_files = sorted(SCANS_DIR.glob("*.parquet"))
    if not scan_files:
        return None

    # 建立 (stock_id, date) → close 的查表
    df_lookup = _df_daily.set_index(['stock_id', 'date'])['close']
    ticker_dates = _df_daily.groupby('stock_id')['date'].apply(list).to_dict()

    rows = []
    for scan_file in scan_files:
        try:
            picks = pd.read_parquet(scan_file)
        except Exception:
            continue

        # 優先取檔內 scan_date 欄位（新版檔名含時戳）；無則回退到檔名前 10 字
        if 'scan_date' in picks.columns and len(picks) > 0:
            scan_date = str(picks['scan_date'].iloc[0])
        else:
            scan_date = scan_file.stem[:10]  # YYYY-MM-DD

        for _, pick in picks.iterrows():
            sid = pick.get('代號')
            entry_price = float(pick.get('現價', 0))
            if not sid or entry_price <= 0:
                continue

            dates_list = ticker_dates.get(sid, [])
            if not dates_list:
                continue
            # 找到 scan_date 在這檔的位置
            future_dates = [d for d in dates_list if d >= scan_date]
            if not future_dates:
                continue
            start_idx = dates_list.index(future_dates[0])

            for n_days, label in [(5, "1週"), (20, "4週"), (60, "12週")]:
                end_idx = start_idx + n_days
                if end_idx >= len(dates_list):
                    continue
                end_date = dates_list[end_idx]
                try:
                    end_price = float(df_lookup.loc[(sid, end_date)])
                except KeyError:
                    continue
                ret = (end_price / entry_price - 1) * 100
                rows.append({
                    "scan_date": scan_date, "sid": sid, "horizon": label,
                    "return_pct": ret, "score": int(pick.get('總分', 0)),
                    "rs_rating": int(pick.get('RS Rating', 0)),
                    "vcp": pick.get('VCP收縮', '-') == '🧨',
                    "stage2": pick.get('Stage2', '-') == '🏗️',
                    "power_day": pick.get('PowerDay', '-') == '⚡',
                    "break_": pick.get('突破', '-') == '🚀',
                    "pullback": pick.get('回檔', '-') == '🎣',
                })

    return pd.DataFrame(rows) if rows else None

@st.cache_data(ttl=3600)
def load_market_index(ticker="^GSPC"):
    try:
        data = yf.download(ticker, period="180d", progress=False)
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)
        if data.empty: return None
        if data.index.tz is not None: data.index = data.index.tz_localize(None)
        return data['Close'].sort_index()
    except Exception: return None

# --- Sidebar UI ---
st.sidebar.title("🇺🇸 策略控制中心")
strategy_mode = st.sidebar.selectbox("🎯 選擇交易心法", options=['BREAKOUT', 'PULLBACK', 'COMBO'],
    format_func=lambda x: {'BREAKOUT':'🚀 突破動能','PULLBACK':'🎣 回檔轉強','COMBO':'📊 綜合計分'}[x])

st.sidebar.markdown("---")
min_price = st.sidebar.number_input("最低股價 (USD)", value=10.0)
min_vol = st.sidebar.slider("最低均量 (萬股)", 10, 500, 100) * 10000
require_ma60 = st.sidebar.toggle("強制站上季線 (MA60)", value=True)

st.sidebar.markdown("### 🏆 品質過濾（第一波）")
filter_52w_high = st.sidebar.toggle("距 52 週新高 ≤ 25%", value=True,
    help="Mark Minervini 經典條件：真正強勢股距離 52 週高都在 25% 之內")
filter_ma_stack = st.sidebar.toggle("多頭排列 (MA5>MA20>MA60)", value=True,
    help="短中長期趨勢一致向上")
filter_ma60_slope = st.sidebar.toggle("季線上揚", value=True,
    help="MA60 比 20 個交易日前高 = 真正的上升趨勢，而非橫盤")

st.sidebar.markdown("### 💎 籌碼計分（第二波・軟計分）")
rs_threshold = st.sidebar.slider("RS Rating 加分閾值 (1-99)", 1, 99, 80,
    help="達到此分數才算「強勢加分」。原本是硬過濾，現改為計分項，不再剔除標的")
ud_threshold = st.sidebar.slider("U/D Ratio 加分閾值", 0.5, 3.0, 1.25, 0.05,
    help="達到此倍數才算「籌碼集中加分」。>1.25 表示機構吃貨，<1.0 為派發")

st.sidebar.markdown("### 💼 可執行性（下單參數）")
min_dollar_vol_m = st.sidebar.slider("最低日成交額 (百萬美元)", 1.0, 100.0, 5.0, 0.5,
    help="Minervini 建議 >$1M。預設 $5M 確保進出無滑點")
rr_target = st.sidebar.slider("R/R 目標倍數", 1.5, 5.0, 2.0, 0.5,
    help="目標獲利 = R/R × 預期風險。2.0 = 賠 1 賺 2")
atr_stop_mult = st.sidebar.slider("停損 ATR 倍數", 1.5, 4.0, 2.5, 0.5,
    help="停損 = 現價 - N × ATR（與 MA20 取較高者）")

st.sidebar.markdown("### 🎯 總分過濾（適用所有模式）")
# 軟計分後總分上限為 9（基本 4 + 籌碼 2 + 進階型態 3）
default_score = 5 if strategy_mode == 'COMBO' else 1
pass_score = st.sidebar.slider("最低總分 (滿分9分)", 1, 9, default_score,
    help="COMBO 模式：通過條件。\nBREAKOUT/PULLBACK 模式：在策略條件之外，額外要求最低總分。設 1 = 不額外過濾。\n"
         "計分項：型態(1) + 量增紅K(1) + 短期RS(1) + 乖離冷卻(1) + RS Rating(1) + U/D Ratio(1) + VCP(1) + Stage2(1) + PowerDay(1)")

st.sidebar.markdown("---")
st.sidebar.markdown("### 💾 持倉備份")
_pos_now = load_positions()
if _pos_now:
    st.sidebar.download_button(
        label=f"📥 下載持倉 JSON（{len(_pos_now)} 筆）",
        data=json.dumps(_pos_now, ensure_ascii=False, indent=2, default=str),
        file_name="positions.json",
        mime="application/json",
        help="下載後妥善保存，下次重開 App 時可透過下方按鈕還原",
    )
_uploaded_pos = st.sidebar.file_uploader("📤 還原持倉 JSON", type="json", key="pos_upload")
if _uploaded_pos is not None:
    try:
        _imported = json.loads(_uploaded_pos.read().decode("utf-8"))
        save_positions(_imported)
        st.sidebar.success(f"✓ 已載入 {len(_imported)} 筆持倉")
        st.rerun()
    except Exception as _e:
        st.sidebar.error(f"讀取失敗：{_e}")

# --- Main Logic ---
st.title("美股量化選股終端機")

# === 大盤狀態紅綠燈 (CAN SLIM 的 M) ===
market = get_market_status()
if market:
    c1, c2, c3, c4, c5 = st.columns([2, 1, 1, 1, 1])
    c1.metric("🚦 大盤狀態", f"{market['light']} {market['label']}")
    c2.metric("SPY", f"${market['spy_price']:.0f}",
              delta=f"{(market['spy_price']/market['ma200']-1)*100:+.1f}% vs 200MA")
    c3.metric("vs 50MA",
              "站上" if market['above_50ma'] else "跌破",
              delta="上揚" if market['ma50_up'] else "下彎",
              delta_color="normal" if market['ma50_up'] else "inverse")
    c4.metric("派發日(近25)", market['dist_days'],
              delta="健康" if market['dist_days'] < 4 else ("警示" if market['dist_days'] < 6 else "危險"),
              delta_color="inverse" if market['dist_days'] >= 4 else "normal")
    c5.metric("VIX", f"{market['vix']:.1f}" if market['vix'] else "N/A",
              delta="低波動" if market['vix'] and market['vix'] < 20 else ("高波動" if market['vix'] and market['vix'] >= 25 else "中性"),
              delta_color="normal" if market['vix'] and market['vix'] < 20 else "inverse")

    if "🔴" in market['light']:
        st.error(f"⚠️ {market['msg']}")
    elif "🟡" in market['light']:
        st.warning(f"⚠️ {market['msg']}")
    else:
        st.success(f"✓ {market['msg']}")

df_daily, cache_mtime = load_stock_data()

if df_daily is None:
    st.error("⚠️ 請先執行 fetch_cache_us.py 更新資料。"); st.stop()

# 提示快取新鮮度
age_hours = (datetime.now() - cache_mtime).total_seconds() / 3600
if age_hours > 24:
    st.warning(f"⏰ 快取已過期 {age_hours:.1f} 小時（更新於 {cache_mtime:%Y-%m-%d %H:%M}），建議重跑 fetch_cache_us.py")
else:
    st.caption(f"📅 快取更新於 {cache_mtime:%Y-%m-%d %H:%M}（{age_hours:.1f} 小時前）")

if st.button("🚀 執行量化掃描"):
    with st.spinner("運算中..."):
        market_close = load_market_index("^GSPC")

        # 預計算 IBD RS Rating（全市場一次性排名，會被 cache 1 小時）
        rs_ratings = compute_rs_ratings(df_daily, cache_mtime.timestamp())
        st.caption(f"📊 已對 {len(rs_ratings)} 檔標的計算 IBD RS Rating")

        results = []
        progress_bar = st.progress(0)

        # 使用 groupby 一次性切分，避免每次 boolean mask 掃全表（O(N²) → O(N)）
        grouped = df_daily.sort_values(['stock_id', 'date']).groupby('stock_id', sort=False)
        total = len(grouped)

        for i, (sid, g) in enumerate(grouped):
            if i % 25 == 0: progress_bar.progress(i / total)

            if len(g) < 60: continue
            g = g.reset_index(drop=True)

            closes = g['close']; vols = g['Trading_Volume']; dates = g['date']

            # [Bug 修正] NaN 防呆：休市或剛 IPO 標的會讓 int(NaN) 崩潰
            if pd.isna(closes.iloc[-1]) or pd.isna(vols.iloc[-1]): continue

            price_now = float(closes.iloc[-1]); vol_now = int(vols.iloc[-1])
            avg_vol_20 = vols.tail(20).mean()
            if pd.isna(avg_vol_20) or avg_vol_20 <= 0: continue

            # 過濾
            if price_now < min_price or avg_vol_20 < min_vol: continue
            ma60 = float(closes.tail(60).mean())
            if require_ma60 and price_now < ma60: continue

            # 指標
            ma20 = float(closes.tail(20).mean()); ma20_y = float(closes.iloc[:-1].tail(20).mean())
            bias_pct = (price_now / ma20 - 1) * 100

            # === 第一波品質過濾訊號 ===
            # 1. 52週新高距離（用快取內最高價作 52w high 近似）
            high_52w = float(closes.max())
            near_high_pct = (price_now / high_52w) * 100 if high_52w > 0 else 0.0
            sig_near_high = near_high_pct >= 75.0

            # 2. 多頭排列
            ma5 = float(closes.tail(5).mean())
            sig_ma_stack = (ma5 > ma20) and (ma20 > ma60)

            # 3. 季線斜率：MA60 比 20 個交易日前更高
            if len(g) >= 80:
                ma60_20d_ago = float(closes.iloc[-80:-20].mean())
                sig_ma60_up = ma60 > ma60_20d_ago
            else:
                sig_ma60_up = False  # 資料不足保守視為 False

            # 品質過濾（任一未通過即剔除）
            if filter_52w_high and not sig_near_high: continue
            if filter_ma_stack and not sig_ma_stack: continue
            if filter_ma60_slope and not sig_ma60_up: continue

            # === 第二波籌碼計分（軟計分，不過濾）===
            # 4. U/D Volume Ratio：近 50 日 上漲日總量 / 下跌日總量
            last_50 = g.tail(50)
            deltas = last_50['close'].diff()
            up_vol = last_50.loc[deltas > 0, 'Trading_Volume'].sum()
            dn_vol = last_50.loc[deltas < 0, 'Trading_Volume'].sum()
            ud_ratio = (up_vol / dn_vol) if dn_vol > 0 else float('inf')
            sig_ud_strong = ud_ratio >= ud_threshold

            # 5. IBD RS Rating：標準化相對強度排名
            rs_rating = rs_ratings.get(sid, 0)
            sig_rs_rating_strong = rs_rating >= rs_threshold

            sig_break = (price_now >= float(closes.iloc[:-1].tail(20).max()) * 0.995) and (vol_now >= avg_vol_20 * 1.5)
            sig_pullback = (price_now > ma20) and (bias_pct <= 5.0) and (ma20 > ma20_y)

            # === 第三波進階型態（軟計分）===
            # 6. VCP 波動收縮（O'Neil / Minervini 經典）
            if len(g) >= 20:
                hi_4w = float(closes.tail(20).max()); lo_4w = float(closes.tail(20).min())
                hi_2w = float(closes.tail(10).max()); lo_2w = float(closes.tail(10).min())
                range_4w = (hi_4w / lo_4w - 1) * 100 if lo_4w > 0 else 0
                range_2w = (hi_2w / lo_2w - 1) * 100 if lo_2w > 0 else 0
                sig_vcp = (range_2w < range_4w * 0.6) and (range_2w < 8) and (range_4w > 0)
            else:
                sig_vcp = False; range_2w = 0; range_4w = 0

            # 7. Stage 2 Breakout：近 30 日盤整 + 今日突破 30 日新高
            if len(g) >= 30:
                hi_30d = float(closes.tail(30).max()); lo_30d = float(closes.tail(30).min())
                range_30d = (hi_30d / lo_30d - 1) * 100 if lo_30d > 0 else 0
                hi_30d_excl_today = float(closes.iloc[:-1].tail(30).max())
                sig_stage2 = (range_30d < 15) and (price_now >= hi_30d_excl_today * 0.995)
            else:
                sig_stage2 = False

            # 8. Power Day（簡化版 Power Earnings Gap：近 5 日有單日 ≥5% 跳空且量爆 2x）
            if len(g) >= 6:
                last_6 = g.tail(6).reset_index(drop=True)
                gaps = (last_6['open'].iloc[1:].values / last_6['close'].iloc[:-1].values - 1) * 100
                day_change = (last_6['close'].iloc[1:].values / last_6['open'].iloc[1:].values - 1) * 100
                vols_5 = last_6['Trading_Volume'].iloc[1:].values
                sig_power_day = bool(((gaps > 5) & (day_change > 0) & (vols_5 > avg_vol_20 * 2)).any())
            else:
                sig_power_day = False

            # === 可執行性計算（ATR / 停損 / 目標 / R/R / 流動性）===
            # ATR(14)
            if len(g) >= 15:
                high_s = g['max'].astype(float); low_s = g['min'].astype(float)
                close_s = closes.astype(float)
                prev_close = close_s.shift(1)
                tr = pd.concat([high_s - low_s, (high_s - prev_close).abs(), (low_s - prev_close).abs()],
                               axis=1).max(axis=1)
                atr14 = float(tr.tail(14).mean())
            else:
                atr14 = price_now * 0.02  # 預設 2%
            atr_pct = (atr14 / price_now * 100) if price_now > 0 else 0

            # 建議停損：max(ATR 停損, MA20×0.99) — 取較高者 = 較小風險
            stop_atr = price_now - atr_stop_mult * atr14
            stop_ma20 = ma20 * 0.99
            suggested_stop = max(stop_atr, stop_ma20)
            risk_amount = price_now - suggested_stop
            risk_pct = (risk_amount / price_now * 100) if price_now > 0 and risk_amount > 0 else 0

            # 建議目標：依 R/R 倍數
            target_price = price_now + risk_amount * rr_target
            gain_pct = (target_price / price_now - 1) * 100 if price_now > 0 else 0

            # 流動性過濾：日成交額（百萬美元）
            dollar_vol_m = (avg_vol_20 * price_now) / 1_000_000
            if dollar_vol_m < min_dollar_vol_m: continue

            # [Bug 修正] sig_vol 必須是「量增 + 紅K」，避免放量下殺被算成強勢
            sig_vol = (
                (vols.iloc[-1] > avg_vol_20 * 1.2 and closes.iloc[-1] > closes.iloc[-2]) and
                (vols.iloc[-2] > avg_vol_20 * 1.2 and closes.iloc[-2] > closes.iloc[-3])
            )
            sig_cool = bias_pct < 15.0

            # 短期 RS：與大盤 21 日比較
            sig_rs = False; rs_val = 0.0
            if market_close is not None:
                try:
                    m_end = market_close.asof(pd.Timestamp(dates.iloc[-1]))
                    m_start = market_close.asof(pd.Timestamp(dates.iloc[-21]))
                except Exception:
                    m_end = m_start = None
                if pd.notna(m_end) and pd.notna(m_start) and m_start > 0 and closes.iloc[-21] > 0:
                    s_change = (price_now / closes.iloc[-21] - 1) * 100
                    m_change = (m_end / m_start - 1) * 100
                    rs_val = s_change - m_change
                    sig_rs = (rs_val > 0)
            
            # 模式判定
            # 總分上限 9：型態(1) + 量增紅K(1) + 短期RS(1) + 乖離冷卻(1)
            #         + RS Rating(1) + U/D Ratio(1) + VCP(1) + Stage2(1) + Power Day(1)
            sig_setup = sig_break or sig_pullback
            score = (int(sig_setup) + int(sig_vol) + int(sig_rs) + int(sig_cool)
                     + int(sig_rs_rating_strong) + int(sig_ud_strong)
                     + int(sig_vcp) + int(sig_stage2) + int(sig_power_day))
            passed = False
            if strategy_mode == 'BREAKOUT':
                # 突破動能：嚴格進場條件
                passed = sig_break and sig_cool and sig_rs
            elif strategy_mode == 'PULLBACK':
                # 回檔轉強：嚴格進場條件
                passed = sig_pullback and sig_rs
            else:
                # COMBO：分數制 + 型態必備
                passed = sig_setup

            # 所有模式共通：最低總分過濾（pass_score=1 等同不過濾）
            if passed and score < pass_score:
                passed = False
            
            if passed:
                results.append({
                    "代號": sid,
                    "現價": round(price_now, 2),
                    "總分": score,
                    "RS Rating": rs_rating,
                    "U/D Ratio": round(ud_ratio, 2) if ud_ratio != float('inf') else 99.0,
                    "建議停損": round(suggested_stop, 2),
                    "建議目標": round(target_price, 2),
                    "風險%": round(risk_pct, 2),
                    "獲利空間%": round(gain_pct, 1),
                    "ATR%": round(atr_pct, 2),
                    "日成交額(M$)": round(dollar_vol_m, 1),
                    "距52週高(%)": round(near_high_pct, 1),
                    "RS差值(%)": round(rs_val, 2),
                    "月線乖離(%)": round(bias_pct, 2),
                    "突破": "🚀" if sig_break else "-",
                    "回檔": "🎣" if sig_pullback else "-",
                    "VCP收縮": "🧨" if sig_vcp else "-",
                    "Stage2": "🏗️" if sig_stage2 else "-",
                    "PowerDay": "⚡" if sig_power_day else "-",
                    "強RS加分": "💎" if sig_rs_rating_strong else "-",
                    "吃貨加分": "🏦" if sig_ud_strong else "-",
                    "多頭排列": "✓" if sig_ma_stack else "-",
                    "季線上揚": "📈" if sig_ma60_up else "-",
                    "20日均量(萬)": round(avg_vol_20/10000, 1),
                })

        progress_bar.progress(1.0)
        if results:
            df_res = pd.DataFrame(results).sort_values(
                by=["總分", "RS Rating", "U/D Ratio"], ascending=[False, False, False]
            )
            st.success(f"🎊 找到 {len(df_res)} 檔標的")

            # === F. 自動存檔供回測（檔名含時間戳+策略，避免同日覆寫）===
            SCANS_DIR.mkdir(exist_ok=True)
            scan_filename = SCANS_DIR / f"{datetime.now():%Y-%m-%d_%H%M%S}_{strategy_mode}.parquet"
            df_res_save = df_res.copy()
            df_res_save['mode'] = strategy_mode
            df_res_save['scan_time'] = datetime.now().isoformat()
            df_res_save['scan_date'] = datetime.now().strftime('%Y-%m-%d')
            df_res_save.to_parquet(scan_filename)
            st.caption(f"💾 已存檔至 {scan_filename.name}（供回測模組使用）")

            st.dataframe(df_res, use_container_width=True, hide_index=True)
            
            # 下載與 Firstrade 複製區
            col1, col2 = st.columns(2)
            with col1:
                buf = io.BytesIO()
                with pd.ExcelWriter(buf, engine='openpyxl') as writer: df_res.to_excel(writer, index=False)
                st.download_button("📥 下載 Excel 報表", buf.getvalue(), f"US_Scan_{datetime.now().strftime('%m%d')}.xlsx")
            with col2:
                st.subheader("🏦 Firstrade 快速複製")
                st.code(", ".join(df_res["代號"].tolist()), language="text")
        else:
            st.warning("☹️ 無符合標的，請放寬條件。")

# ============================================================
# 📌 D+E. 持倉管理
# ============================================================
st.divider()
with st.expander("📌 我的持倉", expanded=False):
    positions = load_positions()

    # 新增持倉表單
    with st.form("add_position", clear_on_submit=True):
        st.markdown("**新增持倉**")
        pcols = st.columns([1.5, 1, 1, 1.2, 1, 1])
        new_sid = pcols[0].text_input("代號", placeholder="例如 NVDA").strip().upper()
        new_entry_price = pcols[1].number_input("進場價", min_value=0.01, value=100.0, step=0.01, format="%.2f")
        new_shares = pcols[2].number_input("股數", min_value=1, value=10, step=1)
        new_entry_date = pcols[3].date_input("進場日", value=datetime.now().date())
        new_stop = pcols[4].number_input("停損價(選填)", min_value=0.0, value=0.0, step=0.01, format="%.2f",
            help="留 0 = 自動用 -7% 鐵則。\n注意：實際生效停損 = max(你輸入的停損, 進場價×0.93)。"
                 "比 -7% 寬鬆的停損會被自動緊縮（Minervini 鐵則）。"
                 "若想設更緊停損（例如 -5%），輸入 進場價×0.95 即可生效。")
        new_target = pcols[5].number_input("目標價(選填)", min_value=0.0, value=0.0, step=0.01, format="%.2f")
        st.caption("💡 停損會自動緊縮為「進場價 × 0.93（-7% 鐵則）」與「你輸入的停損」之較緊者。"
                   "若想設定更緊停損，請直接輸入該價位。")
        submitted = st.form_submit_button("➕ 加入持倉")
        if submitted and new_sid:
            positions.append({
                "sid": new_sid,
                "entry_price": float(new_entry_price),
                "shares": int(new_shares),
                "entry_date": new_entry_date.isoformat(),
                "stop": float(new_stop) if new_stop > 0 else None,
                "target": float(new_target) if new_target > 0 else None,
            })
            save_positions(positions)
            st.success(f"✓ 已加入 {new_sid}")
            st.rerun()

    # 顯示現有持倉
    if not positions:
        st.info("📭 目前沒有持倉。在上方表單新增。")
    else:
        # 預計算 RS Rating（複用 cache）
        rs_ratings_for_pos = compute_rs_ratings(df_daily, cache_mtime.timestamp())
        rows = []
        missing_sids = []
        for pos in positions:
            r = evaluate_position(pos, df_daily, rs_ratings_for_pos)
            if r:
                rows.append(r)
            else:
                missing_sids.append(pos['sid'])

        # [Fix 4] 缺失代號顯式警告
        if missing_sids:
            st.warning(f"⚠️ 以下持倉的代號在快取中找不到，無法評估："
                       f"{', '.join(missing_sids)}（可能拼寫錯誤或已從指數移除）")

        if rows:
            df_pos = pd.DataFrame(rows)

            # 彙總統計
            total_alert = sum(1 for r in rows if "🔴" in r['警示'])
            total_warn = sum(1 for r in rows if "🟡" in r['警示'] and "🔴" not in r['警示'])
            avg_ret = df_pos['報酬%'].mean()
            mcols = st.columns(4)
            mcols[0].metric("持倉檔數", len(rows))
            mcols[1].metric("平均報酬", f"{avg_ret:+.2f}%")
            mcols[2].metric("🔴 嚴重警示", total_alert)
            mcols[3].metric("🟡 一般警示", total_warn)

            st.dataframe(df_pos, use_container_width=True, hide_index=True)

            # 移除持倉
            st.markdown("**移除持倉**")
            del_sid = st.selectbox("選擇要移除的代號", options=[p['sid'] for p in positions], key="del_pos")
            if st.button("🗑️ 移除"):
                positions = [p for p in positions if p['sid'] != del_sid]
                save_positions(positions)
                st.success(f"✓ 已移除 {del_sid}")
                st.rerun()
        else:
            st.warning("⚠️ 所有持倉的代號都不在快取中，無法評估。")

# ============================================================
# 📈 F. 績效回測
# ============================================================
st.divider()
with st.expander("📈 績效回測", expanded=False):
    st.caption("讀取 scans/ 歷史掃描快照，分析訊號的後續表現。需累積至少 5-12 週的歷史快照才有統計意義。")
    st.info("☁️ 雲端版本：本次工作階段執行的掃描會自動記錄到 scans/，但 App 重啟後消失。"
            "建議每週將下方 Excel 報表存檔，日後可透過「上傳掃描快照」還原回測資料。")

    _scan_uploads = st.file_uploader(
        "📤 上傳歷史掃描快照（.parquet，可多選）",
        type="parquet",
        accept_multiple_files=True,
        key="scan_upload",
    )
    if _scan_uploads:
        SCANS_DIR.mkdir(exist_ok=True)
        for _sf in _scan_uploads:
            (SCANS_DIR / _sf.name).write_bytes(_sf.read())
        st.success(f"✓ 已上傳 {len(_scan_uploads)} 個掃描快照，請按下方「刷新回測」")
    if st.button("🔄 刷新回測資料"):
        st.cache_data.clear()
        st.rerun()

    df_bt = run_backtest(df_daily, cache_mtime.timestamp())
    if df_bt is None or df_bt.empty:
        st.info("📭 尚無回測資料。每次掃描完成後會自動存到 scans/，累積數週後回來看。")
    else:
        st.success(f"📊 共 {df_bt['sid'].nunique()} 檔、{df_bt['scan_date'].nunique()} 個掃描日、{len(df_bt)} 筆觀察")

        # 三個時間框架的彙總
        for horizon in ["1週", "4週", "12週"]:
            sub = df_bt[df_bt['horizon'] == horizon]
            if sub.empty: continue
            st.markdown(f"### 📅 {horizon}後表現")
            cols = st.columns(5)
            cols[0].metric("樣本數", len(sub))
            cols[1].metric("勝率", f"{(sub['return_pct'] > 0).mean()*100:.1f}%")
            cols[2].metric("中位數獲利", f"{sub['return_pct'].median():+.2f}%")
            cols[3].metric("平均獲利", f"{sub['return_pct'].mean():+.2f}%")
            cols[4].metric("最大回撤", f"{sub['return_pct'].min():+.2f}%")

            # 訊號別表現
            sig_rows = []
            for sig_col, sig_name in [('vcp', '🧨 VCP'), ('stage2', '🏗️ Stage2'),
                                       ('power_day', '⚡ PowerDay'),
                                       ('break_', '🚀 突破'), ('pullback', '🎣 回檔')]:
                for has in [True, False]:
                    subset = sub[sub[sig_col] == has]
                    if len(subset) >= 3:  # 至少 3 筆樣本才有意義
                        sig_rows.append({
                            "訊號": f"{sig_name} {'✓' if has else '✗'}",
                            "樣本數": len(subset),
                            "勝率": f"{(subset['return_pct'] > 0).mean()*100:.1f}%",
                            "中位數%": round(subset['return_pct'].median(), 2),
                            "平均%": round(subset['return_pct'].mean(), 2),
                        })
            if sig_rows:
                st.dataframe(pd.DataFrame(sig_rows), use_container_width=True, hide_index=True)

            # 分數分組表現
            score_rows = []
            for score_range, label in [((1,3), "1-3 分"), ((4,5), "4-5 分"),
                                        ((6,7), "6-7 分"), ((8,9), "8-9 分")]:
                subset = sub[(sub['score'] >= score_range[0]) & (sub['score'] <= score_range[1])]
                if len(subset) >= 3:
                    score_rows.append({
                        "分數": label,
                        "樣本數": len(subset),
                        "勝率": f"{(subset['return_pct'] > 0).mean()*100:.1f}%",
                        "中位數%": round(subset['return_pct'].median(), 2),
                        "平均%": round(subset['return_pct'].mean(), 2),
                    })
            if score_rows:
                st.caption("**分數分組表現**")
                st.dataframe(pd.DataFrame(score_rows), use_container_width=True, hide_index=True)
            st.markdown("---")