"""Technical layer.

Fetches OHLC candles from Twelve Data (daily + native 4h interval) and computes
indicators locally: RSI, MACD, Bollinger Bands, SMA50/200, ATR, recent
support/resistance and classic pivot points.

Computing locally (rather than calling per-indicator endpoints) keeps us to two
API calls per pair and means the indicator definitions are explicit and auditable.
Twelve Data's free tier (~800 calls/day, 8/min) comfortably covers this.
"""

import time

import numpy as np
import pandas as pd
import requests

import config
from src import cache

_TD_URL = "https://api.twelvedata.com/time_series"
_TIMEOUT = 30

_CACHE_TTL = 24.0  # default hours (also used for daily candles)

# Per-interval cache TTL.  Shorter-lived timeframes expire faster so intraday
# scans always see fresh 1H / 4H data without hammering the API.
_INTERVAL_TTL: dict = {
    "1month": 48.0,
    "1week":  24.0,
    "1day":   24.0,
    "4h":      6.0,
    "1h":      2.0,
}

# Per-run Twelve Data API call counter (cache misses only — live API calls).
_td_calls_this_run: int = 0


def reset_call_count() -> None:
    global _td_calls_this_run
    _td_calls_this_run = 0


def get_call_count() -> int:
    return _td_calls_this_run


def _td_request(symbol: str, interval: str, outputsize: int) -> dict:
    """Call Twelve Data time_series with caching and error/rate-limit detection."""
    key = f"TD:{symbol}:{interval}:{outputsize}"
    cached = cache.get(key, ttl_hours=_INTERVAL_TTL.get(interval, _CACHE_TTL))
    if cached is not None:
        return cached

    global _td_calls_this_run
    _td_calls_this_run += 1

    resp = requests.get(
        _TD_URL,
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "format": "JSON",
            "apikey": config.TWELVE_DATA_KEY,
        },
        timeout=_TIMEOUT,
    )
    if resp.status_code == 429:
        raise RuntimeError("Twelve Data rate limit reached (free tier 8/min, ~800/day). Try again shortly.")
    resp.raise_for_status()
    data = resp.json()

    # Twelve Data signals problems with {"status": "error", "code": ..., "message": ...}
    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(f"Twelve Data error ({data.get('code')}): {data.get('message')}")

    cache.set(key, data)
    return data


def _frame_from_td(data: dict) -> pd.DataFrame:
    """Convert a Twelve Data time_series payload into a sorted OHLC frame."""
    values = data.get("values") if isinstance(data, dict) else None
    if not values:
        raise RuntimeError("Twelve Data returned no candles: " + str(data)[:200])
    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()  # API returns newest-first
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[["open", "high", "low", "close"]].dropna()


# ---------------------------------------------------------------------------
# Indicator math
# ---------------------------------------------------------------------------
def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _macd(close: pd.Series):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd, signal, macd - signal


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _pivots(prev: pd.Series) -> dict:
    """Classic floor-trader pivots from the last completed candle."""
    p = (prev["high"] + prev["low"] + prev["close"]) / 3
    return {
        "pivot": round(p, 5),
        "r1": round(2 * p - prev["low"], 5),
        "s1": round(2 * p - prev["high"], 5),
        "r2": round(p + (prev["high"] - prev["low"]), 5),
        "s2": round(p - (prev["high"] - prev["low"]), 5),
    }


