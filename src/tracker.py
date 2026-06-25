"""Trade outcome tracker.

Every analysis is appended as a row to data/trades.csv (a spreadsheet you can open
in Excel/Google Sheets). Actionable calls (TRADE_THIS: YES) start life as OPEN;
non-actionable ones are logged as NO_TRADE for the record and for learning stats.
Outcomes are recorded later with `update_outcome`, which computes the realised
R-multiple and pips from entry/stop/exit.
"""

import csv
from datetime import datetime

import config

FIELDS = [
    "id", "timestamp", "pair", "direction",
    "confidence", "technical", "fundamental", "sentiment", "positioning", "macro",
    "entry", "target", "stop_loss", "reward_risk", "trade_this", "data_sources",
    "status", "exit_price", "r_multiple", "pips", "net_pips", "closed_at", "notes",
    "report_file", "key_thesis", "best_entry_time",
    # ── Cascading targets (append-only) ──────────────────────────────────────
    "t1_price",                   # entry ± 0.4× ATR
    "t2_price",                   # entry ± 0.7× ATR
    "t3_price",                   # existing target
    "t1_hit",                     # TRUE / FALSE
    "t1_hit_price",               # live price when T1 triggered
    "t1_hit_pips",                # pip profit on 40% closed at T1
    "t2_hit",                     # TRUE / FALSE
    "t2_hit_price",
    "t2_hit_pips",                # pip profit on 30% closed at T2
    "t3_hit",                     # TRUE / FALSE
    "t3_hit_price",
    "t3_hit_pips",                # pip profit on final 30% at T3
    "effective_stop",             # starts as stop_loss; moves to entry after T1 hit
    "cascading_total_pips",       # sum of pips across all hit portions
    "cascading_total_pips_weighted",  # 0.4×t1 + 0.3×t2 + 0.3×t3
    # ── Adaptive sizing at entry ─────────────────────────────────────────────
    "sizing_mode",                    # normal | win_streak | drawdown_caution | drawdown_protection
    "consecutive_wins_at_entry",      # win streak length when trade was opened
    "drawdown_pct_at_entry",          # fund drawdown % from peak at time of entry
    "position_size_pct_at_entry",     # adaptive risk % applied to this trade
    # ── Dynamic threshold at entry ───────────────────────────────────────────
    "threshold_at_entry",             # dynamic confidence threshold active when trade was opened
    "regime_base_at_entry",           # regime component of the threshold
    "win_rate_adjustment_at_entry",   # win rate component (negative = lowered threshold)
    "data_quality_adjustment_at_entry", # data quality component
    # ── Adaptive cascade targets ──────────────────────────────────────────────
    "t1_was_adaptive",            # TRUE when pair-specific MFE data was used
    "t1_target_atr_multiple",     # actual T1 multiplier applied (0.4 standard)
    "t2_target_atr_multiple",     # actual T2 multiplier applied (0.7 standard)
    "t3_target_atr_multiple",     # actual T3 multiplier applied (1.0 standard)
    "volatility_tier_at_entry",  # VERY_QUIET | QUIET | NORMAL | VOLATILE | VERY_VOLATILE
    "atr_percentile_at_entry",   # atr_percentile_6m ratio used for tier (1.0 = normal)
    # ── Conditional entry system ──────────────────────────────────────────────
    "entry_type",                 # IMMEDIATE | LIMIT_BUY | LIMIT_SELL | BREAKOUT_BUY | BREAKOUT_SELL | PULLBACK
    "entry_trigger_price",        # price level that activates the trade
    "entry_trigger_direction",    # ABOVE | BELOW | None for IMMEDIATE
    "entry_trigger_reason",       # human-readable description of the trigger
    "entry_trigger_expiry",       # UTC datetime when the pending setup expires
    "entry_confirmed_at",         # UTC datetime when the trigger was actually hit
    # ── Rich entry-context features (ML fuel — Part 1) ───────────────────────
    "rsi_at_entry",               # RSI(14) value at time of entry
    "macd_at_entry",              # MACD histogram value at entry
    "bb_position_at_entry",       # Bollinger Band position at entry
    "atr_at_entry",               # ATR(14) value at entry
    "regime_at_entry",            # market regime string at entry
    "session_at_entry",           # active forex session(s) at entry
    "weekly_trend_at_entry",      # weekly trend direction at entry
    "monthly_trend_at_entry",     # monthly trend direction at entry
    "trend_score_at_entry",       # trend alignment score 0-4 at entry
    "technical_score",            # technical score from AI analysis
    "fundamental_score",          # fundamental score from AI analysis
    "sentiment_score",            # sentiment score from AI analysis
    "cot_score",                  # COT score from AI analysis
    "momentum_score",             # momentum score from AI analysis
    "stop_pips_at_entry",         # stop distance in pips at entry
    "rr_at_entry",                # R:R ratio at entry
    "atr_multiple_at_entry",      # stop distance as multiple of ATR
    "consecutive_losses_at_entry", # consecutive fund losses at entry
    "drawdown_at_entry",          # fund drawdown % at entry
    "open_trades_at_entry",       # number of open fund trades at entry
    "ml_win_probability",         # online learner P(win) at entry
]

