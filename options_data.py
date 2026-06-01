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

import json
import math
import os
import time
from datetime import datetime, date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ============================================================
# 磁碟快取 + 智能冷卻（雲端 yfinance 限流的補強）
# ============================================================
DISK_CACHE_DIR = Path("cache/options")
DISK_FRESH_HOURS = 6          # 6 小時內視為新鮮，直接用不打 API
COOLDOWN_429_SEC = 300        # 被限流後冷卻 5 分鐘
COOLDOWN_PREMIUM_SEC = 3600   # 403/402 視為永久鎖（session 內 1 小時不再試）

_source_cooldown: dict[str, float] = {}  # source → 解除時間（unix）


def _cooldown_remaining(source: str) -> int:
    """回傳冷卻剩餘秒數（0 表示已解除）"""
    until = _source_cooldown.get(source, 0)
    return max(0, int(until - time.time()))


def _mark_cooldown(source: str, duration_sec: int) -> None:
    _source_cooldown[source] = time.time() + duration_sec


def _classify_error_and_cooldown(source: str, err: str) -> None:
    """根據錯誤訊息決定冷卻時長"""
    if not err:
        return
    if "HTTP 403" in err or "HTTP 402" in err or "Payment" in err:
        _mark_cooldown(source, COOLDOWN_PREMIUM_SEC)
    elif "HTTP 429" in err or "Rate limit" in err or "Too Many Requests" in err:
        _mark_cooldown(source, COOLDOWN_429_SEC)
    # 其他暫時錯誤不冷卻，下次重試


def _disk_paths(ticker: str, expiration: str) -> Path:
    DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = f"{ticker}_{expiration}".replace("/", "_")
    return DISK_CACHE_DIR / f"{safe}.parquet"


def _disk_read(ticker: str, expiration: str
               ) -> tuple[pd.DataFrame, pd.DataFrame, float] | None:
    """讀磁碟快取。回傳 (df_call, df_put, mtime) 或 None"""
    p = _disk_paths(ticker, expiration)
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p)
        mtime = p.stat().st_mtime
        df_call = df[df["_kind"] == "call"].drop(columns=["_kind"]).reset_index(drop=True)
        df_put = df[df["_kind"] == "put"].drop(columns=["_kind"]).reset_index(drop=True)
        return df_call, df_put, mtime
    except Exception:
        return None


def _disk_write(ticker: str, expiration: str,
                df_call: pd.DataFrame, df_put: pd.DataFrame) -> None:
    try:
        c = df_call.copy()
        c["_kind"] = "call"
        p = df_put.copy()
        p["_kind"] = "put"
        combined = pd.concat([c, p], ignore_index=True)
        combined.to_parquet(_disk_paths(ticker, expiration))
    except Exception:
        pass


def clear_options_cache() -> int:
    """清空所有磁碟+記憶體快取，回傳清掉的檔案數"""
    n = 0
    if DISK_CACHE_DIR.exists():
        for p in DISK_CACHE_DIR.glob("*.parquet"):
            try:
                p.unlink()
                n += 1
            except Exception:
                pass
    _exp_cache.clear()
    _chain_cache.clear()
    _fh_chain_full_cache.clear() if "_fh_chain_full_cache" in globals() else None
    _source_cooldown.clear()
    return n


def cooldown_status() -> dict[str, int]:
    """回傳各 source 的冷卻剩餘秒數，供 UI 顯示"""
    return {s: _cooldown_remaining(s) for s in ("finnhub", "marketdata", "yfinance")
            if _cooldown_remaining(s) > 0}

# ============================================================
# Finnhub API（首選資料源，60 calls/min 免費版）
#   1. 註冊 https://finnhub.io/register → 收信驗證
#   2. Dashboard 首頁就有 API Key
#   3. 設 env var FINNHUB_TOKEN
#   ⚠️ /stock/option-chain 可能屬於 Premium。若回 403 會自動 fallback。
# ============================================================
FINNHUB_TOKEN = os.environ.get("FINNHUB_TOKEN", "").strip()
FINNHUB_BASE = "https://finnhub.io/api/v1"
_FH_AVAILABLE = bool(FINNHUB_TOKEN)
_FH_LAST_ERROR: str | None = None


def _fh_get(path: str, params: dict | None = None) -> dict | None:
    """Finnhub 共用 GET；token 走 query param（Finnhub 慣例）"""
    global _FH_LAST_ERROR
    if not _FH_AVAILABLE:
        return None
    try:
        params = dict(params or {})
        params["token"] = FINNHUB_TOKEN
        resp = requests.get(f"{FINNHUB_BASE}{path}", params=params, timeout=15)
        if resp.status_code != 200:
            _FH_LAST_ERROR = f"HTTP {resp.status_code}: {resp.text[:160]}"
            return None
        _FH_LAST_ERROR = None
        return resp.json()
    except Exception as e:
        _FH_LAST_ERROR = f"{type(e).__name__}: {e}"
        return None


def finnhub_status() -> str:
    if not FINNHUB_TOKEN:
        return "未設定 FINNHUB_TOKEN"
    if _FH_LAST_ERROR:
        return f"已啟用但最近一次呼叫失敗：{_FH_LAST_ERROR}"
    return "已啟用"


# Finnhub 的 option-chain 一次回所有到期日，整批快取
_fh_chain_full_cache: dict[str, tuple[float, dict]] = {}


def _fh_full_chain(ticker: str) -> dict | None:
    """GET /stock/option-chain?symbol=XXX → 一次取所有到期日的 chain

    回傳結構：
      {expiration: "YYYY-MM-DD" → {"CALL": [...], "PUT": [...]}}
    失敗回 None。
    """
    cached = _cache_get(_fh_chain_full_cache, ticker)
    if cached is not None:
        return cached
    j = _fh_get("/stock/option-chain", {"symbol": ticker})
    if j is None:
        return None
    data = j.get("data") or []
    if not data:
        return {}
    parsed: dict[str, dict] = {}
    for entry in data:
        exp = entry.get("expirationDate")
        if not exp:
            continue
        opts = entry.get("options") or {}
        parsed[exp] = {
            "CALL": opts.get("CALL") or [],
            "PUT": opts.get("PUT") or [],
        }
    _cache_set(_fh_chain_full_cache, ticker, parsed)
    return parsed


def _fh_list_expirations(ticker: str) -> list[str] | None:
    parsed = _fh_full_chain(ticker)
    if parsed is None:
        return None
    return sorted(parsed.keys())


