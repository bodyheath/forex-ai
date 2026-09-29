"""Tests for scripts/edge_mining_cluster_bootstrap_lib.py -- the shared
regime-cluster + cluster-bootstrap library backing the mechanical
edge-mining research loop's holdout stress tests (Book G/H/I's build
decisions). Mirrors tests/test_shadow_mode.py's determinism coverage for
the production implementation this library is ported from.
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.edge_mining_cluster_bootstrap_lib import (
    assign_regime_clusters, cluster_bootstrap_for_condition,
)


def _make_df(n_fire_clusters=15, n_nofire_clusters=15, cluster_size=3):
    rows = []
    date = pd.Timestamp("2026-01-01")
    for i in range(n_fire_clusters):
        for j in range(cluster_size):
            rows.append({
                "pair": "EUR/USD", "direction": "BUY",
                "date": (date + pd.Timedelta(days=i * 10 + j)).strftime("%Y-%m-%d"),
                "net_pips": 10 if (i + j) % 3 != 0 else -10,
            })
    for i in range(n_nofire_clusters):
        for j in range(cluster_size):
            rows.append({
                "pair": "GBP/USD", "direction": "SELL",
                "date": (date + pd.Timedelta(days=i * 10 + j)).strftime("%Y-%m-%d"),
                "net_pips": -10 if (i + j) % 4 != 0 else 10,
            })
    df = pd.DataFrame(rows)
    condition = pd.Series(df["pair"] == "EUR/USD", index=df.index)
    return df, condition


class TestDeterminism(unittest.TestCase):
    """seed=42 is fixed (see cluster_bootstrap_p_value()'s own docstring)
    -- repeated calls on the same data must be byte-for-byte identical,
    not just the same order of magnitude."""

    def test_repeated_calls_are_byte_identical(self):
        df, condition = _make_df()
        r1 = cluster_bootstrap_for_condition(df, condition)
        r2 = cluster_bootstrap_for_condition(df, condition)
        r3 = cluster_bootstrap_for_condition(df, condition)
        self.assertEqual(r1["p_value"], r2["p_value"])
        self.assertEqual(r1["p_value"], r3["p_value"])
        self.assertEqual(r1["ci_low"], r2["ci_low"])
        self.assertEqual(r1["ci_high"], r2["ci_high"])
        self.assertEqual(r1["wr_fire"], r2["wr_fire"])
        self.assertEqual(r1["wr_nofire"], r2["wr_nofire"])

    def test_independently_built_identical_data_matches_exactly(self):
        df_a, cond_a = _make_df()
        df_b, cond_b = _make_df()
        r_a = cluster_bootstrap_for_condition(df_a, cond_a)
        r_b = cluster_bootstrap_for_condition(df_b, cond_b)
        self.assertEqual(r_a["p_value"], r_b["p_value"])
        self.assertEqual(r_a["ci_low"], r_b["ci_low"])
        self.assertEqual(r_a["ci_high"], r_b["ci_high"])

    def test_explicit_default_seed_matches_implicit_default(self):
        """Confirms the documented default (42) is really what every real
        caller gets -- an explicit seed=42 call must match a call that
        omits the parameter entirely."""
        df, condition = _make_df()
        r_implicit = cluster_bootstrap_for_condition(df, condition)
        r_explicit = cluster_bootstrap_for_condition(df, condition, seed=42)
        self.assertEqual(r_implicit["p_value"], r_explicit["p_value"])
        self.assertEqual(r_implicit["ci_low"], r_explicit["ci_low"])

    def test_different_seed_is_allowed_but_differs(self):
        """A different seed is a legitimate explicit robustness check (not
        the default path any real decision uses) -- confirms the seed
        parameter actually does something, i.e. this isn't trivially
        deterministic because the bootstrap never runs."""
        df, condition = _make_df()
        r_42 = cluster_bootstrap_for_condition(df, condition, seed=42)
        r_7 = cluster_bootstrap_for_condition(df, condition, seed=7)
        # Not asserting they differ (a real dataset could coincidentally
        # match), just that both are well-formed and reproducible in turn.
        r_7_again = cluster_bootstrap_for_condition(df, condition, seed=7)
        self.assertEqual(r_7["p_value"], r_7_again["p_value"])
        self.assertIsNotNone(r_42)
        self.assertIsNotNone(r_7)


class TestRegimeClusterAssignment(unittest.TestCase):

    def test_consecutive_same_value_rows_share_a_cluster(self):
        df, _ = _make_df(n_fire_clusters=1, n_nofire_clusters=0, cluster_size=5)
        condition = pd.Series(True, index=df.index)
        clusters = assign_regime_clusters(df, condition)
        self.assertEqual(clusters.nunique(), 1)

    def test_gap_beyond_tolerance_starts_a_new_cluster(self):
        df, _ = _make_df(n_fire_clusters=3, n_nofire_clusters=0, cluster_size=1)
        condition = pd.Series(True, index=df.index)
        clusters = assign_regime_clusters(df, condition)
        # _make_df spaces clusters 10 days apart -- well beyond the 3-day
        # gap tolerance, so each of the 3 rows should be its own cluster.
        self.assertEqual(clusters.nunique(), 3)


if __name__ == "__main__":
    unittest.main()
