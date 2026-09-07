"""Tests for the 2026-09-07 rejection-observability addition to
src/virtual_books.py: dd_mode/conf_threshold persisted per-candidate, and
book-level rejections (eligibility() or compute_sizing() saying no) wired
into shadow_mode the same way fires already are -- see that module's
docstring (REJECTION OBSERVABILITY section) for the full rationale.

Uses a fully isolated tmp directory for VBOOKS_DIR/CANDIDATES_CSV/
REJECTIONS_CSV and a minimal fake BOOKS registry (not the 6 real books) so
these tests exercise the rejection-tracking MACHINERY in isolation from any
real book's specific eligibility logic.
"""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import virtual_books as vb


def _fake_deep_result(pair="EUR/USD", direction="BUY", entry=1.1000, stop=1.0950,
                       target=1.1100, confidence=8, agreeing_count=2):
    return {
        "pair": pair,
        "parsed": {
            "direction": direction, "entry": entry, "stop_loss": stop,
            "target": target, "confidence": confidence,
        },
        "bundle": {"mtf": {"agreeing_count": agreeing_count}},
    }


def _always_true(r, quality_grades, dd_mode, conf_threshold, eff_conf_fn, dd_allows_fn):
    return True


def _always_false(r, quality_grades, dd_mode, conf_threshold, eff_conf_fn, dd_allows_fn):
    return False


