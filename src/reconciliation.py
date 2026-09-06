"""Balance reconciliation -- automatic tracked-vs-recomputed drift detection.

Built directly from this engagement's own biggest recurring finding: every
tracked-balance bug found this session (the peak-balance ratchet drift that
drove 3 days of phantom "caution" sizing; the gross-vs-net-pips mismatches;
the fund_total_trades drift -- cached 194 vs a fresh recompute's real 197 --
that originally motivated fund_state.reconcile_from_trades()) was caught by
someone happening to look, not by anything standing watch. This module is
that standing watch.

Runs UNCONDITIONALLY on monitor.py's existing 30-minute cadence -- not
gated on "did a fund trade close this run" the way
fund_state.reconcile_from_trades() already is (see monitor.py's own
comment there: that function only runs `if fund_closed`, so fund_state.json
can go long stretches -- confirmed empirically this session, real decisive
fund trades can go days between closes -- without ever being checked
against a fresh recompute at all). This module closes that gap: every
single monitor run gets one reconciliation check, closed trades or not.

============================================================================
TWO LEGS TODAY (paper-trading, no broker involved yet)
============================================================================
  TRACKED    -- fund_state.json's own incrementally-updated `balance`
                (src/fund_state.py::load()).
  RECOMPUTED -- src/trading/financials.py::calculate_fund_state(), built
                fresh from the full trade history, independently of
                whatever code path wrote the tracked figure. This is
                exactly the comparison that caught this session's own real
                bugs when done by hand -- formalized here as an automatic,
                permanent, alerting check instead of an occasional manual
                one.

Both are realized-balance-only by construction (neither includes
unrealised P&L on open positions -- fund_state.json tracks that
separately as `unrealised_dollars`/`total_equity`, and
calculate_fund_state()'s `balance` is accumulated only from CLOSED
trades' net-of-cost pips), so no separate "exclude open positions" step is
needed -- comparing the two `balance` fields directly is already
apples-to-apples.

============================================================================
THIRD LEG -- STUBBED, NOT IMPLEMENTED (Phase 08)
============================================================================
get_real_broker_balance() below calls src.broker.get_live_balance(), which
already returns None whenever LIVE_TRADING is False (i.e., always, today --
see src/broker.py's own docstring). check_balance_reconciliation() already
checks for a non-None real balance and folds it into the same tolerance/
alert/log machinery when present -- meaning the moment Phase 08 implements
get_live_balance() for real, this module becomes a genuine three-leg check
with ZERO changes needed here. Do not implement the broker side ahead of
real broker integration -- that's the whole point of leaving it a plug-in.
"""
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path("data/balance_reconciliation_log.csv")
LOG_FIELDS = [
    "timestamp", "leg_a", "leg_b", "leg_a_name", "leg_b_name",
    "diff", "diff_pct", "tolerance", "status", "notes",
]

# Tolerance = greater of a small fixed dollar floor or a basis-point
# fraction of balance. At the current ~$10,000 paper balance, 5bps = $5 --
# comfortably wider than plausible cumulative cent-rounding error across a
# few hundred realized trades, but far tighter than the real historical
# incidents this module exists to catch (the peak-balance ratchet drift
# was large enough to change sizing tier for 3 days, not a few dollars).
TOLERANCE_FIXED_USD = 1.00
TOLERANCE_BPS = 5  # 0.05%


def _tolerance(reference_balance: float) -> float:
    return max(TOLERANCE_FIXED_USD, abs(reference_balance) * TOLERANCE_BPS / 10_000)


def get_real_broker_balance():
    """Third leg, stubbed -- see module docstring. Returns None today
    (LIVE_TRADING=False always, pre-Phase-08); becomes real the moment
    broker.get_live_balance() is implemented, with no caller changes."""
    try:
        from src import broker
        return broker.get_live_balance()
    except Exception:
        return None


