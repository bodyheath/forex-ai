"""Smart daily pair selection — 8-factor merit scoring.

Scores the complete Twelve Data forex universe on eight axes before deciding
which 15 pairs go to deep analysis:

  1. Momentum Quality     — magnitude, directional consistency, ATR-relative size.
  2. Technical Setup      — RSI at extremes, price near key support/resistance.
  3. Fundamental Divergence — interest-rate differential between the two currencies.
  4. Trend Clarity        — how consistently directional recent candles have been.
  5. Volatility Sweet Spot — current ATR relative to recent average (not too low / high).
  6. Session Timing       — pairs entering their peak session in the next 1-2 hours.
  7. News Catalyst Window — high-impact events 6-24 h away (not too soon, not too far).
  8. System Performance   — historical win rate for this pair in our own trade history.

Each factor is scored 0 to its maximum; together they sum to 100.
No pair is guaranteed a spot — the 15 highest scorers win each day.

Eligible universe (expanded from G8 to G10 + SGD/HKD):
  G8  majors : EUR GBP USD JPY CHF AUD NZD CAD
  G10 add    : NOK SEK
  Liquid Asia: SGD HKD
  Blacklisted: TRY ZAR MXN BRL IDR INR RUB THB PHP CZK HUF PLN RON
               (known illiquid / spread-too-wide currencies)

Flow:
  a) Fetch complete pair universe from Twelve Data /forex_pairs (cached 12h).
  b) Fetch economic calendar (cached).
  c) Fetch policy rates for all core currencies from FRED (cached 24h).
  d) Load per-pair win rates from trades.csv.
  e) Pre-score ALL liquid pairs on events + session + rate divergence + tier.
  f) Fetch OHLCV snapshots for the top PRICE_FETCH_LIMIT pre-scored pairs.
  g) Compute full 8-factor score; log the breakdown; return top 15.
"""

import time
import numpy as np
from datetime import datetime, timedelta

import requests

import config
from src import cache

# Fallback universe when Twelve Data /forex_pairs is unavailable.
UNIVERSE = [
    # G8 majors
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "USD/CAD", "AUD/USD", "NZD/USD",
    # G8 crosses
    "EUR/JPY", "GBP/JPY", "AUD/JPY", "CAD/JPY", "CHF/JPY",
    "EUR/GBP", "EUR/AUD", "EUR/CAD", "EUR/CHF",
    "GBP/AUD", "GBP/CAD", "GBP/CHF",
    "AUD/CAD", "AUD/NZD",
    # G10 Scandinavian (NOK, SEK)
    "EUR/NOK", "USD/NOK", "GBP/NOK",
    "EUR/SEK", "USD/SEK", "GBP/SEK",
    # Liquid Asian (SGD, HKD)
    "USD/SGD", "EUR/SGD", "GBP/SGD", "SGD/JPY",
    "USD/HKD", "EUR/HKD",
]

_SESSION_UTC = {
    "EUR": (7, 16), "GBP": (7, 16), "CHF": (7, 16),
    "SEK": (7, 16), "NOK": (7, 16), "DKK": (7, 16),
    "PLN": (7, 16), "HUF": (7, 16), "CZK": (7, 16),
    "JPY": (0, 9),  "CNH": (1, 9),  "HKD": (1, 9),
    "SGD": (1, 9),  "KRW": (0, 9),  "THB": (1, 9),
    "USD": (13, 22), "CAD": (13, 22), "MXN": (13, 22),
    "AUD": (21, 7),  "NZD": (21, 7),
    "ZAR": (6, 15),  "TRY": (6, 16),
}

