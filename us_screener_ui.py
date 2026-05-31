import warnings
warnings.filterwarnings("ignore")

import streamlit as st
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import datetime, timezone, timedelta
import io
import json
import requests

POSITIONS_FILE = Path("positions") / "positions.json"
SCANS_DIR = Path("scans")
OPTIONS_GUIDE = Path("期權新手指南.md")

# 期權工具模組（軟相依：缺檔時不影響主程式）
try:
    import options_data as opt
    OPTIONS_AVAILABLE = True
except Exception as _e:
    OPTIONS_AVAILABLE = False
    _OPT_IMPORT_ERROR = str(_e)
TPE_TZ = timezone(timedelta(hours=8))   # 台北時間（UTC+8），Streamlit Cloud 跑在 UTC

def now_tpe():
    """回傳當下台北時間（naive datetime，方便顯示與相減）"""
    return datetime.now(TPE_TZ).replace(tzinfo=None)

st.set_page_config(page_title="美股量化選股終端機", page_icon="🇺🇸", layout="wide")
CACHE_DIR = Path("cache")

# 自定義美化 CSS
st.markdown("""<style>
    /* 縮減主畫面頂部留白（保留 2.5rem 避免被 Streamlit toolbar 蓋住）*/
    .block-container { padding-top: 4rem !important; padding-bottom: 1rem !important; }
    /* 自訂緊湊型標題 — color: inherit 確保深淺色主題都顯示 */
    .app-title {
        font-size: 1.6rem;
        font-weight: 700;
        margin: 0 0 0.75rem 0;
        line-height: 1.2;
        color: inherit;
    }
    /* 縮小大盤狀態 metric 區字體 */
    [data-testid="stMetricValue"] { font-size: 1.4rem !important; line-height: 1.2 !important; }
    [data-testid="stMetricLabel"] { font-size: 0.85rem !important; }
    [data-testid="stMetricDelta"] { font-size: 0.8rem !important; }
    /* 縮小 success / warning / error 提示橫條的內外邊距 */
    [data-testid="stAlert"] {
        padding: 0.4rem 0.75rem !important;
        margin: 0.25rem 0 !important;
    }
    [data-testid="stAlert"] p { margin: 0 !important; line-height: 1.3 !important; }
    /* 縮小 expander / tab 內 markdown 標題字體（教學文件用） */
    [data-testid="stExpander"] h1,
    [data-baseweb="tab-panel"] h1 { font-size: 1.4rem !important; margin: 0.6rem 0 0.4rem !important; }
    [data-testid="stExpander"] h2,
    [data-baseweb="tab-panel"] h2 { font-size: 1.15rem !important; margin: 0.5rem 0 0.3rem !important; }
    [data-testid="stExpander"] h3,
    [data-baseweb="tab-panel"] h3 { font-size: 1.05rem !important; margin: 0.4rem 0 0.2rem !important; }
    [data-testid="stExpander"] p, [data-testid="stExpander"] li,
    [data-baseweb="tab-panel"] p, [data-baseweb="tab-panel"] li {
        font-size: 0.92rem !important;
        line-height: 1.5 !important;
        margin: 0.25rem 0 !important;
    }
    [data-testid="stExpander"] table,
    [data-baseweb="tab-panel"] table { font-size: 0.88rem !important; }
    [data-testid="stExpander"] hr,
    [data-baseweb="tab-panel"] hr { margin: 0.6rem 0 !important; }
    /* tab 列本身美化：字稍大、底線粗一點 */
    [data-baseweb="tab-list"] button[data-baseweb="tab"] {
        font-size: 1rem !important;
        font-weight: 600 !important;
        padding: 0.5rem 1rem !important;
    }
    .main { background-color: #f8f9fa; }
    .stButton>button { background-color: #007bff; color: white; border-radius: 8px; font-weight: bold; }
    .stDownloadButton>button { background-color: #28a745 !important; color: white !important; }
</style>""", unsafe_allow_html=True)

@st.cache_data(ttl=3600)
def _read_parquet_cached(path_str: str, mtime_key: float):
    return pd.read_parquet(path_str)

@st.cache_data(ttl=3600, show_spinner="正在從雲端下載市場資料（~15-25 MB）...")
def _download_parquet_from_url(url: str):
    """回傳 (DataFrame, 檔案最後更新時間 / 台北時區)"""
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    df = pd.read_parquet(io.BytesIO(resp.content))
    # 嘗試從 HTTP header 抓取真實上傳時間
    last_mod = resp.headers.get("Last-Modified")
    if last_mod:
        try:
            from email.utils import parsedate_to_datetime
            mtime = parsedate_to_datetime(last_mod).astimezone(TPE_TZ).replace(tzinfo=None)
        except Exception:
            mtime = now_tpe()
    else:
        mtime = now_tpe()
    return df, mtime

def load_stock_data():
    """
    優先序：
      1. 若有設定 PARQUET_URL secret → 一律從 GitHub Releases 下載（雲端部署用）
         避免 repo 內舊的 cache/us_daily.parquet 蓋掉新版資料
      2. 否則嘗試本地檔案（本機開發用）
      3. 都沒有則報錯
    """
    try:
        parquet_url = st.secrets.get("PARQUET_URL", "")
    except Exception:
        parquet_url = ""

    # 雲端模式優先：有設 URL 就用 URL（永遠抓最新）
    if parquet_url:
        try:
            df, mtime_tpe = _download_parquet_from_url(parquet_url)
            return df, mtime_tpe
        except Exception as e:
            st.warning(f"⚠️ 雲端資料下載失敗：{e}，嘗試本地快取...")

    # 本地模式 fallback
    local_file = CACHE_DIR / "us_daily.parquet"
    if local_file.exists():
        try:
            mtime_ts = local_file.stat().st_mtime
            df = _read_parquet_cached(str(local_file), mtime_ts)
            mtime_tpe = datetime.fromtimestamp(mtime_ts, TPE_TZ).replace(tzinfo=None)
            return df, mtime_tpe
        except Exception as e:
            st.error(f"⚠️ 快取檔損毀：{e}")
            return None, None

    st.error("⚠️ 找不到本機快取，也未設定 PARQUET_URL。"
             "請執行 fetch_cache_us.py，或在 Streamlit Cloud Secrets 設定 PARQUET_URL。")
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
run_scan = st.sidebar.button("🚀 執行量化掃描", type="primary", use_container_width=True)

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
st.markdown('<div class="app-title">🇺🇸 美股量化選股終端機</div>', unsafe_allow_html=True)

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

# 提示快取新鮮度 + 強制刷新按鈕
_cache_info_cols = st.columns([5, 1])
with _cache_info_cols[0]:
    age_hours = (now_tpe() - cache_mtime).total_seconds() / 3600
    if age_hours > 24:
        st.warning(f"⏰ 快取已過期 {age_hours:.1f} 小時（更新於 {cache_mtime:%Y-%m-%d %H:%M}），"
                   f"建議重跑 fetch_cache_us.py。目前共 **{len(df_daily['stock_id'].unique())} 檔**")
    else:
        st.caption(f"📅 快取更新於 {cache_mtime:%Y-%m-%d %H:%M}（{age_hours:.1f} 小時前）"
                   f"｜共 **{len(df_daily['stock_id'].unique())} 檔**")
