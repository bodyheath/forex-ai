"""Virtual book system — parallel rule-configuration backtesting at zero extra
LLM/API cost.

Why this exists: this is a paper/simulation system (confirmed no broker
integration anywhere — see src/broker.py) costing ~NZD $0.50/day to run, with
no real capital at stake. Every scan currently produces exactly one fund
decision under the real rules; testing a different rule set means changing
the real rule and waiting weeks for a fresh sample. This module runs several
alternate rule configurations ("books") alongside the real decision, every
scan, each with its own fully independent simulated balance, position
sizing, peak, and drawdown — not just a would-fire/wouldn't-fire log (that
already exists for single boolean rules, see src/shadow_mode.py; this is for
whole alternate portfolios).

Design:
  - `candidates.csv` (data/virtual_books/) is a SHARED registry, one row per
    Sonnet-analyzed candidate with valid entry/stop, written ONCE regardless
    of how many books end up taking it. Entry/stop/target (pure 2:1, same
    mechanical rule real trades use — see src/cascade.py) are identical for
    every book that takes a given candidate, so the underlying outcome only
    needs to be tracked once and shared, not duplicated per book.
  - Each book has its own `<book_id>_state.json` (balance/peak/drawdown/
    streaks) and `<book_id>_positions.csv` (which candidates it took, at
    what size, and the resulting P&L) — fully independent portfolios.
  - Settlement (checking live prices against open candidates, closing them,
    updating every book that holds a position in a closed candidate) reuses
    the same on-disk price cache (`financials.load_prices()`) monitor.py
    already reads for real/research trades — no new network calls.

A new book is a `BookConfig` entry in `BOOKS` below plus an eligibility
function; nothing else in this module changes. This module never reads or
writes trades.csv, fund_state.json, or risk_profile.json — it has no way to
affect the real fund's decisions or state.
"""
from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

import config
from src.trading import financials
from src import fund_state as _fs_mod
from src import cascade
from src import trade_costs

VBOOKS_DIR = config.DATA_DIR / "virtual_books"
CANDIDATES_CSV = VBOOKS_DIR / "candidates.csv"

STARTING_BALANCE = 10_000.0
EXPIRY_DAYS = 14  # matches research_tracker.py's research-trade expiry window

CANDIDATE_FIELDS = [
    "id", "date", "scan_mode", "pair", "direction",
    "entry", "stop_loss", "t2_price",
    "confidence", "eff_conf", "grade", "da_grade_before", "rr",
    "mtf_agreeing_count",
    "status",            # OPEN | WIN | LOSS | EXPIRED
    "opened_at", "closed_at", "exit_price", "pips", "net_pips",
]

POSITION_FIELDS = [
    "id", "candidate_id", "pair", "direction", "opened_at",
    "position_size_pct", "sizing_mode", "balance_at_entry", "stop_pips",
    "status",             # OPEN | CLOSED
    "closed_at", "net_pips", "dollars", "balance_after",
]


# ─── Generic CSV helpers (mirrors tracker.py's own atomic-rewrite pattern) ────

def _load_csv(path: Path) -> list:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_csv(path: Path, rows: list, fields: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in fields})
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _next_id(rows: list) -> int:
    ids = [int(r["id"]) for r in rows if str(r.get("id", "")).isdigit()]
    return (max(ids) + 1) if ids else 1


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _pip_size(pair: str) -> float:
    return 0.01 if "JPY" in (pair or "").upper() else 0.0001


# ─── Book registry ─────────────────────────────────────────────────────────
#
# Each eligibility function takes:
#   r               -- one deep_results candidate dict (parsed/bundle/pair)
#   quality_grades  -- {pair: grade_dict} from daily.py's real _trade_quality_grade()
#                       pass, already including the real Devil's Advocate fold-in
#   dd_mode         -- the real scan's drawdown-tier string ("normal"/"caution"/...)
#   conf_threshold  -- the real scan's confidence threshold
#   eff_conf_fn     -- daily.py's _eff_conf, injected so this module never
#                       needs to import daily.py (which is a script, not a library)
#   dd_allows_fn    -- daily.py's _dd_allows_trade, injected for the same reason.
#                       It is a pure function of its arguments (confirmed: no
#                       side effects besides an optional log_fn call), so it is
#                       safe to call once per book per candidate without any
#                       risk of affecting the real decision it was also used for.
#
# Reasoning behind each book's implementation is in the docstring of its
# eligibility function -- these are judgment calls on an intentionally
# ambiguous brief, documented so they're easy to correct in review.

