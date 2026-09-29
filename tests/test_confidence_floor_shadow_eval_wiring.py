"""Tests for the 2026-09-29 wiring of confidence_floor_underperforms_conf6
into the real shadow_mode.record_evaluation() feed in
src/research_outcome_checker.py.

This rule was registered by hand weeks ago (see PROMOTION_DISCIPLINE.md)
on a discovery sample showing a striking degradation from conf==6 (best)
to conf==8 (worst) -- but nothing ever fed record_evaluation() for it, so
it sat at 0 fresh evaluations indefinitely, exactly the gap
technical_carries_divergence and sentiment_agent_supports each had before
their own feeds were wired. Mirrors _record_divergence_evaluation()'s
exact shape: would_fire is a pure function of a field already persisted
on the row, this is pure observability that never touches the trade's own
fields and never influences any real decision.
"""
import unittest
from unittest.mock import patch, MagicMock

from src import research_outcome_checker as roc


class TestConfidenceFloorWouldFire(unittest.TestCase):

    def test_fires_at_the_gate_confidence(self):
        row = {"confidence": 7}
        self.assertTrue(roc._confidence_floor_would_fire(row))

    def test_fires_above_the_gate_confidence(self):
        row = {"confidence": 9}
        self.assertTrue(roc._confidence_floor_would_fire(row))

    def test_does_not_fire_at_conf_six(self):
        row = {"confidence": 6}
        self.assertFalse(roc._confidence_floor_would_fire(row))

    def test_returns_none_below_conf_six(self):
        """This rule's pre-registered comparison is scoped exactly to
        conf==6 vs conf>=7 -- conf<=5 is outside its population, same as
        a missing score is for the divergence rule."""
        row = {"confidence": 5}
        self.assertIsNone(roc._confidence_floor_would_fire(row))

    def test_returns_none_when_confidence_missing(self):
        row = {}
        self.assertIsNone(roc._confidence_floor_would_fire(row))

    def test_returns_none_when_confidence_unparseable(self):
        row = {"confidence": "n/a"}
        self.assertIsNone(roc._confidence_floor_would_fire(row))


class TestRecordConfidenceFloorEvaluation(unittest.TestCase):

    def test_records_evaluation_with_correct_shape(self):
        row = {"id": 9001, "pair": "EUR/USD", "direction": "BUY",
               "status": "WIN", "net_pips": 55.0, "confidence": 6}
        fake_sm = MagicMock()
        with patch("src.shadow_mode.register_rule", fake_sm.register_rule), \
             patch("src.shadow_mode.record_evaluation", fake_sm.record_evaluation):
            roc._record_confidence_floor_evaluation(row)

        fake_sm.register_rule.assert_called_once()
        self.assertEqual(fake_sm.register_rule.call_args[0][0],
                          "confidence_floor_underperforms_conf6")
        fake_sm.record_evaluation.assert_called_once()
        call = fake_sm.record_evaluation.call_args
        self.assertEqual(call[0][0], "confidence_floor_underperforms_conf6")
        self.assertEqual(call[1]["would_fire"], False)
        self.assertEqual(call[1]["outcome"], "WIN")
        self.assertEqual(call[1]["net_pips"], 55.0)

    def test_records_would_fire_true_for_conf_seven(self):
        row = {"id": 9002, "pair": "GBP/USD", "status": "LOSS",
               "net_pips": -40.0, "confidence": 7}
        fake_sm = MagicMock()
        with patch("src.shadow_mode.register_rule", fake_sm.register_rule), \
             patch("src.shadow_mode.record_evaluation", fake_sm.record_evaluation):
            roc._record_confidence_floor_evaluation(row)
        call = fake_sm.record_evaluation.call_args
        self.assertEqual(call[1]["would_fire"], True)

    def test_skips_recording_outside_the_comparison_population(self):
        row = {"id": 1, "pair": "EUR/USD", "status": "WIN", "net_pips": 100,
               "confidence": 4}
        fake_sm = MagicMock()
        with patch("src.shadow_mode.register_rule", fake_sm.register_rule), \
             patch("src.shadow_mode.record_evaluation", fake_sm.record_evaluation):
            roc._record_confidence_floor_evaluation(row)
        fake_sm.record_evaluation.assert_not_called()

    def test_never_raises_if_shadow_mode_itself_errors(self):
        row = {"id": 1, "pair": "EUR/USD", "status": "WIN", "net_pips": 100,
               "confidence": 6}
        with patch("src.shadow_mode.register_rule", side_effect=RuntimeError("boom")):
            try:
                roc._record_confidence_floor_evaluation(row)
            except Exception as exc:
                self.fail(f"_record_confidence_floor_evaluation raised {exc!r} -- "
                          f"must never break the real close path")

    def test_never_mutates_the_trade_row(self):
        row = {"id": 9001, "pair": "EUR/USD", "direction": "BUY",
               "status": "WIN", "net_pips": 55.0, "confidence": 6}
        original = dict(row)
        with patch("src.shadow_mode.register_rule"), \
             patch("src.shadow_mode.record_evaluation"):
            roc._record_confidence_floor_evaluation(row)
        self.assertEqual(row, original)


if __name__ == "__main__":
    unittest.main()
