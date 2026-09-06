"""Engine-disagreement rule for when more than one signal-generating engine
exists and proposes conflicting directions on the same pair (designed
2026-09-07, ahead of Phase 01B/02C's specialists going live -- see
PROMOTION_DISCIPLINE.md's "any specialist agent's signal being given real
influence over a live trading decision" promotion trigger).

Today there is exactly one engine with real directional authority: the main
LLM analysis pipeline (daily.py/cascade.py). Positioning/COT, Carry/Macro,
and Sentiment are shadow-mode advisory signals only -- none of them
independently propose a BUY/SELL direction with real capital behind it, so
this module has nothing to arbitrate yet. It exists so the rule is decided
and tested BEFORE the pressure of a real live conflict, not improvised in
the moment a second engine starts producing its own directional signal --
see config/known_dormant.md for why resolve() has no call site yet and what
will unorphan it.

============================================================================
THE RULE (decided now, not left to be argued case-by-case later)
============================================================================
1. Opposite-direction conflict (Engine A says BUY pair X, Engine B says
   SELL pair X, same day) -- HARD BLOCK, no trade taken. Not resolved by
   "higher confidence wins" and not averaged/netted.

   This mirrors every existing directional-conflict rule already in this
   codebase: w_d_conflict (weekly vs daily MTF disagreement) and
   ribbon-opposition both already treat a directional disagreement between
   two independent signal sources as an automatic F-grade block, never a
   vote. Direction is the one dimension where compromise is meaningless --
   a BUY and a SELL cannot be netted into a valid position, and picking
   whichever engine sounds more confident just relabels "we don't actually
   know which way this goes" as a decision. It's also the fail-safe
   direction for real capital: if the rule ever turns out to be wrong, it
   costs a missed opportunity, never a bad fill.

2. Same-direction, differing conviction (Engine A: BUY conf 8, Engine B:
   BUY conf 4; or Engine B abstains/NO_TRADE while Engine A signals BUY) --
   NOT a conflict in the blocking sense. This is a confidence-adjustment
   input, handled the same way COT/Carry/Sentiment already work today as
   shadow-mode-derived confidence nudges once promoted -- resolve() reports
   PROCEED with the agreed direction; it does not attempt to blend or
   weight confidences itself, since sizing already has its own machinery
   (risk_manager.py) for that.

3. Every opposite-direction block should be independently logged (via
   shadow_mode.record_evaluation() under _DISAGREEMENT_RULE, pre-registered
   below per PROMOTION_DISCIPLINE.md so it's ready the moment a second
   engine exists) capturing which two engines conflicted, their
   confidences, and which direction actually would have won. This makes
   "does disagreement itself predict a bad setup, or is blocking costing
   real opportunity" an evidence question to answer LATER, under the same
   n/PF/Bonferroni promotion bar as every other rule in this codebase --
   not something decided by fiat today. The operating rule in (1) stands
   regardless of what that future evidence shows, unless a change to it
   clears shadow_mode.check_promotion_readiness() the same way any other
   rule change would have to.

Public API
----------
resolve(signals: list[dict]) -> dict
    One dict per engine proposing on the SAME pair for the SAME day, each
    shaped {"engine": str, "direction": "BUY"|"SELL"|"NO_TRADE", "confidence": float}.
    Returns {"action": "BLOCK"|"PROCEED"|"NO_SIGNAL", "reason": str,
    "direction": str|None, "conflicting_engines": list[str]}.
"""
from src import shadow_mode

_DISAGREEMENT_RULE = "engine_disagreement_block_correct"
_DISAGREEMENT_RULE_DESCRIPTION = (
    "When 2+ engines propose opposite directions on the same pair/day, the "
    "trade is hard-blocked. Tracks whether the eventually-correct direction "
    "(if either) matches the engine that would have been overridden, to "
    "test later whether blocking-on-disagreement is itself a real signal."
)


def resolve(signals: list) -> dict:
    """Arbitrate directional signals from 2+ engines for one pair/day."""
    directional = [s for s in signals if s.get("direction") in ("BUY", "SELL")]
    if not directional:
        return {
            "action": "NO_SIGNAL",
            "reason": "no engine proposed a direction",
            "direction": None,
            "conflicting_engines": [],
        }

    directions = {s["direction"] for s in directional}
    if len(directions) > 1:
        engines = [s["engine"] for s in directional]
        parts = []
        for s in directional:
            parts.append(s["engine"] + "=" + s["direction"])
        summary = ", ".join(parts)
        reason = (
            f"{len(directional)} engines disagree on direction ({summary}) -- "
            f"hard block per the engine-arbitration rule (src/engine_arbitration.py), "
            f"not resolved by confidence"
        )
        return {
            "action": "BLOCK",
            "reason": reason,
            "direction": None,
            "conflicting_engines": engines,
        }

    # All directional engines agree -- proceed with the agreed direction.
    # Differing conviction between agreeing engines is a sizing/confidence
    # input elsewhere (risk_manager.py), not this module's concern.
    direction = directional[0]["direction"]
    return {
        "action": "PROCEED",
        "reason": "all directional engines agree",
        "direction": direction,
        "conflicting_engines": [],
    }


def register_disagreement_rule() -> None:
    """Register _DISAGREEMENT_RULE with shadow_mode. Idempotent -- safe to
    call repeatedly. Not yet called from any live path (see module
    docstring); call this once from the same startup sequence that would
    call resolve() for the first time on a real second engine."""
    shadow_mode.register_rule(_DISAGREEMENT_RULE, _DISAGREEMENT_RULE_DESCRIPTION)
