"""
options_data.py — 美股期權鏈抓取 + Black-Scholes Greeks + 智能標籤

提供給 us_screener_ui.py 的「🎯 期權瀏覽」分頁使用。

關鍵函式：
    fetch_option_chain(ticker, expiration=None) → DataFrame
    add_greeks(df, spot_price, risk_free=0.045) → DataFrame  附加 Delta/Theta/Vega/Gamma
    add_labels(df, spot_price) → DataFrame              附加新手安全標籤
    list_expirations(ticker) → List[str]                可選到期日清單
    pick_recommended(df_call, df_put, mode='call') → DataFrame  挑 ⭐ 推薦合約
"""

from __future__ import annotations

import math
from datetime import datetime, date

import numpy as np
import pandas as pd
import yfinance as yf


# ============================================================
# Black-Scholes Greeks
# ============================================================
def _norm_cdf(x: float) -> float:
    """標準常態 CDF（用 math.erf 不依賴 scipy）"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """標準常態 PDF"""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_d1_d2(S, K, T, r, sigma, q=0.0):
    """計算 d1、d2（S/K/T/sigma 必須 > 0）"""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return None, None
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def bs_greeks(option_type: str, S: float, K: float, T_days: float,
              sigma: float, r: float = 0.045, q: float = 0.0) -> dict:
    """
    回傳一個合約的 Greeks dict：{delta, gamma, theta_per_day, vega_per_1pct}

    Args:
        option_type: 'call' or 'put'
        S: 標的現價
        K: 行權價
        T_days: 距到期天數
        sigma: IV（小數，如 0.28）
        r: 無風險利率（小數，預設 4.5%）
        q: 股息率（小數，預設 0）
    """
    T = T_days / 365.0
    d1, d2 = _bs_d1_d2(S, K, T, r, sigma, q)
    if d1 is None:
        return {"delta": np.nan, "gamma": np.nan,
                "theta_per_day": np.nan, "vega_per_1pct": np.nan}

    pdf_d1 = _norm_pdf(d1)
    sqrt_T = math.sqrt(T)

    gamma = pdf_d1 * math.exp(-q * T) / (S * sigma * sqrt_T)
    vega = S * math.exp(-q * T) * pdf_d1 * sqrt_T / 100.0  # 每 IV +1% 變動

    if option_type == "call":
        delta = math.exp(-q * T) * _norm_cdf(d1)
        theta_year = (-S * pdf_d1 * sigma * math.exp(-q * T) / (2 * sqrt_T)
                      - r * K * math.exp(-r * T) * _norm_cdf(d2)
                      + q * S * math.exp(-q * T) * _norm_cdf(d1))
    else:  # put
        delta = math.exp(-q * T) * (_norm_cdf(d1) - 1)
        theta_year = (-S * pdf_d1 * sigma * math.exp(-q * T) / (2 * sqrt_T)
                      + r * K * math.exp(-r * T) * _norm_cdf(-d2)
                      - q * S * math.exp(-q * T) * _norm_cdf(-d1))

    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 5),
        "theta_per_day": round(theta_year / 365.0, 4),
        "vega_per_1pct": round(vega, 4),
    }


# ============================================================
# 期權鏈抓取
# ============================================================
def list_expirations(ticker: str) -> list[str]:
    """回傳該標的可選到期日清單（按時間排序）"""
    try:
        tk = yf.Ticker(ticker)
        return list(tk.options) if tk.options else []
    except Exception:
        return []


def get_spot_price(ticker: str) -> float | None:
    """抓即時/最新收盤價"""
    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period="5d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


def fetch_option_chain(ticker: str, expiration: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    抓取指定到期日的 Call + Put 鏈

    Returns:
        (df_call, df_put)：兩個 DataFrame，欄位包含
        strike, lastPrice, bid, ask, impliedVolatility, openInterest, volume
    """
    tk = yf.Ticker(ticker)
    chain = tk.option_chain(expiration)
    df_call = chain.calls.copy()
    df_put = chain.puts.copy()
    return df_call, df_put


