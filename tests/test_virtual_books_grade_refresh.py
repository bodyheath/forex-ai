"""Tests for the 2026-09-09 stale-grade fix in
src/virtual_books.py::evaluate_candidates().

Real incident: AUD/NZD SELL (2026-09-07) was created with grade=F --
correctly rejected by every grade-respecting book at that moment (confirmed
by reading _dd_allows_trade() directly: the F hard floor has no bug). Hours
later the same day, a re-analysis (the pair moved >10 pips, bypassing the
skip-cache) produced a materially different grade, and B_conf6_rr15/D_no_da
admitted it 11 hours after creation, A_control/C_grade_based/E_no_dd_gate
18 hours after creation. Every admission was a correct decision against the
REAL grade in effect at that moment -- but candidates.csv kept showing
"F" forever, because evaluate_candidates() only ever set the descriptive
fields (grade/confidence/eff_conf/da_grade_before/rr/mtf_agreeing_count/
dd_mode/conf_threshold) at first creation and never refreshed them on
same-day reuse. This is a data-integrity bug (misleading records), not an
admission-logic bug (every book's own rule was correctly enforced against
the real grade at decision time) -- these tests cover the former.

Uses the same isolated tmp-dir + fake-BOOKS harness as
tests/test_virtual_books_rejections.py.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import virtual_books as vb


def _fake_deep_result(pair="AUD/NZD", direction="SELL", entry=1.2236, stop=1.2390,
                       target=1.1928, confidence=7, agreeing_count=2):
    return {
        "pair": pair,
        "parsed": {
            "direction": direction, "entry": entry, "stop_loss": stop,
            "target": target, "confidence": confidence,
        },
        "bundle": {"mtf": {"agreeing_count": agreeing_count}},
    }


def _grade_gated(r, quality_grades, dd_mode, conf_threshold, eff_conf_fn, dd_allows_fn):
    grade = (quality_grades.get(r["pair"]) or {}).get("grade", "F")
    return grade in ("A", "B", "C")


class TestStaleGradeRefresh(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_root = Path(self._tmpdir.name)
        self._patchers = [
            patch.object(vb, "VBOOKS_DIR", tmp_root),
            patch.object(vb, "CANDIDATES_CSV", tmp_root / "candidates.csv"),
            patch.object(vb, "REJECTIONS_CSV", tmp_root / "rejections.csv"),
            patch.object(vb, "BOOKS", {
                "GRADED": vb.BookConfig("GRADED", "grade-gated", _grade_gated),
            }),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmpdir.cleanup()

    def _eff_conf_fn(self, r):
        return float((r.get("parsed") or {}).get("confidence") or 0)

    def _dd_allows_fn(self, *a, **kw):
        return True

    def _evaluate(self, grade, dd_mode="normal", conf_threshold=6.0):
        return vb.evaluate_candidates(
            [_fake_deep_result()],
            quality_grades={"AUD/NZD": {"grade": grade, "da_grade_before": grade, "rr": 2.0}},
            dd_mode=dd_mode, conf_threshold=conf_threshold,
            eff_conf_fn=self._eff_conf_fn, dd_allows_fn=self._dd_allows_fn,
            scan_mode="full", date_str="2026-09-07",
        )

    def test_stored_grade_refreshes_on_same_day_reanalysis(self):
        # First scan: grade=F, correctly rejected everywhere.
        self._evaluate(grade="F")
        rows = vb._load_csv(vb.CANDIDATES_CSV)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["grade"], "F")
        rejections = vb._load_csv(vb.REJECTIONS_CSV)
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["status"], "PENDING")

        # Later same-day re-analysis: grade improves to C -- GRADED book
        # should now admit it, and the stored row must show the REAL grade
        # that actually drove admission, not the stale "F".
        self._evaluate(grade="C")
        rows = vb._load_csv(vb.CANDIDATES_CSV)
        self.assertEqual(len(rows), 1, "must reuse the same row, not create a second one")
        self.assertEqual(rows[0]["grade"], "C")
        self.assertEqual(rows[0]["da_grade_before"], "C")

        positions = vb._load_csv(vb._positions_path("GRADED"))
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["candidate_id"], str(rows[0]["id"]))

        # The earlier PENDING rejection must be gone -- shadow_mode must
        # only ever see this book's FINAL decision, never both.
        rejections = vb._load_csv(vb.REJECTIONS_CSV)
        self.assertEqual(rejections, [])

    def test_entry_stop_target_never_change_on_reevaluation(self):
        # These define the shared mechanical trade every book settles its
        # own P&L against -- must stay frozen regardless of grade changes.
        self._evaluate(grade="F")
        rows_before = vb._load_csv(vb.CANDIDATES_CSV)
        self._evaluate(grade="C")
        rows_after = vb._load_csv(vb.CANDIDATES_CSV)
        for field in ("entry", "stop_loss", "t2_price", "opened_at", "id"):
            self.assertEqual(rows_before[0][field], rows_after[0][field], field)

    def test_dd_mode_and_conf_threshold_also_refresh(self):
        self._evaluate(grade="F", dd_mode="caution", conf_threshold=7.0)
        rows = vb._load_csv(vb.CANDIDATES_CSV)
        self.assertEqual(rows[0]["dd_mode"], "caution")
        self.assertEqual(rows[0]["conf_threshold"], "7.0")

        self._evaluate(grade="C", dd_mode="normal", conf_threshold=6.0)
        rows = vb._load_csv(vb.CANDIDATES_CSV)
        self.assertEqual(rows[0]["dd_mode"], "normal")
        self.assertEqual(rows[0]["conf_threshold"], "6.0")

    def test_grade_regressing_after_a_book_already_holds_a_position_does_not_affect_that_position(self):
        # Book admits on grade=C. A later same-day re-analysis regresses to
        # F (e.g. a fresh DA downgrade) -- the ALREADY-OPEN position must be
        # untouched (a book's decision, once made, is final for that
        # candidate); only the shared descriptive fields keep refreshing.
        self._evaluate(grade="C")
        positions_before = vb._load_csv(vb._positions_path("GRADED"))
        self.assertEqual(len(positions_before), 1)

        self._evaluate(grade="F")
        positions_after = vb._load_csv(vb._positions_path("GRADED"))
        self.assertEqual(positions_before, positions_after)

        rows = vb._load_csv(vb.CANDIDATES_CSV)
        self.assertEqual(rows[0]["grade"], "F")  # descriptive field still refreshes


if __name__ == "__main__":
    unittest.main()
