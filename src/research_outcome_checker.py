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
_EXPIRY_DAYS = 14
_FETCH_DELAY = 10  # seconds between calls; free tier = 8 req/min


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
                       entry, stop, target, opened_at: str):
    try:
        opened = datetime.strptime(opened_at[:10], "%Y-%m-%d")
        if (datetime.now() - opened).days >= _EXPIRY_DAYS:
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


def check_open_research_trades(log=print) -> list:
    """Check all OPEN research trades; close any that hit target/stop/expiry.

    Returns list of closed trade row dicts.  Fully fault-tolerant.
    """
    if not config.TWELVE_DATA_KEY:
        log("Research outcome check: TWELVE_DATA_KEY not set — skipping.")
        return []

    rows       = research_tracker.load()
    open_trades = [r for r in rows if r.get("status") == "OPEN"]

    if not open_trades:
        log("Research outcome check: no open research trades to monitor.")
        return []

    log(f"Research outcome check: monitoring {len(open_trades)} open research trade(s).")
    closed = []

    for i, row in enumerate(open_trades):
        if i > 0:
            time.sleep(_FETCH_DELAY)

        rec_id    = int(row.get("id", 0))
        pair      = row.get("pair", "")
        direction = (row.get("direction") or "").upper()

        try:
            price = _fetch_live_price(pair)
            if price is None:
                log(f"  Research #{rec_id} {pair}: price unavailable, skipping.")
                continue

            outcome = _determine_outcome(
                direction, price,
                row.get("entry"), row.get("stop_loss"), row.get("target"),
                row.get("date", ""),
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
    if closed:
        log(f"Research outcome check complete: {wins} WIN, {losses} LOSS, {expired} EXPIRED.")
    else:
        log("Research outcome check: no research trades hit target/stop today.")
    return closed
