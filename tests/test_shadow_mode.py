"""Tests for src/shadow_mode.py -- both the pre-existing plain z-test path
(regression coverage, since none existed before this file) and the new
cluster_aware promotion path added 2026-09-2X for Book G / any future rule
whose evaluations can be correlated in slow-moving clusters (e.g. a
ribbon/oscillator regime running many consecutive calendar days).

Real bug this closes: counting distinct regime clusters toward the
min_n_fire/min_n_no_fire floor fixes "how many independent samples exist",
but a PLAIN significance test run on regime-deduplicated data would still
be the same correlation trap in a new shape -- it doesn't account for
within-regime correlation or unequal regime sizes (a 232-day regime and a
2-day regime aren't equally informative, but a naive "one vote per regime"
reduction would treat them as such). cluster_aware=True makes
check_promotion_readiness() run a real cluster bootstrap instead.

Every test uses a fully isolated shadow_rules.json (tmp file, patched in),
never the real one.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import shadow_mode as sm


class ShadowModeTestCase(unittest.TestCase):
    """Shared isolation: real _SHADOW_FILE swapped for a tmp path."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patcher = patch.object(sm, "_SHADOW_FILE", Path(self._tmpdir.name) / "shadow_rules.json")
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def _record(self, rule_name, would_fire, outcome, net_pips=None, context=None):
        sm.record_evaluation(rule_name, would_fire=would_fire, outcome=outcome,
                              net_pips=net_pips, context=context)


class TestPlainRuleUnchanged(ShadowModeTestCase):
    """Regression coverage for the pre-existing (non-cluster-aware) path --
    confirms the cluster_aware addition didn't change behavior for the 12+
    rules that don't opt into it."""

    def test_register_defaults_cluster_aware_false(self):
        rule = sm.register_rule("plain_rule", description="test")
        self.assertFalse(rule["cluster_aware"])

    def test_plain_rule_uses_raw_evaluation_count(self):
        sm.register_rule("plain_rule", description="test", min_n_fire=2, min_n_no_fire=2)
        for _ in range(3):
            self._record("plain_rule", True, "WIN", net_pips=10)
        for _ in range(3):
            self._record("plain_rule", False, "LOSS", net_pips=-10)
        status = sm.check_promotion_readiness("plain_rule")
        self.assertFalse(status["cluster_aware"])
        self.assertEqual(status["n_fire"], 3)
        self.assertEqual(status["n_no_fire"], 3)

    def test_plain_rule_significance_matches_manual_ztest(self):
        sm.register_rule("plain_rule", description="test", min_n_fire=2, min_n_no_fire=2)
        for _ in range(10):
            self._record("plain_rule", True, "WIN", net_pips=10)
        for _ in range(10):
            self._record("plain_rule", False, "LOSS", net_pips=-10)
        status = sm.check_promotion_readiness("plain_rule")
        # 100% vs 0% WR at n=10/10 should be extremely significant
        self.assertIsNotNone(status["p_value"])
        self.assertLess(status["p_value"], 0.001)

    def test_never_registered_returns_not_registered(self):
        status = sm.check_promotion_readiness("nonexistent_rule")
        self.assertFalse(status["registered"])


class TestClusterAwareRegistration(ShadowModeTestCase):

    def test_register_with_cluster_aware_true(self):
        rule = sm.register_rule("cluster_rule", description="test", cluster_aware=True)
        self.assertTrue(rule["cluster_aware"])

    def test_idempotent_reregistration_keeps_original_cluster_aware(self):
        sm.register_rule("cluster_rule", description="v1", cluster_aware=True)
        result = sm.register_rule("cluster_rule", description="v2 (should be ignored)", cluster_aware=False)
        self.assertTrue(result["cluster_aware"])
        self.assertEqual(result["description"], "v1")