# status values: NO_TRADE | PENDING | OPEN | WIN | LOSS | BREAKEVEN | SKIPPED | EXPIRED | CANCELLED | PARTIAL_WIN | FULL_WIN
OUTCOME_STATUSES = {"WIN", "LOSS", "BREAKEVEN", "SKIPPED", "EXPIRED", "PARTIAL_WIN", "FULL_WIN"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load() -> list:
    if not config.TRADES_CSV.exists():
        return []
    with config.TRADES_CSV.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_all(rows: list) -> None:
    with config.TRADES_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDS})


def _next_id(rows: list) -> int:
    ids = [int(r["id"]) for r in rows if str(r.get("id", "")).isdigit()]
    return (max(ids) + 1) if ids else 1


def _pip_size(pair: str) -> float:
    """0.01 for JPY-quoted pairs, else 0.0001."""
    quote = pair.split("/")[-1].upper() if "/" in pair else pair[-3:].upper()
    return 0.01 if quote == "JPY" else 0.0001


def _save_report_file(rec_id: int, pair: str, report: str) -> str:
    safe = pair.replace("/", "")
    path = config.REPORTS_DIR / f"{rec_id:04d}_{safe}.txt"
    path.write_text(report, encoding="utf-8")
    return path.name


def log_recommendation(pair: str, parsed: dict, data_sources, report: str) -> int:
    """Append one recommendation; return its assigned id.

    If a YES trade for this pair is already OPEN, returns the existing id and
    overwrites the report file — prevents duplicate OPEN rows on re-analysis.
    """
    rows = load()

    # Guard: if this is a YES trade, check for an existing OPEN or PENDING row for the same pair
    if parsed.get("trade_this") == "YES":
        existing = next(
            (r for r in rows
             if r.get("pair", "").upper() == pair.upper()
             and r.get("status") in ("OPEN", "PENDING")),
            None,
        )
        if existing:
            # Update the existing row with the new analysis instead of creating a duplicate
            rec_id = int(existing["id"])
            _save_report_file(rec_id, pair, report)
            existing["timestamp"]     = _now()
            existing["confidence"]    = parsed.get("confidence")
            existing["entry"]         = parsed.get("entry") or existing.get("entry", "")
            existing["target"]        = parsed.get("target") or existing.get("target", "")
            existing["stop_loss"]     = parsed.get("stop_loss") or existing.get("stop_loss", "")
            existing["key_thesis"]    = parsed.get("key_thesis") or existing.get("key_thesis", "")
            existing["best_entry_time"] = parsed.get("best_entry_time") or existing.get("best_entry_time", "")
            _write_all(rows)
            return rec_id

    rec_id = _next_id(rows)
    report_file = _save_report_file(rec_id, pair, report)
    status = "OPEN" if parsed.get("trade_this") == "YES" else "NO_TRADE"

    rows.append({
        "id": rec_id,
        "timestamp": _now(),
        "pair": pair,
        "direction": parsed.get("direction"),
        "confidence": parsed.get("confidence"),
        "technical": parsed.get("technical_score"),
        "fundamental": parsed.get("fundamental_score"),
        "sentiment": parsed.get("sentiment_score"),
        "positioning": parsed.get("positioning_score"),
        "macro": parsed.get("macro_score"),
        "entry": parsed.get("entry"),
        "target": parsed.get("target"),
        "stop_loss": parsed.get("stop_loss"),
        "reward_risk": parsed.get("reward_risk"),
        "trade_this": parsed.get("trade_this"),
        "data_sources": data_sources,
        "status": status,
        "exit_price": "", "r_multiple": "", "pips": "", "net_pips": "",
        "closed_at": "", "notes": "",
        "report_file": report_file,
        "key_thesis": parsed.get("key_thesis") or "",
        "best_entry_time": parsed.get("best_entry_time") or "",
    })
    _write_all(rows)
    return rec_id


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _compute_result(row: dict, status: str, exit_price):
    """Return (r_multiple, pips) from entry/stop/exit and direction.

    If no exit price is given, assume the planned outcome: WIN -> target hit,
    LOSS -> stop hit. BREAKEVEN/SKIPPED/EXPIRED -> 0 / no result.
    """
    entry = _to_float(row.get("entry"))
    stop = _to_float(row.get("stop_loss"))
    target = _to_float(row.get("target"))
    direction = (row.get("direction") or "").upper()

    if status == "SKIPPED":
        return "", ""
    if status == "EXPIRED" and _to_float(exit_price) is None:
        return "", ""
    if status == "BREAKEVEN":
        return 0.0, 0.0

    exit_price = _to_float(exit_price)
    if exit_price is None:
        exit_price = target if status == "WIN" else stop
    if entry is None or stop is None or exit_price is None or direction not in ("BUY", "SELL"):
        return "", ""

    risk = abs(entry - stop)
    if risk == 0:
        return "", ""
    profit = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
    r_multiple = round(profit / risk, 2)
    pips = round(profit / _pip_size(row.get("pair", "")), 1)
    return r_multiple, pips


