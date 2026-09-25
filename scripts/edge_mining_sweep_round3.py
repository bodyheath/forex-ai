"""Round 3 of the 2026-09-25 mechanical edge-mining research loop.

DISCOVERY SLICE ONLY. Reasoning from round 2: the "mean-reversion stack"
(rib_against AND osc_agrees) turned out NOT to be a real synergistic
interaction -- oscillator confirmation didn't add significant value on top
of ribbon alone (p=0.0185, doesn't survive), and vice versa (p=0.283). The
real, striking finding was a BUY/SELL asymmetry: the stack's BUY-only slice
hit PF=1.727 (p<0.00001, n=972), while SELL-only was statistically flat
(PF=1.055, p=0.70, n=1669) -- essentially the entire "edge" lives in BUY
trades. Economically plausible (currencies often fall faster/more violently
than they rise -- "escalator up, elevator down" -- so a SELL-side oversold-
bounce setup fighting a real downtrend may face more follow-through against
it than a BUY-side dip-buy in an uptrend), but a real quant does NOT accept
a striking asymmetry on n=972 without checking it isn't (a) driven by one
or two pairs, (b) a fluke of one narrow time window within discovery, or
(c) present in the RAW underlying signals too (not an artifact unique to
this one derived combination).

Before ever proposing this for the one holdout check, this round checks
exactly those three things. Cumulative correction: round 1 (21) + round 2
(12) + this round's 8 new tests = 41 total; corrected alpha = 0.05/41.
"""
import sys
from math import sqrt
from pathlib import Path

import pandas as pd
from scipy.stats import norm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "mechanical_edge_mining_dataset.csv"
ROUND1_TESTS = 21
ROUND2_TESTS = 12


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
    disc["date"] = pd.to_datetime(disc["date"])
    disc["macd_agrees_direction"] = (
        (disc["direction"] == "BUY") & disc["macd_bullish"]
    ) | (
        (disc["direction"] == "SELL") & ~disc["macd_bullish"]
    )
    disc["counter_trend_ribbon"] = disc["rib_against"]
    disc["mr_stack_2way"] = disc["counter_trend_ribbon"] & disc["osc_agrees"]
    return disc


def report(label, fire, nofire):
    n_fire, wr_fire, pf_fire = wr_pf(fire)
    n_nofire, wr_nofire, pf_nofire = wr_pf(nofire)
    w_fire = int((fire["net_pips"] > 0).sum())
    w_nofire = int((nofire["net_pips"] > 0).sum())
    p = ztest(w_fire, n_fire, w_nofire, n_nofire) if n_fire and n_nofire else None
    p_str = f"{p:.5f}" if p is not None else "n/a"
    print(f"[{label}] fire: n={n_fire} WR={wr_fire*100:.1f}% PF={pf_fire:.3f} | "
          f"no-fire: n={n_nofire} WR={wr_nofire*100:.1f}% PF={pf_nofire:.3f} | p={p_str}")
    return n_fire, wr_fire, pf_fire, p


def main():
    disc = load_discovery()
    stack_buy = disc[disc["mr_stack_2way"] & (disc["direction"] == "BUY")]
    print(f"mr_stack_2way BUY-only population: n={len(stack_buy)}")
    print()

    # (1) Is it driven by one or two pairs?
    print("=== Per-pair breakdown of mr_stack_2way BUY-only (min n=20) ===")
    for pair, grp in stack_buy.groupby("pair"):
        if len(grp) < 20:
            continue
        n, wr, pf = wr_pf(grp)
        print(f"  {pair:<8} n={n:>4} WR={wr*100:>5.1f}% PF={pf:.3f}")
    n_pairs_with_data = stack_buy["pair"].nunique()
    print(f"  ({n_pairs_with_data} distinct pairs contribute to this population)")
    print()

    # (2) Is it a fluke of one time window? Split discovery chronologically into
    # thirds and check the BUY-only stack's WR/PF in each third independently.
    print("=== Temporal stability: mr_stack_2way BUY-only across discovery thirds ===")
    disc_sorted = disc.sort_values("date")
    t1, t2 = disc_sorted["date"].quantile(1/3), disc_sorted["date"].quantile(2/3)
    for label, lo, hi in [("first third", disc_sorted["date"].min(), t1),
                           ("middle third", t1, t2),
                           ("last third", t2, disc_sorted["date"].max())]:
        window = stack_buy[(stack_buy["date"] > lo) & (stack_buy["date"] <= hi)]
        n, wr, pf = wr_pf(window)
        print(f"  {label:<14} n={n:>4} WR={wr*100 if n else 0:>5.1f}% PF={pf:.3f}")
    print()

    # (3) Is the BUY/SELL asymmetry present in the RAW underlying signals too,
    # not just this one derived combination? Check rib_against alone and
    # osc_agrees alone, BUY vs SELL, independently.
    print("=== Is the BUY/SELL asymmetry present in the raw signals too? ===")
    for sig_name, sig_col in [("rib_against", "rib_against"), ("osc_agrees", "osc_agrees")]:
        for direction in ("BUY", "SELL"):
            sub = disc[disc[sig_col] & (disc["direction"] == direction)]
            n, wr, pf = wr_pf(sub)
            print(f"  {sig_name} + {direction}: n={n} WR={wr*100:.1f}% PF={pf:.3f}")
        print()

    # Also: overall BUY vs SELL baseline (unconditional) -- is there already
    # a baseline BUY>SELL skew in this mechanical population before any
    # signal is applied at all?
    print("=== Baseline BUY vs SELL (no signal at all) ===")
    for direction in ("BUY", "SELL"):
        sub = disc[disc["direction"] == direction]
        n, wr, pf = wr_pf(sub)
        print(f"  {direction}: n={n} WR={wr*100:.1f}% PF={pf:.3f}")


if __name__ == "__main__":
    main()
