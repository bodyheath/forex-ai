"""Backtest for daily.py's _cot_reversal_penalty() (2026-09-07).

Phase 01B (scripts/cot_backtest.py, removed after shipping its NO-GO finding
but recoverable from git history at commit 9ac78d0a) rigorously tested a
DIFFERENT COT mechanism -- a cross-currency z-score contrarian spread -- and
found no real edge (aggregate hit rates 48-52%, the signature of noise)
before any standalone Positioning Agent was ever built. cot_reversal_penalty
is a separate, pre-existing mechanism that also consumes CFTC COT data but
via a different construction: same-currency 52-week percentile + 3-week
momentum classification (REVERSING / UNWINDING), computed in
src/positioning.py and applied as a live -1 confidence penalty in
daily.py's _cot_reversal_penalty(). This backtests THAT specific mechanism,
byte-for-byte (the real live function is imported and called directly, not
reimplemented), against real historical CFTC + price data -- not just an
aggregate check, a real per-pair breakdown too, matching Phase 01B's rigor.

============================================================================
MECHANISM UNDER TEST (see daily.py's _cot_reversal_penalty() docstring)
============================================================================
Fires (-1 confidence) when, for the base or quote currency of a proposed
trade, institutional (large speculator) positioning either:
  REVERSING  -- flipped sign vs. 3 weeks ago (any magnitude), or
  UNWINDING  -- moved >=20% of the 52-week range over 3 weeks, starting from
                a reading that was itself within 15% of that range's
                top/bottom (a "real extreme")
...in the direction that means "you are trading with the crowd that is now
exiting" for the candidate's proposed BUY/SELL direction.

The claim being tested: when this fires against a direction, does price
actually tend to move AGAINST that direction afterward (validating the
penalty) -- or is the hit rate indistinguishable from the non-firing
population (no real edge, same conclusion as Phase 01B's z-score version)?

============================================================================
METHOD
============================================================================
1. Real weekly Legacy Non-Commercial net speculator position per currency
   (CFTC Socrata, same endpoint/series positioning.py uses live), full
   history, with the same USD post-2022-02-01 freeze handling Phase 01B
   used (verified there directly against both CFTC datasets).
2. At each week t (>=52 trailing weeks of history, so hi/lo matches what
   positioning.py would have seen live -- never a future-informed range),
   compute one_year_high/low, net_3w_ago, and call
   src.positioning._cot_momentum() -- the REAL live function -- to get
   cot_momentum/momentum_delta_pct, exactly reproducing what the live
   system computes from the same inputs.
3. For every UNIVERSE pair containing this currency as base or quote, and
   both BUY/SELL, build the exact minimal `result` dict shape
   daily._cot_reversal_penalty() expects and call THAT REAL FUNCTION
   directly (imported, not reimplemented) -- zero logic drift from live.
4. Publication lag: report_date + 3 days (Tue position date -> Fri CFTC
   release), matching positioning.py's live timing and Phase 01B's own
   convention -- a real trader could not have acted any sooner.
5. Real forward price return (yfinance daily OHLC, same fetch_daily() the
   mass mechanical backtest and Phase 01B both already use) from the first
   trading day at/after publication_date to N trading days later. Primary
   window: 5 trading days (~1 week), matching this system's real trade
   duration far more closely than Phase 01B's 1/2/4/8-WEEK windows (that
   backtest was testing a slower-moving z-score signal; this one is a
   per-trade confidence adjustment on trades that typically resolve in
   days, not weeks) -- also reports 10 and 20 trading days for robustness.
   A "hit" = price moved AGAINST the penalized direction (validates the
   penalty); hit rate compared to the non-firing population via the same
   two-proportion z-test (src.shadow_mode._ztest) used throughout this
   codebase's promotion-discipline checks.

KNOWN NON-INDEPENDENCE, STATED PLAINLY: one currency's momentum condition
fires simultaneously for every pair it appears in (e.g. GBP REVERSING fires
for GBP/USD, GBP/JPY, EUR/GBP, ... all in the same week) -- these are NOT
independent draws. The aggregate hit rate is reported for completeness but
should be read cautiously; the per-pair (and per-currency) breakdown is the
more trustworthy check for whether this is a real, broad-based signal or a
handful of correlated pair-instances of the same few currency events.

Usage: python scripts/cot_reversal_penalty_backtest.py
Writes data/cot_reversal_penalty_backtest_results.csv (aggregate + per-pair
+ per-currency + REVERSING-vs-UNWINDING breakdown) and
data/cot_reversal_penalty_backtest_detail.csv (one row per firing instance).
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("ALLOW_LOCAL_RUN", "YES")

import numpy as np
import pandas as pd
import requests

import config
from src.selector import UNIVERSE
from src.positioning import _cot_momentum
from src.shadow_mode import _ztest
import historical_grading_backtest as hgb

hgb.FETCH_PERIOD = "max"

LEGACY_URL = config.COT_DATASET_URL
CURRENCY_MARKETS = {
    ccy: meta["cot_market"]
    for ccy, meta in config.CURRENCIES.items()
    if meta.get("cot_market")
}

_Z_WINDOW_WEEKS = 52     # trailing window for one_year_high/low -- matches
                          # positioning.py's own live 52-row fetch exactly
_USD_CUTOFF = pd.Timestamp("2022-02-01")
_FWD_DAYS = [5, 10, 20]  # trading days -- primary window is 5 (~1 real week)


def _fetch_cot(market_name: str) -> pd.DataFrame:
    escaped = market_name.replace("'", "''")
    resp = requests.get(LEGACY_URL, params={
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


def build_currency_history() -> dict:
    """{ccy: DataFrame[date, net]} using Legacy Non-Commercial, full history."""
    out = {}
    for ccy, market in CURRENCY_MARKETS.items():
        print(f"[fetch] Legacy COT for {ccy} ({market})...")
        df = _fetch_cot(market)
        if df.empty:
            print(f"  -> no rows, skipping {ccy}")
            continue
        df["date"] = pd.to_datetime(df["report_date_as_yyyy_mm_dd"])
        df["net"]  = _legacy_net(df)
        df = df[["date", "net"]].dropna().sort_values("date").reset_index(drop=True)
        if ccy == "USD":
            before = len(df)
            df = df[df["date"] < _USD_CUTOFF].reset_index(drop=True)
            print(f"  -> USD: dropped {before - len(df)} post-freeze rows")
        df["publication_date"] = df["date"] + pd.Timedelta(days=3)
        out[ccy] = df
        print(f"  -> {len(df)} weekly rows, {df['date'].min().date()} to {df['date'].max().date()}")
    return out


def compute_momentum_series(hist: pd.DataFrame) -> pd.DataFrame:
    """At each week t (index order oldest->newest), compute the SAME fields
    positioning.py's live _for_currency() would have produced at that time,
    using only trailing data (never future weeks) -- a true walk-forward
    reconstruction, not a lookahead-contaminated full-history range."""
    nets = hist["net"].tolist()
    rows = []
    for t in range(len(nets)):
        if t < _Z_WINDOW_WEEKS - 1 or t < 3:
            continue
        window = nets[max(0, t - _Z_WINDOW_WEEKS + 1):t + 1]   # trailing <=52 weeks ending at t
        # _cot_momentum expects nets[0]=latest ... nets[3]=3 weeks ago, so reverse.
        nets_desc = list(reversed(window))
        hi, lo = max(window), min(window)
        mom = _cot_momentum(nets_desc, hi - lo)
        old_pct_in_range = ((nets_desc[3] - lo) / (hi - lo) * 100) if hi != lo else 50.0
        rows.append({
            "date": hist["date"].iloc[t],
            "publication_date": hist["publication_date"].iloc[t],
            "net": nets[t],
            "net_3w_ago": mom["net_3w_ago"],
            "one_year_high": hi,
            "one_year_low": lo,
            "old_pct_in_range": old_pct_in_range,
            "cot_momentum": mom["momentum"],
            "momentum_delta_pct": mom["delta_pct_range"],
        })
    return pd.DataFrame(rows)


def fake_positioning_result(pair: str, direction: str, base_row, quote_row) -> dict:
    def _leg(row):
        if row is None:
            return {"status": "UNAVAILABLE"}
        return {
            "status": "ok",
            "cot_momentum": row["cot_momentum"],
            "net_3w_ago": row["net_3w_ago"],
            "one_year_high": row["one_year_high"],
            "one_year_low": row["one_year_low"],
            "momentum_delta_pct": row["momentum_delta_pct"],
        }
    return {
        "pair": pair,
        "parsed": {"direction": direction},
        "bundle": {"positioning": {"base": _leg(base_row), "quote": _leg(quote_row)}},
    }


def forward_return(price_df: pd.DataFrame, pub_date, n_days: int):
    idx = price_df.index
    closes = price_df["close"]
    pos0 = idx.searchsorted(pub_date)
    pos1 = pos0 + n_days
    if pos0 >= len(idx) or pos1 >= len(idx):
        return None
    return float(closes.iloc[pos1] - closes.iloc[pos0])


def run():
    from daily import _cot_reversal_penalty, _COT_EXTREME_PCT, _COT_UNWIND_MAGNITUDE_PCT
    print(f"Using live thresholds: _COT_EXTREME_PCT={_COT_EXTREME_PCT} "
          f"_COT_UNWIND_MAGNITUDE_PCT={_COT_UNWIND_MAGNITUDE_PCT}")

    history = build_currency_history()
    momentum = {ccy: compute_momentum_series(df) for ccy, df in history.items()}
    for ccy, df in momentum.items():
        print(f"[momentum] {ccy}: {len(df)} weeks with a computed momentum reading "
              f"({(df['cot_momentum']=='REVERSING').sum()} REVERSING, "
              f"{(df['cot_momentum']=='UNWINDING').sum()} UNWINDING)")

    price_cache = {}
    detail_rows = []

    for pair in UNIVERSE:
        base, quote = pair.split("/")
        if base not in momentum or quote not in momentum:
            continue
        b_df, q_df = momentum[base], momentum[quote]
        merged = pd.merge(
            b_df, q_df, on="publication_date", how="outer", suffixes=("_base", "_quote"),
        ).sort_values("publication_date")
        if merged.empty:
            continue

        print(f"[price] fetching {pair}...")
        try:
            px = hgb.fetch_daily(pair)
        except Exception as exc:
            print(f"  -> price fetch failed: {exc}")
            continue
        if px.empty:
            continue
        px = px.sort_index()
        price_cache[pair] = px
        time.sleep(0.2)

        for _, row in merged.iterrows():
            pub = row["publication_date"]
            base_row = row if pd.notna(row.get("cot_momentum_base")) else None
            quote_row = row if pd.notna(row.get("cot_momentum_quote")) else None
            if base_row is None and quote_row is None:
                continue

            def _mk(prefix):
                return {
                    "cot_momentum": row.get(f"cot_momentum_{prefix}"),
                    "net_3w_ago": row.get(f"net_3w_ago_{prefix}"),
                    "one_year_high": row.get(f"one_year_high_{prefix}"),
                    "one_year_low": row.get(f"one_year_low_{prefix}"),
                    "momentum_delta_pct": row.get(f"momentum_delta_pct_{prefix}"),
                }
            b = _mk("base") if base_row is not None else None
            q = _mk("quote") if quote_row is not None else None

            for direction in ("BUY", "SELL"):
                fake = fake_positioning_result(pair, direction, b, q)
                penalty = _cot_reversal_penalty(fake)
                if penalty >= 0:
                    continue   # doesn't fire for this direction

                fired_side = "base" if (b and b["cot_momentum"] in ("REVERSING", "UNWINDING")) else "quote"
                fired_type = (b if fired_side == "base" else q)["cot_momentum"]

                rec = {
                    "pair": pair, "direction": direction, "publication_date": pub,
                    "fired_side": fired_side, "fired_type": fired_type,
                    "currency": base if fired_side == "base" else quote,
                }
                for nd in _FWD_DAYS:
                    rec[f"fwd_{nd}d"] = forward_return(px, pub, nd)
                detail_rows.append(rec)

    if not detail_rows:
        print("NO FIRINGS FOUND -- aborting.")
        return

    detail = pd.DataFrame(detail_rows)
    detail.to_csv("data/cot_reversal_penalty_backtest_detail.csv", index=False)
    print(f"\nTotal (pair, direction, week) firing instances: {len(detail)}")

    # Build the non-firing baseline: for every pair/direction/week where the
    # SAME currency data existed but the penalty did NOT fire, for the same
    # z-test comparison used everywhere else in this codebase.
    baseline_rows = []
    for pair in UNIVERSE:
        base, quote = pair.split("/")
        if base not in momentum or quote not in momentum or pair not in price_cache:
            continue
        b_df, q_df = momentum[base], momentum[quote]
        merged = pd.merge(
            b_df, q_df, on="publication_date", how="outer", suffixes=("_base", "_quote"),
        ).sort_values("publication_date")
        px = price_cache[pair]
        for _, row in merged.iterrows():
            pub = row["publication_date"]
            for direction in ("BUY", "SELL"):
                def _mk2(prefix):
                    if pd.isna(row.get(f"cot_momentum_{prefix}")):
                        return None
                    return {
                        "cot_momentum": row.get(f"cot_momentum_{prefix}"),
                        "net_3w_ago": row.get(f"net_3w_ago_{prefix}"),
                        "one_year_high": row.get(f"one_year_high_{prefix}"),
                        "one_year_low": row.get(f"one_year_low_{prefix}"),
                        "momentum_delta_pct": row.get(f"momentum_delta_pct_{prefix}"),
                    }
                b, q = _mk2("base"), _mk2("quote")
                if b is None and q is None:
                    continue
                fake = fake_positioning_result(pair, direction, b, q)
                if _cot_reversal_penalty(fake) < 0:
                    continue   # this is a firing instance, already counted above
                rec = {"pair": pair, "direction": direction, "publication_date": pub}
                for nd in _FWD_DAYS:
                    rec[f"fwd_{nd}d"] = forward_return(px, pub, nd)
                baseline_rows.append(rec)
    baseline = pd.DataFrame(baseline_rows)
    print(f"Non-firing baseline instances: {len(baseline)}")

    def _hit(sub: pd.DataFrame, direction_col, nd: int) -> pd.Series:
        # A "hit" for a FIRING row = price moved AGAINST the penalized
        # direction (validates the penalty). For the baseline (no penalty),
        # "hit" = price moved WITH the proposed direction (the ordinary
        # sense of a correct trade), so the two hit rates are directly
        # comparable as "was the implied direction right".
        col = f"fwd_{nd}d"
        sign = sub[col].apply(lambda v: None if pd.isna(v) else (1 if v > 0 else (-1 if v < 0 else 0)))
        dir_sign = sub[direction_col].map({"BUY": 1, "SELL": -1})
        return sign == dir_sign

    results = []
    print("\n" + "=" * 100)
    print("AGGREGATE: does the penalty correctly flag a worse-than-baseline direction?")
    print("=" * 100)
    for nd in _FWD_DAYS:
        col = f"fwd_{nd}d"
        d_valid = detail.dropna(subset=[col])
        b_valid = baseline.dropna(subset=[col])
        # Penalty "correct" = direction LOSES (price moves against it) after firing.
        d_dir_hits = (_hit(d_valid, "direction", nd)).sum()   # direction "wins" despite penalty
        d_n = len(d_valid)
        b_dir_hits = (_hit(b_valid, "direction", nd)).sum()
        b_n = len(b_valid)
        z = _ztest(int(d_dir_hits), d_n, int(b_dir_hits), b_n)
        p_value, wr_fire, wr_base = z if z else (None, None, None)
        print(f"fwd={nd}d  penalized-direction win-rate={wr_fire*100 if wr_fire is not None else float('nan'):5.1f}% "
              f"(n={d_n})   non-penalized win-rate={wr_base*100 if wr_base is not None else float('nan'):5.1f}% "
              f"(n={b_n})   p={p_value if p_value is not None else float('nan'):.4f}")
        results.append({
            "scope": "aggregate", "pair": "ALL", "fwd_days": nd,
            "n_penalized": d_n, "n_baseline": b_n,
            "penalized_dir_win_rate": wr_fire, "baseline_dir_win_rate": wr_base,
            "p_value": p_value,
        })
    print("(If the penalty has real predictive value, the penalized-direction win rate should be")
    print(" MEANINGFULLY LOWER than the non-penalized baseline -- that's what 'the penalty correctly")
    print(" flags a worse setup' looks like. Indistinguishable win rates = no edge, same as Phase 01B.)")

    print("\n" + "=" * 100)
    print("BY FIRING TYPE (REVERSING vs UNWINDING) -- fwd=5d")
    print("=" * 100)
    for ftype in ("REVERSING", "UNWINDING"):
        sub = detail[detail["fired_type"] == ftype].dropna(subset=["fwd_5d"])
        if len(sub) < 10:
            print(f"{ftype}: n={len(sub)} -- too few to report")
            continue
        hits = _hit(sub, "direction", 5).sum()
        b_valid = baseline.dropna(subset=["fwd_5d"])
        b_hits = _hit(b_valid, "direction", 5).sum()
        z = _ztest(int(hits), len(sub), int(b_hits), len(b_valid))
        p_value, wr, wr_b = z if z else (None, None, None)
        print(f"{ftype:10s} n={len(sub):4d}  win_rate={wr*100:5.1f}%  (baseline={wr_b*100:5.1f}%, n={len(b_valid)})  p={p_value:.4f}")
        results.append({
            "scope": "by_type", "pair": ftype, "fwd_days": 5,
            "n_penalized": len(sub), "n_baseline": len(b_valid),
            "penalized_dir_win_rate": wr, "baseline_dir_win_rate": wr_b, "p_value": p_value,
        })

    print("\n" + "=" * 100)
    print("PER-CURRENCY (fwd=5d, n>=8) -- is any effect broad-based or a few correlated pairs?")
    print("=" * 100)
    b_valid_all = baseline.dropna(subset=["fwd_5d"])
    for ccy in sorted(detail["currency"].dropna().unique()):
        sub = detail[(detail["currency"] == ccy)].dropna(subset=["fwd_5d"])
        if len(sub) < 8:
            continue
        hits = _hit(sub, "direction", 5).sum()
        b_sub = b_valid_all[b_valid_all["pair"].str.contains(ccy)]
        b_hits = _hit(b_sub, "direction", 5).sum() if len(b_sub) else 0
        z = _ztest(int(hits), len(sub), int(b_hits), len(b_sub)) if len(b_sub) else None
        p_value, wr, wr_b = z if z else (None, None, None)
        print(f"{ccy:5s} n={len(sub):4d}  win_rate={wr*100 if wr is not None else float('nan'):5.1f}%  "
              f"(baseline n={len(b_sub)}, {wr_b*100 if wr_b is not None else float('nan'):5.1f}%)  "
              f"p={p_value if p_value is not None else float('nan'):.4f}")
        results.append({
            "scope": "per_currency", "pair": ccy, "fwd_days": 5,
            "n_penalized": len(sub), "n_baseline": len(b_sub),
            "penalized_dir_win_rate": wr, "baseline_dir_win_rate": wr_b, "p_value": p_value,
        })

    pd.DataFrame(results).to_csv("data/cot_reversal_penalty_backtest_results.csv", index=False)
    print("\nWrote data/cot_reversal_penalty_backtest_results.csv and "
          "data/cot_reversal_penalty_backtest_detail.csv")


if __name__ == "__main__":
    run()
