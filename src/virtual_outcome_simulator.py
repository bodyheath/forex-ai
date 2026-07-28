"""Virtual outcome simulator for blocked (NO_TRADE / SKIPPED) trade candidates.

Answers: if a candidate the system blocked had actually been allowed to
trade, would it have won or lost?  This is NOT a new outcome methodology —
it reuses the exact same target/stop/expiry functions that real open trades
are checked against (cascade.py, plus each source module's own
_determine_outcome / _compute_expiry_days / stale-exit logic), just fed
historical OHLCV bars instead of one live price per poll.

Key difference from live polling: live polling sees one current price per
~30 min scan. This simulator sees full historical high/low per hour, so it
walks bar-by-bar checking both extremes. That is a strictly more accurate
view of the same target_hit/stop_hit rules, not a different rule — except
for one unavoidable, explicitly documented approximation: if a single 1h
bar's range crosses BOTH the target and the stop (impossible to happen with
a single live price, but possible with hourly high/low), intrabar order is
unknown and the simulator conservatively resolves it as the STOP (LOSS).
This is a standard, conservative backtesting convention and is recorded
per-row so any tie-broken outcome can be identified in virtual_outcomes.csv.

Research rows only carry a date (no time-of-day) for `date`, matching what
research's own real logic already does (opened_at[:10] everywhere) — so
this simulator does not owe research rows any more precision than the real
system already has.

Fund and research use separate, independently-tuned real implementations
(different _EXPIRY_DAYS fallback, different _compute_expiry_days formula,
and research-only STALE_EXIT). This module calls each source's own real
functions rather than a unified reimplementation, so results stay directly
comparable to that source's real trade outcomes.

Persistence: data/virtual_outcomes.csv, a new file. trades.csv and
research_trades.csv are never written to by this module.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

import config
from src import cascade as _casc
from src import outcome_checker as _oc
from src import research_outcome_checker as _roc

_TD_URL = "https://api.twelvedata.com/time_series"
_FETCH_DELAY = 10           # seconds between API calls (free tier: 8 req/min)
_INTERVAL = "1h"
_TIMEOUT = 20

# Shared with monitor.py / selector.py / daily.py — same file, same schema
# ({"date": "YYYY-MM-DD", "calls": N}), reset daily. NOTE: this counter is
# known to under-report real usage — selector.py's own _increment_td_usage()
# is dead code (never called), so the bulk of the main pipeline's per-pair
# technical/price fetches are invisible to it. Treat a high reading here as
# a reliable STOP signal (real usage is at least this high) but never as
# proof of headroom (real usage could be far higher than it shows). This is
# why this module is self-limiting via _MAX_PAIRS_PER_RUN rather than
# sizing itself off whatever headroom this counter appears to report.
_API_USAGE_FILE = config.DATA_DIR / "api_usage.json"
_QUOTA_ABORT_THRESHOLD = 700   # matches monitor.py's own "critical" cutoff

# Hard per-run cap, independent of the (unreliable) quota reading above.
# Steady-state need is small (only pairs for newly-added blocked candidates
# since the last run + pairs with a currently-PENDING row) — 25 pairs/run
# covers that with room to spare while keeping worst-case daily add-on cost
# at 25 calls, well inside the shared 800/day budget even on a heavy scan day.
_MAX_PAIRS_PER_RUN = 25

_TERMINAL_OUTCOMES = {"WIN", "LOSS", "EXPIRED", "STALE_EXIT", "INVALID_ROW"}
_RETRYABLE_OUTCOMES = {"PENDING", "NO_DATA"}

_TRADES_CSV = config.DATA_DIR / "trades.csv"
_RESEARCH_CSV = config.DATA_DIR / "research_trades.csv"
_OUT_CSV = config.DATA_DIR / "virtual_outcomes.csv"

_OUT_COLUMNS = [
    "source_table", "source_id", "pair", "direction", "opened_at",
    "entry", "stop_loss", "target_raw", "t2_price",
    "blocked_by_gate", "block_notes_raw",
    "virtual_outcome", "virtual_close_price", "virtual_exit_reason",
    "virtual_r_multiple", "virtual_pips", "virtual_closed_at",
    "same_bar_tiebreak", "bars_used", "data_start", "data_end",
    "simulated_at",
]


def _get_td_calls_today() -> int:
    """Read the shared TD call counter (same file/schema as monitor.py /
    selector.py / daily.py). See the module-level note above on why this is
    treated as a floor, not a true count."""
    try:
        usage = json.loads(_API_USAGE_FILE.read_text(encoding="utf-8")) if _API_USAGE_FILE.exists() else {}
        from datetime import date as _date
        return int(usage.get("calls", 0)) if usage.get("date") == str(_date.today()) else 0
    except Exception:
        return 0


def _increment_td_usage(n: int = 1) -> None:
    """Contribute this module's real TD calls to the shared daily counter."""
    try:
        from datetime import date as _date
        usage: dict = {}
        if _API_USAGE_FILE.exists():
            try:
                usage = json.loads(_API_USAGE_FILE.read_text(encoding="utf-8"))
            except Exception:
                usage = {}
        today = str(_date.today())
        if usage.get("date") != today:
            usage = {"date": today, "calls": 0}
        usage["calls"] = int(usage.get("calls", 0)) + n
        _API_USAGE_FILE.write_text(json.dumps(usage), encoding="utf-8")
    except Exception:
        pass


