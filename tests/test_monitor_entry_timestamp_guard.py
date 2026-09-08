"""Tests for the 2026-09-08 entry-timestamp guard in
src/monitor.py::_detect_candle_milestones().

Real incident: fund trade #6987 (USD/JPY, real entry 2026-09-08 05:15:56
UTC) was closed as a stop-loss hit on the very next monitor cycle using a
candle dated "2026-09-08 00:00" (Yahoo 1H, Europe/London local) -- which
converts to 2026-09-07 23:00 UTC, a full 5+ hours BEFORE the real entry.
fetch_1h_candles() always returns "the last N completed hourly bars as of
now" with no awareness of any specific trade's entry time, so a freshly
opened trade checked on the very next cycle will, by construction, always
be checked against pre-entry candles unless guarded explicitly -- there
hasn't been enough real time for even one new post-entry hourly bar to
close yet.
"""
import unittest

from src import monitor


def _candle(dt: str, high: float, low: float) -> dict:
    return {"datetime": dt, "high": high, "low": low}


class TestCandleWindowEndUtc(unittest.TestCase):

    def test_london_bst_candle_converts_correctly(self):
        # "2026-09-08 00:00" London (BST, UTC+1) -> starts 2026-09-07 23:00 UTC
        # -> window end (start + 1h) = 2026-09-08 00:00 UTC.
        end = monitor._candle_window_end_utc("2026-09-08 00:00")
        self.assertEqual(end.strftime("%Y-%m-%d %H:%M"), "2026-09-08 00:00")

    def test_unparseable_returns_none(self):
        self.assertIsNone(monitor._candle_window_end_utc("not a date"))
        self.assertIsNone(monitor._candle_window_end_utc(""))


class TestEntryUtc(unittest.TestCase):

    def test_parses_real_trades_csv_format(self):
        ts = monitor._entry_utc({"timestamp": "2026-09-08 05:15:56"})
        self.assertEqual(ts.strftime("%Y-%m-%d %H:%M:%S"), "2026-09-08 05:15:56")

    def test_missing_timestamp_returns_none(self):
        self.assertIsNone(monitor._entry_utc({}))
        self.assertIsNone(monitor._entry_utc({"timestamp": ""}))


class TestDetectCandleMilestonesEntryGuard(unittest.TestCase):

    def _row(self, **overrides):
        row = {
            "id": 6987, "pair": "USD/JPY", "direction": "BUY",
            "entry": 156.197, "stop_loss": 154.6068, "t2_price": 159.3774,
            "timestamp": "2026-09-08 05:15:56",
            "t2_hit": "", "t1_hit": "",
        }
        row.update(overrides)
        return row

    def test_the_real_6987_case_no_longer_fires_on_pre_entry_candle(self):
        # The exact real candle from the incident: dated before real entry,
        # LOW already below stop_loss. Only this one pre-entry candle exists.
        candles = [_candle("2026-09-08 00:00", high=154.167, low=153.791)]
        milestones, _ = monitor._detect_candle_milestones(
            self._row(), candles, "USD/JPY", log=lambda m: None,
        )
        self.assertEqual(milestones, [])

    def test_a_genuine_post_entry_candle_still_fires_normally(self):
        # Same stop level, but this candle's window is AFTER real entry --
        # the guard must not suppress a real, legitimate post-entry stop hit.
        candles = [_candle("2026-09-08 08:00", high=154.9, low=154.0)]
        milestones, _ = monitor._detect_candle_milestones(
            self._row(), candles, "USD/JPY", log=lambda m: None,
        )
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0]["level"], "STOP")

    def test_mixed_candles_only_pre_entry_ones_are_skipped(self):
        # Oldest candle is pre-entry (would have fired if not guarded);
        # newest candle is post-entry and genuinely hits stop -- must still fire.
        candles = [
            _candle("2026-09-08 08:00", high=154.9, low=154.0),   # post-entry: real hit
            _candle("2026-09-08 00:00", high=154.167, low=153.791),  # pre-entry: must be skipped
        ]
        milestones, _ = monitor._detect_candle_milestones(
            self._row(), candles, "USD/JPY", log=lambda m: None,
        )
        # Loop processes oldest->newest and breaks on first hit; the
        # pre-entry candle must never be the one credited.
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0]["candle_dt"], "2026-09-08 08:00")

    def test_missing_entry_timestamp_fails_open_unaffected(self):
        # No timestamp on the row -- guard can't apply, old behavior preserved
        # rather than silently dropping every candle.
        candles = [_candle("2026-09-08 00:00", high=154.167, low=153.791)]
        milestones, _ = monitor._detect_candle_milestones(
            self._row(timestamp=""), candles, "USD/JPY", log=lambda m: None,
        )
        self.assertEqual(len(milestones), 1)


if __name__ == "__main__":
    unittest.main()
