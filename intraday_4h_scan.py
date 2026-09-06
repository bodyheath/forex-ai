"""Entry point for the intraday 4H book's own scheduled workflow
(.github/workflows/intraday_4h.yml). Standalone by design -- does not run
through daily.py's monolithic pipeline, matching this book's isolation from
every other book and from the real fund.

Each run: evaluates every pair's latest closed 4H bar (src/intraday_4h.py,
Yahoo-only, zero Twelve Data calls), opens positions for anything that
clears grade F, then settles any of this book's own open positions against
live prices. Never touches trades.csv, research_trades.csv, fund_state.json,
or any other book's files.
"""
import sys
from datetime import datetime, timezone

from src import intraday_4h


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def main() -> int:
    log("=== Intraday 4H scan starting ===")
    try:
        scan_result = intraday_4h.run_scan(log=log)
        log(f"Scan complete: {scan_result}")
    except Exception as exc:
        log(f"FATAL: run_scan() failed: {exc}")
        import traceback
        traceback.print_exc()
        return 1

    try:
        settle_result = intraday_4h.settle_open_positions(log=log)
        log(f"Settlement complete: {settle_result}")
    except Exception as exc:
        log(f"FATAL: settle_open_positions() failed: {exc}")
        import traceback
        traceback.print_exc()
        return 1

    log("=== Intraday 4H scan finished ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
