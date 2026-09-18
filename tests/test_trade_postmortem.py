"""Tests for src/trade_postmortem.py -- the 2026-09-19 structured, queryable
postmortem record built automatically for every real fund trade close (win,
loss, or expiry), so future investigations can query confidence/grade/
ribbon-alignment/divergence/risk-factor-materialization directly instead of
re-reading raw report text by hand.

Pure observability: every test here confirms the module never raises, never
mutates the input row, and never writes anywhere except its own companion
log (mocked in every test that touches disk).
"""
import json
import unittest
from unittest.mock import patch, mock_open, MagicMock

from src import trade_postmortem as pm


class TestClassifyRibbonAlignment(unittest.TestCase):

    def test_buy_strongly_against(self):
        self.assertEqual(pm.classify_ribbon_alignment("BUY", "ALIGNED_BEAR"), "strongly_against")

    def test_buy_against(self):
        self.assertEqual(pm.classify_ribbon_alignment("BUY", "LEANING_BEAR"), "against")

    def test_buy_aligned(self):
        self.assertEqual(pm.classify_ribbon_alignment("BUY", "ALIGNED_BULL"), "aligned")
        self.assertEqual(pm.classify_ribbon_alignment("BUY", "LEANING_BULL"), "aligned")

    def test_sell_strongly_against(self):
        self.assertEqual(pm.classify_ribbon_alignment("SELL", "ALIGNED_BULL"), "strongly_against")

    def test_sell_against(self):
        self.assertEqual(pm.classify_ribbon_alignment("SELL", "LEANING_BULL"), "against")

    def test_sell_aligned(self):
        self.assertEqual(pm.classify_ribbon_alignment("SELL", "ALIGNED_BEAR"), "aligned")

    def test_neutral_states(self):
        self.assertEqual(pm.classify_ribbon_alignment("BUY", "NEUTRAL"), "neutral")
        self.assertEqual(pm.classify_ribbon_alignment("BUY", "CONVERGING"), "neutral")

    def test_unknown_when_direction_missing(self):
        self.assertEqual(pm.classify_ribbon_alignment("", "ALIGNED_BULL"), "unknown")
        self.assertEqual(pm.classify_ribbon_alignment(None, "ALIGNED_BULL"), "unknown")

    def test_unknown_when_ribbon_state_missing(self):
        self.assertEqual(pm.classify_ribbon_alignment("BUY", ""), "unknown")
        self.assertEqual(pm.classify_ribbon_alignment("BUY", None), "unknown")

    def test_case_insensitive(self):
        self.assertEqual(pm.classify_ribbon_alignment("buy", "aligned_bear"), "strongly_against")


class TestComputeDivergence(unittest.TestCase):

    def test_chfjpy_7325_shape(self):
        # tech=8, others average 4.5 -> divergence=3.5, this month's discovery shape
        self.assertEqual(pm.compute_divergence(8, 4, 5, 4, 5), 3.5)

    def test_zero_divergence_when_agreeing(self):
        self.assertEqual(pm.compute_divergence(6, 6, 6, 6, 6), 0.0)

    def test_negative_divergence(self):
        self.assertEqual(pm.compute_divergence(4, 8, 8, 8, 8), -4.0)

    def test_none_when_any_score_missing(self):
        self.assertIsNone(pm.compute_divergence(8, 4, 5, 4, None))
        self.assertIsNone(pm.compute_divergence(8, 4, 5, 4, ""))

    def test_none_when_any_score_unparseable(self):
        self.assertIsNone(pm.compute_divergence(8, "n/a", 5, 4, 5))


