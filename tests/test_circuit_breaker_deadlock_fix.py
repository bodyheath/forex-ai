"""Regression tests for the 2026-09-09 circuit-breaker deadlock fix.

Real incident: with consecutive_losses=3, pause_until correctly computed
(2026-09-10T23:11:17), but ZERO open fund positions, the real fund could
never open another trade -- not "wait 48 hours", but a genuine permanent
deadlock. Root cause: daily.py had TWO separate, redundant "circuit
breaker" implementations. The one that actually gated real trades was an
ad-hoc raw `consecutive_losses >= 3` check with no time-based expiry at
all, positioned first in the candidate loop, unconditionally blocking and
`continue`-ing past the properly-designed, pause_until-aware
is_trading_blocked() check further down the same function -- which was
therefore never reached for this exact scenario. Since nothing but a WIN
resets consecutive_losses, and no trade could ever open to produce one,
the block could never clear, even once its own pause_until timestamp
passed.

Fix: the ad-hoc check is removed; is_trading_blocked() (with a fresh
per-candidate disk read and fail-closed error handling, reproducing the
ad-hoc check's own guarantees) is now the sole gate. A second, separate
gap found during verification -- is_trading_blocked() gates on
pause_until, which is only ever set by update_after_close(), which is
never invoked for an EXPIRED close -- is closed by having
reconcile_from_trades() self-heal a missing pause_until whenever the
freshly recomputed consecutive_losses already meets the threshold.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src import fund_state as fs


def _auckland_str(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


class TestRealDeadlockScenario(unittest.TestCase):
    """Reproduces today's exact real numbers."""

    def _real_state(self, pause_offset_hours: float) -> dict:
        pause_ts = fs._auckland_now() + timedelta(hours=pause_offset_hours)
        return {
            "consecutive_losses": 3,
            "consecutive_wins": 0,
            "pause_until": _auckland_str(pause_ts),
            "circuit_breaker_active": False,
            "circuit_breaker_reason": None,
            "daily_trades_count": 0,
            "observation_mode": False,
            "observation_mode_until": None,
        }

    def test_is_trading_blocked_matches_real_current_state(self):
        # Today's actual real fund_state.json: pause_until ~1.5 days out.
        state = self._real_state(pause_offset_hours=36)
        blocked, reason, btype = fs.is_trading_blocked(state)
        self.assertTrue(blocked)
        self.assertEqual(btype, "pause")

    def test_old_ad_hoc_pattern_would_never_clear_even_after_pause_expires(self):
        """Demonstrates the actual bug: a raw consecutive_losses>=3 check,
        with no expiry awareness, blocks forever regardless of how much
        time passes -- exactly what daily.py used to do."""
        state = self._real_state(pause_offset_hours=-1)  # pause_until already in the past
        old_ad_hoc_would_block = int(state.get("consecutive_losses", 0) or 0) >= 3
        self.assertTrue(
            old_ad_hoc_would_block,
            "the old ad-hoc check has no time awareness -- it blocks forever",
        )

    def test_is_trading_blocked_correctly_unblocks_after_pause_expires(self):
        """The fix's whole point: once pause_until has actually passed,
        the real gate now consulted (is_trading_blocked()) lets trading
        resume on its own -- no manual intervention, no code change."""
        state = self._real_state(pause_offset_hours=-1)  # pause_until in the past
        blocked, reason, btype = fs.is_trading_blocked(state)
        self.assertFalse(
            blocked,
            "is_trading_blocked() must NOT block once pause_until has passed -- "
            "this is the exact recovery the ad-hoc check could never provide",
        )

    def test_zero_open_positions_does_not_prevent_eventual_recovery(self):
        """The deadlock's defining condition (0 open positions, so no trade
        could ever win to reset the counter) must be irrelevant to whether
        trading resumes -- recovery is purely time-based now."""
        state = self._real_state(pause_offset_hours=-0.01)
        # open_count / v2_open_count deliberately absent/zero -- is_trading_blocked()
        # must not reference them at all.
        blocked, _, _ = fs.is_trading_blocked(state)
        self.assertFalse(blocked)


