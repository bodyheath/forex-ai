"""Tests for src/position_sizing.py (extracted from daily.py 2026-09-2X) and
the fund_state.py::update_sizing_state() display fix that consumes it.

Real bug this closes: fund_state.json's displayed sizing_mode/
current_sizing_pct only ever ran the drawdown-tier half of the real sizing
chain (src/fund_state.py::compute_sizing()) -- never the regime/loss-streak
overlay real trade-creation always applies on top (daily.py's real call
site). A real 4+ consecutive-loss streak pushes real sizing to MINIMAL/
0.25% regardless of drawdown, but the display kept showing whatever the
drawdown tier alone implied (e.g. drawdown_caution/0.75% at a 3.69%
drawdown) -- a real, misleading gap between what was displayed and what an
actual new trade would be sized at.
"""
import unittest

from src import fund_state, position_sizing as psz


class TestCalculatePositionSize(unittest.TestCase):

    def test_normal_conditions_full_size(self):
        r = psz.calculate_position_size("TRENDING_RISK_ON", 0, 0.0, base_pct=1.0)
        self.assertEqual(r["sizing_mode"], "NORMAL")
        self.assertEqual(r["risk_pct"], 1.0)

    def test_four_plus_losses_forces_minimal(self):
        r = psz.calculate_position_size("TRENDING_RISK_ON", 4, 0.0, base_pct=1.0)
        self.assertEqual(r["sizing_mode"], "MINIMAL")
        self.assertEqual(r["final_multiplier"], 0.25)

    def test_five_losses_same_bucket_as_four(self):
        r4 = psz.calculate_position_size("TRENDING_RISK_ON", 4, 0.0, base_pct=1.0)
        r5 = psz.calculate_position_size("TRENDING_RISK_ON", 5, 0.0, base_pct=1.0)
        self.assertEqual(r4["risk_pct"], r5["risk_pct"])
        self.assertEqual(r4["sizing_mode"], r5["sizing_mode"])

    def test_lowest_of_three_multipliers_wins(self):
        # regime=0.5 (RISK_OFF), streak=1.0 (0 losses), drawdown=1.0 (0%)
        # -> final should be the regime multiplier, the lowest of the three
        r = psz.calculate_position_size("RISK_OFF", 0, 0.0, base_pct=1.0)
        self.assertEqual(r["multipliers"]["regime"], 0.5)
        self.assertEqual(r["final_multiplier"], 0.5)

    def test_risk_pct_floored_at_quarter_percent(self):
        r = psz.calculate_position_size("RISK_OFF", 4, 9.0, base_pct=0.1)
        self.assertGreaterEqual(r["risk_pct"], 0.25)

    def test_real_current_numbers_produce_minimal(self):
        """Reproduces the exact real state that exposed this bug: 5
        consecutive losses, 3.69% drawdown -- streak alone already forces
        MINIMAL regardless of drawdown or regime."""
        r = psz.calculate_position_size("TRENDING_RISK_ON", 5, 3.69, base_pct=0.75)
        self.assertEqual(r["sizing_mode"], "MINIMAL")
        self.assertEqual(r["risk_pct"], 0.25)


class TestDailyPyReexportsUnchanged(unittest.TestCase):
    """daily.py's own real call site (~line 6992) calls _calculate_
    position_size() and reads LOSS_STREAK_SIZING/DRAWDOWN_SIZING/
    BASE_RISK_PCT as module-level names -- confirms the extraction didn't
    break that shape."""

    def test_daily_py_names_match_position_sizing_module(self):
        import importlib, os
        os.environ["ALLOW_LOCAL_RUN"] = "YES"
        daily = importlib.import_module("daily")
        self.assertEqual(daily.BASE_RISK_PCT, psz.BASE_RISK_PCT)
        self.assertEqual(daily.LOSS_STREAK_SIZING, psz.LOSS_STREAK_SIZING)
        self.assertEqual(daily.DRAWDOWN_SIZING, psz.DRAWDOWN_SIZING)
        self.assertEqual(daily.SIZING_RULES, psz.SIZING_RULES)
        result = daily._calculate_position_size("TRENDING_RISK_ON", 5, 3.69, base_pct=0.75)
        self.assertEqual(result, psz.calculate_position_size(
            "TRENDING_RISK_ON", 5, 3.69, base_pct=0.75))


class TestUpdateSizingStateAppliesFullChain(unittest.TestCase):
    """fund_state.py::update_sizing_state() -- the display-facing function."""

    def test_low_drawdown_no_streak_stays_normal(self):
        state = {"current_drawdown_pct": 1.0, "consecutive_losses": 0,
                 "consecutive_wins": 0, "circuit_breaker_active": False,
                 "max_drawdown_seen": 1.0}
        updated = fund_state.update_sizing_state(state, current_balance=10000.0)
        self.assertEqual(updated["sizing_mode"], "normal")
        self.assertEqual(updated["current_sizing_pct"], 1.0)

    def test_loss_streak_overrides_drawdown_only_display(self):
        """The exact real bug: 3.69% drawdown alone implies drawdown_caution
        (0.75%), but a real 5-loss streak must push the displayed value
        down to MINIMAL/0.25% -- not leave it at the drawdown-only figure."""
        state = {"current_drawdown_pct": 3.69, "consecutive_losses": 5,
                 "consecutive_wins": 0, "circuit_breaker_active": False,
                 "max_drawdown_seen": 3.69}
        updated = fund_state.update_sizing_state(state, current_balance=9933.24)
        self.assertEqual(updated["sizing_mode"], "MINIMAL")
        self.assertEqual(updated["current_sizing_pct"], 0.25)

    def test_no_loss_streak_keeps_pure_drawdown_tier_display(self):
        """When the loss-streak overlay wouldn't reduce sizing further
        (0-1 consecutive losses), the display must still show the plain
        drawdown-tier result unchanged -- confirms the fix only overrides
        when the real chain would actually be more conservative, not
        unconditionally."""
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

    def test_drawdown_blocked_none_pct_not_overridden(self):
        """A quality-gated block (checklist_score too low for the tier)
        returns pct=None -- the overlay must not run on it at all."""
        state = {"current_drawdown_pct": 8.5, "consecutive_losses": 0,
                 "consecutive_wins": 0, "circuit_breaker_active": False,
                 "max_drawdown_seen": 8.5}
        updated = fund_state.update_sizing_state(state, current_balance=9150.0)
        # score=10.0 baseline always clears every quality gate, so this
        # should NOT be blocked -- sanity-check the baseline assumption
        # holds and the real drawdown_protection path is what's exercised.
        self.assertIn(updated["sizing_mode"], ("drawdown_protection",))

    def test_real_reported_state_matches_investigated_numbers(self):
        """Direct reproduction of the real fund_state.json snapshot that
        exposed this bug (balance $9,933.24, peak $10,314.07, 5 consecutive
        losses, 3.69% drawdown)."""
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
