"""Tests for src/correlation_model.py and its wiring into
risk_manager.apply_correlation_checks() (2026-09-07 correlation-clustering
replacement -- see src/correlation_model.py's module docstring for the
investigation and backtest this is based on).

No network calls: are_correlated()/get_correlation() are tested against a
matrix injected directly into the module's in-process cache, never against
a real fetch. refresh_correlation_matrix()'s own network path (_fetch_closes,
yfinance) is mocked out; only its staleness-alerting behaviour is exercised
here (2026-09-07, added after an audit found the refresh had no failure-path
alerting at all -- a stale matrix with nobody alerted is worse than no
correlation check, since it gives false confidence the real-correlation gate
is protecting sizing when it has silently fallen back to the literal check).
"""
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src import correlation_model
from src import risk_manager


class TestCorrelationModel(unittest.TestCase):

    def setUp(self):
        # Reset the in-process cache so tests don't leak state into each other.
        correlation_model._matrix_cache = None
        self._fake_matrix = {
            "EUR/USD": {"GBP/USD": 0.85, "EUR/AUD": 0.01},
            "NZD/USD": {"AUD/CAD": 0.66},
        }

    def tearDown(self):
        correlation_model._matrix_cache = None

    def _inject(self):
        correlation_model._matrix_cache = self._fake_matrix

    def test_get_correlation_direct_lookup(self):
        self._inject()
        self.assertEqual(correlation_model.get_correlation("EUR/USD", "GBP/USD"), 0.85)

    def test_get_correlation_reverse_lookup(self):
        self._inject()
        # Matrix only stores one triangle -- reverse order must still resolve.
        self.assertEqual(correlation_model.get_correlation("GBP/USD", "EUR/USD"), 0.85)

    def test_get_correlation_same_pair_is_one(self):
        self.assertEqual(correlation_model.get_correlation("EUR/USD", "EUR/USD"), 1.0)

    def test_get_correlation_missing_returns_none(self):
        self._inject()
        self.assertIsNone(correlation_model.get_correlation("EUR/USD", "USD/CHF"))

    def test_get_correlation_no_matrix_returns_none(self):
        with patch.object(correlation_model, "MATRIX_PATH") as mock_path:
            mock_path.exists.return_value = False
            self.assertIsNone(correlation_model.get_correlation("EUR/USD", "GBP/USD"))

    def test_are_correlated_both_buy_high_corr_flags(self):
        self._inject()
        self.assertTrue(correlation_model.are_correlated("EUR/USD", "BUY", "GBP/USD", "BUY"))

    def test_are_correlated_shared_currency_low_real_corr_does_not_flag(self):
        # This is the exact false-positive case the old literal check made:
        # EUR/USD and EUR/AUD share the EUR code but have ~zero real corr.
        self._inject()
        self.assertFalse(correlation_model.are_correlated("EUR/USD", "BUY", "EUR/AUD", "BUY"))

    def test_are_correlated_no_shared_currency_high_real_corr_flags(self):
        # NZD/USD + AUD/CAD share no currency code at all -- the literal
        # check structurally cannot catch this; real correlation does.
        self._inject()
        self.assertTrue(correlation_model.are_correlated("NZD/USD", "BUY", "AUD/CAD", "BUY"))

    def test_are_correlated_opposite_directions_flips_sign(self):
        self._inject()
        # BUY EUR/USD + SELL GBP/USD: a EUR/USD rally and GBP/USD sell-off are
        # NOT reinforcing (their raw returns are positively correlated, but
        # the positions bet on opposite directions), so this should not flag.
        self.assertFalse(correlation_model.are_correlated("EUR/USD", "BUY", "GBP/USD", "SELL"))
        # BUY EUR/USD + SELL EUR/AUD would need real corr <= -0.60 to flag;
        # at +0.01 it doesn't.
        self.assertFalse(correlation_model.are_correlated("EUR/USD", "BUY", "EUR/AUD", "SELL"))

    def test_are_correlated_unavailable_pair_returns_none(self):
        self._inject()
        self.assertIsNone(correlation_model.are_correlated("USD/CHF", "BUY", "CAD/CHF", "BUY"))


