"""Smart daily pair selection.

Scores the complete Twelve Data forex universe on four axes:
  1. 24-hour absolute price movement (%) — pairs in motion offer setups.
  2. 5-day directional momentum — candles moving in the dominant direction.
  3. Upcoming economic-event boost — high/medium impact events next 48h.
  4. Trading-session alignment — is the pair's primary session active now?

Flow:
  a) Fetch complete pair universe from Twelve Data /forex_pairs (cached 12h).
  b) Pre-score ALL pairs using session alignment + event boost + liquidity tier.
  c) Fetch daily price snapshots for the top PRICE_FETCH_LIMIT pre-scored pairs.
  d) Apply full composite score; return top_n for deep analysis plus full ranked list.
"""

import time
from datetime import datetime, timedelta

import requests

import config
from src import cache

# Fallback universe when Twelve Data /forex_pairs is unavailable.
UNIVERSE = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "USD/CAD", "AUD/USD", "NZD/USD",
    "EUR/JPY", "GBP/JPY", "AUD/JPY", "CAD/JPY", "CHF/JPY",
    "EUR/GBP", "EUR/AUD", "EUR/CAD", "EUR/CHF",
    "GBP/AUD", "GBP/CAD", "GBP/CHF",
    "AUD/CAD", "AUD/NZD",
]

_SESSION_UTC = {
    "EUR": (7, 16), "GBP": (7, 16), "CHF": (7, 16),
    "SEK": (7, 16), "NOK": (7, 16), "DKK": (7, 16),
    "PLN": (7, 16), "HUF": (7, 16), "CZK": (7, 16),
    "JPY": (0, 9), "CNH": (1, 9), "HKD": (1, 9),
    "SGD": (1, 9), "KRW": (0, 9), "THB": (1, 9),
    "USD": (12, 21), "CAD": (12, 21), "MXN": (12, 21),
    "AUD": (21, 7), "NZD": (21, 7),
    "ZAR": (6, 15), "TRY": (6, 16),
}

# Tier scores use a deliberately wide range so the tier weight dominates the
# composite and guarantees majors always outrank exotics regardless of
# short-term volatility.  An exotic pair would need ~7 % more daily movement
# than a major to overcome the gap — essentially impossible in normal markets.
_TIER_SCORES = {
    # G7 majors — always the first picks
    "EUR/USD": 8.0, "GBP/USD": 8.0, "USD/JPY": 8.0,
    "AUD/USD": 7.5, "USD/CAD": 7.5, "NZD/USD": 7.0, "USD/CHF": 7.0,
    # Key crosses among the eight core currencies — second tier
    "EUR/GBP": 6.5, "EUR/JPY": 6.5, "GBP/JPY": 6.5,
    "AUD/JPY": 6.0, "EUR/AUD": 5.5, "EUR/CAD": 5.5,
    "GBP/AUD": 5.0, "GBP/CAD": 5.0, "CAD/JPY": 5.0, "CHF/JPY": 5.0,
    "EUR/CHF": 5.0, "AUD/CAD": 4.5, "AUD/NZD": 4.5,
    "AUD/CHF": 4.0, "GBP/CHF": 4.0, "NZD/JPY": 4.0,
    "NZD/CAD": 4.0, "NZD/CHF": 4.0, "GBP/NZD": 4.0,
}
_CORE_CURRENCIES = {"EUR", "GBP", "USD", "JPY", "CHF", "AUD", "NZD", "CAD"}

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

_PRICE_FETCH_LIMIT = 75


def _fetch_universe() -> list:
    """Fetch the complete forex pair list from Twelve Data. Cached for 12 hours."""
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

    pairs = []
    for item in raw:
        sym = item.get("symbol", "")
        if "/" in sym and len(sym) == 7:
            pairs.append(sym.upper())

    if not pairs:
        return list(UNIVERSE)

    cache.set(cache_key, pairs)
    return pairs


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
    """Return liquidity tier score for this pair."""
    if pair in _TIER_SCORES:
        return _TIER_SCORES[pair]
    if base in _CORE_CURRENCIES and quote in _CORE_CURRENCIES:
        return 1.5
    return 0.8


def _event_boost(base: str, quote: str, events: list) -> float:
    """Sum impact weights for events affecting either currency; capped at 4.0."""
    total = sum(
        _IMPACT[ev["importance"]]
        for ev in events
        if ev["currency"] in (base, quote)
    )
    return min(total, 4.0)


def _snapshot_scores(snapshot: dict):
    """Return (change_24h_abs_pct, directional_days) from a closes snapshot."""
    closes = snapshot["closes"]
    change_abs = abs(closes[0] - closes[1]) / closes[1] * 100 if closes[1] else 0.0
    if len(closes) >= 6:
        moves = [closes[i] - closes[i + 1] for i in range(5)]
        ups = sum(1 for m in moves if m > 0)
        downs = sum(1 for m in moves if m < 0)
        directional = float(max(ups, downs))
    else:
        directional = 3.0
    return change_abs, directional


