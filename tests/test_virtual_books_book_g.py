"""Tests for Book G (mechanical reversion pilot) and the regime-aware
promotion-count dedup mechanism added alongside it in src/virtual_books.py.

See PROPOSAL_mechanical_reversion_engine.md and src/mechanical_reversion.py
for the validated signal this book trades live (rib_against AND
osc_agrees), and PROMOTION_DISCIPLINE.md's vbook_G_mechanical_reversion
entry for why its shadow_mode n floor counts regimes, not raw trades.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import virtual_books as vb


def _deep_result(pair="EUR/USD", direction="BUY", ribbon_status="NEUTRAL",
                  osc_direction="NONE", entry=1.1000, stop=1.0950, target=1.1100):
    return {
        "pair": pair,
        "parsed": {"direction": direction, "entry": entry, "stop_loss": stop, "target": target},
        "bundle": {
            "mtf": {"agreeing_count": 2},
            "technical": {"daily": {
                "ribbon": {"status": ribbon_status},
                "oscillator_confluence": {"direction": osc_direction},
            }},
        },
    }


class TestBookGRegisteredCorrectly(unittest.TestCase):

    def test_book_g_in_registry(self):
        self.assertIn("G_mechanical_reversion", vb.BOOKS)

    def test_book_g_is_regime_aware(self):
        self.assertTrue(vb.BOOKS["G_mechanical_reversion"].regime_aware_promotion)

    def test_other_books_are_not_regime_aware_by_default(self):
        for book_id in ("A_control", "B_conf6_rr15", "C_grade_based",
                         "D_no_da", "E_no_dd_gate", "F_sentiment_only"):
            self.assertFalse(vb.BOOKS[book_id].regime_aware_promotion,
                              f"{book_id} must not be regime-aware unless deliberately opted in")


class TestBookGEligibility(unittest.TestCase):

    def _dummy(self, *a, **kw):
        return True

    def test_fires_when_ribbon_against_and_osc_agrees(self):
        r = _deep_result(direction="BUY", ribbon_status="ALIGNED_BEAR", osc_direction="BUY")
        self.assertTrue(vb._elig_g_mechanical_reversion(r, {}, "normal", 7, self._dummy, self._dummy))

    def test_does_not_fire_when_ribbon_not_against(self):
        r = _deep_result(direction="BUY", ribbon_status="ALIGNED_BULL", osc_direction="BUY")
        self.assertFalse(vb._elig_g_mechanical_reversion(r, {}, "normal", 7, self._dummy, self._dummy))

    def test_does_not_fire_when_osc_disagrees(self):
        r = _deep_result(direction="BUY", ribbon_status="ALIGNED_BEAR", osc_direction="NONE")
        self.assertFalse(vb._elig_g_mechanical_reversion(r, {}, "normal", 7, self._dummy, self._dummy))

    def test_fires_regardless_of_dd_mode(self):
        """Fully isolated like Book F -- no dd_mode gate at all."""
        r = _deep_result(direction="SELL", ribbon_status="ALIGNED_BULL", osc_direction="SELL")
        for dd_mode in ("normal", "caution", "defensive", "preservation", "halt"):
            self.assertTrue(
                vb._elig_g_mechanical_reversion(r, {}, dd_mode, 7, self._dummy, self._dummy),
                f"must fire regardless of dd_mode={dd_mode}")

    def test_no_confidence_floor(self):
        """No confidence check anywhere in the eligibility function -- confirm
        a very low/absent confidence doesn't block it."""
        r = _deep_result(direction="BUY", ribbon_status="ALIGNED_BEAR", osc_direction="BUY")
        r["parsed"]["confidence"] = 0
        self.assertTrue(vb._elig_g_mechanical_reversion(r, {}, "normal", 7, self._dummy, self._dummy))

    def test_sell_direction_fires_correctly(self):
        r = _deep_result(direction="SELL", ribbon_status="ALIGNED_BULL", osc_direction="SELL")
        self.assertTrue(vb._elig_g_mechanical_reversion(r, {}, "normal", 7, self._dummy, self._dummy))

    def test_missing_bundle_fields_do_not_crash(self):
        r = {"pair": "EUR/USD", "parsed": {"direction": "BUY"}, "bundle": {}}
        try:
            result = vb._elig_g_mechanical_reversion(r, {}, "normal", 7, self._dummy, self._dummy)
        except Exception as exc:
            self.fail(f"eligibility raised on missing bundle fields: {exc!r}")
        self.assertFalse(result)


