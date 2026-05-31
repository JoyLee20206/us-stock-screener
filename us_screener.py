import warnings

# ============================================================
# [環境設定] 企業網路 SSL 中間人攔截繞過 (必須在 yfinance 之前執行)
# ============================================================
warnings.filterwarnings("ignore")

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

try:
    import curl_cffi.requests as _cc
    _orig_session_init = _cc.Session.__init__
    def _patched_session_init(self, *args, **kwargs):
        _orig_session_init(self, *args, **kwargs)
        self.verify = False
    _cc.Session.__init__ = _patched_session_init
except Exception:
    pass

try:
    import ssl
    ssl._create_default_https_context = ssl._create_unverified_context
except Exception:
    pass

# ============================================================
# 模組引入與全域設定
# ============================================================
import streamlit as st
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import datetime
import io
import json

POSITIONS_FILE = Path("positions") / "positions.json"
SCANS_DIR = Path("scans")
CACHE_DIR = Path("cache")

st.set_page_config(page_title="美股量化選股終端機", page_icon="🇺🇸", layout="wide")

# 自定義美化 CSS
st.markdown("""<style>
    .main { background-color: #f8f9fa; }
    .stButton>button { background-color: #007bff; color: white; border-radius: 8px; font-weight: bold; }
    .stDownloadButton>button { background-color: #28a745 !important; color: white !important; }
</style>""", unsafe_allow_html=True)

# === 狀態初始化 (解決 Streamlit 重新整理導致資料消失的問題) ===
if "df_res" not in st.session_state:
    st.session_state.df_res = None
if "scan_time" not in st.session_state:
    st.session_state.scan_time = None

# ============================================================
# 資料載入與快取模組
# ============================================================
@st.cache_data(ttl=3600)
def _read_parquet_cached(path_str: str, mtime_key: float):
    return pd.read_parquet(path_str)

def load_stock_data():
    file_path = CACHE_DIR / "us_daily.parquet"
    if not file_path.exists():
        return None, None
    try:
        mtime_ts = file_path.stat().st_mtime
        df = _read_parquet_cached(str(file_path), mtime_ts)
        return df, datetime.fromtimestamp(mtime_ts)
    except Exception as e:
        st.error(f"⚠️ 快取檔損毀：{e}")
        return None, None

@st.cache_data(ttl=3600, show_spinner=False)
def compute_rs_ratings(_df_daily: pd.DataFrame, mtime_key: float) -> dict:
    """計算 IBD 風格 RS Rating (1-99 百分位排名)"""
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
            
    if not raw_scores: return {}
    ranked = pd.Series(raw_scores).rank(pct=True) * 98 + 1
    return ranked.round().astype(int).to_dict()

@st.cache_data(ttl=3600, show_spinner=False)
def get_market_status():
    """CAN SLIM 大盤健康度檢測 (Market Direction)"""
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

        pct_change = spy_close.pct_change()
        avg_vol_50 = spy_vol.rolling(50).mean()
        dist_mask = (pct_change < -0.002) & (spy_vol > avg_vol_50)
        dist_days = int(dist_mask.tail(25).sum())

        ma50_up = True
        if len(spy_close) >= 70:
            ma50_20d_ago = float(spy_close.iloc[-70:-20].mean())
            ma50_up = ma50 > ma50_20d_ago

        vix_now = None
        try:
            vix = yf.download("^VIX", period="1mo", progress=False)
            if isinstance(vix.columns, pd.MultiIndex):
                vix.columns = vix.columns.get_level_values(0)
            if not vix.empty:
                vix_now = float(vix['Close'].iloc[-1])
        except Exception:
            pass

        if above_200ma and above_50ma and dist_days < 4 and ma50_up:
            light, label, msg = "🟢", "多頭健康", "市況良好，可積極選股建倉"
        elif above_200ma and dist_days < 6:
            light, label, msg = "🟡", "警示", "派發日累積中，建議降低持倉、嚴選滿分標的"
        else:
            light, label, msg = "🔴", "空頭/弱勢", "大盤弱勢，建議空手觀望，僅做最頂級設定"

        return {
            "light": light, "label": label, "msg": msg, "spy_price": price_now,
            "ma200": ma200, "ma50": ma50, "above_200ma": above_200ma, 
            "above_50ma": above_50ma, "dist_days": dist_days, 
            "ma50_up": ma50_up, "vix": vix_now
        }
    except Exception:
        return None

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

