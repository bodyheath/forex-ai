"""Tests for the 2026-09-09 fix making _check_pending_trades()'s circuit-
breaker re-check use fund_state.is_trading_blocked() instead of a narrower
ad-hoc `consecutive_losses >= 3` read.

Real gap: activating a PENDING trade (a conditional order whose trigger just
hit) is entering a new real position, exactly like a fresh candidate -- but
the old check only looked at a raw consecutive-losses count read straight
from fund_state.json. It had no awareness of pause_until expiry,
daily_trades_count, or observation_mode, and didn't fail closed on a read
error. That's a smaller version of the same deadlock shape fixed in
fund_state.py/daily.py this same day (see test_circuit_breaker_deadlock_fix.py)
-- this test file confirms the sibling call site now goes through the same
real gate.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src import monitor


_TRADES_COLUMNS = [
    "id", "pair", "direction", "status", "trade_this", "entry_type",
    "entry_trigger_price", "entry_trigger_expiry", "entry", "stop_loss",
    "target", "t1_price", "t2_price", "t3_price", "reward_risk",
    "confidence", "capacity_override",
]


class TestPendingTradeCircuitBreakerConsistency(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_cwd = os.getcwd()
        os.chdir(self._tmpdir.name)
        Path("data").mkdir(exist_ok=True)
        self._write_pending_row()

    def tearDown(self):
        os.chdir(self._orig_cwd)
        self._tmpdir.cleanup()

    def _write_pending_row(self):
        row = {c: "" for c in _TRADES_COLUMNS}
        row.update({
            "id": 9001,
            "pair": "AUD/NZD",
            "direction": "BUY",
            "status": "PENDING",
            "trade_this": "YES",
            "entry_type": "IMMEDIATE",
            "entry_trigger_price": 1.2000,
            "entry": 1.2000,
            "stop_loss": 1.1900,
            "target": 1.2200,
            "reward_risk": 2.0,
            "confidence": 7,
        })
        pd.DataFrame([row], columns=_TRADES_COLUMNS).to_csv(
            "data/trades.csv", index=False
        )

    def _reload_row(self):
        df = pd.read_csv("data/trades.csv", encoding="utf-8-sig")
        return df.iloc[0].to_dict()

    def test_blocked_extends_expiry_instead_of_activating(self):
        """Checks the in-memory decision only (not the on-disk row): whether
        that decision actually persists is gated by `if activated or
        cancelled:` a few lines later in _check_pending_trades(), which the
        "extend expiry while blocked" branch never populates -- a separate,
        narrow, pre-existing gap unrelated to this fix (the extension is
        genuinely never saved; flagged separately, not fixed here)."""
        with patch("src.fund_state.load", return_value={}), \
             patch("src.fund_state.is_trading_blocked",
                   return_value=(True, "3 consecutive losses — paused until Thu", "pause")):
            activated = monitor._check_pending_trades({"AUD/NZD": 1.2000}, log_fn=lambda m: None)

        self.assertEqual(activated, [])
        row = self._reload_row()
        self.assertEqual(row["status"], "PENDING")

    def test_not_blocked_activates_normally(self):
        """Checks the in-memory decision only (not the on-disk row): whether
        an activation persists to trades.csv is controlled by a separate,
        pre-existing argument-order bug in the _awc_pend(df, path) call at
        the end of _check_pending_trades() (path/df swapped vs.
        atomic_write_csv's real signature) -- unrelated to this fix, flagged
        and fixed on its own dedicated branch."""
        with patch("src.fund_state.load", return_value={}), \
             patch("src.fund_state.is_trading_blocked", return_value=(False, "", "")):
            activated = monitor._check_pending_trades({"AUD/NZD": 1.2000}, log_fn=lambda m: None)

        self.assertEqual(len(activated), 1)
        self.assertEqual(activated[0]["trade_id"], 9001)
        self.assertEqual(activated[0]["pair"], "AUD/NZD")

    def test_gate_check_failure_fails_closed(self):
        """If is_trading_blocked() itself can't even be reached, this must
        block (extend expiry) rather than activate a real trade on a
        assumption of safety."""
        with patch("src.fund_state.load", side_effect=RuntimeError("disk error")):
            activated = monitor._check_pending_trades({"AUD/NZD": 1.2000}, log_fn=lambda m: None)

        self.assertEqual(activated, [])
        row = self._reload_row()
        self.assertEqual(row["status"], "PENDING")

    def test_pause_until_expiry_awareness_matches_main_gate(self):
        """The real point of this fix: unlike the old raw consecutive_losses
        check, this must respect a real is_trading_blocked() result that
        knows the pause already expired -- not just the loss count."""
        with patch("src.fund_state.load", return_value={"consecutive_losses": 3}), \
             patch("src.fund_state.is_trading_blocked", return_value=(False, "", "")):
            activated = monitor._check_pending_trades({"AUD/NZD": 1.2000}, log_fn=lambda m: None)

        self.assertEqual(len(activated), 1,
                          "3 consecutive_losses alone must not block once "
                          "is_trading_blocked() says the pause has expired")


if __name__ == "__main__":
    unittest.main()
