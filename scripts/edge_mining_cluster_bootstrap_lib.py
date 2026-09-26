"""Shared regime-cluster + cluster-bootstrap machinery for the mechanical
edge-mining research loop, factored out so phenomena beyond the ribbon
signal (Book G) can be put through the EXACT SAME rigor without
re-deriving it by hand each time.

This is a direct port of src/shadow_mode.py's _cluster_bootstrap_stats()
(the live, production cluster-bootstrap used by Book G's shadow_mode
promotion check) onto historical backtest rows instead of live shadow-mode
evaluation records -- same resampling math, same regime-run concept,
verified to produce the same qualitative conclusion phenomenon 1's own
holdout stress-test already found (rib_against alone: does NOT survive;
rib_against AND osc_agrees: DOES survive) before being trusted on new data
(see scripts/edge_mining_cluster_bootstrap_stress_test.py's reconfirmation
section).

GAP TOLERANCE: 3 calendar days. Verified here (not assumed) to be a
property of THIS DATASET'S TRADING CALENDAR, not something that needs
re-deriving per condition -- data/mechanical_edge_mining_dataset.csv is one
row per (pair, weekday, direction), so date-to-date gaps for a fixed pair
are governed entirely by the trading week (gap=1 the overwhelming
majority, gap=3 the normal Friday->Monday weekend, gap=2/5 rare holiday
adjustments) -- confirmed empirically (EUR/USD: gap=1 n=615, gap=3 n=154,
gap=2 n=3, gap=5 n=1). This is the same 3-day figure already validated for
the ribbon regime in src/virtual_books.py's _REGIME_GAP_DAYS, and applies
identically to any OTHER condition built from this same daily-bar dataset,
since the gap is about the calendar, not about how fast any one condition's
truth value happens to change.
"""
import random
from math import erf, sqrt

import pandas as pd

REGIME_GAP_DAYS = 3


def assign_regime_clusters(df: pd.DataFrame, condition: pd.Series,
                            group_cols=("pair", "direction"),
                            gap_days: int = REGIME_GAP_DAYS) -> pd.Series:
    """Assigns a cluster id to every row of df, where a cluster is a
    maximal run of consecutive-in-time (within gap_days) rows sharing the
    SAME group (e.g. pair+direction) AND the SAME boolean value of
    `condition` -- exactly the "regime" concept src/virtual_books.py's
    _regime_cluster_tag() uses for Book G, generalised to work directly on
    a full historical dataframe instead of one live evaluation at a time.

    Returns a Series of string cluster ids, same index as df.
    """
    work = pd.DataFrame({
        "grp": list(zip(*[df[c] for c in group_cols])),
        "date": pd.to_datetime(df["date"]),
        "cond": condition.astype(bool).values,
    }, index=df.index)

    cluster_ids = pd.Series(index=df.index, dtype=object)
    for grp_key, grp_df in work.groupby("grp", sort=False):
        grp_df = grp_df.sort_values("date")
        regime_num = 0
        last_date = None
        last_cond = None
        for idx, row in grp_df.iterrows():
            gap_ok = (
                last_date is not None
                and row["cond"] == last_cond
                and (row["date"] - last_date).days <= gap_days
            )
            if not gap_ok:
                regime_num += 1
            cluster_ids.loc[idx] = f"{grp_key}_{row['cond']}_r{regime_num}"
            last_date = row["date"]
            last_cond = row["cond"]
    return cluster_ids