# ============================================================
# 持倉與回測模組
# ============================================================
def load_positions():
    if POSITIONS_FILE.exists():
        try: return json.loads(POSITIONS_FILE.read_text(encoding='utf-8'))
        except Exception: return []
    return []

def save_positions(positions):
    POSITIONS_FILE.parent.mkdir(exist_ok=True)
    POSITIONS_FILE.write_text(json.dumps(positions, ensure_ascii=False, indent=2, default=str), encoding='utf-8')

def evaluate_position(pos, df_daily, rs_ratings):
    sid = pos['sid']
    g = df_daily[df_daily['stock_id'] == sid].sort_values('date').reset_index(drop=True)
    if g.empty: return None

    closes = g['close']; opens = g['open']; vols = g['Trading_Volume']
    price_now = float(closes.iloc[-1])
    entry_price = float(pos['entry_price'])
    
    ret_pct = (price_now / entry_price - 1) * 100
    try: days_held = (pd.Timestamp(str(g['date'].iloc[-1])) - pd.Timestamp(pos['entry_date'])).days
    except Exception: days_held = 0

    ma50 = float(closes.tail(50).mean()) if len(g) >= 50 else 0.0
    avg_vol_20 = float(vols.tail(20).mean()) if len(g) >= 20 else 0.0
    vol_now = float(vols.iloc[-1]) if pd.notna(vols.iloc[-1]) else 0.0

    stop_7pct = entry_price * 0.93
    stop_custom = float(pos.get('stop') or stop_7pct)
    effective_stop = max(stop_7pct, stop_custom)
    stop_dist_pct = (price_now / effective_stop - 1) * 100
    rs_rating = int(rs_ratings.get(sid, 0)) if rs_ratings else 0

    alerts = []
    if price_now <= effective_stop: alerts.append("🔴 觸發停損")
    if ma50 > 0 and price_now < ma50: alerts.append("🔴 跌破MA50")
    if rs_rating > 0 and rs_rating < 50: alerts.append("🟡 RS<50")
    if avg_vol_20 > 0 and vol_now > avg_vol_20 * 2 and closes.iloc[-1] < opens.iloc[-1]: alerts.append("🟡 量大長黑")
    if days_held > 56 and ret_pct < 5: alerts.append("🟡 8週未動")
    
    if len(g) >= 30:
        high_30 = float(closes.iloc[:-1].tail(30).max())
        if price_now >= high_30 * 0.995 and avg_vol_20 > 0 and vol_now > avg_vol_20:
            alerts.append("🟢 創30日新高")
            
    target = pos.get('target')
    if target and price_now >= float(target): alerts.append("🟢 達目標價")

    return {
        "代號": sid, "進場日": pos['entry_date'], "持有日": days_held,
        "進場價": round(entry_price, 2), "現價": round(price_now, 2),
        "報酬%": round(ret_pct, 2), "停損": round(effective_stop, 2),
        "距停損%": round(stop_dist_pct, 2), "MA50": round(ma50, 2),
        "RS Rating": rs_rating, "警示": " ".join(alerts) if alerts else "✓ 正常",
    }

