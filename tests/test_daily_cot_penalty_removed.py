"""Confirms the 2026-09-08 removal of daily.py's deterministic
_cot_reversal_penalty() (the code-side half of the COT reversal/unwind fix
-- see tests/test_haiku_cot_prompt_removal.py for the LLM-prompt-side half).

Real backtest (scripts/cot_reversal_penalty_backtest.py) found the
penalized direction's forward win rate HIGHER than the non-penalized
baseline at every lag tested -- the opposite of this penalty's premise, not
just "no edge" -- so it was removed outright rather than down-weighted.

Requires ALLOW_LOCAL_RUN=YES to import daily.py (its own module-level
guard), same as every other daily.py-dependent local run in this repo.
"""
import os
import unittest

os.environ.setdefault("ALLOW_LOCAL_RUN", "YES")

import daily


class TestCotReversalPenaltyRemoved(unittest.TestCase):

    def test_function_and_constants_no_longer_exist(self):
        self.assertFalse(hasattr(daily, "_cot_reversal_penalty"))
        self.assertFalse(hasattr(daily, "_COT_EXTREME_PCT"))
        self.assertFalse(hasattr(daily, "_COT_UNWIND_MAGNITUDE_PCT"))

    def _bundle(self, side: str, momentum: str, old_net: float, ribbon="NEUTRAL"):
        return {
            "positioning": {side: {"status": "ok", "cot_momentum": momentum, "net_3w_ago": old_net}},
            "technical": {"daily": {"ribbon": {"status": ribbon}}},
        }

    def test_reversing_case_that_used_to_penalise_now_unaffected(self):
        # SELL GBP/USD with quote(USD) old_net > 0 (old long) REVERSING used
        # to trigger -1 under the old rule (SELL + quote old_long -> penalty).
        result = {
            "parsed": {"direction": "SELL", "confidence": 7},
            "bundle": self._bundle("quote", "REVERSING", 1000),
            "pair": "GBP/USD",
        }
        self.assertEqual(daily._eff_conf(result), 7.0)

    def test_unwinding_from_extreme_case_now_unaffected(self):
        # A case that used to trigger the widened UNWINDING branch (old
        # position within 15% of the 52-week extreme, >=20% unwind since).
        result = {
            "parsed": {"direction": "BUY", "confidence": 8},
            "bundle": {
                "positioning": {
                    "base": {
                        "status": "ok", "cot_momentum": "UNWINDING",
                        "net_3w_ago": 95, "one_year_high": 100, "one_year_low": 0,
                        "momentum_delta_pct": 25,
                    }
                },
                "technical": {"daily": {"ribbon": {"status": "NEUTRAL"}}},
            },
            "pair": "EUR/USD",
        }
        self.assertEqual(daily._eff_conf(result), 8.0)

    def test_ribbon_penalty_still_works_unaffected_by_removal(self):
        # Confirms the removal didn't collaterally break a DIFFERENT,
        # untouched adjustment in the same _eff_conf() cascade.
        result = {
            "parsed": {"direction": "SELL", "confidence": 7},
            "bundle": self._bundle("quote", "STABLE", 0, ribbon="ALIGNED_BULL"),
            "pair": "EUR/USD",
        }
        self.assertEqual(daily._eff_conf(result), 6.0)


if __name__ == "__main__":
    unittest.main()
