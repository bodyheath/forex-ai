"""Regression tests for the 2026-09-2X data_quality_flag PERSISTENCE fix in
src/tracker.py.

Real incident this guards against: data_quality_flag was added directly to
trades.csv on 2026-09-17 (a raw CSV edit, not via tracker.update_fields())
to mark #6987 as a confirmed-fictitious close -- but the field was never
added to tracker.FIELDS. Every routine trades.csv rewrite (_write_all(),
triggered by ANY update_fields()/update_outcome() call, on ANY row) writes
out only the columns named in FIELDS -- so a column that exists on a live
row but isn't in that list is silently dropped on the very next routine
write, regardless of which row triggered it. Confirmed via git history: the
flag was gone by the very next day's routine automated commit
(2026-09-18), and the real data_quality_flag exclusion in financials.py
(already tested in test_data_quality_flag_exclusion.py) was a structural
no-op in production the entire time since, because pd.read_csv never saw
the column at all.

These tests are about PERSISTENCE (does the column survive a write cycle it
wasn't the target of) -- test_data_quality_flag_exclusion.py already covers
CONSUMPTION (how calculate_fund_state() treats a flagged row once present).
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from src import tracker


class TestDataQualityFlagInSchema(unittest.TestCase):

    def test_data_quality_flag_is_a_canonical_field(self):
        self.assertIn("data_quality_flag", tracker.FIELDS,
                       "data_quality_flag must be in tracker.FIELDS or it "
                       "cannot survive a routine trades.csv rewrite -- see "
                       "this file's module docstring for the real incident")


class TestDataQualityFlagPersistence(unittest.TestCase):
    """Hermetic: every test redirects config.TRADES_CSV to a temp file."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._trades_csv = Path(self._tmpdir.name) / "trades.csv"
        self._patcher = patch.object(config, "TRADES_CSV", self._trades_csv)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def _row(self, rec_id):
        row = {f: "" for f in tracker.FIELDS}
        row.update({
            "id": rec_id, "timestamp": "2026-09-08 05:15:56", "pair": "USD/JPY",
            "direction": "BUY", "status": "LOSS", "trade_this": "YES",
            "system_version": "v2",
        })
        return row

    def test_flag_set_via_update_fields_is_readable_immediately(self):
        tracker._write_all([self._row(1)])
        tracker.update_fields(1, data_quality_flag="stale_daily_bar_entry_price")
        rows = tracker.load()
        self.assertEqual(rows[0]["data_quality_flag"], "stale_daily_bar_entry_price")

    def test_flag_survives_an_unrelated_rows_routine_rewrite(self):
        """The exact real regression: a routine write triggered by a
        DIFFERENT row must not silently drop this row's flag."""
        tracker._write_all([self._row(1), self._row(2)])
        tracker.update_fields(1, data_quality_flag="stale_daily_bar_entry_price")

        # Simulate a routine write with no knowledge of row 1 at all --
        # exactly what happens on every real trade close/update anywhere
        # else in the system (e.g. #2's own outcome being recorded).
        tracker.update_fields(2, notes="unrelated routine update")

        rows = tracker.load()
        row1 = next(r for r in rows if str(r["id"]) == "1")
        row2 = next(r for r in rows if str(r["id"]) == "2")
        self.assertEqual(row1["data_quality_flag"], "stale_daily_bar_entry_price",
                          "flag must survive a routine rewrite of an unrelated row")
        self.assertEqual(row2["notes"], "unrelated routine update")

    def test_flag_survives_update_outcome_on_a_different_row(self):
        """Same regression, but via update_outcome() -- the real call shape
        every actual trade close uses (see outcome_checker.py/monitor.py)."""
        tracker._write_all([self._row(1), self._row(2)])
        tracker.update_fields(1, data_quality_flag="stale_daily_bar_entry_price")

        tracker.update_outcome(2, "LOSS", exit_price=1.0, notes="closed via monitor")

        rows = tracker.load()
        row1 = next(r for r in rows if str(r["id"]) == "1")
        self.assertEqual(row1["data_quality_flag"], "stale_daily_bar_entry_price")

    def test_flag_survives_many_consecutive_unrelated_rewrites(self):
        """Simulates a realistic sequence of routine writes across several
        other rows -- the flag must be stable across all of them, not just
        the first one."""
        rows = [self._row(i) for i in range(1, 6)]
        tracker._write_all(rows)
        tracker.update_fields(1, data_quality_flag="stale_daily_bar_entry_price")

        for other_id in (2, 3, 4, 5):
            tracker.update_fields(other_id, notes=f"routine update #{other_id}")

        final = tracker.load()
        row1 = next(r for r in final if str(r["id"]) == "1")
        self.assertEqual(row1["data_quality_flag"], "stale_daily_bar_entry_price")

    def test_a_field_not_in_canonical_schema_would_not_survive(self):
        """Documents the general mechanism this regression class depends on
        -- any field genuinely absent from FIELDS is dropped by design, not
        by accident. Confirms _write_all()'s real behavior directly rather
        than asserting it only indirectly via the data_quality_flag case
        above."""
        row = self._row(1)
        row["totally_unrecognized_field"] = "should not survive"
        tracker._write_all([row])
        reloaded = tracker.load()
        self.assertNotIn("totally_unrecognized_field", reloaded[0])


if __name__ == "__main__":
    unittest.main()
