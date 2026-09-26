# Promotion discipline

**Enforced default as of 2026-09-04.** Any of the following is a "promotion":

- A new or changed grading/gating rule in `_trade_quality_grade()` or the
  fund-trade eligibility gate (`_dd_allows_trade()` and friends) in `daily.py`.
- Any virtual book's configuration ([`src/virtual_books.py`](src/virtual_books.py))
  being adopted as the real fund's rules.
- Any specialist agent's signal (Phase 01B+) being given real influence over
  a live trading decision.

**No promotion may ship without first clearing `src/shadow_mode.py`'s
`check_promotion_readiness()` for that specific change**, and no code path
may apply a promoted rule without first calling `assert_promotion_authorized()`
(which raises if the bar isn't cleared). This module never auto-promotes
anything — clearing the bar means "have the promotion conversation with a
human," never "ship it automatically."

## Why this exists

Every grading-rule change before 2026-09-02 (the GBP-cross exclusion, the
CHF-cluster exclusion, the ribbon-only-relief mechanism itself) was found and
shipped directly into `_trade_quality_grade()` in the same sitting that
discovered it — using the same discovery sample to both find and validate the
pattern, with no out-of-sample check and no multiple-comparisons correction.
The ribbon-regime carve-out and VIX-regime-edge findings got a real
pre-registered holdout instead, but only because a human insisted on it, case
by case, in conversation. Nothing in the codebase enforced it, and nothing
stopped the other three from shipping without one.

Phase 01 makes this more urgent, not less: several virtual books already run
in parallel every scan, and Phase 01B adds a specialist agent's own book on
top of that. Every additional thing being tested simultaneously raises the
chance that *something* clears a raw `p<0.05` by pure multiple-comparisons
luck alone — the risk compounds, it doesn't dilute.

## What `check_promotion_readiness()` actually checks

- **Sample size**, per side (`min_n_fire` / `min_n_no_fire` — asymmetric by
  design; a regime-conditional carve-out's two sides don't need equal
  evidence).
- **Statistical significance**, Bonferroni-corrected by how many other rules
  are currently active (registered, not yet promoted) — `corrected_alpha =
  alpha / n_active_rules`. Register a fourth parallel book and every rule's
  bar gets stricter automatically, with no manual recalibration.
- **Optional profit-factor criteria** (`pf_max_fire`, `pf_min_fire`,
  `pf_min_gap`) for rules that specifically claim a group is a net loser, a
  net winner, or that the gap between two groups is real and non-trivial —
  not just a win-rate blip.

`promotable` is the real go/no-go signal. `ready_for_review` is a looser,
older trigger (enough data OR enough time has passed) that only means "worth
a look" — a rule can be `ready_for_review` and still correctly conclude
"still too thin, wait."

## Currently tracked rules (as of 2026-09-04)

| Rule | Status | Notes |
|---|---|---|
| `ribbon_carveout_exclude_trending_risk_on` | **Not promotable** | n=98/32 (need 100/40); p=0.38 (need <0.025 corrected); PF(fire)=1.51 (need ≤0.80). See [`project_ribbon_regime_carveout_threshold.md`](../../memory) and the 2026-09-04 re-verification below. A separate 43,344-candidate 3-year mechanical backtest (`scripts/historical_grading_backtest.py`, no regime split) found ribbon-strongly-against and ribbon-against statistically indistinguishable from each other (p=0.48) and both mildly beating non-opposition — doesn't resolve this rule's regime-specific question directly, but weakens ribbon-opposition-as-explanation generally. |
| `vix_regime_edge_trending_risk_on` | **Not promotable — trending toward confirmed false lead** | Post-freeze new data (n=97/94) shows the gap has *inverted* (below now beats above), not merely narrowed. p=0.11, PF gap is negative. See [`project_vix_regime_edge_threshold.md`](../../memory). |
| `vbook_B_conf6_rr15`, `vbook_C_grade_based`, `vbook_D_no_da`, `vbook_E_no_dd_gate` | Auto-registered, accumulating | Every settled position each book takes is logged automatically (`src/virtual_books.py::_settle_book_positions()`) — no manual step required. Currently near-zero volume (the feature is new); check `check_promotion_readiness()` before treating any of these as evidence either way. |
| `ribbon_general_population_demotion_removed` | **Promoted 2026-09-06** | Removal-type rule — read inverted (see [`project_ribbon_grading_fix_sep2026.md`](../../memory)): supporting evidence is fire/no-fire being statistically indistinguishable (p=0.435, WR 45.7% vs 46.3%), not the usual "fire is worse." Code change (removing the general-population ribbon-opposition F/D demotion in `_trade_quality_grade()`) reviewed and merged to `main` at `c068d7c1`; `mark_promoted()` called by hand per this file's own rule — no automatic promotion occurred. |
| `ribbon_general_population_demotion_removed` | **Implemented on `wip/expiry-window-and-ribbon-grading-fix`, pending human review — NOT merged, NOT `mark_promoted()`'d** | A removal, not a new demotion — read criteria inverted from usual. Sampled evidence (n=300/300): p=0.435, fire WR=45.7% vs no-fire WR=46.3% — statistically indistinguishable, which is the supporting result for removing ribbon-opposition as a general-population F/D trigger. Full 43,344-row population backs the same conclusion. See [`project_ribbon_grading_fix_sep2026.md`](../../memory). |
| `technical_carries_divergence` | **Not promotable — live feed wired 2026-09-13, accumulating fresh evaluations** | divergence = tech_score − mean(fund/sent/pos/macro) ≥ 3.0 (the CHF/JPY #7325 postmortem shape). Discovery sample (research_trades.csv, n=41): WR=17.1% vs 26.2% (divergence≤1, n=909), PF=0.243 vs 0.754 — real, large gap, but p=0.096 (not <0.05) and non-monotonic at the tail (n=18 @divergence≥3.5 bounced to WR=33.3%). Bar: n≥100/100, p<0.05, on fresh out-of-sample data. `_record_divergence_evaluation()` in `src/research_outcome_checker.py` now feeds every real research-trade close (mirrors `_record_ribbon_carveout_evaluation()`'s shape exactly — pure observability, never touches the trade's own fields, never gates a decision) — check `check_promotion_readiness()` before treating this as more than a snapshot. |
| `confidence_floor_underperforms_conf6` | **Not promotable — registered on the discovery sample, 0 fresh evaluations yet** | would_fire=True means confidence≥7 (the real gate's current floor); would_fire=False means confidence==6 (below the floor, never reaches a real trade under current gating). Discovery sample (research_trades.csv, strict v2/post-cutoff, EXPIRY-inclusive): conf==6 n=492 WR=41.9% PF=1.175 (best); conf==7 n=484 WR=40.3% PF=0.792; conf==8 n=117 WR=34.2% PF=0.469 (worst) — a monotonic-looking degradation from 6→7→8, opposite of what confidence should predict, consistent with this codebase's own known grade-ordering-inversion pattern. None of the pairwise WR gaps clear significance yet (conf6-vs-7 p=0.31; conf7-vs-8 p=0.11; conf6-vs-8 p=0.064) — real and large in PF/pnl terms, not yet confirmed. Bar: n≥100/100, p<0.05, on fresh data. Nothing currently feeds `record_evaluation()` for this rule; wiring a live feed (mirroring `technical_carries_divergence`'s in `research_outcome_checker.py`) is a separate, not-yet-done follow-up. |
| `repeat_thesis_after_materialized_risk` | **Not promotable — registered 2026-09-19 on an n=1 discovery sample, 0 fresh evaluations yet** | would_fire=True means this candidate's pair+direction matches a real fund LOSS in the prior 30 days whose `risk_factor_tags` (`src/trade_postmortem.py`) included at least one tag that materialized. Raised 2026-09-17 during the 5-consecutive-loss streak review; correctly scoped to the real fund's own decisive history (not the much noisier research population, where "same pair+direction within 30 days" is nearly universal and produced a nonsense ~5,000-repeats/326-losses result). Real fund history to date has exactly ONE such repeat: the two AUD/NZD SELL trades (#6895, #7615), 7 days apart, both losses, both confirmed ribbon-strongly-against — n=1, explicitly too thin to mean anything, reported as such rather than forced into a read. Registered now purely so this real, plausible idea isn't lost track of. Bar: n≥30/30 (the plain default — no evidence yet to justify an asymmetric split), p<0.05, `pf_max_fire`≤0.80 (this last bar is *borrowed* from `ribbon_carveout_exclude_trending_risk_on` for consistency, not independently derived — n=1 cannot derive a PF bar). Nothing currently feeds `record_evaluation()` for this rule; wiring a live feed (checking each new real fund candidate against `data/trade_postmortems.json`) is a separate, not-yet-done follow-up. |
| `vbook_G_mechanical_reversion` | **Not promotable — registered 2026-09-2X, 0 live evaluations recorded yet** | Live pilot of `rib_against AND osc_agrees` (see `PROPOSAL_mechanical_reversion_engine.md` and `src/mechanical_reversion.py`), no confidence floor, no dd_mode gate, no LLM call, both directions (the BUY-only refinement's currency attribution was unstable between discovery and holdout — left for real forward data to resolve, not baked in). **Registered with `shadow_mode.register_rule(..., cluster_aware=True)`** (`BookConfig.regime_aware_promotion=True` in `src/virtual_books.py`): every real settled trade is still recorded (never skipped), tagged with a `regime_cluster` identifier by `_regime_cluster_tag()` (gap tolerance `_REGIME_GAP_DAYS=3`, empirically checked against the real gap distribution in `mechanical_edge_mining_dataset.csv` — gap==3 is a genuine 550-instance spike matching the Friday→Monday FX weekend, not an arbitrary default; live scan-selection gaps not present in that exhaustive-daily backtest are an open, disclosed caveat). `check_promotion_readiness()`'s `cluster_aware` branch (`_cluster_bootstrap_stats()` in `src/shadow_mode.py`) then does TWO things, not just one: (1) counts DISTINCT REGIME CLUSTERS toward n_fire/n_no_fire instead of raw trades, and (2) runs a real cluster bootstrap (resampling whole clusters with replacement, preserving each cluster's real internal size) for the p-value itself — fixing both the sample-size-inflation trap AND the within-regime-correlation trap a plain z-test on regime-deduplicated data would still have fallen into. This is the same methodology that found the naive per-row test on `rib_against` alone (raw p=0.00015) was NOT actually significant once regime-clustering was honestly accounted for (cluster-bootstrap p=0.052, regimes ran up to 232 consecutive calendar days in the backtest data), while the combined signal this book trades did survive (cluster-bootstrap p=0.0060, effect +6.2pp WR, 95% CI [+1.8, +11.0]pp). Bar: n≥30/30 distinct regime clusters (materially harder than the same number would be for a per-row-counted rule), p<0.05 from the cluster bootstrap (Bonferroni-corrected by active-rule count as usual). |
| `vbook_H_oscillator_extremity` | **Not promotable — registered 2026-09-26, 0 live evaluations recorded yet** | Round 4 of the edge-mining research loop (`scripts/edge_mining_sweep_round4.py`, `edge_mining_holdout_check_round4.py`, `edge_mining_cluster_bootstrap_stress_test.py`) put phenomenon 3 (oscillator extremity confirming direction, `osc_agrees` alone — no ribbon check) through the exact same three-gate process Book G's signal went through: discovery (already round-1-significant, p=0.00001), holdout plain z-test (p=0.00000), and — critically — a cluster bootstrap on holdout (p=0.00100, clearing round 4's own local bar of 0.00833). Book G's own population is a **strict subset** of this broader condition (100% overlap) — this book exists to test, with real forward evidence, whether requiring ribbon-against on top of oscillator extremity actually adds value over the broader signal alone, or just shrinks the sample. **Registered `cluster_aware=True` from day one** (`BookConfig.regime_aware_promotion=True`) — applying Phase 32's fix consistently from the start this time, not retrofitted the way Book G's was. Bar: n≥30/30 distinct regime clusters, p<0.05 from the cluster bootstrap (Bonferroni-corrected by active-rule count). |
| `vbook_I_bollinger_extremity` | **Not promotable — registered 2026-09-26, 0 live evaluations recorded yet** | Round 4 also validated a genuinely new indicator never tested in any prior round: `bollinger_extreme_agrees` (price at a Bollinger Band extreme confirming direction — volatility-band position, not momentum). Survived discovery (p=0.00025), holdout (p=0.00003), and cluster bootstrap on holdout (p=0.00600, clears round 4's local bar of 0.00833). Materially lower population overlap against every other validated signal from this research loop (Jaccard ≤0.59 vs Book G/H) — a genuinely different indicator family, not a near-duplicate. (A fourth round-4 survivor, `phenomenon2_stack AND osc_agrees` — the 200MA/MACD counter-trend axis combined with oscillator extremity — also cleared all three gates but was deliberately NOT spun into its own book: it overlaps 94.7% with Book G's own population, Jaccard 0.799, essentially the same trades under a different label. Reported as a confirming finding, not manufactured into redundant infrastructure.) **Registered `cluster_aware=True` from day one**, same as Book H. Bar: n≥30/30 distinct regime clusters, p<0.05 from the cluster bootstrap (Bonferroni-corrected by active-rule count). |

Re-run `check_promotion_readiness()` against current data before trusting
any of the above as more than a snapshot — this table will go stale the
moment new trades close.

## Adding a new rule (a grading idea, a book, or a specialist agent's signal)

```python
from src import shadow_mode

shadow_mode.register_rule(
    "my_new_idea",
    description="...",
    min_n_fire=30, min_n_no_fire=30,   # or an asymmetric pre-registered bar
    pf_max_fire=0.80,                   # optional — only if the hypothesis needs it
)

# alongside (never instead of) the real decision:
shadow_mode.record_evaluation(
    "my_new_idea", would_fire=..., outcome=..., net_pips=..., context={...},
)

# before ever proposing this as a real change:
shadow_mode.assert_promotion_authorized("my_new_idea")  # raises if not ready
```

Nothing about this API is specific to grading rules, ribbon regimes, or
virtual books — a Phase 01B specialist agent's book registers and checks in
exactly this shape, no rebuilding required.