def _tech_signal(rsi14: float, macd_hist: float, bb_state: str, trend: str,
                 close: float = 0.0, sma20: float = 0.0, sma50: float = 0.0) -> dict:
    """Compute a Python-anchored technical signal score (1–10) and direction.

    RSI tiers set direction and base score.  MACD, Bollinger, SMA20/50 alignment,
    and trend each add or subtract 1 point.

    Floor rule: score can never be below 3 when real data is present.
    T:1 is reserved exclusively for missing / UNAVAILABLE data.

    RSI tiers (matches Haiku prompt exactly):
      <  30  → BUY  base 9    (deeply oversold)
      30–35  → BUY  base 7
      35–45  → BUY  base 5
      45–55  → NEUTRAL base 3
      55–65  → SELL base 4
      65–70  → SELL base 7
      >  70  → SELL base 9    (deeply overbought)
    """
    if rsi14 < 30:
        direction, base = "BUY", 9
    elif rsi14 < 35:
        direction, base = "BUY", 7
    elif rsi14 < 45:
        direction, base = "BUY", 5
    elif rsi14 > 70:
        direction, base = "SELL", 9
    elif rsi14 > 65:
        direction, base = "SELL", 7
    elif rsi14 > 55:
        direction, base = "SELL", 4
    else:  # 45–55
        direction, base = "NEUTRAL", 3

    score = base

    if direction == "NEUTRAL":
        # Bollinger extreme lifts neutral to 4 — price at a band is a tension point
        if "upper band" in bb_state or "lower band" in bb_state:
            score += 1
    else:
        # MACD confirmation (+1)
        if direction == "BUY" and macd_hist > 0:
            score += 1
        elif direction == "SELL" and macd_hist < 0:
            score += 1

        # Bollinger confirmation (+1)
        if direction == "BUY" and "lower band" in bb_state:
            score += 1
        elif direction == "SELL" and "upper band" in bb_state:
            score += 1

        # SMA20/SMA50 alignment (+1 confirming / -1 contradicting)
        if close > 0 and sma20 > 0 and sma50 > 0 and sma20 == sma20 and sma50 == sma50:
            if direction == "BUY":
                if close > sma20 and close > sma50:
                    score += 1
                elif close < sma20 and close < sma50:
                    score -= 1
            else:  # SELL
                if close < sma20 and close < sma50:
                    score += 1
                elif close > sma20 and close > sma50:
                    score -= 1

        # Trend alignment (+1) and contradiction (-1)
        trend_l = trend.lower()
        if direction == "BUY" and "uptrend" in trend_l:
            score += 1
        elif direction == "SELL" and "downtrend" in trend_l:
            score += 1
        if direction == "BUY" and "downtrend" in trend_l and "golden" not in trend_l:
            score -= 1
        elif direction == "SELL" and "uptrend" in trend_l and "death" not in trend_l:
            score -= 1

    # Floor at 3 — T:1 must only appear for genuinely missing data
    return {"direction": direction, "score": max(3, min(10, score))}


def _trend(close: pd.Series, sma50: float, sma200: float) -> str:
    last = close.iloc[-1]
    if np.isnan(sma200):
        bias = "above" if last > sma50 else "below"
        return f"price {bias} SMA50 (insufficient history for SMA200)"
    if sma50 > sma200 and last > sma50:
        return "uptrend (price > SMA50 > SMA200, golden-cross structure)"
    if sma50 < sma200 and last < sma50:
        return "downtrend (price < SMA50 < SMA200, death-cross structure)"
    return "mixed / range (price and MAs not aligned)"


def _summarise(df: pd.DataFrame, label: str) -> dict:
    if len(df) < 30:
        return {"timeframe": label, "status": "insufficient data"}
    close = df["close"]
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    sma50 = close.rolling(50).mean().iloc[-1]
    sma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else float("nan")
    rsi = _rsi(close)
    macd, signal, hist = _macd(close)
    atr = _atr(df)
    last = close.iloc[-1]

    macd_state = "bullish (MACD > signal)" if macd.iloc[-1] > signal.iloc[-1] else "bearish (MACD < signal)"
    bb_upper = (sma20 + 2 * std20).iloc[-1]
    bb_lower = (sma20 - 2 * std20).iloc[-1]
    if last >= bb_upper:
        bb_state = "at/above upper band (stretched, mean-reversion risk down)"
    elif last <= bb_lower:
        bb_state = "at/below lower band (stretched, mean-reversion risk up)"
    else:
        bb_state = "inside bands"

    rsi14_val  = round(rsi.iloc[-1], 1)
    macd_hist_val = round(hist.iloc[-1], 6)
    trend_str  = _trend(close, sma50, sma200)

    return {
        "timeframe": label,
        "last_close": round(last, 5),
        "trend": trend_str,
        "rsi14": rsi14_val,
        "macd": macd_state,
        "macd_hist": macd_hist_val,
        "bollinger": bb_state,
        "sma20": round(sma20.iloc[-1], 5),
        "sma50": round(sma50, 5),
        "sma200": (round(sma200, 5) if not np.isnan(sma200) else "n/a"),
        "atr14": round(atr.iloc[-1], 5),
        "recent_high_20": round(df["high"].tail(20).max(), 5),
        "recent_low_20": round(df["low"].tail(20).min(), 5),
        "pivots_from_last_candle": _pivots(df.iloc[-2]),
        "tech_signal": _tech_signal(rsi14_val, macd_hist_val, bb_state, trend_str),
    }


