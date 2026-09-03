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

This module is the fix: the STANDARD way any new grading/gating rule idea, any
parallel virtual book's configuration, or any specialist agent's own signal
gets introduced from now on, not an optional extra step -- register_rule() /
record_evaluation() / check_promotion_readiness() is the MANDATORY path before
any of those can be proposed as a real change to the actual fund's rules. A
candidate rule is registered once, then every real scan records what the rule
WOULD have done (via record_evaluation()) without the rule actually affecting
any real decision. Promotion -- flipping the rule live -- always requires a
separate, explicit code change reviewed and approved by a human; this module
never auto-promotes anything, and no code path in this repo may apply a
promoted rule without a passing assert_promotion_authorized() check first
(see that function). It only tracks whether a rule has accumulated enough
shadow evidence to be worth the promotion conversation.

2026-09-04: extended for the multi-book, multi-engine era (Phase 01+). Every
book/agent registered here shares ONE pool of "things currently being tested"
-- the more that pile up, the harder it should be for any single one to look
significant by chance alone. check_promotion_readiness() now Bonferroni-
corrects each rule's significance bar by the number of other rules currently
active (registered, not yet promoted or abandoned) at check time: more
parallel tests in flight makes EVERY one of them harder to promote, not
easier -- this is deliberate, not a bug, and gets stricter automatically as
Phase 01B's specialist-agent books come online. See PROMOTION_DISCIPLINE.md
at the repo root for the full policy this module enforces.

Usage pattern for a new rule idea (the general/default case -- a single
would_fire/would_not_fire split, symmetric sample-size bar):

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
        net_pips=<net_pips once known, or None>,
        context={"pair": pair, "grade": grade, ...},
    )

    # separately, e.g. in a health_check pass or on request:
    status = shadow_mode.check_promotion_readiness("example_new_exclusion")
    # status["promotable"] is True only once every configured criterion
    # (sample size per side, corrected significance, any PF bar) is met --
    # that is a signal to have the promotion conversation, not permission to
    # promote. status["ready_for_review"] fires earlier/looser (time or count
    # elapsed) and just means "worth a look," not "clear to ship."

Usage pattern for a rule with its own pre-registered, asymmetric criteria
(e.g. a regime-conditional carve-out where one side needs far more data than
the other, or a required profit-factor bar) -- exactly the shape both
long-pre-registered items (ribbon-regime carve-out, VIX-regime-edge) already
have:

    shadow_mode.register_rule(
        "ribbon_carveout_regime_exclusion",
        description="...",
        min_n_fire=100, min_n_no_fire=40,      # asymmetric, not the symmetric default
        pf_max_fire=0.80,                       # promote only if the "fires" side is a net loser
    )

