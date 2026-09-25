"""Regression tests for the 2026-09-2X entry-date fix to
src/health_check.py::check_grade_ordering().

Real bug this closes: a row's grade is frozen at ENTRY time and never
recomputed when it later closes. The existing closed_at-based cutoff
(_GRADE_ORDERING_CUTOFF, get_strict_decisive_grade_population()) filters by
when a trade CLOSED, which is not the same thing -- after the 2026-09-06
ribbon_general_population_demotion_removed fix narrowed Grade F's real
trigger condition, 90.4% of the population the standing F-vs-D alert was
still flagging (329 of 364 rows) had actually been ENTERED, and therefore
GRADED, before that fix -- carrying a grade computed under the old, broader
condition, not the current one. The one genuinely clean post-fix-entry
slice available showed the opposite direction (not confirmed either way,
too thin), meaning the standing alert was measuring a stale, contaminated
blend. This fix adds an entry-date (the "date" column) filter on top of the
existing closed_at cutoff, scoped to check_grade_ordering() only --
get_strict_decisive_grade_population() itself (also used by dashboard.py)
is deliberately left untouched, per instruction.
"""
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src import health_check as hc


def _row(id_, grade, status, date, closed_at="2026-09-15 00:00:00"):
    return {
        "id": id_, "status": status, "system_version": "v2",
        "closed_at": closed_at, "date": date, "grade": grade,
        "pair": "EUR/USD", "direction": "BUY", "net_pips": 1.0,
    }


class TestGradeOrderingEntryDateFilter(unittest.TestCase):

    def _write_csv(self, rows) -> str:
        self._tmpdir = tempfile.TemporaryDirectory()
        path = Path(self._tmpdir.name) / "research_trades.csv"
        pd.DataFrame(rows).to_csv(path, index=False)
        return str(path)

    def tearDown(self):
        if hasattr(self, "_tmpdir"):
            self._tmpdir.cleanup()

    def test_pre_fix_only_inversion_is_not_flagged(self):
        """All rows entered before the 2026-09-06 fix -- even though this
        population WOULD show a stark inversion under the old (uncorrected)
        methodology, the fix filters it out entirely: no clean data to
        evaluate, so no flag."""
        rows = []
        # Grade F, pre-fix entries, artificially HIGH win rate (the exact
        # kind of stale-contamination shape the real bug produced)
        for i in range(20):
            rows.append(_row(i, "F", "WIN" if i < 17 else "LOSS", "2026-08-15 00:00:00"))
        # Grade D, pre-fix entries, artificially LOW win rate
        for i in range(20, 40):
            rows.append(_row(i, "D", "LOSS" if i < 37 else "WIN", "2026-08-20 00:00:00"))
        path = self._write_csv(rows)
        flags = hc.check_grade_ordering(csv_path=path)
        self.assertEqual(flags, [])

    def test_mixed_pre_and_post_fix_uses_only_post_fix_rows(self):
        """Pre-fix rows still show the stale inverted pattern; post-fix
        rows (same grades) show NO inversion (F and D win at the same
        rate) -- the check must follow the post-fix rows only, and stay
        silent."""
        rows = []
        # Pre-fix: stale inverted pattern (would trigger the old bug)
        for i in range(20):
            rows.append(_row(i, "F", "WIN" if i < 17 else "LOSS", "2026-08-15 00:00:00"))
        for i in range(20, 40):
            rows.append(_row(i, "D", "LOSS" if i < 37 else "WIN", "2026-08-20 00:00:00"))
        # Post-fix: same win rate for both grades -- no real inversion
        for i in range(40, 60):
            rows.append(_row(i, "F", "WIN" if i < 50 else "LOSS", "2026-09-10 00:00:00"))
        for i in range(60, 80):
            rows.append(_row(i, "D", "WIN" if i < 70 else "LOSS", "2026-09-10 00:00:00"))
        path = self._write_csv(rows)
        flags = hc.check_grade_ordering(csv_path=path)
        self.assertEqual(flags, [])

    def test_genuine_post_fix_inversion_still_flags(self):
        """The fix must not silence everything -- if the CLEAN, post-fix-
        entry population itself shows a real, significant inversion, it
        must still fire."""
        rows = []
        # Post-fix only, clear real inversion: F wins ~90%, D wins ~10%
        for i in range(30):
            rows.append(_row(i, "F", "WIN" if i < 27 else "LOSS", "2026-09-10 00:00:00"))
        for i in range(30, 60):
            rows.append(_row(i, "D", "LOSS" if i < 57 else "WIN", "2026-09-12 00:00:00"))
        path = self._write_csv(rows)
        flags = hc.check_grade_ordering(csv_path=path)
        self.assertEqual(len(flags), 1)
        self.assertIn("grade F", flags[0])
        self.assertIn("outperforms grade D", flags[0])

    def test_exactly_on_fix_date_is_included(self):
        """>= comparison -- an entry dated exactly on the fix date counts
        as post-fix, not pre-fix."""
        rows = []
        for i in range(30):
            rows.append(_row(i, "F", "WIN" if i < 27 else "LOSS", "2026-09-06 00:00:00"))
        for i in range(30, 60):
            rows.append(_row(i, "D", "LOSS" if i < 57 else "WIN", "2026-09-06 00:00:01"))
        path = self._write_csv(rows)
        flags = hc.check_grade_ordering(csv_path=path)
        self.assertEqual(len(flags), 1)

    def test_no_date_column_falls_back_to_unfiltered_behavior(self):
        """A population with no 'date' column at all (e.g. an older
        snapshot, or a synthetic test fixture) must not crash -- the entry-
        date filter is skipped gracefully, matching how every other
        optional-column check in this file already degrades."""
        rows = []
        for i in range(20):
            r = _row(i, "F", "WIN" if i < 17 else "LOSS", "2026-08-15 00:00:00")
            del r["date"]
            rows.append(r)
        for i in range(20, 40):
            r = _row(i, "D", "LOSS" if i < 37 else "WIN", "2026-08-20 00:00:00")
            del r["date"]
            rows.append(r)
        path = self._write_csv(rows)
        try:
            flags = hc.check_grade_ordering(csv_path=path)
        except Exception as exc:
            self.fail(f"check_grade_ordering raised with no 'date' column: {exc!r}")
        # Without a date column to filter on, falls back to the existing
        # closed_at-only behavior -- the stale pattern is visible again.
        self.assertEqual(len(flags), 1)

    def test_thin_post_fix_population_stays_silent(self):
        """Below _GRADE_MIN_N after the entry-date filter narrows the
        population -- must stay silent, not flag on a now-thinner sample."""
        rows = []
        for i in range(5):  # below _GRADE_MIN_N=15
            rows.append(_row(i, "F", "WIN", "2026-09-10 00:00:00"))
        for i in range(5, 10):
            rows.append(_row(i, "D", "LOSS", "2026-09-10 00:00:00"))
        path = self._write_csv(rows)
        flags = hc.check_grade_ordering(csv_path=path)
        self.assertEqual(flags, [])


if __name__ == "__main__":
    unittest.main()