# Tier scores reflect tradeable liquidity rank (G7 majors highest, unlisted liquid = 1.5).
# With a small coefficient (0.3) in the pre-score, tier is a modest tiebreaker.
# All pairs scoring 0.1 (illiquid or unknown currency) are excluded before scoring.
_TIER_SCORES = {
    # G7 majors — highest liquidity, not a guaranteed selection
    "EUR/USD": 8.0, "GBP/USD": 8.0, "USD/JPY": 8.0,
    "AUD/USD": 7.5, "USD/CAD": 7.5, "NZD/USD": 7.0, "USD/CHF": 7.0,
    # Key crosses among the G8 currencies
    "EUR/GBP": 6.5, "EUR/JPY": 6.5, "GBP/JPY": 6.5,
    "AUD/JPY": 6.0, "EUR/AUD": 5.5, "EUR/CAD": 5.5,
    "GBP/AUD": 5.0, "GBP/CAD": 5.0, "CAD/JPY": 5.0, "CHF/JPY": 5.0,
    "EUR/CHF": 5.0, "AUD/CAD": 4.5, "AUD/NZD": 4.5,
    "AUD/CHF": 4.0, "GBP/CHF": 4.0, "NZD/JPY": 4.0,
    "NZD/CAD": 4.0, "NZD/CHF": 4.0, "GBP/NZD": 4.0,
    # G10 Scandinavian currencies (NOK, SEK)
    "USD/HKD": 4.5, "USD/SGD": 4.0,
    "EUR/NOK": 3.5, "USD/NOK": 3.5, "EUR/SEK": 3.5, "USD/SEK": 3.5,
    "EUR/SGD": 3.5, "SGD/JPY": 3.0,
    "GBP/NOK": 3.0, "GBP/SEK": 3.0, "GBP/SGD": 3.0,
    "EUR/HKD": 3.0, "GBP/HKD": 3.0,
    "AUD/SGD": 2.5, "AUD/NOK": 2.5, "AUD/SEK": 2.5,
    "NZD/NOK": 2.5, "NZD/SEK": 2.5,
    "CAD/NOK": 2.5, "CAD/SEK": 2.5,
    "NOK/JPY": 2.5, "SEK/JPY": 2.5, "NOK/SEK": 2.5,
    "CHF/NOK": 2.5, "CHF/SEK": 2.5,
    "AUD/HKD": 2.5, "HKD/JPY": 2.5, "SGD/HKD": 2.0,
}

# Expanded eligible universe: G8 + G10 Scandinavian + liquid Asian crosses.
# All combinations of these 12 currencies pass the liquidity filter.
_LIQUID_CURRENCIES = {
    "EUR", "GBP", "USD", "JPY", "CHF", "AUD", "NZD", "CAD",  # G8 majors
    "NOK", "SEK",                                               # G10 Scandinavian
    "SGD", "HKD",                                               # Liquid Asian
}

# Currencies with excessive spread / thin liquidity — any pair containing one
# of these is excluded before scoring regardless of the other currency.
_ILLIQUID_CURRENCIES = {
    "TRY", "ZAR", "MXN", "BRL", "IDR", "INR", "RUB", "THB",
    "PHP", "CZK", "HUF", "PLN", "RON",
}

# Approximate current central-bank policy rates (% p.a.).
# Used as the primary rate source when FRED is unreachable AND as a guaranteed
# floor so that F3 (fundamental divergence) always produces differentiated
# values — even if FRED is down for a whole day.
# Update these whenever a major central bank moves rates.
# Last updated: 2026-06-15 (USD 5.33, JPY 0.50 after Mar-2024 hike, etc.)
_FALLBACK_RATES: dict = {
    "USD": 5.33,   # Federal Reserve — effective fed funds
    "EUR": 3.65,   # ECB deposit facility (cut Jun 2025)
    "GBP": 4.50,   # Bank of England (cut to 4.50 Feb 2025)
    "JPY": 0.50,   # Bank of Japan (hiked Mar 2024, Jul 2024)
    "AUD": 4.10,   # RBA cash rate (cut Feb 2025)
    "NZD": 3.75,   # RBNZ OCR
    "CAD": 3.00,   # Bank of Canada
    "CHF": 0.25,   # Swiss National Bank
    "NOK": 4.50,   # Norges Bank
    "SEK": 2.50,   # Riksbank (cut Feb 2025)
    "SGD": 3.60,   # MAS 3-month SIBOR proxy
    "HKD": 5.33,   # HKMA base rate (pegged, mirrors Fed)
}

_IMPACT = {"high": 2.0, "medium": 0.7}

_COUNTRY_CURRENCY = {
    "united states": "USD", "us": "USD",
    "eurozone": "EUR", "euro area": "EUR", "european union": "EUR",
    "united kingdom": "GBP", "uk": "GBP",
    "japan": "JPY", "australia": "AUD", "canada": "CAD",
    "switzerland": "CHF", "new zealand": "NZD",
    "china": "CNH", "hong kong": "HKD", "singapore": "SGD",
    "south africa": "ZAR", "turkey": "TRY", "mexico": "MXN",
    "sweden": "SEK", "norway": "NOK", "denmark": "DKK",
    "poland": "PLN", "hungary": "HUF", "czech republic": "CZK",
}

_PRICE_FETCH_LIMIT = 30


# ── Universe, calendar, rates, performance ───────────────────────────────────

def _fetch_universe() -> list:
    """Fetch the complete forex pair list from Twelve Data. Cached 12h."""
    cache_key = "SEL:universe"
    cached = cache.get(cache_key, ttl_hours=12.0)
    if cached is not None:
        return cached

    if not config.TWELVE_DATA_KEY:
        return list(UNIVERSE)

    try:
        r = requests.get(
            "https://api.twelvedata.com/forex_pairs",
            params={"apikey": config.TWELVE_DATA_KEY},
            timeout=20,
        )
        data = r.json()
    except Exception:
        return list(UNIVERSE)

    raw = data.get("data", [])
    if not isinstance(raw, list) or not raw:
        return list(UNIVERSE)

    pairs = [
        item.get("symbol", "").upper()
        for item in raw
        if "/" in item.get("symbol", "") and len(item.get("symbol", "")) == 7
    ]
    if not pairs:
        return list(UNIVERSE)

    cache.set(cache_key, pairs)
    return pairs


