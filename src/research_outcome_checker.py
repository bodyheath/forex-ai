"""Automatic outcome detection for open research trades.

Mirrors outcome_checker.py but operates on research_trades.csv rather than
trades.csv.  Only processes trades with status=OPEN (i.e., those that had
entry/stop/target from Sonnet analysis).  NO_PRICE_LEVELS trades are skipped.
14-day expiry rule is identical to the real trade checker.
"""

import time
from datetime import datetime

import requests

import config
from src import research_tracker

_PRICE_URL   = "https://api.twelvedata.com/price"
_EXPIRY_DAYS = 7    # fallback; actual expiry is computed from R:R
_FETCH_DELAY = 10   # seconds between calls; free tier = 8 req/min


def _compute_expiry_days(row: dict) -> int:
    """Dynamic expiry from R:R: max(7, round(rr * 2)).

    Stop ≈ 1x ATR ≈ ADR, so rr ≈ target_pips / adr_pips.
    For a 2:1 R:R this gives 7 days; for 3:1 gives 7; for 4:1 gives 8.
    Falls back to _EXPIRY_DAYS if levels are missing.
    """
    try:
        entry  = float(row.get("entry")     or 0)
        stop   = float(row.get("stop_loss") or 0)
        target = float(row.get("target")    or 0)
        sd = abs(entry - stop)
        td = abs(entry - target)
        if sd <= 0:
            return _EXPIRY_DAYS
        rr = td / sd
        return max(7, round(rr * 2))
    except (TypeError, ValueError, ZeroDivisionError):
        return _EXPIRY_DAYS


def _is_partial_win(direction: str, price: float, entry, stop, target) -> bool:
    """Return True if price is >=50% of the way to target AND gross pips positive.

    Used to reclassify EXPIRED trades that were moving well but ran out of time.
    """
    e = _to_float(entry)
    t = _to_float(target)
    if None in (e, t, price) or direction not in ("BUY", "SELL"):
        return False
    reward = abs(t - e)
    if reward <= 0:
        return False
    if direction == "BUY":
        gross_pips = price - e
        pct_to_target = (price - e) / reward
    else:
        gross_pips = e - price
        pct_to_target = (e - price) / reward
    return gross_pips > 0 and pct_to_target >= 0.5


