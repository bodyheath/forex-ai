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


if __name__ == "__main__":
    unittest.main()
