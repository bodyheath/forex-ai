"""Tests for src/mechanical_reversion.py -- the proposal-stage logic module
for the 2026-09-25 mechanical edge-mining finding (see
PROPOSAL_mechanical_reversion_engine.md). NOT live code: this module is not
imported by any real trading path, so these tests cover the pure logic
only, not any wiring (there is none to test).
"""
import unittest

from src import mechanical_reversion as mr


class TestRibbonAgainst(unittest.TestCase):

    def test_buy_against_aligned_bear(self):
        self.assertTrue(mr.ribbon_against("BUY", "ALIGNED_BEAR"))

    def test_buy_against_leaning_bear(self):
        self.assertTrue(mr.ribbon_against("BUY", "LEANING_BEAR"))

    def test_buy_not_against_aligned_bull(self):
        self.assertFalse(mr.ribbon_against("BUY", "ALIGNED_BULL"))

    def test_sell_against_aligned_bull(self):
        self.assertTrue(mr.ribbon_against("SELL", "ALIGNED_BULL"))

    def test_sell_against_leaning_bull(self):
        self.assertTrue(mr.ribbon_against("SELL", "LEANING_BULL"))

    def test_neutral_ribbon_never_against(self):
        self.assertFalse(mr.ribbon_against("BUY", "NEUTRAL"))
        self.assertFalse(mr.ribbon_against("SELL", "NEUTRAL"))

    def test_case_insensitive(self):
        self.assertTrue(mr.ribbon_against("buy", "aligned_bear"))

    def test_invalid_direction_returns_false(self):
        self.assertFalse(mr.ribbon_against("", "ALIGNED_BEAR"))
        self.assertFalse(mr.ribbon_against(None, "ALIGNED_BEAR"))


class TestOscillatorAgrees(unittest.TestCase):

    def test_buy_agrees_with_buy_oscillator(self):
        self.assertTrue(mr.oscillator_agrees("BUY", "BUY"))

    def test_sell_agrees_with_sell_oscillator(self):
        self.assertTrue(mr.oscillator_agrees("SELL", "SELL"))

    def test_buy_does_not_agree_with_sell_oscillator(self):
        self.assertFalse(mr.oscillator_agrees("BUY", "SELL"))

    def test_none_oscillator_never_agrees(self):
        self.assertFalse(mr.oscillator_agrees("BUY", "NONE"))
        self.assertFalse(mr.oscillator_agrees("SELL", "NONE"))

    def test_invalid_direction_returns_false(self):
        self.assertFalse(mr.oscillator_agrees("", "BUY"))


class TestBollingerExtremeAgrees(unittest.TestCase):

    def test_buy_agrees_at_lower_band(self):
        self.assertTrue(mr.bollinger_extreme_agrees("BUY", 0.1))

    def test_buy_agrees_at_threshold(self):
        self.assertTrue(mr.bollinger_extreme_agrees("BUY", 0.2))

    def test_buy_does_not_agree_mid_band(self):
        self.assertFalse(mr.bollinger_extreme_agrees("BUY", 0.5))

    def test_buy_agrees_when_price_pierces_lower_band(self):
        """bb_position can go negative when price pierces the band --
        that's MORE extreme, not out of range."""
        self.assertTrue(mr.bollinger_extreme_agrees("BUY", -0.3))

    def test_sell_agrees_at_upper_band(self):
        self.assertTrue(mr.bollinger_extreme_agrees("SELL", 0.9))

    def test_sell_agrees_at_threshold(self):
        self.assertTrue(mr.bollinger_extreme_agrees("SELL", 0.8))

    def test_sell_does_not_agree_mid_band(self):
        self.assertFalse(mr.bollinger_extreme_agrees("SELL", 0.5))

    def test_sell_agrees_when_price_pierces_upper_band(self):
        self.assertTrue(mr.bollinger_extreme_agrees("SELL", 1.4))

    def test_buy_does_not_agree_at_upper_band(self):
        self.assertFalse(mr.bollinger_extreme_agrees("BUY", 0.9))

    def test_invalid_direction_returns_false(self):
        self.assertFalse(mr.bollinger_extreme_agrees("", 0.1))
        self.assertFalse(mr.bollinger_extreme_agrees(None, 0.1))

    def test_missing_bb_position_returns_false(self):
        self.assertFalse(mr.bollinger_extreme_agrees("BUY", None))
        self.assertFalse(mr.bollinger_extreme_agrees("BUY", "n/a"))


