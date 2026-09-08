"""Smoke test for the COT-momentum prompt instructions in analyst.py's Haiku
system prompt, covering two separate removals against the same MOM= tag:

  - 2026-09-08: the REVERSING/UNWINDING penalty (-1/-2 POSITIONING_SCORE,
    -1 CONFIDENCE, a RISK_FACTORS mention) was removed from
    daily.py's deterministic _cot_reversal_penalty() (deleted entirely) and
    from analyst.py's Haiku prompt (instruction removed, MOM= tag still
    passed through but now explicitly marked informational-only).
  - 2026-09-08 (systemic audit): BUILDING's separate +1 POSITIONING_SCORE
    instruction was ALSO downgraded to informational-only, after a real
    walk-forward backtest (scripts/extreme_flag_and_building_boost_backtest.py)
    found no aggregate edge for it -- leaning backwards at 20 days, with
    individual currencies disagreeing in sign. All four MOM= values
    (BUILDING/STABLE/UNWINDING/REVERSING) are now informational-only.

IMPORTANT, same honest framing as the Sentiment Agent's live-only
validation: this is a STRUCTURAL check, not an empirical re-backtest of
Haiku's real behavior. No ANTHROPIC_API_KEY is available in this
environment, so there is no way to actually call Haiku and observe its real
output before/after. What IS verified here:
  1. The exact removed phrases (the -1/-2 POSITIONING_SCORE penalty, the
     separate CONFIDENCE -1, the RISK_FACTORS mention) are gone from the
     real, live _haiku_system_prompt() output.
  2. The new informational-only instruction is present instead.
  3. The MOM= data tag itself still flows through _compress_bundle() for
     real historical REVERSING/UNWINDING cases (pulled from
     data/cot_reversal_penalty_backtest_detail.csv, the same real CFTC data
     the backtest used) -- Haiku still SEES the classification, it's just
     no longer told to penalise it.
Whether Haiku's real output actually changes accordingly can only be
confirmed by watching real production scans after this ships, the same way
the Sentiment Agent's live-only shadow-mode evidence is the only real
validation that ever existed for it.
"""
import unittest

import pandas as pd

from src import analyst


_REMOVED_PHRASES = [
    "lower POSITIONING_SCORE -1 and note as risk",
    "lower POSITIONING_SCORE -2",
    "COT reversal: institutional positioning flipped",
    "CRITICAL: if trade direction aligns with the OLD positioning but COT is now REVERSING",
    "reduce CONFIDENCE by 1 and flag in RISK_FACTORS",
]


class TestHaikuPromptNoLongerPenalisesCotReversal(unittest.TestCase):

    def test_removed_phrases_absent_from_live_prompt(self):
        prompt = analyst._haiku_system_prompt()
        for phrase in _REMOVED_PHRASES:
            self.assertNotIn(phrase, prompt, f"stale penalty phrase still present: {phrase!r}")

    def test_new_informational_only_instruction_present(self):
        prompt = analyst._haiku_system_prompt()
        self.assertIn("UNWINDING/REVERSING", prompt)
        self.assertIn("informational context", prompt)
        self.assertIn("do NOT adjust POSITIONING_SCORE or CONFIDENCE", prompt)

    def test_building_boost_removed(self):
        # BUILDING's +1 POSITIONING_SCORE instruction was investigated and
        # found empirically unsupported (2026-09-08 systemic audit) -- it
        # must no longer appear as a scored instruction.
        prompt = analyst._haiku_system_prompt()
        self.assertNotIn("BUILDING=institutions increasing conviction in current direction: "
                         "raise POSITIONING_SCORE +1", prompt)
        self.assertNotIn("raise POSITIONING_SCORE +1", prompt)

    def test_all_four_mom_values_marked_informational_only(self):
        prompt = analyst._haiku_system_prompt()
        for value in ("BUILDING", "STABLE", "UNWINDING", "REVERSING"):
            self.assertIn(value, prompt)
        self.assertIn("informational context only", prompt)

    def test_mom_tag_still_flows_through_for_real_historical_reversing_cases(self):
        # Real historical firing instances from the backtest -- Haiku must
        # still SEE these classifications in its data (just no longer be
        # instructed to penalise them). Confirms the fix removed the
        # INSTRUCTION, not the underlying data delivery.
        detail = pd.read_csv("data/cot_reversal_penalty_backtest_detail.csv")
        real_cases = detail[detail["fired_type"].isin(["REVERSING", "UNWINDING"])].head(5)
        self.assertGreater(len(real_cases), 0, "expected real historical firing cases to exist")
        for _, row in real_cases.iterrows():
            bundle = {
                "positioning": {
                    row["fired_side"]: {
                        "status": "ok",
                        "currency": row["currency"],
                        "direction": "net LONG",
                        "percentile_in_range": 50.0,
                        "cot_momentum": row["fired_type"],
                    }
                }
            }
            compressed = analyst._compress_bundle(row["pair"], bundle)
            self.assertIn(f"MOM={row['fired_type']}", compressed,
                          f"MOM= tag missing for real case {row['pair']} {row['publication_date']}")


if __name__ == "__main__":
    unittest.main()