@st.cache_data(ttl=3600, show_spinner=False)
def run_backtest(_df_daily, mtime_key: float):
    if not SCANS_DIR.exists(): return None
    scan_files = sorted(SCANS_DIR.glob("*.parquet"))
    if not scan_files: return None

    df_lookup = _df_daily.set_index(['stock_id', 'date'])['close']
    ticker_dates = _df_daily.groupby('stock_id')['date'].apply(list).to_dict()
    rows = []
    
    for scan_file in scan_files:
        try: picks = pd.read_parquet(scan_file)
        except Exception: continue

        scan_date = str(picks['scan_date'].iloc[0]) if 'scan_date' in picks.columns and len(picks) > 0 else scan_file.stem[:10]

        for _, pick in picks.iterrows():
            sid = pick.get('代號')
            entry_price = float(pick.get('現價', 0))
            if not sid or entry_price <= 0: continue

            dates_list = ticker_dates.get(sid, [])
            future_dates = [d for d in dates_list if d >= scan_date]
            if not future_dates: continue
            
            start_idx = dates_list.index(future_dates[0])
            for n_days, label in [(5, "1週"), (20, "4週"), (60, "12週")]:
                end_idx = start_idx + n_days
                if end_idx >= len(dates_list): continue
                end_date = dates_list[end_idx]
                try: end_price = float(df_lookup.loc[(sid, end_date)])
                except KeyError: continue
                
                rows.append({
                    "scan_date": scan_date, "sid": sid, "horizon": label,
                    "return_pct": (end_price / entry_price - 1) * 100,
                    "score": int(pick.get('總分', 0)),
                    "rs_rating": int(pick.get('RS Rating', 0)),
                    "vcp": pick.get('VCP收縮', '-') == '🧨',
                    "stage2": pick.get('Stage2', '-') == '🏗️',
                    "power_day": pick.get('PowerDay', '-') == '⚡',
                    "break_": pick.get('突破', '-') == '🚀',
                    "pullback": pick.get('回檔', '-') == '🎣',
                })
    return pd.DataFrame(rows) if rows else None

# ============================================================
# 左側控制面板 (Sidebar UI)
# ============================================================
st.sidebar.title("🇺🇸 策略控制中心")
strategy_mode = st.sidebar.selectbox("🎯 選擇交易心法", options=['BREAKOUT', 'PULLBACK', 'COMBO'],
    format_func=lambda x: {'BREAKOUT':'🚀 突破動能','PULLBACK':'🎣 回檔轉強','COMBO':'📊 綜合計分'}[x])

st.sidebar.markdown("---")
min_price = st.sidebar.number_input("最低股價 (USD)", value=10.0)
min_vol = st.sidebar.slider("最低均量 (萬股)", 10, 500, 100) * 10000
require_ma60 = st.sidebar.toggle("強制站上季線 (MA60)", value=True)

st.sidebar.markdown("### 🏆 品質過濾（第一波）")
filter_52w_high = st.sidebar.toggle("距 52 週新高 ≤ 25%", value=True)
filter_ma_stack = st.sidebar.toggle("多頭排列 (MA5>MA20>MA60)", value=True)
filter_ma60_slope = st.sidebar.toggle("季線上揚", value=True)

st.sidebar.markdown("### 💎 籌碼計分（第二波）")
rs_threshold = st.sidebar.slider("RS Rating 加分閾值", 1, 99, 80)
ud_threshold = st.sidebar.slider("U/D Ratio 加分閾值", 0.5, 3.0, 1.25, 0.05)

st.sidebar.markdown("### 💼 下單參數")
min_dollar_vol_m = st.sidebar.slider("最低日成交額 (百萬美元)", 1.0, 100.0, 5.0, 0.5)
rr_target = st.sidebar.slider("R/R 目標倍數", 1.5, 5.0, 2.0, 0.5)
atr_stop_mult = st.sidebar.slider("停損 ATR 倍數", 1.5, 4.0, 2.5, 0.5)

st.sidebar.markdown("### 🎯 總分過濾")
default_score = 5 if strategy_mode == 'COMBO' else 1
pass_score = st.sidebar.slider("最低總分 (滿分9分)", 1, 9, default_score)

# ============================================================
# 主畫面 (Main UI)
# ============================================================
st.title("美股量化選股終端機")

# === 大盤狀態紅綠燈 ===
market = get_market_status()
if market:
    c1, c2, c3, c4, c5 = st.columns([2, 1, 1, 1, 1])
    c1.metric("🚦 大盤狀態", f"{market['light']} {market['label']}")
    c2.metric("SPY", f"${market['spy_price']:.0f}", delta=f"{(market['spy_price']/market['ma200']-1)*100:+.1f}% vs 200MA")
    c3.metric("vs 50MA", "站上" if market['above_50ma'] else "跌破", delta="上揚" if market['ma50_up'] else "下彎", delta_color="normal" if market['ma50_up'] else "inverse")
    c4.metric("派發日(近25)", market['dist_days'], delta="健康" if market['dist_days'] < 4 else "警示", delta_color="inverse" if market['dist_days'] >= 4 else "normal")
    c5.metric("VIX", f"{market['vix']:.1f}" if market['vix'] else "N/A")
    
    if "🔴" in market['light']: st.error(f"⚠️ {market['msg']}")
    elif "🟡" in market['light']: st.warning(f"⚠️ {market['msg']}")
    else: st.success(f"✓ {market['msg']}")