def _fh_option_chain(ticker: str, expiration: str
                     ) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    parsed = _fh_full_chain(ticker)
    if parsed is None:
        return None
    entry = parsed.get(expiration)
    if entry is None:
        return pd.DataFrame(), pd.DataFrame()

    def _rows_to_df(rows: list[dict]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        out = []
        for o in rows:
            out.append({
                "strike": float(o.get("strike") or 0),
                "lastPrice": float(o.get("lastPrice") or 0),
                "bid": float(o.get("bid") or 0),
                "ask": float(o.get("ask") or 0),
                "volume": int(o.get("volume") or 0),
                "openInterest": int(o.get("openInterest") or 0),
                "impliedVolatility": float(o.get("impliedVolatility") or 0),
                "contractSymbol": o.get("contractName", ""),
            })
        return pd.DataFrame(out)

    return _rows_to_df(entry["CALL"]), _rows_to_df(entry["PUT"])


def _fh_spot(ticker: str) -> float | None:
    j = _fh_get("/quote", {"symbol": ticker})
    if j is None:
        return None
    try:
        c = j.get("c")
        return float(c) if c else None
    except Exception:
        return None


# ============================================================
# MarketData.app API（次要資料源，避開 Yahoo 對共用 IP 的限流）
#   1. 註冊 https://www.marketdata.app/
#   2. Dashboard 點 Generate Token → 信箱收 token
#   3. 設 env var MARKETDATA_TOKEN（Streamlit Cloud → Settings → Secrets）
#   免費版自動走 cached 端點（15 分鐘延遲），對選股完全夠用。
# ============================================================
MARKETDATA_TOKEN = os.environ.get("MARKETDATA_TOKEN", "").strip()
MARKETDATA_BASE = "https://api.marketdata.app/v1"
# 免費版沒 OPRA entitlement → 必須用 cached feed。付費後可改成 "live"。
MARKETDATA_FEED = os.environ.get("MARKETDATA_FEED", "cached").strip()
_MD_AVAILABLE = bool(MARKETDATA_TOKEN)
_MD_LAST_ERROR: str | None = None


def _md_get(path: str, params: dict | None = None,
            use_feed: bool = False) -> dict | None:
    """共用 GET 包裝；失敗回 None，原因留在 _MD_LAST_ERROR

    use_feed: 是否帶 feed=cached 參數。
      ✅ 適用：/options/chain/、/stocks/quotes/
      ❌ 不適用：/options/expirations/（會回 HTTP 402 Payment Required）
    """
    global _MD_LAST_ERROR
    if not _MD_AVAILABLE:
        return None
    try:
        params = dict(params or {})
        if use_feed:
            params.setdefault("feed", MARKETDATA_FEED)
        resp = requests.get(
            f"{MARKETDATA_BASE}{path}",
            headers={"Authorization": f"Bearer {MARKETDATA_TOKEN}",
                     "Accept": "application/json"},
            params=params,
            timeout=15,
        )
        # 203 = cached data（免費版正常狀態）；200 = live；其它都是失敗
        if resp.status_code not in (200, 203):
            _MD_LAST_ERROR = f"HTTP {resp.status_code}: {resp.text[:160]}"
            return None
        j = resp.json()
        # API 慣例：s == "ok" 或 "no_data" 才是正常回應
        if j.get("s") == "error":
            _MD_LAST_ERROR = f"API error: {j.get('errmsg', '')[:160]}"
            return None
        _MD_LAST_ERROR = None
        return j
    except Exception as e:
        _MD_LAST_ERROR = f"{type(e).__name__}: {e}"
        return None


def marketdata_status() -> str:
    if not MARKETDATA_TOKEN:
        return "未設定 MARKETDATA_TOKEN（純 yfinance 模式）"
    if _MD_LAST_ERROR:
        return f"已啟用（feed={MARKETDATA_FEED}）但最近一次呼叫失敗：{_MD_LAST_ERROR}"
    return f"已啟用（feed={MARKETDATA_FEED}）"


def _md_list_expirations(ticker: str) -> list[str] | None:
    """GET /v1/options/expirations/{ticker}/（不帶 feed 參數，會 402）"""
    j = _md_get(f"/options/expirations/{ticker}/", use_feed=False)
    if j is None:
        return None
    if j.get("s") == "no_data":
        return []
    exps = j.get("expirations")
    if not exps:
        return []
    return list(exps)


def _md_option_chain(ticker: str, expiration: str
                     ) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """
    GET /v1/options/chain/{ticker}/?expiration=YYYY-MM-DD

    MarketData 回傳是 column-oriented：每個欄位是 list。
    """
    j = _md_get(f"/options/chain/{ticker}/", {"expiration": expiration}, use_feed=True)
    if j is None:
        return None
    if j.get("s") == "no_data":
        return pd.DataFrame(), pd.DataFrame()

    # 必要欄位（缺哪個就略過該筆）
    sides = j.get("side") or []
    strikes = j.get("strike") or []
    n = len(strikes)
    if n == 0:
        return pd.DataFrame(), pd.DataFrame()

    def col(name, default=0):
        v = j.get(name)
        return v if (v and len(v) == n) else [default] * n

    df = pd.DataFrame({
        "strike": [float(x or 0) for x in strikes],
        "lastPrice": [float(x or 0) for x in col("last")],
        "bid": [float(x or 0) for x in col("bid")],
        "ask": [float(x or 0) for x in col("ask")],
        "volume": [int(x or 0) for x in col("volume")],
        "openInterest": [int(x or 0) for x in col("openInterest")],
        "impliedVolatility": [float(x or 0) for x in col("iv")],
        "contractSymbol": col("optionSymbol", ""),
        "option_type": [str(s or "").lower() for s in sides],
        "dte": [int(x or 0) for x in col("dte")],
    })
    # underlyingPrice 用來在 IV 缺失/異常時反推
    _under_list = j.get("underlyingPrice") or []
    spot_in_response = float(_under_list[0]) if _under_list else 0.0

    # MarketData cached 在盤後常見兩種異常：
    #   (1) IV 全 0（沒給）
    #   (2) IV 異常低（< 5%）或異常高（> 300%）—— 對美股一年期權都不合理
    # 兩種情況都用 lastPrice 反推（Black-Scholes Newton）讓 Delta/Theta 正常
    _PLAUSIBLE_LO, _PLAUSIBLE_HI = 0.05, 3.0
    if spot_in_response > 0 and (df["lastPrice"] > 0).any():
        fixed = []
        for _, r in df.iterrows():
            iv = r["impliedVolatility"]
            # 合理區間 → 沿用 MarketData 給的值
            if _PLAUSIBLE_LO <= iv <= _PLAUSIBLE_HI:
                fixed.append(iv)
                continue
            # 不合理 → 從 lastPrice 反推
            if r["lastPrice"] <= 0 or r["dte"] <= 0:
                fixed.append(iv)
                continue
            iv_est = implied_vol_from_price(
                option_type=r["option_type"],
                S=spot_in_response,
                K=float(r["strike"]),
                T_days=int(r["dte"]),
                market_price=float(r["lastPrice"]),
            )
            # 反推也回 0 → 保留原值（避免製造假數據）
            fixed.append(iv_est if iv_est > 0 else iv)
        df["impliedVolatility"] = fixed

    if df.empty:
        return df, df
    df_call = df[df["option_type"] == "call"].drop(columns=["option_type"]).reset_index(drop=True)
    df_put = df[df["option_type"] == "put"].drop(columns=["option_type"]).reset_index(drop=True)
    return df_call, df_put


def _md_spot(ticker: str) -> float | None:
    """GET /v1/stocks/quotes/{ticker}/"""
    j = _md_get(f"/stocks/quotes/{ticker}/", use_feed=True)
    if j is None or j.get("s") == "no_data":
        return None
    last = j.get("last")
    if not last:
        return None
    try:
        return float(last[0])
    except Exception:
        return None

# ============================================================
# 反 rate-limit：用 curl_cffi 偽裝成真實 Chrome
# Yahoo 對 requests / urllib 的標準 UA 很容易限流；curl_cffi 模擬 Chrome 的
# TLS 指紋 + HTTP/2 行為，通常可繞過。
# yfinance 0.2.40+ 支援透過 session= 參數注入。
# ============================================================
_YF_SESSION = None
_CURL_CFFI_STATUS = "未載入"
try:
    from curl_cffi import requests as _cc_requests
    _YF_SESSION = _cc_requests.Session(impersonate="chrome")
    _CURL_CFFI_STATUS = "已啟用（impersonate=chrome）"
except Exception as _e:
    _YF_SESSION = None
    _CURL_CFFI_STATUS = f"載入失敗：{type(_e).__name__}: {_e}"


def yf_session_status() -> str:
    """供 UI 顯示 curl_cffi 載入狀態"""
    return _CURL_CFFI_STATUS


def _ticker(symbol: str) -> "yf.Ticker":
    """建立 yf.Ticker，盡可能用 curl_cffi session 避免限流"""
    if _YF_SESSION is not None:
        try:
            return yf.Ticker(symbol, session=_YF_SESSION)
        except TypeError:
            # 舊版 yfinance 不接受 session 參數
            pass
    return yf.Ticker(symbol)


# ============================================================
# Process 內 TTL 快取（減少重複呼叫 yfinance）
# ============================================================
_TTL_SEC = 600  # 10 分鐘
_exp_cache: dict[str, tuple[float, list[str]]] = {}
_chain_cache: dict[tuple[str, str], tuple[float, tuple]] = {}


def _cache_get(d: dict, key):
    v = d.get(key)
    if v is None:
        return None
    ts, payload = v
    if time.time() - ts > _TTL_SEC:
        d.pop(key, None)
        return None
    return payload


def _cache_set(d: dict, key, payload) -> None:
    d[key] = (time.time(), payload)


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


def implied_vol_from_price(option_type: str, S: float, K: float, T_days: float,
                            market_price: float, r: float = 0.045,
                            initial_sigma: float = 0.30,
                            max_iter: int = 60, tol: float = 1e-5) -> float:
    """
    用 Newton 法從市價反推 IV（Black-Scholes）。
    用途：MarketData.app cached 免費版在盤後不回 IV，但 lastPrice 有，
          可從中價反推 IV，讓 Greeks / Delta 標籤系統正常運作。

    回 0 表示無法收斂（如 deep ITM 沒有時間價值、或市價低於內含價值）。
    """
    T = T_days / 365.0
    if T <= 0 or market_price <= 0 or S <= 0 or K <= 0:
        return 0.0
    # 內含價值，市價 < intrinsic → 無解（資料異常）
    intrinsic = max(S - K, 0.0) if option_type == "call" else max(K - S, 0.0)
    if market_price < intrinsic * 0.95:
        return 0.0
    sigma = max(initial_sigma, 0.05)
    for _ in range(max_iter):
        d1, d2 = _bs_d1_d2(S, K, T, r, sigma)
        if d1 is None:
            return 0.0
        if option_type == "call":
            theo = S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
        else:
            theo = K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
        vega = S * _norm_pdf(d1) * math.sqrt(T)  # 每 1.0 sigma 的價格變化
        if vega < 1e-8:
            return 0.0
        diff = theo - market_price
        if abs(diff) < tol:
            return max(0.001, min(sigma, 5.0))
        sigma -= diff / vega
        sigma = max(0.001, min(sigma, 5.0))  # 夾在合理範圍避免發散
    return max(0.001, min(sigma, 5.0))


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
                   bid: float, ask: float,
                   next_earnings: str | None = None,
                   expiration: str | None = None) -> list[dict]:
    """
    買方進場前 7 項風險檢查。回傳清單，每項：
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

    # 4. 流動性 - OI（yfinance 的 OI 常 lag/回 0，量大時放寬一格）
    oi = open_interest or 0
    _vol_for_oi = volume or 0
    if oi >= 500:
        items.append({"key": "oi", "label": "未平倉量 OI", "status": "✅",
                      "detail": f"{oi:,}（流動性充足）"})
    elif oi >= 100:
        items.append({"key": "oi", "label": "未平倉量 OI", "status": "⚠️",
                      "detail": f"{oi:,}（可接受）"})
    elif _vol_for_oi >= 50:
        # OI 顯示偏低但今日量很大 → 多半是 yfinance 資料 lag，不該判紅燈
        items.append({"key": "oi", "label": "未平倉量 OI", "status": "⚠️",
                      "detail": f"{oi:,}（資料偏低，但今日量 {_vol_for_oi:,} 充足）"})
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

    # 7. 跨財報日（IV crush 風險）
    if next_earnings and expiration:
        try:
            today = date.today()
            earn = datetime.strptime(next_earnings, "%Y-%m-%d").date()
            exp = datetime.strptime(expiration, "%Y-%m-%d").date()
            days_to_earn = (earn - today).days
            if today <= earn <= exp:
                # 確認跨財報 → 嚴重程度看距離財報多近
                if days_to_earn <= 7:
                    items.append({"key": "earnings", "label": "跨財報日", "status": "❌",
                                  "detail": f"{next_earnings}（{days_to_earn} 天後）IV crush 風險極高"})
                else:
                    items.append({"key": "earnings", "label": "跨財報日", "status": "⚠️",
                                  "detail": f"{next_earnings}（{days_to_earn} 天後）注意 IV crush"})
            else:
                items.append({"key": "earnings", "label": "跨財報日", "status": "✅",
                              "detail": f"下次財報 {next_earnings}（在合約到期之後）"})
        except Exception:
            items.append({"key": "earnings", "label": "跨財報日", "status": "✅",
                          "detail": "未檢出（資料解析失敗）"})
    else:
        items.append({"key": "earnings", "label": "跨財報日", "status": "✅",
                      "detail": "未檢出財報日（可能無近期財報或資料不可得）"})

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


# ============================================================
# 期權持倉評估（P2 用）
# ============================================================
def evaluate_option_position(pos: dict, risk_free: float = 0.045) -> dict | None:
    """
    給定一筆期權持倉，回傳即時評估結果。

    pos 結構：
        {
            'type': 'option',
            'sid': 'NVDA',
            'option_type': 'call' | 'put',
            'strike': 150.0,
            'expiration': '2026-06-20',
            'premium': 5.30,           # 進場權利金（每股）
            'contracts': 1,            # 口數
            'entry_date': '2026-05-31',
        }

    回傳 dict 包含：sid、進場/現價/損益/Greeks/DTE/警示。
    若找不到合約則 fallback 用 BS 估算。
    """
    try:
        sid = pos.get("sid")
        opt_type = pos.get("option_type", "call").lower()
        strike = float(pos.get("strike", 0))
        expiration = pos.get("expiration", "")
        entry_premium = float(pos.get("premium", 0))
        contracts = int(pos.get("contracts", 1))
        entry_date = pos.get("entry_date", "")
        if not (sid and strike and expiration and entry_premium):
            return None

        # 1. 抓現價
        spot = get_spot_price(sid)
        if spot is None:
            return None

        # 2. 抓當前合約價（從鏈裡找對應 strike）
        current_premium = None
        current_iv = None
        oi = 0
        try:
            df_c, df_p = fetch_option_chain(sid, expiration)
            df = df_c if opt_type == "call" else df_p
            match = df[df["strike"] == strike]
            if not match.empty:
                row = match.iloc[0]
                bid = float(row.get("bid", 0) or 0)
                ask = float(row.get("ask", 0) or 0)
                last = float(row.get("lastPrice", 0) or 0)
                current_premium = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
                current_iv = float(row.get("impliedVolatility", 0) or 0)
                oi = int(row.get("openInterest", 0) or 0)
        except Exception:
            pass

        # 3. DTE
        exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
        today = date.today()
        dte = (exp_date - today).days

        # 4. 若鏈裡查不到，用 BS 估算（用進場 IV 反推或 fallback 30%）
        if current_premium is None or current_premium <= 0:
            # 嘗試用進場資料反推 IV（簡化：假設 IV 不變）
            assumed_iv = current_iv if current_iv and current_iv > 0 else 0.30
            current_premium = bs_price(opt_type, spot, strike, max(dte, 0), assumed_iv, risk_free)
            current_iv = assumed_iv

        # 5. Greeks（用當前 IV）
        greeks = bs_greeks(opt_type, spot, strike, max(dte, 1),
                           current_iv if current_iv > 0 else 0.30, risk_free)

        # 6. 損益
        pnl_per_share = current_premium - entry_premium
        pnl_total = pnl_per_share * 100 * contracts
        cost_total = entry_premium * 100 * contracts
        return_pct = (pnl_per_share / entry_premium * 100) if entry_premium > 0 else 0.0

        # 7. 盈虧平衡與內含/外含
        if opt_type == "call":
            break_even = strike + entry_premium
            intrinsic = max(spot - strike, 0)
        else:
            break_even = strike - entry_premium
            intrinsic = max(strike - spot, 0)
        time_value = max(current_premium - intrinsic, 0)

        # 8. 警示
        alerts = []
        if dte <= 0:
            alerts.append("⏰ 已到期")
        elif dte <= 7:
            alerts.append("🚨 DTE≤7 建議平倉")
        elif dte <= 14:
            alerts.append("⚠️ DTE≤14 Theta 加速")

        if return_pct <= -50:
            alerts.append("🔴 觸發停損(-50%)")
        elif return_pct <= -30:
            alerts.append("🟡 虧損 -30%")

        if return_pct >= 100:
            alerts.append("🟢 達 +100% 可獲利了結")
        elif return_pct >= 50:
            alerts.append("🟢 +50% 可考慮減倉")

        if intrinsic > 0:
            alerts.append("💎 已 ITM")
        if time_value > 0 and dte > 0:
            tv_decay_per_day = abs(greeks.get("theta_per_day", 0))
            if tv_decay_per_day * 7 > time_value * 0.5 and dte < 21:
                alerts.append("⚡ 時間價值將快速燒完")

        # 跨財報警示（IV crush 風險）
        try:
            next_earn = get_next_earnings(sid)
            if next_earn and crosses_earnings(expiration, next_earn):
                earn_date = datetime.strptime(next_earn, "%Y-%m-%d").date()
                days_to_earn = (earn_date - date.today()).days
                if days_to_earn <= 3:
                    alerts.append(f"🚨 財報倒數 {days_to_earn}d，建議平倉避 IV crush")
                else:
                    alerts.append(f"📅 跨財報 ({next_earn})")
        except Exception:
            pass

        return {
            "代號": sid,
            "類型": f"{opt_type.upper()} ${strike:.2f}",
            "到期": expiration,
            "DTE": dte,
            "口數": contracts,
            "進場權利金": round(entry_premium, 2),
            "現價": round(spot, 2),
            "現權利金": round(current_premium, 2),
            "盈虧/口($)": round(pnl_per_share * 100, 2),
            "總損益($)": round(pnl_total, 2),
            "成本($)": round(cost_total, 2),
            "報酬%": round(return_pct, 1),
            "盈虧平衡": round(break_even, 2),
            "Delta": round(greeks["delta"], 3),
            "Theta/天": round(greeks["theta_per_day"], 3),
            "IV%": round(current_iv * 100, 1) if current_iv else 0.0,
            "內含價值": round(intrinsic, 2),
            "時間價值": round(time_value, 2),
            "進場日": entry_date,
            "警示": " ".join(alerts) if alerts else "✓ 正常",
        }
    except Exception as e:
        return {"代號": pos.get("sid", "?"), "警示": f"⚠️ 評估失敗：{e}"}


# ============================================================
# 自動推薦：對選股結果批次產生 ⭐ 推薦合約
# ============================================================
def recommend_for_tickers(tickers: list[str], target_dte: int = 30,
                          option_type: str = "call",
                          risk_free: float = 0.045,
                          avoid_earnings: bool = False,
                          post_earnings_dte: int = 14) -> list[dict]:
    """
    對一批 ticker，找出最接近 target_dte 的到期日，並回傳 ⭐ 推薦合約。

    Args:
        avoid_earnings: True 時，跳過跨財報的到期日，自動挑「財報後 N 天」最接近的
        post_earnings_dte: avoid_earnings=True 時的目標 DTE（從財報日算起）

    Returns: 每檔一筆 dict，包含 sid + 推薦 Call/Put 摘要 + 標籤。
    找不到推薦時欄位留 None。
    """
    results = []
    today = date.today()
    for sid in tickers:
        out = {"代號": sid, "推薦合約": None, "現價": None,
               "到期": None, "DTE": None, "Δ": None, "權利金": None,
               "盈虧平衡": None, "距現價%": None, "備註": ""}
        try:
            exps = list_expirations(sid)
            if not exps:
                out["備註"] = "無期權"
                results.append(out)
                continue

            # 決定到期日
            if avoid_earnings:
                chosen_exp = find_post_earnings_expiration(sid, post_earnings_dte)
                if chosen_exp is None:
                    out["備註"] = "❌ 找不到財報後到期"
                    results.append(out)
                    continue
            else:
                target = today + timedelta(days=target_dte)
                deltas = [abs((datetime.strptime(e, "%Y-%m-%d").date() - target).days)
                          for e in exps]
                chosen_exp = exps[deltas.index(min(deltas))]

            spot = get_spot_price(sid)
            if spot is None:
                out["備註"] = "無現價"
                results.append(out)
                continue
            out["現價"] = round(spot, 2)
            out["到期"] = chosen_exp

            view = build_buyer_view(sid, chosen_exp, spot_price=spot, risk_free=risk_free)
            if "error" in view:
                out["備註"] = view["error"]
                results.append(out)
                continue

            out["DTE"] = view["dte"]
            rec = view.get(f"recommended_{option_type}")
            if rec:
                out["推薦合約"] = f"{option_type.upper()} ${rec['strike']:.2f}"
                out["Δ"] = round(rec["delta"], 2)
                out["權利金"] = round(rec["mid"], 2)
                out["盈虧平衡"] = round(rec["break_even"], 2)
                out["距現價%"] = round(rec["distance_pct"], 2)
                out["備註"] = "⭐ 推薦（已避開財報）" if avoid_earnings else "⭐ 推薦"
            else:
                out["備註"] = "無 ⭐ 推薦（可手動到期權瀏覽查看其他標籤）"
        except Exception as e:
            out["備註"] = f"❌ {e}"
        results.append(out)
    return results


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
# ============================================================
# 財報日抓取（避免 IV Crush）
# ============================================================
_earnings_cache: dict[str, tuple[str, str | None]] = {}  # ticker → (抓取日, 下次財報日 ISO)


def get_next_earnings(ticker: str, cache_hours: int = 6) -> str | None:
    """
    抓取下次財報日（ISO 字串 YYYY-MM-DD）。找不到回 None。
    用 session 內字典快取避免重複呼叫 yfinance。

    來源優先序：
        1. ticker.calendar['Earnings Date']
        2. ticker.earnings_dates（時序資料）
    """
    today_iso = date.today().isoformat()
    # session cache hit
    if ticker in _earnings_cache:
        cached_day, cached_val = _earnings_cache[ticker]
        if cached_day == today_iso:
            return cached_val

    next_earn = None
    try:
        tk = _ticker(ticker)

        # 嘗試 1：calendar dict
        try:
            cal = tk.calendar
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date")
                if isinstance(ed, list) and ed:
                    ed = ed[0]
                if hasattr(ed, "isoformat"):
                    next_earn = ed.isoformat()[:10]
                elif isinstance(ed, str) and len(ed) >= 10:
                    next_earn = ed[:10]
        except Exception:
            pass

        # 嘗試 2：earnings_dates DataFrame
        if next_earn is None:
            try:
                df_ed = tk.earnings_dates
                if df_ed is not None and not df_ed.empty:
                    today = date.today()
                    # 索引可能是 DatetimeIndex（含時區），轉成 date 比較
                    dates_only = []
                    for ts in df_ed.index:
                        try:
                            d = ts.date() if hasattr(ts, "date") else None
                            if d:
                                dates_only.append(d)
                        except Exception:
                            continue
                    future = [d for d in dates_only if d >= today]
                    if future:
                        next_earn = min(future).isoformat()
            except Exception:
                pass
    except Exception:
        pass

    _earnings_cache[ticker] = (today_iso, next_earn)
    return next_earn


def list_expirations_after_earnings(ticker: str, next_earnings: str | None
                                    ) -> tuple[list[str], list[str]]:
    """
    將標的的可選到期日分成兩組：(財報前, 財報後)。

    若無財報資訊，全部歸為「財報前」（不影響原本選擇邏輯）。
    """
    exps = list_expirations(ticker)
    if not exps:
        return [], []
    if not next_earnings:
        return exps, []
    try:
        earn_date = datetime.strptime(next_earnings, "%Y-%m-%d").date()
        pre, post = [], []
        for e in exps:
            try:
                d = datetime.strptime(e, "%Y-%m-%d").date()
                (pre if d <= earn_date else post).append(e)
            except Exception:
                continue
        return pre, post
    except Exception:
        return exps, []


def find_post_earnings_expiration(ticker: str, target_dte_after: int = 14) -> str | None:
    """
    找「財報後 target_dte_after 天」最接近的到期日。

    用途：批次推薦時，自動避開 IV crush，挑選財報之後的合約。
    若該標的無財報資料，退回一般 30 天邏輯。
    """
    next_earn = get_next_earnings(ticker)
    exps = list_expirations(ticker)
    if not exps:
        return None

    if not next_earn:
        # 無財報 → 用一般 30 天目標
        target = date.today() + timedelta(days=30)
        deltas = [abs((datetime.strptime(e, "%Y-%m-%d").date() - target).days) for e in exps]
        return exps[deltas.index(min(deltas))]

    try:
        earn_date = datetime.strptime(next_earn, "%Y-%m-%d").date()
        target = earn_date + timedelta(days=target_dte_after)
        _, post_exps = list_expirations_after_earnings(ticker, next_earn)
        if not post_exps:
            return None
        deltas = [abs((datetime.strptime(e, "%Y-%m-%d").date() - target).days)
                  for e in post_exps]
        return post_exps[deltas.index(min(deltas))]
    except Exception:
        return None


def crosses_earnings(expiration: str, next_earn_iso: str | None) -> bool:
    """合約到期日是否跨越下次財報日（介於今天～到期 之間）"""
    if not next_earn_iso:
        return False
    try:
        today = date.today()
        exp = datetime.strptime(expiration, "%Y-%m-%d").date()
        earn = datetime.strptime(next_earn_iso, "%Y-%m-%d").date()
        return today <= earn <= exp
    except Exception:
        return False


_last_expirations_error: dict[str, str] = {}  # ticker → 最近一次失敗原因


def list_expirations(ticker: str) -> list[str]:
    """回傳該標的可選到期日清單。Tradier 優先、yfinance fallback。10 分鐘 TTL 快取。"""
    cached = _cache_get(_exp_cache, ticker)
    if cached is not None:
        _last_expirations_error.pop(ticker, None)
        return cached

    # 1. Finnhub 優先（順帶把整個 chain cache 起來，後面 fetch_option_chain 不用再打）
    if _FH_AVAILABLE:
        exps = _fh_list_expirations(ticker)
        if exps:
            _cache_set(_exp_cache, ticker, exps)
            _last_expirations_error.pop(ticker, None)
            return exps

    # 2. MarketData
    if _MD_AVAILABLE:
        exps = _md_list_expirations(ticker)
        if exps:
            _cache_set(_exp_cache, ticker, exps)
            _last_expirations_error.pop(ticker, None)
            return exps

    # 3. yfinance fallback
    try:
        tk = _ticker(ticker)
        exps = tk.options
        if not exps:
            notes = []
            if _FH_AVAILABLE and _FH_LAST_ERROR:
                notes.append(f"Finnhub 也失敗：{_FH_LAST_ERROR}")
            if _MD_AVAILABLE and _MD_LAST_ERROR:
                notes.append(f"MarketData 也失敗：{_MD_LAST_ERROR}")
            tail = ("；" + "；".join(notes)) if notes else ""
            _last_expirations_error[ticker] = (
                "yfinance 回傳空到期日清單（可能 rate limit、無期權或暫時 API 異常）" + tail
            )
            return []
        result = list(exps)
        _cache_set(_exp_cache, ticker, result)
        _last_expirations_error.pop(ticker, None)
        return result
    except Exception as e:
        notes = []
        if _FH_AVAILABLE and _FH_LAST_ERROR:
            notes.append(f"Finnhub：{_FH_LAST_ERROR}")
        if _MD_AVAILABLE and _MD_LAST_ERROR:
            notes.append(f"MarketData：{_MD_LAST_ERROR}")
        tail = ("；" + "；".join(notes)) if notes else ""
        _last_expirations_error[ticker] = f"{type(e).__name__}: {e}{tail}"
        return []


def last_expirations_error(ticker: str) -> str | None:
    """取得最近一次 list_expirations(ticker) 的失敗原因（沒失敗時回 None）"""
    return _last_expirations_error.get(ticker)


def get_spot_price(ticker: str) -> float | None:
    """抓即時/最新收盤價。Finnhub → MarketData → yfinance fallback。"""
    # 1. Finnhub
    if _FH_AVAILABLE:
        price = _fh_spot(ticker)
        if price and price > 0:
            return price
    # 2. MarketData.app
    if _MD_AVAILABLE:
        price = _md_spot(ticker)
        if price and price > 0:
            return price
    # 3. yfinance fallback
    try:
        tk = _ticker(ticker)
        hist = tk.history(period="5d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


def fetch_option_chain(ticker: str, expiration: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    抓取指定到期日的 Call + Put 鏈。

    完整流程（雲端強化版）：
      1. 10 分鐘記憶體快取（同 Streamlit session 重複查詢用）
      2. 6 小時磁碟快取（同程序重啟前的查詢結果）
      3. 試 Finnhub → MarketData → yfinance（各 source 冷卻中跳過）
         yfinance 失敗 + 非限流 → 等 3s 再試一次
      4. 全失敗 → 用磁碟舊資料（含過期），UI 顯示「資料 X 小時前」
      5. 完全沒舊資料 → raise
    """
    key = (ticker, expiration)

    # 1. 記憶體快取
    cached = _cache_get(_chain_cache, key)
    if cached is not None:
        return cached[0].copy(), cached[1].copy()

    # 2. 磁碟快取（新鮮）
    disk = _disk_read(ticker, expiration)
    if disk is not None:
        df_c, df_p, mtime = disk
        if time.time() - mtime < DISK_FRESH_HOURS * 3600:
            _cache_set(_chain_cache, key, (df_c, df_p))
            return df_c.copy(), df_p.copy()

    # 3. 跑 API 來源
    errors: list[str] = []

    def _save_and_return(df_c, df_p):
        _disk_write(ticker, expiration, df_c, df_p)
        _cache_set(_chain_cache, key, (df_c, df_p))
        return df_c.copy(), df_p.copy()

    # 3a. Finnhub（冷卻中跳過）
    if _FH_AVAILABLE:
        cd = _cooldown_remaining("finnhub")
        if cd > 0:
            errors.append(f"Finnhub：冷卻中（{cd}s 後解除）")
        else:
            tr = _fh_option_chain(ticker, expiration)
            if tr is not None and (not tr[0].empty or not tr[1].empty):
                return _save_and_return(tr[0], tr[1])
            if _FH_LAST_ERROR:
                _classify_error_and_cooldown("finnhub", _FH_LAST_ERROR)
                errors.append(f"Finnhub：{_FH_LAST_ERROR}")

    # 3b. MarketData
    if _MD_AVAILABLE:
        cd = _cooldown_remaining("marketdata")
        if cd > 0:
            errors.append(f"MarketData：冷卻中（{cd}s 後解除）")
        else:
            tr = _md_option_chain(ticker, expiration)
            if tr is not None and (not tr[0].empty or not tr[1].empty):
                return _save_and_return(tr[0], tr[1])
            if _MD_LAST_ERROR:
                _classify_error_and_cooldown("marketdata", _MD_LAST_ERROR)
                errors.append(f"MarketData：{_MD_LAST_ERROR}")

    # 3c. yfinance（冷卻中跳過；不在冷卻則最多重試 1 次）
    yf_cd = _cooldown_remaining("yfinance")
    if yf_cd > 0:
        errors.append(f"yfinance：冷卻中（{yf_cd}s 後解除）")
    else:
        last_yf_err = None
        for attempt in (1, 2):
            try:
                tk = _ticker(ticker)
                chain = tk.option_chain(expiration)
                return _save_and_return(chain.calls.copy(), chain.puts.copy())
            except Exception as e:
                last_yf_err = f"{type(e).__name__}: {e}"
                _classify_error_and_cooldown("yfinance", last_yf_err)
                # 被限流 → 直接放棄重試
                if _cooldown_remaining("yfinance") > 0:
                    break
                # 一般錯誤 → 等 3s 重試
                if attempt == 1:
                    time.sleep(3)
        if last_yf_err:
            errors.append(f"yfinance：{last_yf_err}")

    # 4. 全失敗 → 用過期磁碟快取
    if disk is not None:
        df_c, df_p, mtime = disk
        age_h = (time.time() - mtime) / 3600
        # 把過期警示放進 DataFrame 屬性，UI 可讀
        df_c.attrs["stale_hours"] = round(age_h, 1)
        df_p.attrs["stale_hours"] = round(age_h, 1)
        _cache_set(_chain_cache, key, (df_c, df_p))
        return df_c.copy(), df_p.copy()

    # 5. 完全沒有資料
    raise RuntimeError("；".join(errors) if errors else "未知錯誤")


