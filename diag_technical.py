"""Technical scoring diagnostic — run before any live scan to verify T:1 fix.

Tests:
  1. Unit-tests _tech_signal() across the full RSI range — every result must be T:3+
  2. Fetches live candle data for the top 15 pairs (uses selector output when available)
  3. Always tests AUD/CHF explicitly
  4. Prints a table: Pair | RSI | MACDh | BB | T_sig | Pass/Fail
  5. Flags any T:1 or T:2 that appears alongside real candle data

Usage:
  python diag_technical.py            # live data
  python diag_technical.py --cached   # cache only, no extra API calls
"""

import sys
import time
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import config
from src import technical as _tech

# Fallback list if selector is unavailable
_FALLBACK_PAIRS = [
    "AUD/CHF", "AUD/JPY", "AUD/USD", "AUD/NZD", "AUD/CAD",
    "EUR/AUD", "GBP/AUD", "EUR/USD", "GBP/USD", "USD/JPY",
    "EUR/JPY", "GBP/JPY", "USD/CHF", "EUR/GBP", "NZD/USD",
]

_SEP  = "=" * 72
_DASH = "-" * 72


# ── Unit tests ─────────────────────────────────────────────────────────────────

def _unit_tests() -> bool:
    """Verify _tech_signal() produces T:3+ for every RSI value with real data."""
    print(f"\n{_SEP}")
    print("UNIT TEST  —  _tech_signal() floor rule (T:1 must never appear with real data)")
    print(_SEP)

    # (rsi, macd_hist, bb_state, trend, close, sma20, sma50)
    cases = [
        (24.0,  0.002, "at/below lower band (stretched, mean-reversion risk up)",
         "downtrend (price < SMA50 < SMA200, death-cross structure)", 0.56, 0.57, 0.58),
        (32.0,  0.001, "inside bands",
         "uptrend (price > SMA50 > SMA200, golden-cross structure)",  1.08, 1.07, 1.06),
        (40.0,  0.000, "inside bands",
         "mixed / range (price and MAs not aligned)",                  1.08, 1.08, 1.07),
        (47.0,  0.000, "inside bands",
         "mixed / range (price and MAs not aligned)",                  1.08, 1.08, 1.08),
        (50.0, -0.001, "inside bands",
         "mixed / range (price and MAs not aligned)",                  1.08, 1.08, 1.09),
        (53.0, -0.001, "inside bands",
         "mixed / range (price and MAs not aligned)",                  1.09, 1.08, 1.07),
        (58.0, -0.002, "inside bands",
         "downtrend (price < SMA50 < SMA200, death-cross structure)", 1.06, 1.07, 1.08),
        (67.0, -0.003, "inside bands",
         "downtrend (price < SMA50 < SMA200, death-cross structure)", 1.05, 1.07, 1.08),
        (75.0, -0.004, "at/above upper band (stretched, mean-reversion risk down)",
         "downtrend (price < SMA50 < SMA200, death-cross structure)", 1.12, 1.09, 1.08),
    ]

    all_ok  = True
    headers = f"  {'RSI':>5}  {'MACD':>8}  {'Direction':>9}  {'T_sig':>6}  Result"
    print(headers)
    print("  " + "-" * 56)

    for rsi, macdh, bb, trend, close, sma20, sma50 in cases:
        out   = _tech._tech_signal(rsi, macdh, bb, trend, close, sma20, sma50)
        score = out["score"]
        dirn  = out["direction"]
        ok    = score >= 3
        if not ok:
            all_ok = False
        flag  = "✅" if ok else "❌ FAIL — T:1/T:2 with real data!"
        print(f"  {rsi:>5.1f}  {macdh:>8.4f}  {dirn:>9}  T:{score}/10  {flag}")

    print()
    verdict = "✅  ALL UNIT TESTS PASS — Python floor rule correct" if all_ok \
        else "❌  UNIT TEST FAILURES — Python floor rule broken!"
    print(f"  {verdict}")
    return all_ok


# ── Live pair analysis ─────────────────────────────────────────────────────────

def _analyse_pair(pair: str) -> dict:
    clean = pair.upper().replace("/", "").replace("-", "")
    base, quote = clean[:3], clean[3:]
    try:
        result = _tech.analyse(base, quote)
    except Exception as exc:
        return {"pair": pair, "status": "ERROR", "detail": str(exc)[:100]}

    if result.get("status") == "UNAVAILABLE":
        return {"pair": pair, "status": "UNAVAILABLE", "detail": result.get("error", "")[:80]}

    daily = result.get("daily", {})
    if not isinstance(daily, dict):
        return {"pair": pair, "status": "NO_DAILY"}

    if daily.get("status") == "insufficient data":
        return {"pair": pair, "status": "INSUFFICIENT",
                "candles": daily.get("candle_count", 0)}

    rsi   = daily.get("rsi14")
    macdh = daily.get("macd_hist")
    if rsi is None:
        return {"pair": pair, "status": "NO_RSI"}

    ts = daily.get("tech_signal", {})
    return {
        "pair":    pair,
        "status":  "ok",
        "rsi14":   rsi,
        "macdh":   macdh,
        "bb":      (daily.get("bollinger") or "?")[:28],
        "trend":   (daily.get("trend") or "?")[:35],
        "t_dir":   ts.get("direction", "?"),
        "t_score": ts.get("score"),
        "sma20":   daily.get("sma20"),
        "sma50":   daily.get("sma50"),
        "close":   daily.get("last_close"),
        "h4_rsi":  (result.get("4h") or {}).get("rsi14"),
    }