def _fetch_daily_snapshot(pair: str):
    """Return the last 6 daily closes for pair. Cached under global TTL."""
    cache_key = f"SEL:daily:{pair}"
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
                "symbol": symbol,
                "interval": "1day",
                "outputsize": 6,
                "format": "JSON",
                "apikey": config.TWELVE_DATA_KEY,
            },
            timeout=12,
        )
        data = r.json()
    except Exception:
        return None

    if data.get("status") == "error" or "values" not in data:
        return None

    closes = []
    for v in data["values"]:
        try:
            closes.append(float(v["close"]))
        except (KeyError, ValueError):
            pass

    if len(closes) < 2:
        return None

    result = {"closes": closes}
    cache.set(cache_key, result)
    return result


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
                "end_date": end.strftime("%Y-%m-%d"),
                "apikey": config.TWELVE_DATA_KEY,
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
        if len(cur_raw) == 3:
            currency = cur_raw.upper()
        else:
            currency = _COUNTRY_CURRENCY.get(cur_raw.lower(), "").upper()

        if not currency:
            continue

        events.append({
            "currency": currency,
            "importance": importance,
            "event": ev.get("event", ""),
            "datetime": ev.get("datetime", ""),
        })

    cache.set(cache_key, events)
    return events


def select_pairs(top_n: int = 15, price_fetch_limit: int = _PRICE_FETCH_LIMIT,
                 log=print) -> dict:
    """Score the full Twelve Data forex universe and return the top_n pairs.

    Returns a dict:
        selected        -- list of top_n pair strings for deep analysis
        ranked          -- list of (pair, score_meta) tuples covering all pairs
                           that received a price-data fetch, sorted best-first
        universe_size   -- total pairs in the Twelve Data universe
        prescreened     -- number of pairs that received a price-data fetch

    Rate-limit pacing: pauses 62s after every 7 uncached Twelve Data calls.
    Falls back to config.WATCHLIST when no price data is available.
    """
    all_pairs = _fetch_universe()
    universe_size = len(all_pairs)
    log(f"Universe: {universe_size} forex pairs available on Twelve Data")

    events = _fetch_calendar()
    if events:
        log(f"  Calendar: {len(events)} medium/high-impact events in next 48h")
    else:
        log("  Calendar: unavailable — scoring on movement + session only")

    utc_hour = datetime.utcnow().hour
    prescore_list = []
    for pair in all_pairs:
        parts = pair.split("/")
        if len(parts) != 2:
            continue
        base, quote = parts[0].upper(), parts[1].upper()
        ev = _event_boost(base, quote, events)
        sess = _session_score(base, quote, utc_hour)
        tier = _tier_score(pair, base, quote)
        pre = ev * 1.5 + sess * 1.2 + tier * 1.0
        prescore_list.append((pair, base, quote, pre))

    prescore_list.sort(key=lambda x: x[3], reverse=True)
    candidates = prescore_list[:price_fetch_limit]
    log(f"  Pre-screened top {len(candidates)} pairs by session+events+tier for price fetch")

    api_calls = 0
    pair_scores = {}

    for pair, base, quote, _pre in candidates:
        cache_key = f"SEL:daily:{pair}"
        is_miss = cache.get(cache_key) is None and bool(config.TWELVE_DATA_KEY)
        if is_miss:
            if api_calls > 0 and api_calls % 7 == 0:
                log("  (rate-limit pause 62s ...)")
                time.sleep(62)
            api_calls += 1

        snapshot = _fetch_daily_snapshot(pair)

        if snapshot:
            change_abs, directional = _snapshot_scores(snapshot)
        else:
            change_abs, directional = 0.0, 3.0

        ev_boost = _event_boost(base, quote, events)
        sess = _session_score(base, quote, utc_hour)
        tier = _tier_score(pair, base, quote)

        composite = (
            change_abs * 2.2
            + directional * 0.5
            + ev_boost * 1.6
            + sess * 0.8
            + tier * 0.4
        )
        pair_scores[pair] = {
            "score": round(composite, 3),
            "change_24h": round(change_abs, 4),
            "momentum": int(directional),
            "events": round(ev_boost, 2),
            "session": round(sess, 2),
            "tier": round(tier, 2),
            "base": base,
            "quote": quote,
        }

    ranked = sorted(pair_scores.items(), key=lambda x: x[1]["score"], reverse=True)

    log(f"  Top {min(top_n, len(ranked))} selected (score = movement + momentum + events + session + tier):")
    for pair, meta in ranked[:top_n]:
        event_flag = f"  ** {meta['events']:.1f} evt" if meta["events"] > 0 else ""
        sess_flag = f"  sess={meta['session']:.1f}" if meta["session"] > 0 else ""
        log(
            f"    {pair:10s}  score={meta['score']:5.2f}"
            f"  24h={meta['change_24h']:.3f}%"
            f"  mom={meta['momentum']}/5"
            f"{event_flag}{sess_flag}"
        )

    selected = [pair for pair, _ in ranked[:top_n]]

    if not selected:
        log("  WARNING: no price data — falling back to config.WATCHLIST.")
        return {
            "selected": list(config.WATCHLIST),
            "ranked": ranked,
            "universe_size": universe_size,
            "prescreened": len(candidates),
        }

    return {
        "selected": selected,
        "ranked": ranked,
        "universe_size": universe_size,
        "prescreened": len(candidates),
    }
