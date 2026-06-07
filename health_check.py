"""One-shot data health check — conserves Twelve Data quota (1 API call only)."""

import os
import sys

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

SEP = "-" * 60
PASS = "[ PASS ]"
FAIL = "[ FAIL ]"

results = {}


def section(title):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


# ── Step 1 & 2: key validation ──────────────────────────────────────────────
section("STEP 1 + 2  |  API Key Check")

key = os.getenv("TWELVE_DATA_KEY", "")
key_len = len(key)
key_looks_valid = key_len >= 20  # real keys are 32-char hex

print(f"  Key present : {'Yes' if key else 'No'}")
print(f"  Key length  : {key_len} chars")
print(f"  Looks valid : {'Yes (≥20 chars)' if key_looks_valid else 'No — too short or empty'}")

results["key_present"] = bool(key)
results["key_valid_shape"] = key_looks_valid

status = PASS if (key and key_looks_valid) else FAIL
print(f"\n  {status}  API key present and plausibly valid")


# ── Step 3 + 1: single live call ─────────────────────────────────────────────
section("STEP 3 + 1  |  Live Twelve Data Call  (EUR/USD 1day, 200 candles)")

TD_URL = "https://api.twelvedata.com/time_series"
raw_resp = None
data = None
http_status = None
rate_limit_headers = {}
candle_count = 0

try:
    raw_resp = requests.get(
        TD_URL,
        params={
            "symbol": "EUR/USD",
            "interval": "1day",
            "outputsize": 200,
            "format": "JSON",
            "apikey": key,
        },
        timeout=30,
    )
    http_status = raw_resp.status_code

    # Capture every rate-limit / quota header Twelve Data might send
    rl_header_names = [h for h in raw_resp.headers if "credit" in h.lower()
                       or "rate" in h.lower() or "limit" in h.lower()
                       or "quota" in h.lower() or "remaining" in h.lower()]
    rate_limit_headers = {h: raw_resp.headers[h] for h in rl_header_names}

    print(f"  HTTP status       : {http_status}")
    if rate_limit_headers:
        print("  Rate-limit headers:")
        for k, v in rate_limit_headers.items():
            print(f"    {k}: {v}")
    else:
        print("  Rate-limit headers: none returned (Twelve Data free tier "
              "does not expose per-request quota headers)")

    data = raw_resp.json()
    api_status = data.get("status", "unknown") if isinstance(data, dict) else "unknown"
    api_error  = data.get("message", "") if api_status == "error" else ""
    values     = data.get("values") if isinstance(data, dict) else None
    candle_count = len(values) if values else 0

    print(f"  API status field  : {api_status}")
    if api_error:
        print(f"  API error message : {api_error}")
    print(f"  Candles returned  : {candle_count}")
    if values:
        first = values[-1]  # oldest (API returns newest-first before we sort)
        last  = values[0]   # newest
        print(f"  Oldest candle     : {first['datetime']}  close={first['close']}")
        print(f"  Newest candle     : {last['datetime']}   close={last['close']}")

    results["http_ok"]       = http_status == 200
    results["api_ok"]        = api_status != "error"
    results["candles_ok"]    = candle_count >= 50
    all_live = results["http_ok"] and results["api_ok"] and results["candles_ok"]
    print(f"\n  {PASS if all_live else FAIL}  Live call succeeded with real candle data")

except Exception as exc:
    print(f"  ERROR: {exc}")
    results["http_ok"] = results["api_ok"] = results["candles_ok"] = False
    print(f"\n  {FAIL}  Live call failed")


# ── Step 4: local indicator computation ──────────────────────────────────────
section("STEP 4  |  Local RSI / MACD / SMA  (same logic as src/technical.py)")

