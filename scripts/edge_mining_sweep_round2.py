"""Round 2 of the 2026-09-25 mechanical edge-mining research loop.

DISCOVERY SLICE ONLY. Reasoning from round 1's actual results (not
pre-planned before seeing them, per the request):

Round 1 found 9 "survivors" of the corrected bar, but a correlation/overlap
check (done by hand before writing this round) shows they collapse to
roughly THREE independent underlying phenomena, not nine:
  1. Counter-trend vs. the EMA ribbon (rib_against / rib_strongly_against /
     rib_aligned -- one axis; "strongly" vs "merely" against don't separate).
  2. Counter-trend vs. the 200-day MA / MACD (with_trend_200ma,
     macd_agrees_direction -- correlated with #1 at r=0.51 / r=0.24, not
     identical to it).
  3. Oscillator extremity CONFIRMING the trade's own direction (osc_agrees /
     osc_opposes / osc_triple_agrees / rsi_extreme_against -- rsi_extreme_
     against turns out to be a near-strict SUBSET of osc_agrees, 96.6% of
     rsi_extreme_against rows are also osc_agrees rows -- not a fourth
     independent signal).

All three axes point the same direction: counter-trend entries, confirmed
by oscillator extremity, beat with-trend entries -- consistent with this
mechanical 2:1 R:R / 4-day-expiry construction structurally favoring short-
term mean reversion over trend continuation. The obvious next question a
quant would ask: does COMBINING these axes produce a materially stronger,
cleaner signal than any one alone (a real interaction), or are they
redundant restatements of the same underlying fact? Then: does it hold
across currency clusters and volatility regimes, or is it another case
like the JPY-fundamentals or GBP/CHF-ribbon exclusions -- real in the
aggregate but concentrated/reversed in a specific slice?

Cumulative correction: round 1 ran 21 tests. This round pre-registers 12
more BEFORE looking at their p-values. Corrected alpha = 0.05 / (21+12).
"""
import sys
from math import sqrt
from pathlib import Path

import pandas as pd
from scipy.stats import norm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "mechanical_edge_mining_dataset.csv"
ROUND1_TESTS = 21
MIN_N_PER_SIDE = 50  # lower than round 1 -- interaction cells are naturally smaller


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
    disc["osc_triple_agrees"] = disc["osc_agrees"] & (disc["osc_score"] >= 3)
    # The combined "mean-reversion stack": counter to ribbon AND oscillators
    # confirm the reversal -- the single most theoretically motivated
    # 2-way interaction from round 1's own reasoning.
    disc["mr_stack_2way"] = disc["counter_trend_ribbon"] & disc["osc_agrees"]
    disc["mr_stack_3way"] = (
        disc["counter_trend_ribbon"] & disc["counter_trend_200ma"] & disc["osc_agrees"]
    )
    return disc


CONDITIONS = [
    ("mr_stack_2way (rib_against AND osc_agrees)",
        lambda d: d["mr_stack_2way"]),
    ("mr_stack_3way (rib_against AND 200ma_against AND osc_agrees)",
        lambda d: d["mr_stack_3way"]),
    ("osc_agrees WITHIN rib_against (does osc add value on top of ribbon alone)",
        lambda d: d["counter_trend_ribbon"] & d["osc_agrees"], "counter_trend_ribbon"),
    ("rib_against WITHIN osc_agrees (does ribbon add value on top of osc alone)",
        lambda d: d["counter_trend_ribbon"] & d["osc_agrees"], "osc_agrees"),
    ("mr_stack_2way, JPY crosses only",
        lambda d: d["mr_stack_2way"] & d["pair"].str.contains("JPY")),
    ("mr_stack_2way, non-JPY only",
        lambda d: d["mr_stack_2way"] & ~d["pair"].str.contains("JPY")),
    ("mr_stack_2way, GBP crosses only",
        lambda d: d["mr_stack_2way"] & d["pair"].str.contains("GBP")),
    ("mr_stack_2way, CHF crosses only",
        lambda d: d["mr_stack_2way"] & d["pair"].str.contains("CHF")),
    ("mr_stack_2way, elevated ATR (>=1.0)",
        lambda d: d["mr_stack_2way"] & (d["atr_pct_6m"] >= 1.0)),
    ("mr_stack_2way, calm ATR (<1.0)",
        lambda d: d["mr_stack_2way"] & (d["atr_pct_6m"] < 1.0)),
    ("mr_stack_2way, BUY only",
        lambda d: d["mr_stack_2way"] & (d["direction"] == "BUY")),
    ("mr_stack_2way, SELL only",
        lambda d: d["mr_stack_2way"] & (d["direction"] == "SELL")),
]
N_NEW_TESTS = len(CONDITIONS)
TOTAL_TESTS = ROUND1_TESTS + N_NEW_TESTS
CORRECTED_ALPHA = 0.05 / TOTAL_TESTS


def main():
    disc = load_discovery()
    print(f"Discovery slice: n={len(disc)}")
    print(f"Round 1 ran {ROUND1_TESTS} tests. Round 2 pre-registers {N_NEW_TESTS} more.")
    print(f"Cumulative corrected alpha = 0.05/{TOTAL_TESTS} = {CORRECTED_ALPHA:.5f}")
    print()
    baseline_dec, baseline_wr, baseline_pf = wr_pf(disc)
    print(f"Baseline: n={baseline_dec} WR={baseline_wr*100:.1f}% PF={baseline_pf:.3f}")
    print()

    for entry in CONDITIONS:
        if len(entry) == 3:
            label, mask_fn, restrict_col = entry
            base = disc[disc[restrict_col]]
        else:
            label, mask_fn = entry
            base = disc
        mask = mask_fn(disc).reindex(base.index)
        fire = base[mask.loc[base.index]]
        nofire = base[~mask.loc[base.index]]
        n_fire, wr_fire, pf_fire = wr_pf(fire)
        n_nofire, wr_nofire, pf_nofire = wr_pf(nofire)
        if n_fire < MIN_N_PER_SIDE or n_nofire < MIN_N_PER_SIDE:
            print(f"[{label}] THIN: n_fire={n_fire} n_nofire={n_nofire} -- not reported as a candidate")
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

    # Also report the mr_stack_2way's absolute performance vs the overall
    # baseline directly (not just vs its complement) -- what a real quant
    # cares about first: is this actually a good population in absolute terms.
    stack = disc[disc["mr_stack_2way"]]
    n, wr, pf = wr_pf(stack)
    print(f"mr_stack_2way absolute: n={n} WR={wr*100:.1f}% PF={pf:.3f} "
          f"(baseline was WR={baseline_wr*100:.1f}% PF={baseline_pf:.3f})")


if __name__ == "__main__":
    main()
