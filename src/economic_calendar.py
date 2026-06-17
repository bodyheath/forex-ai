"""Economic calendar — high impact event fetching and pair-level news warnings.

Data source: Twelve Data economic calendar API (same credentials and endpoint
already used by src/selector.py for pair pre-scoring).

Events are fetched for the next 7 days, filtered to HIGH impact only, and
cached for 3 hours to avoid redundant API calls.

Public API
----------
get_events_7d()
    Return list[dict] of all HIGH impact events in the next 7 days.
    Each dict: currency, event, plain_name, plain_desc, dt_utc, dt_ak,
               ak_display, avoid_advice.

events_for_pair(pair, hours=48)
    Return events within `hours` for either currency in `pair`.
    Re-derives hours_away fresh from dt_utc at call time (cache-safe).

build_calendar_section()
    Return list[str] of Telegram-ready lines for the 7-day event timeline.
    Returns [] when no events available or Twelve Data unreachable.

warning_lines_for_pair(pair, events)
    Return list[str] of ⚠️ warning lines for a trade block (empty when none).
"""

from datetime import datetime, timedelta, timezone

import requests

import config
from src import cache

_CACHE_KEY = "CAL:events_7d"
_CACHE_TTL  = 3.0   # hours — generous enough to avoid quota burn

# ── Plain English event name lookup ───────────────────────────────────────────
# (keyword_in_lowercase, plain_name, plain_description)
# Checked in order; first match wins.
_EVENT_LOOKUP = [
    # Major central bank decisions ────────────────────────────────────────────
    ("non-farm payroll",            "US Jobs Report",                       "this is the most watched jobs data and regularly causes large USD moves"),
    ("nonfarm payroll",             "US Jobs Report",                       "this is the most watched jobs data and regularly causes large USD moves"),
    ("fomc statement",              "US Federal Reserve Rate Decision",      "the Fed announces US interest rates — this causes large USD moves"),
    ("fomc",                        "US Federal Reserve Meeting",            "the Federal Reserve discusses US interest rates — this causes large USD moves"),
    ("federal reserve",             "US Federal Reserve Meeting",            "the Fed sets US interest rates — this causes large USD moves"),
    ("fed funds",                   "US Interest Rate Decision",             "the Federal Reserve sets US interest rates — this causes large USD moves"),
    ("bank of england rate",        "Bank of England Rate Decision",         "the Bank of England sets UK interest rates — this causes large GBP moves"),
    ("bank of england",             "Bank of England Meeting",               "the Bank of England sets UK interest rates — this causes large GBP moves"),
    ("ecb rate",                    "ECB Rate Decision",                     "the ECB sets Eurozone interest rates — this causes large EUR moves"),
    ("european central bank",       "ECB Meeting",                           "the ECB sets Eurozone interest rates — this causes large EUR moves"),
    ("bank of japan rate",          "Bank of Japan Rate Decision",           "the Bank of Japan sets Japanese interest rates — this causes large JPY moves"),
    ("bank of japan",               "Bank of Japan Meeting",                 "the Bank of Japan sets Japanese interest rates — this causes large JPY moves"),
    ("reserve bank of australia",   "RBA Rate Decision",                     "the RBA sets Australian interest rates — this causes large AUD moves"),
    ("rba rate",                    "RBA Rate Decision",                     "the RBA sets Australian interest rates — this causes large AUD moves"),
    ("reserve bank of new zealand", "RBNZ Rate Decision",                    "the RBNZ sets NZ interest rates — this causes large NZD moves"),
    ("rbnz rate",                   "RBNZ Rate Decision",                    "the RBNZ sets NZ interest rates — this causes large NZD moves"),
    ("bank of canada rate",         "Bank of Canada Rate Decision",          "the Bank of Canada sets Canadian rates — this causes large CAD moves"),
    ("bank of canada",              "Bank of Canada Meeting",                "the Bank of Canada sets Canadian rates — this causes large CAD moves"),
    ("swiss national bank",         "Swiss National Bank Rate Decision",     "the SNB sets Swiss interest rates — this causes large CHF moves"),
    ("norges bank",                 "Norges Bank Rate Decision",             "this causes large NOK moves"),
    ("riksbank",                    "Riksbank Rate Decision",                "this causes large SEK moves"),
    # Key data releases ───────────────────────────────────────────────────────
    ("consumer price index",        "Inflation Report (CPI)",                "inflation data regularly causes large currency moves"),
    ("cpi",                         "Inflation Report",                      "inflation data regularly causes large currency moves"),
    ("producer price",              "Producer Price Inflation Report",       "wholesale inflation data regularly causes large currency moves"),
    ("ppi",                         "Producer Price Report",                 "producer price data regularly causes large currency moves"),
    ("gdp",                         "GDP Growth Report",                     "economic growth data regularly causes large currency moves"),
    ("retail sales",                "Retail Sales Report",                   "consumer spending data regularly causes large currency moves"),
    ("unemployment rate",           "Unemployment Report",                   "jobs data regularly causes large currency moves"),
    ("claimant count",              "Unemployment Claims Report",            "jobs data regularly causes large currency moves"),
    ("jobless claims",              "Unemployment Claims",                   "jobs data regularly causes large currency moves"),
    ("employment change",           "Employment Change Report",              "jobs data regularly causes large currency moves"),
    ("payroll",                     "Jobs Report",                           "payroll data regularly causes large currency moves"),
    ("jobs",                        "Jobs Report",                           "jobs data regularly causes large currency moves"),
    ("pmi",                         "Business Activity Report (PMI)",        "business confidence data regularly causes large currency moves"),
    ("trade balance",               "Trade Balance Report",                  "trade data regularly causes large currency moves"),
    ("current account",             "Current Account Report",                "trade/capital flow data regularly causes large currency moves"),
    ("interest rate decision",      "Interest Rate Decision",                "central bank rate decisions regularly cause large currency moves"),
    ("rate decision",               "Interest Rate Decision",                "central bank rate decisions regularly cause large currency moves"),
    ("monetary policy",             "Central Bank Policy Statement",         "policy statements regularly cause large currency moves"),
    ("inflation",                   "Inflation Report",                      "inflation data regularly causes large currency moves"),
]

