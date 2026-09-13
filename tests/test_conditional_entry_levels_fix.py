"""Tests for the 2026-09-13 conditional-entry ENTRY/STOP_LOSS/TARGET fix.

Real incident: 17 of 229 real fund YES candidates (7%) were discarded as
SKIPPED because a conditional ENTRY_TYPE (BREAKOUT_*/LIMIT_*/PULLBACK)
response from Sonnet omitted ENTRY/STOP_LOSS/TARGET entirely -- confirmed
directly against the raw saved reports (e.g. data/reports/1709_EURUSD.txt,
2034_EURGBP.txt): the fields are genuinely absent, not malformed or
differently-named, and every IMMEDIATE-type candidate in the same
population reliably includes them. Root cause was the prompt
(src/analyst.py's _build_sonnet_message), not parsing.

Fixes covered here:
  1. src/analyst.py: strengthened prompt instruction + a one-shot retry
     backstop in analyse() when a conditional-type TRADE_THIS: YES response
     still lacks ENTRY/STOP_LOSS/TARGET.
  2. daily.py: CONDITIONAL_ENTRY_LIVE safety gate. Fixing the data gap makes
     the existing conditional-entry -> PENDING pathway reachable for the
     first time in the fund's history -- which would start creating real
     PENDING orders as a side effect of a data fix. Default OFF: records
     what would have been used to data/conditional_entry_shadow.json,
     touches no real trade row.
  3. daily.py: _check_conditional_entry_plausibility(). Requiring ENTRY/
     STOP_LOSS/TARGET gives the model a new reason to pad a plausible-
     looking number just to satisfy the requirement even when it isn't
     actually confident in a level. Three independent checks recorded on
     every shadow row -- sign/direction, distance vs. this pair's own real
     IMMEDIATE-trade history (None when there isn't enough of it yet, not a
     fail), and internal entry/stop/target vs. stated REWARD_RISK_RATIO
     consistency -- so a genuinely usable setup can be told apart from a
     padded one without re-deriving it from the raw numbers later.

No real API calls anywhere in this file: _call_api() is mocked directly.
"""
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tempfile

from src import analyst


def _fake_resp(text: str, stop_reason: str = "end_turn", input_tokens=100, output_tokens=50):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


_CONDITIONAL_MISSING_LEVELS = (
    "PAIR: EUR/USD\nDIRECTION: BUY\nCONFIDENCE: 6\nTECHNICAL_SCORE: 6\n"
    "FUNDAMENTAL_SCORE: 4\nSENTIMENT_SCORE: 6\nPOSITIONING_SCORE: 4\nMACRO_SCORE: 5\n"
    "DIVERGENCE: NONE\nOSCILLATOR_CONFLUENCE: NONE\n"
    "KEY_THESIS: Pin bar reversal setup.\nRISK_FACTORS: Ribbon against.\n"
    "TRADE_THIS: YES\n"
    "ENTRY_TYPE: LIMIT_BUY\nENTRY_TRIGGER_PRICE: 1.1390\n"
    "ENTRY_TRIGGER_REASON: Pin bar low + buffer.\nENTRY_TRIGGER_EXPIRY_HOURS: 24"
)

_CONDITIONAL_WITH_LEVELS = (
    "PAIR: EUR/USD\nDIRECTION: BUY\nCONFIDENCE: 6\nTECHNICAL_SCORE: 6\n"
    "FUNDAMENTAL_SCORE: 4\nSENTIMENT_SCORE: 6\nPOSITIONING_SCORE: 4\nMACRO_SCORE: 5\n"
    "DIVERGENCE: NONE\nOSCILLATOR_CONFLUENCE: NONE\n"
    "ENTRY: 1.1390\nTARGET: 1.1590\nSTOP_LOSS: 1.1290\nREWARD_RISK_RATIO: 2.0:1\n"
    "KEY_THESIS: Pin bar reversal setup.\nRISK_FACTORS: Ribbon against.\n"
    "TRADE_THIS: YES\n"
    "ENTRY_TYPE: LIMIT_BUY\nENTRY_TRIGGER_PRICE: 1.1390\n"
    "ENTRY_TRIGGER_REASON: Pin bar low + buffer.\nENTRY_TRIGGER_EXPIRY_HOURS: 24"
)

