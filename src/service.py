"""Glue between the analysis pipeline and the tracker: run an analysis, parse the
recommendation, and log it to the trades spreadsheet."""

from src import pipeline, recparse, tracker


def validate_trade_data(
    parsed: dict,
    pair: str,
    direction: str,
    bundle: dict = None,
    log_fn=None,
) -> dict:
    """Hard validation gate — blocks trades with missing or corrupt critical data.

    Prevents trading blind, which caused 6 of 7 losses in the loss autopsy.
    Returns {valid, failures, critical_failure}.

    Moved here from daily.py (2026-08-20) so analyse_and_log() below can run
    the CRITICAL subset immediately after recparse.parse() — before a
    candidate is eligible for DA or grading in daily.py's scan pipeline, not
    just at the two terminal call sites (fund-loop gate, research-trade
    logging gate) that used to be the only place this ran. Root cause: Haiku
    never emits ENTRY/STOP_LOSS/TARGET (Sonnet's job, gated on raw pre-
    consensus confidence >= sonnet_threshold); a candidate whose raw Haiku
    confidence misses that gate but later gets consensus-bumped above the
    fund threshold reached DA (a real Sonnet-tier API call, evaluating
    "Entry: ? Stop: ? Target: ?") and grading before finally failing here —
    see 2026-08-20 investigation for the full trace (real case: USD/CHF
    #5367, conf 6->7 via consensus, DA fired with no objections and no log
    line, only caught by this same check at the very last gate).
    """
    _log = log_fn or print
    failures = []
    critical = False
    bundle = bundle or {}

    # ── CRITICAL: Stop loss ──────────────────────────────────────────────────
    stop  = float(parsed.get("stop_loss")  or parsed.get("stop")         or 0)
    entry = float(parsed.get("entry")      or parsed.get("entry_price")   or 0)

    if stop == 0 or stop != stop:
        failures.append("stop_loss = 0 or NaN")
        critical = True

    if entry == 0 or entry != entry:
        failures.append("entry price = 0 or NaN")
        critical = True

    if entry > 0 and stop > 0:
        ps = 0.01 if "JPY" in pair else 0.0001
        stop_pips = abs(entry - stop) / ps
        if stop_pips < 5:
            failures.append(f"stop too close: {stop_pips:.1f}p (minimum 5p)")
            critical = True
        if stop_pips > 2000:
            failures.append(f"stop too wide: {stop_pips:.0f}p (maximum 2000p)")
            critical = True

    # ── CRITICAL: Direction ──────────────────────────────────────────────────
    if direction not in ("BUY", "SELL"):
        failures.append(f"invalid direction: {direction!r}")
        critical = True

    if entry > 0 and stop > 0:
        if direction == "BUY" and stop >= entry:
            failures.append(f"BUY stop {stop:.5f} >= entry {entry:.5f}")
            critical = True
        if direction == "SELL" and stop <= entry:
            failures.append(f"SELL stop {stop:.5f} <= entry {entry:.5f}")
            critical = True

    # ── CRITICAL: Target ─────────────────────────────────────────────────────
    t1 = float(
        parsed.get("t1_price") or parsed.get("target") or
        parsed.get("target_price") or 0
    )
    if t1 == 0 or t1 != t1:
        failures.append("t1_price = 0 — no target set")
        critical = True

    if t1 > 0 and entry > 0:
        if direction == "BUY" and t1 <= entry:
            failures.append(f"BUY target {t1:.5f} <= entry {entry:.5f}")
            critical = True
        if direction == "SELL" and t1 >= entry:
            failures.append(f"SELL target {t1:.5f} >= entry {entry:.5f}")
            critical = True

    # ── CRITICAL: Confidence ─────────────────────────────────────────────────
    conf = float(parsed.get("confidence") or 0)
    if conf <= 0 or conf != conf:
        failures.append(f"confidence = {conf}")
        critical = True

    # ── IMPORTANT: RSI (from technical bundle) ───────────────────────────────
    rsi = float(
        (bundle.get("technical") or {}).get("daily", {}).get("rsi14") or
        parsed.get("rsi_at_entry") or 0
    )
    if rsi == 0 or rsi != rsi:
        failures.append("RSI = 0 — technical data missing or pipeline error")
    elif rsi < 1 or rsi > 99:
        failures.append(f"RSI = {rsi:.1f} — invalid range (must be 1–99)")

    # ── IMPORTANT: R:R check ─────────────────────────────────────────────────
    if entry > 0 and stop > 0 and t1 > 0:
        ps = 0.01 if "JPY" in pair else 0.0001
        stop_pips = abs(entry - stop) / ps
        t1_pips   = abs(t1 - entry) / ps
        rr = t1_pips / stop_pips if stop_pips > 0 else 0
        if rr < 1.0:
            failures.append(f"R:R = {rr:.2f} < 1.0 (stop wider than target)")

    # ── ATR stop cap — stop must not exceed 1× ATR (prevents disaster stops) ─
    _atr_val = float(parsed.get("atr_at_entry", 0) or parsed.get("atr", 0) or 0)
    if _atr_val > 0 and entry > 0 and stop > 0:
        _atr_ps  = 0.01 if "JPY" in pair else 0.0001
        _sp_atr  = abs(entry - stop) / _atr_ps
        _atr_pip = _atr_val / _atr_ps
        if _sp_atr > _atr_pip * 1.0:
            failures.append(
                f"Stop {_sp_atr:.0f}p > 1×ATR {_atr_pip:.0f}p — exposed to outsized loss")
            critical = True
            _log(f"[atr-cap] {pair} stop={_sp_atr:.0f}p exceeds 1×ATR cap {_atr_pip:.0f}p")

    valid = not critical and len(failures) == 0

    if failures:
        _log(
            f"[validate-data] {pair} {direction}: "
            f"{len(failures)} failure(s) — critical={critical}"
        )
        for _f in failures:
            _log(f"[validate-data]   FAIL {_f}")
    else:
        _log(f"[validate-data] {pair} {direction}: all valid")

    return {"valid": valid, "failures": failures, "critical_failure": critical}


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

    # 2026-08-20: fail-fast data check, immediately after parsing — before any
    # candidate becomes eligible for DA (a real Sonnet-tier API call) or
    # grading in daily.py's scan pipeline. Only the CRITICAL subset gates
    # here (missing/corrupt entry, stop, target, direction, confidence) —
    # this is deliberately the same check and the same critical/non-critical
    # split validate_trade_data() has always used, just run far earlier.
    # Root cause this closes: Haiku never emits ENTRY/STOP_LOSS/TARGET (only
    # Sonnet does, gated on raw pre-consensus confidence); a candidate whose
    # raw confidence misses that gate can still get consensus-bumped above
    # the fund threshold later and reach DA/grading carrying no real price
    # levels, only to fail at the fund-loop's terminal validate_trade_data()
    # call — after already paying for a DA call. Does NOT reconcile
    # ENTRY_TYPE/ENTRY_TRIGGER_PRICE into entry/stop/target (a separate,
    # more consequential decision, deliberately not made here) — a
    # conditional-entry candidate with no plain price fields is simply
    # excluded early, same outcome as before, just cheaper to reach.
    _early_val = validate_trade_data(
        parsed, result["pair"], parsed.get("direction") or "",
        bundle=result.get("bundle", {}), log_fn=log,
    )
    if _early_val["critical_failure"]:
        parsed["trade_this"]   = "NO"
        parsed["block_reason"] = f"Data validation: {_early_val['failures'][0]}"
        parsed["_early_reject"] = True
        log(
            f"[early-reject] {result['pair']} — {_early_val['failures'][0]}, "
            f"excluded before DA/grading (raw_conf={parsed.get('confidence')})"
        )

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