def _fetch_calendar(hours_ahead: int = 48) -> list:
    """Fetch upcoming economic events from Twelve Data. Cached under global TTL."""
    cache_key = "SEL:calendar"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if not config.TWELVE_DATA_KEY:
        return []

    now = datetime.utcnow()
    end = now + timedelta(hours=hours_ahead)
    try:
        r = requests.get(
            "https://api.twelvedata.com/economic_calendar",
            params={
                "start_date": now.strftime("%Y-%m-%d"),
                "end_date":   end.strftime("%Y-%m-%d"),
                "apikey":     config.TWELVE_DATA_KEY,
            },
            timeout=15,
        )
        data = r.json()
    except Exception:
        return []

    raw = data.get("result", data)
    if isinstance(raw, dict):
        raw = raw.get("events", [])
    if not isinstance(raw, list):
        return []

    events = []
    for ev in raw:
        imp_raw = ev.get("importance", "")
        if isinstance(imp_raw, int):
            importance = {3: "high", 2: "medium", 1: "low"}.get(imp_raw, "")
        else:
            importance = str(imp_raw).lower().strip()
        if importance not in _IMPACT:
            continue

        cur_raw = (ev.get("currency") or ev.get("country") or "").strip()
        currency = (
            cur_raw.upper() if len(cur_raw) == 3
            else _COUNTRY_CURRENCY.get(cur_raw.lower(), "").upper()
        )
        if not currency:
            continue

        events.append({
            "currency":   currency,
            "importance": importance,
            "event":      ev.get("event", ""),
            "datetime":   ev.get("datetime", ""),
        })

    cache.set(cache_key, events)
    return events


_MAJOR_EVENT_KEYWORDS = [
    "nfp", "non-farm", "nonfarm", "payroll",
    "fomc", "federal reserve", "fed rate",
    "cpi", "inflation",
    "gdp",
    "boe", "bank of england",
    "ecb", "european central bank",
    "rba", "reserve bank of australia",
    "rbnz", "reserve bank of new zealand",
    "boj", "bank of japan",
]


def count_weekly_high_impact_events() -> tuple:
    """Count high-impact economic events for the next 7 days.

    Returns (count: int, notable_names: list[str]) where notable_names
    contains up to 3 well-known event labels (NFP, FOMC, CPI, etc.).
    Cached 6h — cheap to call from the 6am message builder.
    """
    cache_key = "SEL:calendar_7d"
    cached = cache.get(cache_key, ttl_hours=6.0)
    if cached is not None:
        return cached

    if not config.TWELVE_DATA_KEY:
        return (0, [])

    now = datetime.utcnow()
    end = now + timedelta(days=7)
    try:
        r = requests.get(
            "https://api.twelvedata.com/economic_calendar",
            params={
                "start_date": now.strftime("%Y-%m-%d"),
                "end_date":   end.strftime("%Y-%m-%d"),
                "apikey":     config.TWELVE_DATA_KEY,
            },
            timeout=15,
        )
        data = r.json()
    except Exception:
        result = (0, [])
        cache.set(cache_key, result)
        return result

    raw = data.get("result", data)
    if isinstance(raw, dict):
        raw = raw.get("events", [])
    if not isinstance(raw, list):
        result = (0, [])
        cache.set(cache_key, result)
        return result

    count = 0
    notable = []
    seen_notable = set()
    for ev in raw:
        imp_raw = ev.get("importance", "")
        if isinstance(imp_raw, int):
            importance = {3: "high", 2: "medium", 1: "low"}.get(imp_raw, "")
        else:
            importance = str(imp_raw).lower().strip()
        if importance != "high":
            continue
        count += 1
        name = ev.get("event", "")
        name_lower = name.lower()
        for kw in _MAJOR_EVENT_KEYWORDS:
            if kw in name_lower and name not in seen_notable:
                label = name[:40].strip()
                notable.append(label)
                seen_notable.add(name)
                break

    result = (count, notable[:3])
    cache.set(cache_key, result)
    return result


