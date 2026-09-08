"""Regression test for the 2026-09-08 casc_oc NameError in
src/monitor.py::_apply_fund_milestones().

Real incident: fund trade #6987 (USD/JPY) hit its stop-loss and was
correctly closed as LOSS in trades.csv, with fund_state.json correctly
synced and the Telegram stop-loss alert sent -- all of that happens BEFORE
the crash point. The function then raised
`NameError: name 'casc_oc' is not defined` at the loss-autopsy check,
aborting the whole monitor run before it reached the Fund Trades Dashboard
/ Closed Fund Trades Discord embed updates further down in monitor.py's
run() (both live well after the per-trade loop that crashed).

Root cause (confirmed via git history, commit fda3729c, 2026-06-30): a
refactor correctly removed `casc_oc = _casc.cascade_outcome(row_state)`
from this function (fund trades don't have research trades' cascading
WIN/LOSS/PARTIAL_WIN ambiguity -- a STOP hit here is always a LOSS) and
replaced most `casc_oc` usages with the literal string "LOSS", but missed
two: the safety-net CSV rewrite's fallback value, and the loss-autopsy
gate. Both referenced a variable that was never defined in this function's
scope -- guaranteed to crash on every real fund stop-loss processed here
since that commit (confirmed only one such close has happened since:
#6987 itself, so no other real trade was silently affected before now).

This test reproduces the exact real shape (an OPEN fund trade whose price
has crossed its stop_loss, detected via a STOP milestone) end-to-end
through the real _apply_fund_milestones(), with real trades.csv/
fund_state.json writes redirected to a temp directory so the test is
hermetic but still exercises the genuine write paths -- confirming the fix
doesn't change any of the already-correct pre-crash writes, only unblocks
the code after them.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from src import monitor
from src import tracker
from src.trading import financials


def _fund_trade_row(trade_id=99001, pair="USD/JPY", direction="BUY",
                     entry=156.197, stop_loss=154.6068, target=159.3774):
    row = {f: "" for f in tracker.FIELDS}
    row.update({
        "id": trade_id,
        "timestamp": "2026-09-08 05:15:56",
        "pair": pair,
        "direction": direction,
        "confidence": 7,
        "entry": entry,
        "stop_loss": stop_loss,
        "effective_stop": stop_loss,
        "target": target,
        "t2_price": target,
        "trade_this": "YES",
        "status": "OPEN",
        "position_size_pct_at_entry": 1.0,
        "system_version": "v2",
    })
    return row


class TestFundStopLossCascOcFix(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_root = Path(self._tmpdir.name)
        self._trades_csv = tmp_root / "trades.csv"
        self._fund_state_json = tmp_root / "fund_state.json"
        self._milestone_log = tmp_root / "milestone_log.json"

        tracker._write_all.__globals__  # no-op, keeps linters happy about usage
        self._patchers = [
            patch.object(config, "TRADES_CSV", self._trades_csv),
            patch.object(financials, "TRADES_CSV", self._trades_csv),
            patch.object(financials, "FUND_STATE_JSON", self._fund_state_json),
            patch.object(monitor, "_MILESTONE_LOG", self._milestone_log),
            patch.object(monitor, "_dn", None),
        ]
        for p in self._patchers:
            p.start()

        self._row = _fund_trade_row()
        tracker._write_all([self._row])

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmpdir.cleanup()

    def _row_state_stopped_out(self):
        row_state = dict(self._row)
        row_state["t2_hit"] = "FALSE"
        return row_state

    def test_stop_hit_no_longer_raises_nameerror(self):
        """The exact real shape: a STOP milestone on an open fund trade."""
        milestones = [{"level": "STOP", "price": 154.6068, "candle_dt": "2026-09-08 00:00", "pips": None}]
        with patch.object(monitor, "_analyse_loss", return_value={"summary": "test"}) as _mock_analyse, \
             patch.object(monitor, "_save_loss_analysis") as _mock_save:
            closed = monitor._apply_fund_milestones(
                self._row, milestones, self._row_state_stopped_out(), log=lambda m: None,
            )
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["status"], "LOSS")
        # The whole point of the fix: loss autopsy is now actually reached.
        _mock_analyse.assert_called_once()
        _mock_save.assert_called_once()

    def test_trades_csv_correctly_shows_loss_after_fix(self):
        """Confirms the pre-crash writes are unchanged by the fix."""
        milestones = [{"level": "STOP", "price": 154.6068, "candle_dt": "2026-09-08 00:00", "pips": None}]
        with patch.object(monitor, "_analyse_loss", return_value={}), \
             patch.object(monitor, "_save_loss_analysis"):
            monitor._apply_fund_milestones(
                self._row, milestones, self._row_state_stopped_out(), log=lambda m: None,
            )
        rows = tracker.load()
        target = next(r for r in rows if str(r.get("id")) == str(self._row["id"]))
        self.assertEqual(target["status"], "LOSS")
        self.assertEqual(float(target["exit_price"]), 154.6068)

    def test_fund_state_json_synced_after_fix(self):
        milestones = [{"level": "STOP", "price": 154.6068, "candle_dt": "2026-09-08 00:00", "pips": None}]
        with patch.object(monitor, "_analyse_loss", return_value={}), \
             patch.object(monitor, "_save_loss_analysis"):
            monitor._apply_fund_milestones(
                self._row, milestones, self._row_state_stopped_out(), log=lambda m: None,
            )
        self.assertTrue(self._fund_state_json.exists())
        state = json.loads(self._fund_state_json.read_text(encoding="utf-8"))
        self.assertIn("balance", state)


if __name__ == "__main__":
    unittest.main()
