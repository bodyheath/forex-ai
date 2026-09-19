"""One-off registration of the repeat_thesis_after_materialized_risk shadow
hypothesis -- item 3 (gap audit) of the 2026-09-19 auto-learning-
infrastructure request. Paperwork only: register_rule() never affects any
real decision. See PROMOTION_DISCIPLINE.md for the full table this updates
alongside this script.
"""
from src import shadow_mode

DESCRIPTION = (
    "A candidate whose pair+direction matches a PREVIOUS REAL fund trade that "
    "closed as a LOSS within the prior 30 days, where that previous loss had "
    "at least one risk_factor_tag (src/trade_postmortem.py) that materialized "
    "(the flagged concern was directionally consistent with the loss). "
    "would_fire=True means this exact repeat condition is met for the new "
    "candidate; would_fire=False covers every other real fund candidate. "
    "Raised 2026-09-17 as item 2 of the confidence/ribbon streak investigation "
    "(the 5-consecutive-loss review): scoped correctly to the real fund's own "
    "decisive history (n=14 at the time, methodology first tried on the much "
    "larger, much noisier research population and produced a nonsense "
    "~5000-repeats/326-losses result there -- research re-evaluates every pair "
    "many times a day, so 'same pair+direction within 30 days' is nearly "
    "universal and not a real filter in that population). Correctly scoped to "
    "real fund trades only, there is exactly ONE repeat in the fund's entire "
    "history to date: the two AUD/NZD SELL trades (#6895, #7615), 7 days "
    "apart, both losses, both confirmed ribbon-strongly-against. n=1 -- "
    "explicitly too thin to establish a pattern or backtest any blocking "
    "rule's real historical benefit, reported plainly as such rather than "
    "forced into a read. Registered now so this real, plausible idea is not "
    "lost track of and accumulates real evidence automatically as further "
    "real fund trades close, rather than only being revisited if a human "
    "happens to ask again. pf_max_fire=0.80 is BORROWED from "
    "ribbon_carveout_exclude_trending_risk_on's own bar for consistency -- "
    "NOT independently derived from data (n=1 cannot derive a PF bar). No "
    "live record_evaluation() feed exists yet for this rule -- wiring one "
    "(checking each new real fund candidate against "
    "data/trade_postmortems.json for a same-pair+direction loss with a "
    "materialized risk factor in the prior 30 days) is a separate, "
    "not-yet-done follow-up, exactly the same open-item shape "
    "confidence_floor_underperforms_conf6 was registered with."
)

result = shadow_mode.register_rule(
    "repeat_thesis_after_materialized_risk",
    description=DESCRIPTION,
    min_n_fire=30, min_n_no_fire=30,
    pf_max_fire=0.80,
)
print("Registered:", result["description"][:100], "...")
print("min_n_fire:", result["min_n_fire"], "min_n_no_fire:", result["min_n_no_fire"],
      "pf_max_fire:", result["pf_max_fire"], "promoted:", result["promoted"])