# ============================================================
# 加 Greeks 與 BE 點
# ============================================================
def enrich_chain(df: pd.DataFrame, option_type: str, spot_price: float,
                 expiration: str, risk_free: float = 0.045,
                 dividend_yield: float = 0.0) -> pd.DataFrame:
    """
    給原始 option chain 加上：
      - dte（剩餘天數）
      - mid（買賣中價）
      - delta, gamma, theta_per_day, vega_per_1pct
      - break_even（盈虧平衡點）
      - distance_pct（BE 距現價百分比）
      - iv（% 顯示用）
    """
    if df.empty:
        return df

    df = df.copy()

    # 剩餘天數
    exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
    today = date.today()
    dte = max((exp_date - today).days, 1)
    df["dte"] = dte

    # 中價（bid/ask 不健康時 fallback 到 lastPrice）
    df["mid"] = df.apply(
        lambda r: (r["bid"] + r["ask"]) / 2 if (r.get("bid", 0) > 0 and r.get("ask", 0) > 0)
        else r.get("lastPrice", 0),
        axis=1,
    )

    # Greeks（逐列計算）
    greeks_list = []
    for _, r in df.iterrows():
        iv = r.get("impliedVolatility", 0)
        K = r.get("strike", 0)
        if pd.isna(iv) or iv <= 0 or K <= 0:
            greeks_list.append({"delta": np.nan, "gamma": np.nan,
                                "theta_per_day": np.nan, "vega_per_1pct": np.nan})
            continue
        greeks_list.append(bs_greeks(
            option_type=option_type,
            S=spot_price, K=float(K), T_days=dte,
            sigma=float(iv), r=risk_free, q=dividend_yield,
        ))
    df_greeks = pd.DataFrame(greeks_list, index=df.index)
    df = pd.concat([df, df_greeks], axis=1)

    # 盈虧平衡點 & 距現價%
    if option_type == "call":
        df["break_even"] = df["strike"] + df["mid"]
    else:
        df["break_even"] = df["strike"] - df["mid"]
    df["distance_pct"] = (df["break_even"] / spot_price - 1) * 100

    # IV 顯示成百分比
    df["iv_pct"] = (df["impliedVolatility"] * 100).round(1)

    return df


# ============================================================
# 智能標籤（新手安全包核心）
# ============================================================
def label_contract(row, option_type: str = "call") -> str:
    """
    給單一合約打上一個主要標籤。
    優先序：流動性差 > Theta黑洞 > 高IV > 太OTM > ITM穩 > 推薦 > 一般
    """
    delta = abs(row.get("delta", 0)) if pd.notna(row.get("delta")) else 0
    dte = row.get("dte", 0)
    iv = row.get("impliedVolatility", 0) or 0
    oi = row.get("openInterest", 0) or 0

    if oi < 100:
        return "❓ 流動性差"
    if dte < 14:
        return "💀 Theta 黑洞"
    if iv > 0.50:
        return "🔥 高 IV"
    if delta < 0.30:
        return "⚠️ 太 OTM"
    if delta > 0.70:
        return "💎 ITM 穩"
    if 0.45 <= delta <= 0.65 and 21 <= dte <= 45:
        return "⭐ 推薦"
    return "—"


def add_labels(df: pd.DataFrame, option_type: str = "call") -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["label"] = df.apply(lambda r: label_contract(r, option_type), axis=1)
    return df