def _elig_a_control(r, quality_grades, dd_mode, conf_threshold, eff_conf_fn, dd_allows_fn) -> bool:
    """Book A: byte-for-byte the real gate. Should reproduce the real
    yes_trades set exactly -- this is also this module's own smoke test."""
    return dd_allows_fn(r, dd_mode, quality_grades, conf_threshold, log_fn=lambda m: None)


def _elig_b_flat_conf_rr(r, quality_grades, dd_mode, conf_threshold, eff_conf_fn, dd_allows_fn) -> bool:
    """Book B: conf>=6, RR>=1.5 -- a flat, grade-independent bar, not "Book A
    with a lower threshold". The risk-tier restrictions above "normal"
    (halt/preservation/defensive/caution) are kept as real risk management,
    not part of what's being tested -- only the "normal" tier's eligibility
    condition is replaced. Grade F remains a hard floor even here (it flags
    structural facts -- atr=0, ribbon strongly against, weekly/daily
    conflict -- not a confidence judgement, so loosening the confidence bar
    doesn't argue for admitting these too)."""
    grade = (quality_grades.get(r["pair"]) or {}).get("grade", "F")
    rr    = (quality_grades.get(r["pair"]) or {}).get("rr", 0.0)
    if dd_mode == "halt":
        return False
    if dd_mode == "preservation":
        mtf = (r.get("bundle") or {}).get("mtf") or {}
        return grade == "A" and mtf.get("agreeing_count", 0) >= 3
    if dd_mode == "defensive":
        return grade == "A"
    if dd_mode == "caution":
        return grade in ("A", "B")
    return grade != "F" and eff_conf_fn(r) >= 6 and rr >= 1.5


def _elig_c_grade_based(r, quality_grades, dd_mode, conf_threshold, eff_conf_fn, dd_allows_fn) -> bool:
    """Book C: grade A/B/C required instead of confidence-based eligibility.
    Reuses the real _dd_allows_trade() unmodified with conf_threshold=0 --
    since effective confidence is always >= 0, the "normal" tier's
    `grade in (B,C) and eff_conf>=conf_threshold` condition collapses to
    just `grade in (B,C)`, making grade the sole normal-tier gate while every
    other tier (halt/preservation/defensive/caution) -- and R:R>=2.0, which
    is already baked into what qualifies for each grade -- is untouched."""
    return dd_allows_fn(r, dd_mode, quality_grades, 0, log_fn=lambda m: None)


def _elig_d_no_da(r, quality_grades, dd_mode, conf_threshold, eff_conf_fn, dd_allows_fn) -> bool:
    """Book D: identical to control but with Devil's Advocate's fold-in
    disabled. _trade_quality_grade() already records the pre-DA grade as
    da_grade_before on every graded candidate -- swapping that in for
    "grade" and reusing the real gate needs no grade recomputation."""
    qg_no_da = {
        pair: {**qg, "grade": qg.get("da_grade_before", qg.get("grade", "F"))}
        for pair, qg in quality_grades.items()
    }
    return dd_allows_fn(r, dd_mode, qg_no_da, conf_threshold, log_fn=lambda m: None)


def _elig_e_no_dd_gate(r, quality_grades, dd_mode, conf_threshold, eff_conf_fn, dd_allows_fn) -> bool:
    """Book E: identical to control but with drawdown-tier gating removed --
    forces dd_mode to "normal" (the least-restrictive tier) regardless of
    the real scan's actual tier, leaving grade/confidence eligibility
    otherwise unchanged."""
    return dd_allows_fn(r, "normal", quality_grades, conf_threshold, log_fn=lambda m: None)