def _print_table(results: list) -> list:
    """Print results table; return list of problematic rows."""
    print(f"\n{_SEP}")
    print("LIVE DATA  —  RSI / MACD / T_sig per pair")
    print(_SEP)
    hdr = (f"  {'Pair':<10}  {'RSI':>5}  {'MACDh':>8}  "
           f"{'4H RSI':>7}  {'T_sig':>6}  {'Verdict'}")
    print(hdr)
    print("  " + "-" * 64)

    issues = []
    for r in results:
        pair   = r["pair"]
        status = r["status"]

        if status == "ok":
            rsi   = f"{r['rsi14']:.1f}"
            macdh = f"{r['macdh']:.5f}" if r.get("macdh") is not None else "?"
            h4r   = f"{r['h4_rsi']:.1f}" if r.get("h4_rsi") else "  ?"
            t_dir = r.get("t_dir", "?")
            t_sc  = r.get("t_score")

            try:
                sc_int  = int(t_sc)
                if sc_int <= 2:
                    verdict = f"❌ T:{sc_int} with real data — BUG"
                    issues.append(r)
                elif sc_int == 3:
                    verdict = f"🟡 T:3 (neutral floor — OK)"
                else:
                    verdict = f"✅ T:{sc_int}"
            except (TypeError, ValueError):
                verdict = "⚠️  score missing"
                issues.append(r)

            print(f"  {pair:<10}  {rsi:>5}  {macdh:>8}  "
                  f"{h4r:>7}  {t_dir[:3]}:{t_sc}/10  {verdict}")

        elif status == "INSUFFICIENT":
            print(f"  {pair:<10}  {'':>5}  {'':>8}  {'':>7}  {'?':>6}  "
                  f"⚪ only {r.get('candles', 0)} candles (need 30)")
        elif status == "UNAVAILABLE":
            detail = r.get("detail", "")[:40]
            print(f"  {pair:<10}  {'':>5}  {'':>8}  {'':>7}  {'?':>6}  🔴 {detail}")
        else:
            detail = r.get("detail", status)[:40]
            print(f"  {pair:<10}  {'':>5}  {'':>8}  {'':>7}  {'?':>6}  ❓ {detail}")

    print("  " + "-" * 64)
    return issues