class TestExtractRiskFactorTags(unittest.TestCase):

    def test_empty_text_returns_no_tags(self):
        self.assertEqual(pm.extract_risk_factor_tags(""), [])
        self.assertEqual(pm.extract_risk_factor_tags(None), [])
        self.assertEqual(pm.extract_risk_factor_tags("   "), [])

    def test_ribbon_keyword(self):
        self.assertIn("ribbon_opposition", pm.extract_risk_factor_tags("EMA ribbon is against the trade direction"))

    def test_rate_differential_keyword(self):
        self.assertIn("rate_differential_opposition",
                       pm.extract_risk_factor_tags("Rate differential opposition could pressure this pair"))

    def test_positioning_keyword(self):
        self.assertIn("positioning_extreme_or_reversing",
                       pm.extract_risk_factor_tags("COT positioning is crowded on the long side"))

    def test_mtf_weekly_keyword(self):
        self.assertIn("mtf_weekly_conflict",
                       pm.extract_risk_factor_tags("Weekly trend disagrees with the 4H setup"))

    def test_trend_structure_keyword(self):
        self.assertIn("trend_structure_risk",
                       pm.extract_risk_factor_tags("Price is approaching a key resistance level"))

    def test_multiple_tags_sorted(self):
        text = "Ribbon opposition and crowded COT positioning are both concerns near resistance."
        tags = pm.extract_risk_factor_tags(text)
        self.assertEqual(tags, sorted(tags))
        self.assertIn("ribbon_opposition", tags)
        self.assertIn("positioning_extreme_or_reversing", tags)
        self.assertIn("trend_structure_risk", tags)

    def test_no_match_returns_empty(self):
        self.assertEqual(pm.extract_risk_factor_tags("Everything looks clean, no concerns noted."), [])

    def test_case_insensitive(self):
        self.assertIn("ribbon_opposition", pm.extract_risk_factor_tags("RIBBON is against direction"))


class TestDetermineMaterialization(unittest.TestCase):

    def test_all_true_when_loss(self):
        tags = ["ribbon_opposition", "trend_structure_risk"]
        result = pm.determine_materialization(tags, True)
        self.assertEqual(result, {"ribbon_opposition": True, "trend_structure_risk": True})

    def test_all_false_when_win(self):
        tags = ["ribbon_opposition"]
        result = pm.determine_materialization(tags, False)
        self.assertEqual(result, {"ribbon_opposition": False})

    def test_all_none_when_outcome_unknown(self):
        tags = ["ribbon_opposition", "mtf_weekly_conflict"]
        result = pm.determine_materialization(tags, None)
        self.assertEqual(result, {"ribbon_opposition": None, "mtf_weekly_conflict": None})

    def test_empty_tags_returns_empty_dict(self):
        self.assertEqual(pm.determine_materialization([], True), {})


class TestReconstructGradeBestEffort(unittest.TestCase):

    def test_high_confidence_high_rr_grades_a(self):
        row = {"confidence": 8, "reward_risk": 3.0, "direction": "BUY",
               "ribbon_state_at_entry": "ALIGNED_BULL", "pair": "EUR/USD"}
        self.assertEqual(pm.reconstruct_grade_best_effort(row), "A")

    def test_moderate_confidence_grades_b(self):
        row = {"confidence": 7, "reward_risk": 2.0, "direction": "BUY",
               "ribbon_state_at_entry": "NEUTRAL", "pair": "EUR/USD"}
        self.assertEqual(pm.reconstruct_grade_best_effort(row), "B")

    def test_low_rr_grades_d(self):
        row = {"confidence": 8, "reward_risk": 1.2, "direction": "BUY",
               "ribbon_state_at_entry": "NEUTRAL", "pair": "EUR/USD"}
        self.assertEqual(pm.reconstruct_grade_best_effort(row), "D")

    def test_weekly_monthly_conflict_grades_f(self):
        row = {"confidence": 8, "reward_risk": 3.0, "direction": "BUY",
               "ribbon_state_at_entry": "NEUTRAL", "pair": "EUR/USD",
               "weekly_trend_at_entry": "BUY", "monthly_trend_at_entry": "SELL"}
        self.assertEqual(pm.reconstruct_grade_best_effort(row), "F")

    def test_gbp_cross_strongly_against_ribbon_grades_f(self):
        row = {"confidence": 8, "reward_risk": 3.0, "direction": "BUY",
               "ribbon_state_at_entry": "ALIGNED_BEAR", "pair": "GBP/USD"}
        self.assertEqual(pm.reconstruct_grade_best_effort(row), "F")

    def test_chf_cluster_strongly_against_ribbon_grades_f(self):
        row = {"confidence": 8, "reward_risk": 3.0, "direction": "SELL",
               "ribbon_state_at_entry": "ALIGNED_BULL", "pair": "EUR/CHF"}
        self.assertEqual(pm.reconstruct_grade_best_effort(row), "F")

    def test_non_scoped_pair_strongly_against_ribbon_not_auto_f(self):
        # general-population ribbon-opposition F/D demotion was removed
        # (ribbon_general_population_demotion_removed, promoted 2026-09-06) --
        # only GBP-crosses/CHF-cluster still apply the ribbon-scoped penalty.
        row = {"confidence": 8, "reward_risk": 3.0, "direction": "BUY",
               "ribbon_state_at_entry": "ALIGNED_BEAR", "pair": "USD/JPY"}
        self.assertEqual(pm.reconstruct_grade_best_effort(row), "A")

    def test_missing_fields_do_not_raise(self):
        self.assertIn(pm.reconstruct_grade_best_effort({}), ("A", "B", "C", "D", "F"))

    def test_unparseable_confidence_does_not_raise(self):
        row = {"confidence": "n/a", "reward_risk": "bad", "direction": "BUY", "pair": "EUR/USD"}
        self.assertIn(pm.reconstruct_grade_best_effort(row), ("A", "B", "C", "D", "F"))


