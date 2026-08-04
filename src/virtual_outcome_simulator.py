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
    # Data-integrity guard: a sane stop_loss is always unfavourable (stop_pips < 0).
    # If it's >= 0, stop_loss sits on the *profitable* side of entry — a malformed
    # row (e.g. a Sonnet-output error, same signature as research trades #1642
    # GBP/CHF and #1859 AUD/JPY, both of which have a source_id match in this exact
    # candidate pool: trades.csv #3362 and #3948). Simulating a "stop hit" against
    # such a row would land on the profitable side and get mislabeled LOSS despite
    # a positive pip outcome — mirrors the same bug already fixed in
    # research_outcome_checker.py, independently present here since this module
    # has its own separate resolution logic.
    stop_is_malformed = stop_pips is not None and stop_pips >= 0

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
            # Conservative convention: stop wins the tie — unless the stop itself
            # is malformed, in which case there's no sane "loss" to report here.
            result.update({
                "virtual_outcome": "SKIPPED" if stop_is_malformed else "LOSS",
                "virtual_close_price": stop_loss,
                "virtual_exit_reason": (
                    "STOP_HIT_DATA_INTEGRITY" if stop_is_malformed
                    else "STOP_HIT_SAME_BAR_TIEBREAK"
                ),
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
                "virtual_outcome": "SKIPPED" if stop_is_malformed else "LOSS",
                "virtual_close_price": stop_loss,
                "virtual_exit_reason": (
                    "STOP_HIT_DATA_INTEGRITY" if stop_is_malformed else "STOP_HIT"
                ),
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


def backfill(log=print, limit: int = None, max_pairs: int = None) -> pd.DataFrame:
    """Backfill virtual outcomes for every eligible blocked candidate in
    trades.csv and research_trades.csv.

    Incremental, not merely idempotent: rows already resolved to a terminal
    outcome (WIN/LOSS/EXPIRED/STALE_EXIT/INVALID_ROW) are skipped entirely —
    no re-fetch, no re-simulation. Rows still PENDING or NO_DATA from a
    previous run ARE retried (their expiry window may have elapsed, or bar
    data may now exist), and their row is replaced in place rather than
    duplicated. This is what keeps a daily re-run cheap: steady-state work
    is only newly-added candidates + the still-unresolved backlog.

    Quota safety: aborts outright if the shared TD call counter already
    reads above _QUOTA_ABORT_THRESHOLD (a floor, not a true count — see the
    module docstring). Independent of that, work is capped at
    _MAX_PAIRS_PER_RUN unique pairs per call (override via max_pairs) so a
    single run's worst-case cost is fixed regardless of backlog size.

    Never truncates or corrupts virtual_outcomes.csv: the file is rewritten
    only once, atomically, at the very end, from an in-memory frame built by
    layering this run's results over a full copy of the previous file. A
    crash or exception at any point before that leaves the existing file
    untouched. Per-pair fetch failures and per-candidate simulation errors
    are caught individually and logged — one bad pair or row cannot abort
    the rest of the run.
    """
    if not config.TWELVE_DATA_KEY:
        log("Virtual outcome backfill: TWELVE_DATA_KEY not set — aborting.")
        return _load_existing()

    quota_used = _get_td_calls_today()
    if quota_used > _QUOTA_ABORT_THRESHOLD:
        log(f"Virtual outcome backfill: shared TD quota already at {quota_used}/800 "
            f"(>{_QUOTA_ABORT_THRESHOLD}) — skipping this run entirely to protect the live scan pipeline.")
        return _load_existing()

    cap = max_pairs if max_pairs is not None else _MAX_PAIRS_PER_RUN

    try:
        candidates = _load_candidates()
    except Exception as exc:
        log(f"Virtual outcome backfill: could not load trades.csv / research_trades.csv — {exc} — aborting.")
        return _load_existing()
    existing = _load_existing()

    terminal_keys = set()
    retryable_keys = set()
    if not existing.empty:
        for _, r in existing.iterrows():
            key = (r["source_table"], int(r["source_id"]))
            if r["virtual_outcome"] in _TERMINAL_OUTCOMES:
                terminal_keys.add(key)
            else:
                retryable_keys.add(key)

    new_candidates = [c for c in candidates
                      if (c["source_table"], c["source_id"]) not in terminal_keys
                      and (c["source_table"], c["source_id"]) not in retryable_keys]
    retry_candidates = [c for c in candidates
                        if (c["source_table"], c["source_id"]) in retryable_keys]
    to_process = new_candidates + retry_candidates

    if limit is not None:
        to_process = to_process[:limit]

    log(f"Virtual outcome backfill: {len(new_candidates)} new candidate(s), "
        f"{len(retry_candidates)} retryable (PENDING/NO_DATA) from a previous run, "
        f"{len(terminal_keys)} already resolved and skipped.")

    if not to_process:
        log("Virtual outcome backfill: nothing to do.")
        return existing

    # ── Group by pair, cap the number of distinct pairs fetched this run ───
    by_pair: dict = {}
    for c in to_process:
        by_pair.setdefault(c["pair"], []).append(c)

    pairs_sorted = sorted(by_pair.items())
    if len(pairs_sorted) > cap:
        deferred_pairs = [p for p, _ in pairs_sorted[cap:]]
        deferred_n = sum(len(rows) for _, rows in pairs_sorted[cap:])
        log(f"Virtual outcome backfill: {len(pairs_sorted)} pairs need work this run — "
            f"capped at {cap}/run, deferring {deferred_n} candidate(s) across "
            f"{len(deferred_pairs)} pair(s) to a future run: {', '.join(deferred_pairs)}")
        pairs_sorted = pairs_sorted[:cap]

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    result_rows = []
    fetch_failures = []
    row_errors = []
    _last_call_t = 0.0
    _calls_made = 0

    for i, (pair, rows) in enumerate(pairs_sorted):
        try:
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
            _calls_made += 1
            _increment_td_usage(1)

            if bars:
                log(f"  [{i+1}/{len(pairs_sorted)}] {pair}: {len(rows)} candidate(s), "
                    f"{len(bars)} bar(s) fetched ({start_dt.date()} -> {end_dt.date()})")
            else:
                log(f"  [{i+1}/{len(pairs_sorted)}] {pair}: NO DATA — fetch failed or symbol unavailable.")
                fetch_failures.append(pair)

            data_start = bars[0]["dt"].strftime("%Y-%m-%d %H:%M:%S") if bars else ""
            data_end = bars[-1]["dt"].strftime("%Y-%m-%d %H:%M:%S") if bars else ""

            for c in rows:
                try:
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
                    result_rows.append({
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
                except Exception as exc:
                    row_errors.append((c["source_table"], c["source_id"], str(exc)))
                    log(f"    ERROR simulating {c['source_table']}#{c['source_id']} ({pair}): {exc}")
        except Exception as exc:
            fetch_failures.append(pair)
            log(f"  ERROR processing pair {pair}: {exc} — its candidates are left untouched this run.")

    # ── Merge: this run's results replace any prior PENDING/NO_DATA row for
    #    the same candidate; everything else in the existing file is kept
    #    verbatim. Single write, at the end, so a crash above never touches
    #    the file on disk. ──────────────────────────────────────────────────
    processed_keys = {(r["source_table"], r["source_id"]) for r in result_rows}
    if not existing.empty:
        keep_mask = ~existing.apply(
            lambda r: (r["source_table"], int(r["source_id"])) in processed_keys, axis=1)
        kept_existing = existing[keep_mask]
    else:
        kept_existing = existing

    new_df = pd.DataFrame(result_rows, columns=_OUT_COLUMNS)
    combined = pd.concat([kept_existing, new_df], ignore_index=True) if not kept_existing.empty else new_df

    tmp_path = _OUT_CSV.with_suffix(".csv.tmp")
    combined.to_csv(tmp_path, index=False, encoding="utf-8-sig")
    tmp_path.replace(_OUT_CSV)  # atomic on both POSIX and Windows

    resolved_now = sum(1 for r in result_rows if r["virtual_outcome"] in _TERMINAL_OUTCOMES)
    log(f"Virtual outcome backfill complete: {len(result_rows)} row(s) processed "
        f"({resolved_now} newly resolved) — {_calls_made} TD call(s) made this run — "
        f"written to {_OUT_CSV}.")
    if fetch_failures:
        log(f"  DATA LIMITATION: no historical bars available for {len(set(fetch_failures))} "
            f"pair(s): {', '.join(sorted(set(fetch_failures)))} — their candidates are marked NO_DATA.")
    if row_errors:
        log(f"  {len(row_errors)} row-level simulation error(s) — see ERROR lines above. "
            f"Those candidates were left out of this run's results and will be retried next run.")

    return combined


if __name__ == "__main__":
    backfill()
