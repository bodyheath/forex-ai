"""Thin FRED (Federal Reserve Economic Data) client used by the fundamental and
macro layers. Cached and degrades gracefully."""

import requests

import config
from src import cache

_URL = "https://api.stlouisfed.org/fred/series/observations"
_TIMEOUT = 30


def latest(series_id: str):
    """Return (value, date) for the most recent non-missing observation, or
    (None, None) if the series is unavailable."""
    key = f"FRED:{series_id}"
    cached = cache.get(key, ttl_hours=12.0)
    if cached is None:
        try:
            resp = requests.get(
                _URL,
                params={
                    "series_id": series_id,
                    "api_key": config.FRED_API_KEY,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 60,  # enough to find a recent valid point + trend
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            cached = resp.json().get("observations", [])
            cache.set(key, cached)
        except Exception:  # noqa: BLE001
            return None, None

    for obs in cached:
        if obs.get("value") not in (".", "", None):
            try:
                return float(obs["value"]), obs.get("date")
            except ValueError:
                continue
    return None, None


def history(series_id: str, start: str, end: str = None) -> list:
    """Return the full observation history for series_id between start and
    end (YYYY-MM-DD), oldest first, as [{"date": ..., "value": float}, ...]
    with missing ('.') observations dropped.

    2026-09-06: added for the Carry/Macro Agent backtest -- latest()/trend()
    above only ever fetch the most recent 60 points with no date-range
    params, so there was no existing way to pull real historical rate
    history for a backtest. Uncached (backtest-only, one-shot use); live
    code should keep using latest()/trend()'s cached snapshot path, not this.
    """
    params = {
        "series_id": series_id,
        "api_key": config.FRED_API_KEY,
        "file_type": "json",
        "sort_order": "asc",
        "observation_start": start,
    }
    if end:
        params["observation_end"] = end
    try:
        resp = requests.get(_URL, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
    except Exception:  # noqa: BLE001
        return []
    out = []
    for o in obs:
        if o.get("value") in (".", "", None):
            continue
        try:
            out.append({"date": o["date"], "value": float(o["value"])})
        except (TypeError, ValueError):
            continue
    return out


def trend(series_id: str, lookback: int = 12):
    """Return a short 'rising'/'falling'/'flat' descriptor comparing the latest
    value to one ~`lookback` observations back, or None if unavailable."""
    key = f"FRED:{series_id}"
    cached = cache.get(key)
    if not cached:
        latest(series_id)  # populate cache
        cached = cache.get(key) or []
    vals = []
    for obs in cached:
        if obs.get("value") not in (".", "", None):
            try:
                vals.append(float(obs["value"]))
            except ValueError:
                pass
    if len(vals) < 2:
        return None
    recent, older = vals[0], vals[min(lookback, len(vals) - 1)]
    delta = recent - older
    eps = abs(older) * 0.005 if older else 0.0001
    if delta > eps:
        return "rising"
    if delta < -eps:
        return "falling"
    return "flat"
