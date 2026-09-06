"""Step 1 backtest for the intraday 4H book (2026-09-06) -- same discipline as
scripts/historical_grading_backtest.py, adapted for a genuinely faster clock,
not a reinterpretation of the daily/weekly pipeline.

============================================================================
DATA AVAILABILITY -- CHECKED DIRECTLY, NOT ASSUMED
============================================================================
src/yahoo_finance.py::fetch_4h_candles() (the real production 4H function,
already live -- see src/technical.py:276) resamples 60 days of yfinance 1H
data into 4H bars for LIVE use. That 60-day window is that function's own
choice for a fast live fetch, not a hard ceiling -- checked directly:
yfinance's real limit for interval="1h" is ~730 days (verified: 60d/180d/
730d all returned real data; "max" behaves inconsistently and should not be
relied on). This script pulls the full 730-day window for backtesting
specifically; the live book's own fetch (below) still uses the existing
60-day production function unchanged, since 60 days (~360 4H bars) already
comfortably covers every indicator's lookback (ribbon ~99 bars, SMA200 on
the 4H series itself ~200 bars) and a live scan should stay fast.

Data quality spot-checked across a major, a JPY cross, and a minor cross:
~150 gaps >6h out of ~17,200 hourly rows each, at almost exactly 1/week --
consistent with normal weekend market closures, not a data quality problem.

============================================================================
WHAT TRANSFERS CLEANLY AND WHAT DOESN'T -- READ BEFORE TRUSTING ANY NUMBER
============================================================================
Reused UNCHANGED, same production functions as the daily mass backtest:
  src.technical._summarise() / _ema_ribbon() / _tech_signal()  (bar-count
    generic -- confirmed no calendar-date assumptions, just needs >=30 bars
    and works identically regardless of what timeframe label you give it)
  src.mtf._tf_signal()
  src.trade_costs.net_pips_for_closed_trade()  (days_held is already a
    float multiplied against a per-day swap rate -- fractional-day/hour
    holds work correctly with no changes needed)
  The exact same deterministic grade buckets historical_grading_backtest.py
    established: would_F (rib_strongly_against OR higher-tf conflict),
    would_D_ribbon (rib_against), clean. Same 2:1 mechanical R:R
    construction (entry=close, stop=1xATR14, target=2xstop) cascade.py's
    real production trades also use.

Does NOT transfer, adapted deliberately:
  MTF hierarchy: the daily pipeline checks daily vs weekly. There is no
    "weekly" for a 4H clock -- the higher-timeframe anchor here is DAILY
    (a real, one-level-down shift of the same structural relationship:
    4H:Daily plays the role Daily:Weekly played). w_d_conflict below
    means "4H vs Daily conflict", not "daily vs weekly".
  Full Haiku/Sonnet 5-layer scoring (fundamental/sentiment/positioning/
    macro): this is genuinely stale within a single day -- COT is weekly
    data, central-bank rates move over weeks/months, and none of it changes
    meaningfully between one 4H bar and the next. Re-running the full
    qualitative analysis every 4 hours would be expensive AND methodologically
    hollow (asking an LLM to re-score context that hasn't changed). NOT
    tested here, and NOT part of the live book's design either -- see the
    module docstring's counterpart for the live wiring for how Devil's
    Advocate (which sanity-checks a specific, genuinely-fresh technical
    setup, not the fundamental backdrop) is still used live despite this.
  ATR-based stop/target sizing: the FORMULA transfers (ATR is self-scaling
    to whatever bars you feed it), but the ABSOLUTE stop distance shrinks
    sharply at 4H vs daily, while spread/slippage/commission are roughly
    FIXED pip costs regardless of timeframe. This means real trading costs
    could eat a much larger share of a 4H trade's edge than a daily trade's
    -- this is exactly what this backtest measures directly (gross vs net
    pips), not merely hypothesized.
  Expiry windows: daily.py's/outcome_checker.py's expiry formulas are
    calibrated in CALENDAR DAYS for a pipeline that expects trades to take
    days-to-weeks. This backtest walks forward in 4H BARS and records how
    many bars each trade actually took to resolve, to calibrate a real
    bars-based expiry for the live book (src/intraday_outcome_checker.py)
    -- not reusing the daily day-count constants at all.

Usage: python scripts/intraday_4h_backtest.py
Writes data/intraday_4h_backtest_results.csv and prints the report.
"""
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import yfinance as yf

from src import mtf
from src import technical as tech
from src import trade_costs
from src.selector import UNIVERSE
from src.yahoo_finance import _pair_to_yahoo_symbol

