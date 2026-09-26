"""Holdout check for round 4 (phenomena 2 and 3), run once discovery-side
work (edge_mining_sweep_round4.py) is fully finished -- same discipline as
edge_mining_holdout_check.py: no peeking at holdout before this, candidates
chosen BEFORE looking at any holdout result.

CANDIDATES, chosen for a stated reason each (not everything explored):
  1. counter_trend_200ma alone -- phenomenon 2's simplest single-component
     anchor, mirroring "rib_against alone" as phenomenon 1's anchor. Its
     univariate significance was already established in round 1
     (with_trend_200ma, p=0.00003) -- this is its first HOLDOUT check.
  2. osc_agrees alone -- phenomenon 3's anchor. Already round-1-significant
     (p=0.00001) and already part of mr_stack_2way's holdout check, but
     never checked ALONE at holdout (only in combination with ribbon).
  3. phenomenon2_stack (200ma AND macd both counter-trend) -- round 4's
     combined phenomenon-2 candidate, survived discovery (p=0.00002).
  4. phenomenon2_stack AND osc_agrees -- survived discovery (p=0.00021).
  5. rib_against AND phenomenon2_stack (no osc) -- survived discovery
     (p=0.00000).
  6. bb_extreme_agrees -- new Bollinger-Band-extremity indicator, survived
     discovery (p=0.00025).

Local correction (same convention as edge_mining_holdout_check.py):
alpha=0.05 / len(CANDIDATES) = 0.05/6.
"""
import sys
from math import sqrt
from pathlib import Path

import pandas as pd
from scipy.stats import norm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "mechanical_edge_mining_dataset.csv"


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


def load_holdout():
    df = pd.read_csv(DATA_PATH)
    hold = df[df["split"] == "holdout"].copy()
    hold["macd_agrees_direction"] = (
        (hold["direction"] == "BUY") & hold["macd_bullish"]
    ) | (
        (hold["direction"] == "SELL") & ~hold["macd_bullish"]
    )
    hold["counter_trend_200ma"] = ~hold["with_trend_200ma"]
    hold["counter_trend_macd"] = ~hold["macd_agrees_direction"]
    hold["phenomenon2_stack"] = hold["counter_trend_200ma"] & hold["counter_trend_macd"]
    hold["bb_extreme_agrees"] = (
        ((hold["direction"] == "BUY") & (hold["bb_position"] <= 0.2)) |
        ((hold["direction"] == "SELL") & (hold["bb_position"] >= 0.8))
    )
    return hold


CANDIDATES = [
    ("counter_trend_200ma alone", lambda d: d["counter_trend_200ma"]),
    ("osc_agrees alone", lambda d: d["osc_agrees"]),
    ("phenomenon2_stack (200ma AND macd)", lambda d: d["phenomenon2_stack"]),
    ("phenomenon2_stack AND osc_agrees", lambda d: d["phenomenon2_stack"] & d["osc_agrees"]),
    ("rib_against AND phenomenon2_stack (no osc)", lambda d: d["rib_against"] & d["phenomenon2_stack"]),
    ("bb_extreme_agrees", lambda d: d["bb_extreme_agrees"]),
]


def main():
    hold = load_holdout()
    print(f"ROUND 4 HOLDOUT CHECK -- n={len(hold)} rows, touched for the first time now.")
    baseline_dec, baseline_wr, baseline_pf = wr_pf(hold)
    print(f"Holdout baseline: n_decisive={baseline_dec} WR={baseline_wr*100:.1f}% PF={baseline_pf:.3f}")
    print()
    corrected_alpha = 0.05 / max(1, len(CANDIDATES))
    print(f"Local correction: 0.05/{len(CANDIDATES)} = {corrected_alpha:.5f}")
    print()
    survivors = []
    for label, cond_fn in CANDIDATES:
        mask = cond_fn(hold)
        fire = hold[mask]
        nofire = hold[~mask]
        n_fire, wr_fire, pf_fire = wr_pf(fire)
        n_nofire, wr_nofire, pf_nofire = wr_pf(nofire)
        w_fire = int((fire["net_pips"] > 0).sum())
        w_nofire = int((nofire["net_pips"] > 0).sum())
        p = ztest(w_fire, n_fire, w_nofire, n_nofire)
        p_str = f"{p:.5f}" if p is not None else "n/a"
        survives = (p is not None and p < corrected_alpha)
        print(f"[{label}] fire: n={n_fire} WR={wr_fire*100:.1f}% PF={pf_fire:.3f} | "
              f"no-fire: n={n_nofire} WR={wr_nofire*100:.1f}% PF={pf_nofire:.3f} | "
              f"p={p_str} (bar<{corrected_alpha:.5f}) -> {'SURVIVES' if survives else 'does NOT survive'}")
        if survives:
            survivors.append(label)
    print()
    print(f"Plain-z-test holdout survivors (still need the cluster-bootstrap check next): {len(survivors)}")
    for s in survivors:
        print(f"  {s}")


if __name__ == "__main__":
    main()