indicator_ok = False
if data and data.get("values"):
    try:
        df = pd.DataFrame(data["values"])
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.set_index("datetime").sort_index()
        for col in ("open", "high", "low", "close"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df[["open", "high", "low", "close"]].dropna()
        close = df["close"]

        # RSI-14 (Wilder/EWM variant, matches technical.py exactly)
        delta    = close.diff()
        gain     = delta.clip(lower=0)
        loss     = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
        rs       = avg_gain / avg_loss.replace(0, np.nan)
        rsi      = (100 - 100 / (1 + rs)).iloc[-1]

        # MACD (12/26/9)
        ema12   = close.ewm(span=12, adjust=False).mean()
        ema26   = close.ewm(span=26, adjust=False).mean()
        macd_l  = ema12 - ema26
        signal  = macd_l.ewm(span=9, adjust=False).mean()
        hist    = (macd_l - signal).iloc[-1]
        macd_v  = macd_l.iloc[-1]
        sig_v   = signal.iloc[-1]

        # SMA-20 / SMA-50 / SMA-200
        sma20  = close.rolling(20).mean().iloc[-1]
        sma50  = close.rolling(50).mean().iloc[-1]
        sma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else float("nan")

        print(f"  Candles used for indicators : {len(close)}")
        print(f"  Last close (EUR/USD)        : {close.iloc[-1]:.5f}")
        print()
        print(f"  RSI-14     : {rsi:.2f}  "
              f"({'overbought >70' if rsi > 70 else 'oversold <30' if rsi < 30 else 'neutral'})")
        print(f"  MACD       : {macd_v:.6f}")
        print(f"  MACD signal: {sig_v:.6f}")
        print(f"  MACD hist  : {hist:.6f}  "
              f"({'bullish' if hist > 0 else 'bearish'})")
        print(f"  SMA-20     : {sma20:.5f}")
        print(f"  SMA-50     : {sma50:.5f}")
        sma200_str = f"{sma200:.5f}" if not np.isnan(sma200) else "n/a (need ≥200 candles)"
        print(f"  SMA-200    : {sma200_str}")

        indicator_ok = True
        print(f"\n  {PASS}  RSI, MACD, SMA computed successfully from live candles")
    except Exception as exc:
        print(f"  ERROR computing indicators: {exc}")
        print(f"\n  {FAIL}  Indicator computation failed")
else:
    print("  Skipped — no candle data available from Step 1")
    print(f"\n  {FAIL}  Indicator computation skipped")

results["indicators_ok"] = indicator_ok


# ── Step 5: environment detection ────────────────────────────────────────────
section("STEP 5  |  Execution Environment")

on_gh_actions = os.getenv("GITHUB_ACTIONS") == "true"
ci_runner     = os.getenv("RUNNER_OS", "")
workflow_name = os.getenv("GITHUB_WORKFLOW", "")
python_ver    = sys.version.split()[0]

env_label = "GitHub Actions" if on_gh_actions else "Local machine (Windows)"
print(f"  Environment        : {env_label}")
print(f"  Python             : {python_ver}")
if on_gh_actions:
    print(f"  Runner OS          : {ci_runner}")
    print(f"  Workflow           : {workflow_name}")
print()

# Check whether env vars that daily.py needs are present (same check either side)
needed_keys = ["ANTHROPIC_API_KEY", "TWELVE_DATA_KEY", "NEWS_API_KEY", "FRED_API_KEY"]
missing = [k for k in needed_keys if not os.getenv(k)]
if missing:
    print(f"  Missing env vars   : {', '.join(missing)}")
    print("  NOTE: on GitHub Actions these must be set as repo secrets")
else:
    print("  All required env vars present")

env_ok = not missing
print(f"\n  {PASS if env_ok else FAIL}  "
      f"Environment check ({'local' if not on_gh_actions else 'GitHub Actions'})")
print("  Pipeline would run identically on GitHub Actions provided secrets are set.")

results["env_ok"] = env_ok


# ── Summary ──────────────────────────────────────────────────────────────────
section("SUMMARY")

checks = [
    ("API key present & valid shape",        results.get("key_present") and results.get("key_valid_shape")),
    ("HTTP 200 from Twelve Data",            results.get("http_ok")),
    ("API returned real candle data",        results.get("candles_ok")),
    ("RSI / MACD / SMA computed locally",   results.get("indicators_ok")),
    ("All required env vars present",        results.get("env_ok")),
]

all_pass = True
for label, ok in checks:
    tag = PASS if ok else FAIL
    print(f"  {tag}  {label}")
    if not ok:
        all_pass = False

print()
overall = "ALL CHECKS PASSED" if all_pass else "ONE OR MORE CHECKS FAILED"
print(f"  Overall: {overall}")
print(SEP)
print()