class TestBuildPostmortemRecord(unittest.TestCase):

    def test_full_loss_record_shape(self):
        row = {
            "id": "7325", "pair": "CHF/JPY", "direction": "BUY", "status": "LOSS",
            "closed_at": "2026-09-10 12:00:00", "confidence": 7,
            "technical": 8, "fundamental": 4, "sentiment": 5, "positioning": 4, "macro": 5,
            "ribbon_state_at_entry": "ALIGNED_BEAR",
            "risk_factors": "EMA ribbon opposition and crowded COT positioning near resistance.",
            "net_pips": -186.5, "pips": -186.5,
        }
        rec = pm.build_postmortem_record(row)
        self.assertEqual(rec["id"], "7325")
        self.assertEqual(rec["pair"], "CHF/JPY")
        self.assertEqual(rec["status"], "LOSS")
        self.assertEqual(rec["confidence"], 7.0)
        self.assertEqual(rec["divergence"], 3.5)
        self.assertEqual(rec["ribbon_alignment"], "strongly_against")
        self.assertIn("ribbon_opposition", rec["risk_factor_tags"])
        self.assertIn("positioning_extreme_or_reversing", rec["risk_factor_tags"])
        self.assertIn("trend_structure_risk", rec["risk_factor_tags"])
        self.assertTrue(rec["risk_factor_materialized"]["ribbon_opposition"])
        self.assertTrue(rec["outcome_is_loss"])
        self.assertEqual(rec["net_pips"], -186.5)
        self.assertIn(rec["grade_reconstructed"], ("A", "B", "C", "D", "F"))

    def test_win_record_materialization_all_false(self):
        row = {
            "id": 1, "pair": "EUR/USD", "direction": "SELL", "status": "WIN",
            "confidence": 8, "ribbon_state_at_entry": "ALIGNED_BULL",
            "risk_factors": "Ribbon is against the trade.",
            "net_pips": 150.0,
        }
        rec = pm.build_postmortem_record(row)
        self.assertFalse(rec["outcome_is_loss"])
        self.assertFalse(rec["risk_factor_materialized"]["ribbon_opposition"])

    def test_no_risk_factors_gives_empty_tags(self):
        row = {"id": 2, "pair": "EUR/USD", "direction": "BUY", "status": "EXPIRED", "net_pips": -5.0}
        rec = pm.build_postmortem_record(row)
        self.assertEqual(rec["risk_factor_tags"], [])
        self.assertEqual(rec["risk_factor_materialized"], {})

    def test_falls_back_to_gross_pips_when_net_pips_missing(self):
        row = {"id": 3, "pair": "EUR/USD", "direction": "BUY", "status": "LOSS", "pips": -20.0}
        rec = pm.build_postmortem_record(row)
        self.assertEqual(rec["net_pips"], -20.0)
        self.assertTrue(rec["outcome_is_loss"])

    def test_unknown_outcome_when_no_pnl_field_present(self):
        row = {"id": 4, "pair": "EUR/USD", "direction": "BUY", "status": "OPEN"}
        rec = pm.build_postmortem_record(row)
        self.assertIsNone(rec["outcome_is_loss"])

    def test_never_mutates_input_row(self):
        row = {
            "id": 5, "pair": "EUR/USD", "direction": "BUY", "status": "LOSS",
            "confidence": 7, "net_pips": -10.0, "risk_factors": "ribbon opposition",
        }
        original = dict(row)
        pm.build_postmortem_record(row)
        self.assertEqual(row, original)


