"""Orchestrates the full analysis for one pair: gather every data layer, then
run Haiku full analysis, and escalate to Sonnet only if Haiku confidence reaches
the session-specific threshold."""

import config
from src import analyst, fundamental, macro, mtf, positioning, sentiment, technical


def parse_pair(pair: str):
    cleaned = pair.upper().replace("/", "").replace("-", "").replace(" ", "")
    if len(cleaned) != 6:
        raise ValueError(f"Could not parse currency pair from '{pair}'. Use e.g. EUR/USD.")
    return cleaned[:3], cleaned[3:]


def gather(base: str, quote: str, log=print,
           _shared_fundamental=None, _shared_macro=None) -> dict:
    log(f"  - technical (Twelve Data) ...")
    tech = technical.analyse(base, quote)
    # Prominent diagnostic for technical failures so they're visible in GitHub Actions logs
    if tech.get("status") == "UNAVAILABLE":
        log(f"  ⚠️ TECHNICAL UNAVAILABLE for {base}/{quote}: {tech.get('error','')[:120]}")
    else:
        daily_tf = tech.get("daily", {})
        if isinstance(daily_tf, dict) and daily_tf.get("status") == "insufficient data":
            cnt = daily_tf.get("candle_count", "?")
            log(f"  ⚠️ TECHNICAL DAILY: only {cnt} candles for {base}/{quote} "
                f"(need 30) — indicators unavailable")
        elif isinstance(daily_tf, dict) and daily_tf.get("rsi14") is not None:
            ts = daily_tf.get("tech_signal", {})
            log(f"  ✓ TECHNICAL: RSI={daily_tf['rsi14']}  "
                f"MACDh={daily_tf.get('macd_hist','?')}  "
                f"T_sig={ts.get('direction','?')}_{ts.get('score','?')}/10")
    mtf_result = mtf.analyse(tech)

    if _shared_fundamental is not None:
        fund = _shared_fundamental
    else:
        log(f"  - fundamental (FRED rates) ...")
        fund = fundamental.analyse(base, quote)

    log(f"  - sentiment (NewsAPI) ...")
    sent = sentiment.analyse(base, quote)

    if _shared_macro is not None:
        mac = _shared_macro
    else:
        log(f"  - macro (FRED) ...")
        mac = macro.analyse()

    log(f"  - positioning (CFTC COT) ...")
    pos = positioning.analyse(base, quote)

    return {
        "technical":   tech,
        "mtf":         mtf_result,
        "fundamental": fund,
        "sentiment":   sent,
        "positioning": pos,
        "macro":       mac,
    }


def run(pair: str, log=print, force_deep: bool = False,
        shared_fundamental=None, shared_macro=None,
        sonnet_threshold: int = 6) -> dict:
    """Analyse one pair.

    sonnet_threshold controls when Haiku result is escalated to Sonnet:
      6 = 6am full scan (more Sonnet calls, highest quality).
      7 = intraday scans (Haiku-only for most pairs, Sonnet only for near-certain trades).
    """
    base, quote = parse_pair(pair)
    canonical   = f"{base}/{quote}"
    log(f"Gathering live data for {canonical}:")
    bundle = gather(base, quote, log=log,
                    _shared_fundamental=shared_fundamental,
                    _shared_macro=shared_macro)

    available = _availability(bundle)
    log(f"Data sources available: {available['count']}/5 "
        f"({', '.join(available['ok']) or 'none'})")

    # Skip-unchanged check (10-pip threshold) — skips even Haiku on flat pairs
    if not force_deep:
        should_skip, cached_report = analyst.check_skip(canonical, bundle)
        if should_skip:
            log(f"  SKIP: {canonical} unchanged (<{analyst._SKIP_PIPS} pips) — reusing cached result.")
            return {
                "pair":              canonical,
                "bundle":            bundle,
                "availability":      available,
                "report":            cached_report,
                "screened_out":      False,
                "screen":            {"score": 5, "reason": "cached — price unchanged"},
                "skipped_unchanged": True,
            }

    # Stage 1: Haiku full analysis (every pair in scope)
    log("Haiku: full analysis ...")
    haiku = analyst.analyse_haiku_full(canonical, bundle)
    log(f"  Haiku: conf={haiku['confidence']}/10 {haiku['direction']} — {haiku['reason']}")

    # Very low confidence → screened out (kept out of Watch List too)
    if haiku["confidence"] < 3:
        log(f"  Filtered (Haiku conf {haiku['confidence']} < 3).")
        return {
            "pair":         canonical,
            "bundle":       bundle,
            "availability": available,
            "report":       haiku["report"],
            "screened_out": True,
            "screen":       {"score": 1, "reason": haiku["reason"] or "Very low confluence"},
        }

    # Below Sonnet threshold → use Haiku result directly (Watch List / Approaching)
    if haiku["confidence"] < sonnet_threshold:
        log(f"  Haiku-only (conf {haiku['confidence']} < threshold {sonnet_threshold}).")
        return {
            "pair":         canonical,
            "bundle":       bundle,
            "availability": available,
            "report":       haiku["report"],
            "screened_out": False,
            "screen":       {"score": min(5, haiku["confidence"] // 2 + 1),
                             "reason": f"Haiku {haiku['confidence']}/10"},
        }

    # Stage 2: Sonnet confirmation for high-confidence pairs
    log(f"Sonnet: confirming (Haiku conf={haiku['confidence']}/10, threshold={sonnet_threshold}) ...")
    report = analyst.analyse(canonical, bundle, haiku_report=haiku["report"])
    return {
        "pair":         canonical,
        "bundle":       bundle,
        "availability": available,
        "report":       report,
        "screened_out": False,
        "screen":       {"score": min(5, haiku["confidence"] // 2 + 1),
                         "reason": f"Sonnet confirmed (Haiku {haiku['confidence']}/10)"},
    }


def _availability(bundle: dict) -> dict:
    ok = []
    for layer in ("technical", "fundamental", "sentiment", "positioning", "macro"):
        status = bundle.get(layer, {}).get("status")
        if status == "ok":
            ok.append(layer)
    return {"count": len(ok), "ok": ok}
