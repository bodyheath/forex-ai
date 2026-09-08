"""Walk-forward backtest of src/fundamentals.py's live TAILWIND/HEADWIND/MIXED
classification, requested as item 5 of the 2026-09-08 deterministic-signal
systemic audit. Same rigor discipline as every other backtest in this
sequence (cot_reversal_penalty_backtest.py, carry_backtest.py,
extreme_flag_and_building_boost_backtest.py): real historical data, walk-
forward with no lookahead, calls the REAL live functions rather than
reimplementing their scoring logic, honest disclosure of data-availability
gaps and non-independence.

============================================================================
WHY THIS NEEDED A PROXY FOR ONE OF THREE FACTORS -- CHECKED DIRECTLY, NOT
ASSUMED
============================================================================
fundamentals.py's live get_fundamental_alignment() combines three factors:
  1. cb_factor    -- central bank direction (hiking=bullish/cutting=bearish)
  2. carry_factor -- rate differential vs a static 0.75% threshold
  3. econ_factor   -- "recent data beating/missing consensus forecasts"

Factors 1 and 2 are directly, exactly testable with real free data: central
bank policy rates are public record (same FRED series already used live by
fundamentals.py itself, and already validated in carry_backtest.py), so
"hiking" or "cutting" over a trailing window is an objective fact, not a
proxy.

Factor 3 is NOT directly testable with free data. Checked directly before
building anything (FRED web search + a direct FRED query for "economic
surprise index"): FRED hosts no actual-vs-consensus-forecast series for any
of these 8 currencies, and the Citigroup Economic Surprise Index (the
industry-standard version of exactly this concept) has no free API or
historical download -- it's a Bloomberg/Citi terminal product. There is no
free historical archive of real forecast/consensus values to compare
against, for any currency, at any point in history.

PROXY USED, disclosed plainly rather than silently substituted: the OECD
Composite Leading Indicator (amplitude-adjusted, normalized so 100 = the
country's own long-run trend -- OECD's own documented interpretation, not
an invented threshold), fetched via FRED for all 8 currencies (Euro Area
uses the EA19 aggregate for EUR). This measures "is the economy currently
running above or below its own normal trend" -- a genuine, freely
available, real-time economic-momentum signal, but a DIFFERENT statistical
object than a consensus surprise: CLI is a slow-moving, trending level;
Citi ESI-style surprises are a faster, more mean-reverting spike measure.
Whatever this backtest finds about the CLI-based proxy is evidence about
that momentum premise, not a directly literal confirmation or refutation of
the live dict's judgment-based construction -- reported as exactly that,
not oversold as more than it is.

DATA-AVAILABILITY GAP FOUND DURING SETUP, disclosed rather than smoothed
over: of the 8 OECD CLI series, 5 (USD/GBP/JPY/AUD/CAD) are current through
2026-06-01. CHF and the EA19 (EUR) series stop at 2022-11-01 and NZD stops
at 2019-11-01 -- all three appear to have been discontinued by the OECD,
not a fetch failure (checked directly: the series resolve fine, they just
have no further history). This truncates the usable historical window for
any pair touching CHF, EUR, or NZD to whatever ended before that cutoff;
USD/GBP/JPY/AUD/CAD-only pairs get the full run through 2026. Per-currency
data depth is reported explicitly below, same as carry_backtest.py's
precedent for uneven series depth.

============================================================================
METHOD
============================================================================
1. Real historical policy-rate and OECD CLI series, weekly-resampled,
   forward-filled (monthly series lagged 35 days for realistic FRED/OECD
   publication delay, same convention as carry_backtest.py; USD/GBP policy
   rates are true daily series, used with no artificial lag).
2. cb_bias(ccy, t)   = "bullish" if the rate rose >= 0.05pp over the
   trailing 13 weeks (~3 months), "bearish" if it fell that much, else
   "neutral" -- the same objective threshold either way, no per-currency
   tuning.
3. econ_bias(ccy, t) = "bullish" if CLI(t) > 100.3, "bearish" if CLI(t) <
   99.7, else "neutral" -- OECD's own 100=trend convention with a small
   dead zone, not fit to this data.
4. carry_factor uses fundamentals.py's own live _CARRY_THRESHOLD constant
   (imported, not retyped) against the real rate differential.
5. For every (pair, week), the REAL live fundamentals._CB_STANCE /
   ._RATES / ._ECON_SURPRISE module dicts are overwritten with that week's
   real classified values, then fundamentals.get_fundamental_alignment()
   -- the actual live function -- is called to get the real alignment
   verdict. This is a controlled monkeypatch confined to this standalone
   script's own process; it never touches a running production import.
6. net_score = aligned - opposed (-3..+3). alignment=="TAILWIND"/"HEADWIND"
   is exactly net_score==+-3 -- the real live firing condition. MIXED rows
   (|net_score|<3) still carry a directional lean via net_score's sign,
   used as the non-fired baseline population, mirroring carry_backtest.py's
   convention of keeping the same sign-based hit definition for both fired
   and non-fired rows so the comparison is apples-to-apples.
7. Forward return at lag in {1,2,4,8} weeks (real OHLC via
   historical_grading_backtest.fetch_daily(), imported not reimplemented).
   hit = sign(fwd_return) == sign(net_score) (net_score==0 rows can never
   hit, by construction -- same silent handling as carry_backtest.py's
   sign(0) case, not treated specially).
8. Two-proportion z-test (src.shadow_mode._ztest) comparing the strict
   fired (|net_score|==3, the real TAILWIND/HEADWIND trigger) bucket's hit
   rate against the MIXED bucket's, at both the aggregate and per-currency-
   pair level (n>=8 pairs shown individually, same threshold as every
   other backtest in this sequence) -- explicit multiple-comparisons
   awareness stated in the printed report, not just in this docstring.

Non-independence caveat, same shape as every prior backtest here: one
currency's CB/econ state is shared across every pair containing it in the
same week, so per-pair p-values are correlated, not independent draws --
the per-currency-pair breakdown exists specifically so a handful of
significant pairs among 28 tested at once isn't mistaken for a broad
finding.

Usage: python scripts/fundamentals_tailwind_headwind_backtest.py
Writes data/fundamentals_tailwind_headwind_backtest_results.csv and
data/fundamentals_tailwind_headwind_backtest_detail.csv.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

import config
from src import fred
from src import fundamentals as fund
from src.selector import UNIVERSE
from src.shadow_mode import _ztest
import historical_grading_backtest as hgb
from carry_backtest import forward_returns  # reused, not reimplemented

hgb.FETCH_PERIOD = "max"

_UNIVERSE_CCY = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"}

_POLICY_RATE_SERIES = {
    ccy: meta["rate_fred"] for ccy, meta in config.CURRENCIES.items()
    if meta.get("rate_fred") and ccy in _UNIVERSE_CCY
}
_DAILY_POLICY_CCY = {"USD", "GBP"}   # DFF, IUDSOIA -- true daily series, no lag needed

_CLI_SERIES = {
    "USD": "USALOLITOAASTSAM", "GBP": "GBRLOLITOAASTSAM", "JPY": "JPNLOLITOAASTSAM",
    "AUD": "AUSLOLITOAASTSAM", "CAD": "CANLOLITOAASTSAM", "CHF": "CHELOLITOAASTSAM",
    "NZD": "NZLLOLITOAASTSAM", "EUR": "EA19LOLITOAASTSAM",
}  # all 8 confirmed to resolve directly against FRED before use -- see docstring

_HISTORY_START = "1999-01-01"
_MONTHLY_LAG_DAYS = 35              # same OECD/FRED publication-lag convention as carry_backtest.py
_CB_TREND_LOOKBACK_WEEKS = 13       # ~3 months, matches carry_backtest.py's TREND_LOOKBACK
_CB_MOVE_THRESHOLD = 0.05           # pct-point move over trailing window to count as hiking/cutting
_CLI_NEUTRAL_BAND = 0.3             # OECD's own "100 = trend" convention, small dead zone
_CARRY_THRESHOLD = fund._CARRY_THRESHOLD  # the live constant, not a re-typed copy

_LAG_WEEKS = [1, 2, 4, 8]


def _weekly_series(series_id: str, is_daily: bool) -> pd.DataFrame:
    obs = fred.history(series_id, _HISTORY_START)
    if not obs:
        return pd.DataFrame()
    df = pd.DataFrame(obs)
    df["date"] = pd.to_datetime(df["date"])
    if not is_daily:
        df["date"] = df["date"] + pd.Timedelta(days=_MONTHLY_LAG_DAYS)
    df = df.set_index("date").sort_index()
    weekly = df["value"].resample("W-FRI").ffill().to_frame("value").dropna()
    return weekly.reset_index()


def build_rate_series() -> dict:
    out = {}
    for ccy, sid in _POLICY_RATE_SERIES.items():
        print(f"[fetch] FRED policy rate {sid} for {ccy}...")
        df = _weekly_series(sid, ccy in _DAILY_POLICY_CCY)
        if df.empty:
            print(f"  -> no data, skipping {ccy}")
            continue
        df["cb_change"] = df["value"] - df["value"].shift(_CB_TREND_LOOKBACK_WEEKS)
        out[ccy] = df
        print(f"  -> {len(df)} weekly rows, {df['date'].min().date()} to {df['date'].max().date()}")
    return out


def build_cli_series() -> dict:
    out = {}
    for ccy, sid in _CLI_SERIES.items():
        print(f"[fetch] FRED OECD CLI {sid} for {ccy}...")
        df = _weekly_series(sid, False)
        if df.empty:
            print(f"  -> no data, skipping {ccy}")
            continue
        out[ccy] = df
        print(f"  -> {len(df)} weekly rows, {df['date'].min().date()} to {df['date'].max().date()}")
    return out


def _cb_bias(change) -> str:
    if change is None or pd.isna(change):
        return "neutral"
    if change >= _CB_MOVE_THRESHOLD:
        return "bullish"
    if change <= -_CB_MOVE_THRESHOLD:
        return "bearish"
    return "neutral"


def _econ_bias(level) -> str:
    if level is None or pd.isna(level):
        return "neutral"
    if level > 100 + _CLI_NEUTRAL_BAND:
        return "bullish"
    if level < 100 - _CLI_NEUTRAL_BAND:
        return "bearish"
    return "neutral"


def build_pair_signal(pair: str, rate_series: dict, cli_series: dict) -> pd.DataFrame:
    base, quote = pair.split("/")
    if base not in rate_series or quote not in rate_series:
        return pd.DataFrame()
    if base not in cli_series or quote not in cli_series:
        return pd.DataFrame()

    rb = rate_series[base][["date", "value", "cb_change"]].rename(
        columns={"value": "rate_base", "cb_change": "cb_change_base"})
    rq = rate_series[quote][["date", "value", "cb_change"]].rename(
        columns={"value": "rate_quote", "cb_change": "cb_change_quote"})
    cb_ = cli_series[base][["date", "value"]].rename(columns={"value": "cli_base"})
    cq_ = cli_series[quote][["date", "value"]].rename(columns={"value": "cli_quote"})

    m = pd.merge(rb, rq, on="date", how="inner")
    m = pd.merge(m, cb_, on="date", how="inner")
    m = pd.merge(m, cq_, on="date", how="inner")
    m = m.dropna(subset=["cb_change_base", "cb_change_quote"])
    if m.empty:
        return m

    rows = []
    for r in m.itertuples(index=False):
        cb_base    = _cb_bias(r.cb_change_base)
        cb_quote   = _cb_bias(r.cb_change_quote)
        econ_base  = _econ_bias(r.cli_base)
        econ_quote = _econ_bias(r.cli_quote)

        # Controlled monkeypatch of the REAL live module state, confined to
        # this standalone script's own process -- then call the REAL live
        # function. Not a reimplementation of get_fundamental_alignment()'s
        # scoring logic.
        fund._CB_STANCE[base]     = {"bias": cb_base, "note": ""}
        fund._CB_STANCE[quote]    = {"bias": cb_quote, "note": ""}
        fund._ECON_SURPRISE[base]  = econ_base
        fund._ECON_SURPRISE[quote] = econ_quote
        fund._RATES[base]  = r.rate_base
        fund._RATES[quote] = r.rate_quote

        result = fund.get_fundamental_alignment(base, quote, "BUY")
        rows.append({
            "pair": pair,
            "publication_date": r.date,
            "alignment": result["alignment"],
            "net_score": result["aligned"] - result["opposed"],
            "cb_base": cb_base, "cb_quote": cb_quote,
            "econ_base": econ_base, "econ_quote": econ_quote,
            "carry_diff": result["carry_diff"],
        })
    out = pd.DataFrame(rows)
    out["publication_date"] = pd.to_datetime(out["publication_date"])
    return out


def report(full: pd.DataFrame, price_cache: dict) -> None:
    for pair in full["pair"].unique():
        if pair in price_cache:
            continue
        print(f"[fetch] price history for {pair}...")
        try:
            px = hgb.fetch_daily(pair)
        except Exception as exc:
            print(f"  -> price fetch failed: {exc}")
            continue
        if px.empty:
            continue
        price_cache[pair] = px.sort_index()

    frames = []
    for pair in full["pair"].unique():
        if pair not in price_cache:
            continue
        sub = full[full["pair"] == pair].copy()
        for lag in _LAG_WEEKS:
            sub[f"fwd_ret_{lag}w"] = forward_returns(price_cache[pair], sub["publication_date"], lag)
        frames.append(sub)
    full = pd.concat(frames, ignore_index=True) if frames else full

    fired = full[full["alignment"] != "MIXED"]
    print(f"\ntotal pair-weeks: {len(full)} across {full['pair'].nunique()} pairs "
          f"({len(fired)} real TAILWIND/HEADWIND firings, "
          f"{(full['alignment']=='TAILWIND').sum()} TAILWIND / "
          f"{(full['alignment']=='HEADWIND').sum()} HEADWIND)")

    results = []
    print(f"\n{'='*100}\nAGGREGATE RESULTS (all pairs pooled, strict live TAILWIND/HEADWIND trigger)\n{'='*100}")
    for lag in _LAG_WEEKS:
        col = f"fwd_ret_{lag}w"
        fire_rows = full[full["alignment"] != "MIXED"].dropna(subset=[col])
        base_rows = full[full["alignment"] == "MIXED"].dropna(subset=[col])
        if len(fire_rows) < 10:
            continue
        fire_hits = (np.sign(fire_rows[col]) == np.sign(fire_rows["net_score"])).sum()
        base_hits = (np.sign(base_rows[col]) == np.sign(base_rows["net_score"])).sum()
        z = _ztest(int(fire_hits), len(fire_rows), int(base_hits), len(base_rows))
        p_value, wr_fire, wr_base = z if z else (None, None, None)
        print(f"lag={lag}w  n_fire={len(fire_rows):5d}  hit_rate={wr_fire*100:5.1f}%  "
              f"(mixed-baseline={wr_base*100:5.1f}%, n={len(base_rows)})  "
              f"p={p_value:.4f}" if p_value is not None else
              f"lag={lag}w  n_fire={len(fire_rows):5d}  insufficient baseline")
        results.append({
            "scope": "aggregate", "pair": "ALL", "lag_weeks": lag,
            "n_fire": len(fire_rows), "n_baseline": len(base_rows),
            "hit_rate_fire": wr_fire, "hit_rate_baseline": wr_base, "p_value": p_value,
        })

    print(f"\n{'='*100}\nPER-PAIR RESULTS (lag=4w, n_fire>=8)\n{'='*100}")
    lag = 4
    col = f"fwd_ret_{lag}w"
    n_sig = 0
    n_sig_confirm = 0
    n_pairs_tested = 0
    for pair in sorted(full["pair"].unique()):
        sub = full[full["pair"] == pair]
        fire_rows = sub[sub["alignment"] != "MIXED"].dropna(subset=[col])
        base_rows = sub[sub["alignment"] == "MIXED"].dropna(subset=[col])
        if len(fire_rows) < 8:
            continue
        n_pairs_tested += 1
        fire_hits = (np.sign(fire_rows[col]) == np.sign(fire_rows["net_score"])).sum()
        base_hits = (np.sign(base_rows[col]) == np.sign(base_rows["net_score"])).sum()
        z = _ztest(int(fire_hits), len(fire_rows), int(base_hits), len(base_rows))
        p_value, wr_fire, wr_base = z if z else (None, None, None)
        flag = ""
        if p_value is not None and p_value < 0.05:
            n_sig += 1
            if wr_fire > wr_base:
                n_sig_confirm += 1
                flag = "  <-- significant, CONFIRMS thesis"
            else:
                flag = "  <-- significant, CONTRADICTS thesis"
        if p_value is not None:
            print(f"{pair:10s} n_fire={len(fire_rows):4d}  hit_rate={wr_fire*100:5.1f}%  "
                  f"(mixed-baseline={wr_base*100:5.1f}%, n={len(base_rows)})  p={p_value:.4f}{flag}")
        results.append({
            "scope": "per_pair", "pair": pair, "lag_weeks": lag,
            "n_fire": len(fire_rows), "n_baseline": len(base_rows),
            "hit_rate_fire": wr_fire, "hit_rate_baseline": wr_base, "p_value": p_value,
        })
    expected_by_chance = n_pairs_tested * 0.05
    print(f"\n{n_sig}/{n_pairs_tested} pairs cleared p<0.05 "
          f"({n_sig_confirm} confirming direction, {n_sig - n_sig_confirm} contradicting). "
          f"Expected by chance alone at alpha=0.05: ~{expected_by_chance:.1f}.")

    pd.DataFrame(results).to_csv("data/fundamentals_tailwind_headwind_backtest_results.csv", index=False)
    full.to_csv("data/fundamentals_tailwind_headwind_backtest_detail.csv", index=False)
    print("\nWrote data/fundamentals_tailwind_headwind_backtest_results.csv "
          "and data/fundamentals_tailwind_headwind_backtest_detail.csv")


def run():
    rate_series = build_rate_series()
    cli_series = build_cli_series()

    all_signals = []
    for pair in UNIVERSE:
        sig = build_pair_signal(pair, rate_series, cli_series)
        if not sig.empty:
            all_signals.append(sig)
    if not all_signals:
        print("NO SIGNALS BUILT.")
        return
    full = pd.concat(all_signals, ignore_index=True)
    report(full, price_cache={})


if __name__ == "__main__":
    run()
