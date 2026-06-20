"""Lightweight between-scan price check for open fund trades.

Runs every 2 hours during active Auckland trading sessions via the
price_check GitHub Actions workflow (triggered by cron-job.org).

Auckland active sessions:
  7pm – 4am  (Tokyo / Asian session)
  9am – 6pm  (London / NY session)

What this script does:
  1. Loads all OPEN fund trades from trades.csv.
  2. Fetches live prices from Twelve Data (rate-limited, no full analysis).
  3. Runs partial_profit_checker — applies stage 1 / stage 2 milestones
     and sends Telegram alerts if newly reached.
  4. Sends a 🚨 alert if any trade has reached 80% of its profit target.

Does NOT run the full daily.py pipeline — no Claude API calls, no selector,
no deep analysis.  Only price fetch + partial profit logic.
"""

import os
import sys
import time

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config                               # noqa: E402
from src import tracker                     # noqa: E402
from src import partial_profit_checker as ppc  # noqa: E402

_FETCH_DELAY = 10   # seconds between live API calls (free tier: 8 req/min)
_PRICE_URL   = "https://api.twelvedata.com/price"


def _fetch_price(pair: str) -> float | None:
    try:
        resp = requests.get(
            _PRICE_URL,
            params={"symbol": pair, "apikey": config.TWELVE_DATA_KEY},
            timeout=10,
        )
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "error":
            return None
        return float(data["price"])
    except Exception:
        return None


def main() -> None:
    print("[PRICE CHECK] Starting between-scan price check")

    if not config.TWELVE_DATA_KEY:
        print("[PRICE CHECK] TWELVE_DATA_KEY not set — aborting")
        sys.exit(0)

    rows = tracker.load()
    open_trades = [
        r for r in rows
        if r.get("status") == "OPEN"
        and str(r.get("trade_this", "")).strip().upper() == "YES"
    ]

    if not open_trades:
        print("[PRICE CHECK] No open YES-trades to monitor — done.")
        return

    print(f"[PRICE CHECK] Monitoring {len(open_trades)} open trade(s)")

    # Fetch prices with rate limiting
    price_cache: dict = {}
    last_api_t = 0.0
    for trade in open_trades:
        pair = trade.get("pair", "")
        elapsed = time.time() - last_api_t
        if last_api_t > 0 and elapsed < _FETCH_DELAY:
            time.sleep(_FETCH_DELAY - elapsed)
        price = _fetch_price(pair)
        last_api_t = time.time()
        if price is not None:
            price_cache[pair] = price
            print(f"  {pair}: {price}")
        else:
            print(f"  {pair}: price unavailable")

    # Run 80% milestone check — also triggers stage 1/2 if not already reached
    ppc.check_80pct_milestone(open_trades, price_cache, log=print)

    print("[PRICE CHECK] Complete.")


if __name__ == "__main__":
    main()
