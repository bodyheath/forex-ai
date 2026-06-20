"""Automatic daily outcome detection for open trade recommendations.

Fetches the live price for every OPEN trade from Twelve Data, then:
  1. Runs partial_profit_checker — moves stops to breakeven at 50%, records
     partial closes at 75%, and sends Telegram alerts for each milestone.
  2. Closes any trade that has hit its target (WIN), effective stop (LOSS or
     BREAKEVEN), or been open for 5+ days (EXPIRED).

When a trade has been partially closed (stage 2), the final exit uses the
blended average of the partial close price and the current price so the
recorded pips accurately reflect the two-tranche exit.

Returns the list of closed trade row dicts so the caller can immediately
run win/loss analysis on them.
"""

import time
from datetime import datetime

import requests

import config
from src import tracker
from src import cascade as _casc

_PRICE_URL   = "https://api.twelvedata.com/price"
_EXPIRY_DAYS = 5    # fallback; actual expiry is computed from R:R
_FETCH_DELAY = 10   # seconds between price calls; free tier = 8 req/min


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _compute_expiry_days(row: dict) -> int:
    """Dynamic expiry from R:R: max(4, round(rr * 1.5) + 1)."""
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
                       entry, stop, target, opened_at: str,
                       expiry_days: int = None,
                       breakeven_protected: bool = False) -> str | None:
    """Return 'WIN', 'LOSS', 'BREAKEVEN', 'EXPIRED', or None (still open).

    breakeven_protected=True means the stop has been moved to the breakeven
    level by partial_profit_checker.  When that stop is hit, the outcome is
    BREAKEVEN (not LOSS) — the trade closes at entry price, no loss recorded.
    """
    expiry = expiry_days if expiry_days is not None else _EXPIRY_DAYS
    try:
        opened = datetime.strptime(opened_at[:19], "%Y-%m-%d %H:%M:%S")
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
            return "BREAKEVEN" if breakeven_protected else "LOSS"
    else:
        if price <= target:
            return "WIN"
        if price >= stop:
            return "BREAKEVEN" if breakeven_protected else "LOSS"
    return None


def check_open_trades(log=print, price_cache: dict | None = None) -> list:
    """Check all OPEN trades; close any that hit target/stop/expiry.

    Workflow:
      1. For each open trade, fetch current price (cache or live API).
      2. Run partial_profit_checker.run() — handles stage 1 (breakeven stop
         migration) and stage 2 (50% partial close recording + alert).
      3. Determine outcome using the effective stop (breakeven if stage 1+).
      4. For stage-2 trades, blend the partial close price with the final
         exit price so recorded pips reflect both tranches.
      5. Write the outcome to tracker (trades.csv).

    Returns a list of updated trade row dicts for each trade closed this run.
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
    _last_api_t = 0.0

    # ── Step 1: build price cache for all open trades ─────────────────────
    full_cache = dict(price_cache or {})
    for row in open_trades:
        pair = row.get("pair", "")
        if pair not in full_cache:
            _elapsed = time.time() - _last_api_t
            if _last_api_t > 0 and _elapsed < _FETCH_DELAY:
                time.sleep(_FETCH_DELAY - _elapsed)
            price = _fetch_live_price(pair)
            _last_api_t = time.time()
            if price is not None:
                full_cache[pair] = price

    # ── Step 2: partial profit milestones (stage 1 / stage 2) ────────────
    try:
        from src import partial_profit_checker as _ppc
        _ppc.run(open_trades, full_cache, log)
        _pp_state = _ppc.load_state()
    except Exception as exc:
        log(f"  Partial profit checker error — {exc}")
        _ppc = None
        _pp_state = {}

    # ── Step 3: outcome determination + close ─────────────────────────────
    for row in open_trades:
        rec_id    = int(row.get("id", 0))
        pair      = row.get("pair", "")
        direction = (row.get("direction") or "").upper()

        try:
            price = full_cache.get(pair)
            if price is None:
                log(f"  #{rec_id} {pair}: price unavailable, skipping.")
                continue

            # Effective stop: breakeven if stage 1 was reached
            _eff_stop = row.get("stop_loss")
            _pp_stage = 0
            if _ppc is not None:
                _pp_stage = _ppc.get_stage(str(rec_id), _pp_state)
                _eff_stop = _ppc.effective_stop(str(rec_id), row.get("stop_loss"), _pp_state)

            _bp_protected = _pp_stage >= 1

            outcome = _determine_outcome(
                direction, price,
                row.get("entry"), _eff_stop, row.get("target"),
                row.get("timestamp", ""),
                expiry_days=_compute_expiry_days(row),
                breakeven_protected=_bp_protected,
            )
            if outcome is None:
                continue  # still open

            # Stage 2 + breakeven stop hit → partial profit already locked, this is WIN
            if outcome == "BREAKEVEN" and _pp_stage >= 2:
                outcome = "WIN"

            # Blended exit price for stage-2 trades
            _final_exit = price
            _partial_note = ""
            if _ppc is not None and _pp_stage >= 2:
                _final_exit = _ppc.blended_exit_price(str(rec_id), price, _pp_state)
                _partial_price = _pp_state.get(str(rec_id), {}).get("partial_close_price", "")
                _partial_pips  = _pp_state.get(str(rec_id), {}).get("partial_close_pips", 0)
                if _partial_price:
                    _partial_note = (
                        f" | Partial close: 50% at {_partial_price} "
                        f"(+{_partial_pips:.1f}p), 50% at {price} "
                        f"(blended {_final_exit:.5f})"
                    )

            _notes = f"Auto-closed: {outcome} at {price}{_partial_note}"
            updated = tracker.update_outcome(
                rec_id, outcome,
                exit_price=_final_exit,
                notes=_notes,
            )
            r_txt = f", R={updated.get('r_multiple')}, pips={updated.get('pips')}"
            log(f"  #{rec_id} {pair} {direction}: {outcome} at {price}{r_txt}")
            closed.append(updated)

        except Exception as exc:
            log(f"  #{rec_id} {pair}: outcome check error — {exc}")

    wins    = sum(1 for r in closed if r.get("status") == "WIN")
    losses  = sum(1 for r in closed if r.get("status") == "LOSS")
    beven   = sum(1 for r in closed if r.get("status") == "BREAKEVEN")
    expired = sum(1 for r in closed if r.get("status") == "EXPIRED")
    if closed:
        log(f"Outcome check complete: {wins} WIN, {losses} LOSS, "
            f"{beven} BREAKEVEN, {expired} EXPIRED.")
    else:
        log("Outcome check: no trades hit target/stop today.")
    return closed
