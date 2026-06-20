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
# scans always see fresh 4H data without hammering the API.
_INTERVAL_TTL: dict = {
    "1month": 48.0,
    "1week":  24.0,
    "1day":   24.0,
    "4h":      6.0,
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

    def _do_fetch(sym: str) -> dict:
        global _td_calls_this_run
        _td_calls_this_run += 1
        resp = requests.get(
            _TD_URL,
            params={
                "symbol": sym,
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
        return resp.json()

    # Alternative symbol format: slash ↔ no-slash (e.g. "AUD/JPY" ↔ "AUDJPY").
    # Twelve Data accepts both; some pairs only resolve under one format.
    _sym_clean = symbol.replace("/", "")
    _alt = (
        _sym_clean if "/" in symbol
        else f"{symbol[:3]}/{symbol[3:]}" if len(symbol) == 6
        else None
    )
    if _alt == symbol:
        _alt = None

    data = _do_fetch(symbol)

    # Twelve Data signals problems with {"status": "error", "code": ..., "message": ...}
    # On error: wait 30s and retry with alternative symbol format before failing.
    if isinstance(data, dict) and data.get("status") == "error":
        if _alt:
            time.sleep(30)
            data = _do_fetch(_alt)
        if isinstance(data, dict) and data.get("status") == "error":
            raise RuntimeError(f"Twelve Data error ({data.get('code')}): {data.get('message')}")

    # Empty candles: wait 30s and retry once before failing
    if isinstance(data, dict) and not data.get("values"):
        time.sleep(30)
        data = _do_fetch(symbol)
        if isinstance(data, dict) and data.get("status") == "error":
            raise RuntimeError(f"Twelve Data error ({data.get('code')}): {data.get('message')}")
        if isinstance(data, dict) and not data.get("values"):
            raise RuntimeError(
                f"Twelve Data returned no candles for {symbol} {interval} after retry"
            )

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


def _stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> "tuple":
    """Fast Stochastic Oscillator: %K and %D (signal line = SMA of %K)."""
    low_min  = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    d = k.rolling(d_period).mean()
    return k, d


def _cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Commodity Channel Index.  Mean-deviation form, constant 0.015."""
    tp      = (df["high"] + df["low"] + df["close"]) / 3
    tp_sma  = tp.rolling(period).mean()
    mean_dev = tp.rolling(period).apply(
        lambda x: np.mean(np.abs(x - x.mean())), raw=True
    )
    return (tp - tp_sma) / (0.015 * mean_dev.replace(0, np.nan))


_EMA_RIBBON_PERIODS = [8, 13, 21, 34, 55, 89]


def _ema_ribbon(close: pd.Series) -> dict:
    """Calculate EMA ribbon status using periods [8, 13, 21, 34, 55, 89].

    Status:
      ALIGNED_BULL — EMA8 > EMA13 > EMA21 > EMA34 > EMA55 > EMA89 (strong uptrend)
      ALIGNED_BEAR — EMA8 < EMA13 < EMA21 < EMA34 < EMA55 < EMA89 (strong downtrend)
      CONVERGING   — fully stacked but spread narrowing (trend weakening)
      LEANING_BULL / LEANING_BEAR — 4 of 5 pairs aligned
      NEUTRAL      — mixed / no clear stack

    fanning=True when fully aligned and spread is widening (trend accelerating).
    A fully aligned ribbon in the trade direction grants +2 to tech score.
    """
    periods = _EMA_RIBBON_PERIODS
    if len(close) < periods[-1] + 5:
        return {"status": "UNAVAILABLE", "direction": "NEUTRAL", "fanning": False}

    ema_series = {p: close.ewm(span=p, adjust=False).mean() for p in periods}
    cur        = {p: float(ema_series[p].iloc[-1]) for p in periods}

    n_pairs    = len(periods) - 1   # 5 consecutive pairs
    pairs_bull = sum(1 for i in range(n_pairs) if cur[periods[i]] > cur[periods[i + 1]])
    pairs_bear = sum(1 for i in range(n_pairs) if cur[periods[i]] < cur[periods[i + 1]])

    fully_bull = pairs_bull == n_pairs
    fully_bear = pairs_bear == n_pairs

    # Fanning vs converging: compare EMA8–EMA89 spread now vs 5 bars ago
    fanning    = False
    converging = False
    if fully_bull or fully_bear:
        try:
            spread_now  = abs(cur[8] - cur[89])
            prev        = {p: float(ema_series[p].iloc[-6]) for p in periods}
            spread_prev = abs(prev[8] - prev[89])
            if spread_now > spread_prev * 1.01:
                fanning    = True
            elif spread_now < spread_prev * 0.99:
                converging = True
        except (IndexError, KeyError, ValueError):
            pass

    if fully_bull and not converging:
        status, direction = "ALIGNED_BULL", "BUY"
    elif fully_bear and not converging:
        status, direction = "ALIGNED_BEAR", "SELL"
    elif (fully_bull or fully_bear) and converging:
        status    = "CONVERGING"
        direction = "BUY" if fully_bull else "SELL"
    elif pairs_bull >= 4:
        status, direction = "LEANING_BULL", "BUY"
    elif pairs_bear >= 4:
        status, direction = "LEANING_BEAR", "SELL"
    else:
        status, direction = "NEUTRAL", "NEUTRAL"

    return {
        "status":        status,
        "direction":     direction,
        "fanning":       fanning,
        "converging":    converging,
        "aligned_count": max(pairs_bull, pairs_bear),
        "ema8":  round(cur[8],  5),
        "ema13": round(cur[13], 5),
        "ema21": round(cur[21], 5),
        "ema34": round(cur[34], 5),
        "ema55": round(cur[55], 5),
        "ema89": round(cur[89], 5),
    }


def _oscillator_confluence(
    rsi14: float, stoch_k: float, stoch_d: float, cci20: float
) -> dict:
    """Check RSI, Stochastic, and CCI simultaneously for directional agreement.

    Thresholds:
      RSI:        < 35 = BUY-oversold   > 65 = SELL-overbought
      Stochastic: %K < 20 = BUY         %K > 80 = SELL  (%D must be same side)
      CCI:        < -100 = BUY          > +100 = SELL

    Returns:
      direction   — "BUY" | "SELL" | "NONE"
      score       — 0-3  (oscillators confirming)
      triple      — True when all three agree
      conf_label  — human-readable e.g. "BUY(3/3)" or "NONE"
      rsi_signal  — "BUY" | "SELL" | "NEUTRAL"
      stoch_signal
      cci_signal
      stoch_k, stoch_d, cci  — raw values
    """
    def _safe(v: float) -> float:
        return v if (v == v) else float("nan")   # NaN check

    rsi14   = _safe(rsi14)
    stoch_k = _safe(stoch_k)
    stoch_d = _safe(stoch_d)
    cci20   = _safe(cci20)

    def _classify(sig, buy_thresh, sell_thresh):
        if sig != sig:          # NaN → neutral
            return "NEUTRAL"
        return "BUY" if sig < buy_thresh else ("SELL" if sig > sell_thresh else "NEUTRAL")

    rsi_sig   = _classify(rsi14,   35,  65)
    # Stochastic: require both %K and %D on the same side for a cleaner signal
    stoch_sig = "NEUTRAL"
    if stoch_k == stoch_k and stoch_d == stoch_d:
        if stoch_k < 20 and stoch_d < 25:
            stoch_sig = "BUY"
        elif stoch_k > 80 and stoch_d > 75:
            stoch_sig = "SELL"
    cci_sig   = _classify(cci20, -100, 100)

    signals = [rsi_sig, stoch_sig, cci_sig]
    buy_n   = signals.count("BUY")
    sell_n  = signals.count("SELL")

    if buy_n >= 2 and sell_n == 0:
        direction = "BUY"
        score     = buy_n
    elif sell_n >= 2 and buy_n == 0:
        direction = "SELL"
        score     = sell_n
    else:
        direction = "NONE"
        score     = 0

    conf_label = f"{direction}({score}/3)" if direction != "NONE" else "NONE"

    return {
        "direction":    direction,
        "score":        score,
        "triple":       score == 3,
        "conf_label":   conf_label,
        "rsi_signal":   rsi_sig,
        "stoch_signal": stoch_sig,
        "cci_signal":   cci_sig,
        "stoch_k":      round(stoch_k, 1) if stoch_k == stoch_k else None,
        "stoch_d":      round(stoch_d, 1) if stoch_d == stoch_d else None,
        "cci":          round(cci20,   1) if cci20   == cci20   else None,
    }


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


def _pip_size(price_or_pair) -> float:
    """Return pip size based on pair name (preferred) or price heuristic.

    Pair-based (pass a string like "EUR/USD" or "USDJPY"):
      quote JPY  → 0.01    (USD/JPY, EUR/JPY, NOK/JPY, HKD/JPY …)
      base  JPY  → 0.000001 (JPY/USD, JPY/EUR — rare inverted pairs)
      else       → 0.0001  (all standard, HKD, SGD, NOK, SEK, MXN, ZAR …)

    Price heuristic fallback (pass a float): JPY pairs trade ≥ 50;
    NOK/SEK/HKD/MXN/ZAR pairs all trade < 50, so 50 is a safe boundary.
    """
    if isinstance(price_or_pair, str):
        cleaned = price_or_pair.upper().replace("/", "").replace("-", "")
        if len(cleaned) >= 6:
            quote = cleaned[3:6]
            base  = cleaned[:3]
            if quote == "JPY":
                return 0.01
            if base == "JPY":
                return 0.000001
        elif "JPY" in price_or_pair.upper():
            return 0.01
        return 0.0001
    # Numeric fallback: JPY pairs never trade below 50 (lowest AUD/JPY ≈ 80)
    return 0.01 if float(price_or_pair) >= 50.0 else 0.0001


def _fibonacci(df: pd.DataFrame, current_price: float, pair: str = "") -> dict:
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
    pip    = _pip_size(pair if pair else current_price)

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


def _detect_divergence(df: pd.DataFrame, rsi: pd.Series, pair: str = "") -> dict:
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
    pip     = _pip_size(pair if pair else float(tail["close"].iloc[-1]))
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
                 patterns: "list | None" = None,
                 ribbon: "dict | None" = None) -> dict:
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

    # MA ribbon bonus: fully aligned in trade direction = +2
    if ribbon and direction != "NEUTRAL":
        r_status = ribbon.get("status", "")
        if (direction == "BUY"  and r_status == "ALIGNED_BULL") or \
           (direction == "SELL" and r_status == "ALIGNED_BEAR"):
            score += 2

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


def _summarise(df: pd.DataFrame, label: str, pair: str = "") -> dict:
    if len(df) < 30:
        return {"timeframe": label, "status": "insufficient data", "candle_count": len(df)}
    close = df["close"]
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    sma50 = close.rolling(50).mean().iloc[-1]
    sma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else float("nan")
    rsi = _rsi(close)
    macd, signal, hist = _macd(close)
    atr  = _atr(df)
    stk, std = _stochastic(df)
    cci_s    = _cci(df)
    last     = close.iloc[-1]

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
    stoch_k_val   = float(stk.iloc[-1])
    stoch_d_val   = float(std.iloc[-1])
    cci_val       = float(cci_s.iloc[-1])
    osc_conf      = _oscillator_confluence(rsi14_val, stoch_k_val, stoch_d_val, cci_val)
    pivots        = _pivots(df.iloc[-2])
    fib           = _fibonacci(df, float(last), pair=pair)
    fib_vals      = list(fib["levels"].values()) if fib.get("status") == "ok" else []
    patterns      = _detect_candle_patterns(
        df, float(bb_lower), float(bb_upper), float(sma50), pivots,
        extra_levels=fib_vals,
    )
    divergence    = _detect_divergence(df, rsi, pair=pair)
    ribbon        = _ema_ribbon(close)

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
        "bb_upper": round(float(bb_upper), 5),
        "bb_lower": round(float(bb_lower), 5),
        "atr14": round(atr.iloc[-1], 5),
        "stochastic_k": round(stoch_k_val, 1) if stoch_k_val == stoch_k_val else None,
        "stochastic_d": round(stoch_d_val, 1) if stoch_d_val == stoch_d_val else None,
        "cci": round(cci_val, 1) if cci_val == cci_val else None,
        "oscillator_confluence": osc_conf,
        "recent_high_20": round(df["high"].tail(20).max(), 5),
        "recent_low_20": round(df["low"].tail(20).min(), 5),
        "pivots_from_last_candle": pivots,
        "fibonacci": fib,
        "patterns": patterns,
        "divergence": divergence,
        "ribbon": ribbon,
        "tech_signal": _tech_signal(
            rsi14_val, macd_hist_val, bb_state, trend_str,
            float(last), sma20_val, float(sma50), patterns=patterns,
            ribbon=ribbon,
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
        return {
            "status":  "ok",
            "source":  "Twelve Data",
            "monthly": _summarise(monthly, "Monthly", pair=symbol),
            "weekly":  _summarise(weekly,  "Weekly",  pair=symbol),
            "daily":   _summarise(daily,   "Daily",   pair=symbol),
            "4h":      _summarise(four_h,  "4-Hour",  pair=symbol),
        }
    except Exception as exc:  # noqa: BLE001 - degrade gracefully
        return {"status": "UNAVAILABLE", "error": str(exc)}


def warm_cache(pairs: list, log=print) -> None:
    """Pre-fetch and cache candle data for all pairs before analysis begins.

    Fetches monthly/weekly/daily/4H data for every pair, pacing at 7 API
    calls per 62 seconds to stay within the Twelve Data free-tier rate limit
    (8 calls/min).  Pairs whose data is already fresh in the cache are skipped.

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
            ("1month", 60), ("1week", 200), ("1day", 400), ("4h", 500)
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

    _pairs_needing_fetch: set = {p for p, _, _ in needed}
    _pairs_ok: set = set()
    api_n     = 0
    n_success = 0
    n_failed  = 0
    errors    = 0
    for pair, interval, outputsize in needed:
        if api_n > 0:
            time.sleep(10)
        try:
            _td_request(pair, interval, outputsize)
            log(f"  Cached {pair} {interval}")
            n_success += 1
            _pairs_ok.add(pair)
        except Exception as exc:  # noqa: BLE001
            err_str = str(exc)
            log(f"  Failed  {pair} {interval}: {err_str[:120]}")
            if any(kw in err_str.lower() for kw in
                   ("not found", "invalid symbol", "no data", "no candles")):
                log(f"  ↳ Symbol format? Tried '{pair}' — verify on twelvedata.com/symbols")
            n_failed += 1
            errors   += 1
        api_n += 1

    # Neutral fallbacks = pairs that needed a fetch but had zero successful timeframes
    n_neutral = len(_pairs_needing_fetch - _pairs_ok)
    status = "complete" if errors == 0 else f"complete with {errors} error(s)"
    log(
        f"Technical pre-fetch {status}: {api_n} API call(s) made. "
        f"Candle fetch: {n_success} successful, {n_failed} failed after retry, "
        f"{n_neutral} neutral fallbacks"
    )


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
        stk, std_s    = _stochastic(df)
        cci_s         = _cci(df)
        stoch_k_val   = float(stk.iloc[-1])
        stoch_d_val   = float(std_s.iloc[-1])
        cci_val       = float(cci_s.iloc[-1])
        osc_conf      = _oscillator_confluence(rsi14_val, stoch_k_val, stoch_d_val, cci_val)
        pivots        = _pivots(df.iloc[-2])
        fib           = _fibonacci(df, float(last), pair=pair)
        fib_vals      = list(fib["levels"].values()) if fib.get("status") == "ok" else []
        patterns      = _detect_candle_patterns(
            df, float(bb_lower), float(bb_upper), float(sma50), pivots,
            extra_levels=fib_vals,
        )
        divergence    = _detect_divergence(df, rsi, pair=pair)
        return {
            "pair":       pair,
            "rsi14":      rsi14_val,
            "macd_hist":  macd_hist_val,
            "macd_direction": "bullish" if macd_hist_val > 0 else "bearish",
            "trend":      trend_str,
            "bb_state":   bb_state,
            "sma20":      round(sma20_val, 5),
            "sma50":      round(float(sma50), 5),
            "stochastic_k": round(stoch_k_val, 1) if stoch_k_val == stoch_k_val else None,
            "stochastic_d": round(stoch_d_val, 1) if stoch_d_val == stoch_d_val else None,
            "cci":          round(cci_val, 1) if cci_val == cci_val else None,
            "oscillator_confluence": osc_conf,
            "fibonacci":  fib,
            "patterns":   patterns,
            "divergence": divergence,
            "tech_signal": _tech_signal(
                rsi14_val, macd_hist_val, bb_state, trend_str,
                float(last), sma20_val, float(sma50), patterns=patterns,
            ),
        }
    except Exception:
        return None


def get_monthly_trend(pair: str) -> dict:
    """Return the dominant monthly trend direction for a forex pair.

    Reads from the already-cached TD:{pair}:1month:60 data — zero API calls.
    Falls back to NEUTRAL when the cache is cold or data is insufficient.

    Returns:
        {"trend": "BUY" | "SELL" | "NEUTRAL",
         "strength": "strong" | "moderate" | "weak",
         "n_months": int}  — number of complete months used
    """
    cached = cache.get(f"TD:{pair}:1month:60", ttl_hours=48.0)
    if not isinstance(cached, dict) or not cached.get("values"):
        return {"trend": "NEUTRAL", "strength": "weak", "n_months": 0}

    from datetime import datetime as _dt
    _now_prefix = _dt.utcnow().strftime("%Y-%m")

    closes, opens, highs, lows = [], [], [], []
    for v in cached["values"]:
        # Skip the current (potentially incomplete) month
        if (v.get("datetime") or "")[:7] == _now_prefix:
            continue
        try:
            closes.append(float(v["close"]))
            opens.append(float(v["open"]))
            highs.append(float(v["high"]))
            lows.append(float(v["low"]))
        except (KeyError, TypeError, ValueError):
            pass
        if len(closes) >= 6:
            break

    if len(closes) < 3:
        return {"trend": "NEUTRAL", "strength": "weak", "n_months": len(closes)}

    # Monthly ATR (average true range proxy: high − low per candle)
    n = min(5, len(closes))
    monthly_atr = sum(highs[i] - lows[i] for i in range(n)) / n if n > 0 else 0

    # 3-month directional move (closes[0] = last complete month, closes[2] = 3 months ago)
    n_back = min(2, len(closes) - 1)
    move_3m = closes[0] - closes[n_back]

    # Candle colour count over last 3 complete months
    n_check = min(3, len(closes))
    n_bull = sum(1 for i in range(n_check) if closes[i] > opens[i])
    n_bear = sum(1 for i in range(n_check) if closes[i] < opens[i])

    # Significance threshold: move must be at least 20% of monthly ATR
    sig = monthly_atr * 0.20 if monthly_atr > 0 else 0

    if move_3m > sig and n_bull >= 2:
        strength = "strong" if (move_3m > monthly_atr * 0.7 or n_bull == 3) else "moderate"
        return {"trend": "BUY", "strength": strength, "n_months": n_check}
    elif move_3m < -sig and n_bear >= 2:
        strength = "strong" if (move_3m < -monthly_atr * 0.7 or n_bear == 3) else "moderate"
        return {"trend": "SELL", "strength": strength, "n_months": n_check}
    else:
        return {"trend": "NEUTRAL", "strength": "weak", "n_months": n_check}


def get_trend_structure(pair: str) -> dict:
    """Identify swing-based HH/HL (uptrend) or LH/LL (downtrend) structure.

    Reads from the already-cached TD:{pair}:1day:400 data — zero API calls.
    Uses a 5-bar swing detector: a swing high requires the candle's high to be
    strictly greater than the 5 candles on each side; likewise for swing lows.

    Returns:
        {
          "status":     "ok" | "insufficient",
          "buy_valid":  bool | None,   # True = HH + HL confirmed
          "sell_valid": bool | None,   # True = LH + LL confirmed
          "hh": bool, "hl": bool,      # individual higher-high / higher-low flags
          "lh": bool, "ll": bool,      # individual lower-high  / lower-low  flags
          "detail": str,               # human-readable summary of structure seen
        }
    """
    cached = cache.get(f"TD:{pair}:1day:400", ttl_hours=24.0)
    if not isinstance(cached, dict) or not cached.get("values"):
        return {"status": "insufficient", "buy_valid": None, "sell_valid": None,
                "hh": False, "hl": False, "lh": False, "ll": False, "detail": "no data"}

    highs: list = []
    lows:  list = []
    # values are newest-first; take last 120 candles and reverse to oldest-first
    for v in reversed((cached["values"] or [])[:120]):
        try:
            highs.append(float(v["high"]))
            lows.append(float(v["low"]))
        except (KeyError, TypeError, ValueError):
            pass

    n = len(highs)
    if n < 30:
        return {"status": "insufficient", "buy_valid": None, "sell_valid": None,
                "hh": False, "hl": False, "lh": False, "ll": False, "detail": "insufficient data"}

    SWING = 5  # candles on each side required to confirm a swing point

    swing_highs: list = []  # chronological: oldest → newest
    swing_lows:  list = []

    for i in range(SWING, n - SWING):
        h = highs[i]
        lo = lows[i]
        if all(h > highs[i - k] for k in range(1, SWING + 1)) and \
           all(h > highs[i + k] for k in range(1, SWING + 1)):
            swing_highs.append(h)
        if all(lo < lows[i - k] for k in range(1, SWING + 1)) and \
           all(lo < lows[i + k] for k in range(1, SWING + 1)):
            swing_lows.append(lo)

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {"status": "insufficient", "buy_valid": None, "sell_valid": None,
                "hh": False, "hl": False, "lh": False, "ll": False,
                "detail": "not enough swing points"}

    # Most recent two of each (list is oldest-first, so [-1] = most recent)
    sh1, sh2 = swing_highs[-1], swing_highs[-2]
    sl1, sl2 = swing_lows[-1],  swing_lows[-2]

    hh = sh1 > sh2  # higher high
    hl = sl1 > sl2  # higher low
    lh = sh1 < sh2  # lower high
    ll = sl1 < sl2  # lower low

    if hh and hl:
        detail = "higher highs and higher lows"
    elif lh and ll:
        detail = "lower highs and lower lows"
    elif hh and ll:
        detail = "higher highs but lower lows (diverging)"
    elif lh and hl:
        detail = "lower highs but higher lows (converging)"
    elif ll:
        detail = "lower low detected"
    elif lh:
        detail = "lower high detected"
    elif hh:
        detail = "higher high, flat low"
    elif hl:
        detail = "higher low, flat high"
    else:
        detail = "neutral structure"

    return {
        "status":     "ok",
        "buy_valid":  bool(hh and hl),
        "sell_valid": bool(lh and ll),
        "hh": hh, "hl": hl,
        "lh": lh, "ll": ll,
        "detail": detail,
    }


def get_market_structure(pair: str) -> dict:
    """Detect market structure breaks from the most recent 20 daily candles.

    A bullish break occurs when the latest close exceeds the most recent
    2-bar pivot swing high in the preceding 19 candles.  A bearish break
    occurs when the latest close falls below the most recent swing low.

    Reads from TD:{pair}:1day:400 cache — zero new API calls.

    Returns:
        {
          "result":       "BULLISH_BREAK" | "BEARISH_BREAK" | "CONTINUATION",
          "swing_high":   float | None,   most recent pivot high found
          "swing_low":    float | None,   most recent pivot low found
          "break_level":  float | None,   the level that was broken (if any)
          "latest_close": float | None,
        }
    """
    _neutral = {
        "result": "CONTINUATION",
        "swing_high": None, "swing_low": None,
        "break_level": None, "latest_close": None,
    }

    cached = cache.get(f"TD:{pair}:1day:400", ttl_hours=24.0)
    if not isinstance(cached, dict) or not cached.get("values"):
        return _neutral

    raw = (cached["values"] or [])[:20]
    if len(raw) < 8:
        return _neutral

    # Reverse to chronological order (oldest first)
    arr = list(reversed(raw))

    highs, lows, closes = [], [], []
    for v in arr:
        try:
            highs.append(float(v["high"]))
            lows.append(float(v["low"]))
            closes.append(float(v["close"]))
        except (KeyError, TypeError, ValueError):
            pass

    n = len(highs)
    if n < 8:
        return _neutral

    latest_close = closes[-1]

    # 2-bar pivot: candle i must be strictly greater than the 2 candles on each side.
    # Scan within arr[0 : n-1] (historical candles only) from newest to oldest.
    SWING    = 2
    scan_max = n - 1 - SWING - 1  # last index within the historical window with right-side room
    scan_min = SWING

    swing_high: float = None
    swing_low:  float = None

    for i in range(scan_max, scan_min - 1, -1):
        if swing_high is None:
            if all(highs[i] > highs[i - k] for k in range(1, SWING + 1)) and \
               all(highs[i] > highs[i + k] for k in range(1, SWING + 1)):
                swing_high = highs[i]
        if swing_low is None:
            if all(lows[i] < lows[i - k] for k in range(1, SWING + 1)) and \
               all(lows[i] < lows[i + k] for k in range(1, SWING + 1)):
                swing_low = lows[i]
        if swing_high is not None and swing_low is not None:
            break

    result      = "CONTINUATION"
    break_level = None

    if swing_high is not None and latest_close > swing_high:
        result      = "BULLISH_BREAK"
        break_level = swing_high
    elif swing_low is not None and latest_close < swing_low:
        result      = "BEARISH_BREAK"
        break_level = swing_low

    return {
        "result":       result,
        "swing_high":   swing_high,
        "swing_low":    swing_low,
        "break_level":  break_level,
        "latest_close": latest_close,
    }


def get_rsi_divergence(pair: str) -> dict:
    """Detect RSI divergence from daily OHLCV data.

    Computes RSI-14 (Wilder's smoothing) over the most recent 150 daily
    candles and identifies 3-bar pivot swing highs and lows.  Compares
    the last two swings of each type to classify divergence.

    Low-based (swing lows):
      BULLISH        — price lower low  + RSI higher low  (reversal signal)
      HIDDEN_BULLISH — price higher low + RSI lower low   (continuation signal)

    High-based (swing highs):
      BEARISH        — price higher high + RSI lower high  (reversal signal)
      HIDDEN_BEARISH — price lower high  + RSI higher high (continuation signal)

    Returns:
        {
          "low_divergence":  "BULLISH" | "HIDDEN_BULLISH" | "NONE",
          "high_divergence": "BEARISH" | "HIDDEN_BEARISH" | "NONE",
          "low_details":  {"price_level", "rsi_level", "prev_price", "prev_rsi"} | None,
          "high_details": same structure | None,
        }
    """
    _neutral = {
        "low_divergence":  "NONE",
        "high_divergence": "NONE",
        "low_details":     None,
        "high_details":    None,
    }

    cached = cache.get(f"TD:{pair}:1day:400", ttl_hours=24.0)
    if not isinstance(cached, dict) or not cached.get("values"):
        return _neutral

    raw = (cached["values"] or [])[:150]
    if len(raw) < 40:
        return _neutral

    # Reverse to chronological order (oldest first)
    arr = list(reversed(raw))

    closes: list = []
    highs:  list = []
    lows:   list = []
    for v in arr:
        try:
            closes.append(float(v["close"]))
            highs.append(float(v["high"]))
            lows.append(float(v["low"]))
        except (KeyError, TypeError, ValueError):
            pass

    n = len(closes)
    if n < 40:
        return _neutral

    # ── RSI-14 (Wilder's smoothing) ──────────────────────────────────────────
    RSI_PERIOD = 14
    diffs  = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains  = [max(d, 0.0) for d in diffs]
    losses = [max(-d, 0.0) for d in diffs]

    avg_gain = sum(gains[:RSI_PERIOD]) / RSI_PERIOD
    avg_loss = sum(losses[:RSI_PERIOD]) / RSI_PERIOD

    rsi_vals = [float('nan')] * n

    def _rsi_from_avgs(ag, al):
        if al > 0:
            return 100.0 - 100.0 / (1.0 + ag / al)
        return 100.0 if ag > 0 else 50.0

    rsi_vals[RSI_PERIOD] = _rsi_from_avgs(avg_gain, avg_loss)

    for i in range(RSI_PERIOD + 1, n):
        g = gains[i - 1]
        l = losses[i - 1]
        avg_gain = (avg_gain * (RSI_PERIOD - 1) + g) / RSI_PERIOD
        avg_loss = (avg_loss * (RSI_PERIOD - 1) + l) / RSI_PERIOD
        rsi_vals[i] = _rsi_from_avgs(avg_gain, avg_loss)

    # ── 3-bar pivot swing detection ───────────────────────────────────────────
    SWING = 3
    swing_highs: list = []   # (high_price, rsi)
    swing_lows:  list = []   # (low_price,  rsi)

    for i in range(SWING, n - SWING):
        rv = rsi_vals[i]
        if rv != rv:          # NaN: RSI not yet warmed up
            continue
        h = highs[i]
        lo = lows[i]
        if (all(h  > highs[i - k] for k in range(1, SWING + 1)) and
                all(h  > highs[i + k] for k in range(1, SWING + 1))):
            swing_highs.append((h, rv))
        if (all(lo < lows[i - k]  for k in range(1, SWING + 1)) and
                all(lo < lows[i + k]  for k in range(1, SWING + 1))):
            swing_lows.append((lo, rv))

    # ── Low-based divergence ──────────────────────────────────────────────────
    low_div     = "NONE"
    low_details = None
    if len(swing_lows) >= 2:
        sl_prev = swing_lows[-2]    # older swing low
        sl_curr = swing_lows[-1]    # more recent swing low
        p_ll = sl_curr[0] < sl_prev[0]   # price lower low
        r_hl = sl_curr[1] > sl_prev[1]   # RSI higher low
        p_hl = sl_curr[0] > sl_prev[0]   # price higher low
        r_ll = sl_curr[1] < sl_prev[1]   # RSI lower low
        if p_ll and r_hl:
            low_div = "BULLISH"
            low_details = {
                "price_level": sl_curr[0], "rsi_level": round(sl_curr[1], 1),
                "prev_price":  sl_prev[0], "prev_rsi":  round(sl_prev[1], 1),
            }
        elif p_hl and r_ll:
            low_div = "HIDDEN_BULLISH"
            low_details = {
                "price_level": sl_curr[0], "rsi_level": round(sl_curr[1], 1),
                "prev_price":  sl_prev[0], "prev_rsi":  round(sl_prev[1], 1),
            }

    # ── High-based divergence ─────────────────────────────────────────────────
    high_div     = "NONE"
    high_details = None
    if len(swing_highs) >= 2:
        sh_prev = swing_highs[-2]
        sh_curr = swing_highs[-1]
        p_hh = sh_curr[0] > sh_prev[0]   # price higher high
        r_lh = sh_curr[1] < sh_prev[1]   # RSI lower high
        p_lh = sh_curr[0] < sh_prev[0]   # price lower high
        r_hh = sh_curr[1] > sh_prev[1]   # RSI higher high
        if p_hh and r_lh:
            high_div = "BEARISH"
            high_details = {
                "price_level": sh_curr[0], "rsi_level": round(sh_curr[1], 1),
                "prev_price":  sh_prev[0], "prev_rsi":  round(sh_prev[1], 1),
            }
        elif p_lh and r_hh:
            high_div = "HIDDEN_BEARISH"
            high_details = {
                "price_level": sh_curr[0], "rsi_level": round(sh_curr[1], 1),
                "prev_price":  sh_prev[0], "prev_rsi":  round(sh_prev[1], 1),
            }

    return {
        "low_divergence":  low_div,
        "high_divergence": high_div,
        "low_details":     low_details,
        "high_details":    high_details,
    }
