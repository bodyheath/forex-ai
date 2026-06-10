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


# ── Candlestick / price-action pattern detection ───────────────────────────────

def _pin_bar(c: pd.Series) -> "str | None":
    """Bullish pin (hammer-type) or bearish pin (shooting-star-type), else None.

    Strict: body ≤ 30% of range, dominant wick ≥ 60%, opposing wick ≤ 15%.
    """
    o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
    total = h - l
    if total < 1e-8:
        return None
    body    = abs(cl - o)
    lo_wick = min(o, cl) - l
    hi_wick = h - max(o, cl)
    if body / total > 0.30:
        return None
    if lo_wick / total >= 0.60 and hi_wick / total <= 0.15:
        return "bullish"
    if hi_wick / total >= 0.60 and lo_wick / total <= 0.15:
        return "bearish"
    return None


def _at_key_level(c: pd.Series, bb_lo: float, bb_hi: float, sma50: float,
                  pivot_vals: list, hi20: float, lo20: float) -> bool:
    """True if the candle's extreme (low or high) touched a key price level."""
    lo  = float(c["low"])
    hi  = float(c["high"])
    mid = float(c["close"])
    tol = mid * 0.006  # 0.6% tolerance

    for level in pivot_vals:
        if abs(lo - level) <= tol or abs(hi - level) <= tol or abs(mid - level) <= tol:
            return True
    if abs(lo - bb_lo) <= tol * 1.5 or abs(hi - bb_hi) <= tol * 1.5:
        return True
    if abs(lo - sma50) <= tol * 2 or abs(hi - sma50) <= tol * 2 or abs(mid - sma50) <= tol * 2:
        return True
    if abs(lo - lo20) <= tol * 2 or abs(hi - hi20) <= tol * 2:
        return True
    return False


def _local_peaks(arr: np.ndarray, n: int = 3) -> list:
    """Local maxima with at least n bars on each side."""
    result = []
    for i in range(n, len(arr) - n):
        if all(arr[i] > arr[i - j] for j in range(1, n + 1)) and \
           all(arr[i] > arr[i + j] for j in range(1, n + 1)):
            result.append((i, float(arr[i])))
    return result


def _local_troughs(arr: np.ndarray, n: int = 3) -> list:
    """Local minima with at least n bars on each side."""
    result = []
    for i in range(n, len(arr) - n):
        if all(arr[i] < arr[i - j] for j in range(1, n + 1)) and \
           all(arr[i] < arr[i + j] for j in range(1, n + 1)):
            result.append((i, float(arr[i])))
    return result