def _reclassify_expired(rows: list, log) -> int:
    """Promote EXPIRED rows to PARTIAL_WIN if they meet the criteria.

    Runs once per daily check against all historical EXPIRED trades so that
    the new classification is retroactively applied.  Returns number reclassified.
    """
    count = 0
    for row in rows:
        if row.get("status") != "EXPIRED":
            continue
        cp = _to_float(row.get("close_price"))
        if cp is None:
            continue
        direction = (row.get("direction") or "").upper()
        if _is_partial_win(direction, cp, row.get("entry"), row.get("stop_loss"), row.get("target")):
            try:
                research_tracker.update_outcome(int(row.get("id", 0)), "PARTIAL_WIN", close_price=cp)
                log(f"  Reclassified #{row.get('id')} {row.get('pair')} EXPIRED → PARTIAL_WIN")
                count += 1
            except Exception as exc:
                log(f"  Reclassify error #{row.get('id')}: {exc}")
    return count


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _fetch_live_price(pair: str):
    try:
        resp = requests.get(
            _PRICE_URL,
            params={"symbol": pair, "apikey": config.TWELVE_DATA_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "error":
            return None
        return float(data["price"])
    except Exception:
        return None


def _determine_outcome(direction: str, price: float,
                       entry, stop, target, opened_at: str,
                       expiry_days: int = None):
    expiry = expiry_days if expiry_days is not None else _EXPIRY_DAYS
    try:
        opened = datetime.strptime(opened_at[:10], "%Y-%m-%d")
        if (datetime.now() - opened).days >= expiry:
            return "EXPIRED"
    except (ValueError, TypeError):
        pass

    entry  = _to_float(entry)
    stop   = _to_float(stop)
    target = _to_float(target)

    if direction not in ("BUY", "SELL") or None in (entry, stop, target, price):
        return None

    if direction == "BUY":
        if price >= target:
            return "WIN"
        if price <= stop:
            return "LOSS"
    else:
        if price <= target:
            return "WIN"
        if price >= stop:
            return "LOSS"
    return None


def check_open_research_trades(log=print, price_cache: dict | None = None) -> list:
    """Check all OPEN research trades; close any that hit target/stop/expiry.

    ``price_cache`` is an optional {pair: float} dict pre-fetched by
    price_fetcher.fetch_prices_for_open_trades().  When a pair's price is
    present in the cache the direct API call and rate-limit sleep are both
    skipped, allowing ALL open research trades to be checked regardless of
    whether they appeared in today's scan.  Falls back to the live /price
    endpoint for any cache misses.

    Returns list of closed trade row dicts.  Fully fault-tolerant.
    """
    if not config.TWELVE_DATA_KEY:
        log("Research outcome check: TWELVE_DATA_KEY not set — skipping.")
        return []

    rows        = research_tracker.load()
    open_trades = [r for r in rows if r.get("status") == "OPEN"]
    no_levels   = [r for r in rows if r.get("status") == "NO_PRICE_LEVELS"]
    closed_hist = [r for r in rows if r.get("status") in research_tracker.OUTCOME_STATUSES]

    # ── Audit log — visible in every GitHub Actions run ──────────────────────
    wins_hist    = sum(1 for r in closed_hist if r.get("status") == "WIN")
    losses_hist  = sum(1 for r in closed_hist if r.get("status") == "LOSS")
    expired_hist = sum(1 for r in closed_hist if r.get("status") == "EXPIRED")
    log(
        f"Research outcome audit: {len(rows)} total trades — "
        f"{len(open_trades)} OPEN · {len(no_levels)} NO_PRICE_LEVELS · "
        f"{wins_hist} WIN · {losses_hist} LOSS · {expired_hist} EXPIRED"
    )

    if not open_trades:
        log("Research outcome check: no open research trades to monitor.")
        return []

    n_cached  = sum(1 for r in open_trades if price_cache and r.get("pair") in price_cache)
    n_api     = len(open_trades) - n_cached
    log(
        f"Research outcome check: monitoring {len(open_trades)} open research trade(s) — "
        f"{n_cached} from price cache · {n_api} via direct API."
    )
    closed = []
    _last_api_t = 0.0  # timestamp of the last direct API call (for rate limiting)

    for row in open_trades:
        rec_id    = int(row.get("id", 0))
        pair      = row.get("pair", "")
        direction = (row.get("direction") or "").upper()

        try:
            if price_cache and pair in price_cache:
                price = price_cache[pair]
            else:
                # Rate-limit guard: ensure ≥ _FETCH_DELAY between live API calls
                _elapsed = time.time() - _last_api_t
                if _last_api_t > 0 and _elapsed < _FETCH_DELAY:
                    time.sleep(_FETCH_DELAY - _elapsed)
                price = _fetch_live_price(pair)
                _last_api_t = time.time()
            if price is None:
                log(f"  Research #{rec_id} {pair}: price unavailable, skipping.")
                continue

            outcome = _determine_outcome(
                direction, price,
                row.get("entry"), row.get("stop_loss"), row.get("target"),
                row.get("date", ""),
                expiry_days=_compute_expiry_days(row),
            )
            if outcome is None:
                continue

            updated = research_tracker.update_outcome(rec_id, outcome, close_price=price)
            r_txt   = f", R={updated.get('r_multiple')}, pips={updated.get('pips')}"
            log(f"  Research #{rec_id} {pair} {direction}: {outcome} at {price}{r_txt}")
            closed.append(updated)

        except Exception as exc:
            log(f"  Research #{rec_id} {pair}: outcome check error — {exc}")

    wins    = sum(1 for r in closed if r.get("status") == "WIN")
    losses  = sum(1 for r in closed if r.get("status") == "LOSS")
    expired = sum(1 for r in closed if r.get("status") == "EXPIRED")
    still_open = len(open_trades) - len(closed)
    if closed:
        log(
            f"Research outcome check complete: closed {len(closed)} trade(s) this run — "
            f"{wins} WIN · {losses} LOSS · {expired} EXPIRED · "
            f"{still_open} still open."
        )
    else:
        log(
            f"Research outcome check: no trades hit target/stop/expiry — "
            f"{len(open_trades)} still open."
        )
    return closed
