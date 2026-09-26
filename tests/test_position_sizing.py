"""Tests for src/position_sizing.py and the fund_state.py::
update_sizing_state() display function that consumes it.

2026-09-2X CONSOLIDATED: position_sizing.py used to carry its own
DRAWDOWN_SIZING table, chained on top of fund_state.py's own, separate
drawdown-tier system -- the SAME real current_drawdown_pct value
re-penalized twice by two different, uncoordinated tables. Git-history
evidence pointed to organic duplication (built 3 days apart, no comment
anywhere acknowledging the other mechanism), not a deliberate double safety
margin. Consolidated: fund_state.py's compute_sizing() (the more complete
of the two -- it also gates on real-time checklist_score for its deeper
tiers, and has recovery/win-streak-boost logic this module never had) is
now the sole drawdown-based sizing authority; position_sizing.py keeps only
the regime and loss-streak dimensions.
"""
import unittest

from src import fund_state, position_sizing as psz


class TestCalculatePositionSize(unittest.TestCase):

    def test_normal_conditions_full_size(self):
        r = psz.calculate_position_size("TRENDING_RISK_ON", 0, base_pct=1.0)
        self.assertEqual(r["sizing_mode"], "NORMAL")
        self.assertEqual(r["risk_pct"], 1.0)

    def test_four_plus_losses_forces_minimal(self):
        r = psz.calculate_position_size("TRENDING_RISK_ON", 4, base_pct=1.0)
        self.assertEqual(r["sizing_mode"], "MINIMAL")
        self.assertEqual(r["final_multiplier"], 0.25)

    def test_five_losses_same_bucket_as_four(self):
        r4 = psz.calculate_position_size("TRENDING_RISK_ON", 4, base_pct=1.0)
        r5 = psz.calculate_position_size("TRENDING_RISK_ON", 5, base_pct=1.0)
        self.assertEqual(r4["risk_pct"], r5["risk_pct"])
        self.assertEqual(r4["sizing_mode"], r5["sizing_mode"])

    def test_lowest_of_two_multipliers_wins(self):
        # regime=0.5 (RISK_OFF), streak=1.0 (0 losses) -> final = regime
        r = psz.calculate_position_size("RISK_OFF", 0, base_pct=1.0)
        self.assertEqual(r["multipliers"]["regime"], 0.5)
        self.assertEqual(r["final_multiplier"], 0.5)

    def test_risk_pct_floored_at_quarter_percent(self):
        r = psz.calculate_position_size("RISK_OFF", 4, base_pct=0.1)
        self.assertGreaterEqual(r["risk_pct"], 0.25)

    def test_no_drawdown_dimension_left(self):
        """CONSOLIDATED: there is no drawdown_pct parameter or DRAWDOWN_
        SIZING table left in this module -- calling with the OLD positional
        shape must fail loudly (TypeError), not silently accept and ignore
        an extra argument."""
        with self.assertRaises(TypeError):
            psz.calculate_position_size("TRENDING_RISK_ON", 5, 3.69, base_pct=0.75)
        self.assertFalse(hasattr(psz, "DRAWDOWN_SIZING"))

    def test_real_current_numbers_produce_minimal(self):
        """Reproduces the exact real state that exposed the original display
        bug: 5 consecutive losses -- streak alone already forces MINIMAL
        regardless of regime."""
        r = psz.calculate_position_size("TRENDING_RISK_ON", 5, base_pct=0.75)
        self.assertEqual(r["sizing_mode"], "MINIMAL")
        self.assertEqual(r["risk_pct"], 0.25)


class TestDailyPyReexportsUnchanged(unittest.TestCase):
    """daily.py's own real call site calls _calculate_position_size() and
    reads LOSS_STREAK_SIZING/BASE_RISK_PCT as module-level names -- confirms
    the extraction (and the later drawdown-dimension removal) didn't break
    that shape. DRAWDOWN_SIZING is deliberately no longer re-exported."""

    def test_daily_py_names_match_position_sizing_module(self):
        import importlib, os
        os.environ["ALLOW_LOCAL_RUN"] = "YES"
        daily = importlib.import_module("daily")
        self.assertEqual(daily.BASE_RISK_PCT, psz.BASE_RISK_PCT)
        self.assertEqual(daily.LOSS_STREAK_SIZING, psz.LOSS_STREAK_SIZING)
        self.assertEqual(daily.SIZING_RULES, psz.SIZING_RULES)
        self.assertFalse(hasattr(daily, "DRAWDOWN_SIZING"))
        result = daily._calculate_position_size("TRENDING_RISK_ON", 5, base_pct=0.75)
        self.assertEqual(result, psz.calculate_position_size(
            "TRENDING_RISK_ON", 5, base_pct=0.75))