def _fetch_all_rates() -> dict:
    """Fetch central-bank policy rates for all liquid currencies.

    Strategy (in order):
      1. 24h cache — avoid hammering FRED on every run.
      2. FRED live fetch — real current rates for currencies in config.CURRENCIES.
      3. _FALLBACK_RATES — fills any gap left by FRED (missing currency, stale
         series, timeout, missing FRED_API_KEY).  This guarantees F3 always
         produces differentiated values for G10+SGD/HKD pairs even when FRED
         is completely unavailable.

    The result is ALWAYS cached (even if entirely from fallbacks), so a FRED
    outage doesn't cause repeated failed requests on the same day.

    Returns {currency: rate_pct}, e.g. {"USD": 5.33, "JPY": 0.50, "NOK": 4.50}.
    """
    cache_key = "SEL:policy_rates"
    cached = cache.get(cache_key, ttl_hours=24.0)
    if cached is not None:
        return cached

    rates: dict = {}

    if config.FRED_API_KEY:
        for ccy, ccy_data in config.CURRENCIES.items():
            series_id = ccy_data.get("rate_fred")
            if not series_id:
                continue
            try:
                r = requests.get(
                    "https://api.stlouisfed.org/fred/series/observations",
                    params={
                        "series_id":  series_id,
                        "api_key":    config.FRED_API_KEY,
                        "file_type":  "json",
                        "limit":      5,
                        "sort_order": "desc",
                    },
                    timeout=10,
                )
                for ob in r.json().get("observations", []):
                    val_str = ob.get("value", ".")
                    if val_str not in (".", "", "nd"):
                        rates[ccy] = float(val_str)
                        break
            except Exception:
                pass

    # Fill any gaps (FRED unavailable, currency not in config.CURRENCIES, etc.)
    # with _FALLBACK_RATES so F3 always differentiates between pairs.
    fred_count = len(rates)
    for ccy, fallback in _FALLBACK_RATES.items():
        if ccy not in rates:
            rates[ccy] = fallback

    # Always cache — prevents FRED hammering when it's down.
    cache.set(cache_key, rates)

    return rates


def _load_pair_performance() -> dict:
    """Load per-pair win rates from historical trades.csv.

    Requires >= 3 decisive (WIN/LOSS) trades per pair before reporting a rate.
    Returns {pair: win_rate_0_to_1}, e.g. {"EUR/USD": 0.67}.
    """
    try:
        from src import tracker
        rows = tracker.load()
        per_pair: dict = {}
        for row in rows:
            pair   = (row.get("pair") or "").upper().replace("-", "/")
            status = (row.get("status") or "").upper()
            if status not in ("WIN", "LOSS"):
                continue
            if pair not in per_pair:
                per_pair[pair] = {"wins": 0, "total": 0}
            per_pair[pair]["total"] += 1
            if status == "WIN":
                per_pair[pair]["wins"] += 1
        return {
            pair: d["wins"] / d["total"]
            for pair, d in per_pair.items()
            if d["total"] >= 3
        }
    except Exception:
        return {}


# ── OHLCV snapshot ────────────────────────────────────────────────────────────

def _fetch_ohlcv_snapshot(pair: str):
    """Fetch 20 daily OHLCV candles for a pair. Cached under global TTL.

    Returns {"closes": [...], "highs": [...], "lows": [...], "opens": [...]}
    with values newest-first, or None on failure.
    """
    cache_key = f"SEL:snap:{pair}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if not config.TWELVE_DATA_KEY:
        return None

    symbol = pair.replace("/", "")
    try:
        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol":     symbol,
                "interval":   "1day",
                "outputsize": 20,
                "format":     "JSON",
                "apikey":     config.TWELVE_DATA_KEY,
            },
            timeout=12,
        )
        data = r.json()
    except Exception:
        return None

    if data.get("status") == "error" or "values" not in data:
        return None

    closes, highs, lows, opens = [], [], [], []
    for v in data["values"]:
        try:
            closes.append(float(v["close"]))
            highs.append(float(v["high"]))
            lows.append(float(v["low"]))
            opens.append(float(v["open"]))
        except (KeyError, ValueError):
            pass

    if len(closes) < 2:
        return None

    result = {"closes": closes, "highs": highs, "lows": lows, "opens": opens}
    cache.set(cache_key, result)
    return result


# ── Lightweight helpers (pre-score, no API) ───────────────────────────────────

def _session_score(base: str, quote: str, utc_hour: int) -> float:
    """Return 0-3: how well this pair fits the current trading session."""
    score = 0.0
    for ccy in (base, quote):
        window = _SESSION_UTC.get(ccy)
        if window is None:
            continue
        start, end = window
        if start < end:
            active = start <= utc_hour < end
        else:
            active = utc_hour >= start or utc_hour < end
        until_start = (start - utc_hour) % 24
        if active:
            score += 1.5
        elif until_start <= 2:
            score += 0.5
    return min(score, 3.0)