def _elig_f_sentiment_only(r, quality_grades, dd_mode, conf_threshold, eff_conf_fn, dd_allows_fn) -> bool:
    """Book F: trades purely on the Sentiment Agent's verdict (2026-09-06,
    Phase 01B specialist #3) -- ignores grade/dd_mode/conf_threshold/eff_conf
    entirely, unlike Books B-E which each vary exactly one dimension of an
    otherwise-real gate. This book exists specifically to test the signal in
    total isolation, so it deliberately does NOT inherit even Books B-E's
    one shared carve-out (respecting dd_mode=="halt" as real risk
    management) -- there is no real capital at stake in any virtual book
    regardless, and diluting "fully isolated" with a partial technical
    carve-out would defeat the point of this specific book.

    Reads the SAME memoized verdict src.sentiment_agent.get_or_evaluate()
    already computed for this candidate during research-trade logging
    earlier this scan (see daily.py's _log_one_research()) -- never a
    second LLM call for the same candidate."""
    try:
        from src import sentiment_agent as _sa
        direction = (r.get("parsed") or {}).get("direction", "")
        result = _sa.get_or_evaluate(r.get("pair", ""), direction)
        return _sa.would_fire(result)
    except Exception:
        return False


@dataclass(frozen=True)
class BookConfig:
    book_id: str
    description: str
    eligibility: Callable


BOOKS: dict[str, BookConfig] = {
    "A_control": BookConfig(
        "A_control",
        "Current real rules, unchanged (control)",
        _elig_a_control,
    ),
    "B_conf6_rr15": BookConfig(
        "B_conf6_rr15",
        "Flat conf>=6, RR>=1.5 eligibility (grade-independent)",
        _elig_b_flat_conf_rr,
    ),
    "C_grade_based": BookConfig(
        "C_grade_based",
        "Grade A/B/C required instead of confidence-based eligibility",
        _elig_c_grade_based,
    ),
    "D_no_da": BookConfig(
        "D_no_da",
        "Control with Devil's Advocate fold-in disabled",
        _elig_d_no_da,
    ),
    "E_no_dd_gate": BookConfig(
        "E_no_dd_gate",
        "Control with drawdown-tier gating removed",
        _elig_e_no_dd_gate,
    ),
}


# ─── Per-book state ──────────────────────────────────────────────────────────

def _state_path(book_id: str) -> Path:
    return VBOOKS_DIR / f"{book_id}_state.json"


def _positions_path(book_id: str) -> Path:
    return VBOOKS_DIR / f"{book_id}_positions.csv"


def _default_state(book_id: str, description: str) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "book_id": book_id,
        "description": description,
        "balance": STARTING_BALANCE,
        "peak_balance": STARTING_BALANCE,
        "opening_balance": STARTING_BALANCE,
        "daily_opening_balance": STARTING_BALANCE,
        "daily_trades_date": today,
        "current_drawdown_pct": 0.0,
        "max_drawdown_seen": 0.0,
        "consecutive_wins": 0,
        "consecutive_losses": 0,
        "drawdown_paused": False,
        "circuit_breaker_active": False,
        "pause_until": None,
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "last_updated": _now_str(),
    }


def load_book_state(book_id: str) -> dict:
    path = _state_path(book_id)
    cfg = BOOKS[book_id]
    if not path.exists():
        return _default_state(book_id, cfg.description)
    try:
        import json
        state = json.loads(path.read_text(encoding="utf-8"))
        defaults = _default_state(book_id, cfg.description)
        for k, v in defaults.items():
            state.setdefault(k, v)
        return state
    except Exception:
        return _default_state(book_id, cfg.description)


def save_book_state(state: dict) -> None:
    state["last_updated"] = _now_str()
    financials.atomic_write_json(_state_path(state["book_id"]), state)


# ─── Candidate registration + per-book position opening ─────────────────────

def _find_today_candidate(rows: list, pair: str, direction: str, date_str: str) -> Optional[dict]:
    for row in rows:
        if (row.get("date") == date_str and row.get("pair") == pair
                and row.get("direction") == direction):
            return row
    return None


