"""Tests for the 2026-09-07 Sonnet max_tokens truncation fix (src/analyst.py).

Real incident: AUD/CHF, Haiku conf=8/10 SELL with a coherent thesis, Sonnet's
confirmation call hit stop_reason=max_tokens on both attempts while still
mid-reasoning (MTF check, technical score) -- never reached a parseable
CONFIDENCE line. The candidate was silently dropped, and daily.py's
catch-all logged it identically to any other failure (`FAILED {pair}: {exc}`),
indistinguishable from an ordinary rejection or a data error.

Fixes covered here:
  1. analyse() now raises a distinct SonnetTruncatedError (not a generic
     RuntimeError) when BOTH attempts exhaust max_tokens without a parseable
     CONFIDENCE line -- a non-max_tokens unparseable response still raises
     the generic RuntimeError, unchanged.
  2. max_tokens raised 1000 -> 1500.
  3. The trailing user-message instruction now explicitly forbids preamble
     and demands the response start with "PAIR:".

No real API calls: _call_api() (the only function that touches the network)
is mocked directly with canned fake responses.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src import analyst


def _fake_resp(text: str, stop_reason: str, input_tokens=100, output_tokens=50):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class TestSonnetTruncation(unittest.TestCase):

    def _bundle(self):
        return {"technical": {"daily": {}}, "mtf": {}}

    @patch.object(analyst, "_call_api")
    def test_both_attempts_max_tokens_raises_distinct_error(self, mock_call):
        mock_call.side_effect = [
            _fake_resp("PAIR: AUD/CHF\nDIRECTION: SELL\nTECHNICAL_SCORE: walking through "
                       "the MTF check now, weekly shows...", "max_tokens"),
            _fake_resp("PAIR: AUD/CHF\nDIRECTION: SELL\nlet me reconsider the technical "
                       "score given the ribbon conflict...", "max_tokens"),
        ]
        with self.assertRaises(analyst.SonnetTruncatedError) as ctx:
            analyst.analyse("AUD/CHF", self._bundle(), haiku_report="conf=8 SELL")
        self.assertEqual(ctx.exception.pair, "AUD/CHF")
        self.assertEqual(ctx.exception.stop_reason, "max_tokens")
        self.assertIn("NOT a rejection on merit", str(ctx.exception))

    @patch.object(analyst, "_call_api")
    def test_recovers_on_second_attempt_no_error(self, mock_call):
        mock_call.side_effect = [
            _fake_resp("PAIR: AUD/CHF\nreasoning that got cut off", "max_tokens"),
            _fake_resp("PAIR: AUD/CHF\nDIRECTION: SELL\nCONFIDENCE: 8\nTRADE_THIS: YES", "end_turn"),
        ]
        report = analyst.analyse("AUD/CHF", self._bundle(), haiku_report="conf=8 SELL")
        self.assertIn("CONFIDENCE: 8", report)

    @patch.object(analyst, "_call_api")
    def test_non_max_tokens_unparseable_raises_generic_runtime_error(self, mock_call):
        # A genuinely malformed response under end_turn (not a truncation) must
        # still raise the plain RuntimeError -- SonnetTruncatedError is
        # specifically for the max_tokens case, not a catch-all replacement.
        mock_call.side_effect = [
            _fake_resp("garbled nonsense response", "end_turn"),
            _fake_resp("still garbled", "end_turn"),
        ]
        with self.assertRaises(RuntimeError) as ctx:
            analyst.analyse("AUD/CHF", self._bundle(), haiku_report="conf=8 SELL")
        self.assertNotIsInstance(ctx.exception, analyst.SonnetTruncatedError)

    @patch.object(analyst, "_call_api")
    def test_normal_success_first_attempt(self, mock_call):
        mock_call.return_value = _fake_resp(
            "PAIR: EUR/USD\nDIRECTION: BUY\nCONFIDENCE: 7\nTRADE_THIS: YES", "end_turn"
        )
        report = analyst.analyse("EUR/USD", self._bundle(), haiku_report="conf=7 BUY")
        self.assertIn("CONFIDENCE: 7", report)
        mock_call.assert_called_once()

    @patch.object(analyst, "_call_api")
    def test_max_tokens_ceiling_is_1500(self, mock_call):
        mock_call.return_value = _fake_resp(
            "PAIR: EUR/USD\nDIRECTION: BUY\nCONFIDENCE: 7\nTRADE_THIS: YES", "end_turn"
        )
        analyst.analyse("EUR/USD", self._bundle(), haiku_report="conf=7 BUY")
        # _call_api receives the `_call` closure; invoke it with a fake client
        # that records the kwargs passed to messages.create().
        sent_kwargs = {}
        class _FakeMessages:
            def create(self, **kwargs):
                sent_kwargs.update(kwargs)
                return _fake_resp("PAIR: EUR/USD\nCONFIDENCE: 7", "end_turn")
        fake_client = SimpleNamespace(messages=_FakeMessages())
        call_fn = mock_call.call_args.args[0]
        call_fn(fake_client)
        self.assertEqual(sent_kwargs["max_tokens"], 1500)

    def test_trailing_instruction_forbids_preamble(self):
        msg = analyst._build_sonnet_message("EUR/USD", self._bundle(), "conf=7 BUY")
        self.assertIn("starting immediately", msg)
        self.assertIn("no preamble", msg)
        self.assertIn("PAIR:", msg)


if __name__ == "__main__":
    unittest.main()
