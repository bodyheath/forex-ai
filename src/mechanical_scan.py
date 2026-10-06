"""Mechanical-only forward test of the Book G/H/I signals -- no LLM anywhere.

WHY THIS EXISTS (2026-10-06 cost/evidence review): the only edge this project
has ever validated (chronological holdout + cluster bootstrap) is mechanical --
ribbon / oscillator / Bollinger features computed from daily bars. The live
virtual books G/H/I, however, only see candidates the LLM pipeline proposes
(direction, entry and stop all come from the model's parsed output), so (a)
they cost LLM spend to feed, (b) they trade "signal AND the LLM's chosen
direction", which is not the population the backtest validated, and (c) their
model-supplied entry prices produce instant stop-outs (20% of settled
candidates were stopped within an hour -- the fill happens at the model's
stated entry whatever the market is doing). This module is the clean forward
test of what was actually backtested.

WHAT IT DOES, once per day after the daily bars have closed:
  1. Fetches daily bars for the 28-pair universe through the same
     Yahoo -> Stooq -> Twelve Data chain the live system uses
     (technical._td_request), drops a still-forming bar, and computes features
     with the SAME technical._summarise() the live bundle and the backtest use,
     on the same 261-bar window the backtest used.
  2. For every pair, BOTH directions, creates one candidate: entry = last
     closed bar's close, stop = ATR-derived (mechanical_reversion.
     compute_mechanical_levels, identical to the backtest), target = 2R.
  3. Evaluates the live G/H/I eligibility functions (src/virtual_books.py's
     _elig_g/h/i -- imported, not re-implemented, so the rule cannot drift)
     and stores the fire/no-fire flags.
  4. Settles open candidates against subsequent COMPLETED daily bars exactly as
     scripts/mechanical_edge_mining_dataset.py does: stop checked before
     target within a bar, 4-bar expiry at the 4th bar's close, EXPIRED
     classified by net-pips sign, net pips via trade_costs. (An equivalence
     test replays the backtest's own evaluate_pair_rich() against this engine
     on the same bars -- tests/test_mechanical_scan.py.)
  5. Records each settled candidate into a cluster-aware shadow_mode rule per
     book, tagged with a regime cluster (same gap tolerance as Book G's live
     tracker), so promotion uses the same cluster-bootstrap bar as G/H/I.

ISOLATION (deliberate): everything lives under data/mechanical_path/. It does
not read or write data/virtual_books/*, trades.csv, research_trades.csv,
fund_state.json, or the shared data/shadow_rules.json (it points shadow_mode
at its own file for the duration of a call, restored in a finally). That keeps
this population from contaminating books A-I's evidence, and lets a workflow
commit only this directory with no lost-update race against other workflows.

NO LIVE EFFECT: nothing here influences a real or virtual-book decision, and
nothing in daily.py/monitor.py imports it.

KNOWN LIMITATIONS, stated up front:
  - Regimes are tracked per (pair, direction, fire-state). Correlated pairs
    (e.g. six USD pairs all firing short-USD) are separate regimes here, so
    the regime count overstates independent evidence; treat the cluster
    bootstrap p-value as optimistic until a currency-level cluster is added.
  - Entry is the last close, as in the backtest -- not a tradeable next-open
    fill. This validates the backtested construction; it is not an execution
    simulation.
  - Daily bars from Yahoo roll at London midnight; a bar is treated as closed
    once its date is before today (UTC), same as the live system.
"""
import argparse
import csv
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import config
from src import mechanical_reversion as _mr
from src import shadow_mode as _sm
from src import technical as _tech
from src import trade_costs
from src import virtual_books as _vb
from src.selector import UNIVERSE

# ─── Storage (all under data/mechanical_path/) ───────────────────────────────

MECH_DIR = config.DATA_DIR / "mechanical_path"
CANDIDATES_CSV = MECH_DIR / "candidates.csv"
SHADOW_FILE = MECH_DIR / "shadow_rules.json"
TRACKER_JSON = MECH_DIR / "regime_tracker.json"

# ─── Construction constants (must match scripts/mechanical_edge_mining_dataset.py)

EXPIRY_BARS = 4
LOOKBACK_BARS = 261          # backtest: df_daily.iloc[i-260 : i+1]
MIN_BARS = 210               # backtest MIN_DAILY_HISTORY
MAX_BACKFILL_BARS = 5        # recover a short outage; never backfill history
MAX_STALE_DAYS = 6           # newest bar older than this => skip the pair
REGIME_GAP_DAYS = _vb._REGIME_GAP_DAYS

