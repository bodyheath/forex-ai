"""Builds the rich, deterministic-feature dataset for the 2026-09-25 mechanical
edge-mining research loop (Part B of that request).

Reuses historical_grading_backtest.py's exact real-function replay
(technical.py._summarise(), mtf.py._tf_signal(), the same mechanical 2:1 R:R
trade construction and EXPIRY_DAYS=4 mechanical resolution) over the same
3-year, 28-pair universe -- but extracts a much richer per-candidate feature
set from the SAME already-computed daily_summary/weekly_summary dicts
(previously most of this was computed then discarded), plus a rolling
ATR-percentile-vs-6-month-average feature (a new, cheap addition using the
same _atr() series already computed for the slice) and calendar features
(day-of-week, month).

Deliberately excludes fundamentals TAILWIND/HEADWIND and COT percentile from
this per-day-per-pair sweep: both require a SEPARATE, currency-level (not
pair-day-level) historical reconstruction (see
fundamentals_tailwind_headwind_backtest.py's real FRED/OECD CLI proxy
work) that doesn't parallelize the same way, and the fundamentals JPY-
exclusion finding is already a separately-validated, large-n (n=33,039,
p=1.3e-6) mechanical edge in its own right -- it doesn't need rediscovering
here. Sentiment/positioning-derived divergence score is excluded for the
same reason (both require news headlines / COT data at a specific
historical date, not reconstructable at this pair-day scale without a
paid data source).

TRAIN/HOLDOUT SPLIT: chronological (never random) -- the last EVAL_YEARS
years of history per pair, in date order, is split into a DISCOVERY slice
(the first HOLDOUT_FRACTION-complement of it) and a HOLDOUT slice (the
final HOLDOUT_FRACTION), stored as a "split" column so every downstream
analysis can filter to discovery-only until the one, final holdout check.

Usage: python scripts/mechanical_edge_mining_dataset.py
Writes data/mechanical_edge_mining_dataset.csv.
"""
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
from scripts.historical_grading_backtest import fetch_daily, resample_weekly, _pip_size

EVAL_YEARS = 3
EXPIRY_DAYS = 4
LOOKBACK_DAILY = 260
LOOKBACK_WEEKLY = 210
MIN_DAILY_HISTORY = 210
MIN_WEEKLY_HISTORY = 40
ATR_PCT_WINDOW = 126  # ~6 trading months, matches the live atr_percentile_6m convention
HOLDOUT_FRACTION = 0.30  # last 30% of each pair's eval window, chronologically

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "mechanical_edge_mining_dataset.csv"