def _append_log(timestamp: str, leg_a, leg_b, leg_a_name: str, leg_b_name: str,
                 diff, diff_pct, tolerance, status: str, notes: str) -> None:
    """Append one row to the permanent reconciliation log -- every check's
    result, match or mismatch, never just alert-on-failure. Never raises;
    a logging failure is printed loudly (it's its own kind of silent-
    failure risk) but must not crash the check itself."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        is_new = not LOG_PATH.exists()
        with LOG_PATH.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if is_new:
                writer.writeheader()
            writer.writerow({
                "timestamp": timestamp, "leg_a": leg_a, "leg_b": leg_b,
                "leg_a_name": leg_a_name, "leg_b_name": leg_b_name,
                "diff": diff, "diff_pct": diff_pct, "tolerance": tolerance,
                "status": status, "notes": notes,
            })
    except Exception as exc:
        print(f"[reconciliation] FAILED TO WRITE PERMANENT LOG: {exc}", file=sys.stderr)


def _alert_critical(title: str, message: str, log) -> None:
    """Through the existing critical-severity channels (Discord + Telegram)
    -- mirrors the ghost-trade/fund-loop-exception alert pattern already
    established in this codebase (src/discord_notifier.py). Never raises."""
    try:
        from src import discord_notifier as _dn
        _dn.send_balance_drift_alert(title, message)
    except Exception as exc:
        log(f"[reconciliation] Discord alert failed: {exc}")
    try:
        from src import telegram_alert as _ta
        _ta.send(f"{title}\n\n{message}")
    except Exception as exc:
        log(f"[reconciliation] Telegram alert failed: {exc}")


def _compare(now: str, a: float, b: float, name_a: str, name_b: str, log) -> dict:
    diff = round(a - b, 2)
    tol = round(_tolerance(a), 2)
    within = abs(diff) <= tol
    diff_pct = round(diff / b * 100, 4) if b else 0.0
    status = "OK" if within else "MISMATCH"

    _append_log(now, a, b, name_a, name_b, diff, diff_pct, tol, status, "")

    if within:
        log(f"[reconciliation] OK: {name_a}=${a:,.2f} {name_b}=${b:,.2f} "
            f"diff=${diff:+,.2f} (within ${tol:,.2f} tolerance)")
    else:
        log(f"[reconciliation] MISMATCH: {name_a}=${a:,.2f} {name_b}=${b:,.2f} "
            f"diff=${diff:+,.2f} (tolerance=${tol:,.2f})")
        _alert_critical(
            "\U0001f6a8 BALANCE RECONCILIATION MISMATCH",
            f"{name_a}: ${a:,.2f}\n{name_b}: ${b:,.2f}\n"
            f"Difference: ${diff:+,.2f} ({diff_pct:+.3f}%)\n"
            f"Tolerance: ${tol:,.2f} (greater of ${TOLERANCE_FIXED_USD:.2f} "
            f"or {TOLERANCE_BPS}bps of balance)",
            log,
        )
    return {"status": status, name_a: a, name_b: b, "diff": diff, "tolerance": tol}


def check_balance_reconciliation(log=print) -> dict:
    """Run one full reconciliation pass. Always appends at least one row to
    the permanent log, regardless of outcome. Alerts through the existing
    critical-severity channel on any mismatch beyond tolerance, OR on the
    check itself failing to run at all -- fail CLOSED, mirroring the
    circuit-breaker's own "unreadable state defaults to blocked, not to
    safe" convention (daily.py) rather than silently skipping.

    Returns a summary dict; never raises.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        from src import fund_state as _fs
        from src.trading import financials as _fin
        tracked_state = _fs.load()
        tracked = float(tracked_state.get("balance") or 0)
        recomputed_state = _fin.calculate_fund_state()
        if recomputed_state.get("error"):
            raise RuntimeError(f"calculate_fund_state() error: {recomputed_state['error']}")
        recomputed = float(recomputed_state.get("balance") or 0)
    except Exception as exc:
        _append_log(now, None, None, "tracked", "recomputed", None, None, None,
                    "CHECK_FAILED", str(exc))
        log(f"[reconciliation] CHECK FAILED (fail-closed, alerting): {exc}")
        _alert_critical(
            "\U0001f6a8 BALANCE RECONCILIATION CHECK FAILED",
            f"Could not run the reconciliation check itself -- treating this "
            f"as a real problem, not a skip: {exc}",
            log,
        )
        return {"status": "CHECK_FAILED", "error": str(exc)}

    result = _compare(now, tracked, recomputed, "tracked", "recomputed", log)

    real_balance = get_real_broker_balance()
    if real_balance is not None:
        # Third leg has come online (Phase 08) -- fold it in automatically,
        # no redesign needed. Compares against tracked (the figure the
        # live system actually acts on).
        real_result = _compare(now, tracked, float(real_balance),
                                "tracked", "real_broker", log)
        result["real_broker_check"] = real_result

    return result