def evaluate_candidates(
    deep_results: list,
    quality_grades: dict,
    dd_mode: str,
    conf_threshold: float,
    eff_conf_fn: Callable,
    dd_allows_fn: Callable,
    scan_mode: str,
    date_str: str,
    log_fn: Callable = None,
) -> dict:
    """Main hook, called once per scan from daily.py, right after the real
    _quality_grades/_dd_mode are established and before the real fund-loop
    starts. Costs zero LLM/API calls: every input is already computed for
    the real decision. Returns a summary dict for one optional log line.
    """
    _log = log_fn or (lambda m: None)
    candidates = _load_csv(CANDIDATES_CSV)
    new_candidates = 0
    opened_by_book = {bid: 0 for bid in BOOKS}

    for r in deep_results:
        parsed = r.get("parsed") or {}
        if parsed.get("_early_reject"):
            continue
        pair = r.get("pair", "")
        direction = (parsed.get("direction") or "").upper()
        try:
            entry = float(parsed.get("entry") or 0)
            stop = float(parsed.get("stop_loss") or 0)
        except (TypeError, ValueError):
            entry = stop = 0.0
        if not (entry and stop and direction in ("BUY", "SELL")):
            continue

        existing = _find_today_candidate(candidates, pair, direction, date_str)
        if existing is not None:
            candidate_id = int(existing["id"])
            candidate_row = existing
        else:
            target_raw = float(parsed.get("target") or 0)
            _, t2_price, _ = cascade.compute_levels(entry, stop, target_raw, direction)
            if t2_price is None:
                continue
            qg = quality_grades.get(pair) or {}
            mtf = (r.get("bundle") or {}).get("mtf") or {}
            candidate_id = _next_id(candidates)
            candidate_row = {
                "id": candidate_id,
                "date": date_str,
                "scan_mode": scan_mode,
                "pair": pair,
                "direction": direction,
                "entry": entry,
                "stop_loss": stop,
                "t2_price": t2_price,
                "confidence": parsed.get("confidence", ""),
                "eff_conf": round(eff_conf_fn(r), 2),
                "grade": qg.get("grade", ""),
                "da_grade_before": qg.get("da_grade_before", ""),
                "rr": qg.get("rr", ""),
                "mtf_agreeing_count": mtf.get("agreeing_count", ""),
                "status": "OPEN",
                "opened_at": _now_str(),
                "closed_at": "",
                "exit_price": "",
                "pips": "",
                "net_pips": "",
            }
            candidates.append(candidate_row)
            new_candidates += 1

        if candidate_row.get("status") != "OPEN":
            continue  # already resolved same-day re-analysis; don't reopen

        stop_pips = abs(entry - stop) / _pip_size(pair)
        if stop_pips <= 0:
            continue

        for book_id, cfg in BOOKS.items():
            try:
                allowed = cfg.eligibility(r, quality_grades, dd_mode, conf_threshold,
                                          eff_conf_fn, dd_allows_fn)
            except Exception as exc:
                _log(f"[vbook:{book_id}] eligibility check failed for {pair}: {exc}")
                continue
            if not allowed:
                continue

            positions = _load_csv(_positions_path(book_id))
            if any(p.get("candidate_id") == str(candidate_id) for p in positions):
                continue  # this book already has a position in this candidate

            state = load_book_state(book_id)
            pct, mode, _reason = _fs_mod.compute_sizing(state, state["balance"], eff_conf_fn(r))
            if pct is None:
                continue  # blocked by this book's own checklist-score/drawdown tier gate

            pos_id = _next_id(positions)
            positions.append({
                "id": pos_id,
                "candidate_id": candidate_id,
                "pair": pair,
                "direction": direction,
                "opened_at": _now_str(),
                "position_size_pct": pct,
                "sizing_mode": mode,
                "balance_at_entry": state["balance"],
                "stop_pips": round(stop_pips, 1),
                "status": "OPEN",
                "closed_at": "",
                "net_pips": "",
                "dollars": "",
                "balance_after": "",
            })
            _write_csv(_positions_path(book_id), positions, POSITION_FIELDS)
            state["total_trades"] = int(state.get("total_trades") or 0) + 1
            save_book_state(state)
            opened_by_book[book_id] += 1

    if new_candidates or any(opened_by_book.values()):
        _write_csv(CANDIDATES_CSV, candidates, CANDIDATE_FIELDS)

    return {"new_candidates": new_candidates, "opened_by_book": opened_by_book}


# ─── Settlement (called from monitor.py) ────────────────────────────────────

def _classify_close(net_pips: float, hit: str) -> str:
    if hit == "TARGET":
        return "WIN"
    if hit == "STOP":
        return "LOSS"
    # EXPIRED -- classify by net_pips sign, consistent with this session's
    # own PARTIAL_WIN/EXPIRED net-pips-sign fix elsewhere in the codebase
    # (risk_manager._is_win_outcome, dynamic_threshold._decisive_bucket).
    return "WIN" if net_pips > 0 else ("LOSS" if net_pips < 0 else "EXPIRED")


