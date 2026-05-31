"""
fetch_iv_history.py — 每日抓取觀察清單的 ATM IV，累積成 IV Rank 計算基礎

由 GitHub Actions 每日執行（美股收盤後），會：
  1. 對 WATCHLIST 中每檔，找最接近 30 天的到期日
  2. 找最接近現價的 ATM strike
  3. 取 Call 的 impliedVolatility 作為當日 IV
  4. 附加一筆 (date, ticker, iv) 到 cache/iv_history.parquet

累積 30+ 天後即可用於 IV Rank：
    IV Rank = (current_iv - 52w_low) / (52w_high - 52w_low) * 100
"""
from __future__ import annotations

import warnings
warnings.filterwarnings("ignore")

from pathlib import Path
from datetime import datetime, date, timedelta

import pandas as pd
import yfinance as yf


CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)
HISTORY_FILE = CACHE_DIR / "iv_history.parquet"

# 觀察清單：高流動性的代表性標的（ETF + 大型權值股 + 熱門題材股）
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


def _fetch_one(ticker: str, target_dte: int = 30) -> dict | None:
    """抓取單一標的的 ATM Call IV（最接近 target_dte 天的到期日）"""
    try:
        tk = yf.Ticker(ticker)
        if not tk.options:
            return None
        today = date.today()
        target = today + timedelta(days=target_dte)
        deltas = [
            abs((datetime.strptime(e, "%Y-%m-%d").date() - target).days)
            for e in tk.options
        ]
        chosen_exp = tk.options[deltas.index(min(deltas))]
        chosen_dte = (datetime.strptime(chosen_exp, "%Y-%m-%d").date() - today).days

        # 現價
        hist = tk.history(period="5d")
        if hist.empty:
            return None
        spot = float(hist["Close"].iloc[-1])

        # ATM Call
        chain = tk.option_chain(chosen_exp)
        calls = chain.calls
        if calls.empty:
            return None
        calls = calls.copy()
        calls["_d"] = (calls["strike"] - spot).abs()
        atm = calls.sort_values("_d").iloc[0]

        iv = float(atm.get("impliedVolatility", 0) or 0)
        if iv <= 0:
            return None
        return {
            "date": today.isoformat(),
            "ticker": ticker,
            "spot": round(spot, 2),
            "expiration": chosen_exp,
            "dte": chosen_dte,
            "atm_strike": float(atm["strike"]),
            "iv": round(iv, 5),
        }
    except Exception as e:
        print(f"  ❌ {ticker}: {e}")
        return None


def main():
    print("=" * 60)
    print("📊 IV 歷史累積（用於 IV Rank 計算）")
    print(f"   觀察清單：{len(WATCHLIST)} 檔")
    print("=" * 60)

    rows = []
    for i, t in enumerate(WATCHLIST, 1):
        rec = _fetch_one(t)
        if rec:
            rows.append(rec)
            print(f"  [{i}/{len(WATCHLIST)}] ✓ {t}  IV={rec['iv']*100:.1f}%  "
                  f"K=${rec['atm_strike']:.2f}  DTE={rec['dte']}d")
        else:
            print(f"  [{i}/{len(WATCHLIST)}] - {t}（略過）")

    if not rows:
        print("\n⚠️ 無資料可寫入")
        return

    df_new = pd.DataFrame(rows)

    if HISTORY_FILE.exists():
        try:
            df_old = pd.read_parquet(HISTORY_FILE)
            df = pd.concat([df_old, df_new], ignore_index=True)
            # 同日同 ticker 去重（保留最新）
            df = df.drop_duplicates(subset=["date", "ticker"], keep="last")
        except Exception as e:
            print(f"⚠️ 既有檔案讀取失敗，改為新建：{e}")
            df = df_new
    else:
        df = df_new

    # 修剪超過 400 天的舊資料
    cutoff = (date.today() - timedelta(days=400)).isoformat()
    df = df[df["date"] >= cutoff]

    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)
    df.to_parquet(HISTORY_FILE)

    print(f"\n✅ 完成：本次新增 {len(df_new)} 筆，累積總計 {len(df)} 筆，"
          f"涵蓋 {df['ticker'].nunique()} 檔")
    print(f"📁 檔案：{HISTORY_FILE}")


if __name__ == "__main__":
    main()
