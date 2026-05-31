import time
import warnings
warnings.filterwarnings("ignore")

import requests
from io import StringIO
from pathlib import Path
import pandas as pd
import yfinance as yf

# ==========================================
# 參數設定區
# ==========================================
CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)
PERIOD = "1y"             # 首次/新標的全量下載期間（涵蓋 52 週新高計算）
KEEP_DAYS = 380           # 快取保留視窗（日曆日，含週末緩衝）
FULL_REFRESH_WEEKDAY = 5  # 星期六（Mon=0..Sun=6）強制全量，吸收當週分割/股息調整
STALE_DAYS = 7            # 快取超過此天數未更新也強制全量

def get_sp500_tickers():
    print("🔍 抓取 S&P 500 成分股...")
    try:
        url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(url, headers=headers)
        tables = pd.read_html(StringIO(response.text))
        for df in tables:
            if 'Symbol' in df.columns:
                return [str(t).replace('.', '-') for t in df['Symbol'].tolist()]
    except Exception as e:
        print(f"   !!! S&P 500 抓取失敗: {e}")
    return []

def get_nasdaq100_tickers():
    print("🔍 抓取 NASDAQ 100 成分股...")
    try:
        url = 'https://en.wikipedia.org/wiki/Nasdaq-100'
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        response = requests.get(url, headers=headers)
        tables = pd.read_html(StringIO(response.text))
        for table in tables:
            if 'Ticker' in table.columns:
                return [str(t).replace('.', '-') for t in table['Ticker'].tolist()]
    except Exception as e:
        print(f"   !!! NASDAQ 100 抓取失敗: {e}")
    return []

def get_sp400_tickers():
    print("🔍 抓取 S&P MidCap 400 成分股...")
    try:
        url = 'https://en.wikipedia.org/wiki/List_of_S%26P_400_companies'
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(url, headers=headers)
        tables = pd.read_html(StringIO(response.text))
        for df in tables:
            if 'Symbol' in df.columns:
                return [str(t).replace('.', '-') for t in df['Symbol'].tolist()]
    except Exception as e:
        print(f"   !!! S&P 400 抓取失敗: {e}")
    return []

def get_sox_tickers():
    print("🔍 載入費城半導體 (SOX) 成分股...")
    return ["AMD", "ADI", "AMAT", "ARM", "ASML", "AVGO", "COHR", "ENTG", "GFS",
            "INTC", "KLAC", "LRCX", "LSCC", "MCHP", "MPWR", "MRVL", "MU", "NVDA",
            "NXPI", "ON", "QCOM", "RMBS", "SWKS", "STM", "SYNA", "TER", "TXN",
            "TSM", "WDC", "WOLF"]

def _flatten(data, tickers_list):
    """yf.download 結果 → long format DataFrame"""
    if data is None or data.empty:
        return None

    # 單一 ticker 防呆
    if not isinstance(data.columns, pd.MultiIndex):
        only_ticker = tickers_list[0] if len(tickers_list) == 1 else "UNKNOWN"
        data.columns = pd.MultiIndex.from_product([data.columns, [only_ticker]])

    try:
        df_flat = data.stack(level=1, future_stack=True)
    except Exception:
        df_flat = data.stack(level=1)

    df_flat.index.names = ['date', 'stock_id']
    df_flat = df_flat.reset_index()
    df_flat = df_flat.rename(columns={
        "Open": "open", "High": "max", "Low": "min", "Close": "close", "Volume": "Trading_Volume"
    })

    if df_flat['date'].dt.tz is not None:
        df_flat['date'] = df_flat['date'].dt.tz_localize(None)
    df_flat['date'] = df_flat['date'].dt.strftime('%Y-%m-%d')
    df_flat = df_flat.dropna(subset=['close', 'stock_id'])
    return df_flat

