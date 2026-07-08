"""Economic calendar — high impact event fetching and pair-level news warnings.

Primary data source: Forex Factory XML feed (free, no API key required).
Fallback: Twelve Data economic calendar API.

Events are fetched for the next 7 days, filtered to HIGH impact only, and
cached for 3 hours to avoid redundant API calls.

Public API
----------
get_events_7d()
    Return list[dict] of all HIGH impact events in the next 7 days.
    Each dict: currency, event, plain_name, plain_desc, dt_utc, dt_ak,
               ak_display, avoid_advice, forecast, previous.

events_for_pair(pair, hours=48)
    Return events within `hours` for either currency in `pair`.
    Re-derives hours_away fresh from dt_utc at call time (cache-safe).

build_calendar_section()
    Return list[str] of Telegram-ready lines for the 7-day event timeline.
    Returns [] when no events available. Shows a fallback message when both
    Forex Factory and Twelve Data are unreachable.

warning_lines_for_pair(pair, events)
    Return list[str] of ⚠️ warning lines for a trade block (empty when none).
"""

import sys
from datetime import datetime, timedelta, timezone

import requests

import config
from src import cache

_CACHE_KEY = "CAL:events_7d"
_CACHE_TTL  = 3.0   # hours
_FF_URL     = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

# Updated on each real (non-cached) fetch so build_calendar_section()
# can show the fallback message when both sources fail.
# "forex_factory" | "fmp" | "twelve_data" | "none" | ""
_last_source: str = ""

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

# Forecast direction metadata: (keyword, unit_label, bullish_when)
# bullish_when: "above" = higher result strengthens the currency
#               "below" = lower result strengthens the currency
_FORECAST_META = [
    ("non-farm payroll",     "new jobs",           "above"),
    ("nonfarm payroll",      "new jobs",           "above"),
    ("employment change",    "new jobs",           "above"),
    ("payroll",              "jobs",               "above"),
    ("unemployment rate",    "% unemployment",     "below"),
    ("claimant count",       "claims",             "below"),
    ("jobless claims",       "claims",             "below"),
    ("consumer price index", "%",                  "above"),
    ("cpi",                  "%",                  "above"),
    ("producer price",       "%",                  "above"),
    ("ppi",                  "%",                  "above"),
    ("gdp",                  "% growth",           "above"),
    ("retail sales",         "% change",           "above"),
    ("pmi",                  "",                   "above"),
    ("trade balance",        "",                   "above"),
    ("current account",      "",                   "above"),
    ("interest rate",        "%",                  "above"),
    ("rate decision",        "%",                  "above"),
    ("cash rate",            "%",                  "above"),
    ("fed funds",            "%",                  "above"),
]


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


def _is_us_dst(dt: datetime) -> bool:
    """True when US Eastern is in DST (EDT=UTC-4) rather than EST (UTC-5)."""
    year = dt.year
    mar1      = datetime(year, 3, 1)
    sun2_mar  = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
    dst_start = sun2_mar.replace(hour=2)
    nov1      = datetime(year, 11, 1)
    sun1_nov  = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    dst_end   = sun1_nov.replace(hour=2)
    return dst_start <= dt < dst_end


def _eastern_to_utc(dt_eastern: datetime) -> datetime:
    """Convert US Eastern time to UTC (auto-detects EST vs EDT)."""
    offset = 4 if _is_us_dst(dt_eastern) else 5
    return dt_eastern + timedelta(hours=offset)


def _fmt_value(v: str) -> str:
    """Format Forex Factory value strings for plain-English display.

    '185K' → '185,000'  |  '1.5%' → '1.5%'  |  '2.3B' → '2.3 billion'
    """
    v = v.strip()
    if not v:
        return v
    if len(v) > 1 and v[-1].upper() == "K":
        try:
            num = float(v[:-1].replace(",", ""))
            result = round(num * 1_000)
            return f"{result:,}"
        except ValueError:
            pass
    if len(v) > 1 and v[-1].upper() == "M":
        try:
            num = float(v[:-1].replace(",", ""))
            return f"{num:.1f} million"
        except ValueError:
            pass
    if len(v) > 1 and v[-1].upper() == "B":
        try:
            num = float(v[:-1].replace(",", ""))
            return f"{num:.1f} billion"
        except ValueError:
            pass
    return v