df_daily, cache_mtime = load_stock_data()
if df_daily is None:
    st.error("⚠️ 請先執行 fetch_us_cache.py 更新資料。"); st.stop()

age_hours = (datetime.now() - cache_mtime).total_seconds() / 3600
if age_hours > 24: st.warning(f"⏰ 快取已過期 {age_hours:.1f} 小時，建議重跑抓取程式")
else: st.caption(f"📅 快取更新於 {cache_mtime:%Y-%m-%d %H:%M}")

# ============================================================
# 掃描運算區塊 (運算完畢寫入 session_state)
# ============================================================
if st.button("🚀 執行量化掃描", type="primary"):
    with st.spinner("系統分析運算中..."):
        market_close = load_market_index("^GSPC")
        rs_ratings = compute_rs_ratings(df_daily, cache_mtime.timestamp())
        
        results = []
        progress_bar = st.progress(0)
        grouped = df_daily.sort_values(['stock_id', 'date']).groupby('stock_id', sort=False)
        total = len(grouped)

        for i, (sid, g) in enumerate(grouped):
            if i % 25 == 0: progress_bar.progress(i / total)
            if len(g) < 60: continue
            
            g = g.reset_index(drop=True)
            closes = g['close']; vols = g['Trading_Volume']; dates = g['date']

            if pd.isna(closes.iloc[-1]) or pd.isna(vols.iloc[-1]): continue
            price_now = float(closes.iloc[-1]); vol_now = int(vols.iloc[-1])
            avg_vol_20 = vols.tail(20).mean()
            
            if pd.isna(avg_vol_20) or avg_vol_20 <= 0: continue
            if price_now < min_price or avg_vol_20 < min_vol: continue
            
            ma60 = float(closes.tail(60).mean())
            if require_ma60 and price_now < ma60: continue

            ma20 = float(closes.tail(20).mean()); ma20_y = float(closes.iloc[:-1].tail(20).mean())
            bias_pct = (price_now / ma20 - 1) * 100

            high_52w = float(closes.max())
            near_high_pct = (price_now / high_52w) * 100 if high_52w > 0 else 0.0
            sig_near_high = near_high_pct >= 75.0
            ma5 = float(closes.tail(5).mean())
            sig_ma_stack = (ma5 > ma20) and (ma20 > ma60)
            sig_ma60_up = ma60 > float(closes.iloc[-80:-20].mean()) if len(g) >= 80 else False

            if filter_52w_high and not sig_near_high: continue
            if filter_ma_stack and not sig_ma_stack: continue
            if filter_ma60_slope and not sig_ma60_up: continue

            last_50 = g.tail(50)
            deltas = last_50['close'].diff()
            up_vol = last_50.loc[deltas > 0, 'Trading_Volume'].sum()
            dn_vol = last_50.loc[deltas < 0, 'Trading_Volume'].sum()
            ud_ratio = (up_vol / dn_vol) if dn_vol > 0 else float('inf')
            sig_ud_strong = ud_ratio >= ud_threshold

            rs_rating = rs_ratings.get(sid, 0)
            sig_rs_rating_strong = rs_rating >= rs_threshold

            sig_break = (price_now >= float(closes.iloc[:-1].tail(20).max()) * 0.995) and (vol_now >= avg_vol_20 * 1.5)
            sig_pullback = (price_now > ma20) and (bias_pct <= 5.0) and (ma20 > ma20_y)

            if len(g) >= 20:
                range_4w = (float(closes.tail(20).max()) / float(closes.tail(20).min()) - 1) * 100
                range_2w = (float(closes.tail(10).max()) / float(closes.tail(10).min()) - 1) * 100
                sig_vcp = (range_2w < range_4w * 0.6) and (range_2w < 8) and (range_4w > 0)
            else: sig_vcp = False

            if len(g) >= 30:
                range_30d = (float(closes.tail(30).max()) / float(closes.tail(30).min()) - 1) * 100
                sig_stage2 = (range_30d < 15) and (price_now >= float(closes.iloc[:-1].tail(30).max()) * 0.995)
            else: sig_stage2 = False

            if len(g) >= 6:
                last_6 = g.tail(6).reset_index(drop=True)
                gaps = (last_6['open'].iloc[1:].values / last_6['close'].iloc[:-1].values - 1) * 100
                day_change = (last_6['close'].iloc[1:].values / last_6['open'].iloc[1:].values - 1) * 100
                sig_power_day = bool(((gaps > 5) & (day_change > 0) & (last_6['Trading_Volume'].iloc[1:].values > avg_vol_20 * 2)).any())
            else: sig_power_day = False

            atr14 = float(pd.concat([(g['max']-g['min']), (g['max']-closes.shift(1)).abs(), (g['min']-closes.shift(1)).abs()], axis=1).max(axis=1).tail(14).mean()) if len(g) >= 15 else price_now * 0.02
            suggested_stop = max(price_now - atr_stop_mult * atr14, ma20 * 0.99)
            risk_amount = price_now - suggested_stop
            target_price = price_now + risk_amount * rr_target
            dollar_vol_m = (avg_vol_20 * price_now) / 1_000_000
            
            if dollar_vol_m < min_dollar_vol_m: continue

            sig_vol = ((vols.iloc[-1] > avg_vol_20 * 1.2 and closes.iloc[-1] > closes.iloc[-2]) and
                       (vols.iloc[-2] > avg_vol_20 * 1.2 and closes.iloc[-2] > closes.iloc[-3]))
            sig_cool = bias_pct < 15.0

            sig_rs = False; rs_val = 0.0
            if market_close is not None:
                try: m_end = market_close.asof(pd.Timestamp(dates.iloc[-1])); m_start = market_close.asof(pd.Timestamp(dates.iloc[-21]))
                except Exception: m_end = m_start = None
                if pd.notna(m_end) and pd.notna(m_start) and m_start > 0 and closes.iloc[-21] > 0:
                    rs_val = ((price_now / closes.iloc[-21] - 1) - (m_end / m_start - 1)) * 100
                    sig_rs = (rs_val > 0)

            sig_setup = sig_break or sig_pullback
            score = (int(sig_setup) + int(sig_vol) + int(sig_rs) + int(sig_cool) + 
                     int(sig_rs_rating_strong) + int(sig_ud_strong) + int(sig_vcp) + int(sig_stage2) + int(sig_power_day))
            
            passed = False
            if strategy_mode == 'BREAKOUT': passed = sig_break and sig_cool and sig_rs
            elif strategy_mode == 'PULLBACK': passed = sig_pullback and sig_rs
            else: passed = sig_setup

            if passed and score >= pass_score:
                results.append({
                    "代號": sid, "現價": round(price_now, 2), "總分": score, "RS Rating": rs_rating,
                    "U/D Ratio": round(ud_ratio, 2) if ud_ratio != float('inf') else 99.0,
                    "建議停損": round(suggested_stop, 2), "建議目標": round(target_price, 2),
                    "風險%": round((risk_amount/price_now*100), 2) if price_now>0 else 0,
                    "日成交額(M$)": round(dollar_vol_m, 1), "RS差值(%)": round(rs_val, 2),
                    "突破": "🚀" if sig_break else "-", "回檔": "🎣" if sig_pullback else "-",
                    "VCP收縮": "🧨" if sig_vcp else "-", "Stage2": "🏗️" if sig_stage2 else "-",
                    "PowerDay": "⚡" if sig_power_day else "-", "強RS加分": "💎" if sig_rs_rating_strong else "-",
                    "多頭排列": "✓" if sig_ma_stack else "-", "20日均量(萬)": round(avg_vol_20/10000, 1),
                })

        progress_bar.progress(1.0)
        
        # 將結果寫入 session_state
        if results:
            df_res = pd.DataFrame(results).sort_values(by=["總分", "RS Rating", "U/D Ratio"], ascending=[False, False, False])
            
            # 自動備份掃描快照
            SCANS_DIR.mkdir(exist_ok=True)