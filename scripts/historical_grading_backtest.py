"""Multi-year mechanical grading/technical backtest -- DETERMINISTIC PARTS ONLY.

Requested 2026-09-04 to scale up the daily-candle-freeze fix's own validation
methodology (425 candidates, ~7 weeks, real historical Yahoo OHLC replayed
through the real production functions) to a multi-year, multi-regime
population -- far larger than live scanning can produce in anything under a
year, and specifically able to resolve at scale whether the ribbon-regime
carve-out's F-vs-D-style question (shadow_mode.py's
ribbon_carveout_exclude_trending_risk_on: p=0.38, n too small as of
2026-09-04, see PROMOTION_DISCIPLINE.md) is a real, structural pattern or
noise.

============================================================================
WHAT THIS DOES AND DOES NOT VALIDATE -- READ BEFORE TRUSTING ANY NUMBER BELOW
============================================================================
Tests ONLY the deterministic, non-LLM parts of the pipeline: the real
production functions from src/technical.py (_summarise, _ema_ribbon,
_tech_signal), src/mtf.py (_tf_signal, the weekly/daily agreement gate), and
the ribbon-opposition / weekly-daily-conflict conditions from
_trade_quality_grade() (daily.py). The mechanical 2:1 R:R trade construction
(entry = close, stop = 1.0x ATR14 rounded to nearest 5 pips, target = 2x
stop distance) mirrors _calc_indicative_levels()'s own ATR-fallback path and
cascade.py's TARGET_RR=2.0 exactly.

Does NOT and cannot validate: Haiku/Sonnet's qualitative confidence score,
Devil's Advocate, currency-consensus, fundamental/sentiment/positioning/macro
scoring, or any part of _trade_quality_grade() that depends on the LLM's own
`confidence` value (the full A/B/C/D/F ladder is NOT reproduced here; see
"WHY NOT THE FULL LADDER" below). None of those can be replayed against a
historical date without an actual LLM call, which risks the model's own
training knowledge contaminating the result -- deliberately not attempted.

WHY NOT THE FULL LADDER: _trade_quality_grade()'s real logic is:
  F: rib_strongly_against OR w_d_conflict OR rr<1.5
  D: (not F) AND (rib_against OR rr<2.0 OR (not w_d_agree AND conf<=6))
  C/B/A: all require conf>=6/7/8 respectively.
rr is fixed at exactly 2.0 by this script's own mechanical construction (same
as real production trades -- confirmed session-wide that cascade.py has only
ever produced 2.0 R:R since 2026-06-30), so rr<1.5/rr<2.0 never fire here,
exactly matching live reality. That leaves F fully deterministic
(rib_strongly_against OR w_d_conflict) and D partially so (the rib_against
branch is deterministic; the "not w_d_agree and conf<=6" branch is not, since
conf is an LLM output this script cannot reconstruct without contaminating
the result). This script therefore reports THREE deterministic-only buckets,
not a 5-tier grade:
  - "would_F"       : rib_strongly_against OR w_d_conflict
  - "would_D_ribbon": NOT would_F, AND rib_against
  - "clean"         : neither of the above (would need real confidence to
                      resolve further into C/B/A/remaining-D -- NOT attempted)

TWO-SIDED, SYMMETRIC TEST, NOT A SIMULATION OF WHAT THE LLM WOULD HAVE
PROPOSED: every pair/day is tested in BOTH directions (BUY and SELL)
independently, as a pure hypothesis test of the mechanical conditions'
predictive power -- not a simulation of which single direction an analyst
would actually have picked (that decision is exactly the LLM-judgment layer
this script deliberately does not attempt to reproduce).

Known backtest limitations, stated plainly rather than glossed over:
  - Same-bar target+stop ambiguity (daily OHLC, no intraday path): resolved
    conservatively as a LOSS (stop wins ties). This can only ever understate
    the reported win rate/PF, never inflate it.
  - No 4H timeframe (not reliably available for years of free historical FX
    data) -- all3_agree/A-grade is out of scope by construction, consistent
    with the deterministic-only, no-full-ladder scope above.
  - No news-calendar or fundamental/COT reconstruction -- no_news/fundamental
    tailwind conditions are out of scope (they only matter for grades this
    script doesn't attempt anyway).
  - Costs applied via trade_costs.net_pips_for_closed_trade() with
    base_rate/quote_rate=None (no historical interest-rate series
    reconstructed) -- spread/slippage dominate a 4-day hold, so this is a
    reasonable approximation, not exact for every historical year's swap
    cost.

Usage: python scripts/historical_grading_backtest.py [--years N] [--pairs P1,P2,...]
Writes results to data/historical_backtest_results.csv (one row per
candidate) and prints the aggregate report to stdout.
"""
import argparse
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from src import mtf
from src import technical as tech
from src import trade_costs
from src.selector import UNIVERSE

