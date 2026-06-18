"""Glue between the analysis pipeline and the tracker: run an analysis, parse the
recommendation, and log it to the trades spreadsheet."""

from src import pipeline, recparse, tracker


def analyse_and_log(
    pair: str,
    log=print,
    force_deep: bool = False,
    shared_fundamental=None,
    shared_macro=None,
    sonnet_threshold: int = 6,
    pair_threshold_override: "float | None" = None,
) -> dict:
    result = pipeline.run(
        pair,
        log=log,
        force_deep=force_deep,
        shared_fundamental=shared_fundamental,
        shared_macro=shared_macro,
        sonnet_threshold=sonnet_threshold,
        pair_threshold_override=pair_threshold_override,
    )
    if result.get("screened_out"):
        screen = result.get("screen", {})
        parsed = {
            "trade_this": "NO",
            "confidence": screen.get("score"),
            "direction": None,
            "technical_score": None, "fundamental_score": None,
            "sentiment_score": None, "positioning_score": None, "macro_score": None,
            "entry": None, "target": None, "stop_loss": None, "reward_risk": None,
        }
        rec_id = tracker.log_recommendation(
            result["pair"],
            parsed,
            result["availability"]["count"],
            f"Stage-1 filtered: score {screen.get('score', '?')}/5 — {screen.get('reason', '')}",
        )
        result["id"]     = rec_id
        result["parsed"] = {"trade_this": "FILTERED", "confidence": screen.get("score"), "direction": None}
        return result

    parsed = recparse.parse(result["report"])

    # Graduated MTF gate: block trades where weekly actively opposes daily+4H,
    # or neither weekly nor daily show a directional signal.
    # If MTF data is unavailable (qualifies defaults to True) we don't block.
    _mtf = result.get("bundle", {}).get("mtf", {})
    if parsed.get("trade_this") == "YES" and _mtf and not _mtf.get("qualifies", True):
        parsed["trade_this"] = "NO"

    # Apply MTF confidence penalty for weak signals (weekly-only or mixed).
    # Reduces confidence BEFORE the watch-list threshold check in daily.py.
    _mtf_penalty = _mtf.get("conf_penalty", 0) if _mtf else 0
    if _mtf_penalty > 0 and parsed.get("trade_this") == "YES":
        try:
            _raw_conf = float(parsed.get("confidence") or 5)
            parsed["confidence"] = str(max(1, round(_raw_conf - _mtf_penalty)))
        except (TypeError, ValueError):
            pass

    rec_id = tracker.log_recommendation(
        result["pair"], parsed, result["availability"]["count"], result["report"]
    )
    result["id"]     = rec_id
    result["parsed"] = parsed
    return result