# book key -> (virtual_books eligibility function, shadow rule name, description)
BOOKS = {
    "G": (_vb._elig_g_mechanical_reversion, "mech_G_mechanical_reversion",
          "Mechanical-only forward test of Book G's signal (rib_against AND "
          "osc_agrees), every pair x both directions, no LLM."),
    "H": (_vb._elig_h_oscillator_extremity, "mech_H_oscillator_extremity",
          "Mechanical-only forward test of Book H's signal (osc_agrees "
          "alone), every pair x both directions, no LLM."),
    "I": (_vb._elig_i_bollinger_extremity, "mech_I_bollinger_extremity",
          "Mechanical-only forward test of Book I's signal "
          "(bollinger_extreme_agrees), every pair x both directions, no LLM."),
}

CANDIDATE_FIELDS = [
    "id", "signal_date", "pair", "direction", "entry", "stop_loss", "target",
    "stop_pips", "atr14", "ribbon_status", "osc_direction", "bb_position",
    "fire_G", "fire_H", "fire_I", "cluster_G", "cluster_H", "cluster_I",
    "status", "exit_reason", "exit_date", "exit_price", "gross_pips",
    "net_pips", "days_held", "created_at", "settled_at", "shadow_recorded",
]


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _pip_size(pair: str) -> float:
    # identical rule to the backtest's _pip_size
    return 0.01 if pair.upper().endswith("JPY") else 0.0001


# ─── CSV persistence (csv module only -- never pandas round-trips) ───────────