def _build_forecast_desc(event_name: str, currency: str,
                          forecast: str, previous: str, plain_desc: str) -> str:
    """Build a plain-English forecast context string.

    Returns formatted string when forecast/previous data is present, otherwise
    falls back to the generic plain_desc.

    Example: 'forecast 185,000 new jobs — previous 175,000 — a result above
              185,000 will likely strengthen USD'
    """
    if not forecast and not previous:
        return plain_desc

    parts = []
    unit, direction = "", None
    for keyword, unit_text, bullish_when in _FORECAST_META:
        if keyword in event_name.lower():
            unit, direction = unit_text, bullish_when
            break

    if forecast:
        fmt_fc = _fmt_value(forecast)
        if unit:
            # Avoid "3.5% % unemployment" when forecast already ends with %
            display_unit = unit.lstrip("% ") if fmt_fc.endswith("%") else unit
            if display_unit:
                parts.append(f"forecast {fmt_fc} {display_unit}")
            else:
                parts.append(f"forecast {fmt_fc}")
        else:
            parts.append(f"forecast {fmt_fc}")

    if previous:
        parts.append(f"previous {_fmt_value(previous)}")

    if forecast and direction:
        fmt_fc = _fmt_value(forecast)
        word   = "above" if direction == "above" else "below"
        parts.append(f"a result {word} {fmt_fc} will likely strengthen {currency}")

    return " — ".join(parts) if parts else plain_desc


# ── Forex Factory primary fetcher ─────────────────────────────────────────────

