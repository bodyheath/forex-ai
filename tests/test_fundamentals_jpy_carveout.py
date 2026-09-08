"""Confirms the 2026-09-08 JPY carve-out in
src/fundamentals.py::get_fundamental_alignment().

A real walk-forward backtest (scripts/fundamentals_tailwind_headwind_backtest.py)
found the live TAILWIND/HEADWIND conf adjustment mechanism actively backwards
for every one of the 7 JPY-cross pairs (5 significantly, the other 2
consistent but short of significance -- zero exceptions either way), while
the remaining 21 non-JPY pairs showed a genuine, strengthening-with-lag
edge. JPY pairs now have the conf adjustment forced to MIXED/0 regardless
of how the three underlying factors score; non-JPY pairs are unaffected.
"""
import unittest
from unittest.mock import patch

from src import fundamentals as fund


class TestJpyCarveout(unittest.TestCase):

    def test_non_jpy_tailwind_unaffected(self):
        with patch.dict(fund._CB_STANCE, {
                    "AUD": {"bias": "bullish", "note": ""},
                    "CAD": {"bias": "bearish", "note": ""},
                }), \
             patch.dict(fund._ECON_SURPRISE, {"AUD": "bullish", "CAD": "bearish"}), \
             patch.dict(fund._RATES, {"AUD": 5.0, "CAD": 1.0}):
            result = fund.get_fundamental_alignment("AUD", "CAD", "BUY")
        self.assertEqual(result["alignment"], "TAILWIND")
        self.assertEqual(result["conf_adj"], 1)
        self.assertEqual(result["aligned"], 3)

    def test_non_jpy_headwind_unaffected(self):
        with patch.dict(fund._CB_STANCE, {
                    "AUD": {"bias": "bearish", "note": ""},
                    "CAD": {"bias": "bullish", "note": ""},
                }), \
             patch.dict(fund._ECON_SURPRISE, {"AUD": "bearish", "CAD": "bullish"}), \
             patch.dict(fund._RATES, {"AUD": 1.0, "CAD": 5.0}):
            result = fund.get_fundamental_alignment("AUD", "CAD", "BUY")
        self.assertEqual(result["alignment"], "HEADWIND")
        self.assertEqual(result["conf_adj"], -1)
        self.assertEqual(result["opposed"], 3)

    def test_jpy_as_quote_forced_mixed_despite_full_alignment(self):
        # Same fully-aligned setup that would be TAILWIND for a non-JPY pair.
        with patch.dict(fund._CB_STANCE, {
                    "USD": {"bias": "bullish", "note": ""},
                    "JPY": {"bias": "bearish", "note": ""},
                }), \
             patch.dict(fund._ECON_SURPRISE, {"USD": "bullish", "JPY": "bearish"}), \
             patch.dict(fund._RATES, {"USD": 5.0, "JPY": 0.1}):
            result = fund.get_fundamental_alignment("USD", "JPY", "BUY")
        self.assertEqual(result["alignment"], "MIXED")
        self.assertEqual(result["conf_adj"], 0)
        # Underlying factor scores are still real and visible, only the
        # adjustment itself is neutralized.
        self.assertEqual(result["aligned"], 3)

    def test_jpy_as_base_also_excluded(self):
        with patch.dict(fund._CB_STANCE, {
                    "JPY": {"bias": "bearish", "note": ""},
                    "USD": {"bias": "bullish", "note": ""},
                }), \
             patch.dict(fund._ECON_SURPRISE, {"JPY": "bearish", "USD": "bullish"}), \
             patch.dict(fund._RATES, {"JPY": 0.1, "USD": 5.0}):
            # SELL JPY/USD: JPY bearish vs USD bullish factors all favor SELL.
            result = fund.get_fundamental_alignment("JPY", "USD", "SELL")
        self.assertEqual(result["alignment"], "MIXED")
        self.assertEqual(result["conf_adj"], 0)
        self.assertEqual(result["aligned"], 3)


if __name__ == "__main__":
    unittest.main()
