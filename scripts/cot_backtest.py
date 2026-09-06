"""Step 1 backtest for the Positioning Agent (2026-09-06).

Tests the classic COT contrarian hypothesis directly against real historical
price outcomes, before any live-agent code is written: after a currency's
large-speculator net positioning hits a statistical extreme (z-score vs its
own trailing history), does the pair it's part of actually move the
contrarian-predicted direction, at what lag, and how reliably.

============================================================================
DATA SOURCES -- CHECKED DIRECTLY, NOT ASSUMED
============================================================================
Two CFTC Socrata datasets were pulled and compared for the 8 currencies in
this system's 28-pair universe (EUR, GBP, JPY, AUD, CAD, CHF, NZD, USD):

  Legacy Futures-Only  (6dca-aqww, already the live positioning.py source)
    "Non-Commercial" long/short -- history back to 2000-08-29 for the
    currencies checked. Non-Commercial is a broad bucket (hedge funds, CTAs,
    and other large reportables all pooled together).

  Traders in Financial Futures (TFF), Futures-Only (gpe5-46if)
    History back to 2006-06-13 for the same currencies -- 6 fewer years.
    Breaks large speculators into Dealer/Asset Manager/Leveraged Funds/Other
    Reportables. "Leveraged Funds" (hedge funds, CTAs -- the trader class
    the contrarian thesis is actually about) is a materially cleaner match
    to "large speculator" than Legacy's broader Non-Commercial bucket.

  NOTE ON TERMINOLOGY: the user's brief says "legacy and disaggregated" --
  CFTC's actual "Disaggregated" report covers physical commodities only
  (agriculture/energy/metals) and has no currency markets at all; TFF is
  the report CFTC publishes for financial futures including currencies, so
  TFF is what was compared here as the intended "cleaner alternative".

  BOTH give 100% missing data for USD itself from 2022-02-01 onward -- the
  ICE U.S. Dollar Index futures contract's COT reporting stopped on that
  exact date in BOTH datasets (verified directly, not a Legacy-only gap).
  This is a real, permanent data limitation, not a bug: USD's own z-score
  is UNAVAILABLE for roughly the most recent 4.5 years of this backtest's
  window. Handled below by falling back to a single-currency (non-USD leg
  only) signal for USD-major pairs in that period, never by inventing a
  substitute value.

  DECISION: use Legacy Non-Commercial as the PRIMARY series (longer history,
  and it's what's already live in src/positioning.py -- if this backtest
  validates the hypothesis, Step 2's live check should use the same series
  that was actually tested). TFF Leveraged Funds is computed as a
  robustness cross-check on the overlapping 2006+ window and reported
  alongside, not as the primary result.

============================================================================
REPORTING LAG -- WHAT THIS LIMITS
============================================================================
Each COT report's report_date_as_yyyy_mm_dd is the Tuesday positions were
recorded, but the CFTC does not publish it until the following Friday
(historically 3:30pm ET) -- a real trader could not have acted on it any
sooner. Every forward-return window in this backtest starts from
report_date + 3 days (the Friday release), never from the Tuesday as-of
date, so the results reflect what was actually actionable, not a lookahead-
contaminated Tuesday-to-Tuesday return. This also means the signal is
inherently stale by 3-7 days versus current price action at the moment it
becomes available -- a live version of this check can only ever be reacting
to positioning that is the better part of a week old, never same-day fresh.

============================================================================
METHOD
============================================================================
1. Weekly net large-speculator position per currency = noncomm_long -
   noncomm_short (Legacy).
2. Rolling z-score at each week t: (net[t] - trailing_mean) / trailing_std,
   using a trailing window ending at t (never future data) of
   _Z_WINDOW_WEEKS weeks, requiring >= _Z_MIN_WEEKS of history first so
   early-sample z-scores aren't computed off a handful of points.
3. Per-pair contrarian signal at each week: signal = z_quote - z_base.
   Positive signal => quote currency more crowded-long (or base more
   crowded-short) than the other => contrarian expects quote to weaken
   relative to base => pair (BASE/QUOTE) price predicted to RISE.
   Negative signal => pair predicted to FALL. See module docstring above
   for the full derivation.
4. For a scan of thresholds and lags, bucket weeks into "extreme"
   (|signal| >= threshold) vs "non-extreme", and for the extreme bucket
   check whether the sign of the REAL forward return (real daily OHLC via
   the same yfinance-based fetch_daily() this repo's mass mechanical
   backtest already uses -- imported directly, not reimplemented) matches
   the signal's predicted direction. Reports hit rate, mean forward return,
   and a two-proportion z-test (src.shadow_mode._ztest, the same function
   already used for this system's other promotion-discipline checks)
   comparing the extreme bucket's hit rate against the non-extreme bucket's.

Same same-bar/lag conservatism as every other backtest this session: no
intraday path, so a lag window's forward return is just close-to-close
across real trading days -- no ambiguity to resolve since this isn't
testing a target/stop, just directional sign.

Usage: python scripts/cot_backtest.py
Writes data/cot_backtest_results.csv (one row per pair-week-threshold-lag
combination that qualified as "extreme") and prints the full report to
stdout.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd
import requests

import config
from src.selector import UNIVERSE
from src.shadow_mode import _ztest
import historical_grading_backtest as hgb

hgb.FETCH_PERIOD = "max"   # override the mass-backtest's 10y default -- COT history
                            # goes back to 2000, need matching price history depth

LEGACY_URL = config.COT_DATASET_URL
TFF_URL    = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"

CURRENCY_MARKETS = {
    ccy: meta["cot_market"]
    for ccy, meta in config.CURRENCIES.items()
    if meta.get("cot_market")
}  # EUR, GBP, JPY, AUD, CAD, CHF, NZD, USD

_Z_WINDOW_WEEKS = 156   # 3-year trailing window for the z-score
_Z_MIN_WEEKS    = 52    # require >= 1 year of trailing history before trusting a z
_USD_CUTOFF     = pd.Timestamp("2022-02-01")   # verified directly against both datasets

_THRESHOLDS = [1.0, 1.5, 2.0]
_LAG_WEEKS  = [1, 2, 4, 8]


def _fetch_cot(url: str, market_name: str) -> pd.DataFrame:
    escaped = market_name.replace("'", "''")
    resp = requests.get(url, params={
        "$where": f"market_and_exchange_names = '{escaped}'",
        "$order": "report_date_as_yyyy_mm_dd ASC",
        "$limit": 5000,
    }, timeout=30)
    resp.raise_for_status()
    return pd.DataFrame(resp.json())


def _legacy_net(df: pd.DataFrame) -> pd.Series:
    longs  = pd.to_numeric(df["noncomm_positions_long_all"], errors="coerce")
    shorts = pd.to_numeric(df["noncomm_positions_short_all"], errors="coerce")
    return longs - shorts


def build_currency_series() -> dict:
    """{ccy: DataFrame[date, net, z]} using Legacy Non-Commercial, full history."""
    out = {}
    for ccy, market in CURRENCY_MARKETS.items():
        print(f"[fetch] Legacy COT for {ccy} ({market})...")
        df = _fetch_cot(LEGACY_URL, market)
        if df.empty:
            print(f"  -> no rows, skipping {ccy}")
            continue
        df["date"] = pd.to_datetime(df["report_date_as_yyyy_mm_dd"])
        df["net"]  = _legacy_net(df)
        df = df[["date", "net"]].dropna().sort_values("date").reset_index(drop=True)
        # Drop USD's frozen tail entirely -- rows exist but are stale duplicates
        # of the last real reading, not real new information.
        if ccy == "USD":
            before = len(df)
            df = df[df["date"] < _USD_CUTOFF].reset_index(drop=True)
            print(f"  -> USD: dropped {before - len(df)} post-freeze rows "
                  f"(frozen from {_USD_CUTOFF.date()})")
        roll_mean = df["net"].rolling(_Z_WINDOW_WEEKS, min_periods=_Z_MIN_WEEKS).mean()
        roll_std  = df["net"].rolling(_Z_WINDOW_WEEKS, min_periods=_Z_MIN_WEEKS).std()
        df["z"] = (df["net"] - roll_mean) / roll_std
        df["publication_date"] = df["date"] + pd.Timedelta(days=3)  # Tue -> Fri release
        out[ccy] = df
        print(f"  -> {len(df)} weekly rows, {df['date'].min().date()} to {df['date'].max().date()}, "
              f"{df['z'].notna().sum()} with a computed z-score")
    return out


def build_tff_series() -> dict:
    """Same shape, Leveraged Funds net position, TFF report -- robustness check only."""
    out = {}
    for ccy, market in CURRENCY_MARKETS.items():
        if ccy == "USD":
            continue  # same frozen contract, already excluded from the primary result
        print(f"[fetch] TFF COT for {ccy} ({market})...")
        df = _fetch_cot(TFF_URL, market)
        if df.empty:
            continue
        df["date"] = pd.to_datetime(df["report_date_as_yyyy_mm_dd"])
        longs  = pd.to_numeric(df.get("lev_money_positions_long"), errors="coerce")
        shorts = pd.to_numeric(df.get("lev_money_positions_short"), errors="coerce")
        df["net"] = longs - shorts
        df = df[["date", "net"]].dropna().sort_values("date").reset_index(drop=True)
        roll_mean = df["net"].rolling(_Z_WINDOW_WEEKS, min_periods=_Z_MIN_WEEKS).mean()
        roll_std  = df["net"].rolling(_Z_WINDOW_WEEKS, min_periods=_Z_MIN_WEEKS).std()
        df["z"] = (df["net"] - roll_mean) / roll_std
        df["publication_date"] = df["date"] + pd.Timedelta(days=3)
        out[ccy] = df
    return out


def build_pair_signal(pair: str, currency_z: dict) -> pd.DataFrame:
    base, quote = pair.split("/")
    if base not in currency_z or quote not in currency_z:
        return pd.DataFrame()
    b = currency_z[base][["publication_date", "z"]].rename(columns={"z": "z_base"})
    q = currency_z[quote][["publication_date", "z"]].rename(columns={"z": "z_quote"})
    merged = pd.merge(b, q, on="publication_date", how="inner")
    merged = merged.dropna(subset=["z_base", "z_quote"], how="all")
    # If exactly one leg is USD post-freeze (NaN), fall back to a single-leg
    # signal using only the available currency instead of dropping the row.
    merged["z_base"]  = merged["z_base"]
    merged["z_quote"] = merged["z_quote"]
    both = merged["z_base"].notna() & merged["z_quote"].notna()
    only_base  = merged["z_base"].notna() & merged["z_quote"].isna()
    only_quote = merged["z_quote"].notna() & merged["z_base"].isna()
    merged["signal"] = np.nan
    merged.loc[both, "signal"]       = merged.loc[both, "z_quote"] - merged.loc[both, "z_base"]
    merged.loc[only_base, "signal"]  = -merged.loc[only_base, "z_base"]     # only base info: contrarian on base alone
    merged.loc[only_quote, "signal"] = merged.loc[only_quote, "z_quote"]    # only quote info: contrarian on quote alone
    merged["single_leg"] = only_base | only_quote
    merged = merged.dropna(subset=["signal"])
    merged["pair"] = pair
    return merged[["pair", "publication_date", "signal", "single_leg"]]


def forward_returns(price_df: pd.DataFrame, pub_dates: pd.Series, lag_weeks: int) -> pd.Series:
    """Close-to-close pip return from the first trading day AT/AFTER pub_date
    to the first trading day AT/AFTER pub_date + lag_weeks*7 days."""
    idx = price_df.index
    closes = price_df["close"]
    out = {}
    for pd_ in pub_dates.unique():
        pos0 = idx.searchsorted(pd_)
        pos1 = idx.searchsorted(pd_ + pd.Timedelta(days=lag_weeks * 7))
        if pos0 >= len(idx) or pos1 >= len(idx):
            continue
        out[pd_] = closes.iloc[pos1] - closes.iloc[pos0]
    return pub_dates.map(out)


def run():
    currency_z = build_currency_series()
    tff_z = build_tff_series()

    all_signals = []
    price_cache = {}
    for pair in UNIVERSE:
        sig = build_pair_signal(pair, currency_z)
        if sig.empty:
            print(f"[signal] {pair}: no overlapping COT history, skipping")
            continue
        print(f"[fetch] price history for {pair}...")
        try:
            px = hgb.fetch_daily(pair)
        except Exception as exc:
            print(f"  -> price fetch failed: {exc}")
            continue
        if px.empty:
            print(f"  -> empty price history, skipping")
            continue
        px = px.sort_index()
        price_cache[pair] = px
        for lag in _LAG_WEEKS:
            sig[f"fwd_ret_{lag}w"] = forward_returns(px, sig["publication_date"], lag)
        all_signals.append(sig)
        time.sleep(0.3)  # be polite to yfinance

    if not all_signals:
        print("NO SIGNALS BUILT -- aborting.")
        return

    full = pd.concat(all_signals, ignore_index=True)
    full.to_csv("data/cot_backtest_signal_detail.csv", index=False)
    print(f"\nTotal pair-weeks with a signal: {len(full)} across {full['pair'].nunique()} pairs")
    print(f"Of which single-leg (one currency's COT unavailable, usually post-2022 USD): "
          f"{int(full['single_leg'].sum())} ({full['single_leg'].mean()*100:.1f}%)")

    results = []
    print("\n" + "=" * 100)
    print("AGGREGATE RESULTS (all pairs pooled)")
    print("=" * 100)
    for lag in _LAG_WEEKS:
        col = f"fwd_ret_{lag}w"
        for thr in _THRESHOLDS:
            extreme = full[full["signal"].abs() >= thr].dropna(subset=[col])
            non_extreme = full[full["signal"].abs() < thr].dropna(subset=[col])
            if len(extreme) < 10:
                continue
            ext_hits = (np.sign(extreme[col]) == np.sign(extreme["signal"])).sum()
            non_hits = (np.sign(non_extreme[col]) == np.sign(non_extreme["signal"])).sum()
            z = _ztest(int(ext_hits), len(extreme), int(non_hits), len(non_extreme))
            p_value, wr_ext, wr_non = z if z else (None, None, None)
            mean_ret = extreme[col].mean()
            print(f"lag={lag}w thr={thr:.1f}  n_extreme={len(extreme):5d}  "
                  f"hit_rate={wr_ext*100 if wr_ext is not None else float('nan'):5.1f}%  "
                  f"(non-extreme={wr_non*100 if wr_non is not None else float('nan'):5.1f}%, "
                  f"n={len(non_extreme)})  mean_fwd_pips_raw={mean_ret:8.5f}  "
                  f"p={p_value if p_value is not None else float('nan'):.4f}")
            results.append({
                "scope": "aggregate", "pair": "ALL", "lag_weeks": lag, "threshold": thr,
                "n_extreme": len(extreme), "n_non_extreme": len(non_extreme),
                "hit_rate_extreme": wr_ext, "hit_rate_non_extreme": wr_non,
                "mean_fwd_return_raw": mean_ret, "p_value": p_value,
            })

    print("\n" + "=" * 100)
    print("PER-PAIR RESULTS (threshold=1.5, lag=4w -- representative slice)")
    print("=" * 100)
    thr, lag = 1.5, 4
    col = f"fwd_ret_{lag}w"
    for pair in sorted(full["pair"].unique()):
        sub = full[full["pair"] == pair]
        extreme = sub[sub["signal"].abs() >= thr].dropna(subset=[col])
        non_extreme = sub[sub["signal"].abs() < thr].dropna(subset=[col])
        if len(extreme) < 8:
            continue
        ext_hits = (np.sign(extreme[col]) == np.sign(extreme["signal"])).sum()
        non_hits = (np.sign(non_extreme[col]) == np.sign(non_extreme["signal"])).sum()
        z = _ztest(int(ext_hits), len(extreme), int(non_hits), len(non_extreme))
        p_value, wr_ext, wr_non = z if z else (None, None, None)
        print(f"{pair:10s} n_extreme={len(extreme):4d}  hit_rate={wr_ext*100:5.1f}%  "
              f"(non-extreme={wr_non*100:5.1f}%, n={len(non_extreme)})  "
              f"p={p_value:.4f}" if p_value is not None else f"{pair:10s} n_extreme={len(extreme):4d} (insufficient non-extreme baseline)")
        results.append({
            "scope": "per_pair", "pair": pair, "lag_weeks": lag, "threshold": thr,
            "n_extreme": len(extreme), "n_non_extreme": len(non_extreme),
            "hit_rate_extreme": wr_ext, "hit_rate_non_extreme": wr_non,
            "mean_fwd_return_raw": extreme[col].mean(), "p_value": p_value,
        })

    pd.DataFrame(results).to_csv("data/cot_backtest_results.csv", index=False)
    print("\nWrote data/cot_backtest_results.csv and data/cot_backtest_signal_detail.csv")


if __name__ == "__main__":
    run()