This exact same shape (min_n_fire/min_n_no_fire, pf_max_fire/pf_min_fire/
pf_min_gap, alpha) is what a Phase 01B specialist agent's own book should
register with too -- nothing here is ribbon/VIX-specific, and nothing needs
rebuilding when the first agent book is ready to be evaluated.
"""

import math
import sys
from datetime import datetime, timezone

import config
from src.trading import financials

_SHADOW_FILE = config.DATA_DIR / "shadow_rules.json"

# Default promotion bar: whichever comes first. n>=30 mirrors this session's
# usual real-money-adjacent sample-size floor (see e.g. the VIX-regime-edge
# and ribbon-regime-carveout pre-registrations); 14 days caps how long a
# thin-volume rule can sit in limbo before its shadow evidence gets reviewed
# regardless of count. This is the REVIEW trigger, not the promotion bar --
# see ready_for_review vs promotable in check_promotion_readiness().
DEFAULT_MIN_N = 30
DEFAULT_MAX_DAYS = 14
DEFAULT_ALPHA = 0.05

_WIN_STATUSES = {"WIN", "FULL_WIN"}
_DECISIVE_STATUSES = {"WIN", "FULL_WIN", "LOSS", "PARTIAL_WIN"}


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
                   min_n: int = DEFAULT_MIN_N, max_days: int = DEFAULT_MAX_DAYS,
                   min_n_fire: int = None, min_n_no_fire: int = None,
                   alpha: float = DEFAULT_ALPHA,
                   pf_max_fire: float = None, pf_min_fire: float = None,
                   pf_min_gap: float = None) -> dict:
    """Register a new candidate rule, virtual-book configuration, or
    specialist-agent signal for shadow-mode evaluation.

    Idempotent -- calling this again for an already-registered rule_name is a
    no-op that returns the existing registration UNCHANGED, including its
    original criteria (so re-registering never silently loosens/tightens a
    rule's pre-registered bar -- change criteria only via a fresh rule_name
    or a deliberate, reviewed edit to shadow_rules.json).

    min_n_fire/min_n_no_fire: per-side decisive-evaluation floor. Default to
    `min_n` each (the simple, symmetric case) when not given. Set both
    explicitly for a pre-registered, asymmetric bar (e.g. the ribbon-regime
    carve-out's 100/40 split).

    alpha: this rule's own base significance target (before the multi-
    comparisons correction check_promotion_readiness() applies across all
    currently-active rules).

    pf_max_fire: promote only if the would-fire side's profit factor is AT OR
    BELOW this (a "this condition identifies a net loser" bar, e.g. the
    ribbon carve-out's 0.80).
    pf_min_fire: promote only if the would-fire side's profit factor is AT OR
    ABOVE this (a "this condition identifies a net winner" bar).
    pf_min_gap: promote only if PF(would_fire) - PF(would_not_fire) is at
    least this (e.g. the VIX-regime-edge's 0.30).
    Leave PF params None to skip that check entirely (the general/default
    case -- most rules, including a first specialist-agent book, won't need
    one until there's a specific reason to require it).
    """
    state = _load()
    if rule_name in state:
        return state[rule_name]
    state[rule_name] = {
        "description":     description,
        "registered_at":   _now_iso(),
        "min_n":           min_n,
        "max_days":        max_days,
        "min_n_fire":      min_n_fire if min_n_fire is not None else min_n,
        "min_n_no_fire":   min_n_no_fire if min_n_no_fire is not None else min_n,
        "alpha":           alpha,
        "pf_max_fire":     pf_max_fire,
        "pf_min_fire":     pf_min_fire,
        "pf_min_gap":      pf_min_gap,
        "promoted":        False,
        "evaluations":     [],
    }
    _save(state)
    return state[rule_name]


def record_evaluation(rule_name: str, would_fire: bool,
                       outcome: str = None, net_pips: float = None,
                       context: dict = None) -> dict:
    """Record what the rule would have done for one real candidate, without
    applying it to any real decision.

    outcome: the candidate's real decisive status once known (WIN/FULL_WIN/
    LOSS/PARTIAL_WIN), or None if not yet closed -- pass it again via a
    later call once resolved (leaves a harmless outcome-less entry behind;
    check_promotion_readiness only counts evaluations that DO have an
    outcome, since an undecided one can't inform whether the rule is any
    good). Prefer recording once, at settlement time, when both outcome and
    net_pips are already known -- simpler than a pending-then-resolved pair.

    net_pips: the trade's net_pips once known (real spread/slippage/
    commission/swap already applied). Optional, but required for this rule's
    PF-based criteria (pf_max_fire/pf_min_fire/pf_min_gap) to ever evaluate --
    without it those checks report "insufficient data" rather than passing
    by default. Also used to classify a PARTIAL_WIN outcome correctly (by
    net_pips sign, not by label) -- the same fix applied everywhere else in
    this codebase this session; an evaluation logged with outcome=
    "PARTIAL_WIN" and no net_pips falls back to the conservative (non-win)
    classification, same as the rest of this codebase's fallback convention.

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
        "net_pips":   net_pips,
        "context":    context or {},
    })
    _save(state)
    return state[rule_name]


def _is_win(evaluation: dict) -> bool:
    """PARTIAL_WIN classified by net_pips sign, not label -- see
    record_evaluation()'s docstring. Same fix pattern as risk_manager.py,
    dynamic_threshold.py, learning.py, dashboard.py, confidence_calibration.py."""
    status = evaluation.get("outcome")
    if status in ("WIN", "FULL_WIN"):
        return True
    if status == "PARTIAL_WIN":
        net_pips = evaluation.get("net_pips")
        if net_pips is None:
            return False  # unrecorded net_pips -- conservative, not a win
        try:
            return float(net_pips) > 0
        except (TypeError, ValueError):
            return False
    return False


def _wr(evals: list):
    if not evals:
        return None
    wins = sum(1 for e in evals if _is_win(e))
    return round(wins / len(evals), 4)