def _fetch_forex_factory():
    """Fetch HIGH impact events from Forex Factory XML feed.

    Retries up to 2 times on transient failures. Immediately skips to the
    next source on 429 rate-limit (no retry — sleeping won't help and wastes
    scan time).
    Returns list of event dicts on success (possibly empty when no events this
    week), or None on network/parse failure.
    """
    import xml.etree.ElementTree as ET
    import time as _time_ff
    _last_exc = None
    for _attempt in range(3):
        try:
            r = requests.get(
                _FF_URL,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (forex-ai calendar)"},
            )
            if r.status_code == 429:
                print(f"[ECO-CAL] Forex Factory rate-limited (429) — skipping to next source")
                return None
            r.raise_for_status()
            root = ET.fromstring(r.content)
            break
        except Exception as e:
            _last_exc = e
            if _attempt < 2:
                print(f"[ECO-CAL] Forex Factory attempt {_attempt + 1} failed: {e} — retrying in 5s")
                _time_ff.sleep(5)
    else:
        print(f"[ECO-CAL] Forex Factory fetch failed after 3 attempts: {_last_exc}")
        return None

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    events = []
    _skipped_impact = 0
    _skipped_ccy    = 0
    _skipped_dt     = 0

    # Log first 5 raw impact values so we can verify the actual string format
    _all_events = root.findall("event")
    _sample_impacts = []
    for _ev in _all_events[:10]:
        _iv = (_ev.findtext("impact") or "").strip()
        if _iv and _iv not in _sample_impacts:
            _sample_impacts.append(_iv)
        if len(_sample_impacts) >= 5:
            break
    if _sample_impacts:
        print(f"[ECO-CAL] Raw impact sample values: {_sample_impacts}", file=sys.stderr)

    # Forex Factory uses "High", "Medium", "Low" (title case) in its XML.
    # Lowercased + stripped, valid high-impact strings include "high" and "3"
    # (some versions use numeric 3=high, 2=medium, 1=low). Extra variants for
    # different feed versions / encoding changes.
    _HIGH_IMPACTS   = {"high", "3", "high impact", "red", "🔴"}
    _MEDIUM_IMPACTS = {"medium", "2", "medium impact", "orange", "🟠"}

    # First pass: collect HIGH-impact events
    for ev in _all_events:
        impact = (ev.findtext("impact") or "").strip().lower()
        if impact not in _HIGH_IMPACTS:
            _skipped_impact += 1
            continue

        currency = (ev.findtext("country") or "").strip().upper()
        if currency not in _VALID_CCYS:
            _skipped_ccy += 1
            continue

        title    = (ev.findtext("title")    or "").strip()
        date_str = " ".join((ev.findtext("date") or "").strip().split())  # "07-06-2026" (MM-DD-YYYY) or legacy "Jun 06 2026"
        time_str = (ev.findtext("time")     or "").strip()  # "8:30am", "Tentative"
        forecast = (ev.findtext("forecast") or "").strip()
        previous = (ev.findtext("previous") or "").strip()

        date_part = None
        for _dfmt in ("%m-%d-%Y", "%b %d %Y"):
            try:
                date_part = datetime.strptime(date_str, _dfmt)
                break
            except ValueError:
                pass
        if date_part is None:
            _skipped_dt += 1
            continue

        time_lower = time_str.lower().replace(" ", "")
        if time_lower in ("tentative", "allday", ""):
            hour, minute = 0, 0
        else:
            try:
                if ":" in time_lower:
                    t = datetime.strptime(time_lower, "%I:%M%p")
                else:
                    t = datetime.strptime(time_lower, "%I%p")
                hour, minute = t.hour, t.minute
            except ValueError:
                hour, minute = 0, 0

        dt_eastern = date_part.replace(hour=hour, minute=minute)
        dt_utc     = _eastern_to_utc(dt_eastern)

        if dt_utc < now_utc - timedelta(hours=1):
            _skipped_dt += 1
            continue

        dt_ak       = _to_auckland(dt_utc)
        plain, desc = _plain_name_desc(title)
        desc        = _build_forecast_desc(title, currency, forecast, previous, desc)

        events.append({
            "currency":     currency,
            "event":        title,
            "plain_name":   plain,
            "plain_desc":   desc,
            "dt_utc":       dt_utc.strftime("%Y-%m-%d %H:%M"),
            "dt_ak":        dt_ak.strftime("%Y-%m-%d %H:%M"),
            "ak_display":   _ak_display(dt_ak),
            "avoid_advice": f"avoid new {currency} trades until after this releases",
            "forecast":     forecast,
            "previous":     previous,
        })

    print(
        f"[ECO-CAL] Forex Factory: {len(events)} HIGH-impact events kept "
        f"(skipped: {_skipped_impact} non-high, {_skipped_ccy} unknown ccy, "
        f"{_skipped_dt} bad/past dt)"
    )

    # Fallback: if no HIGH-impact events found, include MEDIUM-impact events as well.
    # This handles Forex Factory format changes and weeks with no high-impact releases.
    if not events:
        print("[ECO-CAL] No HIGH-impact events found — falling back to MEDIUM-impact events")
        _med_skipped_ccy = 0
        _med_skipped_dt  = 0
        for ev in _all_events:
            impact = (ev.findtext("impact") or "").strip().lower()
            if impact not in _MEDIUM_IMPACTS:
                continue
            currency = (ev.findtext("country") or "").strip().upper()
            if currency not in _VALID_CCYS:
                _med_skipped_ccy += 1
                continue
            title    = (ev.findtext("title")    or "").strip()
            date_str = " ".join((ev.findtext("date") or "").strip().split())  # "07-06-2026" (MM-DD-YYYY) or legacy "Jun 06 2026"
            time_str = (ev.findtext("time")     or "").strip()
            forecast = (ev.findtext("forecast") or "").strip()
            previous = (ev.findtext("previous") or "").strip()
            date_part = None
            for _dfmt in ("%m-%d-%Y", "%b %d %Y"):
                try:
                    date_part = datetime.strptime(date_str, _dfmt)
                    break
                except ValueError:
                    pass
            if date_part is None:
                _med_skipped_dt += 1
                continue
            time_lower = time_str.lower().replace(" ", "")
            if time_lower in ("tentative", "allday", ""):
                hour, minute = 0, 0
            else:
                try:
                    if ":" in time_lower:
                        t = datetime.strptime(time_lower, "%I:%M%p")
                    else:
                        t = datetime.strptime(time_lower, "%I%p")
                    hour, minute = t.hour, t.minute
                except ValueError:
                    hour, minute = 0, 0
            dt_eastern = date_part.replace(hour=hour, minute=minute)
            dt_utc     = _eastern_to_utc(dt_eastern)
            if dt_utc < now_utc - timedelta(hours=1):
                _med_skipped_dt += 1
                continue
            dt_ak       = _to_auckland(dt_utc)
            plain, desc = _plain_name_desc(title)
            desc        = _build_forecast_desc(title, currency, forecast, previous, desc)
            events.append({
                "currency":     currency,
                "event":        f"[Medium] {title}",
                "plain_name":   plain,
                "plain_desc":   f"⚠️ Medium impact: {desc}",
                "dt_utc":       dt_utc.strftime("%Y-%m-%d %H:%M"),
                "dt_ak":        dt_ak.strftime("%Y-%m-%d %H:%M"),
                "ak_display":   _ak_display(dt_ak),
                "avoid_advice": f"monitor {currency} — may cause minor movement",
                "forecast":     forecast,
                "previous":     previous,
            })
        print(f"[ECO-CAL] Medium-impact fallback: {len(events)} events (skipped {_med_skipped_ccy} unknown ccy, {_med_skipped_dt} bad/past dt)", file=sys.stderr)

    events.sort(key=lambda e: e["dt_utc"])
    return events


