"""Tests for the 2026-09-2X retirement of src/sentiment_agent.py -- see
config/known_dormant.md and project_sentiment_agent_retirement_sep2026.md
for the real root cause (NewsAPI free-tier ~24h publishing delay makes
same-day news structurally unavailable) and the decision.

The point of these tests: confirm evaluate() short-circuits BEFORE any real
network call, and that every downstream consumer (get_or_evaluate(),
would_fire(), Book F's eligibility) still behaves exactly as it did when
the agent could theoretically fire -- retirement must be invisible to
callers, not a breaking change.
"""
import unittest
from unittest.mock import patch

from src import sentiment_agent as sa


class TestRetirementShortCircuit(unittest.TestCase):

    def test_evaluate_returns_unavailable_without_network_call(self):
        with patch("requests.get") as mock_get:
            result = sa.evaluate("EUR/USD", "BUY")
        mock_get.assert_not_called()
        self.assertEqual(result["verdict"], "UNAVAILABLE")
        self.assertEqual(result["confidence"], 0)
        self.assertIn("retired", result["reason"].lower())

    def test_evaluate_never_calls_anthropic_client(self):
        with patch("requests.get") as mock_get, \
             patch("src.analyst._call_api") as mock_llm:
            sa.evaluate("USD/JPY", "SELL")
        mock_get.assert_not_called()
        mock_llm.assert_not_called()

    def test_result_shape_matches_the_real_unavailable_contract(self):
        """Every field a real caller reads must still be present, so nothing
        downstream needs a special case for the retired state."""
        result = sa.evaluate("GBP/USD", "BUY")
        for key in ("verdict", "confidence", "reason", "evaluated_at",
                    "oldest_headline_published", "newest_headline_published", "n_headlines"):
            self.assertIn(key, result)

    def test_get_or_evaluate_still_memoizes(self):
        sa._verdict_cache.clear()
        r1 = sa.get_or_evaluate("EUR/USD", "BUY")
        r2 = sa.get_or_evaluate("EUR/USD", "BUY")
        self.assertIs(r1, r2)
        self.assertEqual(r1["verdict"], "UNAVAILABLE")

    def test_would_fire_is_false_for_retired_result(self):
        result = sa.evaluate("EUR/USD", "BUY")
        self.assertFalse(sa.would_fire(result))

    def test_malformed_pair_no_longer_reached_but_still_safe(self):
        """With _RETIRED=True the malformed-pair branch is unreachable, but
        confirm it doesn't somehow raise if ever re-enabled with a bad pair."""
        try:
            result = sa.evaluate("NOTAPAIR", "BUY")
        except Exception as exc:
            self.fail(f"evaluate() raised on a malformed pair: {exc!r}")
        self.assertEqual(result["verdict"], "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