with _cache_info_cols[1]:
    if st.button("🔄 刷新雲端資料", key="force_refresh",
                 help="強制清除 Streamlit 快取，從 GitHub Releases 重新下載最新 parquet。"
                      "通常在 GitHub Actions 剛跑完新版資料時使用。"):
        st.cache_data.clear()
        st.rerun()

if run_scan:
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
            _now_tpe = now_tpe()
            scan_filename = SCANS_DIR / f"{_now_tpe:%Y-%m-%d_%H%M%S}_{strategy_mode}.parquet"
            df_res_save = df_res.copy()
            df_res_save['mode'] = strategy_mode
            df_res_save['scan_time'] = _now_tpe.isoformat()
            df_res_save['scan_date'] = _now_tpe.strftime('%Y-%m-%d')
            df_res_save.to_parquet(scan_filename)
            st.caption(f"💾 已存檔至 {scan_filename.name}（供回測模組使用）")

            st.dataframe(df_res, use_container_width=True, hide_index=True)
            
            # 下載與 Firstrade 複製區
            col1, col2 = st.columns(2)
            with col1:
                buf = io.BytesIO()
                with pd.ExcelWriter(buf, engine='openpyxl') as writer: df_res.to_excel(writer, index=False)
                st.download_button("📥 下載 Excel 報表", buf.getvalue(), f"US_Scan_{now_tpe().strftime('%m%d')}.xlsx")
            with col2:
                st.subheader("🏦 Firstrade 快速複製")
                st.code(", ".join(df_res["代號"].tolist()), language="text")

            # ─── 對 Top N 產生期權建議（P2 #10）───
            if OPTIONS_AVAILABLE:
                st.markdown("---")
                st.markdown("### 📈 對掃描結果產生期權買方建議")
                rcols = st.columns([1, 1, 1, 1.2, 1.5])
                top_n = rcols[0].number_input("Top N 名", min_value=1, max_value=20,
                                               value=min(5, len(df_res)), step=1,
                                               key="opt_topn")
                target_dte = rcols[1].number_input("目標 DTE", min_value=14, max_value=90,
                                                    value=30, step=1, key="opt_target_dte",
                                                    help="想看的到期天數，會挑最接近的到期日")
                opt_direction = rcols[2].selectbox("方向", options=["call", "put"],
                                                    key="opt_direction",
                                                    help="看漲選 call，看跌選 put")
                avoid_earnings = rcols[3].toggle("📆 避開財報", value=False,
                                                  key="opt_avoid_earn",
                                                  help="開啟後會自動挑「財報後 ~14 天」的到期日，"
                                                       "避免 IV crush。沒有財報的標的則用一般 30 天邏輯。")
                if rcols[4].button("🎯 批次查詢", type="primary", key="batch_opt_btn",
                                    use_container_width=True):
                    tickers = df_res["代號"].head(int(top_n)).tolist()
                    with st.spinner(f"查詢 {len(tickers)} 檔的 ⭐ 推薦合約"
                                    f"{'（已避開財報）' if avoid_earnings else ''}..."):
                        recs = opt.recommend_for_tickers(tickers, target_dte=int(target_dte),
                                                          option_type=opt_direction,
                                                          avoid_earnings=avoid_earnings)
                    st.session_state["batch_recs"] = pd.DataFrame(recs)
                    st.session_state["batch_recs_key"] = (tuple(tickers), int(target_dte),
                                                            opt_direction, avoid_earnings)

                # 從 session_state 還原批次查詢結果（互動其他元件不會消失）
                _batch_recs = st.session_state.get("batch_recs")
                _batch_key = st.session_state.get("batch_recs_key")
                _current_batch_key = (tuple(df_res["代號"].head(int(top_n)).tolist()),
                                       int(target_dte), opt_direction, avoid_earnings)
                if _batch_recs is not None and _batch_key == _current_batch_key:
                    st.dataframe(_batch_recs, use_container_width=True, hide_index=True)
                    st.caption("💡 想看完整鏈與風險分析，請切到「🎯 期權瀏覽」頁籤輸入代號查詢。")
        else:
            st.warning("☹️ 無符合標的，請放寬條件。")

# ============================================================
# 📑 將底部 4 個區塊整合成頁籤（類似 Excel sheet tabs）
# ============================================================
st.divider()
_tab_pos, _tab_bt, _tab_edu, _tab_opt = st.tabs([
    "📌 我的持倉",
    "📈 績效回測",
    "📚 期權新手教學",
    "🎯 期權瀏覽（純買方視角）",
])

