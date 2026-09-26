# Proposal: mechanical-only reversion engine

**Status: proposal only, held for review. Nothing here is wired into any live
path, virtual book, or real trade decision.** The one thing this branch adds
beyond documentation is a small, pure, fully-tested logic module
(`src/mechanical_reversion.py`) so the proposal is concrete enough to
evaluate — it is not imported by `daily.py`, `virtual_books.py`, or anything
else that runs live.

## What's being proposed

A new signal, mechanical-only (zero LLM calls required to evaluate it),
discovered and holdout-validated in the 2026-09-25 edge-mining research loop
(see `scripts/edge_mining_*` and `data/mechanical_edge_mining_dataset.csv`
on `main`, committed directly as pure historical analysis):

**`rib_against AND osc_agrees`** — the daily EMA ribbon is against the trade
direction (BUY against ALIGNED_BEAR/LEANING_BEAR, or SELL against
ALIGNED_BULL/LEANING_BULL), AND the 3-oscillator confluence (RSI, Stochastic,
CCI — `technical.py::_oscillator_confluence()`, already a real, live
function) agrees with the trade's own direction (i.e., the oscillators are
signaling a reversal in the *same* direction as the trade).

Real, holdout-confirmed numbers (mechanical 2:1 R:R, 4-day expiry, real
transaction costs applied via `trade_costs.py`):

| Population | Discovery (n=30,352) | Holdout (n=12,992, touched once) |
|---|---|---|
| `rib_against` alone | WR 46.3%, PF 1.033 | WR 47.0%, PF 1.028 |
| `rib_against AND osc_agrees` | WR 48.2%, PF 1.256 | WR 50.0%, PF 1.223 |
| ...BUY-only | WR 54.2%, PF 1.727 | WR 54.6%, PF 1.372 |

vs. an unconditional baseline of WR 44.3-44.4%, PF 0.896-0.905 on both
slices.

## What this is NOT

- **Not proven to be a synergistic interaction.** Round 2 found oscillator
  confirmation does not add statistically significant value on top of
  `rib_against` alone (p=0.0185, does not clear the corrected bar) — the two
  signals substantially overlap (r=0.32), and most of the combined lift is
  redistribution, not a genuine new interaction.
- **The BUY-side strength is partly a base-rate artifact.** This exact
  3-year window already shows BUY beating SELL unconditionally (WR 47.2%
  vs 41.4%, PF 1.035 vs 0.774) with *no* signal applied. Some, not all, of
  the BUY-only slice's apparent strength rides that pre-existing skew (the
  signal's own lift is +7.0pp for BUY vs +3.4pp for SELL — real, but smaller
  than the raw comparison first suggested). This base-rate skew is itself
  unexplained and may not persist into a future window with different macro
  conditions (e.g. a genuine USD-strength cycle).
- **Effective sample size is smaller than 43,344 rows suggests.** Daily
  bars are autocorrelated, BUY/SELL rows share the same underlying price
  path per pair-day, and the 28-pair universe shares heavy currency overlap
  (a single EUR-wide move touches 7 pairs at once). The real number of
  independent multi-week market regimes across 3 years is probably in the
  dozens, not thousands — see the honest trust-level discussion in the
  conversation this proposal came from.

## What a mechanical engine around this would look like

1. **Inputs**: `direction`, `ribbon_status` (from `technical._ema_ribbon()`),
   `osc_direction`/`osc_score` (from `technical._oscillator_confluence()`) —
   all already real, live, existing functions. No new data source, no LLM
   call.
2. **Entry construction**: reuse the exact mechanical 2:1 R:R shape already
   validated here and already used everywhere else in this codebase
   (`cascade.py`'s `TARGET_RR=2.0`, stop = ATR14-derived, rounded to the
   nearest 5 pips) — not a new, unvalidated sizing scheme.
3. **Cadence**: since it needs no LLM call, it can evaluate the full 28-pair
   universe every scan (or more often) at effectively zero marginal cost —
   the main current cost driver (Haiku/Sonnet calls) doesn't apply.
4. **Deployment path, matching this engagement's own established
   discipline exactly**:
   - Register as a new `shadow_mode` rule (`mechanical_reversion_rib_osc`),
     paperwork only, with a real pre-registered bar — not done on this
     branch, left for the review conversation, since registering it
     unilaterally would be deciding the next step rather than proposing it.
   - Run as a new virtual book (`Book G`, say) through the *already-existing*
     Phase 01 virtual-books harness (`src/virtual_books.py`) — a fully
     independent simulated portfolio, zero real capital, exactly like
     Books A-F.
   - Accumulate real forward (out-of-sample-in-time) evidence for weeks to
     months before any promotion conversation, exactly like every other
     rule in `PROMOTION_DISCIPLINE.md`.
   - Never touches real trade selection, sizing, or gating unless and until
     it clears `shadow_mode.assert_promotion_authorized()` — the same hard
     gate everything else in this codebase goes through.

## Honest trust assessment (asked for directly, not softened)

This is the most rigorously-checked finding to come out of this engagement
— it's the only one that has ever been run through a genuine chronological
discovery/holdout split with a pre-registered, multiple-comparisons-corrected
bar, and it survived. That is real and should count for something.

But I would not call it proven in the way "3 years of daily data across 28
pairs" sounds like it should mean. The *effective* independent sample here
is much thinner than the row count suggests, for the reasons above, and nothing
about this has been checked against a genuinely different historical era
(a real 2008-style crisis, a real multi-year USD supercycle, a real
sustained risk-off regime) — this whole 3-year window may itself be one
`regime`, statistically speaking. I'd treat this as: real enough to be worth
piloting as a new virtual book and accumulating fresh forward evidence, not
real enough to skip that step or to touch real capital on the strength of
the backtest alone. That's exactly what the deployment path above is
designed to enforce.
