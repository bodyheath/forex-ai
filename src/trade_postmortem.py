"""Structured, queryable postmortem records for every real fund trade close.

2026-09-19: every investigation this month (the CHF/JPY postmortem, the
5-consecutive-loss streak, the confidence/ribbon backtests) required
manually re-opening raw report .txt files and re-reading English prose by
hand to reconstruct confidence, grade, ribbon-alignment, divergence, and
which risk factors were flagged pre-trade -- the exact same handful of
fields, re-derived from scratch, every single time. This builds that record
once, automatically, at close time, for every real fund trade (win, loss,
or expiry) -- not just the ones that catch a human's eye -- so a future
investigation can query it directly instead of repeating that manual work.

Explicitly NOT a decision-making mechanism. This module only ever reads an
already-closed, already-decided trade row and writes an observational
record to a companion log. Nothing here has a return value or side effect
that feeds back into any real gating/sizing/selection decision -- it cannot,
by construction, since it never runs until after a trade has already
closed. Mirrors the same "pure observability" discipline as every
shadow_mode evaluation and diagnostic file built this month (see
PROMOTION_DISCIPLINE.md).
"""
import json
from pathlib import Path

import config

POSTMORTEM_LOG = config.DATA_DIR / "trade_postmortems.json"

_WIN_LIKE_STATUSES = {"WIN", "FULL_WIN", "PARTIAL_WIN", "PROTECTED"}


def _to_float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN check, no math import needed for this


def classify_ribbon_alignment(direction: str, ribbon_state: str) -> str:
    """Classify a ribbon reading relative to a trade's own direction.

    Returns one of "strongly_against" / "against" / "aligned" / "neutral" /
    "unknown". "against" is a strict superset of "strongly_against"
    (LEANING_* included) -- code that wants this month's narrower
    "rib_strongly_against" definition specifically must check for the
    "strongly_against" value, not "against".
    """
    d = (direction or "").upper()
    rib = (ribbon_state or "").upper().strip()
    if d not in ("BUY", "SELL") or not rib:
        return "unknown"
    if d == "BUY":
        if rib == "ALIGNED_BEAR":
            return "strongly_against"
        if rib == "LEANING_BEAR":
            return "against"
        if rib in ("ALIGNED_BULL", "LEANING_BULL"):
            return "aligned"
    else:
        if rib == "ALIGNED_BULL":
            return "strongly_against"
        if rib == "LEANING_BULL":
            return "against"
        if rib in ("ALIGNED_BEAR", "LEANING_BEAR"):
            return "aligned"
    return "neutral"  # NEUTRAL, CONVERGING, or any other real ribbon_state


def compute_divergence(technical, fundamental, sentiment, positioning, macro):
    """tech_score - mean(other 4) -- the exact definition already
    pre-registered as the technical_carries_divergence shadow rule
    (PROMOTION_DISCIPLINE.md). None if any of the 5 scores is missing or
    unparseable: an incomplete divergence calculation is unknown, not 0."""
    vals = [_to_float(v) for v in (technical, fundamental, sentiment, positioning, macro)]
    if any(v is None for v in vals):
        return None
    tech, fund, sent, pos, mac = vals
    return round(tech - (fund + sent + pos + mac) / 4.0, 2)


# Keyword -> tag. Deliberately a plain substring/keyword detector on the raw
# RISK_FACTORS free text, NOT a semantic parser. This month's postmortems
# found the same handful of concerns recur almost verbatim across dozens of
# real reports (ribbon opposition, an opposed rate differential, crowded or
# reversing COT positioning, weekly/MTF disagreement, a specific chart
# structure risk). A tag means "this text mentions the concept" -- a coarse,
# inspectable index into free text that already existed, not a claim the
# system detected the concept with certainty. False negatives (a real risk
# phrased in words not listed here) are expected and acceptable; this is
# meant to catch the recurring, already-observed phrasings, not every
# possible one.
_RISK_TAG_KEYWORDS = {
    "ribbon_opposition":               ("ribbon",),
    "rate_differential_opposition":    ("rate differential",),
    "positioning_extreme_or_reversing": ("cot", "positioning", "crowded"),
    "mtf_weekly_conflict":             ("weekly", "mtf", "non-confirmation", "4h neutral"),
    "trend_structure_risk":            ("death-cross", "death cross", "double-top",
                                        "double top", "resistance", "sma50", "sma200"),
}


def extract_risk_factor_tags(risk_factors_text: str) -> list:
    """Coarse keyword-presence tags on the raw RISK_FACTORS text -- see the
    module-level note above. Not NLP; a documented, inspectable index into
    text that was already being saved, just never queryable."""
    text = (risk_factors_text or "").lower()
    if not text.strip():
        return []
    return sorted(
        tag for tag, keywords in _RISK_TAG_KEYWORDS.items()
        if any(kw in text for kw in keywords)
    )


def determine_materialization(tags: list, outcome_is_loss) -> dict:
    """Plain per-tag determination of whether a flagged risk "materialized".

    Defined explicitly as a DIRECTIONAL CONSISTENCY check, not a proven
    causal attribution: a tag is marked True when the trade's real outcome
    went against the trade (a loss, or an EXPIRED close with negative net
    pips) -- every one of these tags names a reason the trade might fail,
    so if it did fail, every flagged concern is *consistent with* having
    contributed, even though this cannot isolate which one actually did.
    Marked False (not omitted) when the trade won: the concern was present
    but the trade prevailed despite it. outcome_is_loss=None (outcome not
    yet known) marks every tag None, not False.
    """
    if outcome_is_loss is None:
        return {tag: None for tag in tags}
    return {tag: bool(outcome_is_loss) for tag in tags}


