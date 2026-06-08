"""Orchestrates the full analysis for one pair: gather every data layer, then
hand the bundle to Claude."""

import config
from src import analyst, fundamental, macro, positioning, sentiment, technical


def parse_pair(pair: str):
    """Normalise 'eur/usd', 'EURUSD', 'eur-usd' -> ('EUR', 'USD')."""
    cleaned = pair.upper().replace("/", "").replace("-", "").replace(" ", "")
    if len(cleaned) != 6:
        raise ValueError(f"Could not parse currency pair from '{pair}'. Use e.g. EUR/USD.")
    return cleaned[:3], cleaned[3:]


def gather(base: str, quote: str, log=print,
           _shared_fundamental=None, _shared_macro=None) -> dict:
    """Collect all data layers. Accepts pre-fetched fundamental and macro to
    avoid duplicate FRED calls when multiple pairs share currencies."""
    log(f"  - technical (Twelve Data) ...")
    tech = technical.analyse(base, quote)

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
        "technical":    tech,
        "fundamental":  fund,
        "sentiment":    sent,
        "positioning":  pos,
        "macro":        mac,
    }


def run(pair: str, log=print, force_deep: bool = False,
        shared_fundamental=None, shared_macro=None) -> dict:
    base, quote = parse_pair(pair)
    canonical   = f"{base}/{quote}"
    log(f"Gathering live data for {canonical}:")
    bundle = gather(base, quote, log=log,
                    _shared_fundamental=shared_fundamental,
                    _shared_macro=shared_macro)

    available = _availability(bundle)
    log(f"Data sources available: {available['count']}/5 "
        f"({', '.join(available['ok']) or 'none'})")

    # Skip-unchanged check: if price moved < 15 pips since last analysis, reuse
    if not force_deep:
        should_skip, cached_report = analyst.check_skip(canonical, bundle)
        if should_skip:
            log(f"  SKIP: {canonical} unchanged (<15 pips) — reusing cached analysis.")
            screen = {"score": 5, "reason": "cached — price unchanged"}
            return {
                "pair":         canonical,
                "bundle":       bundle,
                "availability": available,
                "report":       cached_report,
                "screened_out": False,
                "screen":       screen,
                "skipped_unchanged": True,
            }

    # Stage 1: fast Haiku screener — only tech + fundamental, score 1-5.
    log("Stage 1: screening with Haiku ...")
    screen = analyst.screen(canonical, bundle)
    log(f"  Screen score: {screen['score']}/5 — {screen['reason']}")

    if not force_deep and screen["score"] < 3:
        log(f"  Filtered out (score {screen['score']} < 3). Skipping deep analysis.")
        return {
            "pair":         canonical,
            "bundle":       bundle,
            "availability": available,
            "report":       None,
            "screened_out": True,
            "screen":       screen,
        }

    # Stage 2: full 5-source deep analysis with Sonnet.
    log(f"Stage 2: deep analysis with {config.CLAUDE_MODEL} ...")
    report = analyst.analyse(canonical, bundle)
    return {
        "pair":         canonical,
        "bundle":       bundle,
        "availability": available,
        "report":       report,
        "screened_out": False,
        "screen":       screen,
    }


def _availability(bundle: dict) -> dict:
    ok = []
    for layer in ("technical", "fundamental", "sentiment", "positioning", "macro"):
        status = bundle.get(layer, {}).get("status")
        if status == "ok":
            ok.append(layer)
    return {"count": len(ok), "ok": ok}