def evaluate_pair_rich(pair: str, df_daily: pd.DataFrame) -> list:
    weekly = resample_weekly(df_daily)
    n = len(df_daily)
    ps = _pip_size(pair)

    eval_start = df_daily.index.max() - pd.Timedelta(days=365 * EVAL_YEARS)
    start_i = max(MIN_DAILY_HISTORY, int((df_daily.index < eval_start).sum()))
    end_i = n - EXPIRY_DAYS - 1
    if end_i <= start_i:
        return []
    split_i = start_i + int(round((end_i - start_i) * (1 - HOLDOUT_FRACTION)))

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
        ribbon = daily_summary.get("ribbon") or {}
        ribbon_status = ribbon.get("status", "")
        ribbon_fanning = bool(ribbon.get("fanning", False))
        w_d_conflict = (w_sig in ("BUY", "SELL") and d_sig in ("BUY", "SELL") and w_sig != d_sig)
        w_d_agree = (w_sig in ("BUY", "SELL") and d_sig == w_sig)

        entry = float(daily_slice["close"].iloc[-1])
        atr = float(daily_summary.get("atr14") or 0)
        if not atr or atr <= 0:
            continue
        atr_pips = atr / ps
        stop_pips = max(round(atr_pips / 5) * 5, 5)
        stop_dist = stop_pips * ps

        try:
            atr_series = tech._atr(daily_slice)
            atr_baseline = atr_series.tail(ATR_PCT_WINDOW).mean()
            atr_pct_6m = float(atr_series.iloc[-1] / atr_baseline) if atr_baseline and atr_baseline > 0 else None
        except Exception:
            atr_pct_6m = None

        rsi14 = daily_summary.get("rsi14")
        macd_hist = daily_summary.get("macd_hist")
        bb_position = daily_summary.get("bb_position")
        price_vs_200ma = daily_summary.get("price_vs_200ma")
        stoch_k = daily_summary.get("stochastic_k")
        cci = daily_summary.get("cci")
        osc = daily_summary.get("oscillator_confluence") or {}
        osc_direction = osc.get("direction", "NONE")
        osc_score = osc.get("score", 0)
        div = daily_summary.get("divergence") or {}
        div_bullish = div.get("bullish") is not None
        div_bearish = div.get("bearish") is not None
        dow = date_t.dayofweek  # 0=Mon .. 4=Fri
        month = date_t.month
        split = "discovery" if i < split_i else "holdout"

        for direction in ("BUY", "SELL"):
            rib_strongly_against = (
                (direction == "BUY" and ribbon_status == "ALIGNED_BEAR") or
                (direction == "SELL" and ribbon_status == "ALIGNED_BULL")
            )
            rib_against = (
                (direction == "BUY" and ribbon_status in ("ALIGNED_BEAR", "LEANING_BEAR")) or
                (direction == "SELL" and ribbon_status in ("ALIGNED_BULL", "LEANING_BULL"))
            )
            rib_aligned = (
                (direction == "BUY" and ribbon_status in ("ALIGNED_BULL", "LEANING_BULL")) or
                (direction == "SELL" and ribbon_status in ("ALIGNED_BEAR", "LEANING_BEAR"))
            )
            osc_agrees = (osc_direction == direction)
            osc_opposes = (osc_direction != "NONE" and osc_direction != direction)
            div_supports = (direction == "BUY" and div_bullish) or (direction == "SELL" and div_bearish)
            rsi_extreme_against = (
                (direction == "BUY" and rsi14 is not None and rsi14 < 30) or
                (direction == "SELL" and rsi14 is not None and rsi14 > 70)
            )
            price_above_200ma = (price_vs_200ma == "above")
            with_trend_200ma = (
                (direction == "BUY" and price_vs_200ma == "above") or
                (direction == "SELL" and price_vs_200ma == "below")
            )

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
                if hit_stop:
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
                net_pips = trade_costs.net_pips_for_closed_trade(pair, direction, entry, gross_pips, days_held)
            except Exception:
                net_pips = gross_pips

            records.append({
                "pair": pair, "date": date_t.strftime("%Y-%m-%d"), "direction": direction,
                "split": split,
                "ribbon_status": ribbon_status, "ribbon_fanning": ribbon_fanning,
                "rib_against": rib_against, "rib_strongly_against": rib_strongly_against,
                "rib_aligned": rib_aligned,
                "w_d_conflict": w_d_conflict, "w_d_agree": w_d_agree,
                "w_sig": w_sig, "d_sig": d_sig,
                "rsi14": rsi14, "rsi_extreme_against": rsi_extreme_against,
                "macd_hist": macd_hist, "macd_bullish": (macd_hist is not None and macd_hist > 0),
                "bb_position": bb_position,
                "price_vs_200ma": price_vs_200ma, "with_trend_200ma": with_trend_200ma,
                "stochastic_k": stoch_k, "cci": cci,
                "osc_direction": osc_direction, "osc_score": osc_score,
                "osc_agrees": osc_agrees, "osc_opposes": osc_opposes,
                "div_bullish": div_bullish, "div_bearish": div_bearish, "div_supports": div_supports,
                "atr_pct_6m": atr_pct_6m,
                "day_of_week": dow, "month": month,
                "status": status_label, "gross_pips": round(gross_pips, 1),
                "net_pips": round(net_pips, 1), "days_held": days_held,
            })
    return records


def main():
    all_records = []
    for idx, pair in enumerate(UNIVERSE):
        print(f"[{idx+1}/{len(UNIVERSE)}] {pair} fetching...", file=sys.stderr, flush=True)
        try:
            df = fetch_daily(pair)
        except Exception as exc:
            print(f"  fetch failed: {exc}", file=sys.stderr)
            continue
        if df.empty:
            print(f"  no data", file=sys.stderr)
            continue
        try:
            recs = evaluate_pair_rich(pair, df)
        except Exception as exc:
            print(f"  evaluate failed: {exc}", file=sys.stderr)
            continue
        print(f"  {len(recs)} candidate rows", file=sys.stderr, flush=True)
        all_records.extend(recs)

    out = pd.DataFrame(all_records)
    out.to_csv(OUT_PATH, index=False)
    print(f"\nWrote {len(out)} rows to {OUT_PATH}", file=sys.stderr)
    print(out["split"].value_counts(), file=sys.stderr)


if __name__ == "__main__":
    main()
