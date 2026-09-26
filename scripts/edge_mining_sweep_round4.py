"""Round 4 of the mechanical edge-mining research loop (2026-09-26).

DISCOVERY SLICE ONLY. Finishes the job rounds 1-3 left half-done: of the
three underlying phenomena round 2 identified in round 1's 9 correlated
univariate survivors --
  1. Counter-trend vs. the EMA ribbon
  2. Counter-trend vs. the 200-day MA / MACD
  3. Oscillator extremity confirming the trade's own direction
-- only #1 was ever carried through a holdout check AND the cluster-
bootstrap stress test that correctly separates real signal from regime-
autocorrelation noise (that combination is what Book G trades live). #2 and
#3's OWN univariate significance was already established in round 1 (their
raw components -- with_trend_200ma, macd_agrees_direction, osc_agrees,
osc_opposes, osc_triple_agrees, rsi_extreme_against -- are already counted,
already-run round-1 tests; this script does NOT re-test them). What #2 and
#3 never got: principled COMBINATION tests (with each other, and with #1
where there's a real hypothesis), a holdout check, or a cluster bootstrap.
This script does the discovery-slice half of that; see
edge_mining_holdout_check_round4.py and
edge_mining_cluster_bootstrap_stress_test.py for the rest.

CORRECTED CUMULATIVE COUNT -- round 3 audited before trusting it: round 3's
own script defines ztest()/report() but NEVER CALLS report() in main() (it
only prints descriptive WR/PF breakdowls via wr_pf(), zero p-values
computed anywhere in its actual output) -- despite its docstring claiming
"this round's 8 new tests" and computing a documentary CORRECTED_ALPHA
comment. Round 3 was pure diagnostic/robustness work on an
already-flagged candidate (the BUY-only asymmetry), not a new screen. The
TRUE cumulative formal test count carried forward is round1 (21) + round2
(12) = 33, NOT 41. This is corrected here rather than silently inherited.

5 new pre-registered tests this round (a small, disciplined set, not a
sweep) -- each with a stated hypothesis, decided BEFORE looking at any
result:

  1. phenomenon2_stack = counter_trend_200ma AND counter_trend_macd.
     Hypothesis: 200MA (long-term structural position) and MACD (short-
     term momentum) are two different measures of trend; if BOTH agree the
     trade is counter-trend, that should be a cleaner version of
     phenomenon 2 than either alone -- the same logic that motivated
     combining ribbon+oscillator into mr_stack_2way.
  2. phenomenon2_stack AND osc_agrees.
     Hypothesis: does oscillator-extremity confirmation generalise as a
     booster for ANY counter-trend signal, or was its (lack of) synergy
     with ribbon specific to that one indicator?
  3. rib_against AND phenomenon2_stack (no oscillator).
     Hypothesis: do two INDEPENDENT counter-trend measures (ribbon vs
     200MA/MACD) agreeing produce a stronger signal than either alone, or
     are phenomena 1 and 2 mostly redundant restatements of the same
     underlying fact?
  4. bb_extreme_agrees -- Bollinger Band extremity confirming direction
     (price near the lower band on a BUY, or the upper band on a SELL).
     A genuinely untested indicator so far (bb_position exists in the
     dataset but was never used in any prior round). Hypothesis: mirrors
     phenomenon 3's own logic (extremity in a mean-reversion indicator
     predicts a bounce) but from a different indicator family than RSI/
     Stochastic/CCI.
  5. mr_stack_2way, commodity-bloc pairs only (AUD/CAD/NZD as either leg).
     Hypothesis: commodity currencies carry distinct mean-reversion
     dynamics (risk-sentiment/commodity-price-linked) that could behave
     differently under this signal than the "is_jpy/is_gbp/is_chf" cuts
     round 2 already tried (a genuinely new currency-cluster angle, not a
     repeat of those).

Cumulative correction: 33 (rounds 1+2) + 5 (this round) = 38.
Corrected alpha = 0.05/38.
"""
import sys
from math import sqrt
from pathlib import Path

import pandas as pd
from scipy.stats import norm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "mechanical_edge_mining_dataset.csv"
PRIOR_FORMAL_TESTS = 33  # round1 (21) + round2 (12); round3 ran zero formal tests -- see docstring
MIN_N_PER_SIDE = 50


def ztest(wins_a, n_a, wins_b, n_b):
    if n_a == 0 or n_b == 0:
        return None
    p_a, p_b = wins_a / n_a, wins_b / n_b
    p_pool = (wins_a + wins_b) / (n_a + n_b)
    se = sqrt(p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b))
    if se == 0:
        return None
    z = (p_a - p_b) / se
    return 2 * (1 - norm.cdf(abs(z)))


def wr_pf(d):
    """Same convention as rounds 1-3's own wr_pf() and
    historical_grading_backtest.py's _is_win(): EXPIRED rows are KEPT and
    classified by net_pips sign, not excluded. Verified against the
    reference script directly before reusing (see
    edge_mining_cluster_bootstrap_stress_test.py's load_holdout() comment
    for the full story of why this was double-checked)."""
    n = len(d)
    if n == 0:
        return 0, float("nan"), float("nan")
    w = (d["net_pips"] > 0).sum()
    l = (d["net_pips"] < 0).sum()
    dec = w + l
    wr = w / dec if dec else float("nan")
    gp = d.loc[d["net_pips"] > 0, "net_pips"].sum()
    gl = -d.loc[d["net_pips"] < 0, "net_pips"].sum()
    pf = gp / gl if gl > 0 else float("inf")
    return dec, wr, pf