class TestUpdateSizingStateAppliesFullChain(unittest.TestCase):
    """fund_state.py::update_sizing_state() -- the display-facing function."""

    def test_low_drawdown_no_streak_stays_normal(self):
        state = {"current_drawdown_pct": 1.0, "consecutive_losses": 0,
                 "consecutive_wins": 0, "circuit_breaker_active": False,
                 "max_drawdown_seen": 1.0}
        updated = fund_state.update_sizing_state(state, current_balance=10000.0)
        self.assertEqual(updated["sizing_mode"], "normal")
        self.assertEqual(updated["current_sizing_pct"], 1.0)

    def test_loss_streak_overrides_drawdown_tier_display(self):
        """3.69% drawdown alone implies drawdown_caution (0.75%), but a real
        5-loss streak must still push the displayed value down to MINIMAL/
        0.25% -- the loss-streak overlay is real and independent of
        drawdown, unaffected by the consolidation."""
        state = {"current_drawdown_pct": 3.69, "consecutive_losses": 5,
                 "consecutive_wins": 0, "circuit_breaker_active": False,
                 "max_drawdown_seen": 3.69}
        updated = fund_state.update_sizing_state(state, current_balance=9933.24)
        self.assertEqual(updated["sizing_mode"], "MINIMAL")
        self.assertEqual(updated["current_sizing_pct"], 0.25)

    def test_zero_streak_no_longer_gets_a_second_drawdown_penalty(self):
        """CONSOLIDATED behavior, replacing the old double-penalty test:
        at 3.69% drawdown with 0 consecutive losses, stage 1
        (compute_sizing) gives drawdown_caution/0.75% -- the overlay must
        NOT re-penalize on drawdown a second time now that its own
        DRAWDOWN_SIZING table is gone. Display must show exactly 0.75%,
        not 0.56% (the old double-penalized figure)."""
        state = {"current_drawdown_pct": 3.69, "consecutive_losses": 0,
                 "consecutive_wins": 0, "circuit_breaker_active": False,
                 "max_drawdown_seen": 3.69}
        updated = fund_state.update_sizing_state(state, current_balance=9933.24)
        self.assertEqual(updated["sizing_mode"], "drawdown_caution")
        self.assertEqual(updated["current_sizing_pct"], 0.75)

    def test_drawdown_pause_zero_pct_not_overridden(self):
        """paused/>=10% drawdown returns pct=0.0 from the first stage --
        the overlay must never turn a real pause into a nonzero size."""
        state = {"current_drawdown_pct": 11.0, "consecutive_losses": 0,
                 "consecutive_wins": 0, "circuit_breaker_active": False,
                 "max_drawdown_seen": 11.0, "drawdown_paused": True}
        updated = fund_state.update_sizing_state(state, current_balance=8900.0)
        self.assertEqual(updated["current_sizing_pct"], 0.0)
        self.assertEqual(updated["sizing_mode"], "drawdown_pause")

    def test_real_reported_state_matches_investigated_numbers(self):
        """Direct reproduction of the real fund_state.json snapshot that
        exposed the original display bug (balance $9,933.24, peak
        $10,314.07, 5 consecutive losses, 3.69% drawdown) -- the loss-
        streak override still applies post-consolidation."""
        state = {
            "current_drawdown_pct": 3.69, "consecutive_losses": 5,
            "consecutive_wins": 0, "circuit_breaker_active": False,
            "max_drawdown_seen": 3.69, "pause_until": "2026-09-19T18:01:49",
        }
        updated = fund_state.update_sizing_state(state, current_balance=9933.24)
        self.assertEqual(updated["sizing_mode"], "MINIMAL")
        self.assertEqual(updated["current_sizing_pct"], 0.25)
        self.assertIn("losses=5", updated["sizing_reason"])


if __name__ == "__main__":
    unittest.main()