def _detect_candle_patterns(
    df: pd.DataFrame,
    bb_lo: float,
    bb_hi: float,
    sma50: float,
    pivots: dict,
    extra_levels: "list | None" = None,
) -> list:
    """Detect high-probability candlestick / price-action patterns on recent bars.

    Returns a list of pattern dicts:
      {"name": str, "direction": "bullish"|"bearish"|"neutral",
       "at_key_level": bool, "strength": "high"|"moderate"}

    extra_levels: additional key price levels (e.g. Fibonacci) to include in
    the at-key-level check so patterns coinciding with Fib levels are flagged.

    Patterns are intentionally conservative — false positives dilute the signal.
    """
    if len(df) < 10:
        return []

    tail   = df.tail(30).copy()
    hi20   = float(tail["high"].max())
    lo20   = float(tail["low"].min())
    p_vals = list(pivots.values()) if isinstance(pivots, dict) else []
    if extra_levels:
        p_vals = p_vals + [float(v) for v in extra_levels if v is not None]

    last  = tail.iloc[-1]
    prev  = tail.iloc[-2]
    prev2 = tail.iloc[-3] if len(tail) >= 3 else None

    found: list = []

    # 1. Pin bars (last two closed candles)
    for candle in (last, prev):
        pin = _pin_bar(candle)
        if pin is None:
            continue
        at_key = _at_key_level(candle, bb_lo, bb_hi, sma50, p_vals, hi20, lo20)
        name   = "pin bar at key level" if at_key else "pin bar"
        found.append({
            "name": name, "direction": pin,
            "at_key_level": at_key, "strength": "high" if at_key else "moderate",
        })

    # 2. Engulfing candles (most recent candle vs previous)
    c_o, c_c = float(last["open"]),  float(last["close"])
    p_o, p_c = float(prev["open"]),  float(prev["close"])
    c_body   = abs(c_c - c_o)
    p_body   = abs(p_c - p_o)
    if c_body >= p_body * 0.8 and p_body > 0:
        if c_c > c_o and p_c < p_o and c_c >= p_o and c_o <= p_c:
            found.append({"name": "bullish engulfing", "direction": "bullish",
                          "at_key_level": False, "strength": "high"})
        elif c_c < c_o and p_c > p_o and c_c <= p_o and c_o >= p_c:
            found.append({"name": "bearish engulfing", "direction": "bearish",
                          "at_key_level": False, "strength": "high"})

    # 3. Inside bar (consolidation — neutral, but signals breakout potential)
    if float(last["high"]) < float(prev["high"]) and float(last["low"]) > float(prev["low"]):
        found.append({"name": "inside bar", "direction": "neutral",
                      "at_key_level": False, "strength": "moderate"})

    # 4. Hammer / Shooting Star (classic 2:1 wick-to-body ratio)
    already_pin = any(p["name"].startswith("pin bar") for p in found)
    if not already_pin:
        for candle in (last, prev):
            o, h, l, cl = (float(candle[x]) for x in ("open", "high", "low", "close"))
            total = h - l
            if total < 1e-8:
                continue
            body = abs(cl - o)
            lo_w = min(o, cl) - l
            hi_w = h - max(o, cl)
            if body > 0:
                at_key = _at_key_level(candle, bb_lo, bb_hi, sma50, p_vals, hi20, lo20)
                if lo_w >= 2.0 * body and hi_w <= 0.3 * body:
                    found.append({"name": "hammer", "direction": "bullish",
                                  "at_key_level": at_key,
                                  "strength": "high" if at_key else "moderate"})
                    break
                if hi_w >= 2.0 * body and lo_w <= 0.3 * body:
                    found.append({"name": "shooting star", "direction": "bearish",
                                  "at_key_level": at_key,
                                  "strength": "high" if at_key else "moderate"})
                    break

    # 5. Morning / Evening Star (3-candle reversal)
    if prev2 is not None:
        c1_o, c1_c = float(prev2["open"]), float(prev2["close"])
        c2_o, c2_c = float(prev["open"]),  float(prev["close"])
        c3_o, c3_c = float(last["open"]),  float(last["close"])
        body1 = abs(c1_c - c1_o)
        body2 = abs(c2_c - c2_o)
        body3 = abs(c3_c - c3_o)
        mid1  = (c1_o + c1_c) / 2
        if body1 > 0 and body2 <= body1 * 0.4 and body3 >= body1 * 0.5:
            if c1_c < c1_o and c3_c > c3_o and c3_c > mid1:
                found.append({"name": "morning star", "direction": "bullish",
                              "at_key_level": False, "strength": "high"})
            elif c1_c > c1_o and c3_c < c3_o and c3_c < mid1:
                found.append({"name": "evening star", "direction": "bearish",
                              "at_key_level": False, "strength": "high"})

    # 6. Double Top / Bottom (structural pattern on last 30 bars)
    if len(tail) >= 20:
        hi_arr  = tail["high"].values
        lo_arr  = tail["low"].values
        cur_px  = float(last["close"])
        peaks   = _local_peaks(hi_arr)
        troughs = _local_troughs(lo_arr)

        if len(peaks) >= 2:
            p1, p2  = peaks[-2][1], peaks[-1][1]
            avg_top = (p1 + p2) / 2
            if avg_top > 0 and abs(p1 - p2) / avg_top <= 0.003 and cur_px < avg_top * 0.997:
                found.append({"name": "double top", "direction": "bearish",
                              "at_key_level": True, "strength": "high"})

        if len(troughs) >= 2:
            t1, t2  = troughs[-2][1], troughs[-1][1]
            avg_bot = (t1 + t2) / 2
            if avg_bot > 0 and abs(t1 - t2) / avg_bot <= 0.003 and cur_px > avg_bot * 1.003:
                found.append({"name": "double bottom", "direction": "bullish",
                              "at_key_level": True, "strength": "high"})

    # 7. Head and Shoulders / Inverse H&S (structural reversal)
    if len(tail) >= 15:
        hi_arr      = tail["high"].values
        lo_arr      = tail["low"].values
        hs_peaks    = _local_peaks(hi_arr, n=2)
        ihs_troughs = _local_troughs(lo_arr, n=2)

        if len(hs_peaks) >= 3:
            ls, hd, rs = hs_peaks[-3], hs_peaks[-2], hs_peaks[-1]
            if (hd[1] > ls[1] and hd[1] > rs[1] and
                    hd[1] > 0 and abs(ls[1] - rs[1]) / hd[1] <= 0.03):
                found.append({"name": "head and shoulders", "direction": "bearish",
                              "at_key_level": True, "strength": "high"})

        if len(ihs_troughs) >= 3:
            ls, hd, rs = ihs_troughs[-3], ihs_troughs[-2], ihs_troughs[-1]
            if (hd[1] < ls[1] and hd[1] < rs[1] and
                    abs(ls[1]) > 0 and abs(ls[1] - rs[1]) / abs(ls[1]) <= 0.03):
                found.append({"name": "inverse head and shoulders", "direction": "bullish",
                              "at_key_level": True, "strength": "high"})

    return found


