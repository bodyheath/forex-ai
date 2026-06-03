"""Glue between the analysis pipeline and the tracker: run an analysis, parse the
recommendation, and log it to the trades spreadsheet. Used by both main.py (manual)
and daily.py (automation)."""

from src import pipeline, recparse, tracker


def analyse_and_log(pair: str, log=print) -> dict:
    result = pipeline.run(pair, log=log)
    if result.get("screened_out"):
        result["id"] = None
        result["parsed"] = {"trade_this": "FILTERED", "confidence": None, "direction": None}
        return result
    parsed = recparse.parse(result["report"])
    rec_id = tracker.log_recommendation(
        result["pair"], parsed, result["availability"]["count"], result["report"]
    )
    result["id"] = rec_id
    result["parsed"] = parsed
    return result