FETCH_PERIOD_1H = "730d"     # yfinance's real ceiling for interval="1h" -- verified directly
EXPIRY_BARS_MAX = 60         # generous walk-forward ceiling for THIS analysis (10 days of 4H
                              # bars) -- the live book's real expiry is calibrated FROM this
                              # script's own bars-to-resolve distribution, not assumed upfront
MIN_4H_HISTORY  = 210        # mirrors historical_grading_backtest.py's MIN_DAILY_HISTORY
MIN_DAILY_HISTORY_4H = 210   # daily-series warmup for the higher-timeframe filter


def _pip_size(pair: str) -> float:
    return 0.01 if pair.upper().endswith("JPY") else 0.0001


def fetch_1h(pair: str) -> pd.DataFrame:
    symbol = _pair_to_yahoo_symbol(pair)
    df = yf.Ticker(symbol).history(period=FETCH_PERIOD_1H, interval="1h", auto_adjust=True)
    if df is None or df.empty:
        return pd.DataFrame()
    df.columns = [str(c).lower() for c in df.columns]
    df = df[["open", "high", "low", "close"]].dropna()
    if df.index.tz is not None:
        df.index = df.index.tz_convert("UTC").tz_localize(None)
    return df


def resample(df_1h: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Same aggregation as src.yahoo_finance.fetch_4h_candles(): first/max/min/last,
    drop the final (likely incomplete) bar."""
    out = df_1h.resample(rule).agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
    }).dropna()
    return out.iloc[:-1] if len(out) else out


def evaluate_pair(pair: str, df_4h: pd.DataFrame, df_daily: pd.DataFrame) -> list:
    n = len(df_4h)
    ps = _pip_size(pair)
    records = []

    for i in range(MIN_4H_HISTORY, n - 1):
        bar_time = df_4h.index[i]
        slice_4h = df_4h.iloc[max(0, i - 260): i + 1]   # same LOOKBACK_DAILY depth as the daily script

        d_idx = df_daily.index.searchsorted(bar_time, side="right") - 1
        if d_idx < MIN_DAILY_HISTORY_4H:
            continue
        slice_daily = df_daily.iloc[max(0, d_idx - 260): d_idx + 1]

        try:
            sum_4h = tech._summarise(slice_4h, "4h", pair)
            sum_daily = tech._summarise(slice_daily, "daily", pair)
        except Exception:
            continue
        if "tech_signal" not in sum_4h or "tech_signal" not in sum_daily:
            continue

        sig_4h = mtf._tf_signal(sum_4h)
        sig_daily = mtf._tf_signal(sum_daily)
        ribbon_status = (sum_4h.get("ribbon") or {}).get("status", "")
        # "w_d_conflict" here means 4H-vs-daily conflict -- the shifted hierarchy,
        # see module docstring.
        hf_conflict = (sig_daily in ("BUY", "SELL") and sig_4h in ("BUY", "SELL")
                       and sig_daily != sig_4h)

        entry = float(slice_4h["close"].iloc[-1])
        atr = float(sum_4h.get("atr14") or 0)
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
            if rib_strongly_against or hf_conflict:
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
            for offset in range(1, EXPIRY_BARS_MAX + 1):
                if i + offset >= n:
                    break
                bar = df_4h.iloc[i + offset]
                hit_target = (bar["high"] >= target) if direction == "BUY" else (bar["low"] <= target)
                hit_stop = (bar["low"] <= stop) if direction == "BUY" else (bar["high"] >= stop)
                if hit_stop:
                    outcome, exit_price, exit_offset = "LOSS", stop, offset
                    break
                if hit_target:
                    outcome, exit_price, exit_offset = "WIN", target, offset
                    break

            if outcome is None:
                last_offset = min(EXPIRY_BARS_MAX, n - 1 - i)
                if last_offset < 1:
                    continue
                exit_price = float(df_4h["close"].iloc[i + last_offset])
                exit_offset = last_offset
                status_label = "EXPIRED"
            else:
                status_label = outcome

            gross_pips = (exit_price - entry) / ps if direction == "BUY" else (entry - exit_price) / ps
            hours_held = exit_offset * 4.0
            try:
                net_pips = trade_costs.net_pips_for_closed_trade(
                    pair, direction, entry, gross_pips, hours_held / 24.0,
                )
            except Exception:
                net_pips = gross_pips

            records.append({
                "pair": pair, "bar_time": bar_time.strftime("%Y-%m-%d %H:%M"),
                "direction": direction, "bucket": bucket, "ribbon_status": ribbon_status,
                "rib_against": rib_against, "rib_strongly_against": rib_strongly_against,
                "hf_conflict": hf_conflict,
                "status": status_label, "gross_pips": round(gross_pips, 1),
                "net_pips": round(net_pips, 1), "bars_to_resolve": exit_offset,
                "hours_held": hours_held, "stop_pips": stop_pips,
            })
    return records


def main():
    all_records = []
    for pair in UNIVERSE:
        print(f"[{pair}] fetching {FETCH_PERIOD_1H} of 1H data...", file=sys.stderr)
        try:
            df_1h = fetch_1h(pair)
        except Exception as exc:
            print(f"[{pair}] fetch failed: {exc}", file=sys.stderr)
            continue
        if df_1h.empty:
            print(f"[{pair}] no data -- skipped", file=sys.stderr)
            continue
        df_4h = resample(df_1h, "4h")
        df_daily = resample(df_1h, "1D")
        if len(df_4h) < MIN_4H_HISTORY + EXPIRY_BARS_MAX + 1 or len(df_daily) < MIN_DAILY_HISTORY_4H:
            print(f"[{pair}] insufficient resampled data (4h={len(df_4h)}, daily={len(df_daily)}) -- skipped",
                  file=sys.stderr)
            continue
        recs = evaluate_pair(pair, df_4h, df_daily)
        print(f"[{pair}] {len(recs)} candidate-evaluations", file=sys.stderr)
        all_records.extend(recs)

    if not all_records:
        print("No records produced -- aborting.")
        return

    out = pd.DataFrame(all_records)
    out_path = Path("data/intraday_4h_backtest_results.csv")
    out.to_csv(out_path, index=False)
    print(f"\nWrote {len(out)} rows to {out_path}\n")

    def _is_win(status, net_pips):
        if status == "WIN":
            return True
        if status == "LOSS":
            return False
        return net_pips > 0

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
        gross_win_pips = df.loc[df["gross_pips"] > 0, "gross_pips"].sum()
        gross_loss_pips = -df.loc[df["gross_pips"] < 0, "gross_pips"].sum()
        gross_pf = gross_win_pips / gross_loss_pips if gross_loss_pips > 0 else float("inf")
        print(f"{label}: n={n}, WR={wr:.1f}%, PF(net)={pf:.3f}, PF(gross, no costs)={gross_pf:.3f}")

    print("=" * 78)
    print("GRADE-CONDITIONAL EXPECTANCY (deterministic buckets, net of real costs)")
    print("=" * 78)
    for bucket in ("would_F", "would_D_ribbon", "clean"):
        _report(out[out["bucket"] == bucket], bucket)

    print()
    print("=" * 78)
    print("COST EROSION CHECK -- mean net pips as a fraction of mean stop_pips, by bucket")
    print("=" * 78)
    for bucket in ("would_F", "would_D_ribbon", "clean"):
        sub = out[out["bucket"] == bucket]
        if len(sub):
            avg_cost = (sub["gross_pips"] - sub["net_pips"]).abs().mean()
            avg_stop = sub["stop_pips"].mean()
            print(f"{bucket}: avg |gross-net| cost = {avg_cost:.2f} pips, "
                  f"avg stop = {avg_stop:.1f} pips ({avg_cost/avg_stop*100:.1f}% of stop)")

    print()
    print("=" * 78)
    print("BARS-TO-RESOLVE DISTRIBUTION (clean + would_D_ribbon, decisive only) -- for expiry calibration")
    print("=" * 78)
    decisive = out[(out["bucket"] != "would_F") & (out["status"].isin(["WIN", "LOSS"]))]
    for pct in (0.5, 0.7, 0.8, 0.9, 0.95):
        print(f"  p{int(pct*100)}: {decisive['bars_to_resolve'].quantile(pct):.1f} bars "
              f"({decisive['bars_to_resolve'].quantile(pct)*4:.0f}h)")
    print(f"  EXPIRED (never resolved within {EXPIRY_BARS_MAX} bars): "
          f"{(out['status']=='EXPIRED').sum()} / {len(out)} "
          f"({(out['status']=='EXPIRED').mean()*100:.1f}%)")

    print()
    print("=" * 78)
    print("BY PAIR (clean bucket only, n>=30)")
    print("=" * 78)
    for pair in sorted(out["pair"].unique()):
        sub = out[(out["pair"] == pair) & (out["bucket"] == "clean")]
        if len(sub) >= 30:
            _report(sub, pair)


if __name__ == "__main__":
    main()