# ── Fibonacci retracement / extension ─────────────────────────────────────────

_FIB_LEVELS = [
    (-0.618, "-61.8%"),
    (-0.272, "-27.2%"),
    ( 0.000,   "0.0%"),
    ( 0.236,  "23.6%"),
    ( 0.382,  "38.2%"),
    ( 0.500,  "50.0%"),
    ( 0.618,  "61.8%"),
    ( 0.786,  "78.6%"),
    ( 1.000, "100.0%"),
    ( 1.272, "127.2%"),
    ( 1.618, "161.8%"),
]


def _pip_size(price: float) -> float:
    """Return pip size: 0.01 for JPY-style pairs (price ≥ 10), 0.0001 for others."""
    return 0.01 if price >= 10.0 else 0.0001


def _fibonacci(df: pd.DataFrame, current_price: float) -> dict:
    """Identify the swing high/low from ~3 months of daily data and compute
    Fibonacci retracement (23.6%–78.6%) and extension (127.2%, 161.8%) levels.

    Returns:
      status        — "ok" | "insufficient" | "range too small"
      swing_high    — highest local peak in window
      swing_low     — lowest local trough in window
      range_pips    — swing range in pips
      levels        — {label: price} for all 11 levels
      near_levels   — levels within 10 pips of current_price, sorted by distance
      nearest_above — up to 3 nearest levels above current price [(label, price), ...]
      nearest_below — up to 3 nearest levels below current price
    """
    tail = df.tail(65)
    if len(tail) < 20:
        return {"status": "insufficient"}

    hi_arr = tail["high"].values
    lo_arr = tail["low"].values
    pip    = _pip_size(current_price)

    # Use n=5 for significant peaks/troughs — filters out minor noise
    peaks   = _local_peaks(hi_arr, n=5)
    troughs = _local_troughs(lo_arr, n=5)

    sh_v = max(peaks, key=lambda x: x[1])[1] if peaks else float(np.max(hi_arr))
    sl_v = min(troughs, key=lambda x: x[1])[1] if troughs else float(np.min(lo_arr))

    rng = sh_v - sl_v
    if rng < pip * 20:
        return {"status": "range too small", "range_pips": round(rng / pip)}

    range_pips = round(rng / pip)
    dec = 5 if pip < 0.001 else 3

    # All levels: level = swing_low + range × ratio
    levels: dict = {}
    for ratio, label in _FIB_LEVELS:
        levels[label] = round(sl_v + rng * ratio, dec)

    # Flag any level within 10 pips of current price
    near_levels = []
    for label, price in levels.items():
        dist_pips = abs(current_price - price) / pip
        if dist_pips <= 10:
            ltype = "support" if price < current_price else ("resistance" if price > current_price else "at")
            near_levels.append({
                "label": label, "price": price,
                "distance_pips": round(dist_pips, 1), "type": ltype,
            })
    near_levels.sort(key=lambda x: x["distance_pips"])

    nearest_above = sorted(
        [(lb, px) for lb, px in levels.items() if px > current_price],
        key=lambda x: x[1],
    )[:3]
    nearest_below = sorted(
        [(lb, px) for lb, px in levels.items() if px < current_price],
        key=lambda x: x[1], reverse=True,
    )[:3]

    return {
        "status":        "ok",
        "swing_high":    round(sh_v, dec),
        "swing_low":     round(sl_v, dec),
        "range_pips":    range_pips,
        "levels":        levels,
        "near_levels":   near_levels,
        "nearest_above": nearest_above,
        "nearest_below": nearest_below,
    }


