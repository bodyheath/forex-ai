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
) -> dict:
    result = pipeline.run(
        pair,
        log=log,
        force_deep=force_deep,
        shared_fundamental=shared_fundamental,
        shared_macro=shared_macro,
        sonnet_threshold=sonnet_threshold,
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

    # Hard MTF gate: TRADE_THIS YES requires >=4/5 timeframes agreeing.
    # If MTF data is unavailable (qualifies defaults to True) we don't block.
    _mtf = result.get("bundle", {}).get("mtf", {})
    if parsed.get("trade_this") == "YES" and _mtf and not _mtf.get("qualifies", True):
        parsed["trade_this"] = "NO"

    rec_id = tracker.log_recommendation(
        result["pair"], parsed, result["availability"]["count"], result["report"]
    )
    result["id"]     = rec_id
    result["parsed"] = parsed
    return result
