"""One-off: explicitly pre-register vbook_G_mechanical_reversion's shadow_mode
promotion bar BEFORE the book ever runs for real, so virtual_books.py's own
auto-registration (which uses generic defaults) becomes a no-op and never
silently overrides this deliberately-chosen bar.

min_n_fire/min_n_no_fire=30 is denominated in REGIMES now, not raw trades
(see BookConfig.regime_aware_promotion and _regime_dedup_allows_recording()
in src/virtual_books.py) -- a real, meaningfully harder bar in practice than
the same number would be for a per-row-counted rule, since the backtest
found regimes commonly run 5-20+ calendar days each.
"""
from src import shadow_mode

DESCRIPTION = (
    "Virtual book G: mechanical reversion (rib_against AND osc_agrees), no "
    "confidence floor, no dd_mode gate, no LLM call -- live pilot of the "
    "signal validated in the 2026-09-2X edge-mining research loop. "
    "IMPORTANT: n_fire/n_no_fire here count REGIMES, not raw trades -- see "
    "BookConfig.regime_aware_promotion in src/virtual_books.py. A raw "
    "per-trade count would badly overstate independent evidence: ribbon/ "
    "oscillator regimes ran up to 232 consecutive calendar days in the "
    "backtest, and a cluster-bootstrap re-analysis found the naive per-row "
    "test on rib_against alone (p=0.00015) was not actually significant "
    "once regime-clustering was honestly accounted for (p=0.052) -- while "
    "the combined rib_against+osc_agrees signal this book trades survived "
    "(cluster-bootstrap p=0.0060, effect +6.2pp WR). Discovery/holdout "
    "currency attribution for a BUY-only refinement was unstable, so this "
    "book deliberately trades both directions -- let real forward regime "
    "count answer that question rather than pre-baking an unresolved "
    "refinement into the rule."
)

result = shadow_mode.register_rule(
    "vbook_G_mechanical_reversion",
    description=DESCRIPTION,
    min_n_fire=30, min_n_no_fire=30,
)
print("Registered:", result["description"][:100], "...")
print("min_n_fire:", result["min_n_fire"], "min_n_no_fire:", result["min_n_no_fire"],
      "promoted:", result["promoted"])