def _profit_factor(evals: list):
    """Profit factor from each evaluation's recorded net_pips. Returns None
    (not 0.0 or inf) when there isn't enough data to compute one -- callers
    must treat None as "can't check this criterion yet," never as a passing
    or failing value."""
    pips = [e.get("net_pips") for e in evals if e.get("net_pips") is not None]
    if len(pips) < 2:
        return None
    try:
        pips = [float(p) for p in pips]
    except (TypeError, ValueError):
        return None
    wins = sum(p for p in pips if p > 0)
    losses = -sum(p for p in pips if p < 0)
    if losses <= 0:
        return None  # no losing evaluations recorded -- PF undefined, not infinite
    return round(wins / losses, 3)


def _ztest(wins_a: int, n_a: int, wins_b: int, n_b: int):
    """Two-proportion z-test. Returns (p_value, wr_a, wr_b), or None if
    inputs are degenerate (matches health_check.py's own
    _ztest_worse_beats_better() math so the two never silently diverge)."""
    if n_a == 0 or n_b == 0:
        return None
    p1, p2 = wins_a / n_a, wins_b / n_b
    p_pool = (wins_a + wins_b) / (n_a + n_b)
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b))
    if se == 0:
        return None
    z = (p1 - p2) / se
    p_value = 1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2)))
    return p_value, p1, p2


def _active_rule_count(state: dict) -> int:
    """How many rules are currently 'in flight' -- registered and not yet
    promoted. This is the Bonferroni divisor: the more of these exist at
    once (several parallel virtual books, eventually specialist-agent books
    on top), the harder EVERY one of them should be to promote, since the
    chance any one clears a raw p<0.05 by pure multiple-comparisons luck
    rises with the count being tested. Always >= 1 so a single rule's
    correction is a no-op, not a divide-by-zero."""
    return max(1, sum(1 for r in state.values() if not r.get("promoted")))


def check_promotion_readiness(rule_name: str) -> dict:
    """Return whether a shadow rule has accumulated enough evidence to be
    worth a promotion conversation, and separately, whether it has actually
    cleared every criterion it was registered with.

    Never promotes anything itself. Two distinct signals, deliberately not
    conflated:
      - ready_for_review: the old, looser trigger (n_decisive>=min_n OR
        days_elapsed>=max_days) -- "worth looking at," not "clear to ship."
        A rule can be ready_for_review and still conclude "still too thin."
      - promotable: True only once EVERY configured criterion is met --
        min_n_fire/min_n_no_fire (per side), the corrected significance bar,
        and any registered PF criteria. This is the real go/no-go signal.
        For a plain rule with no PF criteria registered, promotable reduces
        to "both sides have enough decisive data AND the corrected z-test is
        significant."

    The significance bar is Bonferroni-corrected by the number of other
    rules currently active (see _active_rule_count()) -- more parallel
    virtual books/agent signals being tested at once makes this stricter
    automatically, not something that needs manual recalibration each time
    a new book comes online.
    """
    state = _load()
    if rule_name not in state:
        return {"registered": False}

    rule = state[rule_name]
    registered_at = datetime.fromisoformat(rule["registered_at"])
    now = datetime.now(timezone.utc)
    days_elapsed = (now - registered_at).total_seconds() / 86400.0

    decisive = [e for e in rule["evaluations"] if e.get("outcome") in _DECISIVE_STATUSES]
    n_decisive = len(decisive)

    fires    = [e for e in decisive if e["would_fire"]]
    no_fires = [e for e in decisive if not e["would_fire"]]
    n_fire, n_no_fire = len(fires), len(no_fires)

    min_n_fire    = rule.get("min_n_fire", rule.get("min_n", DEFAULT_MIN_N))
    min_n_no_fire = rule.get("min_n_no_fire", rule.get("min_n", DEFAULT_MIN_N))
    alpha         = rule.get("alpha", DEFAULT_ALPHA)

    n_active = _active_rule_count(state)
    corrected_alpha = alpha / n_active

    wr_fire, wr_no_fire = _wr(fires), _wr(no_fires)
    pf_fire, pf_no_fire = _profit_factor(fires), _profit_factor(no_fires)

    wins_fire    = sum(1 for e in fires if _is_win(e))
    wins_no_fire = sum(1 for e in no_fires if _is_win(e))
    z_result = _ztest(wins_fire, n_fire, wins_no_fire, n_no_fire)
    p_value = z_result[0] if z_result else None

    criteria = {
        "n_fire_ok":    n_fire >= min_n_fire,
        "n_no_fire_ok": n_no_fire >= min_n_no_fire,
        "p_value_ok":   (p_value is not None and p_value < corrected_alpha),
    }
    pf_max_fire = rule.get("pf_max_fire")
    pf_min_fire = rule.get("pf_min_fire")
    pf_min_gap  = rule.get("pf_min_gap")
    if pf_max_fire is not None:
        criteria["pf_max_fire_ok"] = (pf_fire is not None and pf_fire <= pf_max_fire)
    if pf_min_fire is not None:
        criteria["pf_min_fire_ok"] = (pf_fire is not None and pf_fire >= pf_min_fire)
    if pf_min_gap is not None:
        criteria["pf_min_gap_ok"] = (
            pf_fire is not None and pf_no_fire is not None
            and (pf_fire - pf_no_fire) >= pf_min_gap
        )

    promotable = all(criteria.values())
    ready_for_review = (n_decisive >= rule.get("min_n", DEFAULT_MIN_N)) or (days_elapsed >= rule["max_days"])

    return {
        "registered":         True,
        "description":        rule["description"],
        "n_decisive":         n_decisive,
        "n_fire":             n_fire,
        "n_no_fire":          n_no_fire,
        "n_total_evals":      len(rule["evaluations"]),
        "days_elapsed":       round(days_elapsed, 1),
        "min_n_fire":         min_n_fire,
        "min_n_no_fire":      min_n_no_fire,
        "max_days":           rule["max_days"],
        "alpha":              alpha,
        "n_active_rules":     n_active,
        "corrected_alpha":    round(corrected_alpha, 6),
        "p_value":            round(p_value, 6) if p_value is not None else None,
        "would_fire_wr":      wr_fire,
        "would_not_fire_wr":  wr_no_fire,
        "pf_fire":            pf_fire,
        "pf_no_fire":         pf_no_fire,
        "pf_max_fire":        pf_max_fire,
        "pf_min_fire":        pf_min_fire,
        "pf_min_gap":         pf_min_gap,
        "criteria":           criteria,
        "promotable":         promotable,
        "ready_for_review":   ready_for_review,
        "ready":              ready_for_review,  # backward-compat alias
        "promoted":           rule.get("promoted", False),
    }