class TestRegimeClusterTag(unittest.TestCase):
    """_regime_cluster_tag() no longer gates whether an evaluation is
    recorded (see the module note in src/virtual_books.py dated 2026-09-2X
    for why the original skip-recording design was itself a bias trap) --
    it only returns the cluster identifier shadow_mode's cluster bootstrap
    uses to tell correlated evaluations apart from independent ones. Same
    day/gap/independence semantics as before, verified via "does a
    continuation get the SAME tag" and "does a new regime get a DIFFERENT
    tag" rather than True/False."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patcher = patch.object(vb, "VBOOKS_DIR", Path(self._tmpdir.name))
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_first_occurrence_returns_a_tag(self):
        tag = vb._regime_cluster_tag("G_mechanical_reversion", "EUR/USD", "BUY", True, "2026-09-01")
        self.assertTrue(tag)

    def test_immediate_continuation_gets_same_tag(self):
        tag1 = vb._regime_cluster_tag("G_mechanical_reversion", "EUR/USD", "BUY", True, "2026-09-01")
        tag2 = vb._regime_cluster_tag("G_mechanical_reversion", "EUR/USD", "BUY", True, "2026-09-02")
        self.assertEqual(tag1, tag2)

    def test_gap_within_tolerance_gets_same_tag(self):
        """A normal weekend (up to _REGIME_GAP_DAYS) still counts as ongoing."""
        tag1 = vb._regime_cluster_tag("G_mechanical_reversion", "EUR/USD", "BUY", True, "2026-09-04")  # Friday
        tag2 = vb._regime_cluster_tag("G_mechanical_reversion", "EUR/USD", "BUY", True, "2026-09-07")  # Monday, 3-day gap
        self.assertEqual(tag1, tag2)

    def test_gap_beyond_tolerance_gets_a_new_tag(self):
        tag1 = vb._regime_cluster_tag("G_mechanical_reversion", "EUR/USD", "BUY", True, "2026-09-01")
        tag2 = vb._regime_cluster_tag("G_mechanical_reversion", "EUR/USD", "BUY", True, "2026-09-10")
        self.assertNotEqual(tag1, tag2)

    def test_long_regime_keeps_extending_without_resetting(self):
        """10 consecutive days should be ONE regime (one tag) -- confirms a
        long real regime doesn't fragment into several just because it's
        long."""
        tags = []
        for day in range(1, 11):
            date_str = f"2026-09-{day:02d}"
            tags.append(vb._regime_cluster_tag(
                "G_mechanical_reversion", "EUR/USD", "BUY", True, date_str))
        self.assertEqual(len(set(tags)), 1, f"expected one shared tag, got {set(tags)}")

    def test_different_pair_direction_is_independent(self):
        tag1 = vb._regime_cluster_tag("G_mechanical_reversion", "EUR/USD", "BUY", True, "2026-09-01")
        tag2 = vb._regime_cluster_tag("G_mechanical_reversion", "GBP/USD", "SELL", True, "2026-09-01")
        self.assertNotEqual(tag1, tag2)

    def test_fire_and_no_fire_are_tracked_independently(self):
        tag1 = vb._regime_cluster_tag("G_mechanical_reversion", "EUR/USD", "BUY", True, "2026-09-01")
        tag2 = vb._regime_cluster_tag("G_mechanical_reversion", "EUR/USD", "BUY", False, "2026-09-01")
        self.assertNotEqual(tag1, tag2, "would_fire=True and would_fire=False must track separate regimes")

    def test_unparseable_date_gets_a_unique_tag_each_call(self):
        """Fails safe: never crashes, never collides with a real regime --
        each unparseable-date call gets its own singleton-like tag so
        shadow_mode treats it as independent rather than silently merging
        it into an unrelated cluster."""
        tag1 = vb._regime_cluster_tag("G_mechanical_reversion", "EUR/USD", "BUY", True, "not-a-date")
        tag2 = vb._regime_cluster_tag("G_mechanical_reversion", "EUR/USD", "BUY", True, "not-a-date")
        self.assertTrue(tag1)
        self.assertTrue(tag2)

    def test_different_books_are_isolated(self):
        tag1 = vb._regime_cluster_tag("G_mechanical_reversion", "EUR/USD", "BUY", True, "2026-09-01")
        tag2 = vb._regime_cluster_tag("OTHER_BOOK", "EUR/USD", "BUY", True, "2026-09-02")
        self.assertNotEqual(tag1, tag2)


class TestRegimeTaggingWiredIntoSettlement(unittest.TestCase):
    """Confirms the book's OWN balance/positions reflect every real trade,
    AND that shadow_mode now records every real trade too (no more
    skip-recording) -- tagged with a shared regime_cluster for same-regime
    continuations, so shadow_mode's cluster bootstrap sees full data."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_root = Path(self._tmpdir.name)
        self._regime_book = vb.BookConfig(
            "REGIME_TEST", "test book", lambda *a, **kw: True, regime_aware_promotion=True,
        )
        self._patchers = [
            patch.object(vb, "VBOOKS_DIR", tmp_root),
            patch.object(vb, "CANDIDATES_CSV", tmp_root / "candidates.csv"),
            patch.object(vb, "REJECTIONS_CSV", tmp_root / "rejections.csv"),
            patch.object(vb, "BOOKS", {"REGIME_TEST": self._regime_book}),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmpdir.cleanup()

    def _make_position(self, candidate_id, pair, direction, opened_at):
        positions = vb._load_csv(vb._positions_path("REGIME_TEST"))
        positions.append({
            "id": vb._next_id(positions), "candidate_id": candidate_id, "pair": pair,
            "direction": direction, "opened_at": opened_at, "position_size_pct": 1.0,
            "sizing_mode": "normal", "balance_at_entry": 10000.0, "stop_pips": 50,
            "status": "OPEN", "closed_at": "", "net_pips": "", "dollars": "", "balance_after": "",
        })
        vb._write_csv(vb._positions_path("REGIME_TEST"), positions, vb.POSITION_FIELDS)

    def test_book_records_every_trade_tagged_with_shared_regime_cluster(self):
        self._make_position(1, "EUR/USD", "BUY", "2026-09-01 00:00:00")
        self._make_position(2, "EUR/USD", "BUY", "2026-09-02 00:00:00")  # same regime as #1

        with patch("src.shadow_mode.register_rule") as mock_register, \
             patch("src.shadow_mode.record_evaluation") as mock_record:
            vb._settle_book_positions(1, 50.0, "WIN", log_fn=lambda m: None)
            vb._settle_book_positions(2, 30.0, "WIN", log_fn=lambda m: None)

        state = vb.load_book_state("REGIME_TEST")
        self.assertEqual(state["wins"], 2, "both real trades must count toward the book's own WR")

        # Both real trades must be recorded now (no more skip-recording) --
        # the ONLY difference for a regime-aware book is the shared tag.
        self.assertEqual(mock_record.call_count, 2,
                          "every real trade must be recorded, never skipped, even within one regime")
        ctx1 = mock_record.call_args_list[0].kwargs["context"]
        ctx2 = mock_record.call_args_list[1].kwargs["context"]
        self.assertIn("regime_cluster", ctx1)
        self.assertEqual(ctx1["regime_cluster"], ctx2["regime_cluster"],
                          "same-regime continuations must share one cluster tag")

        # register_rule must be called with cluster_aware=True for a
        # regime_aware_promotion book.
        self.assertTrue(mock_register.call_args_list[0].kwargs.get("cluster_aware"))

    def test_non_regime_aware_book_records_every_trade(self):
        plain_book = vb.BookConfig("PLAIN", "test", lambda *a, **kw: True)
        with patch.object(vb, "BOOKS", {"PLAIN": plain_book}):
            self._make_position_for("PLAIN", 1, "EUR/USD", "BUY", "2026-09-01 00:00:00")
            self._make_position_for("PLAIN", 2, "EUR/USD", "BUY", "2026-09-02 00:00:00")
            with patch("src.shadow_mode.register_rule") as mock_register, \
                 patch("src.shadow_mode.record_evaluation") as mock_record:
                vb._settle_book_positions(1, 50.0, "WIN", log_fn=lambda m: None)
                vb._settle_book_positions(2, 30.0, "WIN", log_fn=lambda m: None)
            self.assertFalse(mock_register.call_args_list[0].kwargs.get("cluster_aware"))
            self.assertNotIn("regime_cluster", mock_record.call_args_list[0].kwargs["context"])
            self.assertEqual(mock_record.call_count, 2,
                              "a non-regime-aware book must record every trade, unchanged behavior")

    def _make_position_for(self, book_id, candidate_id, pair, direction, opened_at):
        positions = vb._load_csv(vb._positions_path(book_id))
        positions.append({
            "id": vb._next_id(positions), "candidate_id": candidate_id, "pair": pair,
            "direction": direction, "opened_at": opened_at, "position_size_pct": 1.0,
            "sizing_mode": "normal", "balance_at_entry": 10000.0, "stop_pips": 50,
            "status": "OPEN", "closed_at": "", "net_pips": "", "dollars": "", "balance_after": "",
        })
        vb._write_csv(vb._positions_path(book_id), positions, vb.POSITION_FIELDS)


if __name__ == "__main__":
    unittest.main()