def analyse(base: str, quote: str) -> dict:
    """Return a technical summary dict for base/quote, or an error marker."""
    if not config.TWELVE_DATA_KEY:
        return {
            "status": "UNAVAILABLE",
            "error": "TWELVE_DATA_KEY not set in .env. Get a free key at "
            "https://twelvedata.com/pricing and add it to .env.",
        }

    symbol = f"{base}/{quote}"
    try:
        monthly = _frame_from_td(_td_request(symbol, "1month", 60))
        weekly  = _frame_from_td(_td_request(symbol, "1week",  200))
        daily   = _frame_from_td(_td_request(symbol, "1day",   400))
        four_h  = _frame_from_td(_td_request(symbol, "4h",     500))
        one_h   = _frame_from_td(_td_request(symbol, "1h",     300))
        return {
            "status":  "ok",
            "source":  "Twelve Data",
            "monthly": _summarise(monthly, "Monthly"),
            "weekly":  _summarise(weekly,  "Weekly"),
            "daily":   _summarise(daily,   "Daily"),
            "4h":      _summarise(four_h,  "4-Hour"),
            "1h":      _summarise(one_h,   "1-Hour"),
        }
    except Exception as exc:  # noqa: BLE001 - degrade gracefully
        return {"status": "UNAVAILABLE", "error": str(exc)}


def warm_cache(pairs: list, log=print) -> None:
    """Pre-fetch and cache candle data for all pairs before analysis begins.

    Fetches daily (400 candles) and 4h (500 candles) data for every pair,
    pacing at 7 API calls per 62 seconds to stay within the Twelve Data
    free-tier rate limit (8 calls/min).  Pairs whose data is already fresh
    in the 24-hour cache are skipped — no wasted calls.

    After this returns, every subsequent call to analyse() is a pure cache
    hit: no live API calls and no rate-limit risk during analysis.
    """
    if not config.TWELVE_DATA_KEY:
        log("Technical pre-fetch skipped: TWELVE_DATA_KEY not set.")
        return

    # Determine which (pair, interval) combinations need a live fetch.
    needed = []
    for pair in pairs:
        for interval, outputsize in (
            ("1month", 60), ("1week", 200), ("1day", 400), ("4h", 500), ("1h", 300)
        ):
            key = f"TD:{pair}:{interval}:{outputsize}"
            if cache.get(key, ttl_hours=_INTERVAL_TTL.get(interval, _CACHE_TTL)) is None:
                needed.append((pair, interval, outputsize))

    if not needed:
        log(
            f"Technical pre-fetch: all {len(pairs)} pair(s) already cached "
            f"({_CACHE_TTL:.0f}h TTL) — no API calls needed."
        )
        return

    log(
        f"Technical pre-fetch: {len(needed)} API call(s) needed "
        f"for {len(pairs)} pair(s) — 10s delay between calls ..."
    )

    api_n  = 0
    errors = 0
    for pair, interval, outputsize in needed:
        if api_n > 0:
            time.sleep(10)
        try:
            _td_request(pair, interval, outputsize)
            log(f"  Cached {pair} {interval}")
        except Exception as exc:  # noqa: BLE001
            log(f"  Failed  {pair} {interval}: {exc}")
            errors += 1
        api_n += 1

    status = "complete" if errors == 0 else f"complete with {errors} error(s)"
    log(f"Technical pre-fetch {status}: {api_n} API call(s) made.")


def read_cached_indicators(pair: str) -> dict | None:
    """Read daily indicator values from cache without making any API calls.

    Returns a dict with rsi14, macd_hist, tech_signal etc. or None if the
    daily candle data for this pair is not currently in the 24-hour cache.
    Used for diagnostic logging in the daily runner.
    """
    key = f"TD:{pair}:1day:400"
    cached = cache.get(key, ttl_hours=_CACHE_TTL)
    if cached is None:
        return None
    try:
        df = _frame_from_td(cached)
        if len(df) < 30:
            return None
        close = df["close"]
        rsi   = _rsi(close)
        _, _, hist = _macd(close)
        sma50  = close.rolling(50).mean().iloc[-1]
        sma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else float("nan")
        sma20  = close.rolling(20).mean()
        std20  = close.rolling(20).std()
        last   = close.iloc[-1]
        bb_upper = (sma20 + 2 * std20).iloc[-1]
        bb_lower = (sma20 - 2 * std20).iloc[-1]
        if last >= bb_upper:
            bb_state = "at/above upper band (stretched, mean-reversion risk down)"
        elif last <= bb_lower:
            bb_state = "at/below lower band (stretched, mean-reversion risk up)"
        else:
            bb_state = "inside bands"
        rsi14_val    = round(rsi.iloc[-1], 2)
        macd_hist_val = round(hist.iloc[-1], 6)
        trend_str    = _trend(close, sma50, sma200)
        return {
            "pair":       pair,
            "rsi14":      rsi14_val,
            "macd_hist":  macd_hist_val,
            "macd_direction": "bullish" if macd_hist_val > 0 else "bearish",
            "trend":      trend_str,
            "bb_state":   bb_state,
            "tech_signal": _tech_signal(rsi14_val, macd_hist_val, bb_state, trend_str),
        }
    except Exception:
        return None
