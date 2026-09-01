"""Shadow-mode staging for new grading/gating rule changes.

2026-09-02: this session's own audit found that every grading-rule change so
far (the GBP-cross exclusion, the CHF-cluster exclusion, the ribbon-only-relief
mechanism itself) was found and shipped directly into _trade_quality_grade()
in the same sitting that discovered it -- using the same discovery sample to
both find and validate the pattern, with no out-of-sample check and no
multiple-comparisons correction (the code's own comments admit this). The
ribbon-regime carve-out and VIX-regime-edge findings got a real pre-registered
holdout instead -- but only because a human insisted on it, case by case, in
conversation. Nothing in the codebase enforced it, and nothing stopped the
other three from shipping without one.

This module is the fix: the STANDARD way any new grading/gating rule idea
gets introduced from now on, not an optional extra step. A candidate rule is
registered once, then every real scan records what the rule WOULD have done
(via record_evaluation()) without the rule actually affecting any real
decision. Promotion -- flipping the rule live -- always requires a separate,
explicit code change reviewed and approved by a human; this module never
auto-promotes anything. It only tracks whether a rule has accumulated enough
shadow evidence to be worth that conversation.

Usage pattern for a new rule idea:

    from src import shadow_mode

    # once, when the idea is first introduced:
    shadow_mode.register_rule(
        "example_new_exclusion",
        description="Exclude EUR/SEK from the ribbon-only-relief carve-out",
    )

    # inside the real code path, alongside (not instead of) the existing logic:
    would_fire = <the new rule's own condition, computed but not applied>
    shadow_mode.record_evaluation(
        "example_new_exclusion",
        would_fire=would_fire,
        outcome=<status once known, or None if not yet decisive>,
        context={"pair": pair, "grade": grade, ...},
    )

    # separately, e.g. in a health_check pass or on request:
    status = shadow_mode.check_promotion_readiness("example_new_exclusion")
    # status["ready"] is True once the promotion bar is cleared -- that is a
    # signal to have the promotion conversation, not permission to promote.
"""

import sys
from datetime import datetime, timezone

import config
from src.trading import financials

_SHADOW_FILE = config.DATA_DIR / "shadow_rules.json"

# Default promotion bar: whichever comes first. n>=30 mirrors this session's
# usual real-money-adjacent sample-size floor (see e.g. the VIX-regime-edge
# and ribbon-regime-carveout pre-registrations); 14 days caps how long a
# thin-volume rule can sit in limbo before its shadow evidence gets reviewed
# regardless of count.
DEFAULT_MIN_N = 30
DEFAULT_MAX_DAYS = 14


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    if not _SHADOW_FILE.exists():
        return {}
    try:
        import json
        return json.loads(_SHADOW_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[shadow_mode] load failed ({exc}) -- starting fresh, nothing promoted or lost "
              f"(shadow evaluations are advisory only, never gate a real trade)", file=sys.stderr)
        return {}


def _save(state: dict) -> None:
    if not financials.atomic_write_json(_SHADOW_FILE, state):
        print(f"[shadow_mode] CRITICAL: save failed -- shadow evaluation not persisted", file=sys.stderr)


def register_rule(rule_name: str, description: str,
                   min_n: int = DEFAULT_MIN_N, max_days: int = DEFAULT_MAX_DAYS) -> dict:
    """Register a new candidate rule for shadow-mode evaluation.

    Idempotent -- calling this again for an already-registered rule_name is a
    no-op that returns the existing registration unchanged (so it's safe to
    call at the top of whatever function evaluates the rule, every scan,
    rather than needing a separate one-time setup step).
    """
    state = _load()
    if rule_name in state:
        return state[rule_name]
    state[rule_name] = {
        "description":     description,
        "registered_at":   _now_iso(),
        "min_n":           min_n,
        "max_days":        max_days,
        "promoted":        False,
        "evaluations":     [],
    }
    _save(state)
    return state[rule_name]


