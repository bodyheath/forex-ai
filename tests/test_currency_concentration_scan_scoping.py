"""Regression test for the same-scan phantom-OPEN bug in
tracker.check_currency_concentration() / check_inverse_open().

Bug: log_recommendation() writes status=OPEN to trades.csv the instant a
candidate's trade_this is YES — before Devil's Advocate, drawdown-tier,
not-viable, or demoted-threshold corrections run later in the same scan.
Those corrections write back to SKIPPED if the candidate doesn't survive,
but any other same-currency pair evaluated earlier in that same scan's
per-pair loop, before its correction runs, would see the still-OPEN row on
disk and get wrongly blocked — even though nothing was ever genuinely open.
Confirmed on trades.csv: 3/3 historical concentration blocks were phantom.

Fix: check_currency_concentration()/check_inverse_open() take an optional
max_id — the highest trade id that existed before the current scan started.
Rows with id > max_id were written during this same scan and haven't been
through the correction pass yet, so they no longer count as "already open".
Genuinely pre-existing open trades keep counting correctly because
log_recommendation()'s re-analysis path never reassigns a row's id (even
though it does refresh its timestamp) — this test confirms that
specifically, since a naive timestamp-based scoping fix would have broken it.
"""
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from src import tracker

_FIELDS = ["id", "timestamp", "pair", "direction", "status"]


class TestCurrencyConcentrationScanScoping(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._csv_path = Path(self._tmpdir.name) / "trades.csv"
        self._patcher = patch.object(config, "TRADES_CSV", self._csv_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def _write_rows(self, rows: list) -> None:
        with self._csv_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in _FIELDS})

    # ── check_currency_concentration ────────────────────────────────────────

    def test_same_scan_phantom_open_no_longer_blocks_with_max_id(self):
        """id=100 CAD/CHF is this scan's own not-yet-corrected phantom OPEN
        (baseline was 99, captured before this scan started). GBP/CHF's
        concentration check, scoped to that baseline, must not see it."""
        self._write_rows([
            {"id": 100, "timestamp": "2026-07-28 18:18:31", "pair": "CAD/CHF",
             "direction": "SELL", "status": "OPEN"},
        ])
        warn = tracker.check_currency_concentration("GBP/CHF", "SELL", max_id=99)
        self.assertIsNone(warn, f"expected no block, got: {warn!r}")

    def test_same_scenario_without_max_id_reproduces_the_original_bug(self):
        """Same data, no max_id (old call signature / old behaviour) — proves
        this is the exact bug that fired 3/3 times in production, not a
        hypothetical."""
        self._write_rows([
            {"id": 100, "timestamp": "2026-07-28 18:18:31", "pair": "CAD/CHF",
             "direction": "SELL", "status": "OPEN"},
        ])
        warn = tracker.check_currency_concentration("GBP/CHF", "SELL")
        self.assertIsNotNone(warn, "expected the pre-fix code path to still phantom-block")
        self.assertIn("CHF already appears in 1 open fund trade", warn)

    def test_genuinely_preexisting_open_trade_still_blocks(self):
        """id=50 EUR/CHF predates the scan baseline (99) — a real, confirmed
        open position. It must still block USD/CHF."""
        self._write_rows([
            {"id": 50, "timestamp": "2026-07-20 09:00:00", "pair": "EUR/CHF",
             "direction": "SELL", "status": "OPEN"},
        ])
        warn = tracker.check_currency_concentration("USD/CHF", "SELL", max_id=99)
        self.assertIsNotNone(warn, "a genuinely pre-existing OPEN trade must still block")
        self.assertIn("CHF already appears in 1 open fund trade", warn)

    def test_reanalysed_preexisting_trade_still_blocks_despite_timestamp_bump(self):
        """id=60 NZD/CHF predates the baseline but was re-analysed THIS scan
        (log_recommendation's dedup path bumps timestamp on re-analysis
        without reassigning id). A timestamp-based scoping fix would wrongly
        exclude this; the id-based fix must not."""
        self._write_rows([
            {"id": 60, "timestamp": "2026-07-28 18:19:59", "pair": "NZD/CHF",
             "direction": "SELL", "status": "OPEN"},
        ])
        warn = tracker.check_currency_concentration("USD/CHF", "SELL", max_id=99)
        self.assertIsNotNone(warn, "re-analysis timestamp refresh must not hide a real open trade")

    # ── check_inverse_open ──────────────────────────────────────────────────

    def test_inverse_open_same_scan_phantom_no_longer_blocks(self):
        """id=100 AUD/CAD BUY is this scan's own not-yet-corrected phantom.
        CAD/AUD SELL (the inverse) must not be blocked by it once scoped."""
        self._write_rows([
            {"id": 100, "timestamp": "2026-07-28 18:18:31", "pair": "AUD/CAD",
             "direction": "BUY", "status": "OPEN"},
        ])
        warn = tracker.check_inverse_open("CAD/AUD", "SELL", max_id=99)
        self.assertIsNone(warn, f"expected no block, got: {warn!r}")

    def test_inverse_open_genuinely_preexisting_still_blocks(self):
        self._write_rows([
            {"id": 50, "timestamp": "2026-07-20 09:00:00", "pair": "AUD/CAD",
             "direction": "BUY", "status": "OPEN"},
        ])
        warn = tracker.check_inverse_open("CAD/AUD", "SELL", max_id=99)
        self.assertIsNotNone(warn, "a genuinely pre-existing inverse OPEN trade must still block")


if __name__ == "__main__":
    unittest.main()
