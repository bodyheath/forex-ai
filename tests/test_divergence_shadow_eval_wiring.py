"""Tests for the 2026-09-13 wiring of technical_carries_divergence into the
real shadow_mode.record_evaluation() feed in src/research_outcome_checker.py.

Phase 16 found a real, economically large PF gap for candidates where
technical_score sharply outpaces the mean of fundamental/sentiment/
positioning/macro, and Phase 17 pre-registered it as a shadow rule (see
PROMOTION_DISCIPLINE.md) -- but registration alone doesn't accumulate
evidence (the same missing-feed gap sentiment_agent_supports had, per that
rule's own comment). This wires a real feed, mirroring
_record_ribbon_carveout_evaluation()'s exact shape: would_fire is a pure
function of fields already persisted on the row, this is pure observability
that never touches the trade's own fields and never influences any real
decision.
"""
import unittest
from unittest.mock import patch, MagicMock

from src import research_outcome_checker as roc


class TestDivergenceWouldFire(unittest.TestCase):

    def test_fires_when_technical_carries(self):
        # CHF/JPY #7325's own shape: tech=8, others average 4.5, divergence=3.5
        row = {"tech_score": 8, "fund_score": 4, "sent_score": 5,
               "pos_score": 4, "macro_score": 5}
        self.assertTrue(roc._divergence_would_fire(row))

    def test_does_not_fire_when_broadly_agreeing(self):
        row = {"tech_score": 7, "fund_score": 7, "sent_score": 6,
               "pos_score": 7, "macro_score": 6}
        self.assertFalse(roc._divergence_would_fire(row))

    def test_boundary_exactly_three_fires(self):
        row = {"tech_score": 8, "fund_score": 5, "sent_score": 5,
               "pos_score": 5, "macro_score": 5}  # divergence == 3.0 exactly
        self.assertTrue(roc._divergence_would_fire(row))

    def test_returns_none_when_a_score_is_missing(self):
        row = {"tech_score": 8, "fund_score": 4, "sent_score": 5, "pos_score": 4}
        self.assertIsNone(roc._divergence_would_fire(row))

    def test_returns_none_when_a_score_is_unparseable(self):
        row = {"tech_score": "n/a", "fund_score": 4, "sent_score": 5,
               "pos_score": 4, "macro_score": 5}
        self.assertIsNone(roc._divergence_would_fire(row))


class TestRecordDivergenceEvaluation(unittest.TestCase):

    def test_records_evaluation_with_correct_shape(self):
        row = {"id": 7325, "pair": "CHF/JPY", "direction": "BUY",
               "status": "LOSS", "net_pips": -186.5,
               "tech_score": 8, "fund_score": 4, "sent_score": 5,
               "pos_score": 4, "macro_score": 5}
        fake_sm = MagicMock()
        with patch.dict("sys.modules", {}), \
             patch("src.shadow_mode.register_rule", fake_sm.register_rule), \
             patch("src.shadow_mode.record_evaluation", fake_sm.record_evaluation):
            roc._record_divergence_evaluation(row)

        fake_sm.register_rule.assert_called_once()
        self.assertEqual(fake_sm.register_rule.call_args[0][0],
                          "technical_carries_divergence")
        fake_sm.record_evaluation.assert_called_once()
        call = fake_sm.record_evaluation.call_args
        self.assertEqual(call[0][0], "technical_carries_divergence")
        self.assertEqual(call[1]["would_fire"], True)
        self.assertEqual(call[1]["outcome"], "LOSS")
        self.assertEqual(call[1]["net_pips"], -186.5)

    def test_skips_recording_when_scores_missing(self):
        row = {"id": 1, "pair": "EUR/USD", "status": "WIN", "net_pips": 100}
        fake_sm = MagicMock()
        with patch("src.shadow_mode.register_rule", fake_sm.register_rule), \
             patch("src.shadow_mode.record_evaluation", fake_sm.record_evaluation):
            roc._record_divergence_evaluation(row)
        fake_sm.record_evaluation.assert_not_called()

    def test_never_raises_if_shadow_mode_itself_errors(self):
        row = {"id": 1, "pair": "EUR/USD", "status": "WIN", "net_pips": 100,
               "tech_score": 8, "fund_score": 4, "sent_score": 5,
               "pos_score": 4, "macro_score": 5}
        with patch("src.shadow_mode.register_rule", side_effect=RuntimeError("boom")):
            try:
                roc._record_divergence_evaluation(row)
            except Exception as exc:
                self.fail(f"_record_divergence_evaluation raised {exc!r} -- "
                          f"must never break the real close path")

    def test_never_mutates_the_trade_row(self):
        row = {"id": 7325, "pair": "CHF/JPY", "direction": "BUY",
               "status": "LOSS", "net_pips": -186.5,
               "tech_score": 8, "fund_score": 4, "sent_score": 5,
               "pos_score": 4, "macro_score": 5}
        original = dict(row)
        with patch("src.shadow_mode.register_rule"), \
             patch("src.shadow_mode.record_evaluation"):
            roc._record_divergence_evaluation(row)
        self.assertEqual(row, original)


if __name__ == "__main__":
    unittest.main()