_IMMEDIATE_COMPLETE = (
    "PAIR: EUR/USD\nDIRECTION: BUY\nCONFIDENCE: 7\nTECHNICAL_SCORE: 8\n"
    "FUNDAMENTAL_SCORE: 6\nSENTIMENT_SCORE: 6\nPOSITIONING_SCORE: 6\nMACRO_SCORE: 6\n"
    "ENTRY: 1.1390\nTARGET: 1.1590\nSTOP_LOSS: 1.1290\nREWARD_RISK_RATIO: 2.0:1\n"
    "KEY_THESIS: Trend continuation.\nRISK_FACTORS: None material.\n"
    "TRADE_THIS: YES"
)

_TRADE_THIS_NO_CONDITIONAL = (
    "PAIR: EUR/USD\nDIRECTION: BUY\nCONFIDENCE: 4\nTECHNICAL_SCORE: 5\n"
    "FUNDAMENTAL_SCORE: 4\nSENTIMENT_SCORE: 4\nPOSITIONING_SCORE: 4\nMACRO_SCORE: 4\n"
    "KEY_THESIS: Weak setup.\nRISK_FACTORS: Many.\nTRADE_THIS: NO\n"
    "ENTRY_TYPE: LIMIT_BUY\nENTRY_TRIGGER_PRICE: 1.1390\n"
    "ENTRY_TRIGGER_REASON: Weak reversal.\nENTRY_TRIGGER_EXPIRY_HOURS: 24"
)


class TestConditionalEntryRetryBackstop(unittest.TestCase):

    def _bundle(self):
        return {"technical": {"daily": {}}, "mtf": {}}

    @patch.object(analyst, "_call_api")
    def test_retry_fires_and_uses_retry_report_when_it_supplies_levels(self, mock_call):
        mock_call.side_effect = [
            _fake_resp(_CONDITIONAL_MISSING_LEVELS),
            _fake_resp(_CONDITIONAL_WITH_LEVELS),
        ]
        report = analyst.analyse("EUR/USD", self._bundle(), haiku_report="conf=6 BUY")
        self.assertEqual(mock_call.call_count, 2)
        self.assertIn("ENTRY: 1.1390", report)
        self.assertIn("STOP_LOSS: 1.1290", report)

    @patch.object(analyst, "_call_api")
    def test_retry_still_missing_keeps_original_report(self, mock_call):
        mock_call.side_effect = [
            _fake_resp(_CONDITIONAL_MISSING_LEVELS),
            _fake_resp(_CONDITIONAL_MISSING_LEVELS),  # retry also fails to supply them
        ]
        report = analyst.analyse("EUR/USD", self._bundle(), haiku_report="conf=6 BUY")
        self.assertEqual(mock_call.call_count, 2)
        self.assertNotIn("ENTRY:", report)  # unchanged from the original, still incomplete

    @patch.object(analyst, "_call_api")
    def test_retry_exception_keeps_original_report_no_crash(self, mock_call):
        mock_call.side_effect = [_fake_resp(_CONDITIONAL_MISSING_LEVELS), RuntimeError("network blip")]
        try:
            report = analyst.analyse("EUR/USD", self._bundle(), haiku_report="conf=6 BUY")
        except Exception as exc:
            self.fail(f"analyse() must not raise when only the retry attempt fails: {exc!r}")
        self.assertIn("ENTRY_TYPE: LIMIT_BUY", report)

    @patch.object(analyst, "_call_api")
    def test_immediate_complete_response_does_not_trigger_retry(self, mock_call):
        mock_call.return_value = _fake_resp(_IMMEDIATE_COMPLETE)
        report = analyst.analyse("EUR/USD", self._bundle(), haiku_report="conf=7 BUY")
        mock_call.assert_called_once()
        self.assertIn("ENTRY: 1.1390", report)

    @patch.object(analyst, "_call_api")
    def test_conditional_already_complete_does_not_trigger_retry(self, mock_call):
        mock_call.return_value = _fake_resp(_CONDITIONAL_WITH_LEVELS)
        analyst.analyse("EUR/USD", self._bundle(), haiku_report="conf=6 BUY")
        mock_call.assert_called_once()

    @patch.object(analyst, "_call_api")
    def test_trade_this_no_does_not_trigger_retry(self, mock_call):
        mock_call.return_value = _fake_resp(_TRADE_THIS_NO_CONDITIONAL)
        analyst.analyse("EUR/USD", self._bundle(), haiku_report="conf=4 BUY")
        mock_call.assert_called_once()

    def test_prompt_requires_levels_for_conditional_entries(self):
        msg = analyst._build_sonnet_message("EUR/USD", self._bundle(), "conf=6 BUY")
        self.assertIn("REQUIRED", msg)
        self.assertIn("conditional", msg)
        self.assertIn("Never omit ENTRY/STOP_LOSS/TARGET", msg)


