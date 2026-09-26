"""Cluster-bootstrap stress test for the mechanical edge-mining research
loop's HOLDOUT survivors -- the same rigor already applied to phenomenon 1
(rib_against / mr_stack_2way), now (a) reconfirmed against this shared
library implementation before trusting it on anything new, and (b) applied
to whatever phenomena 2/3 candidates survive their own holdout plain
z-test (see scripts/edge_mining_holdout_check_round4.py).

Part A reconfirms the already-reported result: rib_against ALONE does not
survive cluster-bootstrap (regimes ran up to 232 days -- a handful of long
regimes drove the raw per-row significance), while rib_against AND
osc_agrees (the combined signal Book G actually trades) does. If this
script's numbers don't land close to the previously-reported p=0.052 /
p=0.0060, something in this reimplementation is wrong and nothing built on
top of it should be trusted.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.edge_mining_cluster_bootstrap_lib import (
    cluster_bootstrap_for_condition, regime_length_stats,
)

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "mechanical_edge_mining_dataset.csv"


def load_holdout():
    df = pd.read_csv(DATA_PATH)
    hold = df[df["split"] == "holdout"].copy()
    hold["date"] = pd.to_datetime(hold["date"])
    hold["macd_agrees_direction"] = (
        (hold["direction"] == "BUY") & hold["macd_bullish"]
    ) | (
        (hold["direction"] == "SELL") & ~hold["macd_bullish"]
    )
    # "Decisive" here means net_pips != 0 (the handful of exact-zero rows
    # excluded) -- EXPIRED rows are KEPT and classified by net_pips sign.
    # Verified directly against historical_grading_backtest.py's own
    # _is_win() (status==WIN -> True, status==LOSS -> False, EXPIRED ->
    # net_pips>0) before trusting this: an initial version of this script
    # filtered to status.isin(["WIN","LOSS"]) only, which is WRONG -- it
    # contradicted this codebase's own established, precedented convention
    # (also src/shadow_mode.py::_is_win()'s PARTIAL_WIN-by-net_pips-sign
    # rule) and materially changed the population (EXPIRED is 51% of
    # holdout rows). The mechanical_edge_mining_dataset.py module docstring
    # claiming "EXPIRED excluded... same convention as
    # historical_grading_backtest.py" is itself misleading on this point --
    # the real reference script does NOT exclude EXPIRED, it classifies it
    # by net_pips sign, exactly what round 1-3's own wr_pf() already did.
    dec = hold[hold["net_pips"] != 0].copy()
    return dec


def run_one(label, df, condition):
    stats = regime_length_stats(df, condition)
    result = cluster_bootstrap_for_condition(df, condition)
    if result is None:
        print(f"[{label}] insufficient clustered data -- skipped")
        return None
    p, n_fire, n_nofire, wr_fire, wr_nofire = result
    print(f"[{label}]")
    print(f"    regime stats (fire side): n_clusters={stats['n_clusters']} "
          f"n_rows={stats['n_rows']} max_cluster={stats['max_cluster_size']} "
          f"mean_cluster={stats['mean_cluster_size']:.1f} median={stats['median_cluster_size']:.1f}")
    print(f"    n_fire_clusters={n_fire} n_nofire_clusters={n_nofire} "
          f"WR_fire={wr_fire*100:.1f}% WR_nofire={wr_nofire*100:.1f}%  p={p:.5f}")
    return p, n_fire, n_nofire, wr_fire, wr_nofire


def main():
    dec = load_holdout()
    dec["counter_trend_200ma"] = ~dec["with_trend_200ma"]
    dec["counter_trend_macd"] = ~dec["macd_agrees_direction"]
    dec["phenomenon2_stack"] = dec["counter_trend_200ma"] & dec["counter_trend_macd"]
    dec["bb_extreme_agrees"] = (
        ((dec["direction"] == "BUY") & (dec["bb_position"] <= 0.2)) |
        ((dec["direction"] == "SELL") & (dec["bb_position"] >= 0.8))
    )
    print(f"Holdout decisive (WIN/LOSS-or-nonzero-EXPIRED) rows: n={len(dec)}")
    print()
    print("=== PART A: reconfirm phenomenon 1's already-reported cluster-bootstrap result ===")
    run_one("rib_against ALONE", dec, dec["rib_against"])
    run_one("mr_stack_2way (rib_against AND osc_agrees)", dec, dec["rib_against"] & dec["osc_agrees"])
    run_one("mr_stack_2way BUY-only", dec,
            dec["rib_against"] & dec["osc_agrees"] & (dec["direction"] == "BUY"))
    print()
    print("=== PART B: round 4's plain-z-test holdout survivors, phenomena 2 & 3 ===")
    run_one("osc_agrees ALONE", dec, dec["osc_agrees"])
    run_one("phenomenon2_stack AND osc_agrees", dec, dec["phenomenon2_stack"] & dec["osc_agrees"])
    run_one("rib_against AND phenomenon2_stack (no osc)", dec, dec["rib_against"] & dec["phenomenon2_stack"])
    run_one("bb_extreme_agrees", dec, dec["bb_extreme_agrees"])


if __name__ == "__main__":
    main()
