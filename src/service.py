"""Glue between the analysis pipeline and the tracker: run an analysis, parse the
recommendation, and log it to the trades spreadsheet. Used by both main.py (manual)
and daily.py (automation)."""

from src import pipeline, recparse, tracker


def analyse_and_log(pair: str, log=print) -> dict:
    result = pipeline.run(pair, log=log)
    if result.get("screened_out"):
        # Log screened pairs so every run produces visible output in trades.csv
        # and the dashboard always reflects today's analysis, even when no pairs
        # pass the stage-1 threshold (e.g. when technical data is unavailable).
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
        result["id"] = rec_id
        result["parsed"] = {"trade_this": "FILTERED", "confidence": screen.get("score"), "direction": None}
        return result
    parsed = recparse.parse(result["report"])
    rec_id = tracker.log_recommendation(
        result["pair"], parsed, result["availability"]["count"], result["report"]
    )
    result["id"] = rec_id
    result["parsed"] = parsed
    return result