def _tier_score(pair: str, base: str, quote: str) -> float:
    """Return liquidity-tier score (0.1–8.0).

    Used with coefficient 0.3 in the pre-score so tier acts as a modest
    tiebreaker.  Pairs scoring 0.1 are excluded before any scoring runs.

    Rules applied in order:
      1. Pre-set tier entry → use it (all entries ≥ 2.0, always pass).
      2. Either currency in _ILLIQUID_CURRENCIES → 0.1 (filtered out).
      3. Both currencies in _LIQUID_CURRENCIES (G10 + SGD/HKD) → 1.5 (tradeable).
      4. Otherwise → 0.1 (unknown / outside eligible universe).
    """
    if pair in _TIER_SCORES:
        return _TIER_SCORES[pair]
    if base in _ILLIQUID_CURRENCIES or quote in _ILLIQUID_CURRENCIES:
        return 0.1
    if base in _LIQUID_CURRENCIES and quote in _LIQUID_CURRENCIES:
        return 1.5   # valid cross not yet in the tier table
    return 0.1       # outside eligible universe


def _event_boost(base: str, quote: str, events: list) -> float:
    """Sum impact weights for events affecting either currency; capped at 4.0."""
    total = sum(
        _IMPACT[ev["importance"]]
        for ev in events
        if ev["currency"] in (base, quote)
    )
    return min(total, 4.0)


# ── 8-factor composite scorer ─────────────────────────────────────────────────