class TestMechanicalReversionFires(unittest.TestCase):

    def test_fires_when_both_conditions_met(self):
        self.assertTrue(mr.mechanical_reversion_fires("BUY", "ALIGNED_BEAR", "BUY"))

    def test_does_not_fire_when_ribbon_not_against(self):
        self.assertFalse(mr.mechanical_reversion_fires("BUY", "ALIGNED_BULL", "BUY"))

    def test_does_not_fire_when_oscillator_disagrees(self):
        self.assertFalse(mr.mechanical_reversion_fires("BUY", "ALIGNED_BEAR", "NONE"))

    def test_does_not_fire_when_neither_condition_met(self):
        self.assertFalse(mr.mechanical_reversion_fires("BUY", "NEUTRAL", "NONE"))

    def test_sell_side_fires_correctly(self):
        self.assertTrue(mr.mechanical_reversion_fires("SELL", "ALIGNED_BULL", "SELL"))


class TestComputeMechanicalLevels(unittest.TestCase):

    def test_buy_levels_use_target_rr_2(self):
        levels = mr.compute_mechanical_levels(entry=1.1000, atr14=0.0050, direction="BUY", pip_size=0.0001)
        self.assertAlmostEqual(levels["stop_loss"], 1.0950, places=4)
        risk = levels["entry"] - levels["stop_loss"]
        reward = levels["target"] - levels["entry"]
        self.assertAlmostEqual(reward / risk, 2.0, places=2)
        self.assertEqual(levels["reward_risk"], 2.0)

    def test_sell_levels_mirror_buy(self):
        levels = mr.compute_mechanical_levels(entry=1.1000, atr14=0.0050, direction="SELL", pip_size=0.0001)
        self.assertGreater(levels["stop_loss"], levels["entry"])
        self.assertLess(levels["target"], levels["entry"])
        risk = levels["stop_loss"] - levels["entry"]
        reward = levels["entry"] - levels["target"]
        self.assertAlmostEqual(reward / risk, 2.0, places=2)

    def test_stop_floored_at_5_pips(self):
        levels = mr.compute_mechanical_levels(entry=1.1000, atr14=0.00005, direction="BUY", pip_size=0.0001)
        self.assertEqual(levels["stop_pips"], 5)

    def test_invalid_atr_returns_empty(self):
        self.assertEqual(mr.compute_mechanical_levels(1.1000, 0, "BUY", 0.0001), {})
        self.assertEqual(mr.compute_mechanical_levels(1.1000, -0.001, "BUY", 0.0001), {})

    def test_invalid_direction_returns_empty(self):
        self.assertEqual(mr.compute_mechanical_levels(1.1000, 0.005, "SIDEWAYS", 0.0001), {})

    def test_invalid_entry_returns_empty(self):
        self.assertEqual(mr.compute_mechanical_levels(0, 0.005, "BUY", 0.0001), {})

    def test_jpy_pip_size(self):
        levels = mr.compute_mechanical_levels(entry=150.00, atr14=0.30, direction="BUY", pip_size=0.01)
        self.assertGreater(levels["stop_pips"], 0)
        risk = levels["entry"] - levels["stop_loss"]
        reward = levels["target"] - levels["entry"]
        self.assertAlmostEqual(reward / risk, 2.0, places=2)


if __name__ == "__main__":
    unittest.main()