class TestConditionalEntryShadowGate(unittest.TestCase):

    def setUp(self):
        import daily
        self.daily = daily

    @patch.dict("os.environ", {}, clear=False)
    def test_gate_defaults_off(self):
        import os as _os
        _os.environ.pop("CONDITIONAL_ENTRY_LIVE", None)
        self.assertFalse(self.daily._conditional_entry_live_enabled())

    @patch.dict("os.environ", {"CONDITIONAL_ENTRY_LIVE": "YES"})
    def test_gate_on_when_explicitly_set(self):
        self.assertTrue(self.daily._conditional_entry_live_enabled())

    @patch.dict("os.environ", {"CONDITIONAL_ENTRY_LIVE": "no"})
    def test_gate_off_for_any_non_yes_value(self):
        self.assertFalse(self.daily._conditional_entry_live_enabled())

    def test_shadow_record_appends_and_persists(self):
        with tempfile.TemporaryDirectory() as td:
            fake_path = Path(td) / "conditional_entry_shadow.json"
            with patch.object(self.daily.config, "DATA_DIR", Path(td)):
                self.daily._record_conditional_entry_shadow({
                    "id": 1, "pair": "EUR/USD", "entry_type": "LIMIT_BUY",
                    "entry": 1.139, "stop_loss": 1.129, "target": 1.159,
                })
                self.daily._record_conditional_entry_shadow({
                    "id": 2, "pair": "GBP/USD", "entry_type": "BREAKOUT_SELL",
                    "entry": 1.25, "stop_loss": 1.26, "target": 1.23,
                })
            data = json.loads(fake_path.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["pair"], "EUR/USD")
        self.assertEqual(data[1]["pair"], "GBP/USD")

    def test_shadow_record_never_raises_on_write_failure(self):
        with patch.object(self.daily.config, "DATA_DIR", Path("Z:/definitely/not/a/real/path")):
            try:
                self.daily._record_conditional_entry_shadow({"id": 1, "pair": "EUR/USD"})
            except Exception as exc:
                self.fail(f"_record_conditional_entry_shadow raised {exc!r} -- must never break the caller")