def settle_open_candidates(log_fn: Callable = None, prices: dict = None) -> dict:
    """Called once per monitor run. Checks every OPEN candidate for a
    target/stop hit or 14-day expiry, closes it, then settles every book
    holding an open position in that candidate against its own balance.
    Never touches trades.csv, research_trades.csv, or fund_state.json.

    `prices`: pass monitor.py's own just-fetched in-memory price dict (the
    same one it uses for real/research trade milestone checks) so this never
    settles against a stale disk cache. Falls back to
    financials.load_prices() (a passive on-disk cache read, no network call)
    when called standalone -- e.g. from a script or test.
    """
    _log = log_fn or (lambda m: None)
    candidates = _load_csv(CANDIDATES_CSV)
    if not candidates:
        return {"closed": 0}

    if prices is None:
        prices = financials.load_prices()
    # naive UTC, matching opened_at's strptime (no tz) below -- avoids
    # "can't subtract offset-naive and offset-aware datetimes"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    closed_count = 0

    for row in candidates:
        if row.get("status") != "OPEN":
            continue
        pair = row.get("pair", "")
        direction = row.get("direction", "")
        entry = float(row.get("entry") or 0)
        stop = float(row.get("stop_loss") or 0)
        target = float(row.get("t2_price") or 0)
        price = financials.get_price(prices, pair)

        hit = None
        exit_price = None
        try:
            opened = datetime.strptime(row.get("opened_at", "")[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            opened = now
        expired = (now - opened) > timedelta(days=EXPIRY_DAYS)

        if price is not None:
            if direction == "BUY":
                if price >= target:
                    hit, exit_price = "TARGET", target
                elif price <= stop:
                    hit, exit_price = "STOP", stop
            else:  # SELL
                if price <= target:
                    hit, exit_price = "TARGET", target
                elif price >= stop:
                    hit, exit_price = "STOP", stop

        if hit is None and expired:
            hit, exit_price = "EXPIRED", (price if price is not None else entry)

        if hit is None:
            continue

        pips = cascade.pips_at(entry, exit_price, pair, direction) or 0.0
        try:
            days_held = max(0.0, (now - opened).total_seconds() / 86400.0)
            net_pips = trade_costs.net_pips_for_closed_trade(
                pair, direction, entry, pips, days_held,
            )
        except Exception:
            net_pips = pips

        status = _classify_close(net_pips, hit)
        row["status"] = status
        row["closed_at"] = _now_str()
        row["exit_price"] = exit_price
        row["pips"] = round(pips, 1)
        row["net_pips"] = round(net_pips, 1)
        closed_count += 1
        _log(f"[vbook] candidate #{row['id']} {pair} {direction} closed {status} "
             f"({net_pips:+.1f}p net, hit={hit})")

        _settle_book_positions(int(row["id"]), net_pips, status, _log)

    if closed_count:
        _write_csv(CANDIDATES_CSV, candidates, CANDIDATE_FIELDS)

    return {"closed": closed_count}


def _settle_book_positions(candidate_id: int, net_pips: float, status: str, log_fn: Callable) -> None:
    for book_id in BOOKS:
        positions = _load_csv(_positions_path(book_id))
        touched = False
        for pos in positions:
            if pos.get("candidate_id") != str(candidate_id) or pos.get("status") != "OPEN":
                continue
            touched = True
            balance_at_entry = float(pos.get("balance_at_entry") or STARTING_BALANCE)
            pct = float(pos.get("position_size_pct") or 0)
            stop_pips = float(pos.get("stop_pips") or 0)
            dpp = financials.calculate_dpp(balance_at_entry, pct, stop_pips)
            dollars = round(net_pips * dpp, 2) if dpp else 0.0

            state = load_book_state(book_id)
            new_balance = state["balance"] + dollars
            state["balance"] = round(new_balance, 2)
            state["peak_balance"] = round(max(state["peak_balance"], new_balance), 2)
            dd_pct = (
                (state["peak_balance"] - new_balance) / state["peak_balance"] * 100
                if state["peak_balance"] > 0 else 0.0
            )
            state["current_drawdown_pct"] = round(dd_pct, 4)
            state["max_drawdown_seen"] = round(max(state.get("max_drawdown_seen", 0.0), dd_pct), 4)

            if net_pips > 0:
                state["consecutive_wins"] = int(state.get("consecutive_wins") or 0) + 1
                state["consecutive_losses"] = 0
                state["wins"] = int(state.get("wins") or 0) + 1
            elif net_pips < 0:
                state["consecutive_losses"] = int(state.get("consecutive_losses") or 0) + 1
                state["consecutive_wins"] = 0
                state["losses"] = int(state.get("losses") or 0) + 1
            # net_pips == 0 (EXPIRED/breakeven): streaks unchanged, matching
            # calculate_fund_state()'s own convention for a zero-pip close.

            save_book_state(state)

            pos["status"] = "CLOSED"
            pos["closed_at"] = _now_str()
            pos["net_pips"] = round(net_pips, 1)
            pos["dollars"] = dollars
            pos["balance_after"] = state["balance"]
            log_fn(f"[vbook:{book_id}] candidate #{candidate_id} settled {status} "
                   f"{dollars:+.2f} -> balance ${state['balance']:,.2f}")

            # 2026-09-04: auto-feed every book's real settled outcomes into
            # shadow_mode.py, so evidence accumulates automatically as soon
            # as a book runs -- "mandatory by construction," not dependent on
            # anyone remembering to wire this in before proposing a book's
            # config as a real fund-rule change. Deliberately would_fire=True
            # only (this book DID take this trade) -- would_fire=False
            # (candidates this book rejected) is NOT logged here: recreating
            # that retroactively would need this scan's dd_mode, which isn't
            # persisted per-candidate (CANDIDATE_FIELDS has no dd_mode
            # column), and reconstructing it unsafely risked silently
            # misrepresenting a book's real eligibility logic -- worse than
            # not logging it. What accumulates here is still genuinely
            # useful: this book's own decisive-outcome distribution, ready to
            # compare against another book's own would_fire=True distribution
            # at promotion-check time. Never affects any real decision --
            # failure here must never break real settlement.
            try:
                from src import shadow_mode as _sm
                _sm.register_rule(
                    f"vbook_{book_id}",
                    description=f"Virtual book {book_id}: {BOOKS[book_id].description}",
                )
                _sm.record_evaluation(
                    f"vbook_{book_id}", would_fire=True, outcome=status, net_pips=net_pips,
                    context={"candidate_id": candidate_id, "pair": pos.get("pair"),
                             "direction": pos.get("direction")},
                )
            except Exception as _sm_exc:
                log_fn(f"[vbook:{book_id}] shadow_mode logging failed (non-fatal): {_sm_exc}")

        if touched:
            _write_csv(_positions_path(book_id), positions, POSITION_FIELDS)


def get_open_candidate_pairs() -> list:
    """Pairs with at least one OPEN candidate -- for monitor.py to fold into
    its own price-fetch batch, so an open virtual candidate always gets a
    fresh price even on a scan where every real/research trade on that pair
    has already closed."""
    candidates = _load_csv(CANDIDATES_CSV)
    return sorted({c["pair"] for c in candidates if c.get("status") == "OPEN" and c.get("pair")})


# ─── Reporting ───────────────────────────────────────────────────────────────

def get_book_summary(book_id: str) -> dict:
    """Balance/drawdown/win-rate snapshot for one book -- for a health_check
    or reporting pass. Reads only this module's own files."""
    state = load_book_state(book_id)
    positions = _load_csv(_positions_path(book_id))
    closed = [p for p in positions if p.get("status") == "CLOSED"]
    wins = int(state.get("wins") or 0)
    losses = int(state.get("losses") or 0)
    decisive = wins + losses
    return {
        "book_id": book_id,
        "description": BOOKS[book_id].description,
        "balance": state["balance"],
        "peak_balance": state["peak_balance"],
        "current_drawdown_pct": state["current_drawdown_pct"],
        "max_drawdown_seen": state["max_drawdown_seen"],
        "total_trades": int(state.get("total_trades") or 0),
        "open_positions": len(positions) - len(closed),
        "closed_positions": len(closed),
        "wins": wins,
        "losses": losses,
        "decisive": decisive,
        "win_rate": round(wins / decisive * 100, 1) if decisive > 0 else 0.0,
        "return_pct": round((state["balance"] - STARTING_BALANCE) / STARTING_BALANCE * 100, 2),
    }


def get_all_summaries() -> dict:
    return {book_id: get_book_summary(book_id) for book_id in BOOKS}