def record_evaluation(rule_name: str, would_fire: bool,
                       outcome: str = None, context: dict = None) -> dict:
    """Record what the rule would have done for one real candidate, without
    applying it to any real decision.

    outcome: the candidate's real decisive status once known (WIN/FULL_WIN/
    LOSS/PARTIAL_WIN), or None if not yet closed -- pass it again via a
    later call (or leave the evaluation outcome-less; check_promotion_readiness
    only counts evaluations that do have one, since an undecided outcome
    can't inform whether the rule is any good).

    Raises if rule_name was never registered -- forces register_rule() to be
    called first rather than silently creating an unreviewed rule.
    """
    state = _load()
    if rule_name not in state:
        raise ValueError(
            f"shadow_mode.record_evaluation({rule_name!r}) called before "
            f"register_rule({rule_name!r}, ...) -- register the rule first."
        )
    state[rule_name]["evaluations"].append({
        "timestamp":  _now_iso(),
        "would_fire": bool(would_fire),
        "outcome":    outcome,
        "context":    context or {},
    })
    _save(state)
    return state[rule_name]


def check_promotion_readiness(rule_name: str) -> dict:
    """Return whether a shadow rule has accumulated enough evidence to be
    worth a promotion conversation.

    Never promotes anything itself -- "ready" means "go have the human
    conversation about promoting this," using the same real, decisive-only
    accounting this session has used everywhere else (WIN/FULL_WIN/LOSS with
    a known outcome; undecided evaluations don't count toward n).

    Returns:
        {
          "registered": bool,
          "n_decisive": int,           -- evaluations with a real outcome
          "days_elapsed": float,
          "ready": bool,               -- n_decisive>=min_n OR days_elapsed>=max_days
          "would_fire_wr": float|None, -- win rate among would_fire=True decisive evals
          "would_not_fire_wr": float|None,
        }
    """
    state = _load()
    if rule_name not in state:
        return {"registered": False}

    rule = state[rule_name]
    registered_at = datetime.fromisoformat(rule["registered_at"])
    now = datetime.now(timezone.utc)
    days_elapsed = (now - registered_at).total_seconds() / 86400.0

    decisive = [e for e in rule["evaluations"] if e.get("outcome") in
                ("WIN", "FULL_WIN", "LOSS", "PARTIAL_WIN")]
    n_decisive = len(decisive)

    def _wr(evals):
        if not evals:
            return None
        wins = sum(1 for e in evals if e["outcome"] in ("WIN", "FULL_WIN", "PARTIAL_WIN"))
        return round(wins / len(evals), 4)

    fires    = [e for e in decisive if e["would_fire"]]
    no_fires = [e for e in decisive if not e["would_fire"]]

    ready = (n_decisive >= rule["min_n"]) or (days_elapsed >= rule["max_days"])

    return {
        "registered":        True,
        "description":       rule["description"],
        "n_decisive":        n_decisive,
        "n_total_evals":     len(rule["evaluations"]),
        "days_elapsed":      round(days_elapsed, 1),
        "min_n":             rule["min_n"],
        "max_days":          rule["max_days"],
        "ready":             ready,
        "would_fire_wr":     _wr(fires),
        "would_not_fire_wr": _wr(no_fires),
        "promoted":          rule.get("promoted", False),
    }


def list_rules() -> dict:
    """Return the full shadow-rules state, e.g. for a health_check summary line."""
    return _load()


def mark_promoted(rule_name: str) -> None:
    """Record that a rule was promoted to live -- purely a record-keeping flag
    for whoever reads shadow_rules.json later; does not itself change any
    grading/gating behavior. Call this by hand once the actual code change
    promoting the rule has been reviewed, approved, and shipped -- never
    automatically.
    """
    state = _load()
    if rule_name not in state:
        raise ValueError(f"mark_promoted({rule_name!r}) — rule was never registered")
    state[rule_name]["promoted"] = True
    state[rule_name]["promoted_at"] = _now_iso()
    _save(state)