class TestHistoricalImmediateStopPips(unittest.TestCase):

    def setUp(self):
        import daily
        self.daily = daily

    def _write_research_csv(self, path, rows):
        import pandas as pd
        pd.DataFrame(rows).to_csv(path, index=False)

    def test_returns_none_below_min_n(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            self._write_research_csv(td / "research_trades.csv", [
                {"pair": "EUR/USD", "entry_type": "IMMEDIATE", "entry": 1.10, "stop_loss": 1.09},
                {"pair": "EUR/USD", "entry_type": "", "entry": 1.11, "stop_loss": 1.095},
            ])
            fake_trades = td / "no_trades.csv"  # doesn't exist
            with patch.object(self.daily.config, "DATA_DIR", td), \
                 patch.object(self.daily.config, "TRADES_CSV", fake_trades):
                median, n = self.daily._historical_immediate_stop_pips("EUR/USD", min_n=5)
        self.assertIsNone(median)
        self.assertEqual(n, 2)

    def test_returns_median_when_enough_comparables(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            rows = [
                {"pair": "EUR/USD", "entry_type": "IMMEDIATE", "entry": 1.1000, "stop_loss": 1.0900},  # 100p
                {"pair": "EUR/USD", "entry_type": "IMMEDIATE", "entry": 1.1000, "stop_loss": 1.0900},  # 100p
                {"pair": "EUR/USD", "entry_type": "",          "entry": 1.1000, "stop_loss": 1.0900},  # 100p
                {"pair": "EUR/USD", "entry_type": "IMMEDIATE", "entry": 1.1000, "stop_loss": 1.0800},  # 200p
                {"pair": "EUR/USD", "entry_type": "IMMEDIATE", "entry": 1.1000, "stop_loss": 1.0800},  # 200p
                # Different pair, must not pollute EUR/USD's stats
                {"pair": "GBP/USD", "entry_type": "IMMEDIATE", "entry": 1.25, "stop_loss": 1.20},
                # Conditional entry_type must be excluded from the "IMMEDIATE" baseline
                {"pair": "EUR/USD", "entry_type": "LIMIT_BUY", "entry": 1.10, "stop_loss": 1.05},
            ]
            self._write_research_csv(td / "research_trades.csv", rows)
            fake_trades = td / "no_trades.csv"
            with patch.object(self.daily.config, "DATA_DIR", td), \
                 patch.object(self.daily.config, "TRADES_CSV", fake_trades):
                median, n = self.daily._historical_immediate_stop_pips("EUR/USD", min_n=5)
        self.assertEqual(n, 5)
        self.assertAlmostEqual(median, 100.0, places=6)

    def test_returns_none_when_no_files_exist(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            with patch.object(self.daily.config, "DATA_DIR", td), \
                 patch.object(self.daily.config, "TRADES_CSV", td / "missing.csv"):
                median, n = self.daily._historical_immediate_stop_pips("EUR/USD")
        self.assertIsNone(median)
        self.assertEqual(n, 0)


class TestConditionalEntryPlausibilityCheck(unittest.TestCase):

    def setUp(self):
        import daily
        self.daily = daily
        # Deterministic distance baseline for every test in this class,
        # unless a specific test overrides it -- isolates sign/rr checks
        # from real trades.csv/research_trades.csv on this machine.
        self._hist_patcher = patch.object(
            self.daily, "_historical_immediate_stop_pips", return_value=(100.0, 10)
        )
        self._hist_patcher.start()
        self.addCleanup(self._hist_patcher.stop)

    def test_buy_correct_sign_and_consistent_rr_passes(self):
        # stop=100p, target=200p -> real R:R=2.0, matches stated
        result = self.daily._check_conditional_entry_plausibility(
            "EUR/USD", "BUY", entry=1.1000, stop_loss=1.0900, target=1.1200,
            reward_risk_stated=2.0,
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["failed"], [])
        self.assertTrue(result["checks"]["sign_direction"])
        self.assertTrue(result["checks"]["distance_plausible"])
        self.assertTrue(result["checks"]["rr_consistent"])

    def test_buy_backwards_stop_fails_sign_check(self):
        # stop ABOVE entry on a BUY -- unusable regardless of anything else
        result = self.daily._check_conditional_entry_plausibility(
            "EUR/USD", "BUY", entry=1.1000, stop_loss=1.1100, target=1.1200,
            reward_risk_stated=2.0,
        )
        self.assertFalse(result["passed"])
        self.assertIn("sign_direction", result["failed"])

    def test_sell_correct_sign_passes(self):
        result = self.daily._check_conditional_entry_plausibility(
            "EUR/USD", "SELL", entry=1.1000, stop_loss=1.1100, target=1.0800,
            reward_risk_stated=2.0,
        )
        self.assertTrue(result["checks"]["sign_direction"])

    def test_sell_backwards_target_fails_sign_check(self):
        # target ABOVE entry on a SELL -- backwards
        result = self.daily._check_conditional_entry_plausibility(
            "EUR/USD", "SELL", entry=1.1000, stop_loss=1.1100, target=1.1200,
            reward_risk_stated=2.0,
        )
        self.assertFalse(result["passed"])
        self.assertIn("sign_direction", result["failed"])

    def test_rr_inconsistent_with_stated_fails(self):
        # stop=100p, target=200p -> real R:R=2.0, but model claims 5:1
        result = self.daily._check_conditional_entry_plausibility(
            "EUR/USD", "BUY", entry=1.1000, stop_loss=1.0900, target=1.1200,
            reward_risk_stated=5.0,
        )
        self.assertFalse(result["passed"])
        self.assertIn("rr_consistent", result["failed"])
        self.assertEqual(result["checks"]["rr_real"], 2.0)
        self.assertEqual(result["checks"]["rr_stated"], 5.0)

    def test_rr_within_tolerance_passes(self):
        # real R:R = 210/100 = 2.1, stated 2.0 -- within 20% relative tolerance
        result = self.daily._check_conditional_entry_plausibility(
            "EUR/USD", "BUY", entry=1.1000, stop_loss=1.0900, target=1.1210,
            reward_risk_stated=2.0,
        )
        self.assertTrue(result["checks"]["rr_consistent"])

    def test_distance_far_outside_historical_range_fails(self):
        # Baseline median stop = 100p; this candidate's stop = 1000p (10x)
        result = self.daily._check_conditional_entry_plausibility(
            "EUR/USD", "BUY", entry=1.1000, stop_loss=1.0000, target=1.3000,
            reward_risk_stated=2.0,
        )
        self.assertFalse(result["passed"])
        self.assertIn("distance_plausible", result["failed"])

    def test_distance_within_historical_range_passes(self):
        # 150p stop is within 0.3x-3x of a 100p median baseline
        result = self.daily._check_conditional_entry_plausibility(
            "EUR/USD", "BUY", entry=1.1000, stop_loss=1.0850, target=1.1300,
            reward_risk_stated=2.0,
        )
        self.assertTrue(result["checks"]["distance_plausible"])

    def test_insufficient_historical_data_is_none_not_a_failure(self):
        self._hist_patcher.stop()
        with patch.object(self.daily, "_historical_immediate_stop_pips", return_value=(None, 2)):
            result = self.daily._check_conditional_entry_plausibility(
                "EUR/USD", "BUY", entry=1.1000, stop_loss=1.0900, target=1.1200,
                reward_risk_stated=2.0,
            )
        self._hist_patcher.start()
        self.assertIsNone(result["checks"]["distance_plausible"])
        self.assertTrue(result["passed"])  # insufficient data must not fail the overall verdict

    def test_unparseable_levels_fails_cleanly(self):
        result = self.daily._check_conditional_entry_plausibility(
            "EUR/USD", "BUY", entry="not a number", stop_loss=1.09, target=1.12,
            reward_risk_stated=2.0,
        )
        self.assertFalse(result["passed"])

    def test_missing_stated_rr_skips_that_check_without_failing(self):
        result = self.daily._check_conditional_entry_plausibility(
            "EUR/USD", "BUY", entry=1.1000, stop_loss=1.0900, target=1.1200,
            reward_risk_stated=None,
        )
        self.assertIsNone(result["checks"]["rr_consistent"])
        self.assertTrue(result["passed"])


if __name__ == "__main__":
    unittest.main()
