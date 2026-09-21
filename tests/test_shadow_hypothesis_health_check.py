"""Tests for the 2026-09-19 src/health_check.py::check_shadow_hypothesis_
promotions() -- item 2 of the auto-learning-infrastructure request: a real,
automatic health-check over every registered shadow_mode hypothesis that
surfaces the moment one crosses its own pre-registered promotion bar,
instead of that only being discovered when a human happens to ask.

Every test here mocks src.shadow_mode entirely (list_rules() and
check_promotion_readiness() never touch the real data/shadow_rules.json),
and confirms the function is read-only (never calls
assert_promotion_authorized() or mark_promoted()) and that its flag text is
static per rule -- the mechanism the existing scripts/health_check.py
flag-set dedup relies on to fire exactly once per crossing.
"""
import unittest
from unittest.mock import patch, MagicMock

from src import health_check as hc


def _rule_state(promoted=False):
    return {"promoted": promoted, "description": "test rule", "evaluations": []}


class TestCheckShadowHypothesisPromotions(unittest.TestCase):

    def test_no_registered_rules_returns_no_flags(self):
        with patch("src.shadow_mode.list_rules", return_value={}):
            flags = hc.check_shadow_hypothesis_promotions()
        self.assertEqual(flags, [])

    def test_promotable_rule_produces_a_flag(self):
        with patch("src.shadow_mode.list_rules",
                    return_value={"technical_carries_divergence": _rule_state()}), \
             patch("src.shadow_mode.check_promotion_readiness",
                   return_value={"registered": True, "promotable": True}):
            flags = hc.check_shadow_hypothesis_promotions()
        self.assertEqual(len(flags), 1)
        self.assertIn("technical_carries_divergence", flags[0])
        self.assertIn("PROMOTION_DISCIPLINE.md", flags[0])

    def test_not_promotable_rule_produces_no_flag(self):
        with patch("src.shadow_mode.list_rules",
                    return_value={"confidence_floor_underperforms_conf6": _rule_state()}), \
             patch("src.shadow_mode.check_promotion_readiness",
                   return_value={"registered": True, "promotable": False}):
            flags = hc.check_shadow_hypothesis_promotions()
        self.assertEqual(flags, [])

    def test_already_promoted_rule_is_skipped_entirely(self):
        with patch("src.shadow_mode.list_rules",
                    return_value={"ribbon_general_population_demotion_removed": _rule_state(promoted=True)}), \
             patch("src.shadow_mode.check_promotion_readiness") as mock_check:
            flags = hc.check_shadow_hypothesis_promotions()
        mock_check.assert_not_called()
        self.assertEqual(flags, [])

    def test_multiple_rules_only_promotable_ones_flagged(self):
        rules = {
            "rule_a": _rule_state(),
            "rule_b": _rule_state(),
            "rule_c": _rule_state(promoted=True),
        }
        def _fake_readiness(name):
            return {"registered": True, "promotable": name == "rule_a"}
        with patch("src.shadow_mode.list_rules", return_value=rules), \
             patch("src.shadow_mode.check_promotion_readiness", side_effect=_fake_readiness):
            flags = hc.check_shadow_hypothesis_promotions()
        self.assertEqual(len(flags), 1)
        self.assertIn("rule_a", flags[0])

    def test_never_raises_if_shadow_mode_itself_errors(self):
        with patch("src.shadow_mode.list_rules", side_effect=RuntimeError("boom")):
            try:
                flags = hc.check_shadow_hypothesis_promotions()
            except Exception as exc:
                self.fail(f"check_shadow_hypothesis_promotions raised {exc!r} -- "
                          f"must never break the health-check run")
        self.assertEqual(len(flags), 1)
        self.assertIn("failed to run", flags[0])

    def test_flag_text_is_static_across_different_live_numbers(self):
        """The whole point of a static flag: the exact same text must come
        back regardless of the underlying n/p/PF, so the flag-set dedup in
        scripts/health_check.py fires once per crossing, not once per run."""
        status_v1 = {"registered": True, "promotable": True, "n_fire": 41,
                     "n_no_fire": 909, "p_value": 0.031}
        status_v2 = {"registered": True, "promotable": True, "n_fire": 118,
                     "n_no_fire": 1502, "p_value": 0.0048}
        with patch("src.shadow_mode.list_rules",
                    return_value={"technical_carries_divergence": _rule_state()}):
            with patch("src.shadow_mode.check_promotion_readiness", return_value=status_v1):
                flags_v1 = hc.check_shadow_hypothesis_promotions()
            with patch("src.shadow_mode.check_promotion_readiness", return_value=status_v2):
                flags_v2 = hc.check_shadow_hypothesis_promotions()
        self.assertEqual(flags_v1, flags_v2)

    def test_never_calls_promotion_or_apply_functions(self):
        """Read-only guarantee: this check must never itself authorize or
        mark a promotion -- crossing the bar is surfaced for a human
        conversation, never applied."""
        with patch("src.shadow_mode.list_rules",
                    return_value={"rule_a": _rule_state()}), \
             patch("src.shadow_mode.check_promotion_readiness",
                   return_value={"registered": True, "promotable": True}), \
             patch("src.shadow_mode.assert_promotion_authorized") as mock_assert, \
             patch("src.shadow_mode.mark_promoted") as mock_mark:
            hc.check_shadow_hypothesis_promotions()
        mock_assert.assert_not_called()
        mock_mark.assert_not_called()


class TestWiredIntoRunAllChecks(unittest.TestCase):

    def test_run_all_checks_includes_shadow_promotion_flags(self):
        fake_flag = ["🎯 shadow hypothesis 'rule_a' has crossed its own pre-registered promotion bar"]
        with patch.object(hc, "load_telemetry", return_value=[]), \
             patch.object(hc, "check_universe_coverage", return_value=[]), \
             patch.object(hc, "check_gate_silence", return_value=[]), \
             patch.object(hc, "check_fund_state_staleness", return_value=[]), \
             patch.object(hc, "check_outcome_analysis_gap", return_value=[]), \
             patch.object(hc, "check_duplicate_open_trades", return_value=[]), \
             patch.object(hc, "check_dispatch", return_value=[]), \
             patch.object(hc, "check_grade_ordering", return_value=[]), \
             patch.object(hc, "check_rib_strongly_against_edge_health", return_value=[]), \
             patch.object(hc, "check_currency_consensus_edge_health", return_value=[]), \
             patch.object(hc, "check_ribbon_exclusion_continued_validity", return_value=[]), \
             patch.object(hc, "check_weekly_signal_edge_health", return_value=[]), \
             patch.object(hc, "check_learning_signal_readiness", return_value=[]), \
             patch.object(hc, "check_shadow_hypothesis_promotions", return_value=fake_flag), \
             patch.object(hc, "check_audit_fixes_present", return_value=[]), \
             patch.object(hc, "check_warning_fire_rate", return_value=[]), \
             patch.object(hc, "build_opened_trade_digest", return_value=[]):
            result = hc.run_all_checks()
        self.assertIn(fake_flag[0], result["flags"])


if __name__ == "__main__":
    unittest.main()
