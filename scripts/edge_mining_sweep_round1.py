"""Round 1 of the 2026-09-25 mechanical edge-mining research loop.

DISCOVERY SLICE ONLY -- never touches split=="holdout". A systematic,
pre-enumerated univariate sweep over every already-computed deterministic
condition in data/mechanical_edge_mining_dataset.csv, each tested against
the outcome (WIN vs LOSS, EXPIRED excluded as non-decisive-by-construction
of the mechanical TP-or-SL replay -- same convention as
historical_grading_backtest.py). Bonferroni-corrected by the EXACT number
of conditions tested here (printed at the top of the report), not an
approximate count.

Why univariate-grid, not decision-tree/feature-importance, for round 1:
a fixed, enumerable grid lets the multiple-comparisons correction be
EXACT (alpha / len(CONDITIONS)) rather than an approximation of a tree's
effective degrees of freedom, which is what the user explicitly asked to
avoid glossing over. Round 2+ (separate script) will follow up on whatever
round 1 surfaces with interaction hypotheses, still under a correction
that grows with the cumulative test count across all rounds.
"""
import sys
from math import sqrt
from pathlib import Path

import pandas as pd
from scipy.stats import norm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "mechanical_edge_mining_dataset.csv"
ALPHA = 0.05
MIN_N_PER_SIDE = 100  # a condition splitting into a side below this is not reported as a candidate


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


# Each condition: (label, boolean-mask-producing lambda over the discovery df)
# Pre-enumerated in full BEFORE any result is looked at -- the count below
# is exactly len(CONDITIONS), used as the Bonferroni divisor.
CONDITIONS = [
    ("rib_against",              lambda d: d["rib_against"]),
    ("rib_strongly_against",     lambda d: d["rib_strongly_against"]),
    ("rib_aligned",              lambda d: d["rib_aligned"]),
    ("ribbon_fanning",           lambda d: d["ribbon_fanning"]),
    ("w_d_conflict",             lambda d: d["w_d_conflict"]),
    ("w_d_agree",                lambda d: d["w_d_agree"]),
    ("rsi_extreme_against",      lambda d: d["rsi_extreme_against"]),
    ("macd_agrees_direction",    lambda d: d["macd_agrees_direction"]),
    ("with_trend_200ma",         lambda d: d["with_trend_200ma"]),
    ("osc_agrees",               lambda d: d["osc_agrees"]),
    ("osc_opposes",              lambda d: d["osc_opposes"]),
    ("osc_triple_agrees",        lambda d: d["osc_agrees"] & (d["osc_score"] >= 3)),
    ("div_supports",             lambda d: d["div_supports"]),
    ("atr_pct_6m_elevated_1.0",  lambda d: d["atr_pct_6m"] >= 1.0),
    ("atr_pct_6m_elevated_1.2",  lambda d: d["atr_pct_6m"] >= 1.2),
    ("atr_pct_6m_low_0.7",       lambda d: d["atr_pct_6m"] < 0.7),
    ("is_monday",                lambda d: d["day_of_week"] == 0),
    ("is_friday",                lambda d: d["day_of_week"] == 4),
    ("is_jpy_cross",             lambda d: d["pair"].str.contains("JPY")),
    ("is_gbp_cross",             lambda d: d["pair"].str.contains("GBP")),
    ("is_chf_cross",             lambda d: d["pair"].str.contains("CHF")),
]

N_TESTS = len(CONDITIONS)
CORRECTED_ALPHA = ALPHA / N_TESTS


def main():
    df = pd.read_csv(DATA_PATH)
    disc = df[df["split"] == "discovery"].copy()
    disc["macd_agrees_direction"] = (
        (disc["direction"] == "BUY") & disc["macd_bullish"]
    ) | (
        (disc["direction"] == "SELL") & ~disc["macd_bullish"]
    )
    print(f"Discovery slice: n={len(disc)} rows (holdout untouched, n={len(df)-len(disc)})")
    print(f"Running {N_TESTS} pre-enumerated univariate tests. "
          f"Bonferroni-corrected alpha = {ALPHA}/{N_TESTS} = {CORRECTED_ALPHA:.5f}")
    print()

    baseline_dec, baseline_wr, baseline_pf = wr_pf(disc)
    print(f"Baseline (all discovery rows): n_decisive={baseline_dec} WR={baseline_wr*100:.1f}% PF={baseline_pf:.3f}")
    print()

    results = []
    for label, cond_fn in CONDITIONS:
        try:
            mask = cond_fn(disc)
        except Exception as exc:
            print(f"  [{label}] condition failed: {exc}")
            continue
        fire = disc[mask]
        nofire = disc[~mask]
        n_fire, wr_fire, pf_fire = wr_pf(fire)
        n_nofire, wr_nofire, pf_nofire = wr_pf(nofire)
        if n_fire < MIN_N_PER_SIDE or n_nofire < MIN_N_PER_SIDE:
            results.append((label, n_fire, wr_fire, pf_fire, n_nofire, wr_nofire, pf_nofire, None, "thin"))
            continue
        w_fire = int((fire["net_pips"] > 0).sum())
        w_nofire = int((nofire["net_pips"] > 0).sum())
        p = ztest(w_fire, n_fire, w_nofire, n_nofire)
        flag = "SURVIVES CORRECTED BAR" if (p is not None and p < CORRECTED_ALPHA) else (
            "raw-only (p<0.05, not corrected)" if (p is not None and p < ALPHA) else "no")
        results.append((label, n_fire, wr_fire, pf_fire, n_nofire, wr_nofire, pf_nofire, p, flag))

    print(f"{'condition':<28} {'n_fire':>7} {'WR_fire':>8} {'PF_fire':>8} {'n_no':>7} {'WR_no':>8} {'PF_no':>8} {'p':>10}  flag")
    for label, nf, wrf, pff, nn, wrn, pfn, p, flag in results:
        p_str = f"{p:.5f}" if p is not None else "n/a"
        print(f"{label:<28} {nf:>7} {wrf*100:>7.1f}% {pff:>8.3f} {nn:>7} {wrn*100:>7.1f}% {pfn:>8.3f} {p_str:>10}  {flag}")

    survivors = [r for r in results if r[8] == "SURVIVES CORRECTED BAR"]
    print()
    print(f"Survivors of the corrected bar: {len(survivors)}")
    for s in survivors:
        print(f"  {s[0]}")


if __name__ == "__main__":
    main()
