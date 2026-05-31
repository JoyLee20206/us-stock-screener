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


def bs_price(option_type: str, S: float, K: float, T_days: float,
             sigma: float, r: float = 0.045, q: float = 0.0) -> float:
    """
    Black-Scholes 理論價格。用於 What-if 模擬器重算合約現值。
    參數同 bs_greeks。
    """
    T = T_days / 365.0
    # 到期當日（含 T<=0）→ 直接回內含價值
    if T <= 0:
        if option_type == "call":
            return max(S - K, 0.0)
        return max(K - S, 0.0)

    d1, d2 = _bs_d1_d2(S, K, T, r, sigma, q)
    if d1 is None:
        return 0.0

    if option_type == "call":
        return S * math.exp(-q * T) * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * math.exp(-q * T) * _norm_cdf(-d1)


def simulate_whatif(option_type: str, entry_premium: float,
                    strike: float, dte_now: int, spot_now: float, iv_now: float,
                    spot_pct_change: float, days_passed: int, iv_pct_change: float,
                    risk_free: float = 0.045) -> dict:
    """
    What-if 模擬：給定一組「未來情境」，回傳合約新理論價格與損益。

    Args:
        option_type: 'call' or 'put'
        entry_premium: 進場權利金（每股，例如 $5.30）
        strike, dte_now, spot_now, iv_now: 進場時的合約屬性
        spot_pct_change: 股價變動百分比（-10 ~ +10 表示 -10% ~ +10%）
        days_passed: 經過了幾天（0 ~ dte_now）
        iv_pct_change: IV 變動百分比（-30 ~ +30）

    Returns:
        {
            'new_spot': 新股價,
            'new_dte': 剩餘天數,
            'new_iv': 新 IV（小數）,
            'new_price': 合約新理論價,
            'pnl_per_share': 每股損益,
            'pnl_per_contract': 每口損益（×100）,
            'return_pct': 報酬率%,
        }
    """
    new_spot = spot_now * (1 + spot_pct_change / 100.0)
    new_dte = max(dte_now - days_passed, 0)
    new_iv = max(iv_now * (1 + iv_pct_change / 100.0), 0.001)

    new_price = bs_price(option_type, new_spot, strike, new_dte, new_iv, risk_free)
    pnl_per_share = new_price - entry_premium
    return {
        "new_spot": round(new_spot, 2),
        "new_dte": new_dte,
        "new_iv": round(new_iv, 4),
        "new_price": round(new_price, 4),
        "pnl_per_share": round(pnl_per_share, 4),
        "pnl_per_contract": round(pnl_per_share * 100, 2),
        "return_pct": round((pnl_per_share / entry_premium * 100) if entry_premium > 0 else 0.0, 2),
    }


def expiration_pnl_curve(option_type: str, strike: float, entry_premium: float,
                         spot_now: float, range_pct: float = 25.0,
                         num_points: int = 50) -> tuple[list[float], list[float]]:
    """
    產生到期日 P&L 曲線。

    Returns:
        (spot_prices, pnls_per_contract)
        每點 = (該收盤價對應的到期損益 × 100)
    """
    low = spot_now * (1 - range_pct / 100.0)
    high = spot_now * (1 + range_pct / 100.0)
    step = (high - low) / max(num_points - 1, 1)
    prices, pnls = [], []
    for i in range(num_points):
        s = low + step * i
        if option_type == "call":
            intrinsic = max(s - strike, 0.0)
        else:
            intrinsic = max(strike - s, 0.0)
        pnl = (intrinsic - entry_premium) * 100  # 一口 = 100 股
        prices.append(round(s, 2))
        pnls.append(round(pnl, 2))
    return prices, pnls


