"""Positioning layer: CFTC Commitments of Traders (COT), Legacy Futures-Only.

Large speculators (non-commercials) net long/short is a classic positioning gauge.
We also compute where current net positioning sits within its recent range to flag
'extreme / due for reversal' conditions. Free public Socrata API, no key needed.

Robustness:
- Each currency maps to an EXACT exchange-qualified market name (see config), so we
  never accidentally pick up a cross-rate contract (e.g. EUR/GBP for EUR).
- Stale markets (some froze in this dataset in Feb 2022) are rejected: if the most
  recent report is older than STALE_DAYS, the layer reports UNAVAILABLE rather than
  presenting years-old numbers as current.
"""

from datetime import datetime, timezone

import requests

import config
from src import cache

_TIMEOUT = 30
STALE_DAYS = 30


def _series_for(market_name: str) -> list:
    """Recent weekly COT rows for an EXACT market name, newest first."""
    key = f"COT:exact:{market_name}"
    cached = cache.get(key, ttl_hours=12.0)
    if cached is not None:
        return cached
    try:
        escaped = market_name.replace("'", "''")  # SoQL string-literal escaping
        resp = requests.get(
            config.COT_DATASET_URL,
            params={
                "$where": f"market_and_exchange_names = '{escaped}'",
                "$order": "report_date_as_yyyy_mm_dd DESC",
                "$limit": 52,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()
        cache.set(key, rows)
        return rows
    except Exception:  # noqa: BLE001
        return []


def _net_specs(row: dict):
    """Net non-commercial (large speculator) position = long - short."""
    try:
        longs = float(row["noncomm_positions_long_all"])
        shorts = float(row["noncomm_positions_short_all"])
        return longs - shorts
    except (KeyError, TypeError, ValueError):
        return None


def _days_old(date_str: str):
    try:
        d = datetime.fromisoformat(date_str.replace("Z", "")).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).days
    except (ValueError, AttributeError):
        return None


def _for_currency(ccy: str) -> dict:
    meta = config.CURRENCIES.get(ccy, {})
    market = meta.get("cot_market")
    if not market:
        return {"currency": ccy, "status": "UNAVAILABLE", "reason": "no COT market mapped"}

    rows = _series_for(market)
    if not rows:
        return {
            "currency": ccy,
            "status": "UNAVAILABLE",
            "reason": f"no COT rows for '{market}' (verify dataset id / market name)",
        }

    report_date = (rows[0].get("report_date_as_yyyy_mm_dd") or "")
    age = _days_old(report_date)
    if age is not None and age > STALE_DAYS:
        return {
            "currency": ccy,
            "status": "UNAVAILABLE",
            "reason": f"stale data: latest COT report is {report_date[:10]} ({age} days old)",
            "matched_market": market,
        }

    nets = [n for n in (_net_specs(r) for r in rows) if n is not None]
    if not nets:
        return {"currency": ccy, "status": "UNAVAILABLE", "reason": "net position fields missing"}

    latest = nets[0]
    hi, lo = max(nets), min(nets)
    pct_of_range = (latest - lo) / (hi - lo) * 100 if hi != lo else 50.0
    if pct_of_range >= 85:
        extreme = "net positioning near the TOP of its ~1y range (crowded long, reversal risk)"
    elif pct_of_range <= 15:
        extreme = "net positioning near the BOTTOM of its ~1y range (crowded short, reversal risk)"
    else:
        extreme = "net positioning mid-range (not extreme)"

    return {
        "currency": ccy,
        "status": "ok",
        "report_date": report_date[:10],
        "matched_market": rows[0].get("market_and_exchange_names", market),
        "weeks_of_history": len(nets),
        "net_speculator_position": int(latest),
        "direction": "net LONG" if latest > 0 else "net SHORT",
        "one_year_high": int(hi),
        "one_year_low": int(lo),
        "percentile_in_range": round(pct_of_range, 0),
        "extreme_flag": extreme,
    }


def analyse(base: str, quote: str) -> dict:
    return {"status": "ok", "base": _for_currency(base), "quote": _for_currency(quote)}
