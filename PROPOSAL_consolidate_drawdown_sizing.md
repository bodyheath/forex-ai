# Proposal: consolidate the double-drawdown-penalty in position sizing

**Status: proposal + real backtest, held for review. `src/position_sizing.py`,
`daily.py`, and `src/fund_state.py::update_sizing_state()` are changed on
this branch to make the consolidation concrete and testable — this touches
real sizing logic and must not merge without explicit walk-through and
approval.**

## What was found (investigation, prior round)

Two independent, uncoordinated drawdown-penalty mechanisms existed:

1. `fund_state.py::compute_sizing()` — real drawdown tiers (0/3/5/7/10%),
   with a real quality gate (deeper tiers require `checklist_score>=8/9`)
   and recovery/win-streak-boost logic.
2. `position_sizing.py`'s own `DRAWDOWN_SIZING` table (2/4/6/8/10%),
   chained on top as part of the "volatility-adjusted" overlay at the real
   trade-creation call site — the SAME `current_drawdown_pct` value,
   re-penalized a second time by a different, coarser table.

Git history evidence (introduced 3 days apart, no comment anywhere
acknowledging the other mechanism, the call site's own header describing
itself as "Volatility-adjusted position sizing" with no mention of a
deliberate drawdown safety margin) pointed to **organic duplication, not
deliberate design** — see the prior investigation for the full evidence.

## What's proposed

Consolidate to fund_state.py's `compute_sizing()` as the **sole**
drawdown-based sizing authority — it's the more complete of the two
mechanisms (real quality gate, recovery boost, win-streak boost).
`position_sizing.py` keeps only the regime and loss-streak dimensions,
which are genuinely independent of drawdown, not duplicated elsewhere.

Concretely: `DRAWDOWN_SIZING` and the `drawdown_pct` parameter are removed
from `position_sizing.py::calculate_position_size()`; the two real call
sites (`daily.py`'s real trade-creation step, `fund_state.py::
update_sizing_state()`'s display) no longer pass a drawdown figure into
the overlay.

## Real historical backtest: what this would have changed

Pulled every real fund trade with persisted entry-time sizing context
(`drawdown_pct_at_entry`, `consecutive_losses_at_entry`,
`regime_at_entry` — n=8, the fund's entire real history where sizing was
actually computed and recorded) and recomputed BOTH the current
(double-penalized) and consolidated position size for each, using the real
persisted inputs:

| id | pair | dd% | losses | regime | OLD (double) | CONSOLIDATED |
|---|---|---|---|---|---|---|
| 5468 | USD/CAD | 0.00 | 0 | TRENDING_RISK_ON | 1.00% | 1.00% |
| 5952 | EUR/AUD | 0.00 | 0 | TRENDING_RISK_ON | 1.00% | 1.00% |
| 6895 | AUD/NZD | 0.65 | 1 | TRENDING_RISK_ON | 1.00% | 1.00% |
| 6987 | USD/JPY | 0.65 | 1 | TRENDING_RISK_ON | 1.00% | 1.00% |
| 7325 | CHF/JPY | 3.06 | 3 | TRENDING_RISK_ON | 0.38% | 0.38% |
| 7569 | AUD/JPY | 3.43 | 4 | TRENDING_RISK_ON | 0.25% | 0.25% |
| 7615 | AUD/NZD | 3.43 | 4 | TRENDING_RISK_ON | 0.25% | 0.25% |
| 8100 | GBP/AUD | 3.69 | 5 | TRENDING_RISK_ON | 0.25% | 0.25% |

**Real historical difference: zero.** In every one of these 8 real cases,
the loss-streak multiplier was already at least as strict as what the old
drawdown table would have additionally imposed, so the redundant penalty
was never actually the binding constraint historically. This consolidation
would not have changed a single real trade's size to date — n=8 is thin,
and this is reported as exactly what it is: a real but small sample where
the bug happened not to bite yet, not proof it never would.

**Where it WOULD start to matter (full grid, not just what happened)**:
swept drawdown × consecutive-losses combinations through both formulas.
Divergence begins at **drawdown >= 3.0% with 0-1 consecutive losses**, and
grows to 2x smaller sizing under the old double-penalized version as
drawdown deepens toward 5-6% while the loss streak stays low:

| drawdown | losses | OLD (double) | CONSOLIDATED | ratio |
|---|---|---|---|---|
| 3.0-4.0% | 0-1 | 0.56% | 0.75% | 1.3x |
| 4.5% | 0-1 | 0.38% | 0.75% | 2.0x |
| 5.0-6.0% | 0-1 | 0.25% | 0.50% | 2.0x |

**This is directly relevant right now, not hypothetical**: the fund's real
current drawdown is 3.69% — already inside the divergence zone. The only
reason today's real sizing isn't currently affected is that
`consecutive_losses=4` is independently already forcing MINIMAL via the
loss-streak dimension. The moment that streak breaks (a single real win)
while drawdown is still anywhere near this level, the old double-penalized
formula would immediately start suppressing size for no documented reason
— working directly against the stated priority of increasing real trade
frequency without cause.

## Why this matters given current priorities

The mechanical layer is the only place a real, evidence-backed edge has
been found this engagement (see the mechanical-reversion stress-test and
Book G). Real trade frequency is the binding constraint on getting a
statistically meaningful answer about anything in a reasonable timeframe.
An undocumented, duplicated safety margin that silently halves position
size the moment drawdown crosses ~3% (a level the fund is already at)
works directly against that, for a reason nobody chose deliberately.

## What's NOT proposed

No change to `fund_state.py::compute_sizing()` itself — it's kept exactly
as-is, including its quality-gate and recovery/win-streak-boost logic. No
change to the loss-streak or regime dimensions. This is a narrow removal
of one redundant table, not a broader sizing redesign.
