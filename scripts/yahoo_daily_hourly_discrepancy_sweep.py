"""Historical sweep: how often does Yahoo Finance's own daily ("1d") OHLC
bar disagree with its own hourly ("1h") bars for the same trading day, and
by how much? Built to investigate and quantify the real root cause behind
fund trade #6987 (USD/JPY, 2026-09-08): its entry was priced at 156.197 --
sourced from src/technical.py's daily.last_close, itself sourced from
Yahoo's native daily bar -- while Yahoo's OWN hourly bars for the exact
same trading day put the real close near 154.16, and the scan's own
scan_price_snapshot.json (written moments earlier, same run) recorded the
真 live price as 153.322. A ~290 pip (1.875% of price) gap between two
data feeds from the SAME provider, for the SAME symbol, on the SAME day.

This is NOT a caching bug in this repo -- confirmed via two independent,
cache-bypassing live Yahoo fetches (different lookback periods, same wrong
daily value both times). This script asks the only question that matters
for calibrating a defence against it: is #6987 a rare, essentially
unrepeatable fluke, or a routine feature of this data source?

============================================================================
METHOD
============================================================================
For 12 major/cross pairs, over ~2 years of real history:
  1. Fetch native "1d" and "1h" interval data directly from Yahoo Finance
     (via yfinance), independently -- two different endpoints, not the
     same underlying series resampled.
  2. For each daily bar (except the last, possibly still-forming), find its
     TRUE window end using Yahoo's OWN next daily bar's start timestamp
     (not a guessed timezone/rollover convention -- Yahoo's own consecutive
     daily-bar boundaries are definitionally correct for however Yahoo
     itself defines a trading day).
  3. Find the last hourly bar strictly before that boundary and compare its
     close to the daily bar's own close. Skip a day if the nearest hourly
     bar is more than 6 hours from the boundary (a real data gap/holiday --
     skipped rather than fabricating a false comparison).
  4. Report both raw pips AND percent-of-price (pips alone isn't
     comparable across JPY vs non-JPY pairs; percent is).

Reports the full distribution (median/percentiles/max), a per-pair
breakdown, and how many real days exceed a range of candidate thresholds
-- the evidence src/technical.py's _DAILY_CLOSE_SANITY_PCT constant is
calibrated against (see that constant's comment for the number actually
chosen and why).

Usage: python scripts/yahoo_daily_hourly_discrepancy_sweep.py
Writes data/yahoo_daily_hourly_discrepancy_sweep.csv.
"""
import yfinance as yf
import pandas as pd

PAIRS = {
    "EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X", "USD/JPY": "JPY=X",
    "USD/CHF": "USDCHF=X", "USD/CAD": "USDCAD=X", "AUD/USD": "AUDUSD=X",
    "NZD/USD": "NZDUSD=X", "EUR/JPY": "EURJPY=X", "GBP/JPY": "GBPJPY=X",
    "EUR/GBP": "EURGBP=X", "AUD/JPY": "AUDJPY=X", "EUR/AUD": "EURAUD=X",
}

_MAX_BOUNDARY_GAP_HOURS = 6  # beyond this, treat as a real data gap, not a comparison


def _pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def run():
    rows = []
    for pair, sym in PAIRS.items():
        print(f"[fetch] {pair} ({sym})...")
        t = yf.Ticker(sym)
        daily = t.history(period="2y", interval="1d", auto_adjust=True)
        hourly = t.history(period="730d", interval="1h", auto_adjust=True)
        if daily.empty or hourly.empty:
            print(f"  -> no data for {pair}")
            continue
        daily.columns = [c.lower() for c in daily.columns]
        hourly.columns = [c.lower() for c in hourly.columns]

        hourly_utc = hourly.copy()
        hourly_utc.index = hourly_utc.index.tz_convert("UTC")
        daily_utc_index = daily.index.tz_convert("UTC")

        ps = _pip_size(pair)
        n_compared = 0
        for idx in range(len(daily) - 1):
            d_close = daily["close"].iloc[idx]
            window_end = daily_utc_index[idx + 1]
            candidates = hourly_utc[hourly_utc.index < window_end]
            if candidates.empty:
                continue
            h_close = candidates["close"].iloc[-1]
            h_ts    = candidates.index[-1]
            gap_hours = (window_end - h_ts).total_seconds() / 3600
            if gap_hours > _MAX_BOUNDARY_GAP_HOURS:
                continue
            diff_pips = abs(d_close - h_close) / ps
            pct_diff  = abs(d_close - h_close) / h_close * 100.0
            rows.append({
                "pair": pair, "date": str(daily.index[idx].date()),
                "daily_close": d_close, "hourly_close": h_close,
                "diff_pips": diff_pips, "pct_diff": pct_diff, "gap_hours": gap_hours,
            })
            n_compared += 1
        print(f"  -> {n_compared} days compared")

    df = pd.DataFrame(rows)
    df.to_csv("data/yahoo_daily_hourly_discrepancy_sweep.csv", index=False)

    print(f"\nTotal comparisons: {len(df)}")
    print("\n=== Overall distribution: percent-of-price discrepancy ===")
    print(df["pct_diff"].describe(percentiles=[.5, .75, .9, .95, .99, .999]))
    print("\n=== Per-pair median / p99 / max (percent-of-price) ===")
    print(df.groupby("pair")["pct_diff"].agg(
        ["median", lambda s: s.quantile(0.99), "max", "count"]
    ).rename(columns={"<lambda_0>": "p99"}))
    print("\n=== Worst 15 individual days (percent-of-price) ===")
    print(df.sort_values("pct_diff", ascending=False).head(15).to_string())
    print("\n=== How many real days exceed candidate thresholds (percent-of-price) ===")
    for thr in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]:
        n = (df["pct_diff"] > thr).sum()
        print(f"  > {thr}%: {n:5d} / {len(df)} ({n / len(df) * 100:.2f}%)")

    print("\nWrote data/yahoo_daily_hourly_discrepancy_sweep.csv")


if __name__ == "__main__":
    run()