def load_discovery():
    df = pd.read_csv(DATA_PATH)
    disc = df[df["split"] == "discovery"].copy()
    disc["macd_agrees_direction"] = (
        (disc["direction"] == "BUY") & disc["macd_bullish"]
    ) | (
        (disc["direction"] == "SELL") & ~disc["macd_bullish"]
    )
    disc["counter_trend_ribbon"] = disc["rib_against"]
    disc["counter_trend_macd"] = ~disc["macd_agrees_direction"]
    disc["counter_trend_200ma"] = ~disc["with_trend_200ma"]
    disc["phenomenon2_stack"] = disc["counter_trend_200ma"] & disc["counter_trend_macd"]
    # bb_position: fraction of Bollinger Band width, 0=lower band, 1=upper
    # band (verified range empirically below in main() before trusting the
    # 0.2/0.8 threshold choice).
    disc["bb_extreme_agrees"] = (
        ((disc["direction"] == "BUY") & (disc["bb_position"] <= 0.2)) |
        ((disc["direction"] == "SELL") & (disc["bb_position"] >= 0.8))
    )
    commodity_ccys = ("AUD", "CAD", "NZD")
    disc["is_commodity_bloc"] = disc["pair"].apply(
        lambda p: any(c in p for c in commodity_ccys))
    disc["mr_stack_2way"] = disc["counter_trend_ribbon"] & disc["osc_agrees"]
    return disc


CONDITIONS = [
    ("phenomenon2_stack (200ma_against AND macd_against)",
        lambda d: d["phenomenon2_stack"]),
    ("phenomenon2_stack AND osc_agrees",
        lambda d: d["phenomenon2_stack"] & d["osc_agrees"]),
    ("rib_against AND phenomenon2_stack (no osc)",
        lambda d: d["counter_trend_ribbon"] & d["phenomenon2_stack"]),
    ("bb_extreme_agrees",
        lambda d: d["bb_extreme_agrees"]),
    ("mr_stack_2way, commodity-bloc pairs only (AUD/CAD/NZD)",
        lambda d: d["mr_stack_2way"] & d["is_commodity_bloc"]),
]
N_NEW_TESTS = len(CONDITIONS)
TOTAL_TESTS = PRIOR_FORMAL_TESTS + N_NEW_TESTS
CORRECTED_ALPHA = 0.05 / TOTAL_TESTS


def main():
    disc = load_discovery()
    print(f"Discovery slice: n={len(disc)}")
    print(f"bb_position range check: min={disc['bb_position'].min():.3f} "
          f"max={disc['bb_position'].max():.3f} "
          f"(confirms 0.2/0.8 thresholds are sane fractions of band width)")
    print(f"Prior formal tests (round1 21 + round2 12, round3 ran ZERO "
          f"formal tests -- see this script's docstring): {PRIOR_FORMAL_TESTS}")
    print(f"This round pre-registers {N_NEW_TESTS} new tests.")
    print(f"Cumulative corrected alpha = 0.05/{TOTAL_TESTS} = {CORRECTED_ALPHA:.6f}")
    print()

    baseline_dec, baseline_wr, baseline_pf = wr_pf(disc)
    print(f"Baseline: n={baseline_dec} WR={baseline_wr*100:.1f}% PF={baseline_pf:.3f}")
    print()

    survivors = []
    for label, cond_fn in CONDITIONS:
        mask = cond_fn(disc)
        fire = disc[mask]
        nofire = disc[~mask]
        n_fire, wr_fire, pf_fire = wr_pf(fire)
        n_nofire, wr_nofire, pf_nofire = wr_pf(nofire)
        if n_fire < MIN_N_PER_SIDE or n_nofire < MIN_N_PER_SIDE:
            print(f"[{label}] THIN: n_fire={n_fire} n_nofire={n_nofire} -- not reported as a candidate")
            print()
            continue
        w_fire = int((fire["net_pips"] > 0).sum())
        w_nofire = int((nofire["net_pips"] > 0).sum())
        p = ztest(w_fire, n_fire, w_nofire, n_nofire)
        p_str = f"{p:.5f}" if p is not None else "n/a"
        flag = "SURVIVES CORRECTED BAR" if (p is not None and p < CORRECTED_ALPHA) else "no"
        print(f"[{label}]")
        print(f"    fire:    n={n_fire:>6} WR={wr_fire*100:>5.1f}% PF={pf_fire:.3f}")
        print(f"    no-fire: n={n_nofire:>6} WR={wr_nofire*100:>5.1f}% PF={pf_nofire:.3f}")
        print(f"    p={p_str}  -> {flag}")
        print()
        if flag == "SURVIVES CORRECTED BAR":
            survivors.append(label)

    print(f"Survivors of the corrected bar ({N_NEW_TESTS} new tests this round): {len(survivors)}")
    for s in survivors:
        print(f"  {s}")


if __name__ == "__main__":
    main()