def _diagnose_issue(r: dict) -> None:
    """Print root-cause details for a T:1/T:2 with real data."""
    pair   = r["pair"]
    rsi    = r.get("rsi14", "?")
    macdh  = r.get("macdh", 0)
    bb     = r.get("bb", "")
    trend  = r.get("trend", "")
    close  = r.get("close") or 0
    sma20  = r.get("sma20") or 0
    sma50  = r.get("sma50") or 0
    t_sc   = r.get("t_score", "?")

    print(f"\n  ROOT CAUSE ANALYSIS: {pair}  (T:{t_sc} with real data)")
    print(f"    RSI={rsi}  MACDh={macdh}  BB={bb}")
    print(f"    trend={trend}")
    print(f"    close={close}  sma20={sma20}  sma50={sma50}")

    # Re-run _tech_signal with verbose tracing
    try:
        rsi_f = float(rsi)
        if rsi_f < 30:
            direction, base = "BUY", 9
        elif rsi_f < 35:
            direction, base = "BUY", 7
        elif rsi_f < 45:
            direction, base = "BUY", 5
        elif rsi_f > 70:
            direction, base = "SELL", 9
        elif rsi_f > 65:
            direction, base = "SELL", 7
        elif rsi_f > 55:
            direction, base = "SELL", 4
        else:
            direction, base = "NEUTRAL", 3
        print(f"    → RSI tier: {direction} base={base}")

        computed = _tech._tech_signal(rsi_f, float(macdh or 0), bb, trend,
                                      float(close), float(sma20), float(sma50))
        print(f"    → Python _tech_signal returns: T:{computed['score']} {computed['direction']}")
        if computed["score"] <= 2:
            print(f"    ❌ PYTHON FLOOR BUG — _tech_signal returned {computed['score']} "
                  f"(should be >=3).  Check max() call in _tech_signal().")
        else:
            print(f"    ✅ Python T_sig is correct ({computed['score']}).  "
                  f"Bug is in Claude prompt — T_sig not being used as anchor.")
    except Exception as exc:
        print(f"    ⚠️  trace failed: {exc}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    use_cached = "--cached" in sys.argv

    print(_SEP)
    print("FOREX AI  —  TECHNICAL SCORING DIAGNOSTIC")
    print(f"Run at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if use_cached:
        print("Mode: cache-only (no live API calls)")
    elif not config.TWELVE_DATA_KEY:
        print("⚠️  TWELVE_DATA_KEY not set — using cached data only")
    print(_SEP)

    # ── Step 1: Python unit tests ──────────────────────────────────────────────
    unit_ok = _unit_tests()

    # ── Step 2: Select pairs ───────────────────────────────────────────────────
    pairs_to_test = list(_FALLBACK_PAIRS)
    if not use_cached:
        try:
            from src import selector as _sel
            sel    = _sel.select_pairs(top_n=15)
            picked = sel.get("selected", [])
            if picked:
                pairs_to_test = picked
                if "AUD/CHF" not in pairs_to_test:
                    pairs_to_test = ["AUD/CHF"] + pairs_to_test[:14]
                print(f"\nSelector chose: {', '.join(pairs_to_test)}")
        except Exception as exc:
            print(f"\nSelector unavailable ({exc}) — using fixed list")

    # Rate-limit aware fetching: 12 s between pairs.
    # Each pair triggers up to 5 Twelve Data calls (monthly/weekly/daily/4h/1h).
    # 12 s gap keeps the burst well under the 8-calls/min free-tier ceiling.
    pairs_to_run = pairs_to_test[:15]
    print(f"\nAnalysing {len(pairs_to_run)} pairs "
          f"(12 s gap between each — ~{len(pairs_to_run) * 12 // 60} min total) ...")
    results = []
    for idx, pair in enumerate(pairs_to_run):
        if idx > 0 and not use_cached and config.TWELVE_DATA_KEY:
            print(f"  [{idx}/{len(pairs_to_run)}] waiting 12 s ...", flush=True)
            time.sleep(12)
        r = _analyse_pair(pair)
        results.append(r)

    # ── Step 3: Print table ────────────────────────────────────────────────────
    issues = _print_table(results)

    # ── Step 4: Root-cause details for any issues ──────────────────────────────
    if issues:
        print(f"\n{_SEP}")
        print(f"ROOT CAUSE ANALYSIS  —  {len(issues)} issue(s) found")
        print(_SEP)
        for r in issues:
            _diagnose_issue(r)

    # ── Step 5: AUD/CHF explicit check ────────────────────────────────────────
    aud_chf = next((r for r in results if "AUD" in r["pair"] and "CHF" in r["pair"]), None)
    if not aud_chf:
        print("\n⚠️  AUD/CHF was not in the pair list — running separately ...")
        aud_chf = _analyse_pair("AUD/CHF")
        results.append(aud_chf)

    print(f"\n{_SEP}")
    print("AUD/CHF SPECIFIC CHECK")
    print(_SEP)
    if aud_chf.get("status") == "ok":
        t = aud_chf.get("t_score")
        try:
            sc_int = int(t)
            chf_ok = sc_int >= 3
        except (TypeError, ValueError):
            chf_ok = False
        verdict = f"✅ T:{t}/10 — PASS" if chf_ok else f"❌ T:{t}/10 — FAIL"
        print(f"  RSI={aud_chf['rsi14']}  MACDh={aud_chf['macdh']}  "
              f"T_sig={aud_chf['t_dir']}:{t}/10")
        print(f"  BB: {aud_chf['bb']}")
        print(f"  Trend: {aud_chf['trend']}")
        print(f"  {verdict}")
        if not chf_ok:
            _diagnose_issue(aud_chf)
    else:
        print(f"  Status: {aud_chf.get('status')} — {aud_chf.get('detail','')[:60]}")

    # ── Step 6: Summary ────────────────────────────────────────────────────────
    ok_n   = sum(1 for r in results if r.get("status") == "ok")
    ins_n  = sum(1 for r in results if r.get("status") == "INSUFFICIENT")
    unav_n = sum(1 for r in results if r.get("status") in ("UNAVAILABLE", "ERROR", "NO_RSI"))

    print(f"\n{_SEP}")
    print("SUMMARY")
    print(_SEP)
    print(f"  Pairs tested:        {len(results)}")
    print(f"  With real data:      {ok_n}")
    print(f"  Insufficient candles:{ins_n}")
    print(f"  Unavailable/Error:   {unav_n}")
    print(f"  T:1/T:2 issues:      {len(issues)}")
    print(f"  Python unit tests:   {'PASS ✅' if unit_ok else 'FAIL ❌'}")
    print(f"  Overall result:      {'✅ ALL CLEAR' if (unit_ok and not issues) else '❌ ISSUES NEED FIXING'}")
    print()

    return 0 if (unit_ok and not issues) else 1


if __name__ == "__main__":
    sys.exit(main())