def update_fields(rec_id: int, **kwargs) -> None:
    """Update arbitrary fields on a trade row without changing its status.

    Only updates keys that exist in FIELDS.  Used by cascade milestone tracking
    to record T1/T2/T3 hits and move the effective_stop without closing the trade.
    """
    rows = load()
    target = next((r for r in rows if str(r.get("id")) == str(rec_id)), None)
    if target is None:
        return
    for k, v in kwargs.items():
        if k in FIELDS:
            target[k] = v
    _write_all(rows)


def check_inverse_open(pair: str, direction: str) -> str | None:
    """Return a warning string if an open fund trade is the inverse of this pair/direction.

    "AUD/CAD" BUY is equivalent to "CAD/AUD" SELL — same directional bet.
    Returns warning string if such an inverse trade is already open, None otherwise.
    """
    try:
        cleaned = pair.upper().replace("/", "")
        if len(cleaned) < 6:
            return None
        inv_pair = cleaned[3:6] + "/" + cleaned[:3]
        inv_dir  = "SELL" if direction.upper() == "BUY" else "BUY"
        rows = load()
        for row in rows:
            if row.get("status") != "OPEN":
                continue
            rp = (row.get("pair") or "").upper().replace("/", "")
            rd = (row.get("direction") or "").upper()
            rp_slash = rp[:3] + "/" + rp[3:6] if len(rp) >= 6 else rp
            if rp_slash.upper() == inv_pair.upper() and rd == inv_dir:
                return (
                    f"WARNING: Inverse pair conflict — {inv_pair} {inv_dir} already open "
                    f"(same directional bet as {pair} {direction.upper()}). "
                    f"Avoid doubling exposure."
                )
    except Exception:
        pass
    return None


def check_currency_concentration(pair: str, direction: str, max_per_ccy: int = 2) -> str | None:
    """Return warning string if opening this trade would put >= max_per_ccy open trades on any one currency."""
    try:
        cleaned = pair.upper().replace("/", "").replace("-", "")
        if len(cleaned) < 6:
            return None
        base, quote = cleaned[:3], cleaned[3:6]
        rows = load()
        open_rows = [r for r in rows if r.get("status") == "OPEN"]
        for ccy in [base, quote]:
            count = sum(
                1 for r in open_rows
                if ccy in r.get("pair", "").upper().replace("/", "").replace("-", "")
            )
            if count >= max_per_ccy:
                return (
                    f"WARNING: {ccy} already appears in {count} open fund trade(s) "
                    f"(max {max_per_ccy}) — {pair} {direction.upper()} would add a 3rd {ccy} exposure"
                )
    except Exception:
        pass
    return None


def update_outcome(rec_id: int, status: str, exit_price=None, notes: str = "",
                   cascading_pips=None) -> dict:
    status = status.upper()
    if status not in OUTCOME_STATUSES:
        raise ValueError(f"status must be one of {sorted(OUTCOME_STATUSES)}")

    rows = load()
    target = next((r for r in rows if str(r.get("id")) == str(rec_id)), None)
    if target is None:
        raise ValueError(f"no recommendation with id {rec_id}")

    if cascading_pips is not None and status in ("FULL_WIN", "WIN", "PARTIAL_WIN"):
        pips   = round(float(cascading_pips), 1)
        ps     = _pip_size(target.get("pair", ""))
        entry  = _to_float(target.get("entry"))
        stop   = _to_float(target.get("stop_loss"))
        risk   = abs(entry - stop) if entry is not None and stop is not None else 0
        r_mult = round((pips * ps) / risk, 2) if risk > 0 and ps > 0 else ""
    else:
        r_mult, pips = _compute_result(target, status, exit_price)

    net_pips = pips
    if pips not in ("", None):
        try:
            from src import trade_costs as _tc
            opened = datetime.strptime(target.get("timestamp", "")[:19], "%Y-%m-%d %H:%M:%S")
            days_held = max(0.0, (datetime.now(timezone.utc).replace(tzinfo=None) - opened).total_seconds() / 86400)
            net_pips = _tc.net_pips_for_closed_trade(
                target.get("pair", ""),
                target.get("direction", ""),
                _to_float(target.get("entry")) or 1.0,
                float(pips),
                days_held,
            )
        except Exception:
            pass

    target["status"] = status
    target["exit_price"] = exit_price if exit_price is not None else ""
    target["r_multiple"] = r_mult
    target["pips"] = pips
    target["net_pips"] = net_pips
    target["closed_at"] = _now()
    if notes:
        target["notes"] = notes
    _write_all(rows)
    return target
