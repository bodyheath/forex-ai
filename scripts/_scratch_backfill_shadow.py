import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from src import shadow_mode as sm

df = pd.read_csv('data/research_trades.csv', encoding='utf-8-sig')
status_u = df['status'].astype(str).str.upper()
closed_dt = pd.to_datetime(df['closed_at'], errors='coerce', utc=True)
cutoff = pd.Timestamp('2026-07-14 13:46:31', tz='UTC')

# ── Ribbon-regime carve-out ──────────────────────────────────────────────
sm.register_rule(
    "ribbon_carveout_exclude_trending_risk_on",
    description=("Exclude trending_risk_on from the ribbon-only-relief carve-out "
                  "(_ribbon_only_f_or_d in daily.py) -- pre-registered 2026-08-30, "
                  "see project_ribbon_regime_carveout_threshold.md. would_fire=True means "
                  "trending_risk_on (the proposed exclusion would apply, sending this "
                  "candidate back to its original F/D grade instead of the C rescue); "
                  "would_fire=False means trending_risk_off (direct comparison only -- "
                  "ranging_low_vol is deliberately excluded from this shadow rule, matching "
                  "the frozen criteria's own choice not to pool it in, which is exactly what "
                  "manufactured the original false-positive significance)."),
    min_n_fire=100, min_n_no_fire=40, alpha=0.05, pf_max_fire=0.80,
)

decisive_mask = (status_u.isin(['WIN','FULL_WIN','PARTIAL_WIN','LOSS'])
                  & (df['system_version']=='v2') & (closed_dt >= cutoff))
d = df[decisive_mask].copy()
direction_u = d['direction'].astype(str).str.upper()
ribbon = d['ribbon_state'].astype(str)
rib_against = (((direction_u=='BUY') & ribbon.isin(['ALIGNED_BEAR','LEANING_BEAR']))
               | ((direction_u=='SELL') & ribbon.isin(['ALIGNED_BULL','LEANING_BULL'])))
pair_u = d['pair'].astype(str).str.upper()
is_gbp = pair_u.str.split('/').apply(lambda parts: 'GBP' in parts if isinstance(parts, list) else False)
is_chf_cluster = pair_u.isin(['EUR/CHF','NZD/CHF','AUD/CHF'])
conf = pd.to_numeric(d['confidence'], errors='coerce')
elig = d[rib_against & (~is_gbp) & (~is_chf_cluster) & (conf>=6)
         & d['market_regime'].isin(['trending_risk_off','trending_risk_on'])].copy()
elig['net_pips_f'] = pd.to_numeric(elig['net_pips'], errors='coerce')
elig['net_pips_f'] = elig['net_pips_f'].fillna(pd.to_numeric(elig['pips'], errors='coerce'))

n_logged = 0
for _, row in elig.iterrows():
    would_fire = (row['market_regime'] == 'trending_risk_on')
    net_pips = row['net_pips_f']
    net_pips = float(net_pips) if pd.notna(net_pips) else None
    sm.record_evaluation(
        "ribbon_carveout_exclude_trending_risk_on",
        would_fire=would_fire,
        outcome=str(row['status']).upper(),
        net_pips=net_pips,
        context={"pair": row['pair'], "direction": row['direction'],
                 "market_regime": row['market_regime'], "closed_at": str(row['closed_at']),
                 "backfilled": True},
    )
    n_logged += 1
print(f"ribbon carve-out: backfilled {n_logged} evaluations")

# ── VIX-regime-edge ──────────────────────────────────────────────────────
sm.register_rule(
    "vix_regime_edge_trending_risk_on",
    description=("Within trending_risk_on only, vix_vs_20d_avg above outperforms below -- "
                  "pre-registered 2026-08-31, see project_vix_regime_edge_threshold.md. "
                  "would_fire=True means vix_above (the hypothesized WINNER side); "
                  "would_fire=False means vix_below. Discovery sample (closed_at<=2026-08-31, "
                  "n=127/370) is DELIBERATELY NOT backfilled here -- it must never count toward "
                  "promotion per the frozen walk-forward discipline. Only new-data-only "
                  "evaluations (closed_at>2026-08-31) are logged below."),
    min_n_fire=30, min_n_no_fire=30, alpha=0.05, pf_min_fire=0.70, pf_min_gap=0.30,
)

freeze = pd.Timestamp('2026-08-31', tz='UTC')
strict_mask = (status_u.isin(['WIN','FULL_WIN','LOSS']) & (df['system_version']=='v2')
               & (closed_dt >= cutoff) & (closed_dt > freeze)
               & (df['market_regime'] == 'trending_risk_on'))
v = df[strict_mask].copy()
v['net_pips_f'] = pd.to_numeric(v['net_pips'], errors='coerce')
v['net_pips_f'] = v['net_pips_f'].fillna(pd.to_numeric(v['pips'], errors='coerce'))
v = v[v['vix_vs_20d_avg'].isin([1.0, -1.0])]

n_logged2 = 0
for _, row in v.iterrows():
    would_fire = (float(row['vix_vs_20d_avg']) == 1.0)
    net_pips = row['net_pips_f']
    net_pips = float(net_pips) if pd.notna(net_pips) else None
    sm.record_evaluation(
        "vix_regime_edge_trending_risk_on",
        would_fire=would_fire,
        outcome=str(row['status']).upper(),
        net_pips=net_pips,
        context={"pair": row['pair'], "direction": row['direction'],
                 "vix_vs_20d_avg": row['vix_vs_20d_avg'], "closed_at": str(row['closed_at']),
                 "backfilled": True, "post_freeze_new_data": True},
    )
    n_logged2 += 1
print(f"vix-regime-edge: backfilled {n_logged2} evaluations (new-data-only, post-2026-08-31)")