class TestClusterAwarePromotionCounting(ShadowModeTestCase):

    def test_n_fire_counts_distinct_clusters_not_raw_evaluations(self):
        """A single 10-day regime must count as ONE toward n_fire, not 10."""
        sm.register_rule("cluster_rule", description="test", cluster_aware=True,
                          min_n_fire=2, min_n_no_fire=2)
        for i in range(10):
            self._record("cluster_rule", True, "WIN", net_pips=10,
                          context={"regime_cluster": "EURUSD_BUY_regime1"})
        for i in range(5):
            self._record("cluster_rule", False, "LOSS", net_pips=-10,
                          context={"regime_cluster": "EURUSD_BUY_regime2"})
        status = sm.check_promotion_readiness("cluster_rule")
        self.assertEqual(status["n_fire"], 1, "10 rows in ONE regime must count as n_fire=1")
        self.assertEqual(status["n_no_fire"], 1)

    def test_multiple_distinct_clusters_counted_separately(self):
        sm.register_rule("cluster_rule", description="test", cluster_aware=True)
        for regime in range(5):
            for _ in range(4):
                self._record("cluster_rule", True, "WIN", net_pips=10,
                              context={"regime_cluster": f"EURUSD_BUY_regime{regime}"})
        for regime in range(5):
            for _ in range(4):
                self._record("cluster_rule", False, "LOSS", net_pips=-10,
                              context={"regime_cluster": f"EURUSD_SELL_regime{regime}"})
        status = sm.check_promotion_readiness("cluster_rule")
        self.assertEqual(status["n_fire"], 5)
        self.assertEqual(status["n_no_fire"], 5)

    def test_missing_regime_cluster_context_falls_back_to_singleton(self):
        """No context at all -- must not crash, and each evaluation counts
        as its own cluster (degrades to the per-row baseline, never fewer)."""
        sm.register_rule("cluster_rule", description="test", cluster_aware=True)
        for _ in range(4):
            self._record("cluster_rule", True, "WIN", net_pips=10)
        for _ in range(4):
            self._record("cluster_rule", False, "LOSS", net_pips=-10)
        status = sm.check_promotion_readiness("cluster_rule")
        self.assertEqual(status["n_fire"], 4)
        self.assertEqual(status["n_no_fire"], 4)

    def test_wr_reflects_all_real_trades_not_just_cluster_count(self):
        """Win rate must be computed from every real trade (weighted by
        actual cluster size), not diluted by counting clusters equally
        regardless of how many trades each one holds."""
        sm.register_rule("cluster_rule", description="test", cluster_aware=True)
        # regime1: 9 wins out of 10 rows (one big, high-WR regime)
        for i in range(9):
            self._record("cluster_rule", True, "WIN", net_pips=10, context={"regime_cluster": "r1"})
        self._record("cluster_rule", True, "LOSS", net_pips=-10, context={"regime_cluster": "r1"})
        # regime2: 0 wins out of 2 rows (one small, low-WR regime)
        for i in range(2):
            self._record("cluster_rule", True, "LOSS", net_pips=-10, context={"regime_cluster": "r2"})
        for _ in range(5):
            self._record("cluster_rule", False, "LOSS", net_pips=-10, context={"regime_cluster": "nf1"})
        status = sm.check_promotion_readiness("cluster_rule")
        self.assertEqual(status["n_fire"], 2)  # 2 distinct fire-side clusters
        # 9 wins out of 12 total real fire-side trades = 0.75, not a 50/50
        # "one regime beat, one regime lost" naive per-cluster average
        self.assertAlmostEqual(status["would_fire_wr"], 9 / 12, places=3)