def _detect_divergence(df: pd.DataFrame, rsi: pd.Series) -> dict:
    """Detect classic RSI divergence on the most recent 60 bars.

    Bullish: price making lower lows while RSI makes higher lows → BUY signal.
    Bearish: price making higher highs while RSI makes lower highs → SELL signal.

    Requirements: ≥5 bars between swing points, ≥5 pip price move, ≥2 RSI points.
    Returns {"bullish": dict|None, "bearish": dict|None}.
    """
    n = min(60, len(df))
    tail     = df.iloc[-n:]
    rsi_tail = rsi.iloc[-n:]

    if n < 20:
        return {"bullish": None, "bearish": None}

    hi_arr  = tail["high"].values
    lo_arr  = tail["low"].values
    rsi_arr = rsi_tail.values
    pip     = _pip_size(float(tail["close"].iloc[-1]))
    dec     = 5 if pip < 0.001 else 3

    peaks   = _local_peaks(hi_arr, n=3)
    troughs = _local_troughs(lo_arr, n=3)
    result  = {"bullish": None, "bearish": None}

    # ── Bearish: price higher high + RSI lower high ───────────────────────────
    if len(peaks) >= 2:
        (i1, ph1), (i2, ph2) = peaks[-2], peaks[-1]
        if i2 - i1 >= 5 and i1 < len(rsi_arr) and i2 < len(rsi_arr):
            rh1, rh2 = float(rsi_arr[i1]), float(rsi_arr[i2])
            pdiff    = (ph2 - ph1) / pip   # positive = higher high
            rdiff    = rh1 - rh2           # positive = RSI lower high
            if ph2 > ph1 and rh2 < rh1 and pdiff >= 5 and rdiff >= 2:
                strength = "strong" if pdiff >= 15 and rdiff >= 5 else "moderate"
                result["bearish"] = {
                    "type": "bearish", "strength": strength,
                    "price_diff_pips": round(pdiff, 1), "rsi_diff": round(rdiff, 1),
                    "price_h1": round(ph1, dec), "price_h2": round(ph2, dec),
                    "rsi_h1": round(rh1, 1),     "rsi_h2": round(rh2, 1),
                }

    # ── Bullish: price lower low + RSI higher low ─────────────────────────────
    if len(troughs) >= 2:
        (i1, pl1), (i2, pl2) = troughs[-2], troughs[-1]
        if i2 - i1 >= 5 and i1 < len(rsi_arr) and i2 < len(rsi_arr):
            rl1, rl2 = float(rsi_arr[i1]), float(rsi_arr[i2])
            pdiff    = (pl1 - pl2) / pip   # positive = lower low
            rdiff    = rl2 - rl1           # positive = RSI higher low
            if pl2 < pl1 and rl2 > rl1 and pdiff >= 5 and rdiff >= 2:
                strength = "strong" if pdiff >= 15 and rdiff >= 5 else "moderate"
                result["bullish"] = {
                    "type": "bullish", "strength": strength,
                    "price_diff_pips": round(pdiff, 1), "rsi_diff": round(rdiff, 1),
                    "price_l1": round(pl1, dec), "price_l2": round(pl2, dec),
                    "rsi_l1": round(rl1, 1),     "rsi_l2": round(rl2, 1),
                }

    return result