def main():
    print("=" * 60)
    print("🌟 美股黃金選股池增量快取更新 (S&P 500 + NDX + SP400 + SOX)")
    print("=" * 60)

    output_file = CACHE_DIR / "us_daily.parquet"
    today = pd.Timestamp.now().normalize()

    # === 1. 取得最新成分股清單 ===
    tickers = list(set(get_sp500_tickers() + get_nasdaq100_tickers() + get_sp400_tickers() + get_sox_tickers()))
    tickers = sorted([t for t in tickers if isinstance(t, str) and t.strip()])
    print(f"\n📋 目標成分股共 {len(tickers)} 檔")

    # === 2. 載入現有快取 ===
    existing_df = None
    cached_tickers = set()
    last_global_date = None

    per_ticker_last = None  # Series: stock_id → 該檔最後日期
    if output_file.exists():
        try:
            existing_df = pd.read_parquet(output_file)
            cached_tickers = set(existing_df['stock_id'].unique())
            per_ticker_last = existing_df.groupby('stock_id')['date'].max()
            last_global_date = per_ticker_last.max()  # 用於 stale 判斷
            print(f"📂 既有快取：{len(existing_df):,} 筆，{len(cached_tickers)} 檔，最後日期 {last_global_date}")
        except Exception as e:
            print(f"   !!! 快取讀取失敗，改為全量下載: {e}")
            existing_df = None
            per_ticker_last = None

    # === 2b. 判斷是否需要強制全量刷新 (Option A) ===
    force_full = False
    full_reason = None
    if existing_df is not None and last_global_date is not None:
        days_stale = (today - pd.Timestamp(last_global_date)).days
        if today.weekday() == FULL_REFRESH_WEEKDAY:
            force_full = True
            full_reason = f"今天是週{['一','二','三','四','五','六','日'][today.weekday()]}，定期全量刷新（吸收當週分割/股息）"
        elif days_stale >= STALE_DAYS:
            force_full = True
            full_reason = f"快取已 {days_stale} 天未更新（≥{STALE_DAYS}），強制全量刷新"

    if force_full:
        print(f"\n🔄 {full_reason}")
        # 丟棄舊快取，按首次執行流程處理
        existing_df = None
        cached_tickers = set()
        last_global_date = None
        per_ticker_last = None

    # === 3. 切分增量 vs 新標的 ===
    new_tickers = [t for t in tickers if t not in cached_tickers]
    incr_tickers = [t for t in tickers if t in cached_tickers]

    frames = []
    start_time = time.time()

    # === 3a. 增量下載（已有快取的標的）===
    # 用「最落後 ticker 的最後日期」作為下載起始日，避免暫停交易的標的長期停滯
    if existing_df is not None and incr_tickers and per_ticker_last is not None:
        oldest_last = per_ticker_last[per_ticker_last.index.isin(incr_tickers)].min()
        start_date = (pd.Timestamp(oldest_last) + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
        end_date = (today + pd.Timedelta(days=1)).strftime('%Y-%m-%d')

        if pd.Timestamp(start_date) <= today:
            print(f"\n📈 增量下載 {len(incr_tickers)} 檔 ({start_date} → today)...")
            data = yf.download(incr_tickers, start=start_date, end=end_date,
                               auto_adjust=True, threads=True, progress=False)
            df_incr = _flatten(data, incr_tickers)
            if df_incr is not None and not df_incr.empty:
                frames.append(df_incr)
                print(f"   ✓ 取得 {len(df_incr):,} 筆新資料")
            else:
                print(f"   - 無新資料（今日可能尚未收盤、或為週末/假日）")
        else:
            print(f"\n✅ 快取已是最新日期 ({last_global_date})，跳過增量下載。")

    # === 3b. 新標的全量下載 ===
    if new_tickers:
        print(f"\n🆕 新標的全量下載 {len(new_tickers)} 檔（{PERIOD}）...")
        data = yf.download(new_tickers, period=PERIOD,
                           auto_adjust=True, threads=True, progress=False)
        df_new = _flatten(data, new_tickers)
        if df_new is not None and not df_new.empty:
            frames.append(df_new)
            print(f"   ✓ 取得 {len(df_new):,} 筆")
    elif existing_df is None:
        # 完全沒有快取 → 首次執行
        print(f"\n🚀 首次執行：全量下載 {len(tickers)} 檔（{PERIOD}）...")
        data = yf.download(tickers, period=PERIOD,
                           auto_adjust=True, threads=True, progress=False)
        df_full = _flatten(data, tickers)
        if df_full is not None and not df_full.empty:
            frames.append(df_full)

    # === 4. 合併、剔除已下市/移除標的、去重、修剪 ===
    if existing_df is not None:
        # 只保留還在最新成分股清單內的 ticker（已剔除指數的丟掉）
        dropped = cached_tickers - set(tickers)
        if dropped:
            print(f"\n🗑️ 從快取移除 {len(dropped)} 檔不再屬於指數的標的: {sorted(dropped)[:5]}{'...' if len(dropped) > 5 else ''}")
        existing_kept = existing_df[existing_df['stock_id'].isin(set(tickers))]
        frames.insert(0, existing_kept)

    if not frames:
        print("❌ 無資料可寫入。"); return

    combined = pd.concat(frames, ignore_index=True)
    before_dedup = len(combined)
    combined = combined.drop_duplicates(subset=['stock_id', 'date'], keep='last')

    # 修剪過期資料（即「刪舊快取」）
    cutoff = (today - pd.Timedelta(days=KEEP_DAYS)).strftime('%Y-%m-%d')
    before_prune = len(combined)
    combined = combined[combined['date'] >= cutoff]
    pruned = before_prune - len(combined)

    combined = combined.sort_values(by=['stock_id', 'date']).reset_index(drop=True)

    # === 5. 寫回（覆蓋舊檔）===
    combined.to_parquet(output_file)

    elapsed = time.time() - start_time
    print(f"\n✅ 完成！耗時 {elapsed:.1f} 秒")
    print(f"   合併去重：{before_dedup:,} → {before_prune:,} 筆")
    print(f"   修剪過期：刪除 {pruned:,} 筆（{cutoff} 之前）")
    print(f"📁 檔案: {output_file} (最終 {len(combined):,} 筆，{combined['stock_id'].nunique()} 檔)")

if __name__ == "__main__":
    main()
