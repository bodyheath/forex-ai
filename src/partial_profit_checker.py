"""Partial profit taking system for open fund trades.

Three-stage exit strategy applied during every outcome-checker pass and
the lightweight between-scan price check:

  Stage 0  no milestone reached (default)
  Stage 1  50% of target reached:
             stop moved to breakeven + spread buffer;
             trade guaranteed to be profitable or flat.
  Stage 2  75% of target reached:
             50% of position recorded as closed at current price;
             partial profit permanently locked in.
  Stage 3  100% target or stop hit:
             remaining 50% closed by outcome_checker.
             If stop is at breakeven and price hits it → BREAKEVEN (not LOSS).

State is persisted in data/partial_profit_state.json between runs.
"""

import json
from datetime import datetime, timezone

import config
from src import telegram_alert

_STATE_FILE = config.PARTIAL_PROFIT_STATE_FILE

# Approximate spread added to/subtracted from entry for breakeven stop
_SPREAD_BUFFER = {
    "JPY": 0.030,       # ~3 pips in JPY-quoted pairs
    "DEFAULT": 0.00020, # ~2 pips in non-JPY pairs
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _pip_size(pair: str) -> float:
    quote = pair.split("/")[-1].upper() if "/" in pair else ""
    return 0.01 if quote == "JPY" else 0.0001


def _spread_buffer(pair: str) -> float:
    quote = pair.split("/")[-1].upper() if "/" in pair else ""
    return _SPREAD_BUFFER.get(quote, _SPREAD_BUFFER["DEFAULT"])


# ── Public helpers (used by outcome_checker) ─────────────────────────────────

def trade_progress(entry: float, target: float, price: float, direction: str) -> float:
    """Fraction of the entry→target distance covered by the current price.

    0.0  = at entry (no progress)
    1.0  = at target (full profit realised)
    >1.0 = price beyond target
    <0   = price has moved against us
    """
    if direction == "BUY":
        total = target - entry
        moved = price - entry
    else:
        total = entry - target
        moved = entry - price
    if total <= 0:
        return 0.0
    return moved / total


def breakeven_stop_price(entry: float, direction: str, pair: str) -> float:
    """Return the breakeven stop level: entry + spread buffer for BUY, minus for SELL."""
    buf = _spread_buffer(pair)
    return (entry + buf) if direction == "BUY" else (entry - buf)


def load_state() -> dict:
    """Load partial profit state JSON.  Returns {} on missing or corrupt file."""
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_state(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def effective_stop(trade_id: str, original_stop, state: dict):
    """Return the effective stop price.

    Once stage 1 is reached the stop is permanently at breakeven.
    outcome_checker passes this value to _determine_outcome() so the
    original stop is never used again after the breakeven migration.
    """
    ts = state.get(str(trade_id), {})
    if ts.get("stage", 0) >= 1:
        be = ts.get("breakeven_stop")
        if be is not None:
            return float(be)
    return _to_float(original_stop)


def blended_exit_price(trade_id: str, final_exit: float, state: dict) -> float:
    """Weighted-average exit price for a stage-2 trade being fully closed.

    50% was closed at partial_close_price; 50% closes at final_exit.
    The arithmetic mean gives the correct overall pips when passed as
    exit_price to tracker.update_outcome().
    """
    ts = state.get(str(trade_id), {})
    if ts.get("stage", 0) >= 2:
        partial = _to_float(ts.get("partial_close_price"))
        if partial is not None:
            return (partial + final_exit) / 2.0
    return final_exit


def get_stage(trade_id: str, state: dict) -> int:
    """Return the current stage (0–2) for a trade from loaded state."""
    return int(state.get(str(trade_id), {}).get("stage", 0))


# ── Main runner — called from outcome_checker during every scan ───────────────

def run(open_trades: list, price_cache: dict, log=print) -> None:
    """Check stage 1 and stage 2 milestones; send Telegram alerts; persist state.

    Idempotent: milestones already recorded in the state file are skipped.
    Called by outcome_checker.check_open_trades() BEFORE stop/target checking
    so that the effective breakeven stop is in effect immediately.
    """
    if not open_trades:
        return

    state        = load_state()
    state_changed = False

    for trade in open_trades:
        trade_id  = str(trade.get("id", ""))
        pair      = str(trade.get("pair") or "")
        direction = (trade.get("direction") or "").upper()
        entry     = _to_float(trade.get("entry"))
        target    = _to_float(trade.get("target"))
        stop_orig = _to_float(trade.get("stop_loss"))

        if not trade_id or direction not in ("BUY", "SELL"):
            continue
        if entry is None or target is None or stop_orig is None:
            continue

        price = price_cache.get(pair)
        if price is None:
            continue

        prog = trade_progress(entry, target, price, direction)

        ts = state.setdefault(trade_id, {
            "pair": pair, "stage": 0,
            "alerts_sent": {"stage1": False, "stage2": False, "pct80": False},
        })
        current_stage = ts.get("stage", 0)

        # ── Stage 1: 50% of target reached — move stop to breakeven ──────
        if prog >= 0.50 and current_stage < 1:
            be_stop = breakeven_stop_price(entry, direction, pair)
            ts.update({
                "stage": 1,
                "breakeven_stop": round(be_stop, 6),
                "stage1_reached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "stage1_price": round(price, 6),
            })
            state_changed = True
            log(f"  #{trade_id} {pair}: Stage 1 — stop moved to breakeven {be_stop:.5f}")
            if not ts["alerts_sent"].get("stage1"):
                _alert_stage1(trade, price, prog, be_stop)
                ts["alerts_sent"]["stage1"] = True

        # ── Stage 2: 75% of target reached — record partial close ─────────
        if prog >= 0.75 and ts.get("stage", 0) < 2:
            ps = _pip_size(pair)
            pips = ((price - entry) / ps) if direction == "BUY" else ((entry - price) / ps)
            be_stop = ts.get("breakeven_stop") or breakeven_stop_price(entry, direction, pair)
            ts.update({
                "stage": 2,
                "partial_close_price":     round(price, 6),
                "partial_close_pips":      round(pips, 1),
                "partial_close_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            state_changed = True
            log(f"  #{trade_id} {pair}: Stage 2 — 50% partially closed at {price} (+{pips:.1f} pips)")
            if not ts["alerts_sent"].get("stage2"):
                _alert_stage2(trade, price, pips, be_stop)
                ts["alerts_sent"]["stage2"] = True

    if state_changed:
        save_state(state)


# ── Between-scan 80% check — called from check_prices.py ─────────────────────

def check_80pct_milestone(open_trades: list, price_cache: dict, log=print) -> None:
    """Send a 🚨 alert when any open trade reaches 80% of its target (once only).

    Also runs stage 1/2 logic so milestones caught between scheduled scans
    are applied immediately rather than waiting for the next full scan.
    """
    if not open_trades:
        return

    # Stage 1/2 first — so state is up-to-date before 80% check
    run(open_trades, price_cache, log)

    state         = load_state()
    state_changed = False

    for trade in open_trades:
        trade_id  = str(trade.get("id", ""))
        pair      = str(trade.get("pair") or "")
        direction = (trade.get("direction") or "").upper()
        entry     = _to_float(trade.get("entry"))
        target    = _to_float(trade.get("target"))

        if not trade_id or direction not in ("BUY", "SELL"):
            continue
        if entry is None or target is None:
            continue

        price = price_cache.get(pair)
        if price is None:
            continue

        prog = trade_progress(entry, target, price, direction)
        if prog < 0.80:
            continue

        ts = state.setdefault(trade_id, {
            "pair": pair, "stage": 0,
            "alerts_sent": {"stage1": False, "stage2": False, "pct80": False},
        })
        if not ts["alerts_sent"].get("pct80"):
            _alert_80pct(trade, price, prog)
            ts["alerts_sent"]["pct80"] = True
            state_changed = True
            log(f"  #{trade_id} {pair}: 80% milestone alert sent")

    if state_changed:
        save_state(state)


# ── Telegram alert builders ───────────────────────────────────────────────────

def _alert_stage1(trade, price, progress, be_stop):
    pair      = trade.get("pair", "")
    direction = (trade.get("direction") or "").upper()
    pct       = round(progress * 100)
    msg = (
        f"✅ <b>{pair} trade at halfway point ({pct}% of target)</b>\n\n"
        f"Stop loss moved to breakeven — "
        f"this trade is now guaranteed profitable or breakeven.\n\n"
        f"Direction: {direction}  |  Current price: {price}\n"
        f"New stop: {be_stop:.5f} (breakeven + spread)\n\n"
        f"No action needed — the system has protected this trade."
    )
    try:
        telegram_alert.send(msg)
    except Exception:
        pass


def _alert_stage2(trade, price, pips, be_stop):
    pair      = trade.get("pair", "")
    direction = (trade.get("direction") or "").upper()
    entry     = _to_float(trade.get("entry"))
    target    = _to_float(trade.get("target"))
    ps        = _pip_size(pair)
    if entry and target and direction in ("BUY", "SELL"):
        pips_to_target = (
            (target - price) / ps if direction == "BUY" else (price - target) / ps
        )
        remaining = f"+{pips_to_target:.1f} pips remaining to full target\n"
    else:
        remaining = ""
    msg = (
        f"💰 <b>{pair} trade at 75% of target — taking partial profit</b>\n\n"
        f"50% of position closed at {price} (+{pips:.1f} pips locked in)\n"
        + remaining +
        f"\nRemainder running to full target with stop at breakeven ({be_stop:.5f}).\n\n"
        f"Worst case: second half closes at entry (breakeven).\n"
        f"First half profit is already locked in — no loss possible."
    )
    try:
        telegram_alert.send(msg)
    except Exception:
        pass


def _alert_80pct(trade, price, progress):
    pair      = trade.get("pair", "")
    direction = (trade.get("direction") or "").upper()
    pct       = round(progress * 100)
    entry     = _to_float(trade.get("entry"))
    target    = _to_float(trade.get("target"))
    ps        = _pip_size(pair)
    if entry and target and direction in ("BUY", "SELL"):
        if direction == "BUY":
            pips_so_far    = (price - entry) / ps
            pips_to_target = (target - price) / ps
        else:
            pips_so_far    = (entry - price) / ps
            pips_to_target = (price - target) / ps
        pip_detail = (
            f"+{pips_so_far:.1f} pips captured so far  |  "
            f"{pips_to_target:.1f} pips to full target\n"
        )
    else:
        pip_detail = ""
    msg = (
        f"🚨 <b>{pair} approaching target — now at {pct}%</b>\n\n"
        f"Direction: {direction}  |  Current price: {price}\n"
        + pip_detail +
        f"\nConsider closing manually for a near-full profit "
        f"rather than waiting for the next scheduled scan."
    )
    try:
        telegram_alert.send(msg)
    except Exception:
        pass