# ============================================================
# 📌 D+E. 持倉管理
# ============================================================
with _tab_pos:
    positions = load_positions()
    # 區分股票部位 vs 期權部位（向後相容：沒有 type 欄位視為 stock）
    stock_positions = [p for p in positions if p.get("type", "stock") == "stock"]
    option_positions = [p for p in positions if p.get("type") == "option"]

    tab_stock, tab_option = st.tabs([
        f"📈 股票持倉 ({len(stock_positions)})",
        f"🎯 期權持倉 ({len(option_positions)})",
    ])

    # ─── 股票持倉分頁 ───
    with tab_stock:
        with st.form("add_stock_position", clear_on_submit=True):
            st.markdown("**新增股票持倉**")
            pcols = st.columns([1.5, 1, 1, 1.2, 1, 1])
            new_sid = pcols[0].text_input("代號", placeholder="例如 NVDA").strip().upper()
            new_entry_price = pcols[1].number_input("進場價", min_value=0.01, value=100.0, step=0.01, format="%.2f")
            new_shares = pcols[2].number_input("股數", min_value=1, value=10, step=1)
            new_entry_date = pcols[3].date_input("進場日", value=now_tpe().date())
            new_stop = pcols[4].number_input("停損價(選填)", min_value=0.0, value=0.0, step=0.01, format="%.2f",
                help="留 0 = 自動用 -7% 鐵則。\n注意：實際生效停損 = max(你輸入的停損, 進場價×0.93)。"
                     "比 -7% 寬鬆的停損會被自動緊縮（Minervini 鐵則）。"
                     "若想設更緊停損（例如 -5%），輸入 進場價×0.95 即可生效。")
            new_target = pcols[5].number_input("目標價(選填)", min_value=0.0, value=0.0, step=0.01, format="%.2f")
            st.caption("💡 停損會自動緊縮為「進場價 × 0.93（-7% 鐵則）」與「你輸入的停損」之較緊者。")
            submitted = st.form_submit_button("➕ 加入股票持倉")
            if submitted and new_sid:
                positions.append({
                    "type": "stock",
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

        if not stock_positions:
            st.info("📭 目前沒有股票持倉。在上方表單新增。")
        else:
            rs_ratings_for_pos = compute_rs_ratings(df_daily, cache_mtime.timestamp())
            rows = []
            missing_sids = []
            for pos in stock_positions:
                r = evaluate_position(pos, df_daily, rs_ratings_for_pos)
                if r:
                    rows.append(r)
                else:
                    missing_sids.append(pos['sid'])

            if missing_sids:
                missing_sids = sorted(set(missing_sids))   # 去重 + 排序
                _all_cached = set(df_daily["stock_id"].unique())
                _cache_size = len(_all_cached)
                # 對找不到的代號，建議鄰近字母的相似代號（fuzzy）
                hints = []
                for ms in missing_sids:
                    similar = sorted([t for t in _all_cached
                                       if t.startswith(ms[:2]) or t == ms.upper()])[:5]
                    hints.append(f"**{ms}**" + (f"（快取中相似：{', '.join(similar)}）" if similar else ""))
                st.warning(f"⚠️ 以下持倉的代號在快取中找不到：{', '.join(hints)}")
                st.caption(f"💡 目前快取共 **{_cache_size} 檔**。若你確定代號正確（例如 ETF），"
                           f"可能是 GitHub Actions 還沒抓到 → 觸發 workflow → Streamlit Cloud Reboot。"
                           f"範例已有的 ETF：" + ", ".join(sorted([t for t in _all_cached
                                                                       if t in {"SPY","QQQ","XLK","VEA","VTI",
                                                                                "ARKK","TLT","GLD","IWM"}])))

            if rows:
                df_pos = pd.DataFrame(rows)
                total_alert = sum(1 for r in rows if "🔴" in r['警示'])
                total_warn = sum(1 for r in rows if "🟡" in r['警示'] and "🔴" not in r['警示'])
                avg_ret = df_pos['報酬%'].mean()
                mcols = st.columns(4)
                mcols[0].metric("持倉檔數", len(rows))
                mcols[1].metric("平均報酬", f"{avg_ret:+.2f}%")
                mcols[2].metric("🔴 嚴重警示", total_alert)
                mcols[3].metric("🟡 一般警示", total_warn)

                st.dataframe(df_pos, use_container_width=True, hide_index=True)

                st.markdown("**移除股票持倉**")
                del_sid = st.selectbox("選擇要移除的代號",
                                       options=[p['sid'] for p in stock_positions],
                                       key="del_stock_pos")
                if st.button("🗑️ 移除", key="del_stock_btn"):
                    positions = [p for p in positions
                                 if not (p.get("type", "stock") == "stock" and p['sid'] == del_sid)]
                    save_positions(positions)
                    st.success(f"✓ 已移除 {del_sid}")
                    st.rerun()

    # ─── 期權持倉分頁 ───
    with tab_option:
        if not OPTIONS_AVAILABLE:
            st.error(f"⚠️ 期權模組未載入：{_OPT_IMPORT_ERROR}")
        else:
            with st.form("add_option_position", clear_on_submit=True):
                st.markdown("**新增期權持倉（買方部位）**")
                ocols1 = st.columns([1.2, 1, 1.2, 1.2, 1, 1])
                opt_new_sid = ocols1[0].text_input("代號", placeholder="例如 NVDA",
                                                    key="opt_pos_sid").strip().upper()
                opt_new_type = ocols1[1].selectbox("Call/Put", options=["call", "put"],
                                                    key="opt_pos_type")
                opt_new_strike = ocols1[2].number_input("行權價", min_value=0.01, value=100.0,
                                                         step=0.50, format="%.2f", key="opt_pos_strike")
                opt_new_exp = ocols1[3].text_input("到期日 (YYYY-MM-DD)",
                                                    placeholder="例如 2026-06-20", key="opt_pos_exp")
                opt_new_premium = ocols1[4].number_input("進場權利金/股", min_value=0.01, value=1.00,
                                                          step=0.01, format="%.2f", key="opt_pos_prem",
                                                          help="每股權利金（不是每口）。例：$5.30 表示一口成本 $530。")
                opt_new_contracts = ocols1[5].number_input("口數", min_value=1, value=1, step=1,
                                                            key="opt_pos_qty")
                opt_new_entry = st.date_input("進場日", value=now_tpe().date(), key="opt_pos_date")
                st.caption("💡 評估時系統會即時抓取當前 Bid/Ask 計算合約現值與 Greeks。"
                           "若合約已失效（到期/下市），會用 Black-Scholes 估算。")
                opt_submitted = st.form_submit_button("➕ 加入期權持倉")
                if opt_submitted and opt_new_sid and opt_new_exp:
                    try:
                        datetime.strptime(opt_new_exp, "%Y-%m-%d")  # 驗證格式
                        positions.append({
                            "type": "option",
                            "sid": opt_new_sid,
                            "option_type": opt_new_type,
                            "strike": float(opt_new_strike),
                            "expiration": opt_new_exp,
                            "premium": float(opt_new_premium),
                            "contracts": int(opt_new_contracts),
                            "entry_date": opt_new_entry.isoformat(),
                        })
                        save_positions(positions)
                        st.success(f"✓ 已加入 {opt_new_sid} {opt_new_type.upper()} ${opt_new_strike} {opt_new_exp}")
                        st.rerun()
                    except ValueError:
                        st.error("⚠️ 到期日格式錯誤，請用 YYYY-MM-DD（例如 2026-06-20）")

            if not option_positions:
                st.info("📭 目前沒有期權持倉。在上方表單新增。")
            else:
                if st.button("🔄 重新整理期權部位", key="refresh_opt_pos",
                             help="重新抓取 yfinance 即時報價計算當前損益"):
                    st.cache_data.clear()
                    st.rerun()

                with st.spinner("評估期權部位（抓取即時報價中）..."):
                    opt_rows = []
                    for pos in option_positions:
                        r = opt.evaluate_option_position(pos)
                        if r:
                            opt_rows.append(r)
                if opt_rows:
                    df_opt = pd.DataFrame(opt_rows)

                    # 彙總統計
                    total_pnl = df_opt["總損益($)"].sum() if "總損益($)" in df_opt else 0
                    total_cost = df_opt["成本($)"].sum() if "成本($)" in df_opt else 0
                    avg_ret = (total_pnl / total_cost * 100) if total_cost > 0 else 0
                    urgent = sum(1 for r in opt_rows if "🚨" in str(r.get("警示", "")))
                    profit_count = sum(1 for r in opt_rows if "🟢" in str(r.get("警示", "")))

                    mcols = st.columns(4)
                    mcols[0].metric("口數總計", df_opt["口數"].sum() if "口數" in df_opt else 0)
                    mcols[1].metric("總損益", f"${total_pnl:+,.0f}",
                                    delta_color="normal" if total_pnl >= 0 else "inverse")
                    mcols[2].metric("加權報酬%", f"{avg_ret:+.1f}%")
                    mcols[3].metric("🚨 緊急/🟢 達標", f"{urgent} / {profit_count}")

                    st.dataframe(df_opt, use_container_width=True, hide_index=True)

                    st.markdown("**移除期權持倉**")
                    opt_labels = [f"{i}: {p['sid']} {p.get('option_type','?').upper()} "
                                  f"${p.get('strike',0):.2f} {p.get('expiration','')}"
                                  for i, p in enumerate(option_positions)]
                    del_label = st.selectbox("選擇要移除的合約", options=opt_labels, key="del_opt_pos")
                    if st.button("🗑️ 移除", key="del_opt_btn"):
                        del_idx = int(del_label.split(":")[0])
                        target = option_positions[del_idx]
                        positions = [p for p in positions if p is not target]
                        save_positions(positions)
                        st.success("✓ 已移除")
                        st.rerun()
                else:
                    st.warning("⚠️ 無法評估任何期權部位（可能合約已到期或代號錯誤）")

# ============================================================
# 📈 F. 績效回測
# ============================================================
with _tab_bt:
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

# ============================================================
# 📚 G. 期權新手教學
# ============================================================
with _tab_edu:
    if OPTIONS_GUIDE.exists():
        try:
            guide_md = OPTIONS_GUIDE.read_text(encoding="utf-8")
            st.markdown(guide_md, unsafe_allow_html=True)
        except Exception as e:
            st.error(f"⚠️ 期權新手指南讀取失敗：{e}")
    else:
        st.warning("⚠️ 找不到 `期權新手指南.md`，請確認檔案已隨專案部署。")

# ============================================================
# 🎯 H. 期權瀏覽（純買方視角）
# ============================================================
with _tab_opt:
    if not OPTIONS_AVAILABLE:
        st.error(f"⚠️ 期權模組載入失敗：{_OPT_IMPORT_ERROR}。請確認 options_data.py 已隨專案部署。")
    else:
        st.caption("從選股結果挑一檔輸入，查看可用合約 + 智能標籤 + Greeks。新手請優先看 ⭐ 推薦標籤。")

        # ─── 使用說明（預設收起，新手點開看）───
        with st.expander("📖 使用說明（第一次來請點開）", expanded=False):
            st.markdown("""
#### 🎯 這個頁籤能做什麼？
查看任何美股代號的**期權合約資料**，自動計算 Greeks、智能標籤、推薦合約、風險檢查、損益模擬，
讓你不用打開券商也能評估「該不該買這口期權」。

---

#### 🚦 三步驟操作流程

**Step 1：輸入代號 + 選到期日 → 按 🔍 查詢**
- 代號：例如 `NVDA`、`SPY`、`AAPL`（**新手建議從 ETF 開始**）
- 到期日：系統預設選最接近 **30 天**的（買方甜蜜點）
- 沒有期權的標的（如非主流小型股）會顯示警告

**Step 2：看結果三大區塊**

| 區塊 | 看什麼 |
|------|--------|
| **頂部摘要列** | 現價、到期日、剩餘天數、Call/Put 數量、IV/RV Rank、下次財報日 |
| **⭐ 推薦合約卡** | 系統替你挑出 Delta 0.45-0.65 + DTE 21-45 的甜蜜點合約 |
| **Call 鏈 / Put 鏈** | 完整鏈表格，每口合約都有智能標籤（⭐💎🟠⏱️🟡⚠️🔥💀❓）|

**Step 3：往下捲到「🔍 合約分析」做進階評估**

| 功能 | 用途 |
|------|------|
| **選擇合約下拉** | 預設選 ⭐ 推薦那口，可以下拉切換比較不同行權價 |
| **✅ 風險檢查（7 項）** | 紅黃綠燈，整體判定 GO / CAUTION / STOP |
| **🎮 What-if 模擬器** | 拖 slider 模擬「股價漲 5%、再過 7 天、IV crush -20%」這口會值多少 |
| **📈 到期日損益曲線** | 視覺化到期那天股價在哪→盈虧多少 |

---

#### 💡 常見使用情境

**A. 我看好某檔，想用槓桿放大**
1. 輸入代號 → 預設 30 天到期
2. 看 ⭐ 推薦 Call 卡片
3. 在合約分析勾選風險清單，全綠才下單

**B. 我想看 IV 現在算貴還算便宜**
- 摘要列第 5 欄 **IV Rank** 或 **RV Rank**
- ≥ 70% 🔥 偏貴 → 等等再買
- < 30% ✅ 便宜 → 進場時機好

**C. 想避開財報 IV crush**
- 摘要列第 6 欄 **下次財報日**
- 跨財報時頂端會出現紅色橫條 + **「📆 改選 YYYY-MM-DD」一鍵切換到財報後到期**按鈕
- 系統會額外顯示 **IV/RV 比例**（earnings drift 偵測）：
  - ✓ 正常（< 1.2）→ 進場時機 OK
  - 📈 偏高（1.2-1.4）→ 已有事件溢價
  - 🔥 暴衝（> 1.4）→ 強烈不建議買方進場

**D. 想比較兩個行權價**
- 在「🔍 合約分析」下拉切換不同 strike
- 看哪個的風險檢查整體判定更好、報酬風險比更高

---

#### 🏷️ 智能標籤一覽表

系統會對每口合約自動打上**一個主要標籤**幫你快速判斷。優先序：地雷 > Delta 分級。

##### ✅ 可考慮的合約（由優到劣）

| 標籤 | 條件 | 適合誰 |
|------|------|--------|
| ⭐ **推薦** | Δ 0.45-0.65 + DTE 21-45 天 | **新手首選**（性價比甜蜜點）|
| 💎 **ITM 穩** | Δ > 0.70 | 想替代買股票、要高中獎率 |
| 🟠 **略 ITM** | Δ 0.65-0.70 | 保守派、稍貴但穩 |
| ⏱️ **DTE 不對** | Δ 對了（0.45-0.65）但 DTE 不在 21-45 | Delta OK 但時間軸要重挑 |
| 🟡 **略 OTM** | Δ 0.30-0.45 | 性價比中等、想博更大槓桿 |

##### ❌ 應避開的合約

| 標籤 | 條件 | 為什麼避開 |
|------|------|----------|
| ⚠️ **太 OTM** | Δ < 0.30 | 中獎率低、90% 歸零 |
| 🔥 **高 IV** | IV > 50% | 進場貴、財報前常見 IV crush |
| 💀 **Theta 黑洞** | DTE < 14 天 | 時間損耗每天 5-10% |
| ❓ **流動性差** | OI < 100 | 想出場可能沒人接 |
| 🚨 **跨財報** | 合約跨越下次財報日 | IV crush 風險（單獨欄位顯示）|

##### 標籤如何被決定？

判定流程（**由上往下優先**）：
```
1. OI < 100         → ❓ 流動性差   （地雷一）
2. DTE < 14         → 💀 Theta 黑洞 （地雷二）
3. IV > 50%         → 🔥 高 IV     （地雷三）
4. Delta < 0.30     → ⚠️ 太 OTM    （地雷四）
5. Delta 0.30-0.45  → 🟡 略 OTM
6. Delta 0.45-0.65 + DTE 21-45 → ⭐ 推薦
7. Delta 0.45-0.65 + DTE 不對   → ⏱️ DTE 不對
8. Delta 0.65-0.70  → 🟠 略 ITM
9. Delta > 0.70     → 💎 ITM 穩
```

📅 **跨財報標籤**為**獨立欄位**，與上述標籤可同時出現。例如「⭐ 推薦 + 🚨 跨財報」表示性價比好但有 IV crush 風險。

---

#### 🔗 想多了解概念？

切到「📚 期權新手教學」頁籤，有完整新手指南。
            """)

        # 預設代號：用剛掃描結果的第一檔，否則 SPY
        default_sym = "SPY"
        if isinstance(st.session_state.get("df_res"), pd.DataFrame) and not st.session_state.df_res.empty:
            default_sym = str(st.session_state.df_res["代號"].iloc[0])

        ocol1, ocol2, ocol3 = st.columns([2, 2, 1])
        opt_ticker = ocol1.text_input(
            "標的代號", value=default_sym, key="opt_ticker",
            help="輸入美股代號（例如 NVDA、SPY、AAPL）。新手建議從 SPY、QQQ 等 ETF 開始。",
        ).strip().upper()

        # 抓到期日清單
        expirations = []
        if opt_ticker:
            with st.spinner(f"載入 {opt_ticker} 可選到期日..."):
                expirations = opt.list_expirations(opt_ticker)

        if not expirations:
            ocol2.warning("⚠️ 無法取得到期日（可能代號錯誤或暫無期權）")
            opt_expiration = None
        else:
            # 預設選第 2 個（避開週選常見的高 Theta 風險）
            default_idx = min(1, len(expirations) - 1)
            # 找出最接近 30 天的到期作為預設更聰明
            try:
                today = now_tpe().date()
                target = today + timedelta(days=30)
                deltas = [abs((datetime.strptime(e, "%Y-%m-%d").date() - target).days) for e in expirations]
                default_idx = deltas.index(min(deltas))
            except Exception:
                pass
            opt_expiration = ocol2.selectbox(
                "到期日（預設選最接近 30 天）",
                options=expirations,
                index=default_idx,
                key="opt_expiration",
                help="期權買方甜蜜點是 21-45 天。太短 Theta 損耗快、太長資金占用久。",
            )

        run_options = ocol3.button("🔍 查詢", type="primary", use_container_width=True, key="opt_run")

        # 用 session_state 保存查詢結果，避免互動 slider 時整段消失
        _cache_key = (opt_ticker, opt_expiration)
        if run_options and opt_ticker and opt_expiration:
            with st.spinner(f"抓取 {opt_ticker} {opt_expiration} 期權鏈..."):
                _view = opt.build_buyer_view(opt_ticker, opt_expiration, df_daily=df_daily)
            st.session_state["opt_view"] = _view
            st.session_state["opt_view_key"] = _cache_key

        # 從 session_state 讀回（按過查詢、且 ticker/expiration 未變）
        view = st.session_state.get("opt_view")
        view_key = st.session_state.get("opt_view_key")
        # 若代號/到期變了則清空（要求重新查詢）
        if view is not None and view_key != _cache_key:
            view = None

        if view is not None:
            if "error" in view:
                st.error(f"⚠️ {view['error']}")
            else:
                # ⚠️ 跨財報全域警示橫條 + 「改選財報後到期」按鈕
                if view.get("crosses_earnings"):
                    _dt_earn = view.get("days_to_earnings")
                    _earn_date = view.get("next_earnings")
                    _post_exp = view.get("post_earnings_expiration")
                    _warn_cols = st.columns([4, 1])
                    with _warn_cols[0]:
                        if _dt_earn is not None and _dt_earn <= 7:
                            st.error(f"🚨 跨財報警示：下次財報 **{_earn_date}**（{_dt_earn} 天後），"
                                     f"買方建議避開或選財報後到期。IV crush 風險極高。")
                        else:
                            st.warning(f"📅 跨財報注意：合約到期前有財報 **{_earn_date}**"
                                       f"（{_dt_earn} 天後）。財報後 IV 通常會崩塌，買方獲利機率下降。")
                    with _warn_cols[1]:
                        if _post_exp and _post_exp != view["expiration"]:
                            if st.button(f"📆 改選 {_post_exp}", key="switch_post_earn",
                                         use_container_width=True,
                                         help=f"自動切換到財報後到期日 {_post_exp}（財報後 ~14 天）"):
                                # 直接修改 expiration 選項並重新查詢
                                st.session_state["opt_expiration"] = _post_exp
                                with st.spinner("重新抓取財報後到期合約..."):
                                    _new_view = opt.build_buyer_view(opt_ticker, _post_exp,
                                                                       df_daily=df_daily)
                                st.session_state["opt_view"] = _new_view
                                st.session_state["opt_view_key"] = (opt_ticker, _post_exp)
                                st.rerun()

                # 📈 IV Drift（earnings drift）警示橫條
                _drift = view.get("iv_drift")
                if _drift and _drift.get("is_drift"):
                    if _drift["drift_level"] == "strong":
                        st.error(f"🔥 **IV 暴衝偵測**：{_drift['msg']}。"
                                 f"買方此時進場將承受極大 IV crush 風險，建議**等財報後再進**或**改選財報後到期**。")
                    else:
                        st.warning(f"📈 **IV 突增偵測**：{_drift['msg']}。"
                                   f"已有事件溢價，建議比較 IV/RV 後再決定。")

                # 摘要列（含 IV Rank + 下次財報日）
                mcol1, mcol2, mcol3, mcol4, mcol5, mcol6 = st.columns(6)
                mcol1.metric("標的現價", f"${view['spot']}")
                mcol2.metric("到期日", view["expiration"])
                mcol3.metric("剩餘天數", f"{view['dte']} 天")
                mcol4.metric("Call/Put 筆數", f"{len(view['calls'])} / {len(view['puts'])}")

                # 下次財報日（第 6 欄）
                if view.get("next_earnings"):
                    _dt_earn = view.get("days_to_earnings")
                    if view.get("crosses_earnings"):
                        mcol6.metric("📅 下次財報", view["next_earnings"],
                                     delta=f"🚨 {_dt_earn} 天後（跨合約）",
                                     delta_color="inverse",
                                     help="財報日在合約到期日之前，買方 IV crush 風險。")
                    else:
                        mcol6.metric("📅 下次財報", view["next_earnings"],
                                     delta=f"{_dt_earn} 天後",
                                     help="財報日在合約到期日之後，本次合約不受影響。")
                else:
                    mcol6.metric("📅 下次財報", "—", delta="無資料",
                                 help="yfinance 找不到財報日。可能無近期財報或資料源無此標的。")

                # IV/RV 比率（用於 earnings drift 偵測），單獨一列顯示
                if _drift:
                    _ratio = _drift["iv_rv_ratio"]
                    _iv_pct = _drift["iv"] * 100
                    _rv_pct = _drift["rv"] * 100
                    _level = _drift["drift_level"]
                    _tip = (f"IV (ATM)：{_iv_pct:.1f}%  ｜  RV (20日)：{_rv_pct:.1f}%\n"
                            f"IV/RV = {_ratio}（{_level}）\n"
                            f"財報前 1-3 週這個比例會從 ~1.0 飆到 1.3-1.6（earnings drift）。"
                            f"買方在 ratio < 1.2 時進場最划算。")
                    if _level == "strong":
                        st.error(f"📊 **IV/RV 比例**：{_ratio}（🔥 暴衝） ｜ IV={_iv_pct:.1f}% RV={_rv_pct:.1f}%"
                                 f"  ｜ 距財報 {_drift.get('days_to_earnings', '-')} 天",
                                 icon="📈")
                    elif _level == "elevated":
                        st.warning(f"📊 **IV/RV 比例**：{_ratio}（📈 偏高） ｜ IV={_iv_pct:.1f}% RV={_rv_pct:.1f}%"
                                   f"  ｜ 距財報 {_drift.get('days_to_earnings', '-')} 天",
                                   icon="📈")
                    else:
                        st.caption(f"📊 IV/RV 比例：{_ratio}（✓ 正常） ｜ IV={_iv_pct:.1f}% RV={_rv_pct:.1f}%")

                # 波動率指標雙軌：IV Rank（真實，需累積） + RV Rank（估算，立即可用）
                _atm_iv = None
                if not view["calls"].empty:
                    _calls_view = view["calls"].copy()
                    _calls_view["_d"] = (_calls_view["strike"] - view["spot"]).abs()
                    _atm_row = _calls_view.sort_values("_d").iloc[0]
                    _atm_iv = float(_atm_row.get("impliedVolatility", 0) or 0)

                iv_rank_info = opt.compute_iv_rank(opt_ticker, _atm_iv) if _atm_iv else None
                rv_rank_info = opt.compute_rv_rank(opt_ticker, df_daily)

                # 決定要顯示哪個：IV Rank 累積 ≥ 30 天時優先用真實 IV，否則用 RV Rank 估算
                _use_real_iv = (iv_rank_info and iv_rank_info.get("rank") is not None
                                and iv_rank_info.get("samples", 0) >= 30)

                if _use_real_iv:
                    r = iv_rank_info["rank"]
                    _label = f"IV Rank（{iv_rank_info['samples']} 日歷史）"
                    if r >= 70:
                        mcol5.metric(_label, f"{r:.0f}%", delta="🔥 偏貴", delta_color="inverse",
                                     help="真實 IV Rank：當前 IV 在歷史區間的百分位")
                    elif r >= 30:
                        mcol5.metric(_label, f"{r:.0f}%", delta="中等",
                                     help="真實 IV Rank：當前 IV 在歷史區間的百分位")
                    else:
                        mcol5.metric(_label, f"{r:.0f}%", delta="✅ 便宜",
                                     help="真實 IV Rank：當前 IV 在歷史區間的百分位")
                elif rv_rank_info and rv_rank_info.get("rank") is not None:
                    r = rv_rank_info["rank"]
                    iv_status = opt.iv_history_status()
                    _days = iv_status["days"] if iv_status["exists"] else 0
                    _label = "RV Rank（估算）"
                    _help_text = (f"用過去 1 年的「實現波動率」算百分位，IV Rank 替代指標。"
                                  f"\n當前 RV：{rv_rank_info['rv_now']*100:.1f}% "
                                  f"(範圍 {rv_rank_info['rv_min']*100:.1f}%-{rv_rank_info['rv_max']*100:.1f}%)"
                                  f"\nIV 歷史累積中（已 {_days} 天 / 需 30 天），"
                                  f"累積完成後自動切換為真實 IV Rank。")
                    if r >= 70:
                        mcol5.metric(_label, f"{r:.0f}%", delta="🔥 偏貴", delta_color="inverse", help=_help_text)
                    elif r >= 30:
                        mcol5.metric(_label, f"{r:.0f}%", delta="中等", help=_help_text)
                    else:
                        mcol5.metric(_label, f"{r:.0f}%", delta="✅ 便宜", help=_help_text)
                else:
                    mcol5.metric("波動率 Rank", "—",
                                 delta="無資料",
                                 help="該標的不在股價快取內，且 IV 歷史也未累積。")

                # ⭐ 推薦合約
                rec_c, rec_p = view.get("recommended_call"), view.get("recommended_put")
                if rec_c or rec_p:
                    st.markdown("### ⭐ 系統推薦合約（新手最適合的 Delta 0.45-0.65 + DTE 21-45）")
                    rc1, rc2 = st.columns(2)
                    if rec_c:
                        with rc1:
                            st.success(
                                f"**📈 Long Call**：${rec_c['strike']:.2f}\n\n"
                                f"權利金 ${rec_c['mid']:.2f} × 100 = **${rec_c['mid']*100:.0f}/口**\n\n"
                                f"Δ={rec_c['delta']:.2f}  Θ={rec_c['theta_per_day']:.2f}/天  IV={rec_c['iv_pct']:.1f}%\n\n"
                                f"盈虧平衡 ${rec_c['break_even']:.2f}（距現價 {rec_c['distance_pct']:+.2f}%）"
                            )
                    else:
                        rc1.info("📈 Call 端目前無符合條件的 ⭐ 推薦合約")
                    if rec_p:
                        with rc2:
                            st.success(
                                f"**📉 Long Put**：${rec_p['strike']:.2f}\n\n"
                                f"權利金 ${rec_p['mid']:.2f} × 100 = **${rec_p['mid']*100:.0f}/口**\n\n"
                                f"Δ={rec_p['delta']:.2f}  Θ={rec_p['theta_per_day']:.2f}/天  IV={rec_p['iv_pct']:.1f}%\n\n"
                                f"盈虧平衡 ${rec_p['break_even']:.2f}（距現價 {rec_p['distance_pct']:+.2f}%）"
                            )
                    else:
                        rc2.info("📉 Put 端目前無符合條件的 ⭐ 推薦合約")

                st.caption(
                    "📖 標籤說明（由優到劣）：⭐推薦 → 💎ITM穩 → 🟠略ITM → ⏱️DTE不對 → 🟡略OTM "
                    "→ ⚠️太OTM / 🔥高IV / 💀Theta黑洞 / ❓流動性差"
                )

                # 完整鏈
                tab_call, tab_put = st.tabs(["📈 Call 鏈", "📉 Put 鏈"])
                with tab_call:
                    df_call_show = opt.to_display_df(view["calls"])
                    if df_call_show.empty:
                        st.info("無 Call 資料")
                    else:
                        # 預設只顯示現價 ±20% 範圍內 + 有標籤的
                        spot = view["spot"]
                        df_call_show = df_call_show[
                            (df_call_show["行權價"] >= spot * 0.80) &
                            (df_call_show["行權價"] <= spot * 1.20)
                        ]
                        st.dataframe(
                            df_call_show.sort_values("行權價"),
                            use_container_width=True, hide_index=True,
                            column_config={
                                "中價": st.column_config.NumberColumn(format="$%.2f"),
                                "Bid": st.column_config.NumberColumn(format="$%.2f"),
                                "Ask": st.column_config.NumberColumn(format="$%.2f"),
                                "行權價": st.column_config.NumberColumn(format="$%.2f"),
                                "盈虧平衡": st.column_config.NumberColumn(format="$%.2f"),
                                "距現價%": st.column_config.NumberColumn(format="%+.2f%%"),
                                "Δ": st.column_config.NumberColumn(format="%.2f"),
                                "Θ/天": st.column_config.NumberColumn(format="%.3f"),
                            },
                        )
                with tab_put:
                    df_put_show = opt.to_display_df(view["puts"])
                    if df_put_show.empty:
                        st.info("無 Put 資料")
                    else:
                        spot = view["spot"]
                        df_put_show = df_put_show[
                            (df_put_show["行權價"] >= spot * 0.80) &
                            (df_put_show["行權價"] <= spot * 1.20)
                        ]
                        st.dataframe(
                            df_put_show.sort_values("行權價"),
                            use_container_width=True, hide_index=True,
                            column_config={
                                "中價": st.column_config.NumberColumn(format="$%.2f"),
                                "Bid": st.column_config.NumberColumn(format="$%.2f"),
                                "Ask": st.column_config.NumberColumn(format="$%.2f"),
                                "行權價": st.column_config.NumberColumn(format="$%.2f"),
                                "盈虧平衡": st.column_config.NumberColumn(format="$%.2f"),
                                "距現價%": st.column_config.NumberColumn(format="%+.2f%%"),
                                "Δ": st.column_config.NumberColumn(format="%.2f"),
                                "Θ/天": st.column_config.NumberColumn(format="%.3f"),
                            },
                        )

                st.caption("💡 表格只顯示行權價在現價 ±20% 範圍內的合約。Greeks 由 Black-Scholes 計算，"
                           "假設無風險利率 4.5%、股息 0%。實際下單請以券商報價為準。")

                # ─────────────────────────────────────────────
                # 🔍 合約分析：風險清單 + What-if 模擬器 + 損益圖
                # ─────────────────────────────────────────────
                st.markdown("---")
                st.markdown("### 🔍 合約分析（風險清單 + 模擬器 + 損益圖）")
                st.caption("挑一口你想下的合約，系統幫你做進場前 6 項風險檢查、模擬未來情境、畫到期損益曲線。")

                acol1, acol2, acol3 = st.columns([1, 2, 1])
                analysis_type = acol1.radio(
                    "合約類型",
                    options=["Call", "Put"],
                    horizontal=True,
                    key="opt_analysis_type",
                    help="Call 適合看漲；Put 適合看跌。新手建議先從 Call 開始。",
                )
                _ot = "call" if analysis_type == "Call" else "put"
                _src_df = view["calls"] if _ot == "call" else view["puts"]

                # 篩出可選的行權價（±25% 範圍）
                spot = view["spot"]
                _src_df = _src_df[
                    (_src_df["strike"] >= spot * 0.75) &
                    (_src_df["strike"] <= spot * 1.25)
                ].sort_values("strike")

                if _src_df.empty:
                    st.warning("⚠️ 沒有可分析的合約")
                else:
                    # 用 label + strike 製作下拉選項，預設選 ⭐ 推薦的
                    _options = []
                    _default_idx = 0
                    _star_idx = None
                    for i, (_, r) in enumerate(_src_df.iterrows()):
                        _opt_label = f"${r['strike']:.2f}  [{r['label']}]  Δ={r['delta']:.2f}  Θ={r['theta_per_day']:.3f}  Bid/Ask ${r['bid']:.2f}/${r['ask']:.2f}"
                        _options.append(_opt_label)
                        if r["label"] == "⭐ 推薦" and _star_idx is None:
                            _star_idx = i
                    if _star_idx is not None:
                        _default_idx = _star_idx

                    _picked = acol2.selectbox(
                        "選擇合約",
                        options=_options,
                        index=_default_idx,
                        key="opt_picked",
                        help="預設選 ⭐ 推薦合約。可下拉換不同行權價比較。",
                    )
                    _picked_row = _src_df.iloc[_options.index(_picked)]

                    # ─── 風險清單 ───
                    st.markdown("#### ✅ 進場前 6 項風險檢查")
                    checklist = opt.risk_checklist(
                        option_type=_ot,
                        strike=float(_picked_row["strike"]),
                        mid=float(_picked_row["mid"]),
                        delta=float(_picked_row["delta"]) if pd.notna(_picked_row["delta"]) else 0.0,
                        dte=int(_picked_row["dte"]),
                        iv=float(_picked_row["impliedVolatility"]) if pd.notna(_picked_row["impliedVolatility"]) else 0.0,
                        open_interest=int(_picked_row.get("openInterest") or 0),
                        volume=int(_picked_row.get("volume") or 0),
                        bid=float(_picked_row.get("bid") or 0),
                        ask=float(_picked_row.get("ask") or 0),
                        next_earnings=view.get("next_earnings"),
                        expiration=view.get("expiration"),
                    )
                    verd = opt.verdict(checklist)
                    if verd["light"] == "✅":
                        st.success(f"**{verd['light']} {verd['label']}** — {verd['msg']}")
                    elif verd["light"] == "⚠️":
                        st.warning(f"**{verd['light']} {verd['label']}** — {verd['msg']}")
                    else:
                        st.error(f"**{verd['light']} {verd['label']}** — {verd['msg']}")

                    # 7 項風險檢查改成 2 欄佈局（第 7 項為跨財報，內容較長）
                    chk_cols = st.columns(2)
                    for ci, item in enumerate(checklist):
                        chk_cols[ci % 2].markdown(f"**{item['status']} {item['label']}**: {item['detail']}")

                    # ─── 名詞解釋（緊接在風險檢查後）───
                    with st.expander("💡 名詞解釋：Delta / Theta / IV / BE 是什麼？"):
                        st.markdown("""
- **Δ Delta**：股價 $1 變動 → 期權變多少。0.55 約等於「中獎機率 55%」。新手選 **0.45-0.65**。
- **Θ Theta**：每過一天，期權跌多少（時間價值衰減）。**越接近 0 越好**。所以選 **DTE 21-45 天**，太短 Theta 吃光、太長資金占用。
- **IV 隱含波動率**：市場對未來波動的預期。**IV 越高 → 期權越貴**。新手避開 IV > 50%。
- **盈虧平衡點 (BE)**：到期股價要超過 BE 才賺錢。**Call BE = 行權價 + 權利金**；**Put BE = 行權價 - 權利金**。
- **OI（Open Interest）未平倉量**：這口合約有多少張在市場上。**OI 大 = 流動性好、容易出場**。
- **Bid/Ask 價差**：買賣價差。價差大代表你買進就賠掉滑價。買方要選價差 < 5%。
                        """)

                    # ─── What-if 模擬器 ───
                    st.markdown("#### 🎮 What-if 模擬器（如果...會怎樣）")
                    st.caption("拖動下方 slider，模擬未來情境下這口合約值多少、賺賠多少。"
                               "💡 試試把「股價變動」設 +5%，「天數經過」設 14 天 — 看 Theta 怎麼吃掉你的獲利。")
                    sim_c1, sim_c2, sim_c3 = st.columns(3)
                    sim_spot = sim_c1.slider("股價變動 %", -15.0, 15.0, 0.0, 0.5,
                                              key="sim_spot",
                                              help="假設股價漲跌多少。+5 = 漲 5%、-5 = 跌 5%")
                    _max_days = int(_picked_row["dte"])
                    sim_days = sim_c2.slider("天數經過", 0, _max_days, 0, 1,
                                              key="sim_days",
                                              help="假設過了幾天。Theta 會持續侵蝕時間價值。")
                    sim_iv = sim_c3.slider("IV 變動 %", -30.0, 30.0, 0.0, 1.0,
                                            key="sim_iv",
                                            help="假設 IV 漲跌多少。財報後 IV 常 crush -30% 以上。")

                    entry_premium = float(_picked_row["mid"])
                    sim = opt.simulate_whatif(
                        option_type=_ot,
                        entry_premium=entry_premium,
                        strike=float(_picked_row["strike"]),
                        dte_now=int(_picked_row["dte"]),
                        spot_now=spot,
                        iv_now=float(_picked_row["impliedVolatility"]) if pd.notna(_picked_row["impliedVolatility"]) else 0.0,
                        spot_pct_change=sim_spot,
                        days_passed=sim_days,
                        iv_pct_change=sim_iv,
                    )

                    rcol1, rcol2, rcol3, rcol4 = st.columns(4)
                    rcol1.metric("新股價", f"${sim['new_spot']:.2f}",
                                 delta=f"{sim_spot:+.1f}%")
                    rcol2.metric("合約新理論價", f"${sim['new_price']:.2f}",
                                 delta=f"{sim['new_price'] - entry_premium:+.2f}")
                    rcol3.metric("每口損益", f"${sim['pnl_per_contract']:+,.0f}",
                                 delta_color="normal" if sim["pnl_per_contract"] >= 0 else "inverse",
                                 delta=f"進場 ${entry_premium*100:.0f}")
                    rcol4.metric("報酬率", f"{sim['return_pct']:+.1f}%",
                                 delta_color="normal" if sim["return_pct"] >= 0 else "inverse")

                    # ─── 到期日損益圖（Plotly） ───
                    st.markdown("#### 📈 到期日損益曲線")
                    st.caption("假設你今天進場，到了**到期那天**，股價落在不同位置時你能賺/賠多少。"
                               "**注意：到期前的損益會比這張圖溫和**（因為時間價值還沒燒完）。")
                    try:
                        import plotly.graph_objects as go
                        prices, pnls = opt.expiration_pnl_curve(
                            option_type=_ot,
                            strike=float(_picked_row["strike"]),
                            entry_premium=entry_premium,
                            spot_now=spot,
                            range_pct=25.0,
                            num_points=60,
                        )
                        be_price = float(_picked_row["break_even"])
                        max_loss = -entry_premium * 100

                        fig = go.Figure()
                        # P&L 曲線
                        fig.add_trace(go.Scatter(
                            x=prices, y=pnls, mode="lines",
                            name="到期 P&L", line=dict(width=3, color="#1f9eff"),
                            fill="tozeroy",
                            fillcolor="rgba(0,200,0,0.08)" if _ot == "call" else "rgba(200,0,0,0.08)",
                            hovertemplate="股價 $%{x:.2f}<br>損益 $%{y:+.0f}<extra></extra>",
                        ))
                        # 0 軸
                        fig.add_hline(y=0, line=dict(dash="dash", width=1, color="gray"))
                        # 最大虧損水平線 + 右側標籤
                        fig.add_hline(y=max_loss, line=dict(dash="dot", width=1, color="red"),
                                      annotation_text=f"最大虧損 ${max_loss:.0f}",
                                      annotation_position="bottom right",
                                      annotation_font=dict(color="red", size=11))
                        # 現價（橘色虛線）
                        fig.add_vline(x=spot, line=dict(dash="dot", color="orange"),
                                      annotation_text=f"🟠 現價 ${spot:.2f}", annotation_position="top")
                        # 盈虧平衡（綠色虛線）
                        fig.add_vline(x=be_price, line=dict(dash="dash", color="green"),
                                      annotation_text=f"🟢 BE ${be_price:.2f}",
                                      annotation_position="bottom")
                        # 行權價（藍色虛線）
                        fig.add_vline(x=float(_picked_row["strike"]),
                                      line=dict(dash="dot", color="#3399ff"),
                                      annotation_text=f"🔵 K ${_picked_row['strike']:.2f}",
                                      annotation_position="top")
                        fig.update_layout(
                            xaxis_title="到期日股價 ($)",
                            yaxis_title="損益 ($)",
                            height=380,
                            margin=dict(t=30, b=40, l=40, r=10),
                            showlegend=False,
                        )
                        st.plotly_chart(fig, use_container_width=True)
                    except Exception as _e:
                        st.warning(f"⚠️ 損益圖渲染失敗：{_e}")

                    # ─── 圖表解讀說明 ───
                    with st.expander("📖 怎麼看這張圖？（第一次看請點開）", expanded=False):
                        _ot_zh = "Call" if _ot == "call" else "Put"
                        _strike = float(_picked_row["strike"])
                        st.markdown(f"""
#### 🧭 三條虛線分別代表什麼？

| 顏色 | 線 | 意義 |
|------|----|------|
| 🟠 **橘色** | 現價 ${spot:.2f} | 標的股票**現在**的價格 |
| 🔵 **藍色** | 行權價 K ${_strike:.2f} | 你選的這口 {_ot_zh} 的行使價 |
| 🟢 **綠色** | 盈虧平衡點 BE ${be_price:.2f} | 到期股價要過這個你才賺錢 |
| 🔴 **紅色橫線** | 最大虧損 ${max_loss:.0f} | 不管股價跌多少，你**最多**只賠這麼多（一口）|

---

#### 📊 怎麼讀曲線？

**X 軸**：到期日當天的**股價**
**Y 軸**：你的**損益**（正數賺、負數賠）

藍色曲線在 BE 點之後**才上揚（=賺錢）**，BE 之前**是水平的**（=固定賠權利金）。

**核心三段：**

1. **股價低於行權價 ${_strike:.2f}**
   → 你的 {_ot_zh} 一文不值 → **賠光權利金 ${max_loss:.0f}**

2. **股價介於 ${_strike:.2f} ~ ${be_price:.2f}**
   → 已有內含價值但還抵不過權利金 → **小賠**

3. **股價超過 BE ${be_price:.2f}**
   → 開始**淨賺** → 股票漲越多賺越多（理論上無上限）

---

#### 💡 三個常見問題

**Q1：為什麼圖左半段是平的？**
A: 因為 {_ot_zh} 買方**最多就是賠光權利金**，不會再多賠了（這也是「買方有限風險」的好處）。

**Q2：為什麼右半段是斜的直線？**
A: 股價每漲 $1，到期 {_ot_zh} 內含價值就多 $1（× 100 股 = $100）。所以你在 BE 之後是**槓桿放大**獲利。

**Q3：那為什麼我現在持有時看到的損益不是這條線？**
A: **這是「到期日」的損益**。到期前還有「時間價值」+「IV」加持，所以實際損益曲線會比這條溫和（虧損沒這麼慘、盈利也沒這麼大）。想看到期前的情境，回上面的 **🎮 What-if 模擬器** 拖 slider 看。
                        """)