"""Investigation (2026-09-07): regime-classifier grouping vs real pairwise
correlation, for replacing risk_manager.py's literal currency-code check.

Requested: before picking an approach, report REAL numbers on how differently
the current real portfolio's open positions (or, when none are open, the
historical record of same-day multi-pair candidates) would have been gated
under (a) the existing literal currency-code check, (b) a regime-classifier
risk-on/risk-off grouping applied to the same pairwise architecture, and
(c) genuine pairwise correlation computed from real historical returns.

Read-only. Does not change any live logic. Fetches real daily OHLC via
yfinance for all 28 UNIVERSE pairs (same fetch pattern as
scripts/historical_grading_backtest.py), caches to
data/correlation_price_cache.csv so repeat runs don't re-hit the network.

Usage: python scripts/correlation_investigation.py
"""
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from src.selector import UNIVERSE
from src.market_regime import _RISK_ON_CCYS, _RISK_OFF_CCYS

CACHE_PATH = Path("data/correlation_price_cache.csv")
FETCH_PERIOD = "3y"   # matches historical_grading_backtest.py's EVAL_YEARS window


def _yf_ticker(pair: str) -> str:
    return pair.replace("/", "") + "=X"


def fetch_all_closes() -> pd.DataFrame:
    """Return a DataFrame indexed by date, one column per pair, of daily closes."""
    if CACHE_PATH.exists():
        cached = pd.read_csv(CACHE_PATH, index_col=0, parse_dates=True)
        if set(UNIVERSE).issubset(set(cached.columns)):
            print(f"[cache] using {CACHE_PATH} ({len(cached)} rows)")
            return cached[UNIVERSE]

    import yfinance as yf
    closes = {}
    for pair in UNIVERSE:
        df = yf.download(_yf_ticker(pair), period=FETCH_PERIOD, interval="1d",
                          progress=False, auto_adjust=False)
        if df.empty:
            print(f"  [warn] no data for {pair}")
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [str(c).lower() for c in df.columns]
        closes[pair] = df["close"]
        print(f"  fetched {pair}: {len(df)} bars")

    out = pd.DataFrame(closes)
    out.to_csv(CACHE_PATH)
    return out


def build_correlation_matrix(closes: pd.DataFrame) -> pd.DataFrame:
    rets = np.log(closes / closes.shift(1)).dropna(how="all")
    return rets.corr(min_periods=200)


def _currency_exposure(pair: str, direction: str) -> dict:
    clean = pair.upper().replace("/", "")
    base, quote = clean[:3], clean[3:6]
    if direction.upper() == "BUY":
        return {base: "long", quote: "short"}
    return {base: "short", quote: "long"}


def literal_check(pair1, dir1, pair2, dir2) -> bool:
    exp1 = _currency_exposure(pair1, dir1)
    exp2 = _currency_exposure(pair2, dir2)
    return any(exp1.get(c) == exp2.get(c) for c in exp1)


def regime_group_check(pair1, dir1, pair2, dir2) -> bool:
    """Would-be regime-classifier-based check: flag correlated if the net
    directional currency-basket exposure (risk-on count minus risk-off count,
    sign-adjusted for direction) points the same way for both trades. This is
    the most natural way to stretch the existing binary risk-on/risk-off sets
    into a pairwise same-direction-risk test."""
    def basket_score(pair, direction):
        exp = _currency_exposure(pair, direction)
        score = 0
        for ccy, pos in exp.items():
            sign = 1 if pos == "long" else -1
            if ccy in _RISK_ON_CCYS:
                score += sign
            elif ccy in _RISK_OFF_CCYS:
                score -= sign
        return score
    s1, s2 = basket_score(pair1, dir1), basket_score(pair2, dir2)
    return s1 != 0 and s2 != 0 and (s1 > 0) == (s2 > 0)


def real_correlation_check(pair1, dir1, pair2, dir2, corr_matrix, threshold=0.60) -> tuple:
    """Direction-adjusted position-return correlation. Returns (flagged, eff_corr)."""
    if pair1 not in corr_matrix.columns or pair2 not in corr_matrix.columns:
        return (None, None)
    raw_corr = corr_matrix.loc[pair1, pair2]
    if pd.isna(raw_corr):
        return (None, None)
    sign1 = 1 if dir1.upper() == "BUY" else -1
    sign2 = 1 if dir2.upper() == "BUY" else -1
    eff_corr = raw_corr * sign1 * sign2
    return (eff_corr >= threshold, eff_corr)


