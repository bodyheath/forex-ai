"""Expiry-window extension analysis -- built to answer one question with real
data, not estimation: of all EXPIRED research candidates, how many would have
hit their 2R target if given N more days, and what's the honest cost (how
many would instead have flipped from a flat expiry into a real stop-loss)?

Reuses real historical daily OHLC (same yfinance source as
scripts/historical_grading_backtest.py) to walk forward from each EXPIRED
candidate's actual expiry date through up to MAX_EXTENSION_DAYS additional
trading days, checking each day's high/low against the candidate's own real
target and stop levels.

Usage: python scripts/expiry_extension_analysis.py
Writes data/expiry_extension_results.csv (one row per EXPIRED candidate) and
prints the day-by-day recovery/cost curve to stdout.
"""
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

warnings.filterwarnings("ignore")

MAX_EXTENSION_DAYS = 15


def _pip_size(pair: str) -> float:
    return 0.01 if str(pair).upper().endswith("JPY") else 0.0001


def _yf_ticker(pair: str) -> str:
    return pair.replace("/", "") + "=X"


def fetch_daily(pair: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(_yf_ticker(pair), period="max", interval="1d",
                      progress=False, auto_adjust=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower() for c in df.columns]
    return df[["open", "high", "low", "close"]].dropna()


def main():
    research = pd.read_csv("data/research_trades.csv", encoding="utf-8-sig")
    exp = research[research["status"] == "EXPIRED"].copy()
    exp["closed_dt"] = pd.to_datetime(exp["closed_at"], errors="coerce")
    exp = exp.dropna(subset=["closed_dt", "entry", "stop_loss", "target"])
    print(f"Total EXPIRED candidates with full price data: {len(exp)}", file=sys.stderr)

    pairs = sorted(exp["pair"].unique())
    ohlc_cache = {}
    for pair in pairs:
        try:
            ohlc_cache[pair] = fetch_daily(pair)
            print(f"[{pair}] fetched {len(ohlc_cache[pair])} bars", file=sys.stderr)
        except Exception as exc:
            print(f"[{pair}] fetch failed: {exc}", file=sys.stderr)
            ohlc_cache[pair] = pd.DataFrame()

    records = []
    for _, row in exp.iterrows():
        pair = row["pair"]
        df = ohlc_cache.get(pair)
        if df is None or df.empty:
            continue
        direction = str(row["direction"]).upper()
        entry = float(row["entry"])
        stop = float(row["stop_loss"])
        target = float(row["target"])
        closed_date = row["closed_dt"].normalize()

        # Find the first bar strictly after the actual expiry date.
        after = df[df.index > closed_date]
        if after.empty:
            continue

        outcome = "UNRESOLVED"
        day_hit = None
        for day_offset, (idx, bar) in enumerate(after.head(MAX_EXTENSION_DAYS).iterrows(), start=1):
            hit_target = (bar["high"] >= target) if direction == "BUY" else (bar["low"] <= target)
            hit_stop = (bar["low"] <= stop) if direction == "BUY" else (bar["high"] >= stop)
            if hit_stop:  # conservative same-bar tie-break, matches the mechanical backtest
                outcome, day_hit = "STOP", day_offset
                break
            if hit_target:
                outcome, day_hit = "TARGET", day_offset
                break

        records.append({
            "id": row.get("id"), "pair": pair, "direction": direction,
            "closed_at": row["closed_at"], "outcome_if_extended": outcome,
            "days_to_resolve": day_hit,
        })

    out = pd.DataFrame(records)
    out.to_csv("data/expiry_extension_results.csv", index=False)
    print(f"\nWrote {len(out)} rows to data/expiry_extension_results.csv\n")

    n = len(out)
    print(f"Of {n} EXPIRED candidates checked against real subsequent price action:")
    print(f"  Would have hit TARGET within {MAX_EXTENSION_DAYS} extra days: "
          f"{(out['outcome_if_extended']=='TARGET').sum()} ({(out['outcome_if_extended']=='TARGET').mean()*100:.1f}%)")
    print(f"  Would have hit STOP within {MAX_EXTENSION_DAYS} extra days:   "
          f"{(out['outcome_if_extended']=='STOP').sum()} ({(out['outcome_if_extended']=='STOP').mean()*100:.1f}%)")
    print(f"  Still unresolved after {MAX_EXTENSION_DAYS} extra days:      "
          f"{(out['outcome_if_extended']=='UNRESOLVED').sum()} ({(out['outcome_if_extended']=='UNRESOLVED').mean()*100:.1f}%)")
    print()
    print("Cumulative curve -- extend by N extra days, what fraction of ALL EXPIRED candidates")
    print("would have resolved TARGET vs STOP by that point (rest still flat/unresolved):")
    print(f"{'Days':>5} {'cum% TARGET':>12} {'cum% STOP':>10} {'net (target-stop)':>18}")
    for day in range(1, MAX_EXTENSION_DAYS + 1):
        tgt = ((out["outcome_if_extended"] == "TARGET") & (out["days_to_resolve"] <= day)).sum()
        stp = ((out["outcome_if_extended"] == "STOP") & (out["days_to_resolve"] <= day)).sum()
        print(f"{day:>5} {tgt/n*100:>11.1f}% {stp/n*100:>9.1f}% {(tgt-stp)/n*100:>17.1f}%")


if __name__ == "__main__":
    main()