def assert_promotion_authorized(rule_name: str) -> dict:
    """Hard gate: raise unless this rule has actually cleared every
    registered criterion. Call this at the START of any future code path
    that would apply a shadow-tested rule to a real decision (a real grading
    change, a virtual book's config becoming the fund's config, a specialist
    agent's signal gaining real influence) -- so the mandatory-shadow-first
    discipline is enforced by the code itself, not only by convention.

    This never flips anything live on its own -- it only refuses to let
    calling code proceed when the bar isn't cleared. Passing this check is
    necessary, not sufficient: promotion still requires a human decision and
    an explicit, reviewed code change (see mark_promoted()).
    """
    status = check_promotion_readiness(rule_name)
    if not status.get("registered"):
        raise RuntimeError(
            f"assert_promotion_authorized({rule_name!r}): never registered -- "
            f"call register_rule() and accumulate shadow evidence first."
        )
    if status.get("promoted"):
        raise RuntimeError(
            f"assert_promotion_authorized({rule_name!r}): already marked promoted -- "
            f"this guard is for the promotion moment itself, not for re-checking after."
        )
    if not status["promotable"]:
        failed = [k for k, v in status["criteria"].items() if not v]
        raise RuntimeError(
            f"assert_promotion_authorized({rule_name!r}): not promotable yet -- "
            f"failed criteria: {failed}. n_fire={status['n_fire']}/{status['min_n_fire']}, "
            f"n_no_fire={status['n_no_fire']}/{status['min_n_no_fire']}, "
            f"p={status['p_value']} (need < {status['corrected_alpha']:.5f} corrected for "
            f"{status['n_active_rules']} active rules)."
        )
    return status


def list_rules() -> dict:
    """Return the full shadow-rules state, e.g. for a health_check summary line."""
    return _load()


def mark_promoted(rule_name: str) -> None:
    """Record that a rule was promoted to live -- purely a record-keeping flag
    for whoever reads shadow_rules.json later; does not itself change any
    grading/gating behavior. Call this by hand once the actual code change
    promoting the rule has been reviewed, approved, and shipped -- never
    automatically. Also removes this rule from the Bonferroni divisor
    (_active_rule_count()) going forward, since a promoted rule is no longer
    "one more thing being tested" -- it's shipped.
    """
    state = _load()
    if rule_name not in state:
        raise ValueError(f"mark_promoted({rule_name!r}) — rule was never registered")
    state[rule_name]["promoted"] = True
    state[rule_name]["promoted_at"] = _now_iso()
    _save(state)