def part1_compare_groupings(corr):
    print(f"\n{'='*90}\nPART 1: regime-grouping vs real correlation -- group coherence check\n{'='*90}")
    print(f"Regime classifier's binary split: risk-on={_RISK_ON_CCYS}  risk-off={_RISK_OFF_CCYS}")
    print("(8 currencies -> 2 buckets; no magnitude, no pairwise structure)\n")

    # For every pair of UNIVERSE pairs, compute real |corr| and whether the
    # regime-group check (same-direction-BUY basket score) would call it
    # "same regime bucket" -- i.e. how often does the coarse 2-bucket split
    # agree with what real correlation says?
    rows = []
    pairs = [p for p in UNIVERSE if p in corr.columns]
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            p1, p2 = pairs[i], pairs[j]
            c = corr.loc[p1, p2]
            if pd.isna(c):
                continue
            same_bucket = regime_group_check(p1, "BUY", p2, "BUY")
            rows.append({"pair1": p1, "pair2": p2, "real_corr": c, "regime_says_correlated": same_bucket})
    df = pd.DataFrame(rows)

    high_corr = df[df["real_corr"].abs() >= 0.60]
    low_corr = df[df["real_corr"].abs() < 0.30]
    print(f"Of {len(df)} pair-combinations with valid real correlation:")
    print(f"  {len(high_corr)} have |real_corr| >= 0.60 (genuinely highly correlated)")
    print(f"    -> regime-group check agrees (flags correlated) on {high_corr['regime_says_correlated'].sum()}/{len(high_corr)} "
          f"({high_corr['regime_says_correlated'].mean()*100:.1f}%)")
    print(f"  {len(low_corr)} have |real_corr| < 0.30 (genuinely near-uncorrelated)")
    print(f"    -> regime-group check WRONGLY flags correlated on {low_corr['regime_says_correlated'].sum()}/{len(low_corr)} "
          f"({low_corr['regime_says_correlated'].mean()*100:.1f}%) [false positives]")

    print(f"\n{'-'*90}\nWorked examples -- pairs the regime-group check gets wrong:\n{'-'*90}")
    # Highest real correlation the regime-group check MISSES (false negative)
    missed = df[~df["regime_says_correlated"]].reindex(df[~df["regime_says_correlated"]]["real_corr"].abs().sort_values(ascending=False).index)
    print("Highest real correlation the regime-group check would MISS (false negative):")
    for _, r in missed.head(5).iterrows():
        print(f"  {r['pair1']:9s} vs {r['pair2']:9s}  real_corr={r['real_corr']:+.3f}  regime_check={r['regime_says_correlated']}")

    falsepos = df[(df["regime_says_correlated"]) & (df["real_corr"].abs() < 0.30)]
    falsepos = falsepos.reindex(falsepos["real_corr"].abs().sort_values().index)
    print("\nLowest real correlation the regime-group check would WRONGLY FLAG (false positive):")
    for _, r in falsepos.head(5).iterrows():
        print(f"  {r['pair1']:9s} vs {r['pair2']:9s}  real_corr={r['real_corr']:+.3f}  regime_check={r['regime_says_correlated']}")

    return df


def part1b_open_positions_check(corr):
    print(f"\n{'='*90}\nPART 1b: real current open positions, gated under each approach\n{'='*90}")
    df = pd.read_csv("data/trades.csv")
    fund = df[df["system_version"] == "v2"]
    open_rows = fund[fund["status"].isin(["OPEN", "PENDING"])]
    print(f"Real fund (v2) currently-open/pending trades: {len(open_rows)}")
    if open_rows.empty:
        print("None open right now -- nothing to gate live. Falling back to the historical")
        print("record (Part 2) to validate against real same-day multi-pair decisions instead.")
        return


def part2_backtest_historical_gating():
    print(f"\n{'='*90}\nPART 2: backtest -- would gating decisions have changed historically?\n{'='*90}")
    hist = pd.read_csv("data/historical_backtest_results.csv")
    hist = hist[hist["bucket"] != "would_F"].copy()
    corr = pd.read_csv("data/correlation_matrix.csv", index_col=0) if Path("data/correlation_matrix.csv").exists() else None
    return hist, corr


if __name__ == "__main__":
    print("Fetching/loading real daily closes for all 28 UNIVERSE pairs...")
    closes = fetch_all_closes()
    print(f"\nBuilding correlation matrix from {len(closes)} days of real returns...")
    corr = build_correlation_matrix(closes)
    corr.to_csv("data/correlation_matrix.csv")
    print(f"Saved matrix to data/correlation_matrix.csv")

    part1_compare_groupings(corr)
    part1b_open_positions_check(corr)
