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

import hashlib
import sys
import time as _time_mod
from datetime import datetime, timezone
from pathlib import Path

import requests

import config
from src import cache

_TIMEOUT = 30
STALE_DAYS = 14

# Module-level consecutive failure counter — resets each time a fetch succeeds.
# After 3 consecutive failures across any currencies, all remaining COT fetches
# this scan are skipped and pairs receive a neutral COT score.
_cot_consecutive_failures: list = [0]   # mutable so _series_for can update it

# Track permanently stale markets already logged this session to suppress noise.
_permanently_stale_logged: set = set()


def _cache_path_for_key(key: str) -> Path:
    """Return the disk Path for a cache key (mirrors cache._path_for internals)."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return config.CACHE_DIR / f"{digest}.json"


def _cot_cache_info(market_name: str) -> dict:
    """Return diagnostic info about the COT cache entry for a market.

    Returns:
        file_path    — absolute path to cache file
        file_exists  — bool
        cache_age_h  — hours since last fetch (None if file absent)
        data_age_d   — days since latest report date in cached data (None if absent)
        status       — "fresh" | "stale" | "missing" | "fetching"
    """
    key  = f"COT:exact:{market_name}"
    path = _cache_path_for_key(key)
    if not path.exists():
        return {"file_path": path, "file_exists": False,
                "cache_age_h": None, "data_age_d": None, "status": "missing"}
    try:
        import json as _json
        payload   = _json.loads(path.read_text(encoding="utf-8"))
        cache_age = (_time_mod.time() - payload.get("_cached_at", 0)) / 3600.0
        rows      = payload.get("value") or []
        if rows and isinstance(rows, list):
            rdate    = (rows[0].get("report_date_as_yyyy_mm_dd") or "")
            data_age = _days_old(rdate)
        else:
            data_age = None
        stale = data_age is not None and data_age > STALE_DAYS
        return {
            "file_path":   path,
            "file_exists": True,
            "cache_age_h": round(cache_age, 1),
            "data_age_d":  data_age,
            "status":      "stale" if stale else "fresh",
        }
    except Exception:
        return {"file_path": path, "file_exists": True,
                "cache_age_h": None, "data_age_d": None, "status": "missing"}


def log_and_clean_cot_status(log_fn=None) -> dict:
    """Log COT cache status for every tracked market; delete stale entries.

    Should be called once per scan (before _for_currency is called) so that:
    - The operator can see data age in the run log
    - Stale cache files are proactively deleted so the next _series_for() call
      gets a truly clean fetch from the CFTC API

    log_fn — callable(str) for the scan log; defaults to stderr print.
    Returns a summary dict with worst-case data age across all markets.
    """
    if log_fn is None:
        log_fn = lambda m: print(m, file=sys.stderr)

    markets_checked = 0
    worst_data_age  = 0
    any_deleted     = 0

    for ccy, meta in config.CURRENCIES.items():
        market = meta.get("cot_market")
        if not market:
            continue
        markets_checked += 1
        info = _cot_cache_info(market)
        da   = info.get("data_age_d")
        ca   = info.get("cache_age_h")
        st   = info.get("status")

        if da is not None and da > worst_data_age:
            worst_data_age = da

        if st == "missing":
            log_fn(
                f"[COT] {market}: no cache — will fetch from CFTC on next call"
            )
        elif st == "stale":
            log_fn(
                f"[COT] Cache is {da} days old for '{market}' — "
                f"DELETING stale entry and fetching fresh data"
            )
            try:
                path = info["file_path"]
                if path.exists():
                    path.unlink()
                    any_deleted += 1
                    log_fn(f"[COT] Deleted stale cache for '{market}' ✓")
            except Exception as _del_exc:
                log_fn(f"[COT] Could not delete cache for '{market}': {_del_exc}")
        else:
            ca_str = f"{ca:.1f}h" if ca is not None else "?"
            da_str = f"{da}d" if da is not None else "?"
            log_fn(
                f"[COT] {market}: cache {ca_str} old · data {da_str} old · Status: fresh"
            )

    return {
        "markets_checked": markets_checked,
        "worst_data_age_days": worst_data_age,
        "deleted_stale": any_deleted,
    }


def get_cot_worst_age_days() -> int:
    """Return the maximum data age (days since COT report) across all tracked markets.

    Used by data_quality.assess_scan() for the scorecard.  Returns 0 when all
    markets have fresh data or no COT markets are configured.
    """
    worst = 0
    for ccy, meta in config.CURRENCIES.items():
        market = meta.get("cot_market")
        if not market:
            continue
        info = _cot_cache_info(market)
        da   = info.get("data_age_d")
        if da is not None and da > worst:
            worst = da
    return worst


def _series_for(market_name: str) -> list:
    """Recent weekly COT rows for an EXACT market name, newest first."""
    key = f"COT:exact:{market_name}"
    cached = cache.get(key, ttl_hours=12.0)
    if cached is not None:
        # Check if the cached data is itself stale (> 14 days old)
        if cached:
            _rdate = (cached[0].get("report_date_as_yyyy_mm_dd") or "")
            _age   = _days_old(_rdate)
            if _age is not None and _age > 365:
                # Permanently stale — no point re-fetching; serve from cache
                return cached
            if _age is not None and _age > STALE_DAYS:
                # Stale but recoverable — delete cache so next run gets a fresh fetch
                try:
                    _stale_path = _cache_path_for_key(key)
                    if _stale_path.exists():
                        _stale_path.unlink()
                except Exception:
                    pass
                # Fall through to fresh fetch below
            else:
                return cached
        else:
            return cached
    _last_exc = None
    _BACKOFF = [0, 60, 120]   # seconds to wait before each attempt (0 = immediate)
    for _attempt in range(3):
        if _BACKOFF[_attempt] > 0:
            print(
                f"[COT] CFTC fetch failed (attempt {_attempt} of 3) — "
                f"waiting {_BACKOFF[_attempt]} seconds before retry",
                file=sys.stderr,
            )
            _time_mod.sleep(_BACKOFF[_attempt])
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
            if rows:
                _rd = (rows[0].get("report_date_as_yyyy_mm_dd") or "")
                _da = _days_old(_rd)
                print(
                    f"[COT] '{market_name}': latest report {_rd[:10]} ({_da} days old)",
                    file=sys.stderr,
                )
                if _da is not None and _da > STALE_DAYS:
                    if _da > 365:
                        # Permanently stale — cache with 48h TTL so subsequent calls
                        # within the same run (and next run) skip the re-fetch.
                        cache.set(key, rows)
                    else:
                        print(
                            f"[COT] ❌ COT data: fetch failed — positioning scores set to neutral "
                            f"(latest report {_rd[:10]} is {_da} days old — exceeds {STALE_DAYS}-day limit)",
                            file=sys.stderr,
                        )
                    # Don't log ❌ for permanently stale — _for_currency handles that message
                    return rows
                # Success: data is fresh
                week_end = _rd[:10]
                print(
                    f"[COT] Fresh fetch successful — data from week ending {week_end}",
                    file=sys.stderr,
                )
            _cot_consecutive_failures[0] = 0   # reset on success
            cache.set(key, rows)
            return rows
        except (NameError, TypeError, AttributeError) as _code_exc:
            # Code bug — no point retrying, fail immediately
            print(
                f"[COT] Code bug in CFTC fetch for '{market_name}': {_code_exc} — not retrying",
                file=sys.stderr,
            )
            _cot_consecutive_failures[0] += 1
            return []
        except Exception as _exc:  # noqa: BLE001
            _last_exc = _exc
            print(
                f"[COT] CFTC fetch failed (attempt {_attempt + 1} of 3): {_exc}",
                file=sys.stderr,
            )
    _cot_consecutive_failures[0] += 1
    print(f"[COT] ❌ COT data: fetch failed after 3 attempts for '{market_name}': {_last_exc}", file=sys.stderr)
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


def _cot_momentum(nets: list, range_span: float) -> dict:
    """Compute 3-week institutional positioning momentum.

    Compares the latest net speculator position (nets[0]) to 3 weeks ago
    (nets[3]).  Uses 5 % of the 52-week range span as the STABLE threshold —
    smaller moves are noise; larger moves show real conviction change.

    Returns
    -------
    momentum         BUILDING | STABLE | UNWINDING | REVERSING
    delta            change in net contracts (latest − 3_weeks_ago)
    delta_pct_range  abs(delta) as % of 52-week range span
    net_3w_ago       net position 3 weeks ago (for REVERSING context)
    """
    if len(nets) < 4 or range_span < 1_000:
        return {
            "momentum": "STABLE",
            "delta": 0,
            "delta_pct_range": 0.0,
            "net_3w_ago": int(nets[0]) if nets else 0,
        }

    current       = nets[0]
    three_w_ago   = nets[3]
    delta         = current - three_w_ago
    delta_pct     = abs(delta) / range_span * 100.0

    current_long  = current > 0
    prev_long     = three_w_ago > 0

    if current_long != prev_long and three_w_ago != 0 and current != 0:
        momentum = "REVERSING"
    elif delta_pct < 5.0:
        momentum = "STABLE"
    elif (current > 0 and delta > 0) or (current < 0 and delta < 0):
        momentum = "BUILDING"
    else:
        momentum = "UNWINDING"

    return {
        "momentum":        momentum,
        "delta":           int(delta),
        "delta_pct_range": round(delta_pct, 1),
        "net_3w_ago":      int(three_w_ago),
    }


def _for_currency(ccy: str) -> dict:
    meta = config.CURRENCIES.get(ccy, {})
    market = meta.get("cot_market")
    if not market:
        return {"currency": ccy, "status": "UNAVAILABLE", "reason": "no COT market mapped"}

    # Skip all remaining COT fetches after 3 consecutive failures this scan
    if _cot_consecutive_failures[0] >= 3:
        return {
            "currency": ccy,
            "status": "UNAVAILABLE",
            "reason": "COT disabled after 3 consecutive failures — neutral score applied",
        }

    rows = _series_for(market)
    if not rows:
        return {
            "currency": ccy,
            "status": "UNAVAILABLE",
            "reason": f"no COT rows for '{market}' (verify dataset id / market name)",
        }

    report_date = (rows[0].get("report_date_as_yyyy_mm_dd") or "")
    age = _days_old(report_date)

    # FIX 3 Step 1: Permanently stale data (>365d e.g. USD Dollar Index frozen 2022)
    # is not a failure — return neutral score so it does not trigger the failure counter.
    if age is not None and age > 365:
        print(
            f"[COT] {market}: permanently stale ({age}d) — "
            f"neutral score applied — not counted as failure",
            file=sys.stderr,
        )
        return {
            "currency": ccy,
            "status": "ok",
            "report_date": report_date[:10],
            "matched_market": market,
            "net_speculator_position": 0,
            "direction": "neutral",
            "percentile_in_range": 50.0,
            "extreme_flag": f"permanently stale ({age}d) — neutral score applied",
            "cot_momentum": "STABLE",
            "momentum_delta": 0,
            "momentum_delta_pct": 0.0,
            "net_3w_ago": 0,
        }

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

    momentum_data = _cot_momentum(nets, hi - lo)

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
        # ── momentum fields ──────────────────────────────────────────────────
        "cot_momentum":        momentum_data["momentum"],
        "momentum_delta":      momentum_data["delta"],
        "momentum_delta_pct":  momentum_data["delta_pct_range"],
        "net_3w_ago":          momentum_data["net_3w_ago"],
    }


def analyse(base: str, quote: str) -> dict:
    return {"status": "ok", "base": _for_currency(base), "quote": _for_currency(quote)}
