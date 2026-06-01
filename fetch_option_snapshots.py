"""
fetch_option_snapshots.py — 每日全鏈快照備份

對 WATCHLIST 中每檔，抓取**未來 60 天內**所有到期日的完整期權鏈，
篩出 ATM ±20% 範圍的 strikes，存到 cache/option_snapshots/{ticker}.parquet。

雲端期權瀏覽當 yfinance 即時抓取失敗時，會 fallback 使用這份快照。

由 GitHub Actions 每日於股價/IV 快取後執行（台灣時間 06:00 左右）。
"""
from __future__ import annotations

import warnings
warnings.filterwarnings("ignore")

import time
from pathlib import Path
from datetime import datetime, date, timedelta

import pandas as pd
import yfinance as yf


SNAPSHOT_DIR = Path("cache/option_snapshots")
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

# 與 fetch_iv_history.py 對齊的觀察清單
WATCHLIST = [
    # 大盤 / 板塊 ETF
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLY", "XLI",
    "SMH", "SOXX", "ARKK",
    # Magnificent 7
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    # 半導體龍頭
    "AMD", "AVGO", "TSM", "MU", "QCOM", "INTC", "MRVL",
    # 雲端 / SaaS
    "CRM", "ORCL", "NOW", "SNOW", "DDOG", "NET", "CRWD",
    # 金融
    "JPM", "BAC", "GS", "MS", "WFC",
    # 中概
    "BABA", "PDD",
    # 熱門題材
    "PLTR", "COIN", "MSTR", "UBER",
]

MAX_DAYS_AHEAD = 60     # 只抓 60 天內的到期
STRIKE_RANGE_PCT = 20   # ATM ±20%


def _get_spot(ticker: str) -> float | None:
    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period="5d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


def _fetch_chain_for_ticker(ticker: str) -> int:
    """抓單一標的所有近期到期日的鏈，回傳寫入筆數"""
    try:
        tk = yf.Ticker(ticker)
        all_exps = list(tk.options) if tk.options else []
        if not all_exps:
            print(f"  ❌ {ticker}：無期權")
            return 0

        # 過濾到期日：60 天內
        today = date.today()
        cutoff = today + timedelta(days=MAX_DAYS_AHEAD)
        target_exps = []
        for e in all_exps:
            try:
                d = datetime.strptime(e, "%Y-%m-%d").date()
                if today <= d <= cutoff:
                    target_exps.append(e)
            except Exception:
                continue

        if not target_exps:
            print(f"  ❌ {ticker}：未來 60 天內無到期")
            return 0

        spot = _get_spot(ticker)
        if spot is None:
            print(f"  ❌ {ticker}：無現價")
            return 0

        low = spot * (1 - STRIKE_RANGE_PCT / 100.0)
        high = spot * (1 + STRIKE_RANGE_PCT / 100.0)
        today_iso = today.isoformat()

        all_rows: list[pd.DataFrame] = []
        for exp in target_exps:
            try:
                chain = tk.option_chain(exp)
            except Exception as e:
                print(f"    ⚠️ {ticker} {exp} 抓取失敗：{e}")
                continue

            for kind, df in [("call", chain.calls), ("put", chain.puts)]:
                if df is None or df.empty:
                    continue
                d = df.copy()
                # 過濾 ATM ±20% strike 範圍
                d = d[(d["strike"] >= low) & (d["strike"] <= high)]
                if d.empty:
                    continue
                d["_kind"] = kind
                d["expiration"] = exp
                d["_spot_at_snapshot"] = spot
                d["_snapshot_date"] = today_iso
                all_rows.append(d)

            # 友善延遲，避免 rate limit
            time.sleep(0.3)

        if not all_rows:
            print(f"  ⚠️ {ticker}：無符合範圍的合約")
            return 0

        combined = pd.concat(all_rows, ignore_index=True)
        out_path = SNAPSHOT_DIR / f"{ticker}.parquet"
        combined.to_parquet(out_path)
        print(f"  ✓ {ticker}：{len(combined)} 筆 ({len(target_exps)} 個到期)"
              f"  現價 ${spot:.2f}  範圍 ${low:.2f}-${high:.2f}")
        return len(combined)
    except Exception as e:
        print(f"  ❌ {ticker}：{type(e).__name__}: {e}")
        return 0


def main():
    print("=" * 60)
    print(f"📸 期權全鏈每日快照（{len(WATCHLIST)} 檔 × 60 天內到期 × ATM ±{STRIKE_RANGE_PCT}%）")
    print("=" * 60)

    start = time.time()
    total_rows = 0
    success_count = 0

    for i, ticker in enumerate(WATCHLIST, 1):
        print(f"\n[{i}/{len(WATCHLIST)}] {ticker}")
        n = _fetch_chain_for_ticker(ticker)
        if n > 0:
            total_rows += n
            success_count += 1
        # 標的間延遲
        time.sleep(0.5)

    elapsed = time.time() - start
    print(f"\n{'='*60}")
    print(f"✅ 完成！耗時 {elapsed:.1f} 秒")
    print(f"   成功：{success_count}/{len(WATCHLIST)} 檔")
    print(f"   總筆數：{total_rows:,}")
    print(f"📁 輸出目錄：{SNAPSHOT_DIR}")


if __name__ == "__main__":
    main()
