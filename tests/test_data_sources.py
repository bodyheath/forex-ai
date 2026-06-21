"""Test new data sources: Stooq daily candles and Yahoo Finance 4H reconstruction.

Run from project root: python tests/test_data_sources.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd


def compute_atr(values: list, period: int = 14) -> float | None:
    """Compute ATR(14) from a Twelve Data-format values list."""
    if not values or len(values) < period:
        return None
    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[["open", "high", "low", "close"]].dropna()
    if len(df) < period:
        return None
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    val = float(atr.iloc[-1])
    return val if not np.isnan(val) else None


def check_ohlcv_sanity(values: list) -> list:
    """Return list of (datetime, issue) for bars failing basic OHLCV sanity."""
    bad = []
    for v in values:
        try:
            o = float(v["open"])
            h = float(v["high"])
            l = float(v["low"])
            c = float(v["close"])
        except (KeyError, ValueError, TypeError):
            bad.append((v.get("datetime", "?"), "unparseable values"))
            continue
        if h < l:
            bad.append((v["datetime"], f"high {h} < low {l}"))
        if o <= 0 or h <= 0 or l <= 0 or c <= 0:
            bad.append((v["datetime"], f"non-positive price"))
    return bad


STOOQ_PAIRS = ["AUD/JPY", "EUR/USD", "GBP/USD"]

print("=" * 65)
print("DATA SOURCES TEST")
print("=" * 65)

# ── TEST 1: Stooq daily candles ───────────────────────────────────────────────
print("\n[TEST 1] Stooq daily candles")

from src.stooq_data import fetch_candles as stooq_fetch

stooq_ok = 0
for pair in STOOQ_PAIRS:
    result = stooq_fetch(pair, 100, log=print)
    if result is None:
        print(f"  ❌ {pair}: Stooq returned None")
        continue
    vals = result.get("values", [])
    if not vals:
        print(f"  ❌ {pair}: Stooq returned empty values list")
        continue

    n    = len(vals)
    last = vals[0]   # newest-first → first entry is newest
    atr  = compute_atr(vals)
    bad  = check_ohlcv_sanity(vals)

    if atr is None or atr == 0.0:
        print(f"  ⚠️  {pair}: {n} candles returned but ATR={atr} (suspicious)")
    else:
        stooq_ok += 1
        print(f"  ✅ {pair}: {n} candles · last close={last['close']} · ATR(14)={atr:.5f}")
    if bad:
        for dt, issue in bad[:3]:
            print(f"     ⚠️  Bad bar at {dt}: {issue}")

# ── TEST 2: Yahoo Finance 4H reconstruction ───────────────────────────────────
print("\n[TEST 2] Yahoo Finance 4H reconstruction (AUD/JPY)")

from src.yahoo_finance import fetch_4h_candles

pair_4h   = "AUD/JPY"
result_4h = fetch_4h_candles(pair_4h, 100, log=print)
yf_4h_ok  = False

if result_4h is None:
    print(f"  ❌ {pair_4h}: 4H reconstruction returned None")
else:
    vals_4h = result_4h.get("values", [])
    if not vals_4h:
        print(f"  ❌ {pair_4h}: 4H reconstruction returned empty values list")
    else:
        n     = len(vals_4h)
        last  = vals_4h[0]
        atr   = compute_atr(vals_4h)
        bad   = check_ohlcv_sanity(vals_4h)

        print(f"  {n} 4H bars returned")
        print(f"  Last bar: {last['datetime']}  O={last['open']}  H={last['high']}  L={last['low']}  C={last['close']}")

        if bad:
            print(f"  ⚠️  {len(bad)} bars with OHLCV issues:")
            for dt, issue in bad[:3]:
                print(f"       {dt}: {issue}")
        else:
            print(f"  ✅ OHLCV sanity: high >= low for all {n} bars")

        if atr is None or atr == 0.0:
            print(f"  ⚠️  ATR(14) from 4H = {atr} (suspicious)")
        else:
            print(f"  ✅ ATR(14) from 4H = {atr:.5f}")
            yf_4h_ok = True

# ── SUMMARY ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 65)
print("SUMMARY")
print(f"Stooq daily candles: {stooq_ok}/{len(STOOQ_PAIRS)} pairs returned valid data")
print(f"Yahoo Finance 4H:    {'✅ valid data with non-zero ATR' if yf_4h_ok else '❌ failed or zero ATR'}")
print("=" * 65)
