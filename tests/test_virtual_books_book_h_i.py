"""Tests for Books H and I -- the two new virtual books proposed after
round 4 of the mechanical edge-mining research loop (2026-09-26) put
phenomena 2 (counter-trend vs 200MA/MACD) and 3 (oscillator extremity)
through the exact same discovery/holdout/cluster-bootstrap gauntlet Book
G's ribbon signal already went through.

Book H (osc_agrees alone, no ribbon check): Book G's own population is a
strict subset of this broader condition, and the broader one independently
survives all three gates -- this book runs head-to-head against Book G on
the same real candidates.

Book I (bollinger_extreme_agrees): a genuinely different indicator family
(volatility-band position, not momentum oscillators), with materially
lower population overlap against every other validated signal so far.

Both are registered with regime_aware_promotion=True from day one (per
Phase 32's shadow_mode cluster-bootstrap fix, applied consistently from
the start this time, not retrofitted the way Book G's was).
"""
import unittest

from src import virtual_books as vb


def _deep_result(pair="EUR/USD", direction="BUY", ribbon_status="NEUTRAL",
                  osc_direction="NONE", bb_position=0.5,
                  entry=1.1000, stop=1.0950, target=1.1100):
    return {
        "pair": pair,
        "parsed": {"direction": direction, "entry": entry, "stop_loss": stop, "target": target},
        "bundle": {
            "mtf": {"agreeing_count": 2},
            "technical": {"daily": {
                "ribbon": {"status": ribbon_status},
                "oscillator_confluence": {"direction": osc_direction},
                "bb_position": bb_position,
            }},
        },
    }


class TestBooksRegisteredCorrectly(unittest.TestCase):

    def test_book_h_in_registry(self):
        self.assertIn("H_oscillator_extremity", vb.BOOKS)

    def test_book_i_in_registry(self):
        self.assertIn("I_bollinger_extremity", vb.BOOKS)

    def test_book_h_is_regime_aware(self):
        self.assertTrue(vb.BOOKS["H_oscillator_extremity"].regime_aware_promotion)

    def test_book_i_is_regime_aware(self):
        self.assertTrue(vb.BOOKS["I_bollinger_extremity"].regime_aware_promotion)


class TestBookHEligibility(unittest.TestCase):

    def _dummy(self, *a, **kw):
        return True

    def test_fires_on_osc_agrees_alone_no_ribbon_required(self):
        """The defining difference from Book G: this must fire even when
        the ribbon is ALIGNED WITH the trade (Book G would reject this)."""
        r = _deep_result(direction="BUY", ribbon_status="ALIGNED_BULL", osc_direction="BUY")
        self.assertTrue(vb._elig_h_oscillator_extremity(r, {}, "normal", 7, self._dummy, self._dummy))
        self.assertFalse(vb._elig_g_mechanical_reversion(r, {}, "normal", 7, self._dummy, self._dummy))

    def test_does_not_fire_when_osc_disagrees(self):
        r = _deep_result(direction="BUY", ribbon_status="NEUTRAL", osc_direction="SELL")
        self.assertFalse(vb._elig_h_oscillator_extremity(r, {}, "normal", 7, self._dummy, self._dummy))

    def test_does_not_fire_when_osc_none(self):
        r = _deep_result(direction="BUY", ribbon_status="NEUTRAL", osc_direction="NONE")
        self.assertFalse(vb._elig_h_oscillator_extremity(r, {}, "normal", 7, self._dummy, self._dummy))

    def test_fires_regardless_of_dd_mode(self):
        r = _deep_result(direction="SELL", ribbon_status="NEUTRAL", osc_direction="SELL")
        for dd_mode in ("normal", "caution", "defensive", "preservation", "halt"):
            self.assertTrue(
                vb._elig_h_oscillator_extremity(r, {}, dd_mode, 7, self._dummy, self._dummy),
                f"must fire regardless of dd_mode={dd_mode}")

    def test_sell_direction_fires_correctly(self):
        r = _deep_result(direction="SELL", ribbon_status="NEUTRAL", osc_direction="SELL")
        self.assertTrue(vb._elig_h_oscillator_extremity(r, {}, "normal", 7, self._dummy, self._dummy))

    def test_missing_bundle_fields_do_not_crash(self):
        r = {"pair": "EUR/USD", "parsed": {"direction": "BUY"}, "bundle": {}}
        try:
            result = vb._elig_h_oscillator_extremity(r, {}, "normal", 7, self._dummy, self._dummy)
        except Exception as exc:
            self.fail(f"eligibility raised on missing bundle fields: {exc!r}")
        self.assertFalse(result)


class TestBookIEligibility(unittest.TestCase):

    def _dummy(self, *a, **kw):
        return True

    def test_fires_on_buy_at_lower_band(self):
        r = _deep_result(direction="BUY", bb_position=0.1)
        self.assertTrue(vb._elig_i_bollinger_extremity(r, {}, "normal", 7, self._dummy, self._dummy))

    def test_does_not_fire_mid_band(self):
        r = _deep_result(direction="BUY", bb_position=0.5)
        self.assertFalse(vb._elig_i_bollinger_extremity(r, {}, "normal", 7, self._dummy, self._dummy))

    def test_fires_on_sell_at_upper_band(self):
        r = _deep_result(direction="SELL", bb_position=0.9)
        self.assertTrue(vb._elig_i_bollinger_extremity(r, {}, "normal", 7, self._dummy, self._dummy))

    def test_no_ribbon_or_oscillator_requirement(self):
        """Book I is independent of ribbon/oscillator state entirely --
        only bb_position and direction matter."""
        r = _deep_result(direction="BUY", ribbon_status="ALIGNED_BULL", osc_direction="SELL", bb_position=0.15)
        self.assertTrue(vb._elig_i_bollinger_extremity(r, {}, "normal", 7, self._dummy, self._dummy))

    def test_fires_regardless_of_dd_mode(self):
        r = _deep_result(direction="SELL", bb_position=0.85)
        for dd_mode in ("normal", "caution", "defensive", "preservation", "halt"):
            self.assertTrue(
                vb._elig_i_bollinger_extremity(r, {}, dd_mode, 7, self._dummy, self._dummy),
                f"must fire regardless of dd_mode={dd_mode}")

    def test_missing_bundle_fields_do_not_crash(self):
        r = {"pair": "EUR/USD", "parsed": {"direction": "BUY"}, "bundle": {}}
        try:
            result = vb._elig_i_bollinger_extremity(r, {}, "normal", 7, self._dummy, self._dummy)
        except Exception as exc:
            self.fail(f"eligibility raised on missing bundle fields: {exc!r}")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