# ── Twelve Data fallback fetcher ──────────────────────────────────────────────

def _fetch_twelve_data():
    """Fetch HIGH impact events from Twelve Data economic calendar.

    Returns list of event dicts on success (possibly empty), or None on failure.
    """
    if not config.TWELVE_DATA_KEY:
        print("[ECO-CAL] TWELVE_DATA_KEY not set — Twelve Data calendar unavailable")
        return None

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    end_utc = now_utc + timedelta(days=7)
    _td_resp = None
    try:
        _td_resp = requests.get(
            "https://api.twelvedata.com/economic_calendar",
            params={
                "start_date": now_utc.strftime("%Y-%m-%d"),
                "end_date":   end_utc.strftime("%Y-%m-%d"),
                "apikey":     config.TWELVE_DATA_KEY,
            },
            timeout=15,
        )
        data = _td_resp.json()
    except Exception as e:
        _td_body = ""
        try:
            _td_body = (_td_resp.text[:300] if _td_resp is not None else "")
        except Exception:
            pass
        print(f"[ECO-CAL] Twelve Data fetch failed: {e} — raw response: {_td_body!r}")
        return None

    raw = data.get("result", data)
    if isinstance(raw, dict):
        raw = raw.get("events", [])
    if not isinstance(raw, list):
        print(
            f"[ECO-CAL] Twelve Data unexpected response — keys: "
            f"{list(data.keys()) if isinstance(data, dict) else type(data).__name__}"
        )
        return None

    print(f"[ECO-CAL] Twelve Data: {len(raw)} raw events before impact/currency filter")

    events = []
    _skipped_impact = 0
    _skipped_ccy    = 0
    _skipped_dt     = 0
    for ev in raw:
        imp_raw = ev.get("importance", "")
        if isinstance(imp_raw, int):
            importance = {3: "high", 2: "medium", 1: "low"}.get(imp_raw, "")
        else:
            importance = str(imp_raw).lower().strip()
        if importance != "high":
            _skipped_impact += 1
            continue

        cur_raw  = (ev.get("currency") or ev.get("country") or "").strip()
        currency = (
            cur_raw.upper() if len(cur_raw) == 3
            else _COUNTRY_CURRENCY.get(cur_raw.lower(), "").upper()
        )
        if not currency or currency not in _VALID_CCYS:
            _skipped_ccy += 1
            continue

        dt_str = str(ev.get("datetime", "") or ev.get("date", ""))
        dt_utc = _parse_dt(dt_str)
        if dt_utc is None or dt_utc < now_utc - timedelta(hours=1):
            _skipped_dt += 1
            continue

        dt_ak       = _to_auckland(dt_utc)
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
            "forecast":     "",
            "previous":     "",
        })

    print(
        f"[ECO-CAL] Twelve Data: {len(events)} HIGH-impact events kept "
        f"(skipped: {_skipped_impact} non-high, {_skipped_ccy} unknown ccy, "
        f"{_skipped_dt} bad/past dt)"
    )
    events.sort(key=lambda e: e["dt_utc"])
    return events


