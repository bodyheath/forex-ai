"""Pre-fetch current daily close prices for all open trades.

Called once per run — before outcome_checker and research_outcome_checker —
so both checkers share a warm price dict with zero duplicate API calls.

Design:
  • Uses /time_series?interval=1day&outputsize=2 which is more reliable on
    the free tier than /price and returns a clean last-completed-day close.
  • Rate-limit aware: 10 s between every call (free tier: 8/min), with an
    extra batch pause every 8 calls if the pair count exceeds 8.
  • 2 attempts per pair before marking as unavailable.
  • Consecutive-failure tracking persisted to data/price_fetch_state.json.
    A Telegram warning fires once when a pair reaches exactly 3 consecutive
    scans with no price — indicating the pair may need manual inspection.
"""

import html as _html
import json
import re as _re
import time
import urllib.parse
import urllib.request
from pathlib import Path

import requests

import config

_TD_URL      = "https://api.twelvedata.com/time_series"
_TIMEOUT     = 15           # seconds per HTTP request
_FETCH_DELAY = 10           # seconds between API calls (free tier: 8/min)
_BATCH_SIZE  = 8            # extra pause after this many calls in a single run
_MAX_RETRIES = 2            # total attempts per pair
_STATE_FILE: Path = config.DATA_DIR / "price_fetch_state.json"

# Research trade price cache — 6am scan saves, afternoon scans load to avoid re-fetching 87 pairs
_PRICE_CACHE_FILE: Path = config.DATA_DIR / "price_cache.json"
_PRICE_CACHE_MAX_AGE_HOURS = 8.0


# ── Internal helpers ───────────────────────────────────────────────────────────

def _fetch_one(pair: str) -> float | None:
    """Fetch latest daily close for *pair*.  Returns float or None."""
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(
                _TD_URL,
                params={
                    "symbol":     pair,
                    "interval":   "1day",
                    "outputsize": 2,
                    "format":     "JSON",
                    "apikey":     config.TWELVE_DATA_KEY,
                },
                timeout=_TIMEOUT,
            )
            if resp.status_code == 429:
                # Hard rate limit — back off before retry
                time.sleep(_FETCH_DELAY * 2)
                continue
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and data.get("status") == "error":
                # API-level rejection (e.g. invalid symbol) — no point retrying
                return None
            values = data.get("values") or []
            if values:
                return float(values[0]["close"])
        except (KeyError, ValueError, TypeError):
            pass
        except Exception:
            pass
        if attempt < _MAX_RETRIES - 1:
            time.sleep(5)
    return None


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"consecutive_unavailable": {}}


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def _telegram_warning(pairs: list) -> None:
    """Send a Telegram alert when pair(s) have been price-unavailable for 3+ scans."""
    try:
        if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
            return
        raw = (
            "⚠️ <b>Price unavailable — 3 consecutive scans</b>\n"
            + "\n".join(f"• {p}" for p in pairs)
            + "\n\nOpen research trades for these pairs cannot be outcome-checked. "
            "They may be recorded as EXPIRED instead of WIN/LOSS, "
            "polluting ML training data. Check Twelve Data API key and pair symbol validity."
        )
        _TAG = _re.compile(r'(</?[bi]>)')
        _parts = _TAG.split(raw)
        escaped = "".join(
            p if i % 2 == 1 else _html.escape(p, quote=False)
            for i, p in enumerate(_parts)
        )
        url  = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id":    config.TELEGRAM_CHAT_ID,
            "text":       escaped,
            "parse_mode": "HTML",
        }).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception:
        pass


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_prices_for_open_trades(log=print) -> dict:
    """Fetch current daily close prices for every open main + research trade.

    Returns a ``{pair: float}`` dict.  Pairs whose price could not be fetched
    are absent from the dict.  Logs a summary line of the form:

        Fetched current prices for 12 open trades — 11 successful · 1 unavailable

    The returned dict should be passed to ``outcome_checker.check_open_trades()``
    and ``research_outcome_checker.check_open_research_trades()`` as
    ``price_cache`` so those functions skip per-trade API calls entirely.
    """
    if not config.TWELVE_DATA_KEY:
        log("Price pre-fetch: TWELVE_DATA_KEY not set — skipping.")
        return {}

    # ── Collect unique pairs from open main trades + open research trades ──────
    pairs_needed: set = set()

    try:
        from src import tracker as _trk
        for row in _trk.load():
            if row.get("status") == "OPEN" and row.get("trade_this") == "YES":
                p = (row.get("pair") or "").strip()
                if p:
                    pairs_needed.add(p)
    except Exception as exc:
        log(f"Price pre-fetch: could not read main trades — {exc}")

    try:
        from src import research_tracker as _rtrk
        for row in _rtrk.load():
            if row.get("status") == "OPEN":
                p = (row.get("pair") or "").strip()
                if p:
                    pairs_needed.add(p)
    except Exception as exc:
        log(f"Price pre-fetch: could not read research trades — {exc}")

    if not pairs_needed:
        log("Price pre-fetch: no open trades to price.")
        return {}

    pairs_list = sorted(pairs_needed)
    log(
        f"Price pre-fetch: {len(pairs_list)} open trade pair(s) to price — "
        f"{', '.join(pairs_list)}"
    )

    # ── Fetch prices with rate-limit control ───────────────────────────────────
    state  = _load_state()
    consec = state.get("consecutive_unavailable", {})
    prices: dict  = {}
    unavail: list = []
    newly_warned: list = []

    for i, pair in enumerate(pairs_list):
        if i > 0:
            time.sleep(_FETCH_DELAY)
        if i > 0 and i % _BATCH_SIZE == 0:
            log(f"Price pre-fetch: batch pause after {i} calls — rate-limit protection ...")
            time.sleep(_FETCH_DELAY)

        price = _fetch_one(pair)
        if price is not None:
            prices[pair] = price
            consec.pop(pair, None)          # reset consecutive-failure counter
        else:
            unavail.append(pair)
            consec[pair] = consec.get(pair, 0) + 1
            if consec[pair] == 3:           # warn exactly once at the threshold
                newly_warned.append(pair)

    # ── Persist failure state ──────────────────────────────────────────────────
    state["consecutive_unavailable"] = consec
    _save_state(state)

    # ── Summary log ───────────────────────────────────────────────────────────
    n_ok   = len(prices)
    n_fail = len(unavail)
    log(
        f"Price pre-fetch: fetched current prices for {len(pairs_list)} open trade(s) — "
        f"{n_ok} successful · {n_fail} unavailable"
        + (f" (unavailable: {', '.join(unavail)})" if unavail else "")
    )

    if newly_warned:
        log(
            f"Price pre-fetch: WARNING — "
            f"{', '.join(newly_warned)} price unavailable for 3+ consecutive scans — "
            "sending Telegram alert."
        )
        _telegram_warning(newly_warned)

    return prices