def _to_float(val):
    try:
        v = float(val)
        return v if v == v else None  # reject NaN (NaN != NaN)
    except (TypeError, ValueError):
        return None


# ── Gate / block-reason classification ──────────────────────────────────────

def _classify_block_reason(source: str, notes: str) -> str:
    """Bucket a blocked row's notes into a short gate label.

    Research's own SKIPPED rows are all intra-scan duplicate dedup, not a
    fund-side risk gate, so that's classified directly. Everything else
    (all fund vetoes) reuses scripts/veto_reason_report.classify_veto so
    labels stay identical to the existing veto-reporting tool.
    """
    n = "" if notes is None or notes != notes else str(notes).strip()  # notes != notes catches NaN
    # Strip the existing live-polling VIRTUAL_OUTCOME: tag (outcome_checker.
    # check_virtual_trades already stamps ~100 of these rows) so the gate
    # bucket reflects the real block reason, not that unrelated tag. When
    # nothing follows the tag, the row's original notes were genuinely empty.
    if n.startswith("VIRTUAL_OUTCOME:"):
        n = n.split("|", 1)[1].strip() if "|" in n else ""
    if source == "research" and n.lower().startswith("duplicate"):
        return "Research dedup (duplicate pair/direction, same scan date)"
    try:
        _scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
        if _scripts_dir not in sys.path:
            sys.path.insert(0, _scripts_dir)
        import veto_reason_report as _vr
        return _vr.classify_veto(n)
    except Exception:
        return f"Other/unrecognized: {n[:40]}" if n else "(no notes recorded)"


# ── Historical OHLCV fetch ───────────────────────────────────────────────────

