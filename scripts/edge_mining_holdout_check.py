"""The single, irreversible holdout check for the 2026-09-25 mechanical
edge-mining research loop. Run EXACTLY ONCE, after rounds 1+ on the
discovery slice are fully finished -- no peeking at holdout before this,
no re-running the discovery search after seeing this file's output.

CANDIDATES below must be filled in by hand from whatever rounds 1+ found
promising on the discovery slice ONLY, before this script is ever run.
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


# Filled in after rounds 1-3 concluded on the discovery slice only. A small,
# disciplined set chosen BEFORE looking at holdout -- not everything explored:
#   1. rib_against alone -- the simplest, most basic, most-precedented signal
#      (already known from shadow_mode/health_check work), as a sanity anchor.
#   2. mr_stack_2way -- the combined ribbon+oscillator signal, overall.
#   3. mr_stack_2way BUY-only -- the most specific, most promising candidate,
#      with the caveat (found and disclosed in round 3) that part of its
#      apparent strength rides a broader BUY>SELL base-rate skew in this
#      exact 3-year window, not purely the signal itself.
CANDIDATES = [
    ("rib_against", lambda d: d["rib_against"]),
    ("mr_stack_2way (rib_against AND osc_agrees)",
        lambda d: d["rib_against"] & d["osc_agrees"]),
    ("mr_stack_2way BUY-only",
        lambda d: d["rib_against"] & d["osc_agrees"] & (d["direction"] == "BUY")),
]


def main():
    df = pd.read_csv(DATA_PATH)
    hold = df[df["split"] == "holdout"].copy()
    hold["macd_agrees_direction"] = (
        (hold["direction"] == "BUY") & hold["macd_bullish"]
    ) | (
        (hold["direction"] == "SELL") & ~hold["macd_bullish"]
    )
    print(f"HOLDOUT CHECK -- n={len(hold)} rows, touched for the first time now.")
    baseline_dec, baseline_wr, baseline_pf = wr_pf(hold)
    print(f"Holdout baseline: n_decisive={baseline_dec} WR={baseline_wr*100:.1f}% PF={baseline_pf:.3f}")
    print()
    corrected_alpha = 0.05 / max(1, len(CANDIDATES))
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


if __name__ == "__main__":
    main()