# ── Financial Modeling Prep fallback fetcher ──────────────────────────────────

_FMP_URL = "https://financialmodelingprep.com/api/v3/economic_calendar"

def _fetch_fmp():
    """Fetch HIGH impact events from Financial Modeling Prep free API.

    FMP free tier requires no API key. Returns list of event dicts on success
    (possibly empty), or None on failure.
    """
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    end_utc = now_utc + timedelta(days=7)
    try:
        r = requests.get(
            _FMP_URL,
            params={
                "from": now_utc.strftime("%Y-%m-%d"),
                "to":   end_utc.strftime("%Y-%m-%d"),
            },
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (forex-ai calendar)"},
        )
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        print(f"[ECO-CAL] FMP fetch failed: {e}")
        return None

    if not isinstance(raw, list):
        print(f"[ECO-CAL] FMP unexpected response type: {type(raw).__name__}")
        return None

    _HIGH_IMPACT_FMP = {"high", "3", "high impact"}
    events = []
    _skipped_impact = _skipped_ccy = _skipped_dt = 0
    for ev in raw:
        impact = str(ev.get("impact") or ev.get("importance") or "").strip().lower()
        if impact not in _HIGH_IMPACT_FMP:
            _skipped_impact += 1
            continue

        currency = str(ev.get("currency") or ev.get("country") or "").strip().upper()
        if currency not in _VALID_CCYS:
            _skipped_ccy += 1
            continue

        title    = str(ev.get("event") or ev.get("name") or "").strip()
        date_str = str(ev.get("date") or "").strip()  # "2025-06-06 08:30:00" or "2025-06-06"

        try:
            if len(date_str) >= 16:
                dt_utc = datetime.strptime(date_str[:16], "%Y-%m-%d %H:%M")
            elif len(date_str) == 10:
                dt_utc = datetime.strptime(date_str, "%Y-%m-%d")
            else:
                _skipped_dt += 1
                continue
        except ValueError:
            _skipped_dt += 1
            continue

        if dt_utc < now_utc - timedelta(hours=1):
            _skipped_dt += 1
            continue

        dt_ak       = _to_auckland(dt_utc)
        plain, desc = _plain_name_desc(title)

        events.append({
            "currency":     currency,
            "event":        title,
            "plain_name":   plain,
            "plain_desc":   desc,
            "dt_utc":       dt_utc.strftime("%Y-%m-%d %H:%M"),
            "dt_ak":        dt_ak.strftime("%Y-%m-%d %H:%M"),
            "ak_display":   _ak_display(dt_ak),
            "avoid_advice": f"avoid new {currency} trades until after this releases",
            "forecast":     str(ev.get("estimate") or ev.get("forecast") or "").strip(),
            "previous":     str(ev.get("previous") or "").strip(),
        })

    print(
        f"[ECO-CAL] FMP: {len(events)} HIGH-impact events kept "
        f"(skipped: {_skipped_impact} non-high, {_skipped_ccy} unknown ccy, "
        f"{_skipped_dt} bad/past dt)"
    )
    events.sort(key=lambda e: e["dt_utc"])
    return events


# ── Process-level in-memory cache ────────────────────────────────────────────
# Prevents repeated HTTP round-trips when all sources fail within a single scan
# run (which calls get_events_7d() 5+ times).  Cleared on next successful fetch
# or after _PROC_CACHE_TTL seconds.
import time as _time_proc
_proc_cache_value: list | None = None
_proc_cache_at: float = 0.0
_PROC_CACHE_TTL = 1800  # 30 minutes — long enough to cover a full scan run