def fetch_historical_bars(pair: str, start_date: str, end_date: str) -> list:
    """Fetch hourly OHLC bars for *pair* between start_date and end_date
    (both "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS", UTC).

    Returns a chronologically ascending list of dicts:
      {"dt": datetime (naive, UTC), "open": float, "high": float,
       "low": float, "close": float}
    Returns [] on any failure (invalid symbol, no data, API error) — caller
    must treat an empty list as NO_DATA, not as "nothing happened".
    """
    try:
        resp = requests.get(
            _TD_URL,
            params={
                "symbol": pair,
                "interval": _INTERVAL,
                "start_date": start_date,
                "end_date": end_date,
                "timezone": "UTC",
                "apikey": config.TWELVE_DATA_KEY,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "error":
            return []
        values = data.get("values") or []
        bars = []
        for v in values:
            try:
                bars.append({
                    "dt": datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S"),
                    "open": float(v["open"]),
                    "high": float(v["high"]),
                    "low": float(v["low"]),
                    "close": float(v["close"]),
                })
            except (KeyError, ValueError, TypeError):
                continue
        bars.sort(key=lambda b: b["dt"])
        return bars
    except Exception:
        return []


# ── Core simulation ──────────────────────────────────────────────────────────

def simulate_row(row: dict, source: str, bars: list, now_utc: datetime) -> dict:
    """Simulate one blocked candidate's outcome against historical bars.

    row: dict with at minimum pair, direction, entry, stop_loss, target,
         and opened_at (already extracted as a "YYYY-MM-DD[ ...]" string).
    source: "fund" or "research" — selects which real module's
            _determine_outcome / _compute_expiry_days / stale-exit logic
            to call, so results stay comparable to that source's real
            trade tracking.
    bars: this pair's full fetched bar list (chronological).
    now_utc: real wall-clock (naive UTC) — used to distinguish EXPIRED
             (window elapsed, no data left to check) from PENDING (window
             hasn't elapsed yet in real time, so it's not resolvable yet,
             exactly like a real open trade).

    Returns a result dict (not yet including CSV bookkeeping fields).
    """
    pair = row["pair"]
    direction = row["direction"]
    entry = _to_float(row["entry"])
    stop_loss = _to_float(row["stop_loss"])
    target_raw = _to_float(row["target"])
    opened_at = row["opened_at"]

    result = {
        "virtual_outcome": None, "virtual_close_price": None,
        "virtual_exit_reason": None, "virtual_r_multiple": None,
        "virtual_pips": None, "virtual_closed_at": None,
        "same_bar_tiebreak": "FALSE", "bars_used": 0,
        "t2_price": None,
    }

    if direction not in ("BUY", "SELL") or None in (entry, stop_loss, target_raw):
        result["virtual_outcome"] = "INVALID_ROW"
        return result

    _t1, t2_price, _t3 = _casc.compute_levels(entry, stop_loss, target_raw, direction)
    if t2_price is None:
        result["virtual_outcome"] = "INVALID_ROW"
        return result
    result["t2_price"] = t2_price

    # Use full time-of-day precision when available (fund's timestamp is
    # "YYYY-MM-DD HH:MM:SS") so the bar walk starts at the actual moment the
    # candidate was generated, not midnight of that date — otherwise hours
    # of pre-signal price action would be scanned as if it were "post-block"
    # history. Research only carries a date (matching its own real logic's
    # own opened_at[:10] precision), so midnight-start is its true ceiling.
    try:
        opened_dt = datetime.strptime(opened_at[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        try:
            opened_dt = datetime.strptime(opened_at[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            result["virtual_outcome"] = "INVALID_ROW"
            return result

    row_bars = [b for b in bars if b["dt"] >= opened_dt]
    if not row_bars:
        result["virtual_outcome"] = "NO_DATA"
        return result

    stop_pips = _casc.pips_at(entry, stop_loss, pair, direction)  # negative (unfavourable)

    if source == "fund":
        base_expiry = _oc._compute_expiry_days(row)
    else:
        base_expiry = _roc._compute_expiry_days(row)

    sim_row = {"direction": direction, "stop_loss": stop_loss,
               "t2_price": t2_price, "t2_hit": "FALSE"}

    n_used = 0
    for bar in row_bars:
        n_used += 1
        ext_expiry = _casc.expiry_extension(sim_row, base_expiry)

        # ── Research-only stale-exit hard cap ──────────────────────────
        if source == "research" and _roc._is_stale_exit(opened_at, as_of=bar["dt"]):
            result.update({
                "virtual_outcome": "STALE_EXIT",
                "virtual_close_price": bar["close"],
                "virtual_exit_reason": "STALE_EXIT",
                "virtual_closed_at": bar["dt"].strftime("%Y-%m-%d %H:%M:%S"),
                "bars_used": n_used,
            })
            break

        if direction == "BUY":
            hit_target = bar["high"] >= t2_price
            hit_stop = bar["low"] <= stop_loss
        else:
            hit_target = bar["low"] <= t2_price
            hit_stop = bar["high"] >= stop_loss

        if hit_target and hit_stop:
            # Both crossed within one hourly bar — intrabar order unknown.
            # Conservative convention: stop wins the tie.
            result.update({
                "virtual_outcome": "LOSS",
                "virtual_close_price": stop_loss,
                "virtual_exit_reason": "STOP_HIT_SAME_BAR_TIEBREAK",
                "virtual_closed_at": bar["dt"].strftime("%Y-%m-%d %H:%M:%S"),
                "same_bar_tiebreak": "TRUE",
                "bars_used": n_used,
            })
            break
        if hit_target:
            result.update({
                "virtual_outcome": "WIN",
                "virtual_close_price": t2_price,
                "virtual_exit_reason": "TARGET_HIT",
                "virtual_closed_at": bar["dt"].strftime("%Y-%m-%d %H:%M:%S"),
                "bars_used": n_used,
            })
            break
        if hit_stop:
            result.update({
                "virtual_outcome": "LOSS",
                "virtual_close_price": stop_loss,
                "virtual_exit_reason": "STOP_HIT",
                "virtual_closed_at": bar["dt"].strftime("%Y-%m-%d %H:%M:%S"),
                "bars_used": n_used,
            })
            break

        # ── Expiry check (reuses each source's real _determine_outcome) ─
        determine_fn = _oc._determine_outcome if source == "fund" else _roc._determine_outcome
        outcome = determine_fn(
            direction, bar["close"], entry, stop_loss, target_raw, opened_at,
            expiry_days=ext_expiry, as_of=bar["dt"],
        )
        if outcome == "EXPIRED":
            result.update({
                "virtual_outcome": "EXPIRED",
                "virtual_close_price": bar["close"],
                "virtual_exit_reason": "EXPIRED",
                "virtual_closed_at": bar["dt"].strftime("%Y-%m-%d %H:%M:%S"),
                "bars_used": n_used,
            })
            break
        if outcome == "LOSS":
            # determine_fn's own stop check agrees — should be unreachable
            # since hit_stop above already covers it, but kept for parity.
            result.update({
                "virtual_outcome": "LOSS",
                "virtual_close_price": stop_loss,
                "virtual_exit_reason": "STOP_HIT",
                "virtual_closed_at": bar["dt"].strftime("%Y-%m-%d %H:%M:%S"),
                "bars_used": n_used,
            })
            break
    else:
        # Ran out of bars without resolving.
        last_bar_dt = row_bars[-1]["dt"]
        ext_expiry_final = _casc.expiry_extension(sim_row, base_expiry)
        window_elapsed_by_now = (now_utc - opened_dt).days >= ext_expiry_final
        data_lags_real_time = (now_utc - last_bar_dt).total_seconds() > 3 * 3600
        if window_elapsed_by_now and not data_lags_real_time:
            # Expiry window has passed and our data reaches "now" — genuinely
            # unresolved is impossible here (determine_fn would have caught
            # it), so this branch is a defensive fallback only.
            result["virtual_outcome"] = "EXPIRED"
            result["virtual_close_price"] = row_bars[-1]["close"]
            result["virtual_exit_reason"] = "EXPIRED"
            result["virtual_closed_at"] = last_bar_dt.strftime("%Y-%m-%d %H:%M:%S")
        elif data_lags_real_time and window_elapsed_by_now:
            result["virtual_outcome"] = "NO_DATA_INSUFFICIENT_HISTORY"
        else:
            result["virtual_outcome"] = "PENDING"
        result["bars_used"] = n_used

    if result["virtual_outcome"] in ("WIN", "LOSS", "EXPIRED", "STALE_EXIT") \
            and result["virtual_close_price"] is not None:
        pips = _casc.pips_at(entry, result["virtual_close_price"], pair, direction)
        result["virtual_pips"] = pips
        if stop_pips:
            result["virtual_r_multiple"] = round(pips / abs(stop_pips), 3)

    return result


# ── Backfill orchestration ───────────────────────────────────────────────────

def _load_candidates() -> list:
    """Scope every NO_TRADE/SKIPPED row with valid entry/stop/target from
    both trades.csv (fund) and research_trades.csv (research)."""
    candidates = []

    fund = pd.read_csv(_TRADES_CSV, encoding="utf-8-sig")
    fund_blocked = fund[fund["status"].astype(str).str.upper().isin(["NO_TRADE", "SKIPPED"])]
    for _, r in fund_blocked.iterrows():
        e, s, t = _to_float(r.get("entry")), _to_float(r.get("stop_loss")), _to_float(r.get("target"))
        if None in (e, s, t) or e == 0 or s == 0 or t == 0:
            continue
        if str(r.get("direction", "")).upper() not in ("BUY", "SELL"):
            continue
        candidates.append({
            "source_table": "trades.csv", "source": "fund",
            "source_id": int(r["id"]), "pair": r["pair"],
            "direction": str(r["direction"]).upper(),
            "opened_at": str(r["timestamp"]),
            "entry": e, "stop_loss": s, "target": t,
            "notes": r.get("notes", ""),
            "_raw": r.to_dict(),
        })

    research = pd.read_csv(_RESEARCH_CSV, encoding="utf-8-sig")
    research_blocked = research[research["status"].astype(str).str.upper().isin(["NO_TRADE", "SKIPPED"])]
    for _, r in research_blocked.iterrows():
        e, s, t = _to_float(r.get("entry")), _to_float(r.get("stop_loss")), _to_float(r.get("target"))
        if None in (e, s, t) or e == 0 or s == 0 or t == 0:
            continue
        if str(r.get("direction", "")).upper() not in ("BUY", "SELL"):
            continue
        candidates.append({
            "source_table": "research_trades.csv", "source": "research",
            "source_id": int(r["id"]), "pair": r["pair"],
            "direction": str(r["direction"]).upper(),
            "opened_at": str(r["date"]),
            "entry": e, "stop_loss": s, "target": t,
            "notes": r.get("notes", ""),
            "_raw": r.to_dict(),
        })

    return candidates


def _load_existing() -> pd.DataFrame:
    if _OUT_CSV.exists():
        try:
            return pd.read_csv(_OUT_CSV, encoding="utf-8-sig")
        except Exception:
            pass
    return pd.DataFrame(columns=_OUT_COLUMNS)


def backfill(log=print, limit: int = None) -> pd.DataFrame:
    """Backfill virtual outcomes for every eligible blocked candidate in
    trades.csv and research_trades.csv. Idempotent: rows already present
    in data/virtual_outcomes.csv (by source_table+source_id) are skipped.

    Fetches one historical bar-range per unique pair (not per candidate) to
    stay well inside the shared 800-calls/day TwelveData budget.
    """
    if not config.TWELVE_DATA_KEY:
        log("Virtual outcome backfill: TWELVE_DATA_KEY not set — aborting.")
        return pd.DataFrame(columns=_OUT_COLUMNS)

    candidates = _load_candidates()
    existing = _load_existing()
    done_keys = set(zip(existing.get("source_table", []), existing.get("source_id", [])))
    candidates = [c for c in candidates if (c["source_table"], c["source_id"]) not in done_keys]

    if limit is not None:
        candidates = candidates[:limit]

    log(f"Virtual outcome backfill: {len(candidates)} candidate(s) to simulate "
        f"({len(done_keys)} already done, skipped).")

    if not candidates:
        return existing

    # ── Group by pair, determine the fetch window each pair needs ──────────
    by_pair: dict = {}
    for c in candidates:
        by_pair.setdefault(c["pair"], []).append(c)

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    new_rows = []
    fetch_failures = []
    _last_call_t = 0.0

    for i, (pair, rows) in enumerate(sorted(by_pair.items())):
        opened_dts = []
        for c in rows:
            try:
                opened_dts.append(datetime.strptime(c["opened_at"][:10], "%Y-%m-%d"))
            except (ValueError, TypeError):
                pass
        if not opened_dts:
            continue
        start_dt = min(opened_dts)
        end_dt = now_utc  # 21-day max expiry is always inside "today"

        _elapsed = time.time() - _last_call_t
        if _last_call_t > 0 and _elapsed < _FETCH_DELAY:
            time.sleep(_FETCH_DELAY - _elapsed)
        bars = fetch_historical_bars(
            pair,
            start_dt.strftime("%Y-%m-%d 00:00:00"),
            end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        )
        _last_call_t = time.time()

        log(f"  [{i+1}/{len(by_pair)}] {pair}: {len(rows)} candidate(s), "
            f"{len(bars)} bar(s) fetched "
            f"({start_dt.date()} -> {end_dt.date()})" if bars else
            f"  [{i+1}/{len(by_pair)}] {pair}: NO DATA — fetch failed or symbol unavailable.")

        if not bars:
            fetch_failures.append(pair)

        data_start = bars[0]["dt"].strftime("%Y-%m-%d %H:%M:%S") if bars else ""
        data_end = bars[-1]["dt"].strftime("%Y-%m-%d %H:%M:%S") if bars else ""

        for c in rows:
            sim_input = {
                "pair": c["pair"], "direction": c["direction"],
                "entry": c["entry"], "stop_loss": c["stop_loss"],
                "target": c["target"], "opened_at": c["opened_at"],
                **{k: v for k, v in c["_raw"].items() if k not in
                   ("pair", "direction", "entry", "stop_loss", "target")},
            }
            res = simulate_row(sim_input, c["source"], bars, now_utc)
            notes_str = "" if c["notes"] is None or c["notes"] != c["notes"] else str(c["notes"])
            gate = _classify_block_reason(c["source"], notes_str)
            new_rows.append({
                "source_table": c["source_table"], "source_id": c["source_id"],
                "pair": c["pair"], "direction": c["direction"],
                "opened_at": c["opened_at"],
                "entry": c["entry"], "stop_loss": c["stop_loss"],
                "target_raw": c["target"], "t2_price": res.get("t2_price"),
                "blocked_by_gate": gate, "block_notes_raw": notes_str[:200],
                "virtual_outcome": res["virtual_outcome"],
                "virtual_close_price": res["virtual_close_price"],
                "virtual_exit_reason": res["virtual_exit_reason"],
                "virtual_r_multiple": res["virtual_r_multiple"],
                "virtual_pips": res["virtual_pips"],
                "virtual_closed_at": res["virtual_closed_at"],
                "same_bar_tiebreak": res["same_bar_tiebreak"],
                "bars_used": res["bars_used"],
                "data_start": data_start, "data_end": data_end,
                "simulated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            })

    new_df = pd.DataFrame(new_rows, columns=_OUT_COLUMNS)
    combined = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
    combined.to_csv(_OUT_CSV, index=False, encoding="utf-8-sig")

    log(f"Virtual outcome backfill complete: {len(new_rows)} new row(s) written to {_OUT_CSV}.")
    if fetch_failures:
        log(f"  DATA LIMITATION: no historical bars available for {len(fetch_failures)} "
            f"pair(s): {', '.join(fetch_failures)} — their candidates are marked NO_DATA.")

    return combined


if __name__ == "__main__":
    backfill()
