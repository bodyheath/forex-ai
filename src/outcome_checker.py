"""Automatic daily outcome detection for open trade recommendations.

Fetches the live price for every OPEN trade from Twelve Data, then closes any
trade that has hit its target (WIN), stop loss (LOSS), or been open for 5+
days (EXPIRED).  Returns the list of closed trade row dicts so the caller can
immediately run win/loss analysis on them.
"""

import time
from datetime import datetime

import requests

import config
from src import tracker

_PRICE_URL   = "https://api.twelvedata.com/price"
_EXPIRY_DAYS = 5    # fallback; actual expiry is computed from R:R
_FETCH_DELAY = 10   # seconds between price calls; free tier = 8 req/min


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _compute_expiry_days(row: dict) -> int:
    """Dynamic expiry from R:R: max(4, round(rr * 1.5) + 1).

    Stop ≈ 1x ATR ≈ ADR, so rr ≈ target_pips / adr_pips.
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
        return max(4, round(rr * 1.5) + 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return _EXPIRY_DAYS


def _fetch_live_price(pair: str) -> float | None:
    """Call Twelve Data /price (no cache — must be fresh for outcome checking)."""
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
                       entry, stop, target, opened_at: str) -> str | None:
    """Return 'WIN', 'LOSS', 'EXPIRED', or None (trade still open)."""
    # 14-day expiry takes priority — regardless of price level.
    try:
        opened = datetime.strptime(opened_at[:19], "%Y-%m-%d %H:%M:%S")
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


def check_open_trades(log=print) -> list:
    """Check all OPEN trades; close any that hit target/stop/expiry.

    Returns a list of updated trade row dicts for each trade that was closed
    this run.  Fully fault-tolerant: errors on individual trades are swallowed
    and logged so the rest of the daily run is never blocked.
    """
    if not config.TWELVE_DATA_KEY:
        log("Outcome check: TWELVE_DATA_KEY not set — skipping.")
        return []

    rows = tracker.load()
    open_trades = [r for r in rows if r.get("status") == "OPEN"
                   and r.get("trade_this") == "YES"]

    if not open_trades:
        log("Outcome check: no open YES-trades to monitor.")
        return []

    log(f"Outcome check: monitoring {len(open_trades)} open trade(s).")
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
                log(f"  #{rec_id} {pair}: price unavailable, skipping.")
                continue

            outcome = _determine_outcome(
                direction, price,
                row.get("entry"), row.get("stop_loss"), row.get("target"),
                row.get("timestamp", ""),
            )
            if outcome is None:
                continue  # still open

            updated = tracker.update_outcome(
                rec_id, outcome,
                exit_price=price,
                notes=f"Auto-closed by outcome checker: {outcome} at {price}",
            )
            r_txt = f", R={updated.get('r_multiple')}, pips={updated.get('pips')}"
            log(f"  #{rec_id} {pair} {direction}: {outcome} at {price}{r_txt}")
            closed.append(updated)

        except Exception as exc:
            log(f"  #{rec_id} {pair}: outcome check error — {exc}")

    wins    = sum(1 for r in closed if r.get("status") == "WIN")
    losses  = sum(1 for r in closed if r.get("status") == "LOSS")
    expired = sum(1 for r in closed if r.get("status") == "EXPIRED")
    if closed:
        log(f"Outcome check complete: {wins} WIN, {losses} LOSS, {expired} EXPIRED.")
    else:
        log("Outcome check: no trades hit target/stop today.")
    return closed