class VirtualBooksRejectionTestCase(unittest.TestCase):
    """Shared isolation harness: real BOOKS/paths swapped out, restored on teardown."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_root = Path(self._tmpdir.name)
        self._patchers = [
            patch.object(vb, "VBOOKS_DIR", tmp_root),
            patch.object(vb, "CANDIDATES_CSV", tmp_root / "candidates.csv"),
            patch.object(vb, "REJECTIONS_CSV", tmp_root / "rejections.csv"),
            patch.object(vb, "BOOKS", {
                "FIRE":   vb.BookConfig("FIRE", "always eligible", _always_true),
                "REJECT": vb.BookConfig("REJECT", "never eligible", _always_false),
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
        return True   # unused by the fake eligibility functions above


class TestCandidateContextPersistence(VirtualBooksRejectionTestCase):

    def test_dd_mode_and_conf_threshold_persisted_on_new_candidate(self):
        result = vb.evaluate_candidates(
            [_fake_deep_result()], quality_grades={}, dd_mode="caution",
            conf_threshold=6.5, eff_conf_fn=self._eff_conf_fn,
            dd_allows_fn=self._dd_allows_fn, scan_mode="full", date_str="2026-09-07",
        )
        self.assertEqual(result["new_candidates"], 1)
        rows = vb._load_csv(vb.CANDIDATES_CSV)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dd_mode"], "caution")
        self.assertEqual(rows[0]["conf_threshold"], "6.5")


class TestRejectionRecording(VirtualBooksRejectionTestCase):

    def test_rejected_book_gets_pending_rejection_row(self):
        vb.evaluate_candidates(
            [_fake_deep_result()], quality_grades={}, dd_mode="normal",
            conf_threshold=6.0, eff_conf_fn=self._eff_conf_fn,
            dd_allows_fn=self._dd_allows_fn, scan_mode="full", date_str="2026-09-07",
        )
        rejections = vb._load_csv(vb.REJECTIONS_CSV)
        reject_rows = [r for r in rejections if r["book_id"] == "REJECT"]
        self.assertEqual(len(reject_rows), 1)
        self.assertEqual(reject_rows[0]["status"], "PENDING")
        self.assertEqual(reject_rows[0]["reason"], "eligibility")

    def test_fired_book_gets_no_rejection_row(self):
        vb.evaluate_candidates(
            [_fake_deep_result()], quality_grades={}, dd_mode="normal",
            conf_threshold=6.0, eff_conf_fn=self._eff_conf_fn,
            dd_allows_fn=self._dd_allows_fn, scan_mode="full", date_str="2026-09-07",
        )
        rejections = vb._load_csv(vb.REJECTIONS_CSV)
        fire_rows = [r for r in rejections if r["book_id"] == "FIRE"]
        self.assertEqual(fire_rows, [])
        positions = vb._load_csv(vb._positions_path("FIRE"))
        self.assertEqual(len(positions), 1)

    def test_sizing_rejection_recorded_with_sizing_reason(self):
        # Patch compute_sizing so FIRE's eligibility passes but sizing blocks it.
        with patch.object(vb, "BOOKS", {
            "FIRE": vb.BookConfig("FIRE", "always eligible", _always_true),
        }), patch.object(vb._fs_mod, "compute_sizing", return_value=(None, "blocked", "test block")):
            vb.evaluate_candidates(
                [_fake_deep_result()], quality_grades={}, dd_mode="normal",
                conf_threshold=6.0, eff_conf_fn=self._eff_conf_fn,
                dd_allows_fn=self._dd_allows_fn, scan_mode="full", date_str="2026-09-07",
            )
        rejections = vb._load_csv(vb.REJECTIONS_CSV)
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["reason"], "sizing")
        self.assertEqual(rejections[0]["status"], "PENDING")

    def test_repeated_evaluation_does_not_duplicate_rejection(self):
        for _ in range(3):
            vb.evaluate_candidates(
                [_fake_deep_result()], quality_grades={}, dd_mode="normal",
                conf_threshold=6.0, eff_conf_fn=self._eff_conf_fn,
                dd_allows_fn=self._dd_allows_fn, scan_mode="full", date_str="2026-09-07",
            )
        rejections = vb._load_csv(vb.REJECTIONS_CSV)
        reject_rows = [r for r in rejections if r["book_id"] == "REJECT"]
        self.assertEqual(len(reject_rows), 1)

    def test_later_fire_clears_earlier_pending_rejection(self):
        # First evaluation: book rejects.
        with patch.object(vb, "BOOKS", {"FLIP": vb.BookConfig("FLIP", "d", _always_false)}):
            vb.evaluate_candidates(
                [_fake_deep_result()], quality_grades={}, dd_mode="normal",
                conf_threshold=6.0, eff_conf_fn=self._eff_conf_fn,
                dd_allows_fn=self._dd_allows_fn, scan_mode="full", date_str="2026-09-07",
            )
        rejections = vb._load_csv(vb.REJECTIONS_CSV)
        self.assertEqual(len(rejections), 1)

        # Second evaluation (same day, e.g. a later intraday scan): now eligible.
        with patch.object(vb, "BOOKS", {"FLIP": vb.BookConfig("FLIP", "d", _always_true)}):
            vb.evaluate_candidates(
                [_fake_deep_result()], quality_grades={}, dd_mode="normal",
                conf_threshold=6.0, eff_conf_fn=self._eff_conf_fn,
                dd_allows_fn=self._dd_allows_fn, scan_mode="full", date_str="2026-09-07",
            )
        rejections = vb._load_csv(vb.REJECTIONS_CSV)
        self.assertEqual(rejections, [])
        positions = vb._load_csv(vb._positions_path("FLIP"))
        self.assertEqual(len(positions), 1)


class TestRejectionSettlement(VirtualBooksRejectionTestCase):

    def _open_and_settle(self, price_hits_target: bool):
        vb.evaluate_candidates(
            [_fake_deep_result(entry=1.1000, stop=1.0950, target=1.1100)],
            quality_grades={}, dd_mode="normal", conf_threshold=6.0,
            eff_conf_fn=self._eff_conf_fn, dd_allows_fn=self._dd_allows_fn,
            scan_mode="full", date_str="2026-09-07",
        )
        candidates = vb._load_csv(vb.CANDIDATES_CSV)
        self.assertEqual(len(candidates), 1)
        price = 1.1100 if price_hits_target else 1.0950
        vb.settle_open_candidates(prices={"EUR/USD": price})

    @patch("src.shadow_mode.record_evaluation")
    @patch("src.shadow_mode.register_rule")
    def test_settled_win_feeds_shadow_mode_would_fire_false(self, mock_register, mock_record):
        self._open_and_settle(price_hits_target=True)
        mock_register.assert_any_call(
            "vbook_REJECT", description="Virtual book REJECT: never eligible",
        )
        reject_calls = [c for c in mock_record.call_args_list if c.args[0] == "vbook_REJECT"]
        self.assertEqual(len(reject_calls), 1)
        _, kwargs = reject_calls[0]
        self.assertEqual(kwargs.get("would_fire"), False)
        self.assertEqual(kwargs.get("outcome"), "WIN")
        self.assertGreater(kwargs.get("net_pips"), 0)
        self.assertEqual(kwargs["context"]["reject_reason"], "eligibility")

    @patch("src.shadow_mode.record_evaluation")
    @patch("src.shadow_mode.register_rule")
    def test_settled_rejection_marked_settled_not_refed(self, mock_register, mock_record):
        self._open_and_settle(price_hits_target=True)
        rejections = vb._load_csv(vb.REJECTIONS_CSV)
        self.assertEqual(rejections[0]["status"], "SETTLED")

        # A second settlement pass (nothing left OPEN) must not re-feed shadow_mode.
        mock_record.reset_mock()
        vb.settle_open_candidates(prices={"EUR/USD": 1.1100})
        reject_calls = [c for c in mock_record.call_args_list if c.args[0] == "vbook_REJECT"]
        self.assertEqual(reject_calls, [])

    @patch("src.shadow_mode.record_evaluation")
    @patch("src.shadow_mode.register_rule")
    def test_fired_book_still_settles_normally_alongside_rejection(self, mock_register, mock_record):
        self._open_and_settle(price_hits_target=True)
        fire_calls = [c for c in mock_record.call_args_list if c.args[0] == "vbook_FIRE"]
        self.assertEqual(len(fire_calls), 1)
        _, kwargs = fire_calls[0]
        self.assertEqual(kwargs.get("would_fire"), True)
        self.assertEqual(kwargs.get("outcome"), "WIN")


if __name__ == "__main__":
    unittest.main()