class TestReconcileSelfHealsMissingPauseUntil(unittest.TestCase):
    """Closes the second gap found during verification: a loss streak built
    via EXPIRED closes (which never call update_after_close()) could reach
    the threshold without pause_until ever being set."""

    def _trades_df(self, net_pips_sequence):
        rows = []
        base_ts = datetime(2026, 9, 1, tzinfo=timezone.utc)
        for i, pips in enumerate(net_pips_sequence):
            rows.append({
                "id": i + 1, "pair": "EUR/USD", "direction": "BUY",
                "trade_this": "YES", "system_version": "v2",
                "status": "EXPIRED", "entry": 1.1000, "exit_price": 1.1000 + pips * 0.0001,
                "stop_loss": 1.0950, "target": 1.1200,
                "pips": pips, "net_pips": pips,
                "timestamp": (base_ts + timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S"),
                "closed_at": (base_ts + timedelta(days=i, hours=6)).strftime("%Y-%m-%d %H:%M:%S"),
                "position_size_pct_at_entry": 1.0,
            })
        return pd.DataFrame(rows)

    def test_expired_only_streak_gets_pause_until_set(self):
        df = self._trades_df([-10.0, -15.0, -20.0])  # 3 consecutive EXPIRED losses, no literal LOSS status
        state = {"consecutive_losses": 0, "pause_until": None}
        with patch("src.trading.financials.calculate_fund_state") as mock_calc:
            mock_calc.return_value = {
                "consecutive_losses": 3, "consecutive_wins": 0, "balance": 9700.0,
            }
            new_state = fs.reconcile_from_trades(state, df=df, prices={})
        self.assertEqual(new_state["consecutive_losses"], 3)
        self.assertIsNotNone(
            new_state.get("pause_until"),
            "pause_until must be self-healed when consecutive_losses already "
            "meets the threshold but nothing set it (e.g. an EXPIRED-only streak)",
        )
        blocked, _, btype = fs.is_trading_blocked(new_state)
        self.assertTrue(blocked)
        self.assertEqual(btype, "pause")

    def test_does_not_overwrite_an_existing_pause_until(self):
        existing_pause = "2099-01-01T00:00:00"
        state = {"consecutive_losses": 0, "pause_until": existing_pause}
        with patch("src.trading.financials.calculate_fund_state") as mock_calc:
            mock_calc.return_value = {"consecutive_losses": 3, "consecutive_wins": 0}
            new_state = fs.reconcile_from_trades(state, df=pd.DataFrame(), prices={})
        self.assertEqual(new_state["pause_until"], existing_pause)

    def test_no_healing_when_below_threshold(self):
        state = {"consecutive_losses": 0, "pause_until": None}
        with patch("src.trading.financials.calculate_fund_state") as mock_calc:
            mock_calc.return_value = {"consecutive_losses": 2, "consecutive_wins": 0}
            new_state = fs.reconcile_from_trades(state, df=pd.DataFrame(), prices={})
        self.assertIsNone(new_state.get("pause_until"))


class TestDailyPyAdHocCheckRemoved(unittest.TestCase):
    """Structural check: the old unconditional raw-counter block no longer
    exists in daily.py's source, and the consolidated replacement does."""

    def setUp(self):
        self._source = Path("daily.py").read_text(encoding="utf-8")

    def test_old_ad_hoc_block_reason_text_gone(self):
        self.assertNotIn(
            'f"Circuit breaker active: {_cb_losses} consecutive losses"',
            self._source,
        )

    def test_old_unconditional_threshold_check_gone(self):
        self.assertNotIn("if _cb_losses >= 3:", self._source)

    def test_consolidated_gate_present(self):
        self.assertIn("_blk, _blk_rsn, _blk_tp = _fs.is_trading_blocked(_fund_st)", self._source)
        self.assertIn("_cb_gate_fields", self._source)

    def test_only_one_is_trading_blocked_call_site_remains(self):
        self.assertEqual(
            self._source.count("_fs.is_trading_blocked(_fund_st)"), 1,
            "the old, redundant second call site should have been removed",
        )


class TestResetCircuitBreakerScript(unittest.TestCase):
    """scripts/reset_circuit_breaker.py: safe by default, explicit to act."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._file = Path(self._tmpdir.name) / "fund_state.json"
        self._patcher = patch.object(fs, "_FILE", self._file)
        self._patcher.start()
        fs.save({
            "consecutive_losses": 3, "consecutive_wins": 0,
            "pause_until": "2026-09-10T23:11:17",
        })

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_dry_run_does_not_modify_state(self):
        from scripts import reset_circuit_breaker as reset_script
        with patch("sys.argv", ["reset_circuit_breaker.py"]):
            reset_script.main()
        state = fs.load()
        self.assertEqual(state["consecutive_losses"], 3)
        self.assertEqual(state["pause_until"], "2026-09-10T23:11:17")

    def test_confirm_without_reason_refuses(self):
        from scripts import reset_circuit_breaker as reset_script
        with patch("sys.argv", ["reset_circuit_breaker.py", "--confirm"]):
            rc = reset_script.main()
        self.assertNotEqual(rc, 0)
        state = fs.load()
        self.assertEqual(state["consecutive_losses"], 3)  # unchanged

    def test_confirm_with_reason_resets(self):
        from scripts import reset_circuit_breaker as reset_script
        with patch("sys.argv", ["reset_circuit_breaker.py", "--confirm", "--reason", "test reset"]):
            rc = reset_script.main()
        self.assertEqual(rc, 0)
        state = fs.load()
        self.assertEqual(state["consecutive_losses"], 0)
        self.assertIsNone(state["pause_until"])
        self.assertEqual(len(state.get("manual_overrides", [])), 1)


if __name__ == "__main__":
    unittest.main()