EVAL_YEARS = 3          # candidates are only generated in the most recent N years
FETCH_PERIOD = "10y"    # lookback history fetched (for indicator warmup), independent of EVAL_YEARS
EXPIRY_DAYS = 4          # matches daily.py._compute_expiry_days_from_rr(2.0) == 4
LOOKBACK_DAILY = 260     # bars fed to _summarise() each day -- bounds SMA200 + buffer
LOOKBACK_WEEKLY = 210    # weeks fed to _summarise() each week -- bounds weekly SMA200 + buffer
MIN_DAILY_HISTORY = 210  # minimum daily bars before the first evaluable date (ribbon needs 94+5)
MIN_WEEKLY_HISTORY = 40  # minimum weekly bars before weekly _summarise() is trusted at all


def _pip_size(pair: str) -> float:
    return 0.01 if pair.upper().endswith("JPY") else 0.0001


def _yf_ticker(pair: str) -> str:
    return pair.replace("/", "") + "=X"


def fetch_daily(pair: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(_yf_ticker(pair), period=FETCH_PERIOD, interval="1d",
                      progress=False, auto_adjust=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower() for c in df.columns]
    df = df[["open", "high", "low", "close"]].dropna()
    return df


def resample_weekly(df_daily: pd.DataFrame) -> pd.DataFrame:
    return df_daily.resample("W-FRI").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()


def evaluate_pair(pair: str, df_daily: pd.DataFrame, eval_years: int) -> list:
    weekly = resample_weekly(df_daily)
    n = len(df_daily)
    ps = _pip_size(pair)

    eval_start = df_daily.index.max() - pd.Timedelta(days=365 * eval_years)
    start_i = max(MIN_DAILY_HISTORY, int((df_daily.index < eval_start).sum()))
    end_i = n - EXPIRY_DAYS - 1  # need EXPIRY_DAYS of forward bars to resolve outcome

    records = []
    for i in range(start_i, end_i):
        date_t = df_daily.index[i]
        daily_slice = df_daily.iloc[max(0, i - LOOKBACK_DAILY): i + 1]

        w_idx = weekly.index.searchsorted(date_t, side="right") - 1
        if w_idx < MIN_WEEKLY_HISTORY:
            continue
        weekly_slice = weekly.iloc[max(0, w_idx - LOOKBACK_WEEKLY): w_idx + 1]

        try:
            daily_summary = tech._summarise(daily_slice, "daily", pair)
            weekly_summary = tech._summarise(weekly_slice, "weekly", pair)
        except Exception:
            continue
        if "tech_signal" not in daily_summary or "tech_signal" not in weekly_summary:
            continue

        d_sig = mtf._tf_signal(daily_summary)
        w_sig = mtf._tf_signal(weekly_summary)
        ribbon_status = (daily_summary.get("ribbon") or {}).get("status", "")
        w_d_conflict = (w_sig in ("BUY", "SELL") and d_sig in ("BUY", "SELL") and w_sig != d_sig)

        entry = float(daily_slice["close"].iloc[-1])
        atr = float(daily_summary.get("atr14") or 0)
        if not atr or atr <= 0:
            continue
        atr_pips = atr / ps
        stop_pips = max(round(atr_pips / 5) * 5, 5)
        stop_dist = stop_pips * ps

        for direction in ("BUY", "SELL"):
            rib_strongly_against = (
                (direction == "BUY" and ribbon_status == "ALIGNED_BEAR") or
                (direction == "SELL" and ribbon_status == "ALIGNED_BULL")
            )
            rib_against = (
                (direction == "BUY" and ribbon_status in ("ALIGNED_BEAR", "LEANING_BEAR")) or
                (direction == "SELL" and ribbon_status in ("ALIGNED_BULL", "LEANING_BULL"))
            )
            if rib_strongly_against or w_d_conflict:
                bucket = "would_F"
            elif rib_against:
                bucket = "would_D_ribbon"
            else:
                bucket = "clean"

            if direction == "BUY":
                stop, target = entry - stop_dist, entry + 2 * stop_dist
            else:
                stop, target = entry + stop_dist, entry - 2 * stop_dist

            outcome, exit_price, exit_offset = None, None, None
            for offset in range(1, EXPIRY_DAYS + 1):
                if i + offset >= n:
                    break
                bar = df_daily.iloc[i + offset]
                hit_target = (bar["high"] >= target) if direction == "BUY" else (bar["low"] <= target)
                hit_stop = (bar["low"] <= stop) if direction == "BUY" else (bar["high"] >= stop)
                if hit_stop:  # conservative same-bar tie-break: stop wins
                    outcome, exit_price, exit_offset = "LOSS", stop, offset
                    break
                if hit_target:
                    outcome, exit_price, exit_offset = "WIN", target, offset
                    break

            if outcome is None:
                last_offset = min(EXPIRY_DAYS, n - 1 - i)
                if last_offset < 1:
                    continue
                exit_price = float(df_daily["close"].iloc[i + last_offset])
                exit_offset = last_offset
                status_label = "EXPIRED"
            else:
                status_label = outcome

            gross_pips = (exit_price - entry) / ps if direction == "BUY" else (entry - exit_price) / ps
            exit_date = df_daily.index[i + exit_offset]
            days_held = max(1.0, (exit_date - date_t).days)
            try:
                net_pips = trade_costs.net_pips_for_closed_trade(
                    pair, direction, entry, gross_pips, days_held,
                )
            except Exception:
                net_pips = gross_pips

            records.append({
                "pair": pair, "date": date_t.strftime("%Y-%m-%d"), "direction": direction,
                "bucket": bucket, "ribbon_status": ribbon_status,
                "rib_against": rib_against, "rib_strongly_against": rib_strongly_against,
                "w_d_conflict": w_d_conflict, "w_sig": w_sig, "d_sig": d_sig,
                "status": status_label, "gross_pips": round(gross_pips, 1),
                "net_pips": round(net_pips, 1), "days_held": days_held,
            })
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=EVAL_YEARS)
    ap.add_argument("--pairs", type=str, default=None)
    args = ap.parse_args()

    pairs = args.pairs.split(",") if args.pairs else UNIVERSE
    all_records = []
    for pair in pairs:
        print(f"[{pair}] fetching {FETCH_PERIOD} history...", file=sys.stderr)
        try:
            df = fetch_daily(pair)
        except Exception as exc:
            print(f"[{pair}] fetch failed: {exc}", file=sys.stderr)
            continue
        if df.empty or len(df) < MIN_DAILY_HISTORY + EXPIRY_DAYS + 1:
            print(f"[{pair}] insufficient data ({len(df)} rows) -- skipped", file=sys.stderr)
            continue
        recs = evaluate_pair(pair, df, args.years)
        print(f"[{pair}] {len(recs)} candidate-evaluations", file=sys.stderr)
        all_records.extend(recs)

    if not all_records:
        print("No records produced -- aborting.", file=sys.stderr)
        return

    out = pd.DataFrame(all_records)
    out_path = Path("data/historical_backtest_results.csv")
    out.to_csv(out_path, index=False)
    print(f"\nWrote {len(out)} rows to {out_path}\n")

    def _is_win(status, net_pips):
        if status == "WIN":
            return True
        if status == "LOSS":
            return False
        return net_pips > 0  # EXPIRED, classified by net_pips sign

    out["is_win"] = out.apply(lambda r: _is_win(r["status"], r["net_pips"]), axis=1)

    def _report(df, label):
        n = len(df)
        if n == 0:
            print(f"{label}: n=0")
            return
        wins = int(df["is_win"].sum())
        wr = wins / n * 100
        win_pips = df.loc[df["net_pips"] > 0, "net_pips"].sum()
        loss_pips = -df.loc[df["net_pips"] < 0, "net_pips"].sum()
        pf = win_pips / loss_pips if loss_pips > 0 else float("inf")
        print(f"{label}: n={n}, WR={wr:.1f}%, PF={pf:.3f}")

    print("=" * 70)
    print("GRADE-CONDITIONAL EXPECTANCY (deterministic buckets only)")
    print("=" * 70)
    for bucket in ("would_F", "would_D_ribbon", "clean"):
        _report(out[out["bucket"] == bucket], bucket)

    print()
    print("=" * 70)
    print("RIBBON-OPPOSITION STANDALONE (rib_against, either bucket)")
    print("=" * 70)
    _report(out[out["rib_against"]], "rib_against=True")
    _report(out[~out["rib_against"]], "rib_against=False")

    print()
    print("=" * 70)
    print("BY PAIR (would_F vs would_D_ribbon)")
    print("=" * 70)
    for pair in sorted(out["pair"].unique()):
        sub = out[out["pair"] == pair]
        for bucket in ("would_F", "would_D_ribbon"):
            b = sub[sub["bucket"] == bucket]
            if len(b) >= 10:
                _report(b, f"{pair} / {bucket}")

    print()
    print("=" * 70)
    print("BY YEAR")
    print("=" * 70)
    out["year"] = pd.to_datetime(out["date"]).dt.year
    for year in sorted(out["year"].unique()):
        for bucket in ("would_F", "would_D_ribbon"):
            b = out[(out["year"] == year) & (out["bucket"] == bucket)]
            _report(b, f"{year} / {bucket}")


if __name__ == "__main__":
    main()
