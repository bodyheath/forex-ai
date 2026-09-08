"""Tests for the 2026-09-08 daily-close sanity check in src/technical.py.

Real incident: fund trade #6987 (USD/JPY) was entered at 156.197, sourced
from technical.analyse()'s daily.last_close -- itself sourced from Yahoo
Finance's own daily ("1d") bar, which disagreed with Yahoo's OWN hourly
bars for the same trading day by ~290 pips (1.875% of price). The scan's
own scan_price_snapshot.json (written earlier the same run, before the
deep per-pair analysis loop) recorded the correct live price (153.322) at
that exact moment -- proof accurate data existed in the same pipeline
execution but nothing cross-checked the two.

_daily_close_sanity_check() compares a candidate daily close against this
scan's freshest live price (scan_price_snapshot.json) and, on a confirmed
disagreement beyond a real-data-calibrated threshold (see
_DAILY_CLOSE_SANITY_PCT's comment -- a 12-pair, ~4,950-day historical
sweep), causes analyse() to treat the WHOLE Daily timeframe as
unavailable, not just last_close -- RSI/MACD/Bollinger/patterns/ribbon/
tech_signal all derive from the same daily frame and are equally suspect.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import config
from src import technical


def _snapshot(tmp_path: Path, prices: dict, age_hours: float = 0.1) -> None:
    payload = {"timestamp": time.time() - age_hours * 3600, "prices": prices}
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")


class TestFreshScanPrice(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmpdir.name)
        self._patcher = patch.object(config, "DATA_DIR", self._data_dir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_returns_none_when_snapshot_missing(self):
        self.assertIsNone(technical._fresh_scan_price("USD/JPY"))

    def test_returns_none_when_snapshot_too_old(self):
        _snapshot(self._data_dir / "scan_price_snapshot.json",
                  {"USD/JPY": 153.322006}, age_hours=7.0)
        self.assertIsNone(technical._fresh_scan_price("USD/JPY"))

    def test_returns_none_when_pair_not_in_snapshot(self):
        _snapshot(self._data_dir / "scan_price_snapshot.json", {"EUR/USD": 1.1})
        self.assertIsNone(technical._fresh_scan_price("USD/JPY"))

    def test_returns_price_when_fresh_and_present(self):
        _snapshot(self._data_dir / "scan_price_snapshot.json",
                  {"USD/JPY": 153.322006})
        self.assertEqual(technical._fresh_scan_price("USD/JPY"), 153.322006)


class TestDailyCloseSanityCheck(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmpdir.name)
        self._patcher = patch.object(config, "DATA_DIR", self._data_dir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_ok_when_no_fresh_price_to_check(self):
        ok, reason = technical._daily_close_sanity_check("USD/JPY", 156.197)
        self.assertTrue(ok)

    def test_ok_when_within_tolerance(self):
        _snapshot(self._data_dir / "scan_price_snapshot.json",
                  {"USD/JPY": 156.0})  # ~0.13% away
        ok, reason = technical._daily_close_sanity_check("USD/JPY", 156.197)
        self.assertTrue(ok)

    def test_rejects_the_real_6987_gap(self):
        _snapshot(self._data_dir / "scan_price_snapshot.json",
                  {"USD/JPY": 153.322006})  # the real scan-time snapshot price
        ok, reason = technical._daily_close_sanity_check("USD/JPY", 156.1970)
        self.assertFalse(ok)
        self.assertIn("disagreement", reason)

    def test_boundary_just_under_threshold_passes(self):
        live = 100.0
        # 1.49% away -- just under the 1.5% threshold
        candidate = live * 1.0149
        _snapshot(self._data_dir / "scan_price_snapshot.json", {"EUR/USD": live})
        ok, _ = technical._daily_close_sanity_check("EUR/USD", candidate)
        self.assertTrue(ok)

    def test_boundary_just_over_threshold_fails(self):
        live = 100.0
        candidate = live * 1.0151  # 1.51% away -- just over threshold
        _snapshot(self._data_dir / "scan_price_snapshot.json", {"EUR/USD": live})
        ok, _ = technical._daily_close_sanity_check("EUR/USD", candidate)
        self.assertFalse(ok)


def _td_payload(n=60, start_price=150.0):
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    values = []
    price = start_price
    for d in reversed(dates):  # Twelve Data returns newest-first
        values.append({
            "datetime": d.strftime("%Y-%m-%d"),
            "open": str(price), "high": str(price + 0.2),
            "low": str(price - 0.2), "close": str(price),
        })
        price += 0.01
    return {"status": "ok", "values": values}


class TestAnalyseGatesWholeDailyTimeframe(unittest.TestCase):
    """End-to-end: analyse() itself, with real network calls replaced by
    canned data, correctly swaps in an "insufficient data" Daily block
    (not just a corrected last_close) when the sanity check fails."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmpdir.name)
        self._patchers = [
            patch.object(config, "DATA_DIR", self._data_dir),
            patch.object(config, "TWELVE_DATA_KEY", "fake-key-for-test"),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmpdir.cleanup()

    def test_anomalous_daily_close_marks_whole_timeframe_unavailable(self):
        # Daily's last real close is 156.197 (matches the #6987 report
        # exactly); the scan's own live snapshot says 153.322 -- the real gap.
        daily_payload = _td_payload(n=60, start_price=155.6)
        daily_payload["values"][0]["close"] = "156.197"   # newest row = last close
        daily_payload["values"][0]["datetime"] = "2026-09-07"

        def fake_td_request(symbol, interval, outputsize, _log=None):
            if interval == "1day":
                return daily_payload
            return _td_payload(n=max(outputsize, 60), start_price=1.1)

        _snapshot(self._data_dir / "scan_price_snapshot.json",
                  {"USD/JPY": 153.322006})

        with patch.object(technical, "_td_request", side_effect=fake_td_request):
            result = technical.analyse("USD", "JPY")

        self.assertEqual(result["status"], "ok")
        daily = result["daily"]
        self.assertEqual(daily["status"], "insufficient data")
        self.assertNotIn("last_close", daily)
        self.assertNotIn("rsi14", daily)
        self.assertIn("anomaly", daily)

    def test_agreeing_daily_close_passes_through_normally(self):
        daily_payload = _td_payload(n=60, start_price=153.0)
        daily_payload["values"][0]["close"] = "153.35"
        daily_payload["values"][0]["datetime"] = "2026-09-07"

        def fake_td_request(symbol, interval, outputsize, _log=None):
            if interval == "1day":
                return daily_payload
            return _td_payload(n=max(outputsize, 60), start_price=1.1)

        _snapshot(self._data_dir / "scan_price_snapshot.json",
                  {"USD/JPY": 153.322006})  # ~0.02% away -- well within tolerance

        with patch.object(technical, "_td_request", side_effect=fake_td_request):
            result = technical.analyse("USD", "JPY")

        self.assertEqual(result["daily"]["status"], "ok"[:0] or result["daily"].get("timeframe"), "Daily")
        self.assertIn("last_close", result["daily"])
        self.assertIn("rsi14", result["daily"])


if __name__ == "__main__":
    unittest.main()
