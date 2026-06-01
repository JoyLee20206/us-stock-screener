import os
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

# Releases 上最新 parquet 的 URL（GitHub Actions 設成 env / secret）
# 沒設就跳過此步，本機開發照樣可以用 repo 內 cache/us_daily.parquet
PARQUET_URL = os.environ.get("PARQUET_URL", "").strip()


def sync_latest_from_releases(output_file: Path) -> None:
    """
    從 PARQUET_URL（GitHub Releases）下載最新 parquet，覆蓋本地檔。

    ❗修這個 bug：GitHub Actions checkout 拿到的是 repo 內早期上傳的舊 parquet
    （524 檔、最後日期 2026-05-14），導致每次跑都覺得 18 天未更新而強制全量。
    在本函式裡先用 Releases 上的最新版蓋掉，再進入下方的增量邏輯。
    """
    if not PARQUET_URL:
        print("ℹ️  未設定 PARQUET_URL，跳過 Releases 同步（本機或首次部署模式）")
        return
    try:
        print(f"🌐 從 Releases 拉取最新 parquet：{PARQUET_URL}")
        resp = requests.get(PARQUET_URL, timeout=180)
        resp.raise_for_status()
        size_mb = len(resp.content) / (1024 * 1024)
        # 若本地版已更新（最後日期 ≥ 遠端），略過覆蓋以免回退
        if output_file.exists():
            try:
                _local = pd.read_parquet(output_file)
                _local_last = _local['date'].max() if not _local.empty else None
                tmp = output_file.with_suffix('.tmp.parquet')
                tmp.write_bytes(resp.content)
                _remote = pd.read_parquet(tmp)
                _remote_last = _remote['date'].max() if not _remote.empty else None
                tmp.unlink(missing_ok=True)
                if _local_last and _remote_last and _local_last >= _remote_last:
                    print(f"   ↳ 本地（{_local_last}）已 ≥ 遠端（{_remote_last}），保留本地版")
                    return
                print(f"   ↳ 本地 {_local_last} / 遠端 {_remote_last}，採用遠端版")
            except Exception as _e:
                print(f"   !!! 比較本地/遠端失敗，仍以遠端覆蓋：{_e}")
        output_file.write_bytes(resp.content)
        print(f"   ✓ 已下載 {size_mb:.1f} MB → {output_file}")
    except Exception as e:
        print(f"   !!! Releases 同步失敗（不致命，將用本地或全量）：{e}")

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


def get_etf_tickers():
    """常用 ETF 清單：大盤 / 板塊 / 主題 / 國際 / 債券 / 商品"""
    print("🔍 載入常用 ETF 清單...")
    return [
        # 大盤 / 廣基
        "SPY", "VOO", "IVV", "QQQ", "QQQM", "DIA", "IWM", "VTI", "ITOT", "RSP",
        # SPDR 板塊（SP500 11 大類）
        "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC",
        # 科技 / 半導體 / AI
        "SOXX", "SMH", "SOXL", "VGT", "XSD", "QTUM", "BOTZ", "ROBO", "AIQ",
        # 主題 / 創新
        "ARKK", "ARKQ", "ARKW", "ARKG", "ARKF",
        # 國際
        "VEA", "VWO", "EFA", "EEM", "IEFA", "IEMG",
        "FXI", "KWEB", "MCHI", "ASHR",     # 中國
        "EWJ", "DXJ",                       # 日本
        "INDA", "INDY",                     # 印度
        "EWZ",                              # 巴西
        "EWY",                              # 韓國
        # 債券
        "TLT", "IEF", "SHY", "BND", "AGG", "HYG", "LQD", "JNK", "TIP",
        # 商品 / 黃金
        "GLD", "IAU", "SLV", "USO", "UNG", "DBC",
        # 高股息 / 防禦
        "SCHD", "VYM", "DVY", "HDV", "NOBL",
        # 房地產
        "VNQ", "IYR",
        # 槓桿 / 反向（給可能會用的人）
        "TQQQ", "SQQQ", "SPXL", "SPXS", "UVXY", "VXX",
        # 主題 / 產業
        "IBB", "XBI", "XME", "ITA", "JETS", "ICLN", "TAN", "LIT", "URA",
        # 加密
        "IBIT", "FBTC", "BITO", "ETHE",
    ]

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
    print("🌟 美股黃金選股池增量快取更新 (S&P 500 + NDX + SP400 + SOX + ETFs)")
    print("=" * 60)

    output_file = CACHE_DIR / "us_daily.parquet"
    today = pd.Timestamp.now().normalize()

    # === 0. 先從 GitHub Releases 拉最新 parquet（修「每次都 18 天未更新」bug）===
    sync_latest_from_releases(output_file)

    # === 1. 取得最新成分股清單 ===
    tickers = list(set(
        get_sp500_tickers() + get_nasdaq100_tickers()
        + get_sp400_tickers() + get_sox_tickers()
        + get_etf_tickers()
    ))
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
