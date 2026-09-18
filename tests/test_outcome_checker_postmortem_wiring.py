"""Tests for the 2026-09-19 wiring of src/trade_postmortem.py into every real
fund-trade close path in src/outcome_checker.py::check_open_trades()
(target-hit, stop-hit, and the expiry check) -- see
src/trade_postmortem.py's own module docstring for why this exists.

Pure observability: confirms the postmortem hook fires with the real,
already-updated trade row at each close point, and confirms a failure
inside the postmortem module can never abort the close itself (the same
"never raises" contract _online_learn() already has here).
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config
from src import outcome_checker
from src import telegram_alert
from src import tracker


def _open_trade_row(trade_id=50001, pair="EUR/USD", direction="BUY",
                     entry=1.1000, stop_loss=1.0950, target=1.1100,
                     timestamp=None):
    row = {f: "" for f in tracker.FIELDS}
    row.update({
        "id": trade_id,
        "timestamp": timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "pair": pair,
        "direction": direction,
        "confidence": 7,
        "entry": entry,
        "stop_loss": stop_loss,
        "target": target,
        "trade_this": "YES",
        "status": "OPEN",
        "system_version": "v2",
    })
    return row


class _BaseOutcomeCheckerTest(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._trades_csv = Path(self._tmpdir.name) / "trades.csv"
        self._patchers = [
            patch.object(config, "TRADES_CSV", self._trades_csv),
            patch.object(config, "TWELVE_DATA_KEY", "fake-key-for-test"),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmpdir.cleanup()


class TestTargetHitRecordsPostmortem(_BaseOutcomeCheckerTest):

    def test_postmortem_called_on_target_hit(self):
        row = _open_trade_row()
        tracker._write_all([row])
        with patch.object(outcome_checker, "_fetch_live_price", return_value=1.1105), \
             patch("src.trade_postmortem.record_trade_postmortem") as mock_pm:
            closed = outcome_checker.check_open_trades(log=lambda m: None)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["status"], "WIN")
        mock_pm.assert_called_once()
        self.assertEqual(mock_pm.call_args[0][0]["id"], closed[0]["id"])
        self.assertEqual(mock_pm.call_args[0][0]["status"], "WIN")


class TestStopHitRecordsPostmortem(_BaseOutcomeCheckerTest):

    def test_postmortem_called_on_stop_hit(self):
        row = _open_trade_row()
        tracker._write_all([row])
        with patch.object(outcome_checker, "_fetch_live_price", return_value=1.0940), \
             patch("src.trade_postmortem.record_trade_postmortem") as mock_pm:
            closed = outcome_checker.check_open_trades(log=lambda m: None)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["status"], "LOSS")
        mock_pm.assert_called_once()
        self.assertEqual(mock_pm.call_args[0][0]["status"], "LOSS")


class TestExpiryRecordsPostmortem(_BaseOutcomeCheckerTest):

    def test_postmortem_called_on_expiry(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        row = _open_trade_row(timestamp=old_ts)
        tracker._write_all([row])
        # Price between stop and target -- neither target_hit nor stop_hit fires,
        # only the expiry branch can close this trade.
        with patch.object(outcome_checker, "_fetch_live_price", return_value=1.1010), \
             patch("src.trade_postmortem.record_trade_postmortem") as mock_pm:
            closed = outcome_checker.check_open_trades(log=lambda m: None)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["status"], "EXPIRED")
        mock_pm.assert_called_once()
        self.assertEqual(mock_pm.call_args[0][0]["status"], "EXPIRED")


class TestPostmortemFailureNeverBreaksClose(_BaseOutcomeCheckerTest):

    def test_postmortem_exception_does_not_abort_close(self):
        row = _open_trade_row()
        tracker._write_all([row])
        with patch.object(outcome_checker, "_fetch_live_price", return_value=1.1105), \
             patch("src.trade_postmortem.record_trade_postmortem", side_effect=RuntimeError("boom")):
            try:
                closed = outcome_checker.check_open_trades(log=lambda m: None)
            except Exception as exc:
                self.fail(f"check_open_trades raised {exc!r} -- a postmortem failure "
                          f"must never break the real close path")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["status"], "WIN")
        rows = tracker.load()
        target = next(r for r in rows if str(r.get("id")) == str(row["id"]))
        self.assertEqual(target["status"], "WIN")


if __name__ == "__main__":
    unittest.main()
