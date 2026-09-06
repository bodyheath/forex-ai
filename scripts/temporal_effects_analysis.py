"""Session and day-of-week effect analysis (2026-09-07) -- same backtest-
first discipline as Positioning/Carry-Macro: real historical data, no live
code until something clears a real bar.

============================================================================
DATA SOURCE NOTE -- READ BEFORE TRUSTING EITHER RESULT
============================================================================
Q2 (day-of-week) reuses Phase 02's exact mechanical grading backtest
population (data/historical_backtest_results.csv, 43,344 rows, daily bars,
2 years, 28 pairs) as asked.

Q1 (session) CANNOT use that same dataset -- checked directly: its `date`
column is a bare YYYY-MM-DD string with no time-of-day information at all,
because it's built from DAILY OHLC bars. "Which session would this
candidate have opened in" is an intraday question a daily-bar dataset
structurally cannot answer, no matter how it's sliced. Substituted
data/intraday_4h_backtest_results.csv instead (184,138 rows, real 4H OHLC,
2 years, 28 pairs, same mechanical grade-bucket/2:1-R:R methodology) --
the only dataset in this repo with real intraday timestamps.

Important caveat this substitution carries: the 4H intraday backtest was
already found to be a clean NO-GO on its own (PF<1.0 even gross of costs,
every pair, every grade bucket -- see scripts/intraday_4h_backtest.py). Any
session effect found here is a question of RELATIVE performance within an
already-unprofitable population, not a validated absolute edge -- reported
honestly as exactly that, not smuggled in as if it came from a proven
strategy.

============================================================================
METHOD
============================================================================
Both checks use the "tradeable" population only (bucket != would_F -- i.e.
clean + would_D_ribbon, matching what a real gate would ever consider
opening), same net-of-cost pips, same two-proportion z-test
(src.shadow_mode._ztest) already used for Carry/Macro's per-pair check.
Every bucket is compared against the pooled REST of the tradeable
population (not against 50%), and the report states plainly how many
buckets were tested and what's expected to clear p<0.05 by chance alone --
the same multiple-comparisons framing Carry/Macro's per-pair table used.

Usage: python scripts/temporal_effects_analysis.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src.shadow_mode import _ztest

ALPHA = 0.05


def _is_win(status, net_pips):
    if status == "WIN":
        return True
    if status == "LOSS":
        return False
    return net_pips > 0  # EXPIRED, classified by net_pips sign


def _report_buckets(df: pd.DataFrame, bucket_col: str, label: str, order: list = None) -> None:
    df = df.copy()
    df["is_win"] = df.apply(lambda r: _is_win(r["status"], r["net_pips"]), axis=1)
    buckets = order or sorted(df[bucket_col].unique())
    n_tested = 0
    n_significant = 0

    print(f"\n{'='*90}\n{label}\n{'='*90}")
    print(f"{'bucket':22s} {'n':>7s} {'WR':>7s} {'PF':>7s} {'mean_net_pips':>14s} {'p_vs_rest':>10s}")
    for b in buckets:
        sub = df[df[bucket_col] == b]
        rest = df[df[bucket_col] != b]
        n = len(sub)
        if n == 0:
            continue
        wins = int(sub["is_win"].sum())
        wr = wins / n * 100
        win_pips = sub.loc[sub["net_pips"] > 0, "net_pips"].sum()
        loss_pips = -sub.loc[sub["net_pips"] < 0, "net_pips"].sum()
        pf = win_pips / loss_pips if loss_pips > 0 else float("inf")
        mean_net = sub["net_pips"].mean()

        rest_wins = int(rest["is_win"].sum())
        z = _ztest(wins, n, rest_wins, len(rest))
        p_str = "n/a"
        if z is not None:
            p_value, wr_b, wr_rest = z
            p_str = f"{p_value:.4f}"
            if n >= 30 and len(rest) >= 30:
                n_tested += 1
                if p_value < ALPHA:
                    n_significant += 1
        print(f"{b:22s} {n:7d} {wr:6.1f}% {pf:7.3f} {mean_net:14.2f} {p_str:>10s}")

    expected = n_tested * ALPHA
    print(f"\n{n_significant}/{n_tested} buckets (n>=30 both sides) cleared p<{ALPHA} vs pooled rest. "
          f"Expected by chance alone: ~{expected:.2f}.")


def session_for_hour(hour: int) -> str:
    # 4H bars land exactly on 00/04/08/12/16/20 UTC. Standard session
    # approximations (DST shifts each real session by ~1h either way,
    # immaterial at 4H bucket resolution):
    #   Asian:    Tokyo ~00:00-08:00 UTC
    #   London:   ~08:00-13:00 UTC (pre-NY-open London-only hours)
    #   Overlap:  ~12:00-16:00 UTC (London-NY overlap, highest liquidity)
    #   NY:       ~16:00-22:00 UTC (NY-only hours after London closes)
    # 12:00 bar (opens 12:00, spans to 16:00) is the overlap window;
    # 08:00 bar (opens 08:00, spans to 12:00) is pre-overlap London.
    return {0: "Asian", 4: "Asian", 8: "London", 12: "Overlap", 16: "NY", 20: "NY"}.get(hour, "Unknown")


def run_session_analysis() -> None:
    print("\n### Q1: SESSION EFFECT (data/intraday_4h_backtest_results.csv) ###")
    df = pd.read_csv("data/intraday_4h_backtest_results.csv")
    df = df[df["bucket"] != "would_F"].copy()
    df["hour"] = pd.to_datetime(df["bar_time"]).dt.hour
    df["session"] = df["hour"].apply(session_for_hour)
    print(f"Tradeable population (bucket != would_F): {len(df)} rows")
    _report_buckets(df, "session", "SESSION (all tradeable pairs pooled)",
                     order=["Asian", "London", "Overlap", "NY"])

    # Per-pair sanity check on the Overlap window specifically, since that's
    # the one the user singled out -- same "don't trust one flattering
    # aggregate without checking it's not one or two pairs carrying it"
    # discipline as Carry/Macro's per-pair table.
    print(f"\n{'='*90}\nOVERLAP WINDOW BY PAIR (n>=30 only) -- is any aggregate effect broad-based?\n{'='*90}")
    df["is_win"] = df.apply(lambda r: _is_win(r["status"], r["net_pips"]), axis=1)
    for pair in sorted(df["pair"].unique()):
        sub = df[(df["pair"] == pair) & (df["session"] == "Overlap")]
        rest = df[(df["pair"] == pair) & (df["session"] != "Overlap")]
        if len(sub) < 30 or len(rest) < 30:
            continue
        wins = int(sub["is_win"].sum())
        wr = wins / len(sub) * 100
        z = _ztest(wins, len(sub), int(rest["is_win"].sum()), len(rest))
        p_str = f"{z[0]:.4f}" if z else "n/a"
        print(f"{pair:10s} n={len(sub):5d} WR={wr:5.1f}% (rest={z[2]*100 if z else float('nan'):5.1f}%) p={p_str}")


def run_day_of_week_analysis() -> None:
    print("\n\n### Q2: DAY-OF-WEEK EFFECT (data/historical_backtest_results.csv, Phase 02) ###")
    df = pd.read_csv("data/historical_backtest_results.csv")
    df = df[df["bucket"] != "would_F"].copy()
    df["dow"] = pd.to_datetime(df["date"]).dt.day_name()
    print(f"Tradeable population (bucket != would_F): {len(df)} rows")
    _report_buckets(df, "dow", "DAY OF WEEK (all tradeable pairs pooled)",
                     order=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])

    print(f"\n{'='*90}\nFRIDAY BY PAIR (n>=15 only) -- weekend-gap-risk check, is it broad-based?\n{'='*90}")
    df["is_win"] = df.apply(lambda r: _is_win(r["status"], r["net_pips"]), axis=1)
    for pair in sorted(df["pair"].unique()):
        sub = df[(df["pair"] == pair) & (df["dow"] == "Friday")]
        rest = df[(df["pair"] == pair) & (df["dow"] != "Friday")]
        if len(sub) < 15 or len(rest) < 30:
            continue
        wins = int(sub["is_win"].sum())
        wr = wins / len(sub) * 100
        z = _ztest(wins, len(sub), int(rest["is_win"].sum()), len(rest))
        p_str = f"{z[0]:.4f}" if z else "n/a"
        print(f"{pair:10s} n={len(sub):5d} WR={wr:5.1f}% (rest={z[2]*100 if z else float('nan'):5.1f}%) p={p_str}")


if __name__ == "__main__":
    run_session_analysis()
    run_day_of_week_analysis()
