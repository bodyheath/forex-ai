"""Backtest for the two COT-positioning mechanisms that rode along
unexamined when cot_reversal_penalty was backtested and removed
(2026-09-08): `extreme_flag` (positioning.py's percentile-in-range
descriptive text, shown to Haiku, no deterministic scoring instruction
attached) and the Haiku system prompt's "BUILDING=institutions increasing
conviction in current direction: raise POSITIONING_SCORE +1" instruction.
Both consume the exact same CFTC COT positioning data category that
cot_reversal_penalty turned out to be actively backwards on -- this tests
whether these two hold up any better, using the identical rigor (real
data, walk-forward, no lookahead, all 28 pairs, per-currency/per-lag
breakdown, correlated-draws caveat disclosed).

============================================================================
extreme_flag HAS NO DETERMINISTIC FUNCTION TO CALL -- WHY THIS SCRIPT TESTS
THE PREMISE DIRECTLY INSTEAD
============================================================================
Unlike cot_reversal_penalty (a pure Python function), extreme_flag is only
ever consumed as (a) descriptive text handed to Haiku inside its prompt
data (analyst.py's _compress_bundle()), and (b) a pure logging tally
(daily.py's [extreme_flag] observability block) -- confirmed by grepping
every reference in the codebase. There is no `if extreme_flag: adj -= 1`
anywhere to import and call. What IS testable, with the same rigor, is the
underlying PREMISE the flag encodes: positioning.py's own text says a
currency at >=85th percentile of its 52-week range is "crowded long,
reversal risk" and <=15th percentile is "crowded short, reversal risk".
This script reconstructs that exact percentile classification from real
historical CFTC data (byte-for-byte the same formula positioning.py uses
live: `(net - lo) / (hi - lo) * 100`) and tests whether a trade that piles
onto the crowded side actually underperforms, exactly as the premise
claims -- the same thing Phase 01B's z-score backtest and the
cot_reversal_penalty backtest each did for their own COT construction.

============================================================================
BUILDING -- A REAL FUNCTION EXISTS, BUT IT'S A PROMPT INSTRUCTION, NOT PURE
PYTHON, SO THE SAME "TEST THE PREMISE" APPROACH IS USED
============================================================================
"BUILDING=+1 POSITIONING_SCORE" lives in analyst.py's
_haiku_system_prompt() -- real, live, deterministic IN THE SENSE that the
instruction text is fixed and unconditional, but its actual effect runs
through Haiku's own qualitative judgment (an LLM reading and applying the
instruction), not a callable Python function the way cot_reversal_penalty
was. This script tests the underlying claim directly instead: does
`positioning.py`'s own BUILDING classification -- institutions increasing
conviction in the CURRENT direction -- predict a BETTER-than-baseline
outcome for a trade aligned with that direction, using real historical
data? This is the positive-pole mirror of the removed cot_reversal_penalty
backtest (which tested the negative pole: REVERSING/UNWINDING predicting
worse outcomes for the OPPOSING direction).

============================================================================
SMART MONEY DIVERGENCE -- COVERED BY THE SAME TWO TESTS, NOT RE-TESTED
============================================================================
src/smart_money.py's `_institutional_score()` (feeding the real +-1
_eff_conf() SMD adjustment) normalizes percentile-in-range to [-1,+1] --
mathematically the same construction as extreme_flag, just rescaled -- and
then applies a momentum multiplier (BUILDING=1.25x, STABLE=1.0x,
UNWINDING=0.65x, REVERSING=0.80x) plus a separate BUILDING "conviction"
bonus in `analyse()`. Both of SMD's COT-derived building blocks are
exactly the two premises this script tests below; the retail/sentiment
half of SMD (keyword-scored news headlines) is not independently
backtestable with available historical data (headlines aren't archived).
No separate SMD backtest is run -- the findings below apply directly to
its COT-derived half.

============================================================================
METHOD -- IDENTICAL INFRASTRUCTURE AND RIGOR TO
scripts/cot_reversal_penalty_backtest.py
============================================================================
Reuses that script's real CFTC-fetch and walk-forward momentum/percentile
reconstruction verbatim (imported, not reimplemented) so there is zero
risk of the two backtests silently drifting apart on data or method.
Same publication-lag handling (report_date + 3 days), same non-independence
caveat (one currency's condition fires for every pair containing it in the
same week -- not independent draws), same forward-return windows (5/10/20
trading days), same two-proportion z-test (src.shadow_mode._ztest).

Usage: python scripts/extreme_flag_and_building_boost_backtest.py
Writes data/extreme_flag_building_backtest_detail.csv and
data/extreme_flag_building_backtest_results.csv.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("ALLOW_LOCAL_RUN", "YES")

import numpy as np
import pandas as pd

import config
from src.selector import UNIVERSE
from src.shadow_mode import _ztest
import historical_grading_backtest as hgb
from cot_reversal_penalty_backtest import (
    build_currency_history, compute_momentum_series, forward_return,
)

hgb.FETCH_PERIOD = "max"

_EXTREME_PCT = 15         # matches positioning.py's own <=15 / >=85 bucketing
_FWD_DAYS = [5, 10, 20]


def find_firings(momentum: dict) -> tuple:
    """Returns (extreme_detail_rows, building_detail_rows, baseline_rows) --
    baseline is shared: every (pair, direction, week) that fired NEITHER
    condition, for the same currency, so both tests compare against the
    same non-firing population."""
    extreme_rows, building_rows, baseline_rows = [], [], []
    price_cache = {}

    for pair in UNIVERSE:
        base, quote = pair.split("/")
        if base not in momentum or quote not in momentum:
            continue
        b_df, q_df = momentum[base], momentum[quote]
        merged = pd.merge(
            b_df, q_df, on="publication_date", how="outer", suffixes=("_base", "_quote"),
        ).sort_values("publication_date")
        if merged.empty:
            continue

        print(f"[price] fetching {pair}...")
        try:
            px = hgb.fetch_daily(pair)
        except Exception as exc:
            print(f"  -> price fetch failed: {exc}")
            continue
        if px.empty:
            continue
        px = px.sort_index()
        price_cache[pair] = px
        time.sleep(0.2)

        for _, row in merged.iterrows():
            pub = row["publication_date"]

            def leg(prefix):
                if pd.isna(row.get(f"cot_momentum_{prefix}")):
                    return None
                return {
                    "momentum": row.get(f"cot_momentum_{prefix}"),
                    "pct": row.get(f"pct_in_range_{prefix}"),
                    "net": row.get(f"net_{prefix}"),
                }
            b, q = leg("base"), leg("quote")
            if b is None and q is None:
                continue

            for direction in ("BUY", "SELL"):
                # -- extreme_flag premise: crowded-side currency reinforcing this direction
                extreme_side, extreme_ccy = None, None
                if direction == "BUY":
                    if b and b["pct"] is not None and b["pct"] >= 100 - _EXTREME_PCT:
                        extreme_side, extreme_ccy = "base", base
                    elif q and q["pct"] is not None and q["pct"] <= _EXTREME_PCT:
                        extreme_side, extreme_ccy = "quote", quote
                else:
                    if b and b["pct"] is not None and b["pct"] <= _EXTREME_PCT:
                        extreme_side, extreme_ccy = "base", base
                    elif q and q["pct"] is not None and q["pct"] >= 100 - _EXTREME_PCT:
                        extreme_side, extreme_ccy = "quote", quote

                # -- BUILDING premise: mirrors _cot_reversal_penalty()'s own
                # base/quote directional structure exactly, just with BUILDING
                # (reinforcing) instead of REVERSING/UNWINDING (contradicting)
                # and using the CURRENT net position's sign (BUILDING is about
                # ongoing conviction in whichever direction the currency is
                # ALREADY net positioned, not a historical reference point the
                # way old_net was for the removed penalty).
                #   BUY:  base net-long + BUILDING (doubling down long, base
                #         should strengthen) OR quote net-short + BUILDING
                #         (doubling down short, quote should weaken)
                #   SELL: base net-short + BUILDING OR quote net-long + BUILDING
                building_side, building_ccy = None, None
                if direction == "BUY":
                    if b and b["momentum"] == "BUILDING" and b["net"] is not None and b["net"] > 0:
                        building_side, building_ccy = "base", base
                    elif q and q["momentum"] == "BUILDING" and q["net"] is not None and q["net"] < 0:
                        building_side, building_ccy = "quote", quote
                else:
                    if b and b["momentum"] == "BUILDING" and b["net"] is not None and b["net"] < 0:
                        building_side, building_ccy = "base", base
                    elif q and q["momentum"] == "BUILDING" and q["net"] is not None and q["net"] > 0:
                        building_side, building_ccy = "quote", quote

                if extreme_side is None and building_side is None:
                    rec = {"pair": pair, "direction": direction, "publication_date": pub}
                    for nd in _FWD_DAYS:
                        rec[f"fwd_{nd}d"] = forward_return(px, pub, nd)
                    baseline_rows.append(rec)
                    continue

                if extreme_side is not None:
                    rec = {"pair": pair, "direction": direction, "publication_date": pub,
                           "side": extreme_side, "currency": extreme_ccy}
                    for nd in _FWD_DAYS:
                        rec[f"fwd_{nd}d"] = forward_return(px, pub, nd)
                    extreme_rows.append(rec)

                if building_side is not None:
                    rec = {"pair": pair, "direction": direction, "publication_date": pub,
                           "side": building_side, "currency": building_ccy}
                    for nd in _FWD_DAYS:
                        rec[f"fwd_{nd}d"] = forward_return(px, pub, nd)
                    building_rows.append(rec)

    return (pd.DataFrame(extreme_rows), pd.DataFrame(building_rows),
            pd.DataFrame(baseline_rows), price_cache)


def _hit(sub: pd.DataFrame, nd: int) -> pd.Series:
    col = f"fwd_{nd}d"
    sign = sub[col].apply(lambda v: None if pd.isna(v) else (1 if v > 0 else (-1 if v < 0 else 0)))
    dir_sign = sub["direction"].map({"BUY": 1, "SELL": -1})
    return sign == dir_sign


def report(name: str, fired: pd.DataFrame, baseline: pd.DataFrame, results: list, claim: str):
    print("\n" + "=" * 100)
    print(f"{name}: {claim}")
    print("=" * 100)
    for nd in _FWD_DAYS:
        col = f"fwd_{nd}d"
        f_valid = fired.dropna(subset=[col])
        b_valid = baseline.dropna(subset=[col])
        f_hits = int(_hit(f_valid, nd).sum())
        b_hits = int(_hit(b_valid, nd).sum())
        z = _ztest(f_hits, len(f_valid), b_hits, len(b_valid))
        p_value, wr_f, wr_b = z if z else (None, None, None)
        print(f"fwd={nd:2d}d  fired win-rate={wr_f*100 if wr_f is not None else float('nan'):5.1f}% "
              f"(n={len(f_valid):6d})   baseline win-rate={wr_b*100 if wr_b is not None else float('nan'):5.1f}% "
              f"(n={len(b_valid):6d})   p={p_value if p_value is not None else float('nan'):.4f}")
        results.append({"scope": "aggregate", "signal": name, "pair_or_ccy": "ALL", "fwd_days": nd,
                         "n_fired": len(f_valid), "n_baseline": len(b_valid),
                         "fired_win_rate": wr_f, "baseline_win_rate": wr_b, "p_value": p_value})

    print(f"\nPER-CURRENCY (fwd=5d, n>=8):")
    b5 = baseline.dropna(subset=["fwd_5d"])
    for ccy in sorted(fired["currency"].dropna().unique()):
        sub = fired[fired["currency"] == ccy].dropna(subset=["fwd_5d"])
        if len(sub) < 8:
            continue
        b_sub = b5[b5["pair"].str.contains(ccy)]
        f_hits = int(_hit(sub, 5).sum())
        b_hits = int(_hit(b_sub, 5).sum()) if len(b_sub) else 0
        z = _ztest(f_hits, len(sub), b_hits, len(b_sub)) if len(b_sub) else None
        p_value, wr, wr_b = z if z else (None, None, None)
        print(f"  {ccy:5s} n={len(sub):5d}  win_rate={wr*100 if wr is not None else float('nan'):5.1f}%  "
              f"(baseline n={len(b_sub)}, {wr_b*100 if wr_b is not None else float('nan'):5.1f}%)  "
              f"p={p_value if p_value is not None else float('nan'):.4f}")
        results.append({"scope": "per_currency", "signal": name, "pair_or_ccy": ccy, "fwd_days": 5,
                         "n_fired": len(sub), "n_baseline": len(b_sub),
                         "fired_win_rate": wr, "baseline_win_rate": wr_b, "p_value": p_value})


def run():
    print("Fetching real CFTC COT history (reusing cot_reversal_penalty_backtest.py)...")
    history = build_currency_history()
    momentum = {}
    for ccy, df in history.items():
        m = compute_momentum_series(df)
        m["pct_in_range"] = (m["net"] - m["one_year_low"]) / (m["one_year_high"] - m["one_year_low"]) * 100
        momentum[ccy] = m
        print(f"[momentum] {ccy}: {len(m)} weeks, "
              f"{(m['pct_in_range'] >= 100 - _EXTREME_PCT).sum()} at/above {100-_EXTREME_PCT}pct, "
              f"{(m['pct_in_range'] <= _EXTREME_PCT).sum()} at/below {_EXTREME_PCT}pct, "
              f"{(m['cot_momentum']=='BUILDING').sum()} BUILDING")

    extreme_df, building_df, baseline_df, _ = find_firings(momentum)
    extreme_df.to_csv("data/extreme_flag_building_backtest_detail.csv", index=False)
    building_df.to_csv("data/extreme_flag_building_backtest_building_detail.csv", index=False)
    print(f"\nextreme_flag-condition instances: {len(extreme_df)}")
    print(f"BUILDING-condition instances: {len(building_df)}")
    print(f"Non-firing baseline instances (fired neither): {len(baseline_df)}")

    results = []
    report(
        "extreme_flag", extreme_df, baseline_df, results,
        "does piling onto an already-crowded (>=85th/<=15th pctile) currency "
        "predict a WORSE outcome (validating the 'reversal risk' framing) than baseline? "
        "If validated: fired win-rate should be LOWER than baseline.",
    )
    report(
        "BUILDING_boost", building_df, baseline_df, results,
        "does trading aligned with a currency where institutions are BUILDING "
        "conviction predict a BETTER outcome than baseline? "
        "If validated: fired win-rate should be HIGHER than baseline.",
    )

    pd.DataFrame(results).to_csv("data/extreme_flag_building_backtest_results.csv", index=False)
    print("\nWrote data/extreme_flag_building_backtest_results.csv, "
          "data/extreme_flag_building_backtest_detail.csv, "
          "data/extreme_flag_building_backtest_building_detail.csv")
    print("\nKNOWN NON-INDEPENDENCE, stated plainly: one currency's condition fires for")
    print("every pair containing it in the same week -- not independent draws. Per-currency")
    print("breakdown is the more trustworthy check for a broad-based, real effect.")


if __name__ == "__main__":
    run()