class TestRiskManagerFallback(unittest.TestCase):
    """apply_correlation_checks() must fall back to the literal currency-code
    check when the real correlation model has no data -- a risk gate should
    fail toward more caution, never silently skip the check."""

    def setUp(self):
        correlation_model._matrix_cache = None

    def tearDown(self):
        correlation_model._matrix_cache = None

    def _trade(self, pair, direction):
        return {
            "pair": pair, "direction": direction,
            "lots": 1.0, "risk_amount": 100.0, "risk_pct": 1.0,
        }

    def test_falls_back_to_literal_check_when_model_unavailable(self):
        # No matrix injected -> correlation_model.are_correlated() returns
        # None for every pair -> risk_manager must use the old literal check,
        # which flags EUR/USD + GBP/USD (both short USD on a BUY/BUY).
        trades = [self._trade("EUR/USD", "BUY"), self._trade("GBP/USD", "BUY")]
        result = risk_manager.apply_correlation_checks(trades)
        self.assertTrue(result[0]["correlated"])
        self.assertTrue(result[1]["correlated"])
        self.assertEqual(result[0]["lots"], 0.5)

    def test_uses_real_model_when_available(self):
        correlation_model._matrix_cache = {"NZD/USD": {"AUD/CAD": 0.66}}
        # No shared currency code -- literal check would say NOT correlated,
        # but real correlation (0.66) says it is.
        trades = [self._trade("NZD/USD", "BUY"), self._trade("AUD/CAD", "BUY")]
        result = risk_manager.apply_correlation_checks(trades)
        self.assertTrue(result[0]["correlated"])
        self.assertTrue(result[1]["correlated"])

    def test_real_model_overrides_literal_false_positive(self):
        correlation_model._matrix_cache = {"EUR/USD": {"EUR/AUD": 0.01}}
        # Literal check would flag this (shared EUR code); real correlation
        # (0.01) says these do not move together and should NOT be halved.
        trades = [self._trade("EUR/USD", "BUY"), self._trade("EUR/AUD", "BUY")]
        result = risk_manager.apply_correlation_checks(trades)
        self.assertFalse(result[0].get("correlated", False))
        self.assertFalse(result[1].get("correlated", False))


class TestRefreshStaleAlerting(unittest.TestCase):
    """refresh_correlation_matrix() must alert (Discord + Telegram) when a
    refresh attempt fails AND the on-disk matrix is already past
    STALE_ALERT_DAYS -- but NOT for a single transient failure while the
    existing matrix is still fresh enough to trust."""

    def setUp(self):
        correlation_model._matrix_cache = None
        self._tmpdir_patcher = patch.object(
            correlation_model, "MATRIX_PATH", Path("data/_test_correlation_matrix.json")
        )
        self._tmpdir_patcher.start()
        if correlation_model.MATRIX_PATH.exists():
            correlation_model.MATRIX_PATH.unlink()
        # Force _fetch_closes() to always "fail" (too few pairs) without any
        # network access.
        self._fetch_patcher = patch.object(
            correlation_model, "_fetch_closes",
            return_value=__import__("pandas").DataFrame({"EUR/USD": [1.0] * 5}),
        )
        self._fetch_patcher.start()

    def tearDown(self):
        self._fetch_patcher.stop()
        self._tmpdir_patcher.stop()
        if correlation_model.MATRIX_PATH.exists():
            correlation_model.MATRIX_PATH.unlink()
        correlation_model._matrix_cache = None

    def _write_matrix(self, age_days: float):
        correlation_model.MATRIX_PATH.write_text(json.dumps({
            "computed_at": time.time() - age_days * 86400.0,
            "matrix": {},
        }), encoding="utf-8")

    @patch("src.discord_notifier.send_correlation_stale_alert")
    @patch("src.telegram_alert.send")
    def test_no_matrix_at_all_alerts_immediately(self, mock_tg, mock_discord):
        # No prior matrix -- there's nothing to fall back on except the
        # literal check, and that's exactly the "false confidence" case.
        correlation_model.refresh_correlation_matrix(log=lambda *a: None)
        mock_discord.assert_called_once()
        mock_tg.assert_called_once()

    @patch("src.discord_notifier.send_correlation_stale_alert")
    @patch("src.telegram_alert.send")
    def test_fresh_matrix_transient_failure_does_not_alert(self, mock_tg, mock_discord):
        self._write_matrix(age_days=3)   # well under STALE_ALERT_DAYS (14)
        correlation_model.refresh_correlation_matrix(force=True, log=lambda *a: None)
        mock_discord.assert_not_called()
        mock_tg.assert_not_called()

    @patch("src.discord_notifier.send_correlation_stale_alert")
    @patch("src.telegram_alert.send")
    def test_matrix_past_stale_alert_threshold_alerts(self, mock_tg, mock_discord):
        self._write_matrix(age_days=correlation_model.STALE_ALERT_DAYS + 1)
        correlation_model.refresh_correlation_matrix(force=True, log=lambda *a: None)
        mock_discord.assert_called_once()
        mock_tg.assert_called_once()

    @patch("src.discord_notifier.send_correlation_stale_alert")
    @patch("src.telegram_alert.send")
    def test_successful_refresh_never_alerts(self, mock_tg, mock_discord):
        import pandas as pd
        # 250 days of two fully-correlated series across every UNIVERSE pair
        # -- enough rows to clear min_periods=200 and enough columns to clear
        # MIN_PAIRS_FOR_VALID_REFRESH.
        from src.selector import UNIVERSE
        n = 250
        data = {pair: [1.0 + 0.001 * i for i in range(n)] for pair in UNIVERSE}
        self._fetch_patcher.stop()
        with patch.object(correlation_model, "_fetch_closes", return_value=pd.DataFrame(data)):
            correlation_model.refresh_correlation_matrix(force=True, log=lambda *a: None)
        self._fetch_patcher.start()   # tearDown expects this to still be running
        mock_discord.assert_not_called()
        mock_tg.assert_not_called()
        self.assertTrue(correlation_model.MATRIX_PATH.exists())


if __name__ == "__main__":
    unittest.main()
