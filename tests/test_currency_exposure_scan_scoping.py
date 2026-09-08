"""Regression test for the same-scan phantom-OPEN bug in
fund_state.check_currency_exposure()'s caller (daily.py's
_send_telegram_summary()) -- the sibling bug to the one already fixed for
tracker.check_currency_concentration()/check_inverse_open() (see
tests/test_currency_concentration_scan_scoping.py), just via a different
code path that never got the same fix.

Real incident (2026-09-08 investigation): 5 separate intraday scans on
2026-09-07 each blocked a real conf=7+ EUR/JPY AND USD/JPY candidate with
"Currency concentration -- JPY at 2 open trades", even though git history
of data/trades.csv confirms zero real JPY exposure at every one of those
exact moments (the only real open position across all 5 snapshots that day
was one unrelated AUD/NZD trade). Root cause: tracker.log_recommendation()
writes status=OPEN to trades.csv the instant a candidate's raw trade_this
is YES -- before the fund-loop's drawdown-tier/capacity/concentration
corrections run later in the same _send_telegram_summary() call. Both
EUR/JPY and USD/JPY independently said trade_this=YES from Sonnet, both got
raw-written OPEN moments apart, and _ot_open_trades's un-filtered snapshot
(taken after all of this scan's own analysis had already run) counted both
against each other.

Fix: _send_telegram_summary() takes an optional scan_baseline_max_id (the
same value already passed to every _analyse_pair() call this scan as
max_open_id) and filters _ot_open_trades through the new
_filter_rows_before_scan() helper -- same max_id-ceiling pattern the
tracker.py functions already use, extracted here so it's testable without
invoking _send_telegram_summary()'s real Telegram/Discord side effects.
"""
import os
import unittest

os.environ.setdefault("ALLOW_LOCAL_RUN", "YES")

import daily


class TestFilterRowsBeforeScan(unittest.TestCase):

    def _rows(self):
        return [
            {"id": "100", "pair": "AUD/NZD", "status": "OPEN"},   # genuinely pre-existing
            {"id": "6857", "pair": "EUR/JPY", "status": "OPEN"},  # written by THIS scan
            {"id": "6858", "pair": "USD/JPY", "status": "OPEN"},  # written by THIS scan
        ]

    def test_none_max_id_returns_rows_unchanged(self):
        rows = self._rows()
        result = daily._filter_rows_before_scan(rows, None)
        self.assertEqual(result, rows)

    def test_excludes_rows_written_this_scan(self):
        # Reproduces the real 2026-09-07 scenario: baseline id 6800 (the
        # highest id that existed before this scan started), EUR/JPY (6857)
        # and USD/JPY (6858) were both written AFTER that by this scan's own
        # analysis and must not count as pre-existing exposure.
        result = daily._filter_rows_before_scan(self._rows(), max_id=6800)
        pairs = [r["pair"] for r in result]
        self.assertEqual(pairs, ["AUD/NZD"])
        self.assertNotIn("EUR/JPY", pairs)
        self.assertNotIn("USD/JPY", pairs)

    def test_genuinely_pre_existing_row_still_counts(self):
        # A real open trade from BEFORE this scan (id <= baseline) must
        # still be counted -- the fix must not under-count real exposure.
        result = daily._filter_rows_before_scan(self._rows(), max_id=6900)
        pairs = {r["pair"] for r in result}
        self.assertEqual(pairs, {"AUD/NZD", "EUR/JPY", "USD/JPY"})

    def test_malformed_id_excluded_not_crashed(self):
        rows = [{"id": "not-a-number", "pair": "GBP/USD", "status": "OPEN"}]
        result = daily._filter_rows_before_scan(rows, max_id=100)
        self.assertEqual(result, [])

    def test_missing_id_excluded(self):
        rows = [{"pair": "GBP/USD", "status": "OPEN"}]
        # {}.get("id", 0) -> 0, which is <= any real max_id, so a row with no
        # id at all is treated as pre-existing (matches tracker.py's own
        # int(r.get("id", 0)) <= max_id fallback -- documented consistency,
        # not a new decision made here).
        result = daily._filter_rows_before_scan(rows, max_id=100)
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
