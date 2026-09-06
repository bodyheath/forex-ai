"""Step 1 backtest for the Carry/Macro Agent (2026-09-06), Phase 01B's second
specialist. Mirrors scripts/cot_backtest.py's structure and discipline
exactly: real historical data, the real production price-history loader,
honest sample size / effect size, two independent rate data sources checked
against each other, explicit multiple-comparisons awareness across the
28-pair scan.

============================================================================
DATA SOURCES -- TWO CHECKED, NOT ONE TRUSTED BLINDLY
============================================================================
  Policy rate (config.CURRENCIES[ccy]["rate_fred"])
    Central-bank / short-term policy rate already used live by
    src/fundamental.py's rate-differential calc -- USD/GBP series are daily
    (DFF, IUDSOIA), the other 6 currencies are monthly 3-month-interbank-or-
    equivalent FRED/OECD series. History depth varies by series (checked
    directly, see report).

  10-year government bond yield (IRLTLT01<CC>M156N per currency, DGS10 for
  USD -- OECD long-term-rate series via FRED, monthly, checked directly to
  resolve for all 8 currencies before use)
    The user's brief asked for 2-year yields too "if reasonably obtainable
    free" -- checked directly: FRED has a clean 2-year series only for the
    US (DGS2); no equivalent clean 2-year sovereign series exists for the
    other 7 currencies. 10-year is the one yield tenor genuinely obtainable
    and comparable across all 8 currencies, so it's what's used as the
    second, independent signal.

Both series are pulled via a new src.fred.history() function (added to the
existing production src/fred.py client, which previously only supported a
60-point "latest" snapshot -- extending, not reimplementing, the same
pattern used for src.positioning's config.CURRENCIES mapping in Step 1).

============================================================================
PUBLICATION LAG -- DIFFERENT CHARACTER THAN COT'S, STATED PLAINLY
============================================================================
Central-bank policy rates are public knowledge the moment they're set --
there's no CFTC-style "reported a week late" delay for the two daily series
(USD, GBP). But the six monthly interbank/yield series are the kind of
macro data that typically posts to FRED with a real reporting lag behind
the reference month (OECD-sourced series are usually available only 3-5
weeks after month-end). To avoid lookahead bias on those, every monthly-
frequency series (all 6 non-USD/GBP policy rates, and all 8 of the 10Y
yield series including USD's) is lagged by _MONTHLY_LAG_DAYS before being
treated as "known" on a given date. The two daily policy-rate series
(USD, GBP) are used with no artificial lag.

Forward-filling a monthly series onto a weekly grid does NOT create new
information -- a rate that only actually changes once a month will show
the same value for 4-5 consecutive "weekly" observations. The trend window
below is chosen in months' worth of weeks specifically so this is a real
month-over-month comparison, not a weekly one dressed up to look more
granular than the underlying data supports.

============================================================================
METHOD
============================================================================
1. Weekly-resampled, forward-filled rate/yield series per currency (daily
   series resampled down, monthly series held flat between publications).
2. differential(pair, t) = rate_base(t) - rate_quote(t).
3. z(t) = z-score of differential(t) vs its own trailing _Z_WINDOW_WEEKS
   history (>= _Z_MIN_WEEKS required first) -- same construction as Step 1's
   positioning z-score, for direct methodological consistency.
4. trend(t) = differential(t) - differential(t - _TREND_LOOKBACK_WEEKS)
   (~3 months of weeks) -- the "widening" the classic carry hypothesis is
   actually about, not just a high static level.
5. Signal fires (extreme AND widening) when |z(t)| >= threshold AND
   sign(trend(t)) == sign(z(t)) -- i.e. the already-large differential is
   still growing in the same direction, not flattening or reversing.
   Predicted direction: sign(z(t)) positive => carry favors BASE => pair
   (BASE/QUOTE) predicted to drift UP; negative => predicted DOWN.
6. For a scan of thresholds and lags, compare the "fires" bucket's forward-
   return hit rate against the "doesn't fire" bucket's, real OHLC via
   historical_grading_backtest.fetch_daily() (imported, not reimplemented),
   two-proportion z-test via src.shadow_mode._ztest (same function Step 1
   and this system's other promotion-discipline checks already use).

Reports BOTH data sources' aggregate results, and the PER-PAIR breakdown at
a representative threshold/lag (not just aggregate) specifically so a
handful of pairs clearing p<0.05 by chance among 28 tested at once doesn't
get mistaken for a real finding -- exactly the trap Step 1 caught.

Usage: python scripts/carry_backtest.py
Writes data/carry_backtest_results.csv and data/carry_backtest_signal_detail.csv.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

import config
from src import fred
from src.selector import UNIVERSE
from src.shadow_mode import _ztest
import historical_grading_backtest as hgb

hgb.FETCH_PERIOD = "max"

_POLICY_RATE_SERIES = {ccy: meta["rate_fred"] for ccy, meta in config.CURRENCIES.items()
                       if meta.get("rate_fred")}
# 8-currency universe only (excludes NOK/SEK/SGD/HKD etc also in config.CURRENCIES)
_UNIVERSE_CCY = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"}
_POLICY_RATE_SERIES = {k: v for k, v in _POLICY_RATE_SERIES.items() if k in _UNIVERSE_CCY}

_DAILY_POLICY_CCY = {"USD", "GBP"}   # DFF, IUDSOIA -- true daily series, no lag needed

_YIELD10Y_SERIES = {
    "USD": "DGS10", "EUR": "IRLTLT01DEM156N", "GBP": "IRLTLT01GBM156N",
    "JPY": "IRLTLT01JPM156N", "AUD": "IRLTLT01AUM156N", "CAD": "IRLTLT01CAM156N",
    "CHF": "IRLTLT01CHM156N", "NZD": "IRLTLT01NZM156N",
}  # verified to resolve directly against FRED before use -- see docstring

_HISTORY_START = "1999-01-01"
_MONTHLY_LAG_DAYS = 35   # conservative OECD/FRED monthly-series publication lag

_Z_WINDOW_WEEKS      = 156   # 3-year trailing window, same as the COT backtest
_Z_MIN_WEEKS         = 52
_TREND_LOOKBACK_WEEKS = 13   # ~3 months

_THRESHOLDS = [1.0, 1.5, 2.0]
_LAG_WEEKS  = [1, 2, 4, 8]


def _weekly_series(series_id: str, is_daily: bool) -> pd.DataFrame:
    obs = fred.history(series_id, _HISTORY_START)
    if not obs:
        return pd.DataFrame()
    df = pd.DataFrame(obs)
    df["date"] = pd.to_datetime(df["date"])
    if not is_daily:
        df["date"] = df["date"] + pd.Timedelta(days=_MONTHLY_LAG_DAYS)
    df = df.set_index("date").sort_index()
    weekly = df["value"].resample("W-FRI").ffill().to_frame("value")
    weekly = weekly.dropna()
    return weekly.reset_index()


def build_currency_series(series_map: dict, daily_ccys: set) -> dict:
    out = {}
    for ccy, series_id in series_map.items():
        print(f"[fetch] FRED {series_id} for {ccy}...")
        df = _weekly_series(series_id, ccy in daily_ccys)
        if df.empty:
            print(f"  -> no data, skipping {ccy}")
            continue
        roll_mean = df["value"].rolling(_Z_WINDOW_WEEKS, min_periods=_Z_MIN_WEEKS).mean()
        roll_std  = df["value"].rolling(_Z_WINDOW_WEEKS, min_periods=_Z_MIN_WEEKS).std()
        # z of the RATE LEVEL itself isn't the signal -- differential is computed
        # per-pair below. Keep the raw weekly value here; differential z is
        # computed after differencing two currencies' series.
        out[ccy] = df
        print(f"  -> {len(df)} weekly rows, {df['date'].min().date()} to {df['date'].max().date()}")
    return out


def build_pair_signal(pair: str, ccy_series: dict) -> pd.DataFrame:
    base, quote = pair.split("/")
    if base not in ccy_series or quote not in ccy_series:
        return pd.DataFrame()
    b = ccy_series[base].rename(columns={"value": "rate_base"})
    q = ccy_series[quote].rename(columns={"value": "rate_quote"})
    merged = pd.merge(b, q, on="date", how="inner").sort_values("date").reset_index(drop=True)
    if merged.empty:
        return merged
    merged["diff"] = merged["rate_base"] - merged["rate_quote"]
    roll_mean = merged["diff"].rolling(_Z_WINDOW_WEEKS, min_periods=_Z_MIN_WEEKS).mean()
    roll_std  = merged["diff"].rolling(_Z_WINDOW_WEEKS, min_periods=_Z_MIN_WEEKS).std()
    merged["z"] = (merged["diff"] - roll_mean) / roll_std
    merged["trend"] = merged["diff"] - merged["diff"].shift(_TREND_LOOKBACK_WEEKS)
    merged = merged.dropna(subset=["z", "trend"])
    merged["widening"] = np.sign(merged["trend"]) == np.sign(merged["z"])
    merged["pair"] = pair
    merged["publication_date"] = merged["date"]  # already lagged per-series above
    return merged[["pair", "publication_date", "z", "trend", "widening"]]


def forward_returns(price_df: pd.DataFrame, pub_dates: pd.Series, lag_weeks: int) -> pd.Series:
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


def run_source(label: str, ccy_series: dict, price_cache: dict, results: list) -> pd.DataFrame:
    all_signals = []
    for pair in UNIVERSE:
        sig = build_pair_signal(pair, ccy_series)
        if sig.empty:
            continue
        if pair not in price_cache:
            print(f"[fetch] price history for {pair}...")
            try:
                px = hgb.fetch_daily(pair)
            except Exception as exc:
                print(f"  -> price fetch failed: {exc}")
                continue
            if px.empty:
                continue
            price_cache[pair] = px.sort_index()
        px = price_cache[pair]
        for lag in _LAG_WEEKS:
            sig[f"fwd_ret_{lag}w"] = forward_returns(px, sig["publication_date"], lag)
        all_signals.append(sig)

    if not all_signals:
        print(f"[{label}] NO SIGNALS BUILT.")
        return pd.DataFrame()

    full = pd.concat(all_signals, ignore_index=True)
    fires = full[full["widening"]]
    print(f"\n[{label}] total pair-weeks: {len(full)} across {full['pair'].nunique()} pairs "
          f"({len(fires)} 'extreme+widening' candidate rows before threshold scan)")

    print(f"\n{'='*100}\n[{label}] AGGREGATE RESULTS (all pairs pooled)\n{'='*100}")
    for lag in _LAG_WEEKS:
        col = f"fwd_ret_{lag}w"
        for thr in _THRESHOLDS:
            fire_rows = full[(full["z"].abs() >= thr) & full["widening"]].dropna(subset=[col])
            no_fire_rows = full[~((full["z"].abs() >= thr) & full["widening"])].dropna(subset=[col])
            if len(fire_rows) < 10:
                continue
            fire_hits = (np.sign(fire_rows[col]) == np.sign(fire_rows["z"])).sum()
            no_fire_hits = (np.sign(no_fire_rows[col]) == np.sign(no_fire_rows["z"])).sum()
            z = _ztest(int(fire_hits), len(fire_rows), int(no_fire_hits), len(no_fire_rows))
            p_value, wr_fire, wr_no = z if z else (None, None, None)
            mean_ret = fire_rows[col].mean()
            print(f"lag={lag}w thr={thr:.1f}  n_fire={len(fire_rows):5d}  "
                  f"hit_rate={wr_fire*100 if wr_fire is not None else float('nan'):5.1f}%  "
                  f"(no-fire={wr_no*100 if wr_no is not None else float('nan'):5.1f}%, n={len(no_fire_rows)})  "
                  f"mean_fwd_pips_raw={mean_ret:9.5f}  p={p_value if p_value is not None else float('nan'):.4f}")
            results.append({
                "source": label, "scope": "aggregate", "pair": "ALL", "lag_weeks": lag,
                "threshold": thr, "n_fire": len(fire_rows), "n_no_fire": len(no_fire_rows),
                "hit_rate_fire": wr_fire, "hit_rate_no_fire": wr_no,
                "mean_fwd_return_raw": mean_ret, "p_value": p_value,
            })

    print(f"\n{'='*100}\n[{label}] PER-PAIR RESULTS (threshold=1.5, lag=4w)\n{'='*100}")
    thr, lag = 1.5, 4
    col = f"fwd_ret_{lag}w"
    n_sig = 0
    n_sig_confirm = 0
    for pair in sorted(full["pair"].unique()):
        sub = full[full["pair"] == pair]
        fire_rows = sub[(sub["z"].abs() >= thr) & sub["widening"]].dropna(subset=[col])
        no_fire_rows = sub[~((sub["z"].abs() >= thr) & sub["widening"])].dropna(subset=[col])
        if len(fire_rows) < 8:
            continue
        fire_hits = (np.sign(fire_rows[col]) == np.sign(fire_rows["z"])).sum()
        no_fire_hits = (np.sign(no_fire_rows[col]) == np.sign(no_fire_rows["z"])).sum()
        z = _ztest(int(fire_hits), len(fire_rows), int(no_fire_hits), len(no_fire_rows))
        p_value, wr_fire, wr_no = z if z else (None, None, None)
        flag = ""
        if p_value is not None and p_value < 0.05:
            n_sig += 1
            if wr_fire > wr_no:
                n_sig_confirm += 1
                flag = "  <-- significant, CONFIRMS thesis"
            else:
                flag = "  <-- significant, CONTRADICTS thesis"
        print(f"{pair:10s} n_fire={len(fire_rows):4d}  hit_rate={wr_fire*100:5.1f}%  "
              f"(no-fire={wr_no*100:5.1f}%, n={len(no_fire_rows)})  p={p_value:.4f}{flag}"
              if p_value is not None else f"{pair:10s} n_fire={len(fire_rows):4d} (insufficient baseline)")
        results.append({
            "source": label, "scope": "per_pair", "pair": pair, "lag_weeks": lag,
            "threshold": thr, "n_fire": len(fire_rows), "n_no_fire": len(no_fire_rows),
            "hit_rate_fire": wr_fire, "hit_rate_no_fire": wr_no,
            "mean_fwd_return_raw": fire_rows[col].mean(), "p_value": p_value,
        })
    n_pairs_tested = full["pair"].nunique()
    expected_by_chance = n_pairs_tested * 0.05
    print(f"\n[{label}] {n_sig}/{n_pairs_tested} pairs cleared p<0.05 "
          f"({n_sig_confirm} confirming direction, {n_sig - n_sig_confirm} contradicting). "
          f"Expected by chance alone at alpha=0.05: ~{expected_by_chance:.1f}.")

    return full


def run():
    results = []
    price_cache = {}

    print("### SOURCE 1: policy rate differential ###")
    policy_ccy = build_currency_series(_POLICY_RATE_SERIES, _DAILY_POLICY_CCY)
    full_policy = run_source("policy_rate", policy_ccy, price_cache, results)

    print("\n\n### SOURCE 2: 10-year government yield differential ###")
    yield_ccy = build_currency_series(_YIELD10Y_SERIES, set())  # all monthly
    full_yield = run_source("yield_10y", yield_ccy, price_cache, results)

    detail_frames = [f for f in (full_policy, full_yield) if not f.empty]
    if detail_frames:
        pd.concat(detail_frames, keys=["policy_rate", "yield_10y"], names=["source"]).to_csv(
            "data/carry_backtest_signal_detail.csv"
        )
    pd.DataFrame(results).to_csv("data/carry_backtest_results.csv", index=False)
    print("\nWrote data/carry_backtest_results.csv and data/carry_backtest_signal_detail.csv")


if __name__ == "__main__":
    run()
