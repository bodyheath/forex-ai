"""Smart daily pair selection.

Scores every pair in the extended liquid universe on three axes:
  1. 24-hour absolute price movement (%) — pairs already in motion offer setups.
  2. 5-day directional momentum — count of candles moving in the dominant direction.
  3. Upcoming economic-event boost — high/medium impact events in the next 48 h for
     either currency, fetched from the Twelve Data economic-calendar endpoint.

The top-N pairs by composite score are returned for full pipeline analysis.
Falls back to config.WATCHLIST when Twelve Data is unavailable.
"""

import time
from datetime import datetime, timedelta

import requests

import config
from src import cache

# All liquid pairs constructable from the 8 supported currencies.
UNIVERSE = [
    # Majors
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "USD/CAD", "AUD/USD", "NZD/USD",
    # Yen crosses
    "EUR/JPY", "GBP/JPY", "AUD/JPY", "CAD/JPY", "CHF/JPY",
    # EUR crosses
    "EUR/GBP", "EUR/AUD", "EUR/CAD", "EUR/CHF",
    # GBP crosses
    "GBP/AUD", "GBP/CAD", "GBP/CHF",
    # Antipodean / commodity crosses
    "AUD/CAD", "AUD/NZD",
]

# Twelve Data calendar importance strings → score weight.
_IMPACT = {"high": 2.0, "medium": 0.7}

# Mapping for calendars that return a country name instead of a currency code.
_COUNTRY_CURRENCY = {
    "united states": "USD", "us": "USD",
    "eurozone": "EUR", "euro area": "EUR", "european union": "EUR",
    "united kingdom": "GBP", "uk": "GBP",
    "japan": "JPY",
    "australia": "AUD",
    "canada": "CAD",
    "switzerland": "CHF",
    "new zealand": "NZD",
}


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def _fetch_daily_snapshot(pair):
    """Return the last 6 daily closes for pair as {'closes': [newest, ...]}.
    Returns None on any failure. Results are cached under the global TTL."""
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


def _fetch_calendar(hours_ahead=48):
    """Fetch upcoming economic events from Twelve Data. Returns [] on failure.
    Normalises importance to 'high'/'medium' and currency to ISO-3 code."""
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

    # Response shape: {"result": {"events": [...]}} or {"result": [...]}
    raw = data.get("result", data)
    if isinstance(raw, dict):
        raw = raw.get("events", [])
    if not isinstance(raw, list):
        return []

    events = []
    for ev in raw:
        # Normalise importance (string or int).
        imp_raw = ev.get("importance", "")
        if isinstance(imp_raw, int):
            importance = {3: "high", 2: "medium", 1: "low"}.get(imp_raw, "")
        else:
            importance = str(imp_raw).lower().strip()

        if importance not in _IMPACT:
            continue

        # Normalise currency (ISO-3 code or country name).
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


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _snapshot_scores(snapshot):
    """Return (change_24h_abs_pct, directional_days) from a closes snapshot."""
    closes = snapshot["closes"]  # index 0 = most recent
    change_abs = abs(closes[0] - closes[1]) / closes[1] * 100 if closes[1] else 0.0

    if len(closes) >= 6:
        moves = [closes[i] - closes[i + 1] for i in range(5)]
        ups = sum(1 for m in moves if m > 0)
        downs = sum(1 for m in moves if m < 0)
        directional = float(max(ups, downs))  # 3–5; higher = more directional
    else:
        directional = 3.0  # neutral default

    return change_abs, directional


def _event_boost(base, quote, events):
    """Sum impact weights for events affecting either currency; capped at 4.0."""
    total = sum(
        _IMPACT[ev["importance"]]
        for ev in events
        if ev["currency"] in (base, quote)
    )
    return min(total, 4.0)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def select_pairs(top_n=10, log=print):
    """Score all pairs in UNIVERSE and return the top_n list.

    Paces Twelve Data calls to stay within the 8-per-minute free-tier limit
    (7 uncached calls per batch, 62-second pause between batches).
    Returns config.WATCHLIST as a fallback when no price data is available.
    """
    log(f"Smart selection: scoring {len(UNIVERSE)} pairs ...")

    events = _fetch_calendar()
    if events:
        log(f"  Calendar: {len(events)} medium/high-impact events in next 48h")
    else:
        log("  Calendar: unavailable or no events — scoring on movement+momentum only")

    api_calls = 0
    pair_scores = {}

    for pair in UNIVERSE:
        # Count uncached calls to enforce rate-limit pacing.
        cache_key = f"SEL:daily:{pair}"
        is_miss = cache.get(cache_key) is None and bool(config.TWELVE_DATA_KEY)

        if is_miss:
            if api_calls > 0 and api_calls % 7 == 0:
                log("  (rate-limit pause 62s ...)")
                time.sleep(62)
            api_calls += 1

        snapshot = _fetch_daily_snapshot(pair)
        base, quote = pair.split("/")

        if snapshot:
            change_abs, directional = _snapshot_scores(snapshot)
        else:
            # No data: give neutral movement/momentum so event scores can still
            # elevate the pair, but it won't crowd out pairs with real data.
            change_abs, directional = 0.0, 3.0

        ev_boost = _event_boost(base, quote, events)

        # Composite: 24h movement 40% + directional momentum 25% + events 35%
        composite = (change_abs * 2.5) + (directional * 0.6) + (ev_boost * 1.8)
        pair_scores[pair] = {
            "score": round(composite, 3),
            "change_24h": round(change_abs, 4),
            "momentum": int(directional),
            "events": round(ev_boost, 2),
        }

    ranked = sorted(pair_scores.items(), key=lambda x: x[1]["score"], reverse=True)

    log(f"  Top {min(top_n, len(ranked))} selected (score = movement + momentum + events):")
    for pair, meta in ranked[:top_n]:
        event_flag = f"  ** {meta['events']:.1f} event pts" if meta["events"] > 0 else ""
        log(
            f"    {pair:10s}  score={meta['score']:5.2f}"
            f"  24h={meta['change_24h']:.3f}%"
            f"  mom={meta['momentum']}/5"
            f"{event_flag}"
        )

    selected = [pair for pair, _ in ranked[:top_n]]

    if not selected:
        log("  WARNING: no price data available — falling back to config.WATCHLIST.")
        return list(config.WATCHLIST)

    return selected
