"""One-time registration: sentiment_agent_supports, BEFORE a single live
evaluation happens (2026-09-06). Run once, then delete -- mirrors this
session's earlier one-time shadow_mode registration scripts.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import shadow_mode as sm

sm.register_rule(
    "sentiment_agent_supports",
    description=(
        "Phase 01B specialist #3 (Sentiment Agent, src/sentiment_agent.py). "
        "would_fire=True means the LLM read today's central-bank/news headlines "
        "for the candidate's base and quote currencies and returned verdict=SUPPORTS "
        "with confidence>=6 for this candidate's own stated direction; "
        "would_fire=False covers CONTRADICTS, NEUTRAL, and low-confidence SUPPORTS "
        "(UNAVAILABLE evaluations -- no same-day headlines, or the LLM call itself "
        "failed -- are never recorded here at all; absent evidence isn't evidence "
        "either way, see record_evaluation() call site in virtual_books.py's "
        "_settle_book_positions()). "
        "\n\n"
        "IMPORTANT, READ BEFORE INTERPRETING ANY EARLY RESULT: unlike every other "
        "rule in this file, this one has NO historical backtest behind it at all. "
        "Positioning (src/positioning.py) and Carry/Macro both got a real, cheap, "
        "multi-year historical backtest before being considered -- both came back "
        "honest no-gos. Sentiment structurally cannot get that treatment: there is "
        "no historical corpus of 'hawkishness scores' to test against, since the "
        "score doesn't exist until an LLM reads the text, and asking an LLM to score "
        "OLD news risks it pattern-matching on outcomes it already knows from "
        "training (the same contamination risk Phase 02 flagged for the technical "
        "layer's LLM judgment). This rule's promotion rests ENTIRELY on live, "
        "forward-only evidence accumulated one real candidate at a time -- there is "
        "no way to accelerate the sample size the way a backtest does for the other "
        "rules in this file. Expect this to take real weeks-to-months of elapsed "
        "calendar time before min_n_fire/min_n_no_fire are even reached, not because "
        "the bar here is different, but because there is no shortcut to it. An early "
        "promising-looking streak (n<<60) is NOT the same kind of evidence Positioning "
        "or Carry/Macro would have needed to clear their own bar -- do not treat it "
        "as such."
    ),
    min_n_fire=60, min_n_no_fire=60, alpha=0.05,
    # No pf_max_fire/pf_min_fire/pf_min_gap: deliberately omitted. Every other
    # PF criterion in this file was calibrated against some real backtest number
    # (e.g. the ribbon carve-out's pf_max_fire=0.8 came from live-data evidence).
    # This rule has no backtest to calibrate a PF bar against, so inventing one
    # now would be false precision, not genuine pre-registration -- the sample-
    # size + corrected-significance bar alone is the honest bar for this rule.
)

print("Registered sentiment_agent_supports with zero evaluations (registered_at stamped now).")
readiness = sm.check_promotion_readiness("sentiment_agent_supports")
print(readiness)
