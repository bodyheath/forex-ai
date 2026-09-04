import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from src import shadow_mode as sm

df = pd.read_csv("data/historical_backtest_results.csv")
gbp_pairs = ["GBP/USD", "GBP/JPY", "GBP/CHF", "GBP/CAD", "GBP/AUD", "GBP/NZD", "EUR/GBP"]
chf_pairs = ["EUR/CHF", "NZD/CHF", "AUD/CHF"]
df["rib_still_relevant"] = df["pair"].isin(gbp_pairs + chf_pairs)

# The rule under evaluation: "ribbon-opposition, on its own, in the general
# (non-GBP/CHF) population, predicts failure" -- would_fire=True means the
# candidate WAS in the general ribbon-opposed population that used to be
# auto-demoted. This is a REMOVAL, not a new demotion: promotable here means
# the evidence shows NO significant difference (or a favorable one) between
# fire/no-fire, not the usual "fire is significantly worse" framing -- see
# the registration description and the interpretation note in this session's
# report for how to read would_fire_wr vs would_not_fire_wr for this rule.
sm.register_rule(
    "ribbon_general_population_demotion_removed",
    description=(
        "REMOVAL, not a new demotion (interpret criteria inverted from usual): "
        "would_fire=True means a non-GBP/CHF candidate with rib_strongly_against "
        "or rib_against (the population that used to be auto-demoted to F/D by "
        "ribbon opposition alone); would_fire=False means the non-opposed "
        "population. Backfilled from the 43,344-candidate, 3-year, 28-pair "
        "mechanical backtest (scripts/historical_grading_backtest.py). "
        "Evidence for removal: fire WR=46.2%/PF=0.993 vs no-fire (clean) "
        "WR=43.5%/PF=0.851 -- fire is NOT worse, if anything mildly better. "
        "GBP-crosses/CHF-cluster deliberately excluded from this rule's "
        "population -- they keep their own separate demotion, unchanged. "
        "See project_historical_backtest_sep2026.md and the daily.py comment "
        "at _rib_still_relevant for full detail."
    ),
    min_n_fire=100, min_n_no_fire=100, alpha=0.05,
)

fire = df[(~df["rib_still_relevant"]) & (df["rib_strongly_against"] | df["rib_against"])]
no_fire = df[(~df["rib_still_relevant"]) & (~df["rib_strongly_against"]) & (~df["rib_against"])]

n_logged = 0
for group, would_fire in ((fire, True), (no_fire, False)):
    for _, row in group.iterrows():
        sm.record_evaluation(
            "ribbon_general_population_demotion_removed",
            would_fire=would_fire,
            outcome=row["status"] if row["status"] in ("WIN", "LOSS") else (
                "WIN" if row["net_pips"] > 0 else "LOSS"
            ),
            net_pips=float(row["net_pips"]),
            context={"pair": row["pair"], "direction": row["direction"], "date": row["date"],
                     "backfilled": True, "source": "historical_grading_backtest"},
        )
        n_logged += 1

print(f"Backfilled {n_logged} evaluations ({len(fire)} fire / {len(no_fire)} no-fire)")

status = sm.check_promotion_readiness("ribbon_general_population_demotion_removed")
import json
print(json.dumps(status, indent=2))
