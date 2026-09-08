"""Tests for the 2026-09-09 dashboard-update diagnostic file in
src/monitor.py.

Real incident: data/discord_dashboard.json's last_dashboard_balance was
frozen since 2026-09-01 -- a full week of real GHA monitor runs silently
failing or skipping the Discord fund-dashboard-embed update, with the
only trace ever being a gitignored daily_*.log file no one outside that
one GHA run could see. _record_dashboard_diagnostic() writes the outcome
of each attempt to a committed file instead, so the next real failure (or
success) is directly visible in the repo.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import monitor


class TestRecordDashboardDiagnostic(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._path = Path(self._tmpdir.name) / "discord_dashboard_diagnostic.json"
        self._patcher = patch(
            "src.monitor.Path",
            side_effect=lambda p: self._path if p == "data/discord_dashboard_diagnostic.json" else Path(p),
        )

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_success_is_recorded(self):
        with self._patcher:
            monitor._record_dashboard_diagnostic("dashboard", True, "sent")
        data = json.loads(self._path.read_text(encoding="utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["detail"], "sent")
        self.assertIn("last_attempt_utc", data)

    def test_failure_is_recorded_with_detail(self):
        with self._patcher:
            monitor._record_dashboard_diagnostic("dashboard", False, "ValueError: boom\ntraceback here")
        data = json.loads(self._path.read_text(encoding="utf-8"))
        self.assertFalse(data["ok"])
        self.assertIn("boom", data["detail"])

    def test_never_raises_even_if_write_fails(self):
        with patch("src.monitor.Path", side_effect=OSError("disk full")):
            try:
                monitor._record_dashboard_diagnostic("dashboard", True, "sent")
            except Exception as exc:
                self.fail(f"_record_dashboard_diagnostic raised {exc!r} -- must never break the caller")


if __name__ == "__main__":
    unittest.main()
