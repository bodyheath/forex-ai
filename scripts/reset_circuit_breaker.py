"""Manual override for the fund's consecutive-loss circuit breaker.

2026-09-09: added as defense-in-depth alongside the fix that closed the
real deadlock in is_trading_blocked()/consecutive_losses (see
src/fund_state.py::reconcile_from_trades()'s self-healing pause_until and
daily.py's consolidated gate). That fix closes the specific gap found this
session, but a critical mechanism should not depend on nothing ever going
wrong with it again -- the same instinct behind the DISCORD_LIVE_SEND
safety gate. This gives a human a real, explicit way to unstick real fund
trading if the automatic pause/reset logic ever has its own bug and gets
stuck the same way, without needing a code change to do it.

Safe by default: running with no arguments only REPORTS the current
circuit-breaker-relevant state and does not write anything. Nothing is
changed unless --confirm is passed explicitly.

Usage:
  python scripts/reset_circuit_breaker.py
      Report only -- shows consecutive_losses, pause_until,
      circuit_breaker_active, observation_mode, and whether trading is
      currently blocked and why. Writes nothing.

  python scripts/reset_circuit_breaker.py --confirm
      Actually clears the consecutive-loss pause: consecutive_losses -> 0,
      consecutive_wins -> 0, pause_until -> None. Does NOT touch
      circuit_breaker_active/circuit_breaker_reason (the separate daily-
      loss-limit breaker) or observation_mode (the separate weekly-loss
      mechanism) -- use --also-clear-daily-limit / --also-clear-observation
      if you specifically mean to override those too, since they exist to
      catch a different, more severe condition and shouldn't be cleared as
      a side effect of "the consecutive-loss pause looks stuck."

  Add --reason "why you're doing this" (required with --confirm) -- logged
  into the state file and printed, so there's a real record of when and
  why a human overrode the automatic mechanism.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone

from src import fund_state as fs


def _report(state: dict) -> None:
    blocked, reason, btype = fs.is_trading_blocked(state)
    print("=== Circuit breaker state (data/fund_state.json) ===")
    print(f"  consecutive_losses:      {state.get('consecutive_losses', 0)}")
    print(f"  consecutive_wins:        {state.get('consecutive_wins', 0)}")
    print(f"  pause_until:             {state.get('pause_until') or '(not set)'}")
    print(f"  circuit_breaker_active:  {state.get('circuit_breaker_active', False)}")
    print(f"  circuit_breaker_reason:  {state.get('circuit_breaker_reason') or '(none)'}")
    print(f"  daily_trades_count:      {state.get('daily_trades_count', 0)}")
    print(f"  observation_mode:        {state.get('observation_mode', False)}")
    print(f"  observation_mode_until:  {state.get('observation_mode_until') or '(not set)'}")
    print()
    print(f"  Currently blocked from new fund trades: {blocked}")
    if blocked:
        print(f"    reason: {reason}")
        print(f"    type:   {btype}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm", action="store_true",
                        help="Actually clear the consecutive-loss pause. Without this, report only.")
    parser.add_argument("--reason", type=str, default="",
                        help="Required with --confirm: why a human is overriding this.")
    parser.add_argument("--also-clear-daily-limit", action="store_true",
                        help="Also clear circuit_breaker_active/circuit_breaker_reason (the separate daily-loss-limit breaker).")
    parser.add_argument("--also-clear-observation", action="store_true",
                        help="Also clear observation_mode/observation_mode_until (the separate weekly-loss mechanism).")
    args = parser.parse_args()

    state = fs.load()

    print("BEFORE:")
    _report(state)

    if not args.confirm:
        print("\nDry run only -- nothing written. Pass --confirm (and --reason \"...\") to actually reset.")
        return 0

    if not args.reason.strip():
        print("\n--confirm requires --reason \"why you're doing this\" -- refusing to reset silently.", file=sys.stderr)
        return 1

    state = dict(state)
    state["consecutive_losses"] = 0
    state["consecutive_wins"]   = 0
    state["pause_until"]        = None

    if args.also_clear_daily_limit:
        state["circuit_breaker_active"] = False
        state["circuit_breaker_reason"] = None

    if args.also_clear_observation:
        state["observation_mode"]       = False
        state["observation_mode_until"] = None

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    overrides = state.get("manual_overrides") or []
    overrides.append({
        "timestamp_utc": now,
        "action": "reset_circuit_breaker",
        "reason": args.reason.strip(),
        "also_cleared_daily_limit": args.also_clear_daily_limit,
        "also_cleared_observation": args.also_clear_observation,
    })
    state["manual_overrides"] = overrides[-50:]

    fs.save(state)

    print(f"\nReset applied at {now} -- reason: {args.reason.strip()!r}")
    print("\nAFTER:")
    _report(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
