"""forex-ai command-line entry point.

Usage:
    python main.py EUR/USD                          analyse one pair (logged to tracker)
    python main.py GBPUSD USDJPY                    analyse several pairs
    python main.py --health EUR/USD                 check live sources (no Claude/price call)
    python main.py --raw EUR/USD                    also dump the raw evidence bundle
    python main.py --remember "pattern" "outcome"   add a system-memory note
    python main.py --close 7 WIN 1.0925             record an outcome (WIN/LOSS/BREAKEVEN/SKIPPED/EXPIRED)
    python main.py --stats                           print performance stats
    python main.py --learn                           refresh learning memory from outcomes
    python main.py --dashboard                       rebuild data/dashboard.html

Every analysis is appended to data/trades.csv and the dashboard is rebuilt.
"""

import argparse
import json
import sys

# The analyst output can contain non-cp1252 characters (em-dashes, emoji). When
# stdout is piped or run from Task Scheduler, Python defaults to the legacy code
# page and would crash on encode. Force UTF-8 with replacement.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import config
from src import dashboard, learning, memory, pipeline, service, tracker


def _health(pairs: list) -> int:
    """Report which non-price data sources are live for each pair. Skips Twelve
    Data (price) and Claude (no analysis run)."""
    from src import fundamental, macro, positioning, sentiment

    for pair in pairs:
        try:
            base, quote = pipeline.parse_pair(pair)
        except ValueError as exc:
            print(f"{pair}: {exc}")
            continue
        print(f"\n{base}/{quote} data-source health (price source skipped):")

        fund = fundamental.analyse(base, quote)
        print(f"  fundamental : {fund['status']:8} rate diff {fund.get('rate_differential_pct')}%")

        sent = sentiment.analyse(base, quote)
        print(f"  sentiment   : base={sent['base'].get('status')}, quote={sent['quote'].get('status')}")

        pos = positioning.analyse(base, quote)
        pb, pq = pos["base"], pos["quote"]
        print(f"  positioning : {base}={pb['status']} ({pb.get('direction', pb.get('reason',''))}), "
              f"{quote}={pq['status']} ({pq.get('direction', pq.get('reason',''))})")

        mac = macro.analyse()
        live = sum(1 for v in mac["signals"].values() if "value" in v)
        print(f"  macro       : {live}/{len(mac['signals'])} signals live")
    print("\n(Run without --health to perform the full Claude analysis.)")
    return 0


def _print_stats() -> int:
    s = learning.compute_stats()
    wr = f"{s['win_rate']*100:.0f}%" if s["win_rate"] is not None else "n/a"
    exp = f"{s['expectancy_r']:+.2f}R" if s["expectancy_r"] is not None else "n/a"
    print("forex-ai performance")
    print(f"  recommendations : {s['total_recommendations']}")
    print(f"  actionable (YES): {s['actionable']}")
    print(f"  open / closed   : {s['open']} / {s['closed']}")
    print(f"  wins / losses   : {s['wins']} / {s['losses']}")
    print(f"  win rate        : {wr}")
    print(f"  expectancy      : {exp}")
    return 0


def _close(close_args: list) -> int:
    if len(close_args) < 2:
        print("Usage: --close ID STATUS [EXIT_PRICE]", file=sys.stderr)
        return 1
    rec_id, status = close_args[0], close_args[1]
    exit_price = close_args[2] if len(close_args) > 2 else None
    try:
        row = tracker.update_outcome(rec_id, status, exit_price)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Recorded #{row['id']} {row['pair']} -> {row['status']} "
          f"(R={row.get('r_multiple')}, pips={row.get('pips')})")
    learning.update_memory()
    print(f"Learning memory refreshed. Dashboard: {dashboard.generate()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AI forex pair analyst.")
    parser.add_argument("pairs", nargs="*", help="Currency pairs, e.g. EUR/USD GBPUSD")
    parser.add_argument("--raw", action="store_true", help="Print the raw evidence bundle too")
    parser.add_argument("--health", action="store_true",
                        help="Check live data sources without calling Claude or the price source")
    parser.add_argument("--remember", nargs=2, metavar=("PATTERN", "OUTCOME"),
                        help="Add a learned pattern to system memory and exit")
    parser.add_argument("--close", nargs="+", metavar="ARG",
                        help="Record an outcome: --close ID STATUS [EXIT_PRICE]")
    parser.add_argument("--stats", action="store_true", help="Print performance stats and exit")
    parser.add_argument("--learn", action="store_true",
                        help="Refresh learning memory from recorded outcomes and exit")
    parser.add_argument("--dashboard", action="store_true",
                        help="Rebuild the HTML dashboard and exit")
    args = parser.parse_args()

    # --- commands that don't need API keys ---
    if args.remember:
        memory.add_outcome(*args.remember)
        print("Saved to system memory.")
        return 0
    if args.close:
        return _close(args.close)
    if args.stats:
        return _print_stats()
    if args.learn:
        res = learning.update_memory()
        print(f"Learning memory refreshed: {res['patterns_written']} auto-patterns "
              f"from {res['closed']} closed trades.")
        return 0
    if args.dashboard:
        print(f"Dashboard written to: {dashboard.generate()}")
        return 0

    missing = config.missing_keys()
    if missing:
        print("ERROR: missing API keys in .env: " + ", ".join(missing), file=sys.stderr)
        return 2

    if not args.pairs:
        parser.print_help()
        return 1

    if args.health:
        return _health(args.pairs)

    exit_code = 0
    for pair in args.pairs:
        print("\n" + "=" * 70)
        try:
            result = service.analyse_and_log(pair)
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED to analyse {pair}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        if result.get("screened_out"):
            s = result["screen"]
            print(f"\n{result['pair']} FILTERED at stage 1 — score {s['score']}/5: {s['reason']}")
            print(f"(Score < 4: logged as #{result['id']:04d} with NO_TRADE status — no deep analysis run.)")
            if args.raw:
                print("\nRAW EVIDENCE BUNDLE:")
                print(json.dumps(result["bundle"], indent=2))
            continue

        print("\n" + "=" * 70)
        print(result["report"])
        print("=" * 70)
        if args.raw:
            print("\nRAW EVIDENCE BUNDLE:")
            print(json.dumps(result["bundle"], indent=2))
        print(f"\nLogged as recommendation #{result['id']:04d} "
              f"({result['parsed']['trade_this']}).")

    dashboard.generate()
    print(f"\nDashboard updated: {config.DASHBOARD_HTML}")
    print("Reminder: this is an analysis aid, NOT financial advice. "
          "Most setups should be TRADE_THIS: NO.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