# ============================================================
# 整合：給 UI 直接呼叫的 high-level 介面
# ============================================================
def build_buyer_view(ticker: str, expiration: str,
                     spot_price: float | None = None,
                     risk_free: float = 0.045) -> dict:
    """
    新手買方專屬：抓鏈 + Greeks + 標籤一條龍。

    Returns:
        {
            'spot': 145.20,
            'expiration': '2026-06-20',
            'dte': 30,
            'calls': DataFrame,   # 含 strike, mid, delta, theta_per_day, iv_pct,
                                  #     break_even, distance_pct, label, openInterest
            'puts':  DataFrame,
            'recommended_call': dict | None,   # ⭐ 推薦那一筆
            'recommended_put':  dict | None,
        }
    """
    if spot_price is None:
        spot_price = get_spot_price(ticker)
    if spot_price is None:
        return {"error": f"無法取得 {ticker} 現價"}

    try:
        df_call, df_put = fetch_option_chain(ticker, expiration)
    except Exception as e:
        return {"error": f"期權鏈抓取失敗：{e}"}

    df_call = enrich_chain(df_call, "call", spot_price, expiration, risk_free)
    df_put = enrich_chain(df_put, "put", spot_price, expiration, risk_free)
    df_call = add_labels(df_call, "call")
    df_put = add_labels(df_put, "put")

    # 挑出 ⭐ 推薦的合約（最接近 Delta 0.55 的那一筆）
    def _pick_star(df, target_delta=0.55):
        starred = df[df["label"] == "⭐ 推薦"].copy()
        if starred.empty:
            return None
        starred["_dist"] = (starred["delta"].abs() - target_delta).abs()
        best = starred.sort_values("_dist").iloc[0]
        return {
            "strike": float(best["strike"]),
            "mid": float(best["mid"]),
            "delta": float(best["delta"]),
            "theta_per_day": float(best["theta_per_day"]),
            "iv_pct": float(best["iv_pct"]),
            "break_even": float(best["break_even"]),
            "distance_pct": float(best["distance_pct"]),
            "openInterest": int(best["openInterest"]) if pd.notna(best["openInterest"]) else 0,
        }

    dte = int(df_call["dte"].iloc[0]) if not df_call.empty else (
        int(df_put["dte"].iloc[0]) if not df_put.empty else 0
    )

    return {
        "spot": round(spot_price, 2),
        "expiration": expiration,
        "dte": dte,
        "calls": df_call,
        "puts": df_put,
        "recommended_call": _pick_star(df_call, 0.55),
        "recommended_put": _pick_star(df_put, 0.55),
    }


# ============================================================
# 顯示用：擷取常用欄位給 Streamlit DataFrame
# ============================================================
DISPLAY_COLS = ["label", "strike", "mid", "bid", "ask",
                "delta", "theta_per_day", "iv_pct",
                "break_even", "distance_pct",
                "openInterest", "volume", "dte"]

DISPLAY_COL_NAMES = {
    "label": "標籤",
    "strike": "行權價",
    "mid": "中價",
    "bid": "Bid",
    "ask": "Ask",
    "delta": "Δ",
    "theta_per_day": "Θ/天",
    "iv_pct": "IV%",
    "break_even": "盈虧平衡",
    "distance_pct": "距現價%",
    "openInterest": "OI",
    "volume": "今日量",
    "dte": "剩餘天數",
}


def to_display_df(df: pd.DataFrame) -> pd.DataFrame:
    """轉成 UI 顯示用的精簡版"""
    if df.empty:
        return df
    cols = [c for c in DISPLAY_COLS if c in df.columns]
    out = df[cols].copy()
    out = out.rename(columns=DISPLAY_COL_NAMES)
    return out


if __name__ == "__main__":
    # 簡單測試
    sym = "NVDA"
    exps = list_expirations(sym)
    print(f"{sym} 可選到期日：{exps[:5]}")
    if exps:
        view = build_buyer_view(sym, exps[1])  # 用第二個到期日（第一個常是週選）
        if "error" in view:
            print(view["error"])
        else:
            print(f"現價：${view['spot']}  到期：{view['expiration']}（{view['dte']} 天）")
            print("\n⭐ 推薦 Call：", view["recommended_call"])
            print("\nCall 鏈前 10 筆：")
            print(to_display_df(view["calls"]).head(10).to_string(index=False))