def reconstruct_grade_best_effort(row: dict) -> str:
    """Best-effort A-F grade, reconstructed from fields already persisted on
    a CLOSED trade row -- NOT a call into the live daily.py::
    _trade_quality_grade(), which needs the live `bundle` (MTF signals,
    Fibonacci proximity, ATR calibration, oscillator confluence) that never
    survives past the scan that created it, the same "no live dependency"
    constraint _ribbon_carveout_would_fire() (research_outcome_checker.py)
    already documents for this exact class of problem.

    KNOWN, QUANTIFIED IMPRECISION, stated plainly rather than hidden: this
    cannot see divergence/oscillator-confluence/Fib-proximity/ATR-
    calibration, all real inputs to the live grade, and cascade.py's fixed
    2.0 target R:R means every real v2 trade's reward_risk is ~2.0 --
    structurally too low to ever earn this reconstruction's "A" tier
    (needs >2.5), which the live grade can reach via other inputs this
    function can't see. Treat this field as indicative, not authoritative.
    """
    try:
        conf = float(row.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    try:
        rr = float(row.get("reward_risk") or row.get("rr_at_entry") or 0)
    except (TypeError, ValueError):
        rr = 0.0

    direction = (row.get("direction") or "").upper()
    ribbon_state = row.get("ribbon_state_at_entry") or ""
    alignment = classify_ribbon_alignment(direction, ribbon_state)

    weekly = (row.get("weekly_trend_at_entry") or "").upper()
    monthly = (row.get("monthly_trend_at_entry") or "").upper()
    w_d_conflict = (
        weekly in ("BUY", "SELL") and monthly in ("BUY", "SELL")
        and weekly != monthly
    )

    pair = (row.get("pair") or "").upper()
    ccys = pair.split("/")
    is_gbp_cross = "GBP" in ccys
    is_chf_cluster = pair in ("EUR/CHF", "NZD/CHF", "AUD/CHF")
    rib_scoped_relevant = is_gbp_cross or is_chf_cluster

    if w_d_conflict or (alignment == "strongly_against" and rib_scoped_relevant):
        return "F"
    if rr < 1.5:
        return "D"
    if conf >= 8 and rr > 2.5:
        return "A"
    if conf >= 7 and rr >= 2.0:
        return "B"
    if conf >= 6 and rr >= 1.5:
        return "C"
    return "D"


def build_postmortem_record(row: dict) -> dict:
    """Assemble the full structured postmortem for one closed trade row.

    `row` is a real trades.csv row dict (or equivalent) for a trade whose
    status is a real closed status. Every field here is derived purely from
    already-persisted data -- no live API calls, no bundle dependency.
    """
    status = str(row.get("status") or "").upper()
    net_pips = _to_float(row.get("net_pips"))
    gross_pips = _to_float(row.get("pips"))
    pnl = net_pips if net_pips is not None else gross_pips

    if pnl is None:
        outcome_is_loss = None
    else:
        outcome_is_loss = pnl < 0

    direction = row.get("direction")
    ribbon_state = row.get("ribbon_state_at_entry") or ""
    alignment = classify_ribbon_alignment(direction, ribbon_state)

    divergence = compute_divergence(
        row.get("technical"), row.get("fundamental"), row.get("sentiment"),
        row.get("positioning"), row.get("macro"),
    )

    tags = extract_risk_factor_tags(row.get("risk_factors"))
    materialization = determine_materialization(tags, outcome_is_loss)

    grade = reconstruct_grade_best_effort(row)

    try:
        confidence = float(row.get("confidence")) if row.get("confidence") not in (None, "") else None
    except (TypeError, ValueError):
        confidence = None

    return {
        "id": row.get("id"),
        "pair": row.get("pair"),
        "direction": direction,
        "status": status,
        "closed_at": row.get("closed_at"),
        "confidence": confidence,
        "grade_reconstructed": grade,
        "divergence": divergence,
        "ribbon_state_at_entry": ribbon_state or None,
        "ribbon_alignment": alignment,
        "risk_factor_tags": tags,
        "risk_factor_materialized": materialization,
        "outcome_is_loss": outcome_is_loss,
        "net_pips": pnl,
    }


def record_postmortem(record: dict) -> None:
    """Append or update one postmortem record in the durable companion log,
    keyed by trade id. Idempotent -- re-processing the same id (a trade
    re-checked on a later cycle before its outcome was final) replaces its
    entry rather than duplicating it. Pure observability -- never raises,
    never touches any real trade row."""
    try:
        trade_id = str(record.get("id"))
        log_data: dict = {}
        if POSTMORTEM_LOG.exists():
            try:
                log_data = json.loads(POSTMORTEM_LOG.read_text(encoding="utf-8"))
            except Exception:
                log_data = {}
        if not isinstance(log_data, dict):
            log_data = {}
        log_data[trade_id] = record
        POSTMORTEM_LOG.parent.mkdir(parents=True, exist_ok=True)
        POSTMORTEM_LOG.write_text(json.dumps(log_data, indent=2, default=str), encoding="utf-8")
    except Exception as exc:
        import sys
        print(f"[trade_postmortem] record_postmortem failed (non-fatal): {exc}", file=sys.stderr)


def record_trade_postmortem(row: dict) -> dict:
    """Convenience: build + record in one call, for close-path call sites.
    Returns the built record (even if the write itself failed) so a caller
    can log/inspect it without a second call. Never raises."""
    try:
        rec = build_postmortem_record(row)
    except Exception as exc:
        import sys
        print(f"[trade_postmortem] build_postmortem_record failed (non-fatal): {exc}", file=sys.stderr)
        return {}
    record_postmortem(rec)
    return rec
