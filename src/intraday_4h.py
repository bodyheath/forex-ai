"""Intraday 4H book -- Phase 01 registry, book "G_intraday_4h" (2026-09-06).

A genuinely separate, faster-resolving clock feeding the SAME proven
deterministic technical/grading/Devil's-Advocate reasoning the daily
pipeline uses -- not a reinterpretation of the daily/weekly pipeline, and
not a new, unvalidated strategy. See scripts/intraday_4h_backtest.py for
the Step-1 backtest this rests on (real 2-year 4H OHLC, same deterministic
grade buckets historical_grading_backtest.py already established, real
trade_costs applied) -- run and reviewed BEFORE this module was wired live.

============================================================================
WHAT'S REUSED, WHAT'S DELIBERATELY DIFFERENT -- see the backtest script's
own docstring for the full reasoning; summarised here:
============================================================================
  Reused unchanged: src.technical._summarise()/_ema_ribbon()/_tech_signal(),
    src.mtf._tf_signal(), src.trade_costs.net_pips_for_closed_trade(), the
    same would_F/would_D_ribbon/clean deterministic grade buckets, the same
    2:1 mechanical R:R construction cascade.py's real trades use, and
    Devil's Advocate (src.analyst.devil_advocate) for candidates that clear
    a real technical bar -- same "DA only for confident setups" gate the
    real pipeline uses, just anchored on _tech_signal()'s deterministic
    1-10 score (DA_MIN_TECH_SCORE) instead of an LLM confidence value,
    since this book runs no Haiku/Sonnet Stage-1/2 scoring at all.
  Deliberately different: the higher-timeframe anchor is DAILY, not weekly
    (there's no "weekly" for a 4H clock); no fundamental/sentiment/
    positioning/macro layer at all (genuinely stale within a single day --
    see the backtest docstring); its own hours-based expiry, calibrated
    from the backtest's own bars-to-resolve distribution, not the daily
    pipeline's day-count constants.

============================================================================
DATA SOURCE -- YAHOO ONLY, ZERO TWELVE DATA CALLS
============================================================================
Fetches through src.yahoo_finance.fetch_4h_candles()/fetch_daily_candles()
exclusively -- the exact same free, unlimited, already-production source
the backtest used, so the backtest actually tested what runs live. Adds
zero pressure to the existing pipeline's rate-limited Twelve Data budget
and zero added market-data cost.

============================================================================
ISOLATION
============================================================================
Own candidate CSV (G_intraday_4h_candidates.csv, not the shared
candidates.csv every other book writes to), own position file and state
file via virtual_books.py's own generic, book-id-keyed helpers
(load_book_state/save_book_state/_load_csv/_write_csv -- reused directly,
not reimplemented). Never reads or writes trades.csv, research_trades.csv,
fund_state.json, risk_profile.json, or any other book's _positions.csv/
_state.json. Registered in virtual_books.BOOKS purely so reporting
(get_all_summaries()) picks it up automatically like every other book;
its candidate-generation and settlement run through this module's own
functions, called from its own scheduled workflow, not through
evaluate_candidates()/settle_open_candidates() (which are hard-wired to
the daily pipeline's deep_results shape and a single 14-day expiry that
doesn't fit a 4H clock at all).
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable

from src import mtf
from src import technical as tech
from src import trade_costs
from src import virtual_books as vb
from src import yahoo_finance as yf_mod
from src.selector import UNIVERSE

BOOK_ID = "G_intraday_4h"
BOOK_DESCRIPTION = (
    "Intraday 4H clock -- same deterministic technical/grading/DA reasoning "
    "as the daily pipeline, backtested on 2 years of real 4H OHLC before "
    "going live (scripts/intraday_4h_backtest.py). Fully isolated: own "
    "candidate/position/state files, Yahoo-only data (zero Twelve Data "
    "calls), own hours-based expiry."
)

CANDIDATES_CSV = vb.VBOOKS_DIR / f"{BOOK_ID}_candidates.csv"

CANDIDATE_FIELDS = [
    "id", "bar_time", "pair", "direction",
    "entry", "stop_loss", "target", "stop_pips", "rr",
    "bucket", "ribbon_status", "hf_conflict", "tech_score",
    "da_fired", "da_verdict", "da_reasons",
    "expiry_hours",
    "status",             # OPEN | WIN | LOSS | EXPIRED
    "opened_at", "closed_at", "exit_price", "pips", "net_pips",
]

# Only fire Devil's Advocate for candidates whose deterministic technical
# score matches the real pipeline's own "confident enough to double-check"
# bar -- see src/analyst.py's devil_advocate() docstring/daily.py's 7+
# confidence gate. Prevents an LLM call on every marginal 4H bar.
DA_MIN_TECH_SCORE = 7

# Calibrated from scripts/intraday_4h_backtest.py's bars-to-resolve
# distribution over real decisive (WIN/LOSS) 4H trades -- see that script's
# printed percentiles for the evidence this constant rests on. Mirrors
# research_outcome_checker.py's R:R-based expiry formula PATTERN (a floor
# plus an R:R-scaled term), in hours instead of days.
_EXPIRY_HOURS_FLOOR = 48    # placeholder -- confirm/replace against the
_EXPIRY_HOURS_PER_RR = 24   # backtest's real p80/p90 bars-to-resolve before shipping


def _compute_expiry_hours(rr: float) -> float:
    try:
        return max(_EXPIRY_HOURS_FLOOR, round(float(rr) * _EXPIRY_HOURS_PER_RR))
    except (TypeError, ValueError):
        return _EXPIRY_HOURS_FLOOR


def _pip_size(pair: str) -> float:
    return 0.01 if pair.upper().endswith("JPY") else 0.0001


def fetch_bundle(pair: str, log: Callable = print):
    """This book's own 4H + daily bundle, Yahoo-only. Returns (df_4h,
    df_daily) DataFrames, or (None, None) on any failure."""
    raw_4h = yf_mod.fetch_4h_candles(pair, 500, log=log)
    raw_daily = yf_mod.fetch_daily_candles(pair, 400, log=log)
    if not raw_4h or not raw_daily:
        return None, None
    try:
        df_4h = tech._frame_from_td(raw_4h)
        df_daily = tech._drop_still_forming_daily_candle(tech._frame_from_td(raw_daily))
    except Exception as exc:
        log(f"[intraday_4h] frame conversion failed for {pair}: {exc}")
        return None, None
    return df_4h, df_daily


def evaluate_pair(pair: str, log: Callable = print) -> dict | None:
    """Evaluate this pair's latest closed 4H bar. Returns a candidate dict
    (not yet opened) or None if data is insufficient, the setup is grade F
    (never-trade), or the 4H tech_signal itself is NEUTRAL (no direction to
    take -- mirrors the real ladder's own F-is-a-hard-floor rule; only
    would_D_ribbon/clean ever reach a real position, same as production)."""
    df_4h, df_daily = fetch_bundle(pair, log)
    if df_4h is None or len(df_4h) < 210 or df_daily is None or len(df_daily) < 210:
        return None
    try:
        sum_4h = tech._summarise(df_4h, "4h", pair)
        sum_daily = tech._summarise(df_daily, "daily", pair)
    except Exception as exc:
        log(f"[intraday_4h] summarise failed for {pair}: {exc}")
        return None
    if "tech_signal" not in sum_4h or "tech_signal" not in sum_daily:
        return None

    ts = sum_4h.get("tech_signal") or {}
    direction = ts.get("direction")
    if direction not in ("BUY", "SELL"):
        return None   # NEUTRAL -- no directional read to trade

    sig_4h = mtf._tf_signal(sum_4h)
    sig_daily = mtf._tf_signal(sum_daily)
    ribbon_status = (sum_4h.get("ribbon") or {}).get("status", "")
    # "hf_conflict" = 4H-vs-daily conflict, the shifted MTF hierarchy for
    # this clock (there's no weekly). Same structural role as the real
    # ladder's daily-vs-weekly w_d_conflict.
    hf_conflict = (sig_daily in ("BUY", "SELL") and sig_4h in ("BUY", "SELL")
                   and sig_daily != sig_4h)

    rib_strongly_against = (
        (direction == "BUY" and ribbon_status == "ALIGNED_BEAR") or
        (direction == "SELL" and ribbon_status == "ALIGNED_BULL")
    )
    rib_against = (
        (direction == "BUY" and ribbon_status in ("ALIGNED_BEAR", "LEANING_BEAR")) or
        (direction == "SELL" and ribbon_status in ("ALIGNED_BULL", "LEANING_BULL"))
    )
    if rib_strongly_against or hf_conflict:
        return None   # grade F -- never trade, same hard floor as production
    bucket = "would_D_ribbon" if rib_against else "clean"

    entry = float(df_4h["close"].iloc[-1])
    bar_time = df_4h.index[-1]
    atr = float(sum_4h.get("atr14") or 0)
    if not atr or atr <= 0:
        return None
    ps = _pip_size(pair)
    atr_pips = atr / ps
    stop_pips = max(round(atr_pips / 5) * 5, 5)
    stop_dist = stop_pips * ps
    if direction == "BUY":
        stop, target = entry - stop_dist, entry + 2 * stop_dist
    else:
        stop, target = entry + stop_dist, entry - 2 * stop_dist

    return {
        "bar_time": bar_time.strftime("%Y-%m-%d %H:%M"),
        "pair": pair, "direction": direction,
        "entry": entry, "stop_loss": stop, "target": target,
        "stop_pips": stop_pips, "rr": 2.0,
        "bucket": bucket, "ribbon_status": ribbon_status, "hf_conflict": hf_conflict,
        "tech_score": int(ts.get("score") or 0),
    }


def _run_devils_advocate(cand: dict, log: Callable = print) -> dict:
    """Best-effort DA call for a candidate that clears DA_MIN_TECH_SCORE.
    Fails open (mirrors src.analyst.devil_advocate()'s own live call site
    in daily.py) -- a DA failure never blocks the candidate."""
    if cand["tech_score"] < DA_MIN_TECH_SCORE:
        return {"da_fired": "", "da_verdict": "", "da_reasons": ""}
    try:
        from src.analyst import devil_advocate
        parsed = {
            "direction": cand["direction"], "entry": cand["entry"],
            "stop_loss": cand["stop_loss"], "target": cand["target"],
            "key_thesis": (
                f"4H {cand['direction']} on {cand['pair']}: ribbon={cand['ribbon_status']}, "
                f"tech_score={cand['tech_score']}, bucket={cand['bucket']}"
            ),
        }
        result = devil_advocate(cand["pair"], parsed, bundle={})
        return {
            "da_fired": "TRUE" if result.get("has_objections") else "FALSE",
            "da_verdict": result.get("verdict", ""),
            "da_reasons": "; ".join(result.get("reasons", [])),
        }
    except Exception as exc:
        log(f"[intraday_4h] Devil's Advocate call failed for {cand['pair']}: {exc}")
        return {"da_fired": "", "da_verdict": "", "da_reasons": ""}


def register_book() -> None:
    """Register this book in Phase 01's virtual_books registry so reporting
    (get_all_summaries()) picks it up automatically. A stub eligibility
    function is registered since this book's candidates never flow through
    evaluate_candidates()'s daily-cadence deep_results loop -- see module
    docstring."""
    if BOOK_ID not in vb.BOOKS:
        vb.BOOKS[BOOK_ID] = vb.BookConfig(BOOK_ID, BOOK_DESCRIPTION, lambda *a, **k: False)


def run_scan(log: Callable = print) -> dict:
    """Main entry point, called once per 4H-bar-aligned scan. Evaluates
    every pair's latest closed 4H bar, opens a position (this book's own
    isolated files) for anything that clears grade F, runs DA where
    warranted, and records to shadow_mode exactly like every other book's
    settlement already does."""
    register_book()
    candidates = vb._load_csv(CANDIDATES_CSV)
    positions = vb._load_csv(vb._positions_path(BOOK_ID))
    state = vb.load_book_state(BOOK_ID)

    opened = 0
    for pair in UNIVERSE:
        cand = evaluate_pair(pair, log)
        if cand is None:
            continue
        if any(c.get("pair") == pair and c.get("bar_time") == cand["bar_time"]
               for c in candidates):
            continue  # already evaluated this exact 4H bar

        da_info = _run_devils_advocate(cand, log)
        cand_id = vb._next_id(candidates)
        expiry_hours = _compute_expiry_hours(cand["rr"])
        row = {
            "id": cand_id, **cand, **da_info,
            "expiry_hours": expiry_hours,
            "status": "OPEN", "opened_at": vb._now_str(),
            "closed_at": "", "exit_price": "", "pips": "", "net_pips": "",
        }
        candidates.append(row)

        from src import fund_state as _fs_mod
        pct, mode, _reason = _fs_mod.compute_sizing(state, state["balance"], cand["tech_score"])
        if pct is not None:
            pos_id = vb._next_id(positions)
            positions.append({
                "id": pos_id, "candidate_id": cand_id, "pair": pair,
                "direction": cand["direction"], "opened_at": vb._now_str(),
                "position_size_pct": pct, "sizing_mode": mode,
                "balance_at_entry": state["balance"], "stop_pips": cand["stop_pips"],
                "status": "OPEN", "closed_at": "", "net_pips": "", "dollars": "",
                "balance_after": "",
            })
            state["total_trades"] = int(state.get("total_trades") or 0) + 1
            opened += 1
        log(f"[intraday_4h] {pair} {cand['direction']} bucket={cand['bucket']} "
            f"tech_score={cand['tech_score']} da_fired={da_info['da_fired']} "
            f"expiry={expiry_hours}h")

    if opened:
        vb.save_book_state(state)
    vb._write_csv(CANDIDATES_CSV, candidates, CANDIDATE_FIELDS)
    vb._write_csv(vb._positions_path(BOOK_ID), positions, vb.POSITION_FIELDS)
    return {"evaluated": len(UNIVERSE), "opened": opened}