class TestRecordPostmortem(unittest.TestCase):
    """All file I/O mocked -- per the explicit instruction that this
    always-running infrastructure must never touch real disk in tests."""

    def test_writes_new_log_when_file_absent(self):
        record = {"id": "42", "pair": "EUR/USD"}
        m = mock_open()
        with patch.object(pm.POSTMORTEM_LOG, "exists", return_value=False), \
             patch.object(pm.POSTMORTEM_LOG, "write_text") as mock_write, \
             patch.object(pm.POSTMORTEM_LOG.parent, "mkdir"):
            pm.record_postmortem(record)
        mock_write.assert_called_once()
        written = json.loads(mock_write.call_args[0][0])
        self.assertEqual(written["42"], record)

    def test_merges_into_existing_log(self):
        record = {"id": "2", "pair": "GBP/USD"}
        existing = json.dumps({"1": {"id": "1", "pair": "EUR/USD"}})
        with patch.object(pm.POSTMORTEM_LOG, "exists", return_value=True), \
             patch.object(pm.POSTMORTEM_LOG, "read_text", return_value=existing), \
             patch.object(pm.POSTMORTEM_LOG, "write_text") as mock_write, \
             patch.object(pm.POSTMORTEM_LOG.parent, "mkdir"):
            pm.record_postmortem(record)
        written = json.loads(mock_write.call_args[0][0])
        self.assertIn("1", written)
        self.assertIn("2", written)

    def test_idempotent_replaces_same_id(self):
        record_v2 = {"id": "1", "pair": "EUR/USD", "status": "WIN"}
        existing = json.dumps({"1": {"id": "1", "pair": "EUR/USD", "status": "OPEN"}})
        with patch.object(pm.POSTMORTEM_LOG, "exists", return_value=True), \
             patch.object(pm.POSTMORTEM_LOG, "read_text", return_value=existing), \
             patch.object(pm.POSTMORTEM_LOG, "write_text") as mock_write, \
             patch.object(pm.POSTMORTEM_LOG.parent, "mkdir"):
            pm.record_postmortem(record_v2)
        written = json.loads(mock_write.call_args[0][0])
        self.assertEqual(len(written), 1)
        self.assertEqual(written["1"]["status"], "WIN")

    def test_corrupt_existing_log_does_not_raise_and_resets(self):
        with patch.object(pm.POSTMORTEM_LOG, "exists", return_value=True), \
             patch.object(pm.POSTMORTEM_LOG, "read_text", return_value="not json"), \
             patch.object(pm.POSTMORTEM_LOG, "write_text") as mock_write, \
             patch.object(pm.POSTMORTEM_LOG.parent, "mkdir"):
            pm.record_postmortem({"id": "1", "pair": "EUR/USD"})
        written = json.loads(mock_write.call_args[0][0])
        self.assertEqual(list(written.keys()), ["1"])

    def test_never_raises_when_write_fails(self):
        with patch.object(pm.POSTMORTEM_LOG, "exists", side_effect=RuntimeError("disk full")):
            try:
                pm.record_postmortem({"id": "1", "pair": "EUR/USD"})
            except Exception as exc:
                self.fail(f"record_postmortem raised {exc!r} -- must never break the real close path")


class TestRecordTradePostmortem(unittest.TestCase):

    def test_builds_and_records(self):
        row = {"id": "9", "pair": "EUR/USD", "direction": "BUY", "status": "WIN", "net_pips": 50.0}
        with patch.object(pm, "record_postmortem") as mock_record:
            result = pm.record_trade_postmortem(row)
        mock_record.assert_called_once()
        self.assertEqual(result["id"], "9")
        self.assertEqual(result["status"], "WIN")

    def test_never_raises_when_build_fails(self):
        with patch.object(pm, "build_postmortem_record", side_effect=RuntimeError("boom")):
            try:
                result = pm.record_trade_postmortem({"id": "1"})
            except Exception as exc:
                self.fail(f"record_trade_postmortem raised {exc!r} -- must never break the real close path")
        self.assertEqual(result, {})

    def test_never_raises_when_record_fails(self):
        row = {"id": "1", "pair": "EUR/USD", "status": "WIN"}
        with patch.object(pm, "record_postmortem", side_effect=RuntimeError("boom")):
            try:
                pm.record_trade_postmortem(row)
            except Exception as exc:
                self.fail(f"record_trade_postmortem raised {exc!r} -- must never break the real close path")

    def test_does_not_mutate_input_row(self):
        row = {"id": "1", "pair": "EUR/USD", "direction": "BUY", "status": "WIN", "net_pips": 10.0}
        original = dict(row)
        with patch.object(pm, "record_postmortem"):
            pm.record_trade_postmortem(row)
        self.assertEqual(row, original)


if __name__ == "__main__":
    unittest.main()