def cluster_bootstrap_p_value(fire_df: pd.DataFrame, nofire_df: pd.DataFrame,
                               fire_clusters: pd.Series, nofire_clusters: pd.Series,
                               win_col_mask_fire: pd.Series, win_col_mask_nofire: pd.Series,
                               n_boot: int = 2000, seed: int = 42):
    """Direct port of src/shadow_mode.py's _cluster_bootstrap_stats() onto
    plain pandas Series instead of shadow_mode evaluation-dict lists.
    Resamples CLUSTER IDS (not rows) with replacement, preserving each
    cluster's real internal size, computing an empirical two-sided p-value
    AND a 95% percentile CI for the fire-vs-nofire win-rate difference,
    both from the SAME bootstrap draws (never a separate re-run with a
    different seed, so the reported p-value and CI are always mutually
    consistent).

    Returns a dict: p_value, ci_low, ci_high (in WR-fraction units, e.g.
    0.062 = +6.2pp), n_clusters_fire, n_clusters_nofire, n_rows_fire,
    n_rows_nofire, wr_fire, wr_nofire -- or None if either side has no
    clusters.
    """
    def _cluster_map(clusters, wins):
        out = {}
        for cid, w in zip(clusters, wins):
            wn = out.setdefault(cid, [0, 0])
            wn[0] += int(bool(w))
            wn[1] += 1
        return out

    fire_map = _cluster_map(fire_clusters.values, win_col_mask_fire.values)
    nofire_map = _cluster_map(nofire_clusters.values, win_col_mask_nofire.values)
    fire_ids = list(fire_map.keys())
    nofire_ids = list(nofire_map.keys())
    if not fire_ids or not nofire_ids:
        return None

    total_fire_w = sum(w for w, n in fire_map.values())
    total_fire_n = sum(n for w, n in fire_map.values())
    total_nofire_w = sum(w for w, n in nofire_map.values())
    total_nofire_n = sum(n for w, n in nofire_map.values())
    if total_fire_n == 0 or total_nofire_n == 0:
        return None
    wr_fire = total_fire_w / total_fire_n
    wr_nofire = total_nofire_w / total_nofire_n

    rng = random.Random(seed)
    diffs = []
    for _ in range(n_boot):
        f_sample = [rng.choice(fire_ids) for _ in range(len(fire_ids))]
        nf_sample = [rng.choice(nofire_ids) for _ in range(len(nofire_ids))]
        fw = sum(fire_map[k][0] for k in f_sample)
        fn = sum(fire_map[k][1] for k in f_sample)
        nfw = sum(nofire_map[k][0] for k in nf_sample)
        nfn = sum(nofire_map[k][1] for k in nf_sample)
        if fn == 0 or nfn == 0:
            continue
        diffs.append(fw / fn - nfw / nfn)
    if not diffs:
        return None
    n_valid = len(diffs)
    frac_le_0 = sum(1 for d in diffs if d <= 0) / n_valid
    frac_ge_0 = sum(1 for d in diffs if d >= 0) / n_valid
    p_value = min(2 * min(frac_le_0, frac_ge_0), 1.0)
    return p_value, len(fire_ids), len(nofire_ids), wr_fire, wr_nofire


def cluster_bootstrap_for_condition(df: pd.DataFrame, condition: pd.Series,
                                     group_cols=("pair", "direction"),
                                     gap_days: int = REGIME_GAP_DAYS,
                                     n_boot: int = 2000, seed: int = 42):
    """Convenience wrapper: given a full (decisive-only, WIN/LOSS) dataframe
    and a boolean condition Series aligned to it, assigns regime clusters
    and runs the cluster bootstrap. Returns the same tuple as
    cluster_bootstrap_p_value(), or None."""
    condition = condition.reindex(df.index).fillna(False)
    clusters = assign_regime_clusters(df, condition, group_cols=group_cols, gap_days=gap_days)
    is_win = (df["net_pips"] > 0)
    fire_mask = condition
    nofire_mask = ~condition
    return cluster_bootstrap_p_value(
        df[fire_mask], df[nofire_mask],
        clusters[fire_mask], clusters[nofire_mask],
        is_win[fire_mask], is_win[nofire_mask],
        n_boot=n_boot, seed=seed,
    )


def regime_length_stats(df: pd.DataFrame, condition: pd.Series,
                         group_cols=("pair", "direction"),
                         gap_days: int = REGIME_GAP_DAYS) -> dict:
    """Diagnostic: how long are this condition's TRUE-side regimes, in raw
    row count per cluster? Same diligence applied to phenomenon 1 (ribbon
    regimes ran up to 232 days) -- report before trusting a cluster count,
    since a condition with mostly-singleton clusters needs far less
    correction than one with a few enormous ones."""
    condition = condition.reindex(df.index).fillna(False)
    clusters = assign_regime_clusters(df, condition, group_cols=group_cols, gap_days=gap_days)
    fire_clusters = clusters[condition]
    sizes = fire_clusters.value_counts()
    return {
        "n_clusters": len(sizes),
        "n_rows": int(condition.sum()),
        "max_cluster_size": int(sizes.max()) if len(sizes) else 0,
        "mean_cluster_size": float(sizes.mean()) if len(sizes) else 0.0,
        "median_cluster_size": float(sizes.median()) if len(sizes) else 0.0,
    }