def _pattern_bonus(patterns: list, ts_direction: str) -> int:
    """Score bonus from confirming candlestick patterns (capped at +3).

    Direction map: bullish → BUY, bearish → SELL.
    Pin bar at key level or structural pattern (double top/H&S): +2.
    Other high-strength confirming pattern: +1.
    Neutral patterns (inside bar) contribute 0.
    """
    bonus = 0
    for p in patterns:
        p_dir = p.get("direction", "neutral")
        if p_dir == "neutral":
            continue
        p_ts = "BUY" if p_dir == "bullish" else "SELL"
        if p_ts != ts_direction:
            continue
        if p.get("at_key_level") and p.get("strength") == "high":
            bonus += 2
        elif p.get("strength") == "high":
            bonus += 1
    return min(3, bonus)


def _tech_signal(rsi14: float, macd_hist: float, bb_state: str, trend: str,
                 close: float = 0.0, sma20: float = 0.0, sma50: float = 0.0,
                 patterns: "list | None" = None) -> dict:
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

    # Pattern bonus from candlestick/price-action patterns
    if patterns:
        score += _pattern_bonus(patterns, direction)

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
        return {"timeframe": label, "status": "insufficient data", "candle_count": len(df)}
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

    rsi14_val     = round(rsi.iloc[-1], 1)
    macd_hist_val = round(hist.iloc[-1], 6)
    trend_str     = _trend(close, sma50, sma200)
    sma20_val     = float(sma20.iloc[-1])
    pivots        = _pivots(df.iloc[-2])
    fib           = _fibonacci(df, float(last))
    fib_vals      = list(fib["levels"].values()) if fib.get("status") == "ok" else []
    patterns      = _detect_candle_patterns(
        df, float(bb_lower), float(bb_upper), float(sma50), pivots,
        extra_levels=fib_vals,
    )

    return {
        "timeframe": label,
        "last_close": round(last, 5),
        "trend": trend_str,
        "rsi14": rsi14_val,
        "macd": macd_state,
        "macd_hist": macd_hist_val,
        "bollinger": bb_state,
        "sma20": round(sma20_val, 5),
        "sma50": round(sma50, 5),
        "sma200": (round(sma200, 5) if not np.isnan(sma200) else "n/a"),
        "atr14": round(atr.iloc[-1], 5),
        "recent_high_20": round(df["high"].tail(20).max(), 5),
        "recent_low_20": round(df["low"].tail(20).min(), 5),
        "pivots_from_last_candle": pivots,
        "fibonacci": fib,
        "patterns": patterns,
        "tech_signal": _tech_signal(
            rsi14_val, macd_hist_val, bb_state, trend_str,
            float(last), sma20_val, float(sma50), patterns=patterns,
        ),
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
            err_str = str(exc)
            log(f"  Failed  {pair} {interval}: {err_str[:120]}")
            if any(kw in err_str.lower() for kw in
                   ("not found", "invalid symbol", "no data", "no candles")):
                log(f"  ↳ Symbol format? Tried '{pair}' — verify on twelvedata.com/symbols")
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
        rsi14_val     = round(rsi.iloc[-1], 2)
        macd_hist_val = round(hist.iloc[-1], 6)
        trend_str     = _trend(close, sma50, sma200)
        sma20_val     = float(sma20.iloc[-1])
        pivots        = _pivots(df.iloc[-2])
        fib           = _fibonacci(df, float(last))
        fib_vals      = list(fib["levels"].values()) if fib.get("status") == "ok" else []
        patterns      = _detect_candle_patterns(
            df, float(bb_lower), float(bb_upper), float(sma50), pivots,
            extra_levels=fib_vals,
        )
        return {
            "pair":       pair,
            "rsi14":      rsi14_val,
            "macd_hist":  macd_hist_val,
            "macd_direction": "bullish" if macd_hist_val > 0 else "bearish",
            "trend":      trend_str,
            "bb_state":   bb_state,
            "sma20":      round(sma20_val, 5),
            "sma50":      round(float(sma50), 5),
            "fibonacci":  fib,
            "patterns":   patterns,
            "tech_signal": _tech_signal(
                rsi14_val, macd_hist_val, bb_state, trend_str,
                float(last), sma20_val, float(sma50), patterns=patterns,
            ),
        }
    except Exception:
        return None