def _compute_rich_score(
    pair: str,
    base: str,
    quote: str,
    snapshot,
    rates: dict,
    events: list,
    utc_hour: int,
    perf_map: dict,
) -> tuple:
    """Compute 8-factor composite pre-selection score out of 100.

    Returns (total_score, breakdown_dict).
    Max per factor: mom=15, tech=20, fundiv=15, trend=10, vol=10, sess=10, news=10, sys=10.
    """
    closes = snapshot.get("closes", []) if snapshot else []
    highs  = snapshot.get("highs",  []) if snapshot else []
    lows   = snapshot.get("lows",   []) if snapshot else []
    n      = len(closes)
    bd: dict = {}

    # ── 1. Momentum Quality (max 15) ────────────────────────────────────────
    if n >= 2:
        change_24h = abs(closes[0] - closes[1]) / closes[1] * 100 if closes[1] else 0.0

        # A: raw magnitude — 1 % move earns full 5 pts
        f1_mag = min(change_24h / 0.2, 5.0)

        # B: directional consistency over last 5 days (older data → more pts)
        if n >= 6:
            moves = [closes[i] - closes[i + 1] for i in range(5)]
            dominant = max(
                sum(1 for m in moves if m > 0),
                sum(1 for m in moves if m <= 0),
            )
            f1_dir = (dominant / 5) * 5.0
        else:
            f1_dir = 2.5

        # C: move relative to recent ATR — confirms move isn't just noise
        if highs and lows and len(highs) >= 3:
            atr_periods = min(5, len(highs))
            recent_atr  = sum(highs[i] - lows[i] for i in range(atr_periods)) / atr_periods
            move_abs    = abs(closes[0] - closes[1])
            f1_range    = min((move_abs / recent_atr) * 5.0, 5.0) if recent_atr > 0 else 2.5
        else:
            f1_range = 2.5

        f1 = f1_mag + f1_dir + f1_range
    else:
        f1 = 5.0   # neutral when no price data
    bd["1_momentum_quality"] = round(min(f1, 15.0), 2)

    # ── 2. Technical Setup Quality (max 20) ─────────────────────────────────
    if n >= 10:
        arr    = np.array(closes[:n][::-1], dtype=float)   # oldest first
        delta  = np.diff(arr)
        gains  = np.where(delta > 0, delta, 0.0)
        losses = np.where(delta < 0, -delta, 0.0)
        p      = min(14, len(delta))
        avg_g  = float(gains[-p:].mean())  if p > 0 else 0.0
        avg_l  = float(losses[-p:].mean()) if p > 0 else 0.0
        rsi    = 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 50.0

        # RSI at extremes = clear signal
        if rsi < 30 or rsi > 70:
            f2_rsi = 10.0
        elif rsi < 35 or rsi > 65:
            f2_rsi = 7.0
        elif rsi < 40 or rsi > 60:
            f2_rsi = 4.0
        else:
            f2_rsi = 1.0   # mid-range — no setup

        # Price proximity to 20-day high/low (within recent OHLCV window)
        if highs and lows and len(highs) >= 10:
            h20 = max(highs[:10])
            l20 = min(lows[:10])
            rng = h20 - l20
            if rng > 0:
                dist_pct = min(
                    abs(closes[0] - h20),
                    abs(closes[0] - l20),
                ) / rng
                f2_level = (1.0 - dist_pct) * 10.0  # 10 = at the extreme, 0 = dead centre
            else:
                f2_level = 5.0
        else:
            f2_level = 5.0

        f2 = f2_rsi + f2_level
    else:
        f2 = 7.0   # neutral
    bd["2_technical_setup"] = round(min(f2, 20.0), 2)

    # ── 3. Fundamental Divergence (max 15) ───────────────────────────────────
    base_rate  = rates.get(base)
    quote_rate = rates.get(quote)
    if base_rate is not None and quote_rate is not None:
        diff = abs(base_rate - quote_rate)
        if diff >= 3.0:
            f3 = 15.0
        elif diff >= 1.5:
            f3 = 12.0
        elif diff >= 0.5:
            f3 = 8.0
        elif diff >= 0.1:
            f3 = 4.0
        else:
            f3 = 1.0   # essentially no divergence
    else:
        f3 = 5.0   # neutral when FRED unavailable
    bd["3_fundamental_divergence"] = round(f3, 2)

    # ── 4. Trend Clarity (max 10) ────────────────────────────────────────────
    if n >= 5:
        moves = [closes[i] - closes[i + 1] for i in range(min(5, n - 1))]
        if len(moves) >= 2:
            reversals = sum(
                1 for i in range(1, len(moves))
                if (moves[i] > 0) != (moves[i - 1] > 0)
            )
            f4 = max(0.0, 10.0 - reversals * 3.0)
        else:
            f4 = 5.0
    else:
        f4 = 5.0
    bd["4_trend_clarity"] = round(f4, 2)

    # ── 5. Volatility Sweet Spot (max 10) ────────────────────────────────────
    if highs and lows and len(highs) >= 5:
        daily_ranges  = [highs[i] - lows[i] for i in range(len(highs))]
        avg_range     = sum(daily_ranges) / len(daily_ranges)
        current_range = daily_ranges[0]
        if avg_range > 0:
            ratio = current_range / avg_range
            # Sweet spot: 70 %–150 % of recent average daily range
            if 0.7 <= ratio <= 1.5:
                f5 = 10.0
            elif 0.5 <= ratio < 0.7 or 1.5 < ratio <= 2.0:
                f5 = 6.0
            else:
                f5 = 2.0   # too quiet or too volatile
        else:
            f5 = 5.0
    else:
        f5 = 5.0
    bd["5_volatility_sweetspot"] = round(f5, 2)

    # ── 6. Session Timing Bonus (max 10) ─────────────────────────────────────
    # Highest score when the pair's session is opening in the next 1-2 hours
    # — entering peak liquidity is the best time to set up a trade.
    f6 = 0.0
    for ccy in (base, quote):
        window = _SESSION_UTC.get(ccy)
        if window is None:
            continue
        start, end = window
        until_start = (start - utc_hour) % 24
        if start < end:
            in_session = start <= utc_hour < end
        else:
            in_session = utc_hour >= start or utc_hour < end

        if 1 <= until_start <= 2:
            f6 = max(f6, 10.0)   # prime window — session opens in 1-2h
        elif until_start < 1:
            f6 = max(f6, 8.0)    # session just opened or opening within the hour
        elif until_start <= 4:
            f6 = max(f6, 5.0)    # session in a few hours
        elif in_session:
            f6 = max(f6, 5.0)    # currently in session
    bd["6_session_timing"] = round(min(f6, 10.0), 2)

    # ── 7. News Catalyst Window (max 10) ─────────────────────────────────────
    # Sweet spot: high-impact event 6-24h away — near enough to be a catalyst,
    # far enough that we're not trading into the release itself.
    f7 = 0.0
    now_utc = datetime.utcnow()
    for ev in events:
        if ev.get("currency") not in (base, quote):
            continue
        ev_dt_str = ev.get("datetime", "")
        if not ev_dt_str:
            continue
        try:
            clean = ev_dt_str.replace("Z", "").replace("T", " ")
            ev_dt = datetime.strptime(clean[:19], "%Y-%m-%d %H:%M:%S")
            hours_away = (ev_dt - now_utc).total_seconds() / 3600
        except Exception:
            continue
        imp = ev.get("importance", "")
        if imp == "high":
            if 6 <= hours_away <= 24:
                f7 = max(f7, 10.0)    # sweet spot
            elif 24 < hours_away <= 48:
                f7 = max(f7, 6.0)
            elif 2 <= hours_away < 6:
                f7 = max(f7, 4.0)     # too soon — event risk
            elif 0 <= hours_away < 2:
                f7 = max(f7, 2.0)     # imminent — very risky
        elif imp == "medium":
            if 6 <= hours_away <= 24:
                f7 = max(f7, 5.0)
            elif 24 < hours_away <= 48:
                f7 = max(f7, 3.0)
    bd["7_news_catalyst"] = round(f7, 2)

    # ── 8. System Performance (max 10) ───────────────────────────────────────
    wr = perf_map.get(pair)
    if wr is None:
        f8 = 5.0    # neutral — insufficient history (< 3 trades)
    elif wr >= 0.70:
        f8 = 10.0
    elif wr >= 0.55:
        f8 = 7.0
    elif wr >= 0.45:
        f8 = 5.0
    elif wr >= 0.35:
        f8 = 2.0
    else:
        f8 = 0.0    # consistent loser — small penalty
    bd["8_system_performance"] = round(f8, 2)

    total = sum(bd.values())
    return round(total, 2), bd


