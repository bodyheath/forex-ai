"""Tests for the 2026-09-09 post-outage catch-up lookback in src/monitor.py.

Real gap: the normal candle window (_OHLCV_CANDLES = 6 hours) silently
assumes monitor.py has been running continuously. A real ~20-hour outage
(the monitor.lock incident, same day) proved that wrong -- the next run
after a gap only ever looks at the last 6 hours "as of now", so any
stop/target level touched and reversed before that trailing window is
permanently invisible, with zero trace. This widens the lookback for a
single run when a real gap is detected, measured off heartbeat.json's own
content (not filesystem mtime -- the same mtime-reset class of bug that let
the monitor.lock incident run undetected for 20 hours).

_compute_catchup_window() is a standalone function precisely so this can be
tested in isolation, without invoking run() itself -- run() performs real
network fetches and touches a dozen other real data files (virtual books,
shadow_rules, balance reconciliation, ...) with no test-mode switch, so
calling it directly from a "unit" test silently exercises production I/O
against whatever the test's cwd happens to be.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src import monitor


class TestComputeCatchupWindow(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._hb_path = Path(self._tmpdir.name) / "heartbeat.json"
        self._hb_patcher = patch("src.monitor._HEARTBEAT_FILE", self._hb_path)
        self._hb_patcher.start()

    def tearDown(self):
        self._hb_patcher.stop()
        self._tmpdir.cleanup()

    def _write_heartbeat(self, age_hours: float):
        ts = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        self._hb_path.write_text(json.dumps({
            "last_run": ts.isoformat(),
            "last_monitor_run": ts.isoformat(),
            "monitor_interval_mins": 30,
        }), encoding="utf-8")

    def test_normal_gap_does_not_widen_window(self):
        self._write_heartbeat(age_hours=0.5)  # normal 30-min cadence
        logs = []
        period, candles = monitor._compute_catchup_window(log=logs.append)
        self.assertIsNone(period)
        self.assertEqual(candles, monitor._OHLCV_CANDLES)
        self.assertFalse(any("widening this run's OHLCV lookback" in m for m in logs))

    def test_real_outage_gap_widens_window(self):
        self._write_heartbeat(age_hours=20.0)  # the real 2026-09-08/09 incident
        logs = []
        period, candles = monitor._compute_catchup_window(log=logs.append)
        # 20h gap -> int(20 // 24) + 2 day margin = 2d (covers the full 20h easily)
        self.assertEqual(period, "2d")
        self.assertEqual(candles, 24)
        widen_lines = [m for m in logs if "widening this run's OHLCV lookback" in m]
        self.assertEqual(len(widen_lines), 1)
        self.assertIn("20.0h gap", widen_lines[0])

    def test_gap_measured_from_content_not_mtime(self):
        """The core regression: heartbeat.json's mtime is fresh (as it would
        be after a fresh git checkout) but its own content says the real gap
        is 20 hours -- the widened window must still fire, proving gap
        detection reads the committed value, not filesystem metadata."""
        self._write_heartbeat(age_hours=20.0)
        import os as _os
        import time as _time
        now = _time.time()
        _os.utime(self._hb_path, (now, now))  # force fresh mtime

        period, _candles = monitor._compute_catchup_window(log=lambda m: None)
        self.assertEqual(period, "2d")

    def test_no_heartbeat_file_uses_normal_window(self):
        # First-ever run, or heartbeat.json missing entirely.
        period, candles = monitor._compute_catchup_window(log=lambda m: None)
        self.assertIsNone(period)
        self.assertEqual(candles, monitor._OHLCV_CANDLES)

    def test_corrupted_heartbeat_falls_back_to_normal_window(self):
        self._hb_path.write_text("not valid json", encoding="utf-8")
        period, candles = monitor._compute_catchup_window(log=lambda m: None)
        self.assertIsNone(period)
        self.assertEqual(candles, monitor._OHLCV_CANDLES)

    def test_missing_last_monitor_run_field_uses_normal_window(self):
        self._hb_path.write_text(json.dumps({"monitor_interval_mins": 30}), encoding="utf-8")
        period, candles = monitor._compute_catchup_window(log=lambda m: None)
        self.assertIsNone(period)
        self.assertEqual(candles, monitor._OHLCV_CANDLES)

    def test_extreme_gap_is_capped_at_30_days(self):
        self._write_heartbeat(age_hours=24 * 400)  # a year-old/corrupted-looking gap
        logs = []
        period, _candles = monitor._compute_catchup_window(log=logs.append)
        self.assertEqual(period, "30d")
        widen_lines = [m for m in logs if "widening this run's OHLCV lookback" in m]
        self.assertEqual(len(widen_lines), 1)


class TestFetch1hCandlesPeriodParameter(unittest.TestCase):

    def test_default_period_unchanged(self):
        captured = {}

        class _FakeTicker:
            def history(self, period, interval, auto_adjust):
                captured["period"] = period
                import pandas as pd
                return pd.DataFrame()

        with patch("yfinance.Ticker", return_value=_FakeTicker()):
            from src.yahoo_finance import fetch_1h_candles
            fetch_1h_candles("AUD/NZD", 6, log=lambda m: None)
        self.assertEqual(captured["period"], "2d")

    def test_wider_period_is_passed_through(self):
        captured = {}

        class _FakeTicker:
            def history(self, period, interval, auto_adjust):
                captured["period"] = period
                import pandas as pd
                return pd.DataFrame()

        with patch("yfinance.Ticker", return_value=_FakeTicker()):
            from src.yahoo_finance import fetch_1h_candles
            fetch_1h_candles("AUD/NZD", 30, log=lambda m: None, period="3d")
        self.assertEqual(captured["period"], "3d")


if __name__ == "__main__":
    unittest.main()
