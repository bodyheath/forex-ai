"""Tests for the 2026-09-19 wiring of src/trade_postmortem.py into
src/monitor.py::_apply_fund_milestones() -- the between-scan close path for
real fund trades (T2 target hit -> WIN, STOP hit -> LOSS). Mirrors
tests/test_outcome_checker_postmortem_wiring.py's coverage of the other two
real close paths (scan-time target/stop/expiry) in src/outcome_checker.py.

Follows tests/test_monitor_fund_stop_loss_casc_oc_fix.py's hermetic setup
exactly (temp trades.csv/fund_state.json, monitor._dn forced to None). `ta`
is deliberately left at its default (None) in every call here -- the one
real, unmocked live-send path this function has -- so no test in this file
can ever send a real Telegram/Discord message.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from src import monitor
from src import tracker
from src.trading import financials


def _fund_trade_row(trade_id=50101, pair="USD/JPY", direction="BUY",
                     entry=156.197, stop_loss=154.6068, target=159.3774):
    row = {f: "" for f in tracker.FIELDS}
    row.update({
        "id": trade_id,
        "timestamp": "2026-09-18 05:15:56",
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


class _BaseMonitorPostmortemTest(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_root = Path(self._tmpdir.name)
        self._trades_csv = tmp_root / "trades.csv"
        self._fund_state_json = tmp_root / "fund_state.json"
        self._milestone_log = tmp_root / "milestone_log.json"

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


class TestTargetHitRecordsPostmortem(_BaseMonitorPostmortemTest):

    def test_postmortem_called_on_t2_win(self):
        milestones = [{"level": "T2", "price": 159.3774, "candle_dt": "2026-09-18 12:00", "pips": 320.0}]
        with patch("src.trade_postmortem.record_trade_postmortem") as mock_pm:
            closed = monitor._apply_fund_milestones(
                self._row, milestones, dict(self._row), log=lambda m: None,
            )
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["status"], "WIN")
        mock_pm.assert_called_once()
        self.assertEqual(mock_pm.call_args[0][0]["status"], "WIN")


class TestStopHitRecordsPostmortem(_BaseMonitorPostmortemTest):

    def test_postmortem_called_on_stop_loss(self):
        milestones = [{"level": "STOP", "price": 154.6068, "candle_dt": "2026-09-18 12:00", "pips": None}]
        row_state = dict(self._row)
        row_state["t2_hit"] = "FALSE"
        with patch.object(monitor, "_analyse_loss", return_value={}), \
             patch.object(monitor, "_save_loss_analysis"), \
             patch("src.trade_postmortem.record_trade_postmortem") as mock_pm:
            closed = monitor._apply_fund_milestones(
                self._row, milestones, row_state, log=lambda m: None,
            )
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["status"], "LOSS")
        mock_pm.assert_called_once()
        self.assertEqual(mock_pm.call_args[0][0]["status"], "LOSS")


class TestResearchCloseDoesNotRecordFundPostmortem(unittest.TestCase):
    """_record_postmortem_closure() is scoped to source_table == 'main' only
    -- research trades aren't in scope for the real-fund postmortem log."""

    def test_research_source_table_skipped(self):
        with patch("src.trade_postmortem.record_trade_postmortem") as mock_pm:
            monitor._record_postmortem_closure("research", {"id": 1}, log=lambda m: None)
        mock_pm.assert_not_called()


class TestPostmortemFailureNeverBreaksClose(_BaseMonitorPostmortemTest):

    def test_postmortem_exception_does_not_abort_stop_close(self):
        milestones = [{"level": "STOP", "price": 154.6068, "candle_dt": "2026-09-18 12:00", "pips": None}]
        row_state = dict(self._row)
        row_state["t2_hit"] = "FALSE"
        with patch.object(monitor, "_analyse_loss", return_value={}), \
             patch.object(monitor, "_save_loss_analysis"), \
             patch("src.trade_postmortem.record_trade_postmortem", side_effect=RuntimeError("boom")):
            try:
                closed = monitor._apply_fund_milestones(
                    self._row, milestones, row_state, log=lambda m: None,
                )
            except Exception as exc:
                self.fail(f"_apply_fund_milestones raised {exc!r} -- a postmortem "
                          f"failure must never break the real close path")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["status"], "LOSS")
        rows = tracker.load()
        target = next(r for r in rows if str(r.get("id")) == str(self._row["id"]))
        self.assertEqual(target["status"], "LOSS")


if __name__ == "__main__":
    unittest.main()