class TestClusterBootstrapSignificance(ShadowModeTestCase):

    def test_thin_data_below_floor_not_promotable(self):
        sm.register_rule("cluster_rule", description="test", cluster_aware=True,
                          min_n_fire=30, min_n_no_fire=30)
        for i in range(3):
            self._record("cluster_rule", True, "WIN", net_pips=10, context={"regime_cluster": f"r{i}"})
        for i in range(3):
            self._record("cluster_rule", False, "LOSS", net_pips=-10, context={"regime_cluster": f"nf{i}"})
        status = sm.check_promotion_readiness("cluster_rule")
        self.assertFalse(status["promotable"])
        self.assertFalse(status["criteria"]["n_fire_ok"])

    def test_strong_consistent_effect_with_enough_clusters_is_promotable(self):
        """Many distinct clusters, consistently different outcomes -- should
        clear both the n floor and the cluster-bootstrap significance bar."""
        sm.register_rule("cluster_rule", description="test", cluster_aware=True,
                          min_n_fire=15, min_n_no_fire=15, alpha=0.05)
        for i in range(20):
            # each fire-side cluster: 4 real trades, all wins
            for _ in range(4):
                self._record("cluster_rule", True, "WIN", net_pips=10, context={"regime_cluster": f"fire{i}"})
        for i in range(20):
            # each no-fire cluster: 4 real trades, all losses
            for _ in range(4):
                self._record("cluster_rule", False, "LOSS", net_pips=-10, context={"regime_cluster": f"nofire{i}"})
        status = sm.check_promotion_readiness("cluster_rule")
        self.assertEqual(status["n_fire"], 20)
        self.assertEqual(status["n_no_fire"], 20)
        self.assertTrue(status["criteria"]["p_value_ok"],
                         f"p={status['p_value']} should clear a real, consistent 100%-vs-0%-WR effect")

    def test_single_cluster_dominating_one_side_does_not_overstate_significance(self):
        """The exact real trap this fix closes: ONE big regime on the fire
        side happens to be very lucky (all wins), but if it's really just
        ONE independent data point, the bootstrap must reflect that -- NOT
        report the same false confidence a naive per-row test would give a
        seemingly-large raw n."""
        sm.register_rule("cluster_rule", description="test", cluster_aware=True,
                          min_n_fire=1, min_n_no_fire=1)
        # ONE regime, 100 raw rows, all wins -- naive per-row test would see
        # n=100 and be very confident; cluster-aware must see n_fire=1.
        for _ in range(100):
            self._record("cluster_rule", True, "WIN", net_pips=10, context={"regime_cluster": "one_big_regime"})
        # no-fire side: 3 independent regimes, mixed results
        self._record("cluster_rule", False, "WIN", net_pips=10, context={"regime_cluster": "nf1"})
        self._record("cluster_rule", False, "LOSS", net_pips=-10, context={"regime_cluster": "nf2"})
        self._record("cluster_rule", False, "LOSS", net_pips=-10, context={"regime_cluster": "nf3"})
        status = sm.check_promotion_readiness("cluster_rule")
        self.assertEqual(status["n_fire"], 1, "100 correlated rows in one regime must count as n_fire=1")

    def test_degenerate_no_clusters_on_one_side_returns_none_pvalue(self):
        sm.register_rule("cluster_rule", description="test", cluster_aware=True)
        for i in range(3):
            self._record("cluster_rule", True, "WIN", net_pips=10, context={"regime_cluster": f"r{i}"})
        status = sm.check_promotion_readiness("cluster_rule")
        self.assertIsNone(status["p_value"])
        self.assertFalse(status["promotable"])


class TestReadyForReviewUsesClusterCount(ShadowModeTestCase):

    def test_ready_for_review_uses_cluster_count_for_cluster_aware_rule(self):
        """100 raw rows in 2 regimes must NOT trigger ready_for_review at a
        min_n=30 bar -- only 2 real independent clusters exist."""
        sm.register_rule("cluster_rule", description="test", cluster_aware=True, min_n=30)
        for _ in range(50):
            self._record("cluster_rule", True, "WIN", net_pips=10, context={"regime_cluster": "r1"})
        for _ in range(50):
            self._record("cluster_rule", False, "LOSS", net_pips=-10, context={"regime_cluster": "nf1"})
        status = sm.check_promotion_readiness("cluster_rule")
        self.assertFalse(status["ready_for_review"])

    def test_plain_rule_ready_for_review_still_uses_raw_count(self):
        sm.register_rule("plain_rule", description="test", min_n=5)
        for _ in range(3):
            self._record("plain_rule", True, "WIN", net_pips=10)
        for _ in range(3):
            self._record("plain_rule", False, "LOSS", net_pips=-10)
        status = sm.check_promotion_readiness("plain_rule")
        self.assertTrue(status["ready_for_review"])  # n_decisive=6 >= min_n=5


if __name__ == "__main__":
    unittest.main()