_VALID_CCYS = {
    "USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF",
    "NOK", "SEK", "SGD", "HKD", "CNH", "DKK",
}

_COUNTRY_CURRENCY = {
    "united states": "USD", "us": "USD",
    "eurozone": "EUR", "euro area": "EUR", "european union": "EUR",
    "united kingdom": "GBP", "uk": "GBP",
    "japan": "JPY", "australia": "AUD", "canada": "CAD",
    "switzerland": "CHF", "new zealand": "NZD",
    "china": "CNH", "hong kong": "HKD", "singapore": "SGD",
    "sweden": "SEK", "norway": "NOK", "denmark": "DKK",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_auckland(dt_utc: datetime) -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return dt_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Pacific/Auckland"))
    except Exception:
        off = 13 if dt_utc.month in (10, 11, 12, 1, 2, 3) else 12
        return dt_utc + timedelta(hours=off)


def _ak_display(dt_ak: datetime) -> str:
    """Format as 'Thursday 8:30pm Auckland'."""
    day = dt_ak.strftime("%A")
    h, m = dt_ak.hour, dt_ak.minute
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    time_str = f"{h12}:{m:02d}{suffix}" if m else f"{h12}{suffix}"
    return f"{day} {time_str} Auckland"


def _plain_name_desc(event_name: str) -> tuple:
    low = event_name.lower()
    for keyword, plain, desc in _EVENT_LOOKUP:
        if keyword in low:
            return plain, desc
    return event_name, "this regularly causes large currency moves"


def _parse_dt(dt_str: str):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(dt_str.strip(), fmt)
        except ValueError:
            continue
    return None


# ── Public API ────────────────────────────────────────────────────────────────

def get_events_7d() -> list:
    """Fetch HIGH impact events for the next 7 days from Twelve Data. Cached 3h.

    Returns list of event dicts sorted by dt_utc.
    Each dict has: currency, event, plain_name, plain_desc, dt_utc (str),
                   dt_ak (str), ak_display, avoid_advice.
    """
    cached = cache.get(_CACHE_KEY, ttl_hours=_CACHE_TTL)
    if cached is not None:
        return cached

    if not config.TWELVE_DATA_KEY:
        print("[ECO-CAL] TWELVE_DATA_KEY not set — economic calendar unavailable")
        return []

    now_utc = datetime.utcnow()
    end_utc = now_utc + timedelta(days=7)
    try:
        r = requests.get(
            "https://api.twelvedata.com/economic_calendar",
            params={
                "start_date": now_utc.strftime("%Y-%m-%d"),
                "end_date":   end_utc.strftime("%Y-%m-%d"),
                "apikey":     config.TWELVE_DATA_KEY,
            },
            timeout=15,
        )
        data = r.json()
    except Exception as _api_err:
        print(f"[ECO-CAL] API request failed: {_api_err}")
        cache.set(_CACHE_KEY, [])
        return []

    raw = data.get("result", data)
    if isinstance(raw, dict):
        raw = raw.get("events", [])
    if not isinstance(raw, list):
        print(f"[ECO-CAL] Unexpected API response structure — top-level keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
        cache.set(_CACHE_KEY, [])
        return []

    print(f"[ECO-CAL] API returned {len(raw)} raw events before impact/currency filter")
    if raw:
        _sample = raw[0]
        print(f"[ECO-CAL] Sample event keys: {list(_sample.keys()) if isinstance(_sample, dict) else _sample}")

    events = []
    _skipped_impact = 0
    _skipped_ccy = 0
    _skipped_dt = 0
    for ev in raw:
        # Impact filter — HIGH only
        imp_raw = ev.get("importance", "")
        if isinstance(imp_raw, int):
            importance = {3: "high", 2: "medium", 1: "low"}.get(imp_raw, "")
        else:
            importance = str(imp_raw).lower().strip()
        if importance != "high":
            _skipped_impact += 1
            continue

        # Currency resolution
        cur_raw  = (ev.get("currency") or ev.get("country") or "").strip()
        currency = (
            cur_raw.upper() if len(cur_raw) == 3
            else _COUNTRY_CURRENCY.get(cur_raw.lower(), "").upper()
        )
        if not currency or currency not in _VALID_CCYS:
            _skipped_ccy += 1
            continue

        # Datetime parsing (UTC)
        dt_str = str(ev.get("datetime", "") or ev.get("date", ""))
        dt_utc = _parse_dt(dt_str)
        if dt_utc is None:
            _skipped_dt += 1
            continue
        if dt_utc < now_utc - timedelta(hours=1):
            _skipped_dt += 1
            continue  # already past

        dt_ak      = _to_auckland(dt_utc)
        plain, desc = _plain_name_desc(ev.get("event", ""))

        events.append({
            "currency":     currency,
            "event":        ev.get("event", ""),
            "plain_name":   plain,
            "plain_desc":   desc,
            "dt_utc":       dt_utc.strftime("%Y-%m-%d %H:%M"),
            "dt_ak":        dt_ak.strftime("%Y-%m-%d %H:%M"),
            "ak_display":   _ak_display(dt_ak),
            "avoid_advice": f"avoid new {currency} trades until after this releases",
        })

    print(
        f"[ECO-CAL] Filter result: {len(events)} HIGH-impact events kept "
        f"(skipped: {_skipped_impact} non-high, {_skipped_ccy} unknown currency, "
        f"{_skipped_dt} bad/past datetime)"
    )
    events.sort(key=lambda e: e["dt_utc"])
    cache.set(_CACHE_KEY, events)
    return events


def events_for_pair(pair: str, hours: float = 48.0) -> list:
    """Return upcoming events within `hours` for either currency in `pair`.

    hours_away is recomputed fresh at call time so cached events stay accurate.
    """
    cleaned = pair.upper().replace("/", "").replace("-", "")
    base  = cleaned[:3] if len(cleaned) >= 3 else ""
    quote = cleaned[3:6] if len(cleaned) >= 6 else ""
    ccys  = {c for c in (base, quote) if c}

    all_ev  = get_events_7d()
    now_utc = datetime.utcnow()
    result  = []
    for e in all_ev:
        if e["currency"] not in ccys:
            continue
        try:
            dt_utc   = datetime.strptime(e["dt_utc"], "%Y-%m-%d %H:%M")
            hrs_away = (dt_utc - now_utc).total_seconds() / 3600
        except Exception:
            continue
        if 0 <= hrs_away <= hours:
            result.append({**e, "hours_away": round(hrs_away, 1)})
    return result


def build_calendar_section() -> list:
    """Return Telegram-ready lines for the 7-day high impact event timeline.

    Returns [] when no events are available.
    """
    all_ev  = get_events_7d()
    now_utc = datetime.utcnow()

    # Re-filter so stale cache entries don't show past events
    future = []
    for e in all_ev:
        try:
            dt_utc = datetime.strptime(e["dt_utc"], "%Y-%m-%d %H:%M")
            if dt_utc >= now_utc - timedelta(minutes=30):
                future.append(e)
        except Exception:
            future.append(e)

    if not future:
        return []

    lines = [
        "",
        "━━━━━━━━━━━━━━━━━━━━━",
        "📅 <b>UPCOMING HIGH IMPACT EVENTS — Next 7 Days</b>",
        "These events can cause sudden large moves — plan your trades around them",
    ]
    for ev in future:
        lines.append(
            f"🗓 {ev['ak_display']} — <b>{ev['plain_name']}</b> ({ev['currency']}) — "
            f"{ev['plain_desc']} — {ev['avoid_advice']}"
        )
    return lines


def warning_lines_for_pair(pair: str, events: list = None) -> list:
    """Return ⚠️ plain English warning lines for a trade block.

    `events` should be from events_for_pair(pair, hours=48); fetched automatically
    when omitted.  Returns [] when no events apply.
    """
    if events is None:
        events = events_for_pair(pair, hours=48)
    if not events:
        return []

    lines = []
    for ev in events:
        ccy       = ev["currency"]
        plain     = ev["plain_name"]
        ak_disp   = ev["ak_display"]       # "Friday 2:00pm Auckland"
        day       = ak_disp.split()[0]     # "Friday"
        hours     = ev.get("hours_away", 24)

        if hours <= 6:
            timing = f"very soon ({ak_disp})"
        elif hours <= 24:
            timing = f"today ({ak_disp})"
        else:
            timing = f"on {day} ({ak_disp})"

        # Use natural language based on whether it is a meeting or a data release
        low = plain.lower()
        is_meeting = any(kw in low for kw in (
            "meeting", "decision", "policy statement", "rate statement"
        ))
        if is_meeting:
            summary   = f"{plain} is scheduled {timing}"
            after_txt = "the meeting"
        else:
            summary   = f"{plain} releases {timing}"
            after_txt = "this data"

        lines += [
            f"⚠️ <b>News Warning</b> — {summary}.",
            f"This could cause sudden large moves in all {ccy} pairs.",
            f"Consider waiting until after {after_txt} before entering this trade.",
        ]
    return lines