# ── Main selection ────────────────────────────────────────────────────────────

def select_pairs(top_n: int = 15, price_fetch_limit: int = _PRICE_FETCH_LIMIT,
                 log=print) -> dict:
    """Score the full Twelve Data forex universe and return the top_n pairs.

    Selection is pure-merit: every liquid pair is scored on all 8 factors.
    No pair is guaranteed a slot — the 15 highest scorers that day win.

    Returns a dict:
        selected        -- list of top_n pair strings for deep analysis
        ranked          -- list of (pair, score_meta) tuples, sorted best-first
        universe_size   -- total pairs in the Twelve Data universe
        prescreened     -- number of liquid pairs that received a price-data fetch

    Rate-limit pacing: pauses 10s between uncached Twelve Data calls.
    Falls back to config.WATCHLIST when no price data is available.
    """
    all_pairs     = _fetch_universe()
    universe_size = len(all_pairs)
    log(f"Universe: {universe_size} forex pairs available on Twelve Data")

    events = _fetch_calendar()
    if events:
        log(f"  Calendar: {len(events)} medium/high-impact events in next 48h")
    else:
        log("  Calendar: unavailable — news catalyst factor set to neutral")

    # Fetch structural data once; both pre-score and full score use them.
    rates    = _fetch_all_rates()
    perf_map = _load_pair_performance()

    # Count how many currencies came from FRED vs fallback (FRED_API_KEY present
    # but FRED returned fewer currencies than _FALLBACK_RATES).
    fred_live = {c for c in rates if c in config.CURRENCIES and config.FRED_API_KEY}
    fallback_used = set(rates) - fred_live
    if rates:
        log(
            f"  Policy rates ({len(rates)} currencies — "
            f"{len(fred_live)} from FRED, {len(fallback_used)} from fallback): "
            + ", ".join(f"{c}={v:.2f}%" for c, v in sorted(rates.items()))
        )
    else:
        log("  Policy rates: unavailable — fundamental divergence set to neutral")

    if perf_map:
        log(f"  Trade history: {len(perf_map)} pair(s) with >= 3 decisive trades")
    else:
        log("  Trade history: none yet — system performance factor set to neutral (5 pts)")

    utc_hour = datetime.utcnow().hour

    # Pre-score ALL liquid pairs (no API calls) to prioritise which 20 get
    # price data fetched.  Includes rate divergence so high-divergence pairs
    # (e.g. USD/JPY when rates differ by 5 %) always earn a fetch slot.
    prescore_list = []
    exotic_count  = 0
    for pair in all_pairs:
        parts = pair.split("/")
        if len(parts) != 2:
            continue
        base, quote = parts[0].upper(), parts[1].upper()
        tier = _tier_score(pair, base, quote)
        if tier < 1.0:
            exotic_count += 1
            continue   # minimum-liquidity filter
        ev        = _event_boost(base, quote, events)
        sess      = _session_score(base, quote, utc_hour)
        rate_diff = abs(rates.get(base, 0.0) - rates.get(quote, 0.0))
        rate_bonus  = min(rate_diff / 5.0, 1.0) * 2.0   # 0-2 pts
        tier_weight = tier * 0.3                          # tiebreaker (not a dominant factor)
        pre = ev * 1.5 + sess * 1.2 + tier_weight + rate_bonus
        prescore_list.append((pair, base, quote, pre))

    prescore_list.sort(key=lambda x: x[3], reverse=True)
    candidates = prescore_list[:price_fetch_limit]
    log(
        f"  Universe expanded — {len(prescore_list)} liquid pair(s) eligible "
        f"({exotic_count} illiquid/unknown removed). "
        f"Scanning full eligible universe for best opportunities. "
        f"Fetching OHLCV for top {len(candidates)} by pre-score."
    )

    # ── Pre-score breakdown diagnostic ───────────────────────────────────────
    # Shows what's driving the pre-selection ranking BEFORE OHLCV data is used.
    # Rate column shows the raw rate differential; this is what F3 will score on.
    log(
        f"\n  Pre-score breakdown — top {len(candidates)} candidates:\n"
        f"  {'Pair':<10} {'Events':>7} {'Session':>8} {'RateDiff%':>10} "
        f"{'Tier':>5}  {'Pre':>6}"
    )
    for pair, base, quote, pre in candidates:
        _ev_d    = _event_boost(base, quote, events)
        _sess_d  = _session_score(base, quote, utc_hour)
        _rdiff_d = abs(rates.get(base, 0.0) - rates.get(quote, 0.0))
        _tier_d  = _tier_score(pair, base, quote)
        log(
            f"  {pair:<10} {_ev_d:>7.2f} {_sess_d:>8.2f} "
            f"{_rdiff_d:>9.2f}% {_tier_d:>5.1f}  {pre:>6.2f}"
        )
    log("")

    api_calls  = 0
    snap_ok:   list = []
    snap_fail: list = []
    pair_scores: dict = {}

    for pair, base, quote, _pre in candidates:
        cache_key = f"SEL:snap:{pair}"
        is_miss   = cache.get(cache_key) is None and bool(config.TWELVE_DATA_KEY)
        if is_miss:
            if api_calls > 0:
                time.sleep(10)
            api_calls += 1

        snapshot = _fetch_ohlcv_snapshot(pair)
        if snapshot:
            snap_ok.append(pair)
        else:
            snap_fail.append(pair)
        score, breakdown = _compute_rich_score(
            pair, base, quote, snapshot, rates, events, utc_hour, perf_map
        )

        # change_24h / momentum kept for any external code that reads metadata
        if snapshot and len(snapshot.get("closes", [])) >= 2:
            c = snapshot["closes"]
            chg  = abs(c[0] - c[1]) / c[1] * 100 if c[1] else 0.0
            mvs  = [c[i] - c[i + 1] for i in range(min(4, len(c) - 1))]
            mom  = int(max(
                sum(1 for m in mvs if m > 0),
                sum(1 for m in mvs if m <= 0),
            ))
        else:
            chg, mom = 0.0, 3

        pair_scores[pair] = {
            "score":      score,
            "breakdown":  breakdown,
            "change_24h": round(chg, 4),
            "momentum":   mom,
            "events":     round(_event_boost(base, quote, events), 2),
            "session":    round(_session_score(base, quote, utc_hour), 2),
            "tier":       round(_tier_score(pair, base, quote), 2),
            "base":       base,
            "quote":      quote,
        }

    ranked = sorted(pair_scores.items(), key=lambda x: x[1]["score"], reverse=True)

    # ── Detailed breakdown log ────────────────────────────────────────────────
    hdr = (
        f"  {'Rk':<3} {'Pair':<10} {'Total':>6}  "
        f"{'Momtm':>6} {'Tech':>5} {'FunDiv':>7} "
        f"{'Trend':>6} {'Vol':>4} {'Sess':>5} {'News':>5} {'Sys':>4}  "
        f"24h%   why_selected"
    )
    log(f"\n  Top {min(top_n, len(ranked))} pairs by 8-factor merit score (max 100):")
    log(hdr)
    log("  " + "-" * (len(hdr) - 2))
    for rank, (pair, meta) in enumerate(ranked[:top_n], 1):
        bd  = meta["breakdown"]
        chg = meta["change_24h"]
        # Build a one-word "why" tag based on the top-scoring factor
        factor_vals = {k: v for k, v in bd.items()}
        top_factor  = max(factor_vals, key=factor_vals.get)
        why_tags = {
            "1_momentum_quality":       f"+{chg:+.2f}% move",
            "2_technical_setup":        "RSI/S-R setup",
            "3_fundamental_divergence": "rate divergence",
            "4_trend_clarity":          "clean trend",
            "5_volatility_sweetspot":   "vol sweet-spot",
            "6_session_timing":         "session opening",
            "7_news_catalyst":          "news catalyst",
            "8_system_performance":     "historical edge",
        }
        why = why_tags.get(top_factor, "")
        log(
            f"  #{rank:<2} {pair:<10} {meta['score']:>6.1f}  "
            f"{bd.get('1_momentum_quality', 0):>6.1f} "
            f"{bd.get('2_technical_setup', 0):>5.1f} "
            f"{bd.get('3_fundamental_divergence', 0):>7.1f} "
            f"{bd.get('4_trend_clarity', 0):>6.1f} "
            f"{bd.get('5_volatility_sweetspot', 0):>4.1f} "
            f"{bd.get('6_session_timing', 0):>5.1f} "
            f"{bd.get('7_news_catalyst', 0):>5.1f} "
            f"{bd.get('8_system_performance', 0):>4.1f}  "
            f"{chg:+.3f}%  {why}"
        )

    selected = [pair for pair, _ in ranked[:top_n]]

    if not selected:
        log("  WARNING: no price data — falling back to config.WATCHLIST.")
        return {
            "selected":      list(config.WATCHLIST),
            "ranked":        ranked,
            "universe_size": universe_size,
            "prescreened":   len(candidates),
        }

    return {
        "selected":      selected,
        "ranked":        ranked,
        "universe_size": universe_size,
        "prescreened":   len(candidates),
    }
