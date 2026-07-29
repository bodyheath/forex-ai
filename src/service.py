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
    max_open_id: "int | None" = None,
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
            parsed["confidence"] = max(1, min(10, int(round(_raw_conf - _mtf_penalty))))
        except (TypeError, ValueError):
            pass

    # Check for inverse pair conflict before logging fund trade — BLOCK if detected.
    # max_open_id (the highest trade id that existed before this scan started)
    # excludes phantom OPEN rows from other candidates already written earlier
    # in this same scan but not yet corrected by DA/drawdown-tier/not-viable —
    # see check_inverse_open()'s docstring in tracker.py.
    if parsed.get("trade_this") == "YES":
        try:
            _inv_warn = tracker.check_inverse_open(
                result["pair"], parsed.get("direction") or "", max_id=max_open_id
            )
            if _inv_warn:
                log(f"[service] BLOCKING trade — {_inv_warn}")
                parsed["trade_this"] = "NO"
                parsed["block_reason"] = _inv_warn
                result["inverse_blocked"] = _inv_warn
        except Exception as _e:
            log(f"[service] inverse-check error: {_e}")

    # Check currency concentration — max 1 open fund trade per currency
    if parsed.get("trade_this") == "YES":
        try:
            _conc_warn = tracker.check_currency_concentration(
                result["pair"], parsed.get("direction") or "", max_id=max_open_id
            )
            if _conc_warn:
                log(f"[service] CONCENTRATION BLOCK — {_conc_warn}")
                parsed["trade_this"] = "NO"
                parsed["block_reason"] = _conc_warn
                result["concentration_warning"] = _conc_warn
        except Exception as _e:
            log(f"[service] concentration-check error: {_e}")

    rec_id = tracker.log_recommendation(
        result["pair"], parsed, result["availability"]["count"], result["report"]
    )
    result["id"]     = rec_id
    result["parsed"] = parsed
    return result
