"""Tests for src/engine_arbitration.py's resolve() -- the pre-registered
rule for what happens when 2+ signal-generating engines disagree on
direction for the same pair (designed 2026-09-07, ahead of Phase 01B/02C's
specialists getting real directional authority; see that module's docstring
for the full rationale). No live call site exists yet since only one engine
has real authority today -- these tests exercise the rule directly against
synthetic multi-engine inputs, as a second engine would eventually supply.
"""
import unittest

from src import engine_arbitration as ea


class TestEngineArbitration(unittest.TestCase):

    def test_single_engine_proceeds(self):
        result = ea.resolve([{"engine": "main", "direction": "BUY", "confidence": 8}])
        self.assertEqual(result["action"], "PROCEED")
        self.assertEqual(result["direction"], "BUY")
        self.assertEqual(result["conflicting_engines"], [])

    def test_two_engines_agree_proceeds(self):
        result = ea.resolve([
            {"engine": "main", "direction": "BUY", "confidence": 8},
            {"engine": "positioning", "direction": "BUY", "confidence": 4},
        ])
        self.assertEqual(result["action"], "PROCEED")
        self.assertEqual(result["direction"], "BUY")

    def test_opposite_directions_hard_block_not_confidence_weighted(self):
        # main has much higher confidence -- rule must still block, not let
        # confidence pick a winner.
        result = ea.resolve([
            {"engine": "main", "direction": "BUY", "confidence": 9},
            {"engine": "carry_macro", "direction": "SELL", "confidence": 2},
        ])
        self.assertEqual(result["action"], "BLOCK")
        self.assertIsNone(result["direction"])
        self.assertIn("main", result["conflicting_engines"])
        self.assertIn("carry_macro", result["conflicting_engines"])

    def test_three_engines_two_agree_one_dissents_still_blocks(self):
        # Any opposite-direction disagreement blocks, even 2-vs-1 -- this is
        # not majority voting.
        result = ea.resolve([
            {"engine": "main", "direction": "BUY", "confidence": 8},
            {"engine": "positioning", "direction": "BUY", "confidence": 6},
            {"engine": "carry_macro", "direction": "SELL", "confidence": 7},
        ])
        self.assertEqual(result["action"], "BLOCK")

    def test_abstaining_engine_is_not_a_conflict(self):
        result = ea.resolve([
            {"engine": "main", "direction": "BUY", "confidence": 8},
            {"engine": "sentiment", "direction": "NO_TRADE", "confidence": 0},
        ])
        self.assertEqual(result["action"], "PROCEED")
        self.assertEqual(result["direction"], "BUY")

    def test_all_engines_abstain_no_signal(self):
        result = ea.resolve([
            {"engine": "main", "direction": "NO_TRADE", "confidence": 0},
            {"engine": "positioning", "direction": "NO_TRADE", "confidence": 0},
        ])
        self.assertEqual(result["action"], "NO_SIGNAL")
        self.assertIsNone(result["direction"])

    def test_empty_signals_no_signal(self):
        result = ea.resolve([])
        self.assertEqual(result["action"], "NO_SIGNAL")


if __name__ == "__main__":
    unittest.main()