def risk_checklist(option_type: str, strike: float, mid: float,
                   delta: float, dte: int, iv: float,
                   open_interest: int, volume: int,
                   bid: float, ask: float) -> list[dict]:
    """
    買方進場前 6 項風險檢查。回傳清單，每項：
        {key, label, status: '✅'|'⚠️'|'❌', detail}
    最後可額外用 verdict() 整合成 GO/CAUTION/STOP。
    """
    items = []

    # 1. 到期天數
    if 21 <= dte <= 45:
        items.append({"key": "dte", "label": "到期天數", "status": "✅",
                      "detail": f"{dte} 天（21-45 是甜蜜點）"})
    elif 14 <= dte < 21 or 45 < dte <= 60:
        items.append({"key": "dte", "label": "到期天數", "status": "⚠️",
                      "detail": f"{dte} 天（偏離 21-45 區間）"})
    else:
        items.append({"key": "dte", "label": "到期天數", "status": "❌",
                      "detail": f"{dte} 天（{'太短，Theta 損耗快' if dte < 14 else '太長，資金占用久'}）"})

    # 2. Delta
    abs_d = abs(delta) if delta == delta else 0  # NaN check
    if 0.45 <= abs_d <= 0.65:
        items.append({"key": "delta", "label": "Delta 區間", "status": "✅",
                      "detail": f"{delta:.2f}（0.45-0.65 性價比甜蜜點）"})
    elif 0.30 <= abs_d < 0.45 or 0.65 < abs_d <= 0.75:
        items.append({"key": "delta", "label": "Delta 區間", "status": "⚠️",
                      "detail": f"{delta:.2f}（略偏 {'OTM' if abs_d < 0.45 else 'ITM'}）"})
    else:
        items.append({"key": "delta", "label": "Delta 區間", "status": "❌",
                      "detail": f"{delta:.2f}（{'太 OTM 中獎率低' if abs_d < 0.30 else '太貴'}）"})

    # 3. IV 高低
    iv_pct = iv * 100
    if iv_pct < 35:
        items.append({"key": "iv", "label": "隱含波動率", "status": "✅",
                      "detail": f"IV {iv_pct:.1f}%（不貴）"})
    elif iv_pct < 50:
        items.append({"key": "iv", "label": "隱含波動率", "status": "⚠️",
                      "detail": f"IV {iv_pct:.1f}%（中等，注意 IV crush）"})
    else:
        items.append({"key": "iv", "label": "隱含波動率", "status": "❌",
                      "detail": f"IV {iv_pct:.1f}%（過高，IV crush 風險大）"})

    # 4. 流動性 - OI
    oi = open_interest or 0
    if oi >= 500:
        items.append({"key": "oi", "label": "未平倉量 OI", "status": "✅",
                      "detail": f"{oi:,}（流動性充足）"})
    elif oi >= 100:
        items.append({"key": "oi", "label": "未平倉量 OI", "status": "⚠️",
                      "detail": f"{oi:,}（可接受）"})
    else:
        items.append({"key": "oi", "label": "未平倉量 OI", "status": "❌",
                      "detail": f"{oi:,}（過低，難出場）"})

    # 5. 流動性 - 今日量
    vol = volume or 0
    if vol >= 50:
        items.append({"key": "vol", "label": "今日成交量", "status": "✅",
                      "detail": f"{vol:,}"})
    elif vol >= 10:
        items.append({"key": "vol", "label": "今日成交量", "status": "⚠️",
                      "detail": f"{vol:,}（偏少）"})
    else:
        items.append({"key": "vol", "label": "今日成交量", "status": "❌",
                      "detail": f"{vol:,}（極低）"})

    # 6. Bid/Ask 價差
    if bid and ask and ask > 0:
        spread_pct = (ask - bid) / ask * 100
        if spread_pct < 5:
            items.append({"key": "spread", "label": "Bid/Ask 價差", "status": "✅",
                          "detail": f"{spread_pct:.1f}%（緊）"})
        elif spread_pct < 12:
            items.append({"key": "spread", "label": "Bid/Ask 價差", "status": "⚠️",
                          "detail": f"{spread_pct:.1f}%（可接受）"})
        else:
            items.append({"key": "spread", "label": "Bid/Ask 價差", "status": "❌",
                          "detail": f"{spread_pct:.1f}%（過寬，滑價大）"})
    else:
        items.append({"key": "spread", "label": "Bid/Ask 價差", "status": "❌",
                      "detail": "無有效 Bid/Ask"})

    return items


def verdict(checklist: list[dict]) -> dict:
    """根據檢查清單給整體判定。"""
    fails = sum(1 for it in checklist if it["status"] == "❌")
    warns = sum(1 for it in checklist if it["status"] == "⚠️")
    if fails >= 2:
        return {"light": "❌", "label": "STOP",
                "msg": f"{fails} 項紅燈，建議放棄這口合約另尋目標"}
    if fails == 1:
        return {"light": "⚠️", "label": "CAUTION",
                "msg": "有 1 項紅燈，建議檢視後再決定，或調整合約參數"}
    if warns >= 3:
        return {"light": "⚠️", "label": "CAUTION",
                "msg": f"{warns} 項黃燈，整體勉強但非最佳選擇"}
    return {"light": "✅", "label": "GO",
            "msg": "通過買方進場 6 項檢查，可考慮下單（仍需依個人風控評估）"}


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
    給單一合約打上一個主要標籤，**每口都有明確分類**。

    優先序（由上往下優先）：
      1. ❓ 流動性差  (OI < 100)            → 排除：想出場沒人接
      2. 💀 Theta 黑洞 (DTE < 14)            → 排除：時間損耗太快
      3. 🔥 高 IV     (IV > 50%)             → 注意：進場貴、IV crush 風險
      4. Delta 分級：
         ⚠️ 太 OTM    Delta < 0.30          → 排除：中獎率低
         🟡 略 OTM    0.30 ≤ Delta < 0.45   → 可看：性價比中等
         ⭐ 推薦      0.45 ≤ Delta ≤ 0.65 + DTE 21-45  → 新手最適合
         ⏱️ DTE 不對 同上 Delta 但 DTE 不在 21-45    → 可看：時間維度待調整
         🟠 略 ITM    0.65 < Delta ≤ 0.70   → 可看：偏保守
         💎 ITM 穩    Delta > 0.70          → 可看：貴但中獎率高
    """
    delta = abs(row.get("delta", 0)) if pd.notna(row.get("delta")) else 0
    dte = row.get("dte", 0)
    iv = row.get("impliedVolatility", 0) or 0
    oi = row.get("openInterest", 0) or 0

    # 先排除地雷（無法繼續看）
    if oi < 100:
        return "❓ 流動性差"
    if dte < 14:
        return "💀 Theta 黑洞"
    if iv > 0.50:
        return "🔥 高 IV"

    # 依 Delta 分級（DTE 已確認 >= 14）
    if delta < 0.30:
        return "⚠️ 太 OTM"
    if delta < 0.45:
        return "🟡 略 OTM"
    if delta <= 0.65:
        # 進入「推薦 Delta 區」，再看 DTE 是否在新手甜蜜點
        if 21 <= dte <= 45:
            return "⭐ 推薦"
        return "⏱️ DTE 不對"
    if delta <= 0.70:
        return "🟠 略 ITM"
    return "💎 ITM 穩"


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