# ── Public API ────────────────────────────────────────────────────────────────

def get_events_7d() -> list:
    """Fetch HIGH impact events for the next 7 days.

    Priority order:
      1. Forex Factory XML (no retry on 429; 2 retries on other failures)
      2. Twelve Data economic calendar API (requires TWELVE_DATA_KEY)
      3. Empty list — cached in-process for 30 min so subsequent within-scan
         calls don't re-attempt all sources

    Results cached on-disk for 3 hours on success.
    Note: FMP was removed — it now requires an API key that isn't configured.
    """
    global _last_source, _proc_cache_value, _proc_cache_at

    # 1. On-disk cache (successful fetches only, 3-hour TTL)
    cached = cache.get(_CACHE_KEY, ttl_hours=_CACHE_TTL)
    if cached is not None:
        return cached

    # 2. In-process cache (covers both success and failure within a scan run)
    if _proc_cache_value is not None and (_time_proc.time() - _proc_cache_at) < _PROC_CACHE_TTL:
        return _proc_cache_value

    # Primary: Forex Factory
    ff = _fetch_forex_factory()
    if ff:  # non-empty list = real success; [] means no future events this week
        _last_source = "forex_factory"
        cache.set(_CACHE_KEY, ff)
        _proc_cache_value = ff
        _proc_cache_at = _time_proc.time()
        return ff
    if ff is not None:
        # FF responded but returned 0 future events (all past or bad dates).
        # Try TD which returns a rolling 7-day window regardless of the Mon–Sun
        # week boundary that ff_calendar_thisweek.xml is limited to.
        print("[ECO-CAL] Forex Factory: 0 future events in thisweek feed — trying Twelve Data for next 7 days")

    # Fallback: Twelve Data
    td = _fetch_twelve_data()
    if td is not None:
        _last_source = "twelve_data"
        cache.set(_CACHE_KEY, td)
        _proc_cache_value = td
        _proc_cache_at = _time_proc.time()
        return td

    # All sources failed — cache empty result in-process so subsequent
    # within-scan calls don't hammer the same failing endpoints again.
    _last_source = "none"
    print("[ECO-CAL] Forex Factory and Twelve Data both unavailable")
    _proc_cache_value = []
    _proc_cache_at = _time_proc.time()
    return []


def events_for_pair(pair: str, hours: float = 48.0) -> list:
    """Return upcoming events within `hours` for either currency in `pair`.

    hours_away is recomputed fresh at call time so cached events stay accurate.
    """
    cleaned = pair.upper().replace("/", "").replace("-", "")
    base  = cleaned[:3] if len(cleaned) >= 3 else ""
    quote = cleaned[3:6] if len(cleaned) >= 6 else ""
    ccys  = {c for c in (base, quote) if c}

    all_ev  = get_events_7d()
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
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

    Returns [] when no events are scheduled. Shows a fallback message when
    both data sources are unreachable.
    """
    all_ev  = get_events_7d()
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    future = []
    for e in all_ev:
        try:
            dt_utc = datetime.strptime(e["dt_utc"], "%Y-%m-%d %H:%M")
            if dt_utc >= now_utc - timedelta(minutes=30):
                future.append(e)
        except Exception:
            future.append(e)

    if not future:
        if _last_source == "none":
            return [
                "",
                "━━━━━━━━━━━━━━━━━━━━━",
                "📅 <b>UPCOMING HIGH IMPACT EVENTS — Next 7 Days</b>",
                "📅 Economic calendar temporarily unavailable — check back at next scan",
            ]
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
        ccy     = ev["currency"]
        plain   = ev["plain_name"]
        ak_disp = ev["ak_display"]
        day     = ak_disp.split()[0]
        hours   = ev.get("hours_away", 24)

        if hours <= 6:
            timing = f"very soon ({ak_disp})"
        elif hours <= 24:
            timing = f"today ({ak_disp})"
        else:
            timing = f"on {day} ({ak_disp})"

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
