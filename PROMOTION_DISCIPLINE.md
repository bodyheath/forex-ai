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
| `ribbon_general_population_demotion_removed` | **Implemented on `wip/expiry-window-and-ribbon-grading-fix`, pending human review — NOT merged, NOT `mark_promoted()`'d** | A removal, not a new demotion — read criteria inverted from usual. Sampled evidence (n=300/300): p=0.435, fire WR=45.7% vs no-fire WR=46.3% — statistically indistinguishable, which is the supporting result for removing ribbon-opposition as a general-population F/D trigger. Full 43,344-row population backs the same conclusion. See [`project_ribbon_grading_fix_sep2026.md`](../../memory). |

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
