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


def gather(base: str, quote: str, log=print) -> dict:
    """Collect all data layers. Each is independently fault-tolerant."""
    log(f"  - technical (Twelve Data) ...")
    tech = technical.analyse(base, quote)

    log(f"  - fundamental (FRED rates) ...")
    fund = fundamental.analyse(base, quote)

    log(f"  - sentiment (NewsAPI) ...")
    sent = sentiment.analyse(base, quote)

    log(f"  - positioning (CFTC COT) ...")
    pos = positioning.analyse(base, quote)

    log(f"  - macro (FRED) ...")
    mac = macro.analyse()

    return {
        "technical": tech,
        "fundamental": fund,
        "sentiment": sent,
        "positioning": pos,
        "macro": mac,
    }


def run(pair: str, log=print, force_deep: bool = False) -> dict:
    base, quote = parse_pair(pair)
    canonical = f"{base}/{quote}"
    log(f"Gathering live data for {canonical}:")
    bundle = gather(base, quote, log=log)

    available = _availability(bundle)
    log(f"Data sources available: {available['count']}/5 "
        f"({', '.join(available['ok']) or 'none'})")

    # Stage 1: fast Haiku screener — only tech + fundamental, score 1-5.
    log("Stage 1: screening with Haiku ...")
    screen = analyst.screen(canonical, bundle)
    log(f"  Screen score: {screen['score']}/5 — {screen['reason']}")

    if not force_deep and screen["score"] < 4:
        log(f"  Filtered out (score {screen['score']} < 4). Skipping deep analysis.")
        return {
            "pair": canonical,
            "bundle": bundle,
            "availability": available,
            "report": None,
            "screened_out": True,
            "screen": screen,
        }

    # Stage 2: full 5-source deep analysis with Sonnet.
    log(f"Stage 2: deep analysis with {config.CLAUDE_MODEL} ...")
    report = analyst.analyse(canonical, bundle)
    return {
        "pair": canonical,
        "bundle": bundle,
        "availability": available,
        "report": report,
        "screened_out": False,
        "screen": screen,
    }


def _availability(bundle: dict) -> dict:
    ok = []
    for layer in ("technical", "fundamental", "sentiment", "positioning", "macro"):
        status = bundle.get(layer, {}).get("status")
        if status == "ok":
            ok.append(layer)
    return {"count": len(ok), "ok": ok}
