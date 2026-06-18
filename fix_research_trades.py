"""One-time script: retroactively correct close_price / pips / net_pips /
r_multiple for research trades whose stop or target was hit.

The bug: research_outcome_checker recorded the LIVE market price at the
moment of the periodic scan check as close_price, rather than the stop/target
level.  For slow-moving periodic checks this caused close prices far past the
stop, inflating pip losses (e.g. CAD/HKD shows -444 pips instead of -180).

Fix: for exit_reason=STOP_HIT use stop_loss as close_price;
     for exit_reason=TARGET_HIT use target as close_price.
"""

import csv
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))
import config
from src import research_tracker, trade_costs as _tc

CSV = config.DATA_DIR / "research_trades.csv"


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pip_size(pair: str) -> float:
    cleaned = pair.upper().replace("/", "").replace("-", "")
    if len(cleaned) >= 6:
        base  = cleaned[:3]
        quote = cleaned[3:6]
        if quote == "JPY":
            return 0.01
        if base == "JPY":
            return 0.000001
    elif "JPY" in pair.upper():
        return 0.01
    return 0.0001


def recalculate(row: dict, correct_close: float):
    """Return (pips, net_pips, r_multiple) using the corrected close price."""
    entry     = _to_float(row.get("entry"))
    stop      = _to_float(row.get("stop_loss"))
    direction = (row.get("direction") or "").upper()
    pair      = row.get("pair", "")
    lots      = _to_float(row.get("lots")) or research_tracker.RESEARCH_LOTS

    if None in (entry, stop) or direction not in ("BUY", "SELL"):
        return None, None, None

    risk = abs(entry - stop)
    if risk == 0:
        return None, None, None

    profit     = (correct_close - entry) if direction == "BUY" else (entry - correct_close)
    r_multiple = round(profit / risk, 2)
    pips       = round(profit / _pip_size(pair), 1)

    # Net pips after spread/swap
    net_pips = pips
    try:
        from datetime import datetime
        opened    = datetime.strptime(row.get("date", "")[:10], "%Y-%m-%d")
        days_held = max(0.0, (datetime.now() - opened).total_seconds() / 86400)
        net_pips  = _tc.net_pips_for_closed_trade(
            pair, direction, entry, float(pips), days_held
        )
    except Exception:
        pass

    return pips, net_pips, r_multiple


def main():
    rows = research_tracker.load()
    corrected = 0
    total_pip_delta = 0.0

    print(f"Scanning {len(rows)} research trades for close_price corrections ...\n")
    print(f"{'ID':>4}  {'Pair':<10}  {'Dir':<4}  {'Reason':<12}  "
          f"{'Old close':>10}  {'New close':>10}  {'Old pips':>9}  {'New pips':>9}  Delta")
    print("-" * 95)

    for row in rows:
        status    = row.get("status", "")
        reason    = row.get("exit_reason", "")
        pair      = row.get("pair", "")
        direction = (row.get("direction") or "").upper()
        entry     = _to_float(row.get("entry"))
        stop      = _to_float(row.get("stop_loss"))
        target    = _to_float(row.get("target"))
        old_close = _to_float(row.get("close_price"))

        # Determine the correct close price:
        # - Explicit exit_reason takes precedence
        # - For older trades without exit_reason, infer from status + price position
        if reason == "STOP_HIT" or reason == "TARGET_HIT":
            correct_close = stop if reason == "STOP_HIT" else target
        elif status == "WIN" and target and old_close and direction == "BUY" and old_close > target:
            correct_close = target    # price went beyond target; close at target
        elif status == "WIN" and target and old_close and direction == "SELL" and old_close < target:
            correct_close = target
        elif status == "LOSS" and stop and old_close and direction == "BUY" and old_close < stop:
            correct_close = stop      # price went beyond stop; close at stop
        elif status == "LOSS" and stop and old_close and direction == "SELL" and old_close > stop:
            correct_close = stop
        else:
            continue

        if correct_close is None or entry is None:
            continue

        old_close = _to_float(row.get("close_price"))
        old_pips  = _to_float(row.get("pips"))

        # Check whether close_price is already at the stop/target
        # (tolerance: 1 pip).  If already correct, skip.
        ps = _pip_size(pair)
        if old_close is not None and abs(old_close - correct_close) / ps < 1.0:
            continue

        new_pips, new_net_pips, new_r = recalculate(row, correct_close)
        if new_pips is None:
            continue

        delta = (new_pips or 0) - (old_pips or 0)
        print(f"{row.get('id'):>4}  {pair:<10}  {direction:<4}  {reason:<12}  "
              f"{old_close or 0:>10.5f}  {correct_close:>10.5f}  "
              f"{old_pips or 0:>9.1f}  {new_pips:>9.1f}  {delta:+.1f}")

        # Apply correction
        row["close_price"] = correct_close
        row["pips"]        = new_pips
        row["net_pips"]    = new_net_pips
        row["r_multiple"]  = new_r
        corrected          += 1
        total_pip_delta    += delta

    print("-" * 95)
    print(f"\nCorrected {corrected} trades  |  Total pip delta: {total_pip_delta:+.1f}")

    if corrected == 0:
        print("Nothing to do.")
        return

    # Write back
    with CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=research_tracker.FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in research_tracker.FIELDS})

    print("\nCSV written successfully.")

    # Print revised profit factor
    closed = [r for r in rows if r.get("status") in ("WIN", "LOSS", "PARTIAL_WIN", "EXPIRED")]
    won    = [r for r in closed if r.get("status") in ("WIN", "PARTIAL_WIN")]
    lost   = [r for r in closed if r.get("status") in ("LOSS",)]
    win_pips  = sum(_to_float(r.get("pips")) or 0 for r in won if _to_float(r.get("pips")) is not None)
    loss_pips = abs(sum(_to_float(r.get("pips")) or 0 for r in lost if _to_float(r.get("pips")) is not None))
    pf = win_pips / loss_pips if loss_pips > 0 else float("inf")

    decisive = [r for r in closed if r.get("status") in ("WIN", "LOSS")]
    wins_d   = sum(1 for r in decisive if r.get("status") == "WIN")
    losses_d = sum(1 for r in decisive if r.get("status") == "LOSS")
    print(f"\nRevised stats (decisive only — WIN/LOSS):")
    print(f"  Decisive trades: {len(decisive)}  |  Wins: {wins_d}  |  Losses: {losses_d}")
    print(f"  Win pips  (all closed): {win_pips:.1f}")
    print(f"  Loss pips (all closed): {loss_pips:.1f}")
    print(f"  Profit factor: {pf:.2f}")


if __name__ == "__main__":
    main()