def load_candidates() -> list:
    if not CANDIDATES_CSV.exists():
        return []
    with open(CANDIDATES_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _save_candidates(rows: list) -> None:
    MECH_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(MECH_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CANDIDATE_FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        os.replace(tmp, CANDIDATES_CSV)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _load_tracker() -> dict:
    try:
        return json.loads(TRACKER_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_tracker(tracker: dict) -> None:
    MECH_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(MECH_DIR), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(tracker, f, indent=1, sort_keys=True)
    os.replace(tmp, TRACKER_JSON)


def assign_cluster(tracker: dict, book: str, pair: str, direction: str,
                   fire: bool, signal_date: str) -> str:
    """Regime cluster id: a maximal run of consecutive signal dates (gap <=
    REGIME_GAP_DAYS calendar days) sharing the same (book, pair, direction,
    fire-state). Mutates `tracker`. Candidates are created in chronological
    order, so unlike live settlement-order tagging this has no ordering
    caveat."""
    key = f"{book}|{pair}|{direction}|{bool(fire)}"
    d = datetime.strptime(signal_date[:10], "%Y-%m-%d").date()
    entry = tracker.get(key)
    same = False
    if entry:
        try:
            last = datetime.strptime(entry["last_date"], "%Y-%m-%d").date()
            same = 0 <= (d - last).days <= REGIME_GAP_DAYS
        except (ValueError, KeyError):
            same = False
    num = int(entry["regime_num"]) if (entry and same) else (int(entry["regime_num"]) + 1 if entry else 1)
    tracker[key] = {"last_date": signal_date[:10], "regime_num": num}
    return f"{pair}_{direction}_{bool(fire)}_r{num}"


# ─── Pure logic: features, candidates, settlement ────────────────────────────

def _fetch_daily_frame(pair: str) -> pd.DataFrame:
    """Completed daily bars (OHLC, ascending), via the live data chain."""
    data = _tech._td_request(pair, "1day", 400)
    frame = _tech._frame_from_td(data)
    return _tech._drop_still_forming_daily_candle(frame)


def _summarise_bar(pair: str, frame: pd.DataFrame, i: int):
    """The daily feature dict for bar index `i`, computed on exactly the
    window the backtest used. None when there is too little history."""
    if i + 1 < MIN_BARS:
        return None
    window = frame.iloc[max(0, i - (LOOKBACK_BARS - 1)): i + 1]
    try:
        s = _tech._summarise(window, "daily", pair)
    except Exception:
        return None
    if not isinstance(s, dict) or s.get("atr14") in (None, 0) or "tech_signal" not in s:
        return None
    return s


def candidate_rows_for_bar(pair: str, frame: pd.DataFrame, i: int) -> list:
    """Both-direction candidate rows (unsaved, no id/cluster) for bar `i`."""
    s = _summarise_bar(pair, frame, i)
    if s is None:
        return []
    try:
        atr14 = float(s["atr14"])
        entry = float(frame["close"].iloc[i])
    except (TypeError, ValueError):
        return []
    ps = _pip_size(pair)
    date_str = pd.Timestamp(frame.index[i]).strftime("%Y-%m-%d")
    ribbon_status = (s.get("ribbon") or {}).get("status", "")
    osc_direction = (s.get("oscillator_confluence") or {}).get("direction", "NONE")
    rows = []
    for direction in ("BUY", "SELL"):
        lv = _mr.compute_mechanical_levels(entry, atr14, direction, ps)
        if not lv:
            continue
        r = {"pair": pair,
             "parsed": {"direction": direction, "entry": lv["entry"],
                        "stop_loss": lv["stop_loss"], "target": lv["target"]},
             "bundle": {"technical": {"daily": s}}}
        row = {
            "signal_date": date_str, "pair": pair, "direction": direction,
            "entry": repr(lv["entry"]), "stop_loss": repr(lv["stop_loss"]),
            "target": repr(lv["target"]), "stop_pips": lv["stop_pips"],
            "atr14": atr14, "ribbon_status": ribbon_status,
            "osc_direction": osc_direction, "bb_position": s.get("bb_position"),
        }
        for key, (elig, _rule, _desc) in BOOKS.items():
            # the eligibility fns ignore every argument but r (they are
            # deliberately isolated from grade/confidence/dd gates)
            fired = bool(elig(r, {}, "normal", 0, lambda *a, **k: 0.0, lambda *a, **k: True))
            row[f"fire_{key}"] = "1" if fired else "0"
        rows.append(row)
    return rows


def settle_row(row: dict, bars_after: pd.DataFrame):
    """Settle one candidate against the completed bars AFTER its signal bar.

    Mirrors scripts/mechanical_edge_mining_dataset.py exactly: for bar offsets
    1..EXPIRY_BARS, a stop hit is checked BEFORE a target hit in the same bar;
    if neither occurs within EXPIRY_BARS bars the trade expires at that bar's
    close, classified by net-pips sign. Returns a dict of settlement fields,
    or None while still unresolved (fewer than EXPIRY_BARS bars available and
    no hit yet)."""
    direction = row["direction"]
    entry, stop, target = float(row["entry"]), float(row["stop_loss"]), float(row["target"])
    pair = row["pair"]
    ps = _pip_size(pair)
    signal_ts = pd.Timestamp(row["signal_date"])

    outcome = exit_price = exit_ts = None
    for offset in range(1, min(EXPIRY_BARS, len(bars_after)) + 1):
        ts = bars_after.index[offset - 1]
        bar = bars_after.iloc[offset - 1]
        hit_target = (bar["high"] >= target) if direction == "BUY" else (bar["low"] <= target)
        hit_stop = (bar["low"] <= stop) if direction == "BUY" else (bar["high"] >= stop)
        if hit_stop:
            outcome, exit_price, exit_ts, reason = "LOSS", stop, ts, "STOP"
            break
        if hit_target:
            outcome, exit_price, exit_ts, reason = "WIN", target, ts, "TARGET"
            break
    if outcome is None:
        if len(bars_after) < EXPIRY_BARS:
            return None
        ts = bars_after.index[EXPIRY_BARS - 1]
        exit_price = float(bars_after["close"].iloc[EXPIRY_BARS - 1])
        exit_ts, reason = ts, "EXPIRED"

    gross = (exit_price - entry) / ps if direction == "BUY" else (entry - exit_price) / ps
    days_held = max(1.0, float((pd.Timestamp(exit_ts) - signal_ts).days))
    try:
        net = trade_costs.net_pips_for_closed_trade(pair, direction, entry, gross, days_held)
    except Exception:
        net = gross
    if reason == "EXPIRED":
        status = "WIN" if net > 0 else ("LOSS" if net < 0 else "EXPIRED")
    else:
        status = outcome
    return {
        "status": status, "exit_reason": reason,
        "exit_date": pd.Timestamp(exit_ts).strftime("%Y-%m-%d"),
        "exit_price": repr(float(exit_price)), "gross_pips": round(gross, 1),
        "net_pips": round(net, 1), "days_held": days_held,
        "settled_at": _now_str(),
    }


# ─── Shadow-mode feed (own file, restored after use) ─────────────────────────

@contextmanager
def _own_shadow_file():
    MECH_DIR.mkdir(parents=True, exist_ok=True)
    prev = _sm._SHADOW_FILE
    _sm._SHADOW_FILE = SHADOW_FILE
    try:
        yield
    finally:
        _sm._SHADOW_FILE = prev


def _register_rules() -> None:
    for _key, (_elig, rule, desc) in BOOKS.items():
        _sm.register_rule(rule, description=desc, min_n_fire=30, min_n_no_fire=30,
                          alpha=0.05, cluster_aware=True)


def record_settled(rows: list) -> int:
    """Feed every settled-but-unrecorded candidate into the per-book shadow
    rules (fire AND no-fire evaluations). Idempotent via shadow_recorded."""
    todo = [r for r in rows if r["status"] in ("WIN", "LOSS", "EXPIRED") and r.get("shadow_recorded") != "1"]
    if not todo:
        return 0
    n = 0
    with _own_shadow_file():
        _register_rules()
        for r in todo:
            outcome = r["status"]
            try:
                net = float(r["net_pips"])
            except (TypeError, ValueError):
                net = None
            for key, (_elig, rule, _desc) in BOOKS.items():
                _sm.record_evaluation(
                    rule, would_fire=(r[f"fire_{key}"] == "1"), outcome=outcome, net_pips=net,
                    context={"candidate_id": r["id"], "pair": r["pair"],
                             "direction": r["direction"], "signal_date": r["signal_date"],
                             "regime_cluster": r[f"cluster_{key}"]},
                )
            r["shadow_recorded"] = "1"
            n += 1
    return n


# ─── The scan ────────────────────────────────────────────────────────────────

def run_scan(pairs=None, fetch_fn=None, log=print, today=None) -> dict:
    """One mechanical scan: create new candidates, settle open ones, feed the
    shadow rules. Idempotent -- safe to run repeatedly in one day.
    `fetch_fn(pair) -> completed daily frame` is injectable for tests."""
    pairs = list(pairs or UNIVERSE)
    fetch_fn = fetch_fn or _fetch_daily_frame
    today = today or datetime.now(timezone.utc).date()

    rows = load_candidates()
    tracker = _load_tracker()
    next_id = max([int(r["id"]) for r in rows] + [0]) + 1
    seen = {(r["pair"], r["direction"], r["signal_date"]) for r in rows}
    last_by_pair = {}
    for r in rows:
        if r["signal_date"] > last_by_pair.get(r["pair"], ""):
            last_by_pair[r["pair"]] = r["signal_date"]

    created = settled = 0
    failures, stale = [], []

    for pair in pairs:
        try:
            frame = fetch_fn(pair)
        except Exception as exc:
            log(f"[mechanical] {pair}: fetch failed ({exc})")
            failures.append(pair)
            continue
        if frame is None or len(frame) < MIN_BARS:
            log(f"[mechanical] {pair}: only {0 if frame is None else len(frame)} bars -- skipped")
            failures.append(pair)
            continue
        newest = pd.Timestamp(frame.index[-1]).date()
        if (today - newest).days > MAX_STALE_DAYS:
            log(f"[mechanical] {pair}: newest bar {newest} is stale -- skipped")
            stale.append(pair)
            continue

        prior = last_by_pair.get(pair)
        if prior is None:
            idxs = [len(frame) - 1]                      # first run: latest bar only
        else:
            prior_ts = pd.Timestamp(prior)
            idxs = [i for i in range(len(frame)) if pd.Timestamp(frame.index[i]) > prior_ts]
            idxs = idxs[-MAX_BACKFILL_BARS:]

        for i in idxs:
            for new in candidate_rows_for_bar(pair, frame, i):
                k = (new["pair"], new["direction"], new["signal_date"])
                if k in seen:
                    continue
                seen.add(k)
                new["id"] = next_id
                next_id += 1
                for bk in BOOKS:
                    new[f"cluster_{bk}"] = assign_cluster(
                        tracker, bk, new["pair"], new["direction"],
                        new[f"fire_{bk}"] == "1", new["signal_date"])
                new.update({"status": "OPEN", "exit_reason": "", "exit_date": "",
                            "exit_price": "", "gross_pips": "", "net_pips": "",
                            "days_held": "", "created_at": _now_str(),
                            "settled_at": "", "shadow_recorded": "0"})
                rows.append(new)
                created += 1
        if idxs:
            last_by_pair[pair] = pd.Timestamp(frame.index[idxs[-1]]).strftime("%Y-%m-%d")

        for r in rows:
            if r["pair"] != pair or r["status"] != "OPEN":
                continue
            after = frame[frame.index > pd.Timestamp(r["signal_date"])]
            res = settle_row(r, after)
            if res:
                r.update(res)
                settled += 1

    recorded = record_settled(rows)
    _save_candidates(rows)
    _save_tracker(tracker)
    open_n = sum(1 for r in rows if r["status"] == "OPEN")
    log(f"[mechanical] created={created} settled={settled} shadow_recorded={recorded} "
        f"open={open_n} failures={len(failures)} stale={len(stale)}")
    return {"created": created, "settled": settled, "recorded": recorded,
            "open": open_n, "failures": failures, "stale": stale}


# ─── Reporting ───────────────────────────────────────────────────────────────

def _wr_pf(pips: list):
    n = len(pips)
    if not n:
        return 0, float("nan"), float("nan")
    w = sum(1 for p in pips if p > 0)
    gp = sum(p for p in pips if p > 0)
    gl = -sum(p for p in pips if p < 0)
    return n, w / n * 100, (gp / gl if gl > 0 else float("nan"))


def readiness() -> dict:
    """Per-book promotion readiness from the mechanical path's own evidence.

    shadow_mode applies its Bonferroni correction over the rules in the file
    it is pointed at (just these 3), which understates the real number of
    tests in flight. This adds the correction against the SHARED pool too
    (unpromoted rules there + these 3) as `*_global` fields; use those for
    any promotion conversation."""
    shared_active = sum(1 for r in _sm.list_rules().values() if not r.get("promoted"))
    total_active = shared_active + len(BOOKS)
    out = {}
    with _own_shadow_file():
        for key, (_e, rule, _d) in BOOKS.items():
            s = _sm.check_promotion_readiness(rule)
            if not s.get("registered"):
                out[key] = {"registered": False}
                continue
            g_alpha = s["alpha"] / total_active
            s["global_corrected_alpha"] = round(g_alpha, 6)
            s["p_value_ok_global"] = (s["p_value"] is not None and s["p_value"] < g_alpha)
            s["promotable_global"] = bool(
                s["criteria"]["n_fire_ok"] and s["criteria"]["n_no_fire_ok"] and s["p_value_ok_global"])
            out[key] = s
    return out


def report(log=print) -> None:
    rows = [r for r in load_candidates() if r["status"] in ("WIN", "LOSS", "EXPIRED")]
    all_rows = load_candidates()
    log(f"candidates={len(all_rows)} settled={len(rows)} open={len(all_rows) - len(rows)}")
    for key in BOOKS:
        fire = [float(r["net_pips"]) for r in rows if r[f"fire_{key}"] == "1"]
        nofire = [float(r["net_pips"]) for r in rows if r[f"fire_{key}"] != "1"]
        nf, wrf, pff = _wr_pf(fire)
        nn, wrn, pfn = _wr_pf(nofire)
        log(f"Book {key}: fire n={nf} WR={wrf:.1f}% PF={pff:.2f} | no-fire n={nn} WR={wrn:.1f}% PF={pfn:.2f}")
    for key, s in readiness().items():
        if not s.get("registered"):
            log(f"Book {key}: no shadow evaluations yet")
            continue
        log(f"Book {key}: fire regimes={s['n_fire']}/{s['min_n_fire']} no-fire regimes={s['n_no_fire']}/"
            f"{s['min_n_no_fire']} p={s['p_value']} (alpha shared-pool-corrected={s['global_corrected_alpha']}) "
            f"promotable_global={s['promotable_global']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Mechanical-only G/H/I forward test (no LLM).")
    ap.add_argument("command", choices=("scan", "report"))
    ap.add_argument("--pairs", help="comma-separated subset, e.g. EUR/USD,GBP/USD")
    args = ap.parse_args(argv)
    if args.command == "scan":
        pairs = args.pairs.split(",") if args.pairs else None
        res = run_scan(pairs=pairs)
        # A scan that fetched nothing must fail loudly, not "succeed" with no data.
        total = len(pairs or UNIVERSE)
        return 1 if len(res["failures"]) + len(res["stale"]) >= total else 0
    report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