# ============================================================
# 加 Greeks 與 BE 點
# ============================================================
def enrich_chain(df: pd.DataFrame, option_type: str, spot_price: float,
                 expiration: str, risk_free: float = 0.045,
                 dividend_yield: float = 0.0,
                 next_earnings: str | None = None) -> pd.DataFrame:
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

    # 跨財報旗標
    _cross = crosses_earnings(expiration, next_earnings)
    df["crosses_earnings"] = _cross
    df["earnings_flag"] = "🚨" if _cross else "—"

    return df


# ============================================================
# 智能標籤（新手安全包核心）
# ============================================================
def label_contract(row, option_type: str = "call") -> str:
    """
    給單一合約打上一個主要標籤，**每口都有明確分類**。

    優先序（由上往下優先）：
      1. ❓ 流動性差  (OI < 100 且 今日量 < 20)  → 排除：想出場沒人接
         （yfinance 的 openInterest 常 lag 1-2 天甚至回傳 0，
           因此加上 volume 作 fallback：只要今日有人交易就不算流動性差）
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
    # OI / volume 可能為 NaN，安全轉成 int
    _oi_raw = row.get("openInterest", 0)
    oi = int(_oi_raw) if pd.notna(_oi_raw) else 0
    _vol_raw = row.get("volume", 0)
    vol = int(_vol_raw) if pd.notna(_vol_raw) else 0

    # 先排除地雷（無法繼續看）
    # yfinance 的 openInterest 常常 lag 一天或直接回 0，單看 OI 會誤殺熱門合約
    # → 同時要求「OI 低 *且* 今日量也低」才視為流動性差
    if oi < 100 and vol < 20:
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
                     risk_free: float = 0.045,
                     df_daily: "pd.DataFrame | None" = None) -> dict:
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

    next_earn = get_next_earnings(ticker)
    df_call = enrich_chain(df_call, "call", spot_price, expiration, risk_free,
                           next_earnings=next_earn)
    df_put = enrich_chain(df_put, "put", spot_price, expiration, risk_free,
                          next_earnings=next_earn)
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

    # 推算跨財報的「天數差」（給 UI 顯示警示嚴重程度）
    days_to_earnings = None
    if next_earn:
        try:
            earn_date = datetime.strptime(next_earn, "%Y-%m-%d").date()
            days_to_earnings = (earn_date - date.today()).days
        except Exception:
            pass

    # IV drift（earnings drift）偵測：取 ATM Call IV 跟 RV 比
    iv_drift = None
    if df_daily is not None and not df_call.empty:
        try:
            _atm_row = df_call.copy()
            _atm_row["_d"] = (_atm_row["strike"] - spot_price).abs()
            _atm_iv = float(_atm_row.sort_values("_d").iloc[0].get("impliedVolatility", 0) or 0)
            if _atm_iv > 0:
                iv_drift = detect_iv_drift(_atm_iv, ticker, df_daily, next_earn)
        except Exception:
            pass

    # 財報後到期建議（給 UI「改選財報後到期」按鈕用）
    post_earnings_exp = None
    if next_earn:
        post_earnings_exp = find_post_earnings_expiration(ticker, target_dte_after=14)

    return {
        "spot": round(spot_price, 2),
        "expiration": expiration,
        "dte": dte,
        "calls": df_call,
        "puts": df_put,
        "recommended_call": _pick_star(df_call, 0.55),
        "recommended_put": _pick_star(df_put, 0.55),
        "next_earnings": next_earn,
        "days_to_earnings": days_to_earnings,
        "crosses_earnings": crosses_earnings(expiration, next_earn),
        "iv_drift": iv_drift,
        "post_earnings_expiration": post_earnings_exp,
    }


# ============================================================
# 顯示用：擷取常用欄位給 Streamlit DataFrame
# ============================================================
# ============================================================
# IV Rank 計算（讀 fetch_iv_history.py 累積的歷史）
# ============================================================
def compute_iv_rank(ticker: str, current_iv: float,
                    history_path: str | Path = "cache/iv_history.parquet",
                    lookback_days: int = 252) -> dict | None:
    """
    根據歷史 IV 計算當前的 IV Rank（百分位）。

    Returns:
        {'rank': 35.2, 'iv_min': 0.18, 'iv_max': 0.62, 'samples': 87}
        或 None（資料不足或檔案不存在）
    """
    from pathlib import Path as _P
    p = _P(history_path)
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p)
        sub = df[df["ticker"] == ticker].copy()
        if sub.empty:
            return None
        cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
        sub = sub[sub["date"] >= cutoff]
        if len(sub) < 5:
            return {"rank": None, "iv_min": None, "iv_max": None,
                    "samples": len(sub), "note": "資料不足"}
        iv_min = float(sub["iv"].min())
        iv_max = float(sub["iv"].max())
        if iv_max == iv_min:
            return {"rank": 50.0, "iv_min": iv_min, "iv_max": iv_max,
                    "samples": len(sub)}
        rank = (current_iv - iv_min) / (iv_max - iv_min) * 100
        rank = max(0, min(100, rank))
        return {"rank": round(rank, 1), "iv_min": iv_min, "iv_max": iv_max,
                "samples": len(sub)}
    except Exception:
        return None


def compute_rv_rank(ticker: str, df_daily: "pd.DataFrame | None" = None,
                    window: int = 20, lookback_days: int = 252) -> dict | None:
    """
    用既有股價快取計算「實現波動率（Realized Volatility）Rank」。
    在 IV 歷史資料累積足夠之前，這是 IV Rank 的近似替代品。

    公式：
        log_return = ln(close_t / close_{t-1})
        RV = rolling std(log_return, window=20) × √252
        RV Rank = (RV_now - RV_min_252d) / (RV_max_252d - RV_min_252d) × 100

    Returns:
        {'rank': 45.2, 'rv_now': 0.28, 'rv_min': 0.15, 'rv_max': 0.55, 'samples': 230}
        或 None（資料不足）
    """
    if df_daily is None or len(df_daily) == 0:
        return None
    try:
        sub = df_daily[df_daily["stock_id"] == ticker].copy()
        if sub.empty or len(sub) < window + 10:
            return None
        sub = sub.sort_values("date").reset_index(drop=True)
        sub["log_ret"] = np.log(sub["close"] / sub["close"].shift(1))
        sub["rv"] = sub["log_ret"].rolling(window).std() * np.sqrt(252)
        sub = sub.dropna(subset=["rv"]).tail(lookback_days)
        if len(sub) < 10:
            return None
        rv_now = float(sub["rv"].iloc[-1])
        rv_min = float(sub["rv"].min())
        rv_max = float(sub["rv"].max())
        if rv_max == rv_min:
            return {"rank": 50.0, "rv_now": rv_now, "rv_min": rv_min,
                    "rv_max": rv_max, "samples": len(sub)}
        rank = (rv_now - rv_min) / (rv_max - rv_min) * 100
        rank = max(0.0, min(100.0, rank))
        return {"rank": round(rank, 1), "rv_now": round(rv_now, 4),
                "rv_min": round(rv_min, 4), "rv_max": round(rv_max, 4),
                "samples": len(sub)}
    except Exception:
        return None


def detect_iv_drift(current_iv: float, ticker: str,
                    df_daily: "pd.DataFrame | None" = None,
                    next_earnings: str | None = None,
                    window: int = 20) -> dict | None:
    """
    偵測「財報前 IV 突增（earnings drift）」現象。

    原理：
        財報前 1-3 週，市場參與者買期權對沖/投機，推升 IV 遠高於實現波動率 (RV)。
        透過 IV/RV 比例可以量化這個「事件溢價」。

    判定條件（雙重）：
        1. IV/RV > 1.30（IV 比 RV 高出至少 30%）
        2. 下次財報日在 30 天內

    Returns:
        {
            'iv': 當前 IV (小數),
            'rv': 過去 20 日實現波動率 (小數),
            'iv_rv_ratio': IV/RV 比 (例如 1.45),
            'days_to_earnings': N 天 or None,
            'drift_level': 'normal' | 'elevated' | 'strong',
            'is_drift': bool,
            'msg': 描述字串,
        }
        或 None（無法計算）
    """
    rv_info = compute_rv_rank(ticker, df_daily, window=window)
    if not rv_info or "rv_now" not in rv_info or current_iv <= 0:
        return None

    rv = rv_info["rv_now"]
    if rv <= 0:
        return None

    ratio = current_iv / rv

    # 距財報天數
    days_to_earn = None
    if next_earnings:
        try:
            earn = datetime.strptime(next_earnings, "%Y-%m-%d").date()
            days_to_earn = (earn - date.today()).days
        except Exception:
            pass

    # IV/RV 分級
    if ratio < 1.20:
        level = "normal"
    elif ratio < 1.40:
        level = "elevated"
    else:
        level = "strong"

    # 是否確認 earnings drift：IV/RV 偏高 + 財報在 30 天內
    is_drift = (level in ("elevated", "strong")
                and days_to_earn is not None
                and 0 <= days_to_earn <= 30)

    # 描述
    if is_drift:
        if level == "strong":
            msg = (f"🔥 IV 暴衝（IV/RV={ratio:.2f}）— 財報 {days_to_earn} 天後，"
                   f"市場已大幅 pricing in，買方需謹慎")
        else:
            msg = (f"📈 IV 偏高（IV/RV={ratio:.2f}）— 財報 {days_to_earn} 天後，"
                   f"已有事件溢價")
    elif level != "normal":
        msg = f"⚠️ IV/RV={ratio:.2f}（偏高但無近期財報）"
    else:
        msg = f"✓ IV/RV={ratio:.2f}（正常）"

    return {
        "iv": round(current_iv, 4),
        "rv": round(rv, 4),
        "iv_rv_ratio": round(ratio, 2),
        "days_to_earnings": days_to_earn,
        "drift_level": level,
        "is_drift": is_drift,
        "msg": msg,
    }


def iv_history_status(history_path: str | Path = "cache/iv_history.parquet") -> dict:
    """回傳 IV 累積進度，給 UI 顯示『累積中』訊息用"""
    from pathlib import Path as _P
    p = _P(history_path)
    if not p.exists():
        return {"exists": False, "days": 0, "tickers": 0, "first_date": None, "last_date": None}
    try:
        df = pd.read_parquet(p)
        return {
            "exists": True,
            "days": df["date"].nunique(),
            "tickers": df["ticker"].nunique(),
            "first_date": df["date"].min(),
            "last_date": df["date"].max(),
            "rows": len(df),
        }
    except Exception:
        return {"exists": False, "days": 0, "tickers": 0, "first_date": None, "last_date": None}


DISPLAY_COLS = ["label", "earnings_flag", "strike", "mid", "bid", "ask",
                "delta", "theta_per_day", "iv_pct",
                "break_even", "distance_pct",
                "openInterest", "volume", "dte"]

DISPLAY_COL_NAMES = {
    "label": "標籤",
    "earnings_flag": "📅",
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
