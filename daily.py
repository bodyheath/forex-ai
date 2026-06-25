"""Daily automation runner (intended for a 6am scheduled task).

SCHEDULING: This script is triggered exclusively by cron-job.org via GitHub
Actions workflow_dispatch.  There are no GitHub built-in schedule triggers.

Schedule (all Auckland NZT, UTC+12/13 DST-aware via cron-job.org):
  6:05am  Mon-Fri → daily.yml     SCAN_MODE=full      (6am full scan)
  9:05am  Mon-Fri → intraday.yml  SCAN_MODE=morning   (9am morning check)
  5:05pm  Mon-Fri → intraday.yml  SCAN_MODE=prelondon (5pm pre-London)
  11:05pm Mon-Fri → intraday.yml  SCAN_MODE=preny     (11pm pre-New York)
  Every 30 min    → monitor.yml   SCAN_MODE=monitor   (between-scan monitor)

Run guard: local runs (GITHUB_ACTIONS != "true") never write or read the guard.
  GitHub Actions runs are blocked only if the SAME scan mode ran within 20 min.

Sequence:
  1. Refresh learning memory from any outcomes recorded since the last run.
  2. Fetch the full Twelve Data forex universe; pre-score all pairs by session
     alignment, economic events, momentum, and volatility; select the top 15.
  2b. Two-stage smart pre-filter: score all candidates on free FRED+COT data
      before fetching any Twelve Data price candles — only the top 20 proceed.
  3. Batch pre-fetch FRED, COT, macro data once for all pairs (shared store).
  4. Analyse each selected pair (Haiku screen → Haiku thesis → Sonnet scoring).
  5. Auto-expand: if fewer than 3 pairs score confidence 5+, pull the next pairs
     from the pre-scored ranked list (already pre-filtered) and analyse them.
  6. Regenerate the HTML dashboard.
  7. Send a reformatted Telegram summary with cost breakdown.
"""

import os
import sys
import html as _html_mod
import json
import re as _re_tg
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import config
from src import dashboard, learning, selector, service

try:
    from src import discord_notifier as _discord
except Exception:
    _discord = None

_SESSION_AUCKLAND = {
    "EUR": ("London",        "7pm–4am"),
    "GBP": ("London",        "7pm–4am"),
    "CHF": ("London",        "7pm–4am"),
    "NOK": ("London",        "7pm–4am"),
    "SEK": ("London",        "7pm–4am"),
    "JPY": ("Tokyo",         "12pm–9pm"),
    "SGD": ("Tokyo",         "12pm–9pm"),
    "HKD": ("Tokyo",         "12pm–9pm"),
    "USD": ("New York",      "1am–10am"),
    "CAD": ("New York",      "1am–10am"),
    "AUD": ("Sydney/Tokyo",  "9am–9pm"),
    "NZD": ("Sydney/Tokyo",  "9am–9pm"),
}
_SESSION_PRIORITY = ["EUR", "GBP", "CHF", "AUD", "NZD", "NOK", "SEK", "SGD", "HKD", "JPY", "USD", "CAD"]


# ── Auckland / New Zealand time helpers ────────────────────────────────────────

def _auckland_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Pacific/Auckland"))
    except Exception:
        utc = datetime.now(__import__("datetime").timezone.utc)
        off = 13 if utc.month in (10, 11, 12, 1, 2, 3) else 12
        from datetime import timezone
        return utc.astimezone(timezone(timedelta(hours=off)))


def _fmt_time_nz(dt: datetime) -> str:
    h    = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    return f"{h}:{dt.minute:02d}{ampm}" if dt.minute else f"{h}{ampm}"


def _fmt_date_nz(dt: datetime) -> str:
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B')} {dt.year}"


def _fmt_date_short_nz(dt: datetime) -> str:
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B')}"


def _session_label(pair: str) -> str:
    cleaned = pair.upper().replace("/", "").replace("-", "")
    base  = cleaned[:3]
    quote = cleaned[3:6] if len(cleaned) >= 6 else ""
    for ccy in _SESSION_PRIORITY:
        if ccy in (base, quote):
            name, window = _SESSION_AUCKLAND[ccy]
            return f"{name} session {window} Auckland time"
    return "London session 7pm – 11pm Auckland time"


def _best_session_for_pair(pair: str) -> str:
    """Return the best Auckland trading session window for a pair."""
    cleaned = pair.upper().replace("/", "").replace("-", "")
    base  = cleaned[:3]
    quote = cleaned[3:6] if len(cleaned) >= 6 else ""
    b = _SESSION_AUCKLAND.get(base)
    q = _SESSION_AUCKLAND.get(quote)
    if not b and not q:
        return "London open 7pm–9pm Auckland"
    if b and q and b[0] == q[0]:
        return f"{b[0]} {b[1]} Auckland"
    if b and q:
        sess_set = {b[0], q[0]}
        if {"Tokyo", "London"} <= sess_set or {"Sydney/Tokyo", "London"} <= sess_set:
            return "London open 7pm–9pm Auckland"
        if {"London", "New York"} <= sess_set:
            return "London/NY overlap 1am–4am Auckland"
        if {"Tokyo", "New York"} <= sess_set or {"Sydney/Tokyo", "New York"} <= sess_set:
            return "Sydney/Tokyo 9am–9pm Auckland"
    s = b or q
    return f"{s[0]} {s[1]} Auckland"


def _session_time_label(pair: str, now_ak: datetime) -> str:
    """Return best session label with today/tonight/tomorrow morning based on current Auckland time.

    Uses specific currency logic: cross-JPY pairs go to London open, USD/JPY to NY open,
    pure JPY to Tokyo, EUR/GBP/CHF to London, USD/CAD to New York, AUD/NZD to Sydney/Tokyo.
    'Today' = session starts within 8 hours; 'tonight' = 8–20 hours away; else 'tomorrow morning'.
    """
    cleaned = pair.upper().replace("/", "").replace("-", "")
    base  = cleaned[:3]
    quote = cleaned[3:6] if len(cleaned) >= 6 else ""
    ccys  = {base, quote}

    if "JPY" in ccys and ccys & {"EUR", "GBP", "CHF"}:
        sess_name, window, start_h = "London open sweet spot", "7pm–9pm", 19
    elif "JPY" in ccys and ccys & {"USD", "CAD"}:
        sess_name, window, start_h = "New York open", "1am–3am", 1
    elif "JPY" in ccys:
        sess_name, window, start_h = "Tokyo session peak", "12pm–3pm", 12
    elif ccys & {"EUR", "GBP", "CHF"}:
        sess_name, window, start_h = "London session peak", "7pm–10pm", 19
    elif ccys & {"AUD", "NZD"}:
        sess_name, window, start_h = "Sydney/Tokyo peak", "9am–1pm", 9
    elif ccys & {"USD", "CAD"}:
        sess_name, window, start_h = "New York session peak", "1am–4am", 1
    else:
        sess_name, window, start_h = "London open", "7pm–9pm", 19

    hours_away = (start_h - now_ak.hour) % 24
    if hours_away == 0:
        time_ref = "now"
    elif hours_away <= 8:
        time_ref = "today"
    elif hours_away <= 20:
        time_ref = "tonight"
    else:
        time_ref = "tomorrow morning"

    return f"{sess_name} {window} Auckland {time_ref}"


def _fmt_time_exact(h: int, m: int = 0) -> str:
    """Format 24-hour time as '5:00pm', '10:30pm'."""
    ampm = "am" if h < 12 else "pm"
    h12  = h % 12 or 12
    return f"{h12}:{m:02d}{ampm}"


# ── Trading filter constants ───────────────────────────────────────────────────

MAX_CORRELATED_EXPOSURE = 2
BASE_RISK_PCT           = 1.0

SIZING_RULES = {
    "RANGING_LOW_VOL":    0.5,
    "RANGING_HIGH_VOL":   0.5,
    "RISK_OFF":           0.5,
    "TRENDING_RISK_ON":   1.0,
    "TRENDING_RISK_OFF":  0.7,
}

LOSS_STREAK_SIZING = {
    0: 1.0,
    1: 1.0,
    2: 0.75,
    3: 0.5,
    4: 0.25,
}

DRAWDOWN_SIZING = {
    2.0:  1.0,
    4.0:  0.75,
    6.0:  0.5,
    8.0:  0.25,
    10.0: 0.0,
}

# ── Fund pair universe — explicit banlist ─────────────────────────────────────
# The selector now uses a curated 28-pair G8-only universe so most exotic bans
# are redundant. This frozenset is kept as a safety net for any stale cache data.
FUND_BANNED_PAIRS: frozenset = frozenset({
    # Non-G8 currencies — should never appear from curated universe
    "EUR/HKD", "CAD/HKD", "NZD/HKD", "GBP/HKD", "AUD/HKD", "CHF/HKD", "USD/HKD",
    "USD/NOK", "EUR/NOK", "GBP/NOK", "AUD/NOK", "NZD/NOK", "CAD/NOK",
    "USD/SEK", "EUR/SEK", "GBP/SEK",
    "USD/SGD", "EUR/SGD", "GBP/SGD", "AUD/SGD", "NZD/SGD",
    "USD/DKK", "EUR/DKK", "GBP/DKK",
    "USD/ZAR", "EUR/ZAR", "GBP/ZAR",
    "USD/MXN", "EUR/MXN",
    "USD/TRY", "EUR/TRY", "GBP/TRY",
})

PAIR_BEST_SESSIONS = {
    "JPY": ["tokyo", "london"],
    "AUD": ["tokyo", "london"],
    "NZD": ["tokyo", "london"],
    "SGD": ["tokyo", "london"],
    "HKD": ["tokyo", "london"],
    "EUR": ["london", "new_york"],
    "GBP": ["london", "new_york"],
    "CHF": ["london", "new_york"],
    "NOK": ["london"],
    "SEK": ["london"],
    "USD": ["london", "new_york"],
    "CAD": ["new_york"],
}

_FX_SESSIONS = {
    "tokyo":    (23, 8),
    "london":   (7, 16),
    "new_york": (12, 21),
}


def _get_current_session() -> list:
    """Return list of active forex sessions (UTC hours)."""
    hour = datetime.now(timezone.utc).hour
    active = []
    for session, (start, end) in _FX_SESSIONS.items():
        if start > end:
            if hour >= start or hour < end:
                active.append(session)
        else:
            if start <= hour < end:
                active.append(session)
    return active


def _check_session_filter(pair: str, log_fn=None) -> dict:
    """Check if current session is appropriate for this pair."""
    _log = log_fn or print
    active = _get_current_session() or ["new_york"]
    ccy1, ccy2 = (pair.split("/") if "/" in pair else (pair[:3], pair[3:]))
    best = list(set(PAIR_BEST_SESSIONS.get(ccy1, []) + PAIR_BEST_SESSIONS.get(ccy2, [])))
    if not best:
        return {"allowed": True, "reason": "no session preference",
                "active_sessions": active, "best_sessions": best}
    overlap = [s for s in active if s in best]
    if overlap:
        return {"allowed": True, "reason": f"in best session: {overlap}",
                "active_sessions": active, "best_sessions": best}
    _log(f"[session] {pair}: active={active} best={best} — wrong session")
    return {"allowed": False,
            "reason": f"best sessions {best} not active (current: {active})",
            "active_sessions": active, "best_sessions": best}


def _has_upcoming_news(pair: str, minutes_before: int = 45,
                       minutes_after: int = 30, log_fn=None) -> dict:
    """Check if high-impact news is within the blackout window for this pair."""
    _log = log_fn or print
    try:
        from src import economic_calendar as _ec_news
        ccys = pair.split("/") if "/" in pair else [pair[:3], pair[3:]]
        events = _ec_news.events_for_pair(pair, hours=2)
        if not events:
            return {"blocked": False, "reason": ""}
        for event in events:
            impact = str(event.get("impact", "")).upper()
            if "HIGH" not in impact:
                continue
            event_ccy = str(event.get("currency", "")).upper()
            if event_ccy not in ccys:
                continue
            try:
                mins_until = float(event.get("hours_away", 999)) * 60
                if -minutes_after <= mins_until <= minutes_before:
                    title = event.get("title", "")
                    _log(f"[news] BLOCKING {pair} — {event_ccy} {title} in {mins_until:.0f}m")
                    return {"blocked": True,
                            "reason": f"News blackout: {event_ccy} {title} in {mins_until:.0f}m",
                            "event": event}
            except Exception:
                continue
        return {"blocked": False, "reason": ""}
    except Exception:
        return {"blocked": False, "reason": ""}


def _get_trend_alignment(pair: str, direction: str, log_fn=None) -> dict:
    """Check if trade direction aligns with weekly and monthly trends."""
    _log = log_fn or print
    result = {
        "weekly_aligned": None, "monthly_aligned": None,
        "weekly_trend": "NEUTRAL", "monthly_trend": "NEUTRAL",
        "both_aligned": False, "alignment_score": 0,
    }
    score = 0
    dirn = str(direction).upper()
    try:
        from src import technical as _tech_ta
        wt = _tech_ta.get_weekly_trend(pair)
        weekly_trend = wt.get("trend", "NEUTRAL")
        result["weekly_trend"] = weekly_trend
        if dirn == "BUY":
            aligned = weekly_trend in ("BUY", "NEUTRAL")
            score += 2 if weekly_trend == "BUY" else (1 if weekly_trend == "NEUTRAL" else 0)
        else:
            aligned = weekly_trend in ("SELL", "NEUTRAL")
            score += 2 if weekly_trend == "SELL" else (1 if weekly_trend == "NEUTRAL" else 0)
        result["weekly_aligned"] = aligned
        _log(f"[trend] {pair} weekly: {weekly_trend} aligned={aligned}")
    except Exception as e:
        _log(f"[trend] {pair} weekly error: {e}")
    try:
        from src import technical as _tech_ta2
        mt = _tech_ta2.get_monthly_trend(pair)
        monthly_trend = mt.get("trend", "NEUTRAL")
        result["monthly_trend"] = monthly_trend
        if dirn == "BUY":
            aligned = monthly_trend in ("BUY", "NEUTRAL")
            score += 2 if monthly_trend == "BUY" else (1 if monthly_trend == "NEUTRAL" else 0)
        else:
            aligned = monthly_trend in ("SELL", "NEUTRAL")
            score += 2 if monthly_trend == "SELL" else (1 if monthly_trend == "NEUTRAL" else 0)
        result["monthly_aligned"] = aligned
        _log(f"[trend] {pair} monthly: {monthly_trend} aligned={aligned}")
    except Exception as e:
        _log(f"[trend] {pair} monthly error: {e}")
    result["alignment_score"] = score
    result["both_aligned"] = (result.get("weekly_aligned") is not False and
                               result.get("monthly_aligned") is not False)
    return result


def _check_correlation(new_pair: str, new_direction: str,
                       open_trades: list, log_fn=None) -> dict:
    """Check if new trade adds too much correlated exposure to open trades."""
    _log = log_fn or print

    def _get_groups(pair, direction):
        groups = []
        p, d = str(pair), str(direction).upper()
        if p.startswith("USD/"):
            groups.append("USD_STRENGTH" if d == "BUY" else "USD_WEAKNESS")
        elif "/USD" in p:
            groups.append("USD_WEAKNESS" if d == "BUY" else "USD_STRENGTH")
        if "EUR" in p:
            if (p.startswith("EUR/") and d == "BUY") or (not p.startswith("EUR/") and d == "SELL"):
                groups.append("EUR_STRENGTH")
        if "JPY" in p:
            if (p.endswith("/JPY") and d == "SELL") or (p.startswith("JPY/") and d == "BUY"):
                groups.append("JPY_STRENGTH")
        if p in ("AUD/USD", "NZD/USD", "AUD/JPY", "NZD/JPY", "GBP/JPY"):
            groups.append("RISK_ON" if d == "BUY" else "RISK_OFF")
        return groups

    new_groups = _get_groups(new_pair, new_direction)
    for group in new_groups:
        correlated = []
        for ot in open_trades:
            ot_pair = str(ot.get("pair", ""))
            ot_dir  = str(
                ot.get("direction") or
                (ot.get("parsed") or {}).get("direction") or ""
            ).upper()
            if group in _get_groups(ot_pair, ot_dir):
                correlated.append(ot_pair)
        if len(correlated) >= MAX_CORRELATED_EXPOSURE:
            reason = f"{group} exposure already at {len(correlated)}: {correlated}"
            _log(f"[correlation] BLOCKING {new_pair} {new_direction} — {reason}")
            return {"blocked": True, "reason": reason,
                    "correlated_trades": correlated, "group": group}
    return {"blocked": False, "reason": "", "correlated_trades": [], "group": ""}


def _check_loss_journal(
    pair: str,
    direction: str,
    regime: str,
    session: str,
    weekly_trend: str,
    rsi: float = 0.0,
    stop_pips: float = 0.0,
    confidence: float = 0.0,
    log_fn=None,
) -> dict:
    """Check if this trade setup matches patterns that caused past losses.

    Evaluates extracted IF/THEN rules by testing specific field conditions
    rather than keyword matching. Updates times_triggered counters.

    Returns {blocked, warnings, matching_rules, risk_score}.
    """
    _log = log_fn or print
    _journal_path = "data/loss_journal.json"
    try:
        import json as _jj
        with open(_journal_path, encoding="utf-8") as _f:
            journal = _jj.load(_f)
    except Exception:
        return {"blocked": False, "warnings": [], "matching_rules": [], "risk_score": 0.0}

    warnings            = []
    matching_rules      = []
    risk_score          = 0.0
    any_rule_triggered  = False

    regime_l = str(regime).lower()
    pair_l   = str(pair).lower()
    dir_l    = str(direction).lower()
    wt_l     = str(weekly_trend).lower()
    rsi_f    = float(rsi or 0)
    stop_f   = float(stop_pips or 0)
    conf_f   = float(confidence or 0)

    import re as _re_jrnl

    for rule_entry in journal.get("extracted_rules", []):
        rule_text  = str(rule_entry.get("rule", ""))
        rule_lower = rule_text.lower()
        if not rule_lower:
            continue

        rule_matched = False

        # ── Detect what conditions the rule mentions ──────────────────────────

        _zero_stop = any(kw in rule_lower for kw in (
            "stop_distance = 0", "stop_distance=0",
            "stop distance equals 0", "stop = 0",
            "stop_pips = 0",
        ))
        _rsi_zero = any(kw in rule_lower for kw in (
            "rsi = 0", "rsi=0", "rsi reading = 0",
            "rsi reading incomplete",
        ))
        _rsi_low = (
            ("rsi" in rule_lower and "below 15" in rule_lower) or
            ("rsi" in rule_lower and "< 15" in rule_lower) or
            ("rsi" in rule_lower and "< 10" in rule_lower)
        )
        _conf_m = _re_jrnl.search(
            r"confidence[_a-z]*\s*[<>]=?\s*([0-9.]+)", rule_lower
        )
        _regime_missing = any(kw in rule_lower for kw in (
            "regime = undefined", "regime = null",
            "regime = missing", "market_regime = null",
            "market_regime = missing", "market_regime = undefined",
            "regime.*undefined",
        ))
        _regime_ranging = "ranging" in rule_lower and "regime" in rule_lower
        _wt_missing = (
            "weekly_trend" in rule_lower and
            any(kw in rule_lower for kw in ("null", "missing", "none"))
        )
        _wt_bullish_only = (
            "weekly_trend is not confirmed as bullish" in rule_lower or
            ("weekly_trend" in rule_lower and "bullish" in rule_lower)
        )
        _exotic_pair = any(kw in rule_lower for kw in ("exotic", "hkd", "sek", "nok"))

        # ── Test conditions against current trade context ─────────────────────

        if _zero_stop and stop_f == 0:
            rule_matched = True
        elif _rsi_zero and rsi_f == 0:
            rule_matched = True
        elif _rsi_low and (rsi_f == 0 or rsi_f < 15):
            rule_matched = True
        elif _conf_m and conf_f > 0:
            _thresh  = float(_conf_m.group(1))
            _op_seg  = rule_lower[_conf_m.start():_conf_m.end()]
            if "<" in _op_seg and conf_f < _thresh:
                rule_matched = True
            elif ">" in _op_seg and conf_f > _thresh:
                rule_matched = True
        elif _regime_missing and regime_l in ("", "undefined", "null", "unknown", "none"):
            rule_matched = True
        elif _regime_ranging and "ranging" in regime_l:
            rule_matched = True
        elif _wt_missing and wt_l in ("", "null", "none", "unknown"):
            rule_matched = True
        elif _wt_bullish_only and dir_l == "buy" and wt_l not in ("buy", "bullish", "up"):
            rule_matched = True
        elif _exotic_pair and any(c in pair_l for c in ("hkd", "sek", "nok")):
            rule_matched = True

        if rule_matched:
            warnings.append(f"Loss journal: {rule_text[:100]}")
            matching_rules.append(rule_text)
            risk_score = min(1.0, risk_score + 0.25)
            any_rule_triggered = True
            try:
                rule_entry["times_triggered"] = int(rule_entry.get("times_triggered", 0)) + 1
            except Exception:
                pass

    # Pattern frequency checks
    patterns = journal.get("pattern_counts", {})
    if patterns.get("ranging", 0) >= 3 and "ranging" in regime_l:
        warnings.append(f"Ranging regime caused {patterns['ranging']} past losses")
        risk_score = min(1.0, risk_score + 0.15)
    if patterns.get("stop", 0) >= 2 and stop_f == 0:
        warnings.append("Zero stop contributed to multiple past losses")
        risk_score = min(1.0, risk_score + 0.20)

    # Persist updated trigger counters
    if any_rule_triggered:
        try:
            import shutil as _sh_jrnl
            _tmp_j = _journal_path + ".tmp"
            with open(_tmp_j, "w", encoding="utf-8") as _wf:
                _jj.dump(journal, _wf, indent=2)
            _sh_jrnl.move(_tmp_j, _journal_path)
        except Exception:
            pass

    # Safety valve: conf 9+ with clean validated data bypasses journal block.
    # Prevents a journal bug from permanently suppressing elite validated setups.
    if (risk_score >= 0.6 and
            conf_f >= 9.0 and
            stop_f > 10 and
            rsi_f > 10):
        _log(f"[loss-journal] {pair} HIGH CONF BYPASS: "
             f"conf={conf_f:.1f} stop={stop_f:.0f}p rsi={rsi_f:.0f} — "
             f"overrides risk_score={risk_score:.2f}")
        return {
            "blocked":        False,
            "warnings":       warnings,
            "matching_rules": matching_rules,
            "risk_score":     risk_score,
            "bypassed":       True,
        }

    blocked = risk_score >= 0.6
    if warnings:
        _log(f"[loss-journal] {pair} risk_score={risk_score:.2f} warnings={len(warnings)}")

    return {
        "blocked":        blocked,
        "warnings":       warnings,
        "matching_rules": matching_rules,
        "risk_score":     risk_score,
    }


def _validate_trade_data(
    parsed: dict,
    pair: str,
    direction: str,
    bundle: dict = None,
    log_fn=None,
) -> dict:
    """Hard validation gate — blocks trades with missing or corrupt critical data.

    Prevents trading blind, which caused 6 of 7 losses in the loss autopsy.
    Returns {valid, failures, critical_failure}.
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


def _ensure_trade_data_complete(
    trade_row: dict,
    pair: str,
    log_fn=None,
) -> dict:
    """Fill any computable fields that are zero/missing after a trade is accepted."""
    _log = log_fn or print
    ps    = 0.01 if "JPY" in pair else 0.0001
    entry = float(trade_row.get("entry") or trade_row.get("entry_price") or 0)
    stop  = float(trade_row.get("stop_loss") or trade_row.get("stop") or 0)
    t1    = float(trade_row.get("t1_price") or trade_row.get("target") or 0)

    if entry > 0 and stop > 0:
        stop_pips = abs(entry - stop) / ps
        if not float(trade_row.get("stop_pips_at_entry") or 0):
            trade_row["stop_pips_at_entry"] = round(stop_pips, 1)
        if t1 > 0 and not float(trade_row.get("rr_at_entry") or 0):
            t1_pips = abs(t1 - entry) / ps
            rr = t1_pips / stop_pips if stop_pips > 0 else 0
            trade_row["rr_at_entry"] = round(rr, 2)

    for _field in ("stop_loss", "entry", "confidence", "t1_price"):
        _val = trade_row.get(_field)
        try:
            _f = float(_val or 0)
            if _f == 0 or _f != _f:
                _log(f"[trade-data] WARNING {pair}: {_field} = {_val!r} at write time")
        except Exception:
            pass

    return trade_row


def _send_weekly_learning_summary(log_fn=None) -> None:
    """Send a Discord summary of what the loss-journal learned this week."""
    _log = log_fn or print
    import json as _jj
    try:
        with open("data/loss_journal.json", encoding="utf-8") as _f:
            journal = _jj.load(_f)
    except Exception:
        return

    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    week_ago = (_dt.now(_tz.utc) - _td(days=7)).isoformat()
    recent   = [a for a in journal.get("analyses", []) if a.get("timestamp", "") >= week_ago]
    patterns = journal.get("pattern_counts", {})
    rules    = journal.get("extracted_rules", [])

    if not recent and not rules:
        return

    import os as _os
    import requests as _req
    webhook = _os.getenv("DISCORD_WEBHOOK_HEALTH") or _os.getenv("DISCORD_WEBHOOK_FUND")
    if not webhook:
        return

    top_patterns = sorted(patterns.items(), key=lambda x: x[1], reverse=True)[:5]

    embed = {
        "title":       "\U0001f9e0 Weekly Learning Summary",
        "description": "What the system learned this week",
        "color":       0x3498DB,
        "fields": [
            {
                "name":   "\U0001f4ca Losses analysed this week",
                "value":  str(len(recent)),
                "inline": True,
            },
            {
                "name":   "Rules extracted (total)",
                "value":  str(len(rules)),
                "inline": True,
            },
            {
                "name":   "\U0001f501 Top loss patterns",
                "value":  "\n".join(f"• {p}: {c} losses" for p, c in top_patterns) or "None yet",
                "inline": False,
            },
            {
                "name":   "\U0001f4cf Active rules",
                "value":  "\n".join(f"• {r['rule'][:80]}" for r in rules[-3:]) or "None yet",
                "inline": False,
            },
        ],
        "footer": {"text": "Forex AI — Learning System"},
    }
    try:
        _req.post(webhook, json={"embeds": [embed]}, timeout=10)
        _log("[learning] Weekly learning summary sent to Discord")
    except Exception as exc:
        _log(f"[learning] Weekly summary error: {exc}")


def _calculate_position_size(regime: str, consecutive_losses: int,
                              drawdown_pct: float, base_pct: float = None,
                              log_fn=None) -> dict:
    """Calculate position size based on regime, loss streak, and drawdown."""
    _log = log_fn or print
    if base_pct is None:
        base_pct = BASE_RISK_PCT
    multipliers = {}
    regime_clean = str(regime).upper()
    regime_mult = 1.0
    for key, mult in SIZING_RULES.items():
        if key in regime_clean:
            regime_mult = mult
            break
    multipliers["regime"] = regime_mult
    streak_mult = 1.0
    for streak, mult in sorted(LOSS_STREAK_SIZING.items(), reverse=True):
        if consecutive_losses >= streak:
            streak_mult = mult
            break
    multipliers["loss_streak"] = streak_mult
    dd_mult = 1.0
    for dd_level, mult in sorted(DRAWDOWN_SIZING.items()):
        if drawdown_pct <= dd_level:
            dd_mult = mult
            break
    multipliers["drawdown"] = dd_mult
    final_mult = min(regime_mult, streak_mult, dd_mult)
    risk_pct = max(0.25, min(2.0, round(base_pct * final_mult, 2)))
    if final_mult >= 1.0:
        mode = "NORMAL"
    elif final_mult >= 0.75:
        mode = "REDUCED"
    elif final_mult >= 0.5:
        mode = "DEFENSIVE"
    else:
        mode = "MINIMAL"
    reason_parts = []
    if regime_mult < 1.0:
        reason_parts.append(f"regime={regime_clean}")
    if streak_mult < 1.0:
        reason_parts.append(f"losses={consecutive_losses}")
    if dd_mult < 1.0:
        reason_parts.append(f"drawdown={drawdown_pct:.1f}%")
    reason = ", ".join(reason_parts) if reason_parts else "normal"
    _log(f"[sizing] {mode}: {base_pct}% × {final_mult:.2f} = {risk_pct}% ({reason})")
    return {"risk_pct": risk_pct, "sizing_mode": mode, "reason": reason,
            "multipliers": multipliers, "final_multiplier": final_mult}


def _entry_window_for_pair(pair: str) -> tuple:
    """Return (start_h, start_m, end_h, end_m, window_str, cutoff_str, sess_name).

    Precise 90-minute high-liquidity windows for each currency group.
    Cutoff is the absolute no-entry deadline.
    """
    cleaned = pair.upper().replace("/", "").replace("-", "")
    base  = cleaned[:3]
    quote = cleaned[3:6] if len(cleaned) >= 6 else ""
    ccys  = {base, quote}

    if ccys == {"USD", "JPY"}:
        return (1, 0, 2, 0, "1:00am–2:00am", "2:30am", "New York open")
    if "JPY" in ccys and ccys & {"EUR", "GBP", "CHF"}:
        return (19, 0, 20, 0, "7:00pm–8:00pm", "9:00pm", "London open")
    if "JPY" in ccys:
        return (12, 0, 13, 30, "12:00pm–1:30pm", "3:00pm", "Tokyo open")
    if ccys & {"EUR", "GBP", "CHF"}:
        return (19, 0, 20, 30, "7:00pm–8:30pm", "11:00pm", "London open")
    if ccys & {"AUD", "NZD"}:
        return (9, 0, 10, 30, "9:00am–10:30am", "12:00pm", "Sydney open")
    if ccys & {"USD", "CAD"}:
        return (1, 0, 2, 30, "1:00am–2:30am", "3:00am", "New York open")
    return (19, 0, 20, 30, "7:00pm–8:30pm", "11:00pm", "London open")


def _entry_quality(pair: str, now_ak: datetime) -> tuple:
    """Return (emoji, label) based on how close we are to the optimal entry window now.

    🟢 ENTER NOW   — currently inside optimal window
    🟡 ENTER SOON  — window opens in < 2 hours
    🟠 WAIT        — window opens in 2-6 hours
    🔴 WAIT UNTIL TOMORROW — more than 6 hours away
    """
    start_h, start_m, end_h, end_m, _, _, sess_name = _entry_window_for_pair(pair)
    cur_mins   = now_ak.hour * 60 + now_ak.minute
    win_s_mins = start_h * 60 + start_m
    win_e_mins = end_h   * 60 + end_m

    if win_s_mins <= win_e_mins:
        in_window = win_s_mins <= cur_mins <= win_e_mins
    else:
        in_window = cur_mins >= win_s_mins or cur_mins <= win_e_mins

    if in_window:
        return "🟢", "ENTER NOW — currently in optimal session window"

    mins_away  = (win_s_mins - cur_mins) % (24 * 60)
    hours_away = mins_away / 60
    if mins_away < 120:
        return "🟡", f"ENTER SOON — {sess_name} opens in {mins_away} min"
    hrs = mins_away // 60
    rem = mins_away % 60
    t   = f"{hrs}h {rem}m" if rem else f"{hrs}h"
    # Use same 8h / 20h thresholds as _time_ref_for_entry so badge always matches text
    if hours_away <= 8:
        return "🟠", f"WAIT — {sess_name} opens in {t} (today)"
    if hours_away <= 20:
        return "🟠", f"WAIT — {sess_name} opens in {t} (tonight)"
    return "🔴", "WAIT UNTIL TOMORROW — optimal window more than 20 hours away"


def _session_status_for_pair(pair: str, now_ak: datetime) -> tuple:
    """Return (is_active, close_str) for the pair's primary trading session.

    is_active: True if the session is currently open in Auckland time.
    close_str: Human-readable close time, e.g. '4am', '9pm'.
    """
    _, _, _, _, _, _, sess_name = _entry_window_for_pair(pair)
    hour = now_ak.hour
    _SESS_WINDOWS = {
        "London open":   (19, 4,  "4am",  True),
        "Tokyo open":    (12, 21, "9pm",  False),
        "New York open": (1,  10, "10am", False),
        "Sydney open":   (9,  22, "10pm", False),
    }
    window = _SESS_WINDOWS.get(sess_name)
    if not window:
        return False, ""
    open_h, close_h, close_label, wraps = window
    if wraps:
        is_active = hour >= open_h or hour < close_h
    else:
        is_active = open_h <= hour < close_h
    return is_active, close_label


def _time_ref_for_entry(start_h: int, start_m: int, now_ak: datetime) -> str:
    """Return 'TODAY', 'tonight', or 'tomorrow' for a session start time."""
    cur_mins  = now_ak.hour * 60 + now_ak.minute
    win_mins  = start_h * 60 + start_m
    hours_away = ((win_mins - cur_mins) % (24 * 60)) / 60
    if hours_away <= 8:
        return "TODAY"
    if hours_away <= 20:
        return "tonight"
    return "tomorrow"


def _log_line(handle, msg: str) -> None:
    stamp = _auckland_now().strftime("%H:%M:%S")
    line  = f"[{stamp}] {msg}"
    print(line)
    if handle is None:
        return
    try:
        handle.write(line + "\n")
        handle.flush()
    except Exception:
        pass


def _telegram_test() -> None:
    """Call getMe to verify the bot token is valid; log the result."""
    if not config.TELEGRAM_TOKEN:
        print("[TELEGRAM] Bot token INVALID — check TELEGRAM_TOKEN secret")
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/getMe"
    try:
        resp = urllib.request.urlopen(url, timeout=10)
        data = json.loads(resp.read().decode())
        if data.get("ok"):
            bot_name = data["result"].get("username", "Unknown")
            print(f"[TELEGRAM] Bot token valid — bot name: {bot_name}")
        else:
            print("[TELEGRAM] Bot token INVALID — check TELEGRAM_TOKEN secret")
    except Exception as exc:
        print(f"[TELEGRAM] Bot token INVALID — check TELEGRAM_TOKEN secret ({exc})")


def _telegram(message: str) -> None:
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("[TELEGRAM] SKIP — TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return
    # Preserve intentional <b>, </b>, <i>, </i> tags while escaping all other
    # < > & characters that come from AI-generated text (e.g. "< 1.08 support").
    # Telegram HTML mode rejects any unescaped special chars and returns 400 silently.
    _TAG = _re_tg.compile(r'(</?[bi]>)')
    _parts = _TAG.split(message)
    message = "".join(
        p if i % 2 == 1 else _html_mod.escape(p, quote=False)
        for i, p in enumerate(_parts)
    )
    _named_recipients = [("Heath", config.TELEGRAM_CHAT_ID)]
    if config.TELEGRAM_CHAT_ID_2:
        _named_recipients.append(("George", config.TELEGRAM_CHAT_ID_2))
    if config.TELEGRAM_CHAT_ID_3:
        _named_recipients.append(("Max", config.TELEGRAM_CHAT_ID_3))
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    _preview = message[:120].replace("\n", " ")
    for name, chat_id in _named_recipients:
        try:
            data = urllib.parse.urlencode({
                "chat_id":    chat_id,
                "text":       message,
                "parse_mode": "HTML",
            }).encode()
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15)
            print(f"[TELEGRAM] SUCCESS — sent to {name} ({len(message)} chars)")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            print(f"[TELEGRAM] FAILED — {name}: HTTP {exc.code} {exc.reason} | {body} | preview: {_preview}", file=sys.stderr)
        except Exception as exc:
            print(f"[TELEGRAM] FAILED — {name}: {exc} | preview: {_preview}", file=sys.stderr)


_API_USAGE_FILE = config.DATA_DIR / "api_usage.json"


def _check_twelvedata_health(log=print) -> bool:
    """Test Twelve Data API with a single EUR/USD price request.

    Returns True if the API is responsive, False if unavailable.
    Also tracks daily call count in data/api_usage.json and warns when
    approaching the 800-call free tier limit.
    """
    if not config.TWELVE_DATA_KEY:
        return False

    # Update and check daily call counter
    try:
        from datetime import date as _date
        _today = str(_date.today())
        _usage: dict = {}
        if _API_USAGE_FILE.exists():
            try:
                _usage = json.loads(_API_USAGE_FILE.read_text(encoding="utf-8"))
            except Exception:
                _usage = {}
        # Reset counter if it's a new day
        if _usage.get("date") != _today:
            _usage = {"date": _today, "calls": 0}
        _calls_today = int(_usage.get("calls") or 0)
        if _calls_today > 700:
            log(
                f"⚠️ Twelve Data daily limit approaching — {_calls_today} calls used today — "
                f"reducing pair count to preserve data quality"
            )
        _usage["calls"] = _calls_today + 1
        _API_USAGE_FILE.write_text(json.dumps(_usage), encoding="utf-8")
    except Exception:
        pass

    # Single health-check request
    try:
        _url = (
            f"https://api.twelvedata.com/price"
            f"?symbol=EUR%2FUSD&apikey={config.TWELVE_DATA_KEY}"
        )
        _resp = urllib.request.urlopen(_url, timeout=10)
        _data = json.loads(_resp.read().decode())
        if isinstance(_data, dict) and _data.get("status") == "error":
            log(
                f"❌ Twelve Data API unavailable — technical scores will be neutral this scan "
                f"— consider waiting for next scan before acting ({_data.get('message','')})"
            )
            return False
        return True
    except Exception as _exc:
        log(
            f"❌ Twelve Data API unavailable — technical scores will be neutral this scan "
            f"— consider waiting for next scan before acting ({_exc})"
        )
        return False


def _twelvedata_call_limit_reached() -> bool:
    """Return True if today's Twelve Data call count exceeds 700."""
    try:
        from datetime import date as _date
        if not _API_USAGE_FILE.exists():
            return False
        _usage = json.loads(_API_USAGE_FILE.read_text(encoding="utf-8"))
        return _usage.get("date") == str(_date.today()) and int(_usage.get("calls") or 0) > 700
    except Exception:
        return False


def _fmt_price(v) -> str:
    if v is None:
        return "—"
    f = float(v)
    return f"{f:.3f}" if f > 10 else f"{f:.5f}"


def _conf(result: dict) -> int:
    try:
        return int(result["parsed"].get("confidence") or 0)
    except (TypeError, ValueError):
        return 0


def _cot_reversal_penalty(result: dict) -> int:
    """Return -1 if a REVERSING COT signal aligns with the OLD positioning that
    now contradicts the trade direction, else 0.

    Rule: penalise when institutions recently FLIPPED and you are trading with
    the crowd that is now exiting.
      BUY  + base  COT REVERSING from net LONG  → institutions abandoned their long
      BUY  + quote COT REVERSING from net SHORT → institutions abandoned their short
      SELL + base  COT REVERSING from net SHORT → institutions abandoned their short
      SELL + quote COT REVERSING from net LONG  → institutions abandoned their long
    """
    try:
        direction = (result.get("parsed", {}).get("direction") or "").upper()
        if direction not in ("BUY", "SELL"):
            return 0
        pos = result.get("bundle", {}).get("positioning", {})
        pair = result.get("pair", "")
        clean = pair.upper().replace("/", "")
        base_ccy  = clean[:3] if len(clean) >= 6 else ""
        quote_ccy = clean[3:6] if len(clean) >= 6 else ""

        for side, ccy in (("base", base_ccy), ("quote", quote_ccy)):
            pp = pos.get(side, {})
            if pp.get("status") != "ok":
                continue
            if pp.get("cot_momentum") != "REVERSING":
                continue
            old_net   = pp.get("net_3w_ago", 0)
            old_long  = old_net > 0
            old_short = old_net < 0
            # BUY: you want base up — penalty if base was long (flipped to short)
            #      or quote was short (flipped to long, making quote stronger)
            if direction == "BUY":
                if side == "base"  and old_long:  return -1
                if side == "quote" and old_short: return -1
            # SELL: you want base down — penalty if base was short (flipped to long)
            #       or quote was long (flipped to short, weakening quote you need weak)
            else:
                if side == "base"  and old_short: return -1
                if side == "quote" and old_long:  return -1
    except Exception:
        pass
    return 0


def _smd_score(result: dict) -> int:
    """Extract pre-computed Smart Money Divergence score from the bundle (−10 to +10)."""
    try:
        return int(result.get("bundle", {}).get("smart_money", {}).get("smd_score", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _eff_conf(result: dict) -> float:
    """Confidence after MA ribbon, COT momentum, Smart Money Divergence,
    and devil's advocate adjustments.

    Ribbon:         −1 when ALIGNED ribbon is fully against trade direction.
    COT reversal:   −1 when institutions just flipped away from the direction.
    Devil's adv:    +0.5 when second opinion raises no objections (allows a
                    conf-5 trade to qualify at the 5.5 risk-on threshold).
    All adjustments can stack (max −2 penalty, +0.5 boost).
    """
    raw = _conf(result)
    if raw == 0:
        return 0.0
    adj = 0
    try:
        direction = (result.get("parsed", {}).get("direction") or "").upper()
        rib_status = (
            result.get("bundle", {})
            .get("technical", {})
            .get("daily", {}) or {}
        ).get("ribbon", {}).get("status", "")
        if rib_status == "ALIGNED_BULL" and direction == "SELL":
            adj -= 1
        if rib_status == "ALIGNED_BEAR" and direction == "BUY":
            adj -= 1
    except Exception:
        pass
    adj += _cot_reversal_penalty(result)
    # Fundamental alignment adjustment
    _fa = result.get("_fundamental_alignment")
    if _fa is None:
        _fa_pair = result.get("pair", "")
        _fa_dirn = (result.get("parsed", {}).get("direction") or "").upper()
        if _fa_pair and "/" in _fa_pair and _fa_dirn:
            try:
                from src import fundamentals as _fund
                _fa_base, _fa_quot = _fa_pair.split("/")
                _fa = _fund.get_fundamental_alignment(_fa_base, _fa_quot, _fa_dirn)
                result["_fundamental_alignment"] = _fa
            except Exception:
                _fa = {}
    adj += (_fa or {}).get("conf_adj", 0)
    # Smart Money Divergence: boost when aligned, penalty when opposing
    try:
        _smd = _smd_score(result)
        _smd_dir = (result.get("parsed", {}).get("direction") or "").upper()
        if (_smd >= 8 and _smd_dir == "BUY") or (_smd <= -8 and _smd_dir == "SELL"):
            adj += 1
        elif (_smd <= -5 and _smd_dir == "BUY") or (_smd >= 5 and _smd_dir == "SELL"):
            adj -= 1
    except Exception:
        pass
    # Devil's advocate boost: +0.5 when second opinion raised no objections
    da_boost = float(result.get("_da_boost", 0.0) or 0.0)
    return max(1.0, raw + adj + da_boost)


def _conf_bar(conf) -> str:
    """Generate visual confidence bar: 7 → ███████░░░"""
    try:
        n = max(0, min(10, int(conf)))
    except (TypeError, ValueError):
        n = 0
    return "█" * n + "░" * (10 - n)


def _get_open_fund_count() -> int:
    """Fresh CSV read for fund capacity. Counts OPEN + PENDING (pending will consume a slot)."""
    try:
        import pandas as _pd_cap
        _df_cap = _pd_cap.read_csv("data/trades.csv")
        return int(len(_df_cap[
            (_df_cap["trade_this"].astype(str) == "YES") &
            (_df_cap["status"].isin(["OPEN", "PENDING"]))
        ]))
    except Exception:
        return 0


MAX_FUND_TRADES         = 4   # kept for legacy log lines
MAX_FUND_TRADES_NORMAL   = 4
MAX_FUND_TRADES_OVERRIDE = 5
OVERRIDE_MIN_CONF        = 7.5
OVERRIDE_MAX_DAILY_LOSS  = -2.0
OVERRIDE_MAX_UNREALISED  = -3.0
OVERRIDE_RISK_STRONG     = 0.75
OVERRIDE_RISK_ELITE      = 0.50

# ── Portfolio swap constants ──────────────────────────────────────────────────
SWAP_MIN_NEW_CONF         = 8.0   # new trade must be ≥ this to trigger evaluation
SWAP_MIN_CONF_GAP         = 2.0   # new conf must beat existing by at least this
SWAP_MAX_EXISTING_CONF    = 7.0   # existing trade conf ≤ this is swap-eligible
SWAP_MIN_LOSS_PCT         = 0.40  # ≥ 40% of stop consumed → swap candidate
SWAP_MAX_STOP_PIPS        = 30.0  # ≤ 30 pips from stop → swap candidate
SWAP_MAX_DAYS_NO_PROG     = 4     # days without progress → swap candidate
SWAP_KEEP_SCORE_THRESHOLD = 12.0  # score ≤ this (out of 30) → swap candidate


def _calculate_keep_score(
    trade: dict,
    current_price: float,
    log_fn=None,
) -> dict:
    """Score an open trade 0-30 on how worth keeping it is.

    Components:
      Confidence at entry : 0-10
      Progress toward T1  : 0-10
      Time freshness      : 0-5  (decays over 5 days)
      P&L health          : 0-5  (0 = near stop, 5 = in profit)
      Cascade bonus       : +5 T1 hit, +8 T2 hit

    Returns dict: {score, swap_candidate, reasons, progress_pct, days_open}
    """
    from datetime import datetime, timezone as _tz
    _log = log_fn or print

    pair      = str(trade.get("pair", ""))
    direction = str(trade.get("direction", "")).upper()
    entry     = float(trade.get("entry", 0) or 0)
    stop      = float(trade.get("stop_loss", 0) or 0)
    eff_stop  = float(trade.get("effective_stop", 0) or stop)
    t1        = float(trade.get("t1_price", 0) or trade.get("target", 0) or 0)
    conf      = float(trade.get("confidence", 0) or 0)
    t1_hit    = str(trade.get("t1_hit", "")).upper() in ("TRUE", "YES", "1", "T")
    t2_hit    = str(trade.get("t2_hit", "")).upper() in ("TRUE", "YES", "1", "T")
    ps        = 0.01 if "JPY" in pair else 0.0001

    # Component 1: Confidence (0-10)
    conf_score = min(10.0, max(0.0, float(conf)))

    # Component 2: Progress toward T1 (-100 to +100 → maps to 0-10)
    if entry > 0 and t1 > 0 and current_price > 0:
        total_range = abs(t1 - entry)
        if total_range > 0:
            raw_prog = ((current_price - entry) / total_range * 100.0
                        if direction == "BUY"
                        else (entry - current_price) / total_range * 100.0)
        else:
            raw_prog = 0.0
        progress = max(-100.0, min(100.0, raw_prog))
        prog_score = max(0.0, min(10.0, (progress + 100.0) / 20.0))
    else:
        progress  = 0.0
        prog_score = 5.0

    # Component 3: Freshness (5 → 0 over 5 days)
    try:
        ts_raw = str(trade.get("timestamp", "") or "")
        if ts_raw and ts_raw not in ("nan", "None", ""):
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_tz.utc)
            days_open = (datetime.now(_tz.utc) - ts).total_seconds() / 86400.0
        else:
            days_open = 0.0
    except Exception:
        days_open = 0.0
    time_score = max(0.0, min(5.0, 5.0 - days_open))

    # Component 4: P&L health (0 = at stop, 5 = in profit)
    if entry > 0 and eff_stop > 0:
        stop_dist = abs(entry - eff_stop)
        if direction == "BUY":
            current_loss = entry - current_price
        else:
            current_loss = current_price - entry
        if current_loss <= 0:
            pnl_score = 5.0
        else:
            loss_ratio = current_loss / stop_dist if stop_dist > 0 else 1.0
            pnl_score  = max(0.0, 5.0 * (1.0 - loss_ratio))
    else:
        pnl_score = 2.5

    # Cascade bonus
    cascade_bonus = 8.0 if t2_hit else (5.0 if t1_hit else 0.0)

    score = conf_score + prog_score + time_score + pnl_score + cascade_bonus

    # Swap candidate reasons
    reasons = []
    if eff_stop > 0 and current_price > 0:
        stop_pips = abs(current_price - eff_stop) / ps
        if stop_pips <= SWAP_MAX_STOP_PIPS:
            reasons.append(f"{stop_pips:.0f}p from stop")
    if entry > 0 and eff_stop > 0 and current_price > 0:
        stop_dist = abs(entry - eff_stop)
        cur_loss  = (entry - current_price if direction == "BUY" else current_price - entry)
        if cur_loss > 0 and stop_dist > 0:
            loss_pct = cur_loss / stop_dist
            if loss_pct >= SWAP_MIN_LOSS_PCT:
                reasons.append(f"{loss_pct * 100:.0f}% risk consumed")
    if conf <= SWAP_MAX_EXISTING_CONF:
        reasons.append(f"conf {conf:.1f} low")
    if days_open >= SWAP_MAX_DAYS_NO_PROG and progress < 20 and not t1_hit:
        reasons.append(f"{days_open:.1f}d no progress")

    swap_candidate = (score <= SWAP_KEEP_SCORE_THRESHOLD and not t1_hit and not t2_hit)

    _log(f"[swap] {pair} keep={score:.1f}/30 candidate={swap_candidate}"
         f" ({', '.join(reasons) if reasons else 'solid'})")

    return {
        "score":          score,
        "swap_candidate": swap_candidate,
        "reasons":        reasons,
        "progress_pct":   progress,
        "days_open":      days_open,
    }


def _find_swap_target(
    new_pair: str,
    new_direction: str,
    new_conf: float,
    open_trades: list,
    log_fn=None,
) -> dict:
    """Check if a new high-confidence setup justifies replacing an open trade.

    Returns dict: {should_swap, target_id, target_pair, target_score, reason, ...}
    """
    _log = log_fn or print

    if new_conf < SWAP_MIN_NEW_CONF:
        return {"should_swap": False, "reason": f"conf {new_conf:.1f} < {SWAP_MIN_NEW_CONF}"}
    if not open_trades:
        return {"should_swap": False, "reason": "no open trades to evaluate"}

    try:
        from src.trading.financials import load_prices as _lp_fst, get_price as _gp_fst
        _prices = _lp_fst()
    except Exception as _pe:
        _log(f"[swap] price load error: {_pe}")
        _prices = {}

    scored = []
    for t in open_trades:
        pair    = str(t.get("pair", ""))
        cp      = _gp_fst(_prices, pair) if _prices else None
        cp_val  = float(cp) if cp else float(t.get("entry", 0) or 0)
        result  = _calculate_keep_score(trade=t, current_price=cp_val, log_fn=_log)
        scored.append({
            "id":            t.get("id"),
            "pair":          pair,
            "conf":          float(t.get("confidence", 0) or 0),
            "score":         result["score"],
            "swap_candidate": result["swap_candidate"],
            "reasons":       result["reasons"],
            "t1_hit":        str(t.get("t1_hit", "")).upper() in ("TRUE", "YES", "1", "T"),
        })

    scored.sort(key=lambda x: x["score"])
    weakest = scored[0]

    _log(f"[swap] Weakest: {weakest['pair']} score={weakest['score']:.1f} candidate={weakest['swap_candidate']}")

    if not weakest["swap_candidate"]:
        return {
            "should_swap": False,
            "reason": f"{weakest['pair']} score {weakest['score']:.1f} above threshold — no swap",
        }
    if weakest["t1_hit"]:
        return {"should_swap": False, "reason": f"{weakest['pair']} T1 hit — protected"}

    conf_gap = new_conf - weakest["conf"]
    if conf_gap < SWAP_MIN_CONF_GAP:
        return {
            "should_swap": False,
            "reason": f"conf gap {conf_gap:.1f} < {SWAP_MIN_CONF_GAP} required",
        }

    reason_str = (
        f"Replacing {weakest['pair']} (score {weakest['score']:.1f}, conf {weakest['conf']:.1f}"
        + (f", {', '.join(weakest['reasons'])}" if weakest["reasons"] else "")
        + f") with {new_pair} (conf {new_conf:.1f})"
    )
    _log(f"[swap] APPROVED: {reason_str}")

    return {
        "should_swap":    True,
        "target_id":      weakest["id"],
        "target_pair":    weakest["pair"],
        "target_conf":    weakest["conf"],
        "target_score":   weakest["score"],
        "target_reasons": weakest["reasons"],
        "conf_gap":       conf_gap,
        "reason":         reason_str,
    }


def _check_capacity_tiered(
    pair: str,
    confidence: float,
    regime: str,
    fund_state: dict,
    log_fn=None,
) -> dict:
    """Tiered capacity check. Returns {allowed, is_override, risk_pct, open_count, reason, tier}.

    Normal (open < 4)   : allowed, risk_pct=None (sizing computed elsewhere).
    Override (open == 4): conf≥7.5, daily>-2%, unrealised>-3%, not RANGING
                          → allowed at STRONG=0.75% or ELITE=0.50% (conf≥8.5).
    Blocked (open ≥ 5)  : never open a 6th slot.
    """
    _log = log_fn or print
    _oc  = _get_open_fund_count()

    if _oc < MAX_FUND_TRADES_NORMAL:
        return {
            "allowed":     True,
            "is_override": False,
            "risk_pct":    None,
            "open_count":  _oc,
            "reason":      f"{_oc}/{MAX_FUND_TRADES_NORMAL} slots used",
            "tier":        "NORMAL",
        }

    if _oc >= MAX_FUND_TRADES_OVERRIDE:
        _rsn = f"Fund at hard cap — {_oc}/{MAX_FUND_TRADES_OVERRIDE} open trades"
        _log(f"[capacity] BLOCKED {pair} — {_rsn}")
        return {
            "allowed":     False,
            "is_override": False,
            "risk_pct":    None,
            "open_count":  _oc,
            "reason":      _rsn,
            "tier":        "BLOCKED",
        }

    # open_count == MAX_FUND_TRADES_NORMAL → evaluate 5th-slot override eligibility
    _daily_pnl  = float(fund_state.get("daily_pnl_pct") or 0.0)
    _unrealised = float(fund_state.get("unrealised_pnl_pct") or 0.0)
    _is_ranging = "RANGING" in str(regime or "").upper()

    _blocked_reasons = []
    if confidence < OVERRIDE_MIN_CONF:
        _blocked_reasons.append(
            f"conf {confidence:.1f} < {OVERRIDE_MIN_CONF} override min"
        )
    if _daily_pnl <= OVERRIDE_MAX_DAILY_LOSS:
        _blocked_reasons.append(
            f"daily P&L {_daily_pnl:.2f}% ≤ {OVERRIDE_MAX_DAILY_LOSS}% limit"
        )
    if _unrealised <= OVERRIDE_MAX_UNREALISED:
        _blocked_reasons.append(
            f"unrealised {_unrealised:.2f}% ≤ {OVERRIDE_MAX_UNREALISED}% limit"
        )
    if _is_ranging:
        _blocked_reasons.append(f"regime {regime!r} is RANGING — no override")

    if _blocked_reasons:
        _rsn = (
            f"Fund at {_oc}/{MAX_FUND_TRADES_NORMAL} + override blocked: "
            + "; ".join(_blocked_reasons)
        )
        _log(f"[capacity] BLOCKED {pair} (override) — " + "; ".join(_blocked_reasons))
        return {
            "allowed":     False,
            "is_override": False,
            "risk_pct":    None,
            "open_count":  _oc,
            "reason":      _rsn,
            "tier":        "BLOCKED",
        }

    # Override approved
    if confidence >= 8.5:
        _tier = "ELITE"
        _risk = OVERRIDE_RISK_ELITE
    else:
        _tier = "STRONG"
        _risk = OVERRIDE_RISK_STRONG

    _rsn = (
        f"5th-slot override: conf={confidence:.1f} tier={_tier} "
        f"risk={_risk}% daily={_daily_pnl:.2f}%"
    )
    _log(
        f"[capacity] ⚡ OVERRIDE APPROVED {pair} — {_tier} "
        f"conf={confidence:.1f} → {_risk}% risk"
    )
    return {
        "allowed":     True,
        "is_override": True,
        "risk_pct":    _risk,
        "open_count":  _oc,
        "reason":      _rsn,
        "tier":        _tier,
    }


def _pip_size(pair: str) -> float:
    """Return the pip size for any forex pair based on the QUOTE currency.

    Comprehensive rule covering all 130+ eligible pairs:
      quote JPY  → 0.01     e.g. USD/JPY, EUR/JPY, AUD/JPY, NOK/JPY, HKD/JPY
      base  JPY  → 0.000001 e.g. JPY/USD, JPY/AUD  (rare inverted pairs)
      else  → 0.0001        ALL other pairs including:
                            standard:  EUR/USD, GBP/USD, USD/CAD, AUD/USD …
                            HKD quote: USD/HKD (≈7.8), CAD/HKD (≈5.6), AUD/HKD …
                            SGD quote: USD/SGD (≈1.34), EUR/SGD, AUD/SGD …
                            NOK quote: USD/NOK (≈9.5), EUR/NOK (≈11), GBP/NOK …
                            SEK quote: USD/SEK (≈10.5), EUR/SEK (≈12), GBP/SEK …
                            DKK/MXN/ZAR/TRY quote: all use 0.0001
    Note: HKD/SGD/NOK/SEK pairs all use 4-decimal quoting (0.0001 per pip)
    regardless of their price level — only JPY pairs use 2-decimal quoting.
    """
    cleaned = pair.upper().replace("/", "").replace("-", "")
    if len(cleaned) >= 6:
        base  = cleaned[:3]
        quote = cleaned[3:6]
        if quote == "JPY":
            return 0.01
        if base == "JPY":
            return 0.000001
    elif "JPY" in pair.upper():
        return 0.01
    return 0.0001


def _fmt_pips_between(pair: str, price_a, price_b) -> str:
    try:
        pips = abs(float(price_a) - float(price_b)) / _pip_size(pair)
        return f"{pips:.0f} pips"
    except (TypeError, ValueError, ZeroDivisionError):
        return "—"


def _parse_entry_type(
    analysis_text: str,
    current_price: float,
    direction: str,
    log_fn=None,
) -> dict:
    """Extract entry type and trigger from AI analysis output.

    Returns dict with entry_type, trigger_price, trigger_direction,
    trigger_reason, expiry_hours.
    """
    import re as _re_et
    _log = log_fn or print

    result = {
        "entry_type":        "IMMEDIATE",
        "trigger_price":     current_price,
        "trigger_direction": None,
        "trigger_reason":    "Immediate entry at current price",
        "expiry_hours":      0,
    }

    text = str(analysis_text)
    text_lower = text.lower()

    # Structured field extraction (from AI output)
    et_match = _re_et.search(r"ENTRY_TYPE\s*:\s*(\w+)", text, _re_et.I)
    if et_match:
        result["entry_type"] = et_match.group(1).upper()

    tp_match = _re_et.search(
        r"ENTRY_TRIGGER_PRICE\s*:\s*([0-9]+\.?[0-9]*)", text, _re_et.I
    )
    if tp_match:
        try:
            result["trigger_price"] = float(tp_match.group(1))
        except (ValueError, TypeError):
            pass

    tr_match = _re_et.search(
        r"ENTRY_TRIGGER_REASON\s*:\s*(.+?)(?:\n|$)", text, _re_et.I
    )
    if tr_match:
        result["trigger_reason"] = tr_match.group(1).strip()

    exp_match = _re_et.search(
        r"ENTRY_TRIGGER_EXPIRY_HOURS\s*:\s*([0-9]+)", text, _re_et.I
    )
    if exp_match:
        try:
            result["expiry_hours"] = int(exp_match.group(1))
        except (ValueError, TypeError):
            pass

    # Keyword detection fallback if ENTRY_TYPE not structured
    if result["entry_type"] == "IMMEDIATE":
        if any(x in text_lower for x in [
            "break above", "breaks above", "breakout above",
            "neckline", "resistance break",
        ]):
            if direction == "BUY":
                result["entry_type"] = "BREAKOUT_BUY"
                result["expiry_hours"] = result["expiry_hours"] or 48

        if any(x in text_lower for x in [
            "break below", "breaks below", "breakout below", "support break",
        ]):
            if direction == "SELL":
                result["entry_type"] = "BREAKOUT_SELL"
                result["expiry_hours"] = result["expiry_hours"] or 48

        if any(x in text_lower for x in [
            "pullback", "pull back", "retest", "wait for",
            "on a dip", "better entry",
        ]):
            result["entry_type"] = "PULLBACK"
            result["expiry_hours"] = result["expiry_hours"] or 48

    # Set trigger direction from entry type
    et = result["entry_type"]
    if et in ("BREAKOUT_BUY", "LIMIT_BUY") and direction == "BUY":
        result["trigger_direction"] = "ABOVE"
    elif et in ("BREAKOUT_SELL", "LIMIT_SELL") and direction == "SELL":
        result["trigger_direction"] = "BELOW"
    elif et == "PULLBACK":
        result["trigger_direction"] = "BELOW" if direction == "BUY" else "ABOVE"

    _log(
        f"[entry] {direction} entry_type={result['entry_type']} "
        f"trigger={result['trigger_price']} expiry={result['expiry_hours']}h"
    )
    return result


def _validate_entry_prices(
    yes_trades: list,
    current_prices: dict,
    max_adverse_pips: float = 30.0,
    log_fn=None,
) -> list:
    """Re-check prices after the full scan and validate IMMEDIATE entries.

    PENDING entries skip validation — they wait for their trigger.
    Returns filtered list of validated trades.
    """
    from src.trading.financials import get_price as _get_price_vep
    _log = log_fn or print
    validated = []

    for trade in yes_trades:
        pair       = str(trade.get("pair", ""))
        direction  = str(trade.get("direction") or
                         (trade.get("parsed") or {}).get("direction") or "").upper()
        parsed     = trade.get("parsed") or {}
        signal_price = float(parsed.get("entry") or trade.get("entry") or 0)
        entry_type = str(
            parsed.get("entry_type") or trade.get("entry_type") or "IMMEDIATE"
        ).upper()
        stop = float(parsed.get("stop_loss") or trade.get("stop_loss") or 0)

        if entry_type != "IMMEDIATE":
            validated.append(trade)
            _log(f"[validate] {pair} PENDING ({entry_type}) — no price check needed")
            continue

        current = _get_price_vep(current_prices, pair)
        if not current:
            validated.append(trade)
            _log(f"[validate] {pair} no current price — allowing")
            continue

        ps = 0.01 if "JPY" in pair else 0.0001

        if stop > 0:
            if direction == "BUY" and current <= stop:
                _log(f"[validate] {pair} BUY CANCELLED — price {current} at/below stop {stop}")
                continue
            if direction == "SELL" and current >= stop:
                _log(f"[validate] {pair} SELL CANCELLED — price {current} at/above stop {stop}")
                continue

        if signal_price > 0:
            if direction == "BUY":
                pips_moved = (current - signal_price) / ps
            else:
                pips_moved = (signal_price - current) / ps

            if pips_moved < -max_adverse_pips:
                _log(
                    f"[validate] {pair} CANCELLED — price moved "
                    f"{pips_moved:.1f}p against trade since signal"
                )
                continue

            if pips_moved > 50:
                _log(f"[validate] {pair} — moved {pips_moved:.1f}p with trade — converting to PULLBACK")
                from datetime import datetime, timezone, timedelta
                trade = dict(trade)
                trade["entry_type"] = "PULLBACK"
                trade.setdefault("parsed", {})["entry_type"] = "PULLBACK"
                expiry = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
                trade["entry_trigger_expiry"] = expiry
                validated.append(trade)
                continue
            else:
                _log(f"[validate] {pair} CONFIRMED — signal {signal_price:.5f} current {current:.5f} ({pips_moved:+.1f}p)")

        validated.append(trade)

    _log(f"[validate] {len(validated)}/{len(yes_trades)} trades validated")
    return validated


def _analyse_pair(pair: str, log, force_deep: bool = False,
                  shared_fundamental=None, shared_macro=None,
                  sonnet_threshold: int = 6,
                  pair_threshold_override=None) -> dict | None:
    try:
        return service.analyse_and_log(
            pair,
            log=lambda m: _log_line(log, m),
            force_deep=force_deep,
            shared_fundamental=shared_fundamental,
            shared_macro=shared_macro,
            sonnet_threshold=sonnet_threshold,
            pair_threshold_override=pair_threshold_override,
        )
    except Exception as exc:
        _log_line(log, f"FAILED {pair}: {exc}")
        traceback.print_exc(file=log)
        return None


# ── Currency consensus post-processing ────────────────────────────────────────

def _apply_currency_consensus(deep_results: list, log) -> int:
    """Adjust each result's confidence by ±1 based on cross-pair currency consensus.

    After all pairs are analysed, we aggregate directional signals across results
    to compute a net bullish/bearish score for each currency. A pair whose direction
    aligns with the consensus (e.g. BUY EUR/USD when EUR is broadly bullish and USD
    broadly bearish across other pairs) gets +1 confidence. A pair that contradicts
    the consensus gets -1.

    Rules:
    - Requires ≥4 deep results with known direction; otherwise skips (no consensus).
    - Each result contributes weight = (confidence - 4) to the currency score, so
      low-confidence results have near-zero influence and high-confidence ones drive
      the consensus. Results with confidence < 5 are excluded from voting.
    - A pair's own signal is excluded from the consensus used to judge it (no self-reinforcement).
    - Consensus is only decisive when net_support > 4 (+1) or < -4 (-1); anything
      in between is noise and produces no adjustment.
    - Final confidence is clamped to [1, 10].
    - TRADE_THIS is NOT modified — consensus only shifts watch-list ranking and
      display. A pair already below threshold can surface higher; a trade already
      approved is not revoked.

    Returns the count of results adjusted.
    """
    # Need at least 4 results to form a meaningful consensus
    eligible = [
        r for r in deep_results
        if (_conf(r) or 0) >= 5
        and (r.get("parsed") or {}).get("direction") in ("BUY", "SELL")
        and r.get("pair")
    ]
    if len(eligible) < 4:
        return 0

    # Build global currency score map (all eligible results combined)
    def _pair_ccys(pair: str):
        clean = pair.upper().replace("/", "").replace("-", "")
        if len(clean) < 6:
            return None, None
        return clean[:3], clean[3:6]

    global_score: dict = {}
    for r in eligible:
        base, quote = _pair_ccys(r["pair"])
        if not base or not quote:
            continue
        pp   = r["parsed"]
        conf = _conf(r) or 5
        w    = float(conf - 4)
        if pp["direction"] == "BUY":
            global_score[base]  = global_score.get(base,  0.0) + w
            global_score[quote] = global_score.get(quote, 0.0) - w
        else:  # SELL
            global_score[base]  = global_score.get(base,  0.0) - w
            global_score[quote] = global_score.get(quote, 0.0) + w

    # Currencies with only one pair in the universe give trivial "consensus" —
    # require a currency appears in at least 2 eligible results before we trust it.
    ccy_pair_count: dict = {}
    for r in eligible:
        base, quote = _pair_ccys(r["pair"])
        for c in (base, quote):
            if c:
                ccy_pair_count[c] = ccy_pair_count.get(c, 0) + 1

    adjustments = 0
    for r in deep_results:
        pp = r.get("parsed")
        if not pp:
            continue
        direction = (pp.get("direction") or "").upper()
        if direction not in ("BUY", "SELL"):
            continue
        base, quote = _pair_ccys(r.get("pair", ""))
        if not base or not quote:
            continue

        # Build leave-one-out score (exclude this result's own contribution)
        own_conf = _conf(r) or 5
        own_w    = float(own_conf - 4) if own_conf >= 5 else 0.0
        if direction == "BUY":
            base_excl  = global_score.get(base,  0.0) - own_w
            quote_excl = global_score.get(quote, 0.0) + own_w
        else:
            base_excl  = global_score.get(base,  0.0) + own_w
            quote_excl = global_score.get(quote, 0.0) - own_w

        # Skip if either currency has too few peers to form a real consensus
        if ccy_pair_count.get(base, 0) < 2 or ccy_pair_count.get(quote, 0) < 2:
            continue

        # Net support: positive = consensus agrees with this trade direction
        # BUY BASE/QUOTE: base should be bullish (base_excl > 0) and quote bearish (quote_excl < 0)
        if direction == "BUY":
            net_support = base_excl + (-quote_excl)
        else:  # SELL BASE/QUOTE
            net_support = (-base_excl) + quote_excl

        if net_support > 4:
            adj = +1
        elif net_support < -4:
            adj = -1
        else:
            continue

        old_conf = _conf(r) or 5           # _conf() always returns int; handles str/None safely
        new_conf = max(1, min(10, old_conf + adj))
        if new_conf == old_conf:
            continue

        pp["confidence"] = new_conf
        pp["consensus_adj"] = adj
        adjustments += 1
        direction_str = "agrees" if adj > 0 else "contradicts"
        _log_line(log,
            f"[CONSENSUS] {r['pair']} {direction}: cross-pair {direction_str} "
            f"(base={base_excl:+.1f}, quote={quote_excl:+.1f}, net={net_support:+.1f}) "
            f"→ conf {old_conf}→{new_conf}"
        )

    if adjustments:
        _log_line(log, f"[CONSENSUS] {adjustments} pair(s) confidence adjusted by cross-pair currency consensus")
    else:
        _log_line(log, "[CONSENSUS] No consensus adjustments (signals balanced or insufficient peer data)")
    return adjustments


# ── Telegram context helpers ───────────────────────────────────────────────────

def _derive_market_context(deep_results: list, risk_data: dict) -> dict:
    ctx: dict = {
        "risk_env":      "⚖️ neutral",
        "vix":           None,
        "vix_trend":     None,
        "yield_curve":   None,
        "oil":           None,
        "strongest_ccy": None,
        "weakest_ccy":   None,
        "ccy_scores":    {},
    }
    for r in deep_results:
        try:
            signals  = r["bundle"]["macro"]["signals"]
            vix_data = signals.get("VIX (volatility index)", {})
            if isinstance(vix_data, dict) and vix_data.get("value") is not None:
                ctx["vix"]       = float(vix_data["value"])
                ctx["vix_trend"] = vix_data.get("trend", "unknown")
            oil_data = signals.get("WTI crude oil ($/bbl)", {})
            if isinstance(oil_data, dict) and oil_data.get("value") is not None:
                ctx["oil"] = float(oil_data["value"])
            crv_data = signals.get("US 2s10s curve (10Y-2Y, %)", {})
            if isinstance(crv_data, dict) and crv_data.get("value") is not None:
                ctx["yield_curve"] = float(crv_data["value"])
            if ctx["vix"] is not None:
                break
        except (KeyError, TypeError, ValueError):
            continue

    if ctx["vix"] is not None:
        if ctx["vix"] > 25:
            ctx["risk_env"] = "⚠️ risk-off"
        elif ctx["vix"] < 15:
            ctx["risk_env"] = "🟢 risk-on"
        else:
            ctx["risk_env"] = "⚖️ neutral"
    if ctx["yield_curve"] is not None and ctx["yield_curve"] < 0 and "risk-off" not in ctx["risk_env"]:
        ctx["risk_env"] += " (yield curve inverted)"

    ccy_score: dict[str, float] = {}
    for r in deep_results:
        p    = r["parsed"]
        conf = _conf(r)
        if conf < 4:
            continue
        pair  = r["pair"]
        clean = pair.upper().replace("/", "").replace("-", "")
        if len(clean) < 6:
            continue
        base, quote = clean[:3], clean[3:6]
        direction   = (p.get("direction") or "").upper()
        weight      = float(conf - 3)
        if direction == "BUY":
            ccy_score[base]  = ccy_score.get(base,  0.0) + weight
            ccy_score[quote] = ccy_score.get(quote, 0.0) - weight
        elif direction == "SELL":
            ccy_score[base]  = ccy_score.get(base,  0.0) - weight
            ccy_score[quote] = ccy_score.get(quote, 0.0) + weight

    if ccy_score:
        ctx["ccy_scores"]    = ccy_score
        ctx["strongest_ccy"] = max(ccy_score, key=lambda c: ccy_score[c])
        ctx["weakest_ccy"]   = min(ccy_score, key=lambda c: ccy_score[c])

    # Monthly structural bias — informational background context
    monthly_counts: dict = {"BUY": 0, "SELL": 0}
    for r in deep_results:
        mtf = r.get("bundle", {}).get("mtf", {})
        if isinstance(mtf, dict):
            bd = mtf.get("breakdown", "")
            for part in bd.split():
                if ":" in part:
                    tf, sig = part.split(":", 1)
                    if tf == "M" and sig in ("BUY", "SELL"):
                        monthly_counts[sig] += 1
    total_m = monthly_counts["BUY"] + monthly_counts["SELL"]
    if total_m > 0:
        buy_m, sell_m = monthly_counts["BUY"], monthly_counts["SELL"]
        if sell_m > buy_m:
            ctx["monthly_bias"] = f"SELL ({sell_m}/{total_m} pairs bearish)"
        elif buy_m > sell_m:
            ctx["monthly_bias"] = f"BUY ({buy_m}/{total_m} pairs bullish)"
        else:
            ctx["monthly_bias"] = f"MIXED ({buy_m} bullish / {sell_m} bearish)"

    # MTF averages across all deep-analysed pairs
    mtf_scores, mtf_qualifies = [], []
    for r in deep_results:
        mtf = r.get("bundle", {}).get("mtf", {})
        if isinstance(mtf, dict) and mtf.get("breakdown", "UNAVAILABLE") != "UNAVAILABLE":
            ws = mtf.get("weighted_score")
            if ws is not None:
                mtf_scores.append(float(ws))
            mtf_qualifies.append(bool(mtf.get("qualifies", False)))
    ctx["avg_mtf_score"]   = (sum(mtf_scores) / len(mtf_scores)) if mtf_scores else None
    ctx["qualify_pct"]     = (sum(mtf_qualifies) / len(mtf_qualifies)) if mtf_qualifies else None

    # High-impact event count for the week (cached 6h in selector)
    try:
        hi_count, hi_notable = selector.count_weekly_high_impact_events()
    except Exception:
        hi_count, hi_notable = 0, []
    ctx["high_impact_count"]   = hi_count
    ctx["high_impact_notable"] = hi_notable

    return ctx


def _compute_patience_score(ctx: dict) -> dict:
    """Rate today's trading conditions 1-10 from four factors.

    The regime detector is the single source of truth: its conditions_cap
    sets the hard maximum before the 4-factor calculation even runs.
    This permanently prevents contradictions like "ranging market — 8/10
    strong trend clarity".

    VIX (0-3): calm market scores high, fear index > 25 scores 0.
    News (0-3): more high-impact events this week = lower score.
    MTF agreement (0-2): high avg weighted_score = cleaner trends.
    Trend clarity (0-2): % of pairs that pass weekly+daily gate.

    Returns dict with 'score' (int 1-10), 'description' (str), and
    individual component scores for debugging.
    """
    # ── Step 1: detect regime and get the hard conditions cap ─────────────
    _regime_data_ps = {}
    _conditions_cap = 10
    _regime_key_ps  = ""
    try:
        from src import market_regime as _mr_ps
        _regime_data_ps = _mr_ps.detect()
        _regime_key_ps  = (_regime_data_ps.get("regime") or "").lower()
        _conditions_cap = int(_regime_data_ps.get("conditions_cap", 10) or 10)
    except Exception:
        pass

    # ── Step 2: four-factor raw score ─────────────────────────────────────
    # VIX component (0-3)
    vix = ctx.get("vix")
    if vix is None:
        vix_pts = 1.5
    elif vix <= 13:
        vix_pts = 3.0
    elif vix <= 17:
        vix_pts = 2.5
    elif vix <= 20:
        vix_pts = 2.0
    elif vix <= 25:
        vix_pts = 1.0
    else:
        vix_pts = 0.0

    # News component (0-3)
    hi = ctx.get("high_impact_count", 0) or 0
    if hi == 0:
        news_pts = 3.0
    elif hi == 1:
        news_pts = 2.0
    elif hi <= 3:
        news_pts = 1.0
    else:
        news_pts = 0.0

    # MTF average weighted score (0-2)
    avg_mtf = ctx.get("avg_mtf_score")
    if avg_mtf is None:
        mtf_pts = 0.0
    elif avg_mtf >= 0.70:
        mtf_pts = 2.0
    elif avg_mtf >= 0.45:
        mtf_pts = 1.0
    else:
        mtf_pts = 0.0

    # Trend clarity: % of pairs with qualifying weekly+daily alignment (0-2)
    qpct = ctx.get("qualify_pct")
    if qpct is None:
        trend_pts = 0.0
    elif qpct >= 0.50:
        trend_pts = 2.0
    elif qpct >= 0.25:
        trend_pts = 1.0
    else:
        trend_pts = 0.0

    raw = vix_pts + news_pts + mtf_pts + trend_pts  # 0–10

    # ── Step 3: apply regime cap FIRST — this is the single source of truth
    score = max(1, min(_conditions_cap, round(raw)))

    # ── Step 4: build description driven by regime (no contradictions) ────
    notable = ctx.get("high_impact_notable") or []

    # High-vol ranging: hard override — all description comes from regime
    if "ranging" in _regime_key_ps and "high" in _regime_key_ps:
        description = (
            "high volatility ranging market — chaotic conditions — "
            "position sizes halved and only highest conviction trades recommended"
        )
        return {
            "score": score, "raw": round(raw, 1),
            "vix_pts": vix_pts, "news_pts": news_pts,
            "mtf_pts": mtf_pts, "trend_pts": trend_pts,
            "description": description,
        }

    parts = []
    if "ranging" in _regime_key_ps:
        # Ranging: regime-driven primary description — suppress trend clarity contradiction
        parts = ["ranging market — directionless conditions"]
    else:
        # Trending: let 4-factor detail drive secondary description
        if avg_mtf is not None and avg_mtf < 0.45:
            parts.append("choppy market")
        elif avg_mtf is not None and avg_mtf < 0.70:
            parts.append("moderate trend clarity")
        elif avg_mtf is not None:
            parts.append("strong trend clarity")

        if vix is not None and vix > 25:
            parts.append("VIX elevated")
        elif vix is not None and vix > 20:
            parts.append("VIX above average")

    if hi > 0:
        if notable:
            ev_label = notable[0]
            for kw, short in [("Non-Farm", "NFP"), ("Nonfarm", "NFP"),
                               ("Federal Reserve", "Fed rate decision"),
                               ("FOMC", "FOMC"), ("Consumer Price", "CPI")]:
                if kw.lower() in ev_label.lower():
                    ev_label = short
                    break
            parts.append(f"{ev_label} this week")
        else:
            parts.append(f"{hi} high-impact event{'s' if hi != 1 else ''} this week")

    if qpct is not None and qpct < 0.25 and "ranging" not in _regime_key_ps:
        parts.append("few pairs with clear directional bias")

    desc_body = ", ".join(parts)

    # Suffix is regime-first
    if "risk_off" in _regime_key_ps:
        if vix is not None and vix > 25:
            suffix = "elevated risk aversion — high caution recommended"
        else:
            suffix = "risk-off environment — safe haven pairs favoured — conditions selective"
    elif "ranging" in _regime_key_ps:
        suffix = "mean reversion setups preferred — avoid breakout chasing"
    elif "risk_on" in _regime_key_ps:
        if score >= 8:
            suffix = "ideal conditions — A/B setups in favoured risk-on pairs"
        elif score >= 6:
            suffix = "good conditions for risk-on setups — favour AUD, NZD, CAD"
        else:
            suffix = "risk-on environment — patience for clean entries"
    else:
        if score >= 8:
            suffix = "ideal conditions for A/B setups"
        elif score >= 6:
            suffix = "consider waiting for cleaner setups"
        elif score >= 4:
            suffix = "be selective — only A-grade setups"
        elif score >= 2:
            suffix = "strong recommendation to reduce size"
        else:
            suffix = "strong recommendation to stay in cash today"

    description = f"{desc_body} — {suffix}" if desc_body else suffix

    return {
        "score":     score,
        "raw":       round(raw, 1),
        "vix_pts":   vix_pts,
        "news_pts":  news_pts,
        "mtf_pts":   mtf_pts,
        "trend_pts": trend_pts,
        "description": description,
    }


def _score_breakdown_line(parsed: dict, smd: int = 0) -> str:
    def _s(key):
        v = parsed.get(key)
        return str(v) if v is not None else "—"
    smd_str = f"  SMD:{smd:+d}" if smd != 0 else "  SMD:0"
    return (
        f"T:{_s('technical_score')}  "
        f"F:{_s('fundamental_score')}  "
        f"S:{_s('sentiment_score')}  "
        f"P:{_s('positioning_score')}  "
        f"M:{_s('macro_score')}"
        f"{smd_str}"
    )


def _mtf_plain_english(mtf_data: dict, trade_direction: str = None) -> str:
    """Convert MTF data to a 3-TF swing-trading display for phone-readable output.

    Format: "Timeframes: Weekly SELL Daily SELL 4H SELL — all 3 aligned strongest signal"
    Or:     "Timeframes: Weekly SELL Daily SELL 4H BUY — 2 of 3 agree SELL valid setup"
    Monthly is excluded from the display (shown separately in market context).
    """
    if not isinstance(mtf_data, dict):
        return ""
    breakdown = mtf_data.get("breakdown", "")
    if not breakdown or breakdown == "UNAVAILABLE":
        return ""

    direction = (trade_direction or mtf_data.get("direction") or "NEUTRAL").upper()
    if direction == "NEUTRAL":
        return ""

    # Only display the 3 core swing-trading timeframes (skip monthly M)
    _TF_DISPLAY = {"W": "Weekly", "D": "Daily", "4H": "4H"}

    tf_signals: dict = {}
    for part in breakdown.split():
        if ":" in part:
            tf, sig = part.split(":", 1)
            if tf in _TF_DISPLAY:
                tf_signals[tf] = sig

    if not tf_signals:
        return ""

    parts   = []
    agreeing = 0
    for tf in ("W", "D", "4H"):
        if tf in tf_signals:
            sig = tf_signals[tf]
            parts.append(f"{_TF_DISPLAY[tf]} {sig}")
            if sig == direction:
                agreeing += 1

    tf_str = " ".join(parts)
    total  = len(parts)

    if agreeing == total == 3:
        return f"Timeframes: {tf_str} — all 3 aligned strongest signal"
    if agreeing >= 2:
        return f"Timeframes: {tf_str} — {agreeing} of {total} agree {direction} valid setup"
    if agreeing == 1:
        return f"Timeframes: {tf_str} — only 1 of {total} align {direction}"
    return f"Timeframes: {tf_str}"


def _weakest_layer_hint(parsed: dict) -> str:
    """Return one line identifying the weakest analysis layer and what would fix it."""
    _HINTS = {
        "Technical":   "needs stronger RSI, MACD or trend alignment",
        "Fundamental": "needs clearer rate differential or central bank divergence",
        "Sentiment":   "needs positive news tone shift for this currency",
        "Positioning": "needs more extreme COT positioning data",
        "Macro":       "needs risk-on environment or positive economic data",
    }
    score_keys = [
        ("Technical",   "technical_score"),
        ("Fundamental", "fundamental_score"),
        ("Sentiment",   "sentiment_score"),
        ("Positioning", "positioning_score"),
        ("Macro",       "macro_score"),
    ]
    valid = [(name, parsed.get(key)) for name, key in score_keys if parsed.get(key) is not None]
    if not valid:
        return ""
    name, score = min(valid, key=lambda x: x[1])
    return f"Weakest: {name} {score}/10 — {_HINTS[name]}"


def _ribbon_display(bundle: dict) -> str:
    """Return a one-line MA ribbon status string for Telegram display, or empty string."""
    rib = (bundle or {}).get("technical", {})
    if isinstance(rib, dict):
        rib = rib.get("daily", {})
    if not isinstance(rib, dict):
        return ""
    ribbon = rib.get("ribbon", {})
    if not isinstance(ribbon, dict):
        return ""
    status = ribbon.get("status", "")
    if not status or status in ("UNAVAILABLE", "NEUTRAL", ""):
        return ""
    _LABELS = {
        "ALIGNED_BULL":  "Aligned bull",
        "ALIGNED_BEAR":  "Aligned bear",
        "CONVERGING":    "Converging",
        "LEANING_BULL":  "Leaning bull",
        "LEANING_BEAR":  "Leaning bear",
    }
    label = _LABELS.get(status, status.replace("_", " ").title())
    detail = ""
    if ribbon.get("fanning"):
        detail = " — fanning out (trend accelerating)"
    elif ribbon.get("converging"):
        detail = " — spread tightening (trend weakening)"
    elif status == "CONVERGING":
        detail = " — potential trend change"
    cnt = ribbon.get("aligned_count", 0)
    return f"📊 MA Ribbon: {label} ({cnt}/5 aligned){detail}"


_GRADE_LABELS: dict = {
    "A": "TAKE IMMEDIATELY",
    "B": "TAKE IF NO A AVAILABLE",
    "C": "WATCH ONLY",
    "D": "AVOID",
    "F": "NEVER TRADE",
}
_GRADE_ICONS: dict = {"A": "🏆", "B": "✅", "C": "👀", "D": "⚠️", "F": "❌"}


def _trade_quality_grade(r: dict) -> dict:
    """Compute A–F trade quality grade.

    A — conf≥8, R:R>2.5, all 3 TFs, ATR-calibrated, Fib near, no news, ribbon aligned, divergence
    B — conf≥7, R:R≥2.0, weekly+daily agree, no severe negatives
    C — conf≥6, R:R≥1.5, no blocking conditions
    D — ribbon against direction, R:R<1.5, or TF conflict + low conf
    F — ribbon strongly against, weekly/daily direct conflict, or R:R<1.3
    """
    p         = r.get("parsed", {})
    bundle    = r.get("bundle", {})
    direction = (p.get("direction") or "").upper()

    try:
        conf_raw = int(p.get("confidence") or 0)
    except (TypeError, ValueError):
        conf_raw = 0

    # R:R from parsed price levels (use Claude's field as fallback)
    rr = 0.0
    try:
        e = float(p.get("entry")     or 0)
        s = float(p.get("stop_loss") or 0)
        t = float(p.get("target")    or 0)
        if e and s and t and abs(e - s) > 0:
            rr = abs(t - e) / abs(e - s)
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    if not rr:
        try:
            rr = float(p.get("reward_risk") or 0)
        except (TypeError, ValueError):
            rr = 0.0
    if not rr:
        try:
            _fb_e, _fb_s, _fb_t, _ = _calc_indicative_levels(
                r["pair"], p, r.get("bundle", {})
            )
            if _fb_e and _fb_s and _fb_t and abs(_fb_e - _fb_s) > 0:
                rr = abs(_fb_t - _fb_e) / abs(_fb_e - _fb_s)
        except Exception:
            pass

    if not rr and p.get("trade_this") == "YES":
        # Only log R:R=0 for YES trades — NO_TRADE pairs without levels are expected
        _atr_dbg = float(
            (r.get("bundle", {}).get("technical", {}).get("daily") or {}).get("atr14") or 0
        )
        print(
            f"[DEBUG R:R=0] {r.get('pair','?')} "
            f"entry={p.get('entry')} stop={p.get('stop_loss')} target={p.get('target')} "
            f"rr_field={p.get('reward_risk')} atr14={_atr_dbg:.5f} "
            f"— YES trade missing price levels, will use indicative levels"
        )

    # MTF signals
    mtf    = bundle.get("mtf", {})
    sigs   = mtf.get("signals", {})
    w_sig  = sigs.get("weekly",  "NEUTRAL")
    d_sig  = sigs.get("daily",   "NEUTRAL")
    h4_sig = sigs.get("h4",      "NEUTRAL")
    w_ok   = w_sig  == direction
    d_ok   = d_sig  == direction
    h4_ok  = h4_sig == direction
    w_d_agree    = w_ok and d_ok
    all3_agree   = w_ok and d_ok and h4_ok
    w_d_conflict = (w_sig  in ("BUY", "SELL") and d_sig in ("BUY", "SELL")
                    and w_sig != d_sig)

    # Ribbon
    _dt  = (bundle.get("technical", {}).get("daily") or {}) if bundle else {}
    _rib = (_dt.get("ribbon") or {}) if isinstance(_dt, dict) else {}
    rib_st = str(_rib.get("status") or "") if isinstance(_rib, dict) else ""

    rib_aligned = (
        (direction == "BUY"  and rib_st in ("ALIGNED_BULL", "LEANING_BULL")) or
        (direction == "SELL" and rib_st in ("ALIGNED_BEAR", "LEANING_BEAR"))
    )
    rib_strongly_against = (
        (direction == "BUY"  and rib_st == "ALIGNED_BEAR") or
        (direction == "SELL" and rib_st == "ALIGNED_BULL")
    )
    rib_against = (
        (direction == "BUY"  and rib_st in ("ALIGNED_BEAR", "LEANING_BEAR")) or
        (direction == "SELL" and rib_st in ("ALIGNED_BULL", "LEANING_BULL"))
    )

    # Effective confidence (ribbon ALIGNED penalty already in _eff_conf; replicate here)
    conf = conf_raw - (1 if rib_strongly_against else 0)
    conf = max(1, conf)

    # Divergence
    _div = _dt.get("divergence", {}) if isinstance(_dt, dict) else {}
    div_confirmed = (
        (direction == "BUY"  and bool(_div.get("bullish"))) or
        (direction == "SELL" and bool(_div.get("bearish")))
    )

    # Fibonacci near level
    _fib    = _dt.get("fibonacci", {}) if isinstance(_dt, dict) else {}
    fib_near = (isinstance(_fib, dict) and _fib.get("status") == "ok"
                and bool(_fib.get("near_levels")))

    # ATR calibration (stop ≈ 0.7–1.5× ATR, target ≈ 1.5–3.0× ATR)
    atr14   = float(_dt.get("atr14") or 0) if isinstance(_dt, dict) else 0
    atr_cal = False
    if atr14 > 0:
        try:
            e2 = float(p.get("entry")     or 0)
            s2 = float(p.get("stop_loss") or 0)
            t2 = float(p.get("target")    or 0)
            sd = abs(e2 - s2)
            td = abs(t2 - e2)
            if sd > 0 and td > 0:
                atr_cal = (0.7 <= sd / atr14 <= 1.5) and (1.5 <= td / atr14 <= 3.0)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # News warning (no major events)
    news_raw = (p.get("news_warning") or "").lower().strip()
    no_news  = (not news_raw or
                any(kw in news_raw for kw in ("none", "n/a", "no major", "no significant")))

    # Issue 3: compute fundamental alignment for grade floor
    _fa_grade = r.get("_fundamental_alignment")
    if _fa_grade is None:
        try:
            from src import fundamentals as _fund_grade
            _pair_grade = r.get("pair", "")
            if _pair_grade and "/" in _pair_grade and direction:
                _fb_g, _fq_g = _pair_grade.split("/")
                _fa_grade = _fund_grade.get_fundamental_alignment(_fb_g, _fq_g, direction)
                r["_fundamental_alignment"] = _fa_grade
        except Exception:
            pass
    _fa_grade = _fa_grade if isinstance(_fa_grade, dict) else {}
    _fa_aligned_g   = _fa_grade.get("aligned", 0)
    _fa_alignment_g = _fa_grade.get("alignment", "MIXED")
    _has_fund_tailwind = (_fa_alignment_g == "TAILWIND" and _fa_aligned_g >= 3)

    # Hard block: zero ATR means stop/target cannot be safely calibrated
    if atr14 == 0:
        return {
            "grade": "F",
            "reasons": ["ATR unavailable — cannot set safe stop distance"],
            "atr_zero": True,
        }

    # ── Grade F — never trade ─────────────────────────────────────────────
    if rib_strongly_against or w_d_conflict or rr < 1.5:
        grade = "F"
        # Issue 3 floor: 3/3 fundamental tailwind + conf>=6 cannot be Grade F
        if _has_fund_tailwind and conf >= 6:
            grade = "C"
    # ── Grade D — avoid ──────────────────────────────────────────────────
    elif rib_against or rr < 2.0 or (not w_d_agree and conf <= 6):
        grade = "D"
    # ── Grade A — take immediately ────────────────────────────────────────
    elif (conf >= 8 and rr > 2.5 and all3_agree and
          atr_cal and fib_near and no_news and rib_aligned and div_confirmed):
        grade = "A"
    # ── Grade B — take if no A ────────────────────────────────────────────
    elif conf >= 7 and rr >= 2.5 and w_d_agree:
        grade = "B"
    # ── Grade C — watch only ──────────────────────────────────────────────
    elif conf >= 6 and rr >= 2.0:
        grade = "C"
    # ── Default D ─────────────────────────────────────────────────────────
    else:
        grade = "D"

    return {
        "grade":        grade,
        "label":        _GRADE_LABELS[grade],
        "icon":         _GRADE_ICONS[grade],
        "conf":         conf,
        "rr":           round(rr, 2),
        "all3_agree":   all3_agree,
        "w_d_agree":    w_d_agree,
        "w_d_conflict": w_d_conflict,
        "atr_cal":      atr_cal,
        "fib_near":     fib_near,
        "no_news":      no_news,
        "rib_aligned":  rib_aligned,
        "rib_against":  rib_against,
        "div_confirmed": div_confirmed,
    }


def _grade_display_line(qg: dict) -> str:
    """One-line grade badge: icon, grade, label, key factor dots."""
    grade = qg["grade"]
    rr    = qg["rr"]
    conf  = qg["conf"]

    badges = [f"Conf {conf}", f"R:R {rr:.1f}"]

    if grade in ("A", "B"):
        if qg.get("all3_agree"):
            badges.append("✓ All 3 TFs")
        elif qg.get("w_d_agree"):
            badges.append("✓ W+D agree")
        else:
            badges.append("✗ TF conflict")
        if qg.get("rib_aligned"):
            badges.append("✓ Ribbon")
        elif qg.get("rib_against"):
            badges.append("✗ Ribbon")
        if grade == "A":
            if qg.get("div_confirmed"):
                badges.append("✓ Divergence")
            if qg.get("fib_near"):
                badges.append("✓ Fib")
            if not qg.get("no_news"):
                badges.append("⚠️ News risk")
            if qg.get("atr_cal"):
                badges.append("✓ ATR-calibrated")
    elif grade == "C":
        if qg.get("w_d_agree"):
            badges.append("W+D agree")
        else:
            badges.append("mixed TFs")
    elif grade in ("D", "F"):
        if qg.get("w_d_conflict"):
            badges.append("W/D conflict")
        if qg.get("rib_against"):
            badges.append("ribbon against")
        if rr < 2.5:
            badges.append("low R:R")

    return f"{qg['icon']} <b>Grade {grade} — {qg['label']}</b>  {' · '.join(badges)}"


def _what_needs_to_change(parsed: dict) -> str:
    scores = {
        "Technical":   parsed.get("technical_score"),
        "Fundamental": parsed.get("fundamental_score"),
        "Sentiment":   parsed.get("sentiment_score"),
        "Positioning": parsed.get("positioning_score"),
        "Macro":       parsed.get("macro_score"),
    }
    missing = [k for k, v in scores.items() if v is None]
    weak    = {k: v for k, v in scores.items() if v is not None and v < 7}

    parts = []
    if missing:
        parts.append(f"Restore {' + '.join(missing)} data")
    _DIMENSION_PLAIN = {
        "Technical":   "price charts need a clearer trend signal",
        "Fundamental": "interest rate advantage needs to strengthen",
        "Sentiment":   "news tone needs to become more positive",
        "Positioning": "institutional investors need to increase their position",
        "Macro":       "global economic conditions need to improve",
    }
    if weak:
        for name, _sc in sorted(weak.items(), key=lambda x: x[1])[:2]:
            parts.append(_DIMENSION_PLAIN.get(name, f"{name} needs to strengthen"))
    if not parts:
        parts.append("All layers close — wait for price to reach key level")
    return "; ".join(parts)


def _rejection_reason(result: dict) -> str:
    if result.get("screened_out"):
        s      = result.get("screen", {})
        score  = s.get("score", "?")
        reason = (s.get("reason") or "").strip()
        if not reason:
            reason = "Technical and fundamental below minimum threshold"
        return f"Stage 1 score {score}/5 — {reason}"

    parsed    = result["parsed"]
    conf      = parsed.get("confidence")
    direction = (parsed.get("direction") or "?").upper()

    # Priority 1: MTF gate — most common reason a high-confidence pair is blocked
    mtf = result.get("bundle", {}).get("mtf", {})
    if mtf and not mtf.get("qualifies", True):
        cnt = mtf.get("agreeing_count", 0)
        bkd = mtf.get("breakdown", "")
        bkd_str = f" [{bkd}]" if bkd and bkd != "UNAVAILABLE" else ""
        return f"Conf {conf}/10 {direction} — MTF: weekly+daily must both agree (got {cnt}/3 core TFs){bkd_str}"

    # Priority 2: R:R below minimum threshold
    try:
        from src import threshold_manager as _tm_rr
        rr_min = _tm_rr.get_min_rr()
        rr_actual = float(parsed.get("reward_risk") or 0)
        if rr_actual > 0 and rr_actual < rr_min:
            return f"Conf {conf}/10 {direction} — R:R {rr_actual:.2f}:1 below {rr_min:.1f}:1 minimum"
    except Exception:
        pass

    scores = {
        "Technical":   parsed.get("technical_score"),
        "Fundamental": parsed.get("fundamental_score"),
        "Sentiment":   parsed.get("sentiment_score"),
        "Positioning": parsed.get("positioning_score"),
        "Macro":       parsed.get("macro_score"),
    }
    missing = [k for k, v in scores.items() if v is None]
    below   = sorted(
        [(k, v) for k, v in scores.items() if v is not None and v < 7],
        key=lambda x: x[1],
    )

    parts = []
    for k, v in below[:2]:
        if v <= 4:
            parts.append(f"{k} weak ({v}/10)")
        else:
            parts.append(f"{k} at {v}/10 — needs one more confirmation")
    if missing:
        parts.append(f"{' + '.join(missing[:2])} data missing")
    if not parts:
        parts.append("all layers strong — analyst judged setup not clean enough to trade")

    return f"Conf {conf}/10 {direction} — {', '.join(parts)}"


def _get_mtf_alignment(result: dict, direction: str) -> str:
    try:
        daily = result["bundle"]["technical"]["daily"]
        h4    = result["bundle"]["technical"].get("h4") or result["bundle"]["technical"].get("4h") or {}

        def _trend(tech: dict) -> str | None:
            c = tech.get("close") or tech.get("last_close")
            s = tech.get("sma50")
            if c is None or s is None:
                return None
            return "bullish" if float(c) > float(s) else "bearish"

        d_trend  = _trend(daily)
        h_trend  = _trend(h4)
        expected = "bullish" if direction.upper() == "BUY" else "bearish"

        if d_trend and h_trend:
            if d_trend == h_trend == expected:
                return f"✅ D1+4H both {d_trend}"
            elif d_trend == h_trend:
                return f"⚠️ Both {d_trend} — contrary to {direction}"
            return f"⚠️ Mixed (D1: {d_trend}, 4H: {h_trend})"
        if d_trend:
            return f"D1: {d_trend}"
        return "—"
    except (KeyError, TypeError, ValueError):
        return "—"


def _weekly_performance_section(date: str) -> list:
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return []
    if dt.weekday() != 0:
        return []
    try:
        from src import tracker
        rows   = tracker.load()
        cutoff = (dt - timedelta(days=7)).strftime("%Y-%m-%d")
        recent = [
            r for r in rows
            if r.get("status") in ("WIN", "LOSS", "BREAKEVEN")
            and (r.get("closed_at") or r.get("timestamp", ""))[:10] >= cutoff
        ]
    except Exception:
        return []

    lines = ["", "━━━━━━━━━━━━━━━━━━━━━", "📅 <b>WEEKLY PERFORMANCE SUMMARY</b>"]
    if not recent:
        lines.append("No closed trades in the past 7 days.")
    else:
        wins  = [r for r in recent if r.get("status") == "WIN"]
        total = len(recent)
        wr    = len(wins) / total * 100 if total else 0

        def _r(row):
            try:
                return float(row.get("r_multiple") or 0)
            except (TypeError, ValueError):
                return 0.0

        total_r = sum(_r(r) for r in recent)
        best    = max(recent, key=_r) if recent else None
        worst   = min(recent, key=_r) if recent else None

        lines.append(
            f"7-day: <b>{len(wins)}W / {len(recent)-len(wins)}L</b>  "
            f"<b>{wr:.0f}%</b> wins  Net: <b>{total_r:+.2f}R</b>"
        )
        if best:
            lines.append(f"🏆 Best: #{best.get('id')} {best.get('pair')} — {_r(best):+.2f}R")
        if worst and worst is not best:
            lines.append(f"💔 Worst: #{worst.get('id')} {worst.get('pair')} — {_r(worst):+.2f}R")

    # ── Risk of Ruin + Kelly Criterion ─────────────────────────────────────────
    try:
        from src import risk_manager as _rm_ror
        from src import risk_of_ruin as _ror
        _ror_profile    = _rm_ror.load_profile()
        _ror_state      = _rm_ror.compute_risk_state(_ror_profile)
        _current_risk_f = _rm_ror.MODE_RISK[_ror_state["risk_mode"]] / 100.0
        lines += _ror.build_ror_section(_current_risk_f)
    except Exception as _ror_exc:
        lines.append(f"⚠️ Risk analysis unavailable ({_ror_exc})")

    return lines


def _build_compact_research_section() -> list:
    """2–4 line research summary for intraday scans (9am / 5pm / 11pm)."""
    try:
        from src import research_tracker as _rtrk_cpt
        _cpt_rows = _rtrk_cpt.load()
    except Exception:
        return []
    if not _cpt_rows:
        return []

    def _cf(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    _cpt_open  = [r for r in _cpt_rows if r.get("status") in ("OPEN", "NO_PRICE_LEVELS")]
    _cpt_cls   = [r for r in _cpt_rows if r.get("status") in ("WIN", "LOSS", "BREAKEVEN", "EXPIRED", "PARTIAL_WIN")]
    _cpt_wins  = [r for r in _cpt_cls if r.get("status") == "WIN"]
    _cpt_loss  = [r for r in _cpt_cls if r.get("status") == "LOSS"]
    _cpt_pw    = [r for r in _cpt_cls if r.get("status") == "PARTIAL_WIN"]
    _cpt_dec   = len(_cpt_wins) + len(_cpt_loss)
    _cpt_wr    = int(len(_cpt_wins) / _cpt_dec * 100) if _cpt_dec else 0

    _l2 = [f"Open: {len(_cpt_open)}", f"Closed: {len(_cpt_cls)}"]
    if _cpt_dec:
        _l2.append(f"Win rate: {_cpt_wr}% ({len(_cpt_wins)}W/{len(_cpt_loss)}L)")
    if _cpt_pw:
        _l2.append(f"{len(_cpt_pw)} partial wins")

    sec = [
        "", "━━━━━━━━━━━━━━━━━━━━━",
        "🔬 <b>RESEARCH TRADES</b>",
        " · ".join(_l2),
    ]

    # Best pairs by win rate (≥2 trades for statistical relevance)
    _cpt_ps: dict = {}
    for _cr in _cpt_cls:
        _cp = _cr.get("pair", "?")
        _cpt_ps.setdefault(_cp, {"wins": 0, "total": 0})
        _cpt_ps[_cp]["total"] += 1
        if _cr.get("status") in ("WIN", "PARTIAL_WIN"):
            _cpt_ps[_cp]["wins"] += 1
    _cpt_good = [(p, s) for p, s in _cpt_ps.items() if s["total"] >= 2]
    if _cpt_good:
        _cpt_sorted = sorted(
            _cpt_good,
            key=lambda x: (x[1]["wins"] / x[1]["total"], x[1]["total"]),
            reverse=True,
        )[:3]
        sec.append("Best pairs: " + " · ".join(
            f"{p} {int(s['wins']/s['total']*100)}%" for p, s in _cpt_sorted
        ))

    # Profit factor
    _cpt_wp = [_cf(r.get("pips")) for r in _cpt_wins if _cf(r.get("pips")) is not None]
    _cpt_lp = [_cf(r.get("pips")) for r in _cpt_loss if _cf(r.get("pips")) is not None]
    if _cpt_wp and _cpt_lp:
        _cpt_tl = sum(abs(p) for p in _cpt_lp if p < 0)
        if _cpt_tl > 0:
            _cpt_pf = sum(_cpt_wp) / _cpt_tl
            _cpt_icon = "✅" if _cpt_pf > 1.0 else "⚠️"
            _cpt_txt  = "wins larger than losses" if _cpt_pf > 1.0 else "losses larger than wins"
            sec.append(f"Profit factor: {_cpt_pf:.2f} — {_cpt_txt} {_cpt_icon}")

    return sec


def _build_research_section(research_result=None) -> list:
    """Build the RESEARCH TRADES Telegram section from data/research_trades.csv.

    Three display modes:
      • 30-day analysis complete → recommendation line
      • Closed trades exist      → full stats breakdown
      • No closed trades yet     → collecting-data summary
    Returns an empty list when no research trades exist at all.
    """
    try:
        from src import research_tracker as _rtrk
        rows = _rtrk.load()
    except Exception:
        return []
    if not rows:
        return []

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    open_rows    = [r for r in rows if r.get("status") in ("OPEN", "NO_PRICE_LEVELS")]
    closed       = [r for r in rows if r.get("status") in ("WIN", "LOSS", "BREAKEVEN", "EXPIRED", "PARTIAL_WIN", "FULL_WIN")]
    wins         = [r for r in closed if r.get("status") in ("WIN", "FULL_WIN", "PARTIAL_WIN")]
    losses       = [r for r in closed if r.get("status") == "LOSS"]
    partial_wins = [r for r in closed if r.get("status") == "PARTIAL_WIN"]
    expired_rows = [r for r in closed if r.get("status") == "EXPIRED"]

    # Days elapsed from oldest trade date
    days_elapsed = 0
    try:
        dates = [r.get("date", "") for r in rows if r.get("date")]
        if dates:
            days_elapsed = (_auckland_now().replace(tzinfo=None) -
                            datetime.strptime(min(dates), "%Y-%m-%d")).days
    except Exception:
        pass
    days_remaining = max(0, 30 - days_elapsed)

    sec = ["", "━━━━━━━━━━━━━━━━━━━━━"]

    # ── Mode 1: 30-day analysis has fired ─────────────────────────────────────
    if research_result and research_result.get("recommendation") not in (None, "INSUFFICIENT_DATA"):
        rec = research_result.get("recommendation", "")
        c6  = (research_result.get("band_results") or {}).get("6") or {}
        wr6 = f"{c6.get('win_rate', 0) * 100:.0f}%" if c6 else "?"
        sec.append("🔬 <b>RESEARCH TRADES — ANALYSIS COMPLETE</b>")
        if rec == "LOWER_TO_6":
            sec.append(
                f"Recommendation: LOWER_TO_6 — conf 6 setups profitable at {wr6} win rate"
            )
        elif rec == "KEEP_AT_7":
            sec.append(
                f"Recommendation: KEEP_AT_7 — conf 6 setups only {wr6} win rate, insufficient edge"
            )
        else:
            sec.append(f"Recommendation: {rec}")
            rsn = research_result.get("reasoning", "")
            if rsn:
                sec.append(f"<i>{rsn}</i>")
        return sec

    n_open   = len(open_rows)
    n_closed = len(closed)
    n_total  = len(rows)
    sec.append("🔬 <b>RESEARCH TRADES</b>")

    # ── Mode 2: No closed trades yet ──────────────────────────────────────────
    if not closed:
        sec.append(
            f"<b>{n_open}</b> open trades tracking | <b>0</b> closed trades"
        )
        if days_remaining > 0:
            sec.append(f"Collecting data — first analysis in {days_remaining} days")
        else:
            sec.append("Collecting data — analysis pending (need 10+ closed trades)")
        pair_freq: dict = {}
        for r in rows:
            p = r.get("pair", "")
            if p:
                pair_freq[p] = pair_freq.get(p, 0) + 1
        top = sorted(pair_freq, key=lambda x: pair_freq[x], reverse=True)[:4]
        if top:
            sec.append(f"Most active pairs: {', '.join(top)}")
        return sec

    # ── Mode 3: Full breakdown ─────────────────────────────────────────────────
    n_wins   = len(wins)
    n_losses = len(losses)
    decisive = n_wins + n_losses
    wr_pct   = int(n_wins / decisive * 100) if decisive else 0

    sec.append(
        f"Open: <b>{n_open}</b> trades | Closed: <b>{n_closed}</b> trades | Total: <b>{n_total}</b>"
    )
    sec.append("")
    sec.append(
        f"<b>{wr_pct}%</b> wins ({n_wins}W / {n_losses}L) — {decisive} closed trades"
    )
    if decisive >= 10 and wr_pct < 30:
        sec.append(
            f"⚠️ <b>Win rate {wr_pct}% from {decisive} trades — well below target.</b> "
            f"System is in learning phase — do not adjust strategy until 50+ decisive trades recorded."
        )

    win_pips  = [_f(r.get("pips")) for r in wins   if _f(r.get("pips")) is not None]
    loss_pips = [_f(r.get("pips")) for r in losses if _f(r.get("pips")) is not None]
    if win_pips and loss_pips:
        sec.append(
            f"Avg win: <b>+{sum(win_pips)/len(win_pips):.0f} pips</b> | "
            f"Avg loss: <b>{sum(loss_pips)/len(loss_pips):.0f} pips</b>"
        )
    elif win_pips:
        sec.append(f"Avg win: <b>+{sum(win_pips)/len(win_pips):.0f} pips</b>")
    elif loss_pips:
        sec.append(f"Avg loss: <b>{sum(loss_pips)/len(loss_pips):.0f} pips</b>")

    all_with_pips = [(r, _f(r.get("pips"))) for r in closed if _f(r.get("pips")) is not None]
    if all_with_pips:
        best  = max(all_with_pips, key=lambda x: x[1])
        worst = min(all_with_pips, key=lambda x: x[1])
        sec.append(
            f"Best trade: {best[0].get('pair')} {best[0].get('direction')} {best[1]:+.0f} pips"
        )
        if worst[0] is not best[0]:
            sec.append(
                f"Worst trade: {worst[0].get('pair')} {worst[0].get('direction')} {worst[1]:+.0f} pips"
            )

    if win_pips and loss_pips:
        total_loss = sum(abs(p) for p in loss_pips if p < 0)
        if total_loss > 0:
            _pf = sum(win_pips) / total_loss
            sec.append(f"Profit factor: <b>{_pf:.2f}</b>")
            if _pf > 1.0:
                sec.append(
                    f"📈 Profit factor {_pf:.2f} — your wins are larger than your losses which is "
                    f"a good sign even at an early low win rate. This is how professional traders operate."
                )

    # Pip validation audit — flag NOK/HKD/SGD trades with >500 pips (mathematically correct
    # but large due to price scale — 0.0001 pip size on 9.5 NOK means 500 pips = 0.05 NOK)
    try:
        _large_pip_flags = []
        for _r_audit in closed:
            _rp_str = _r_audit.get("pair", "")
            _rp_quote = _rp_str.split("/")[-1].upper() if "/" in _rp_str else _rp_str[-3:].upper()
            _rp_pips = _f(_r_audit.get("pips"))
            if _rp_pips is not None and abs(_rp_pips) > 500 and _rp_quote != "JPY":
                _rp_dir  = _r_audit.get("direction", "")
                _large_pip_flags.append(
                    f"#{_r_audit.get('id','?')} {_rp_str} {_rp_dir} {_rp_pips:+.0f}p"
                )
        if _large_pip_flags:
            sec.append("")
            sec.append(
                f"⚠️ Pip validation — {len(_large_pip_flags)} trade(s) show >500 pips on non-JPY pair "
                f"(pip_size=0.0001 is correct — large values reflect NOK/HKD price scale):"
            )
            for _flag in _large_pip_flags[:5]:
                sec.append(f"  {_flag}")
    except Exception:
        pass

    # Expiry analysis breakdown
    n_expiry_total = len(expired_rows) + len(partial_wins)
    if n_expiry_total > 0:
        sec.append("")
        sec.append("⏰ <b>Expiry analysis:</b>")
        partial_ep = [_f(r.get("pips")) for r in partial_wins if _f(r.get("pips")) is not None]
        exp_pos    = [_f(r.get("pips")) for r in expired_rows if (_f(r.get("pips")) or 0) > 0]
        exp_neg    = [_f(r.get("pips")) for r in expired_rows if (_f(r.get("pips")) or 0) < 0]
        exp_neu    = [r for r in expired_rows if _f(r.get("pips")) is None or _f(r.get("pips")) == 0]
        if partial_wins:
            avg_p = sum(partial_ep) / len(partial_ep) if partial_ep else 0
            sec.append(
                f"✅ {len(partial_wins)} PARTIAL WIN (>50% to target, avg +{avg_p:.0f}p) · "
                f"{len(expired_rows)} expired"
            )
        else:
            sec.append(f"{n_expiry_total} expired — expiry window extended to 7-10 days")
        detail = []
        if exp_pos:
            avg_pos = sum(exp_pos) / len(exp_pos)
            detail.append(f"{len(exp_pos)} profitable (avg +{avg_pos:.0f}p)")
        if exp_neg:
            avg_neg = sum(exp_neg) / len(exp_neg)
            detail.append(f"{len(exp_neg)} against direction (avg {avg_neg:.0f}p)")
        if exp_neu:
            detail.append(f"{len(exp_neu)} neutral")
        if detail and expired_rows:
            sec.append(f"   Expired: {' · '.join(detail)}")

    # Confidence breakdown (bands 5, 6, 7)
    sec.append("")
    sec.append("📊 <b>Confidence breakdown:</b>")
    for cv in (5, 6, 7):
        band = [r for r in closed if str(r.get("confidence", "")).strip() == str(cv)]
        bw   = sum(1 for r in band if r.get("status") == "WIN")
        bpw  = sum(1 for r in band if r.get("status") == "PARTIAL_WIN")
        bl   = sum(1 for r in band if r.get("status") == "LOSS")
        bd   = bw + bl
        pw_str = f" · {bpw} partial" if bpw else ""
        if bd:
            sec.append(f"{cv}/10 setups: {bw}W {bl}L{pw_str} — {int(bw/bd*100)}% win rate")
        elif bpw:
            sec.append(f"{cv}/10 setups: {bpw} partial wins — no full wins/losses yet")
        elif band:
            sec.append(f"{cv}/10 setups: {len(band)} trades — no decisive outcomes yet")
        else:
            sec.append(f"{cv}/10 setups: 0W 0L — no data yet")

    # Most promising pairs by win rate (PARTIAL_WIN counted as directional success)
    pair_stats: dict = {}
    for r in closed:
        p  = r.get("pair", "?")
        ps = pair_stats.setdefault(p, {"wins": 0, "total": 0})
        ps["total"] += 1
        if r.get("status") in ("WIN", "PARTIAL_WIN"):
            ps["wins"] += 1
    sorted_pairs = sorted(
        pair_stats.items(),
        key=lambda x: (x[1]["wins"] / max(x[1]["total"], 1), x[1]["total"]),
        reverse=True,
    )
    sec.append("")
    sec.append("🔬 <b>Most promising pairs so far:</b>")
    for pair, ps in sorted_pairs[:4]:
        pwr = int(ps["wins"] / ps["total"] * 100) if ps["total"] else 0
        sec.append(f"{pair} — {ps['wins']} wins from {ps['total']} trades ({pwr}%)")

    # ── Pair performance weighting display ───────────────────────────────────
    # Build per-pair stats using decisive trades (WIN + LOSS only, not PARTIAL/EXPIRED)
    _pw_stats: dict = {}
    for r in rows:
        p = r.get("pair", "")
        s = r.get("status", "")
        if not p or s not in ("WIN", "LOSS"):
            continue
        if p not in _pw_stats:
            _pw_stats[p] = {"wins": 0, "n": 0}
        _pw_stats[p]["n"] += 1
        if s == "WIN":
            _pw_stats[p]["wins"] += 1

    _strengths = sorted(
        [(p, d["wins"] / d["n"], d["n"]) for p, d in _pw_stats.items()
         if d["n"] >= 3 and d["wins"] / d["n"] >= 0.60],
        key=lambda x: x[1], reverse=True,
    )
    _weaknesses = sorted(
        [(p, d["n"]) for p, d in _pw_stats.items()
         if d["n"] >= 4 and d["wins"] == 0],
        key=lambda x: x[1], reverse=True,
    )

    if _strengths or _weaknesses:
        sec.append("")
    if _strengths:
        _str_parts = " · ".join(f"{p} {wr:.0%}" for p, wr, _ in _strengths[:6])
        sec.append(f"🏆 System strengths (60%+ win rate with 3+ trades): {_str_parts}")
    if _weaknesses:
        _wk_parts = " · ".join(p for p, _ in _weaknesses[:5])
        sec.append(
            f"⚠️ System weaknesses (0% win rate with 4+ trades): {_wk_parts}"
            f" — deprioritised until conditions change"
        )

    # Recent performance: last 11 decisive trades
    try:
        _rd_all = sorted(
            [r for r in rows if r.get("status") in ("WIN", "LOSS") and r.get("closed_at")],
            key=lambda x: (x.get("closed_at", ""), x.get("id", "0")),
            reverse=True,
        )[:11]
        if _rd_all:
            _rc_wins   = sum(1 for r in _rd_all if r.get("status") == "WIN")
            _rc_losses = len(_rd_all) - _rc_wins
            _rc_n      = len(_rd_all)
            _large_pip_pairs = []
            for _rrd in _rd_all:
                try:
                    _rp_pips  = float(_rrd.get("pips") or 0)
                    _rp_pair  = _rrd.get("pair", "")
                    _rp_quote = _rp_pair.split("/")[-1].upper() if "/" in _rp_pair else _rp_pair[-3:].upper()
                    if abs(_rp_pips) > 500 and _rp_quote != "JPY":
                        _large_pip_pairs.append(f"{_rp_pair} {_rp_pips:+.0f}p")
                except (TypeError, ValueError):
                    pass
            sec.append("")
            sec.append(
                f"Recent performance: last {_rc_n} decisive trades — "
                f"<b>{_rc_wins} WIN {_rc_losses} LOSS</b>"
            )
            if _large_pip_pairs:
                sec.append(
                    f"⚠️ Abnormal pip values (>500p on non-JPY) — likely NOK/HKD price scale: "
                    + ", ".join(_large_pip_pairs)
                )
    except Exception:
        pass

    # Days until / since threshold analysis
    sec.append("")
    if days_remaining > 0:
        sec.append(
            f"⏳ <b>Days until threshold analysis: {days_remaining} days remaining</b>"
        )
        sec.append(
            "(System will recommend whether to lower threshold to 6 after 30 days of data)"
        )
    else:
        sec.append("⏳ <b>Threshold analysis: 30+ days of data — awaiting 10+ closed trades</b>")

    return sec


_TG_MAX = 3800  # Telegram hard limit is 4096; use 3800 for safe margin
_TG_TRUNCATE_NOTE = "\n\n[Message truncated — see full report in GitHub Actions log]"


def _split_long_lines(lines: list, max_len: int) -> list:
    """Split any single line that is itself longer than max_len into chunks."""
    out = []
    for ln in lines:
        while len(ln) > max_len:
            out.append(ln[:max_len])
            ln = ln[max_len:]
        out.append(ln)
    return out


def _send_in_parts(sections: list, urgent_alerts: list = None) -> None:
    """Send Telegram messages with guaranteed delivery of trade alerts.

    1. urgent_alerts — each string sent as a standalone message FIRST so trade
       signals are never buried or lost in a long message.
    2. sections split semantically: critical (trades/watchlist/calendar) → Part 1,
       lower priority (research/fund/health) → Part 2.
    3. Each semantic group is batched at ≤_TG_MAX chars with Part N of M labels.
    """
    # 1. Send urgent pre-alerts individually first
    for _ua in (urgent_alerts or []):
        try:
            _telegram(_ua)
        except Exception:
            pass

    # 2. Categorize sections — these headers indicate lower-priority Part 2 content
    def _is_part2(sec):
        flat = "\n".join(sec).upper()
        return (
            "SYSTEM HEALTH" in flat
            or "FOREX AI FUND" in flat
            or "WEEKLY PERFORMANCE" in flat
            or "RESEARCH" in flat
            or "💳" in "\n".join(sec)
        )

    critical_secs = [s for s in sections if not _is_part2(s)]
    lower_secs    = [s for s in sections if _is_part2(s)]

    # 3. Build batches for each group
    def _build_batches(secs):
        batches = []
        current: list = []
        cur_len = 0
        for sec in secs:
            sec_lines = _split_long_lines(list(sec), _TG_MAX)
            sec_len   = len("\n".join(sec_lines))
            if current and cur_len + sec_len > _TG_MAX:
                batches.append(current)
                current = []
                cur_len = 0
            current.extend(sec_lines)
            cur_len += sec_len
        if current:
            batches.append(current)
        return batches

    critical_batches = _build_batches(critical_secs)
    lower_batches    = _build_batches(lower_secs)
    total_parts      = len(critical_batches) + len(lower_batches)

    def _safe_send(text: str) -> None:
        """Send a Telegram message, hard-truncating at _TG_MAX characters."""
        if len(text) > _TG_MAX:
            cut = text[:_TG_MAX - len(_TG_TRUNCATE_NOTE)]
            last_nl = cut.rfind("\n")
            if last_nl > _TG_MAX * 0.7:
                cut = cut[:last_nl]
            text = cut + _TG_TRUNCATE_NOTE
        _telegram(text)

    part_num = 1
    for batch in critical_batches:
        if total_parts > 1:
            label = f"🤖 FOREX AI — Part {part_num} of {total_parts} — TRADES AND ALERTS"
            _safe_send(label + "\n" + "\n".join(batch))
        else:
            _safe_send("\n".join(batch))
        part_num += 1
    for batch in lower_batches:
        if total_parts > 1:
            label = f"🤖 FOREX AI — Part {part_num} of {total_parts} — RESEARCH AND SYSTEM"
            _safe_send(label + "\n" + "\n".join(batch))
        else:
            _safe_send("\n".join(batch))
        part_num += 1


# ── Trade block helpers ────────────────────────────────────────────────────────

import re as _re


def _parse_entry_window(bet_str: str) -> tuple:
    """Extract (session_label, enter_between, do_not_enter_after) from BEST_ENTRY_TIME."""
    bl = bet_str.lower()
    if "london" in bl:
        sess = "London open"
    elif "new york" in bl or " ny " in bl:
        sess = "New York open"
    elif "tokyo" in bl:
        sess = "Tokyo open"
    elif "sydney" in bl or "asian" in bl:
        sess = "Sydney open"
    else:
        sess = "optimal session"

    m = _re.search(r'(\d+(?::\d+)?(?:am|pm))\s*[–\-—]\s*(\d+(?::\d+)?(?:am|pm))',
                   bet_str, _re.IGNORECASE)
    if m:
        start_s = m.group(1)
        end_s   = m.group(2).lower()
        mid_s   = _add_90min(start_s)
        return sess, f"{start_s} – {mid_s}", end_s
    return sess, bet_str[:40] if bet_str else "session open", "session close"


def _add_90min(time_str: str) -> str:
    """Add 90 min to a time string like '5pm' → '6:30pm'."""
    m = _re.match(r'(\d+)(?::(\d+))?\s*(am|pm)', time_str.lower())
    if not m:
        return time_str
    h    = int(m.group(1))
    mins = int(m.group(2) or "0")
    ampm = m.group(3)
    if ampm == "pm" and h != 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    total = h * 60 + mins + 90
    nh = (total // 60) % 24
    nm = total % 60
    na = "am" if nh < 12 else "pm"
    dh = nh % 12 or 12
    return f"{dh}:{nm:02d}{na}" if nm else f"{dh}{na}"


def _management_lines(direction: str, entry_v: float, stop_v: float,
                      target_v: float, pair: str) -> list:
    """Compute breakeven / trailing stop management instructions."""
    try:
        _ev = float(entry_v)
        if _ev < 0.01:
            dec = 6
        elif _ev < 0.10:
            dec = 5
        elif "JPY" in pair.upper():
            dec = 3
        else:
            dec = 5
    except (TypeError, ValueError):
        dec = 3 if "JPY" in pair.upper() else 5
    try:
        if direction == "BUY":
            dist        = target_v - entry_v
            half_pt     = entry_v + dist * 0.50
            seventy5_pt = entry_v + dist * 0.75
            trail_stop  = entry_v + dist * 0.30
        else:
            dist        = entry_v - target_v
            half_pt     = entry_v - dist * 0.50
            seventy5_pt = entry_v - dist * 0.75
            trail_stop  = entry_v - dist * 0.30

        return [
            f"- Move stop to breakeven ({entry_v:.{dec}f}) when price reaches {half_pt:.{dec}f} (50% to target)",
            f"- Take half profit at {half_pt:.{dec}f} if you want to reduce risk",
            f"- Trail stop to {trail_stop:.{dec}f} when price reaches {seventy5_pt:.{dec}f} (75% to target)",
            f"- Full target: {target_v:.{dec}f} — let it run if momentum is strong",
        ]
    except (TypeError, ValueError):
        return [f"- Full target: {_fmt_price(target_v)} — let it run if momentum is strong"]


def _why_agrees_lines(r: dict, ctx: dict) -> list:
    """Build 'Why all data agrees' bullet points from the bundle."""
    p     = r["parsed"]
    pair  = r["pair"]
    dirn  = (p.get("direction") or "").upper()
    lines = []

    # Technical
    try:
        daily   = r["bundle"]["technical"]["daily"]
        rsi     = daily.get("rsi14")
        macd    = daily.get("macd", "")
        h4      = (r["bundle"]["technical"].get("h4") or
                   r["bundle"]["technical"].get("4h") or {})
        h4_rsi  = h4.get("rsi14") if isinstance(h4, dict) else None
        rsi_str = f"RSI {rsi:.0f}" if rsi is not None else "RSI —"
        mac_str = "bullish MACD" if "bullish" in str(macd).lower() else "bearish MACD"
        h4_str  = f" + 4H RSI {h4_rsi:.0f}" if h4_rsi is not None else ""
        lines.append(f"- Technical: {rsi_str} · {mac_str}{h4_str}")
    except (KeyError, TypeError, ValueError):
        s = p.get("technical_score")
        if s:
            lines.append(f"- Technical: score {s}/10")

    # Fundamental
    try:
        fund = r["bundle"]["fundamental"]
        diff = fund.get("rate_differential_pct")
        if diff is not None:
            carry = (fund.get("carry_note") or "")[:60]
            lines.append(f"- Fundamental: {diff:+.2f}% carry advantage — {carry}")
        else:
            fs = p.get("fundamental_score")
            if fs:
                lines.append(f"- Fundamental: score {fs}/10")
    except (KeyError, TypeError, ValueError):
        pass

    # COT positioning + momentum
    try:
        pos_bundle = r["bundle"]["positioning"]
        for side in ("base", "quote"):
            pp = pos_bundle.get(side, {})
            if pp.get("status") == "ok":
                pct      = pp.get("percentile_in_range")
                pdir     = pp.get("direction", "")
                flag     = (pp.get("extreme_flag") or "")[:55]
                momentum = pp.get("cot_momentum", "")
                if pct is not None:
                    _mom_icons = {
                        "BUILDING":  "📈",
                        "UNWINDING": "📉",
                        "REVERSING": "🔄",
                        "STABLE":    "➡️",
                    }
                    _mom_icon = _mom_icons.get(momentum, "")
                    _mom_str  = f" · {_mom_icon} COT {momentum}" if momentum else ""
                    lines.append(
                        f"- COT: {pp['currency']} {pdir} at {pct:.0f}th pct"
                        f"{_mom_str} — {flag}"
                    )
                    break
    except (KeyError, TypeError, ValueError):
        ps = p.get("positioning_score")
        if ps:
            lines.append(f"- COT: positioning score {ps}/10")

    # Sentiment — draw from key_thesis if available
    kt = (p.get("key_thesis") or "").lower()
    sent_bundle = r["bundle"].get("sentiment", {})
    for side in ("base", "quote"):
        sb = sent_bundle.get(side, {})
        if sb.get("status") == "ok":
            cnt = sb.get("article_count", 0)
            ccy = sb.get("currency", "")
            # Infer tone from thesis
            tone = "mixed"
            if any(w in kt for w in ("bullish", "positive", "strong", "hawkish")):
                tone = "bullish" if dirn == "BUY" else "bearish"
            elif any(w in kt for w in ("bearish", "negative", "weak", "dovish")):
                tone = "bearish" if dirn == "SELL" else "bullish"
            lines.append(f"- Sentiment: {cnt} articles on {ccy} — {tone} news tone")
            break

    # Macro
    risk = ctx.get("risk_env", "⚖️ neutral")
    clean = pair.upper().replace("/", "")
    macro_note = ""
    if "JPY" in clean:
        macro_note = "safe-haven JPY suppressed in risk-on" if "risk-on" in risk else "safe-haven JPY supported"
    elif "USD" in clean:
        macro_note = "USD supported by risk-on flows" if "risk-on" in risk else "USD in defensive mode"
    elif "AUD" in clean or "NZD" in clean:
        macro_note = "commodity currencies benefit from risk-on" if "risk-on" in risk else "commodity currencies under pressure"
    lines.append(f"- Macro: {risk}{' — ' + macro_note if macro_note else ''}")

    return lines


# ── Cost-reduction helpers ─────────────────────────────────────────────────────

def _pre_filter_pairs(ranked_all: list, top_n: int = 20, log=print) -> list:
    """Score pairs using free FRED+COT data before fetching Twelve Data candles.

    Takes the ranked list from selector, pre-fetches fundamental rates and
    COT positioning for all unique currencies (free APIs, disk-cached), scores
    each pair, and returns the top_n symbols for Twelve Data fetching.
    """
    from src import fred as _fred

    all_pairs = [p for p, _ in ranked_all[:max(top_n * 2, 40)]]
    if not all_pairs:
        return []

    # Collect unique currencies
    unique_ccys: set = set()
    for pair in all_pairs:
        clean = pair.upper().replace("/", "").replace("-", "")
        if len(clean) == 6:
            unique_ccys.add(clean[:3])
            unique_ccys.add(clean[3:])

    log(f"Pre-filter: pre-fetching FRED+COT for {len(unique_ccys)} currencies ...")

    # Pre-fetch FRED rates (populate disk cache for all currencies at once)
    _rate_cache: dict = {}
    for ccy in unique_ccys:
        meta   = config.CURRENCIES.get(ccy, {})
        series = meta.get("rate_fred")
        if series:
            val, _ = _fred.latest(series)
            _rate_cache[ccy] = val

    # Pre-fetch COT positioning (populate disk cache)
    from src import positioning as _pos
    _cot_cache: dict = {}          # ccy -> percentile_in_range
    _cot_mom_cache: dict = {}      # ccy -> cot_momentum string
    for ccy in unique_ccys:
        cot_data = _pos._for_currency(ccy)
        if cot_data.get("status") == "ok":
            _cot_cache[ccy]     = cot_data.get("percentile_in_range", 50.0)
            _cot_mom_cache[ccy] = cot_data.get("cot_momentum", "STABLE")

    # Score each pair
    scored: list = []
    for pair in all_pairs:
        clean = pair.upper().replace("/", "").replace("-", "")
        if len(clean) < 6:
            continue
        base, quote = clean[:3], clean[3:]
        score = 0.0

        # Rate differential: larger magnitude = more carry opportunity
        b_r = _rate_cache.get(base)
        q_r = _rate_cache.get(quote)
        if b_r is not None and q_r is not None:
            score += min(5.0, abs(b_r - q_r) / 0.5)

        # COT positioning: distance from 50th percentile + momentum bonus
        for ccy in (base, quote):
            pct = _cot_cache.get(ccy)
            if pct is not None:
                score += abs(pct - 50.0) / 25.0   # max 2 pts per currency
            mom = _cot_mom_cache.get(ccy, "")
            if mom == "BUILDING":
                score += 0.5   # institutions adding conviction — higher opportunity
            elif mom == "REVERSING":
                score -= 0.5   # institutional flip is a warning, deprioritise

        # Fundamental divergence: pairs where one ccy is fundamentally bullish and
        # the other bearish have a clearer directional trade — score them higher
        try:
            from src import fundamentals as _fund_pf
            _cb_b  = _fund_pf.currency_cb_score(base)
            _cb_q  = _fund_pf.currency_cb_score(quote)
            _ec_b  = _fund_pf.currency_econ_score(base)
            _ec_q  = _fund_pf.currency_econ_score(quote)
            score += abs((_cb_b + _ec_b) - (_cb_q + _ec_q)) * 0.5  # max 2.0
        except Exception:
            pass

        scored.append((pair, score))

    # Stable-sort: primary key = free-data score, secondary = original selector rank
    scored.sort(key=lambda x: x[1], reverse=True)
    top = [p for p, _ in scored[:top_n]]
    log(f"Pre-filter: {len(all_pairs)} candidates → top {len(top)} for Twelve Data")
    return top


def _pre_fetch_shared_data(pairs: list, log=print) -> tuple:
    """Pre-fetch FRED, COT, NewsAPI, and macro for all pairs at once.

    Returns (fund_store, macro_result) where fund_store maps pair → fundamental
    result dict. Subsequent analysis reads from these in-memory objects, avoiding
    duplicate disk-cache reads and API round-trips.
    """
    from src import fundamental as _fund, macro as _mac

    log("Batch pre-fetch: fetching shared fundamental + macro data ...")
    macro_result = _mac.analyse()

    fund_store: dict = {}
    for pair in pairs:
        clean = pair.upper().replace("/", "").replace("-", "")
        if len(clean) == 6:
            base, quote = clean[:3], clean[3:]
            try:
                fund_store[pair] = _fund.analyse(base, quote)
            except Exception:
                pass

    hits = sum(1 for v in fund_store.values() if v.get("status") == "ok")
    log(f"Batch pre-fetch: {len(fund_store)} fundamental results ({hits} ok) + 1 macro")
    return fund_store, macro_result


# ── Indicative level calculator ───────────────────────────────────────────────

def _compute_expiry_days_from_rr(rr: float) -> int:
    """Calibrate trade expiry from R:R ratio.

    Stop ≈ 1x ATR ≈ average daily range (ADR), so R:R ≈ target_pips / adr_pips.
    Formula: max(4, round(rr * 1.5) + 1)
    Examples: R:R 2.3 → 4 days; R:R 3.1 → 6 days.
    """
    try:
        return max(4, round(float(rr) * 1.5) + 1)
    except (TypeError, ValueError):
        return 5


def _calc_indicative_levels(pair: str, parsed: dict, bundle: dict,
                             research_mode: bool = False) -> tuple:
    """Return (entry, stop, target, meta) for indicative display.

    Stop = 1.0x ATR(14), rounded to nearest 5 pips.
    Target (display mode):  nearest Fibonacci in [1.5x, 2.5x] ATR, else 2.0x ATR.
    Target (research_mode): nearest Fibonacci in [0.8x, 1.2x] ATR, else 1.0x ATR.
      research_mode=True uses a tighter target so research trades resolve as WIN/LOSS
      faster (more decisive outcomes for ML training).  Fund trade display is always
      called with the default research_mode=False — targets are never changed.
    meta dict: {atr, stop_atr_mult, target_atr_mult, expiry_days, quality_flag}

    Uses Claude's parsed entry/stop/target when present.  When stop or target
    are missing, computes them from ATR.  Returns (None, None, None, {}) if
    price is completely unavailable.
    """
    _empty_meta: dict = {
        "atr": None, "stop_atr_mult": None, "target_atr_mult": None,
        "expiry_days": 5, "quality_flag": "",
    }

    try:
        entry  = float(parsed.get("entry")     or 0) or None
        stop   = float(parsed.get("stop_loss") or 0) or None
        target = float(parsed.get("target")    or 0) or None
    except (TypeError, ValueError):
        entry, stop, target = None, None, None

    # Pull actual ATR14 and current price from bundle
    atr_actual = None
    cur        = None
    try:
        _daily = bundle.get("technical", {}).get("daily", {})
        if isinstance(_daily, dict):
            atr_actual = float(_daily.get("atr14") or 0) or None
            cur        = float(_daily.get("last_close") or _daily.get("close") or 0) or None
    except (TypeError, ValueError):
        pass

    # If all 3 Claude values present, compute meta only
    if entry and stop and target:
        meta = dict(_empty_meta)
        if atr_actual and atr_actual > 0:
            try:
                sd = abs(entry - stop)
                td = abs(entry - target)
                meta["atr"]             = atr_actual
                meta["stop_atr_mult"]   = round(sd / atr_actual, 1)
                meta["target_atr_mult"] = round(td / atr_actual, 1)
                rr = td / sd if sd > 0 else 0
                meta["expiry_days"]  = _compute_expiry_days_from_rr(rr)
                meta["quality_flag"] = "LOW QUALITY SETUP" if rr < 2.5 else ""
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        else:
            try:
                sd = abs(entry - stop)
                td = abs(entry - target)
                rr = td / sd if sd > 0 else 0
                meta["expiry_days"]  = _compute_expiry_days_from_rr(rr)
                meta["quality_flag"] = "LOW QUALITY SETUP" if rr < 2.5 else ""
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        return entry, stop, target, meta

    if cur is None:
        return entry, stop, target, _empty_meta

    # --- ATR-based fallback ---
    quote_ccy = pair.split("/")[-1].upper() if "/" in pair else pair[-3:].upper()
    base_ccy  = pair.split("/")[0].upper()  if "/" in pair else pair[:3].upper()
    is_jpy      = quote_ccy == "JPY"
    # Issue 2: JPY-as-base pairs (JPY/USD, JPY/EUR etc.) have very small prices (~0.006)
    is_jpy_base = (base_ccy == "JPY" and not is_jpy)

    if is_jpy:
        pip_size, dec = 0.01, 3
    elif is_jpy_base:
        pip_size, dec = 0.000001, 6   # micro pip for JPY/USD at price ~0.006
    else:
        pip_size, dec = 0.0001, 5

    # Use actual ATR14 if available, else estimate from pair characteristics
    if atr_actual and atr_actual > 0:
        atr = atr_actual
    elif is_jpy:
        atr = 0.50
    elif is_jpy_base:
        # ~0.3% of price is equivalent to a ~50 pip move in USD/JPY
        atr = (cur * 0.003) if cur else 0.00002
    elif cur < 0.10:
        atr = cur * 0.008
    elif any(c in pair.upper() for c in ("EUR", "GBP")):
        atr = 0.0080
    else:
        atr = 0.0050

    dirn = (parsed.get("direction") or "").upper()
    if dirn not in ("BUY", "SELL"):
        return entry, stop, target, _empty_meta

    entry = entry or cur

    # Stop: 1.0x ATR rounded to nearest 5 pips (minimum 5 pips to avoid zero stop)
    if not stop:
        atr_pips   = atr / pip_size
        stop_pips  = round(atr_pips / 5) * 5   # nearest 5 pips
        stop_pips  = max(stop_pips, 5)          # never allow zero stop distance
        stop_dist  = stop_pips * pip_size
        stop = round(entry - stop_dist if dirn == "BUY" else entry + stop_dist, dec)

    # Target: nearest Fibonacci level in the calibrated ATR range, else ATR fallback.
    # research_mode uses 1.0x ATR target (tighter) so trades resolve WIN/LOSS quickly.
    # Display mode keeps 2.0x ATR for fund-level trade blocks — NEVER changed.
    if not target:
        if research_mode:
            min_dist     = 0.8 * atr
            max_dist     = 1.2 * atr
            fallback_mult = 1.0
        else:
            min_dist     = 1.5 * atr
            max_dist     = 2.5 * atr
            fallback_mult = 2.0
        fib_target = None
        try:
            _fib_d = bundle.get("technical", {}).get("daily", {})
            if isinstance(_fib_d, dict):
                _fib = _fib_d.get("fibonacci", {})
                if isinstance(_fib, dict) and _fib.get("status") == "ok":
                    _fib_key    = "nearest_above" if dirn == "BUY" else "nearest_below"
                    _fib_levels = _fib.get(_fib_key, [])
                    for _lbl, _px in _fib_levels:
                        _px = float(_px)
                        dist = _px - entry if dirn == "BUY" else entry - _px
                        if min_dist <= dist <= max_dist:
                            fib_target = _px
                            break
        except (TypeError, ValueError, IndexError):
            pass
        if fib_target is not None:
            target = round(fib_target, dec)
        else:
            target = round(
                entry + atr * fallback_mult if dirn == "BUY"
                else entry - atr * fallback_mult, dec
            )

    # Reject impossible levels
    if entry and stop is not None and target is not None:
        if stop <= 0 or target <= 0:
            return None, None, None, _empty_meta
        if entry > 0 and (abs(entry - stop) > entry * 0.5 or abs(entry - target) > entry * 0.5):
            return None, None, None, _empty_meta

    # Compute meta
    meta = dict(_empty_meta)
    try:
        sd = abs(entry - stop)
        td = abs(entry - target)
        rr = td / sd if sd > 0 else 0
        meta["atr"]             = atr
        meta["stop_atr_mult"]   = round(sd / atr, 1) if atr > 0 else None
        meta["target_atr_mult"] = round(td / atr, 1) if atr > 0 else None
        meta["expiry_days"]     = _compute_expiry_days_from_rr(rr)
        # research_mode intentionally uses 1:1 R:R — don't flag as low quality
        _rr_threshold = 0.5 if research_mode else 1.5
        meta["quality_flag"]    = "LOW QUALITY SETUP" if rr < _rr_threshold else ""
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    return entry, stop, target, meta


# ── Open trade live-price helpers ─────────────────────────────────────────────

def _fetch_live_price(pair: str, px_cache: dict) -> tuple:
    """Return (price | None, stale: bool) for an open trade pair.

    Checks px_cache (scan data already in memory) first, then the 24-hour
    Twelve Data disk cache (from the 6am warm-cache run), then makes a
    fresh API call as a last resort.  Never falls through to the old
    'price not in today's scan' message.
    """
    if pair in px_cache:
        return px_cache[pair], False
    # Check every possible outputsize that might be cached from today's run
    try:
        from src import cache as _tc, technical as _tt
        for _sz in (400, 200, 100, 2):
            _cdata = _tc.get(f"TD:{pair}:1day:{_sz}", ttl_hours=24.0)
            if _cdata:
                _df = _tt._frame_from_td(_cdata)
                if len(_df) > 0:
                    p = float(_df["close"].iloc[-1])
                    if p > 0:
                        px_cache[pair] = p
                        return p, False
    except Exception:
        pass
    # Live API call as last resort (uses TD cache internally if available)
    if not config.TWELVE_DATA_KEY:
        return None, True
    try:
        from src import technical as _tt_live
        _data = _tt_live._td_request(pair, "1day", 2)
        _df   = _tt_live._frame_from_td(_data)
        if len(_df) > 0:
            p = float(_df["close"].iloc[-1])
            if p > 0:
                px_cache[pair] = p
                return p, False
    except Exception:
        pass
    return None, True


def _build_open_trades_section(open_trades: list, px_cache: dict, now_ak, compact: bool = False, cur_conf_map: dict = None) -> list:
    """Build the OPEN TRADES section lines for any Telegram scan message.

    compact=True: one-line-per-trade format for intraday scans.
    Always returns a non-empty list (shows 'No open trades' when empty).
    Placed at the top of every message immediately after the scan header.
    Each trade shows: entry/current/stop/target, unrealised P&L, progress bar,
    one dynamic status message from 8 possible states, and next key check time.
    """
    sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "📊 <b>YOUR OPEN TRADES</b>"]

    # Load risk profile once for investment detail calculations
    _rm_profile: dict = {}
    _rm_state: dict = {}
    try:
        from src import risk_manager as _rm_inv
        _rm_profile = _rm_inv.load_profile()
        _rm_state   = _rm_inv.compute_risk_state(_rm_profile)
    except Exception:
        pass

    if not open_trades:
        _fund_bal = _rm_profile.get("estimated_balance", 0) if _rm_profile else 0
        if compact:
            sec.append("No trades open · Fund: $" + (f"{_fund_bal:,.0f}" if _fund_bal else "—") + " fully available")
        else:
            sec.append("No trades currently open — the system is waiting for the right opportunity.")
            if _fund_bal > 0:
                sec.append(f"Your fund balance: <b>${_fund_bal:,.0f}</b> — fully available for the next trade.")
        return sec

    # Portfolio summary — quick pre-pass to compute aggregate P&L
    _port_dollar = 0.0
    _port_counted = 0
    for _pr in open_trades:
        try:
            _pc, _ = _fetch_live_price(_pr.get("pair", "?"), px_cache)
            _pe    = float(_pr.get("entry") or 0) or None
            _pd    = (_pr.get("direction") or "").upper()
            if _pc and _pe:
                _pz  = _pip_size(_pr.get("pair", "?"))
                _prw = (_pc - _pe) if _pd == "BUY" else (_pe - _pc)
                _port_dollar += (_prw / _pz) * 1.0
                _port_counted += 1
        except Exception:
            pass

    # Portfolio summary line
    _n = len(open_trades)
    _tw = "trade" if _n == 1 else "trades"
    if _port_counted > 0:
        if _port_dollar >= 0:
            sec.append(f"<b>{_n} open {_tw} — up ${_port_dollar:.0f} overall</b>")
        else:
            sec.append(f"<b>{_n} open {_tw} — down ${abs(_port_dollar):.0f} overall</b>")
    else:
        sec.append(f"<b>{_n} open {_tw}</b>")

    # Detect inverse pair duplicates in open trades (same directional bet on both sides)
    try:
        _ot_cleaned = [(r.get("pair","").upper().replace("/",""), (r.get("direction") or "").upper()) for r in open_trades]
        for _ip1 in range(len(_ot_cleaned)):
            for _ip2 in range(_ip1+1, len(_ot_cleaned)):
                _p1c, _d1c = _ot_cleaned[_ip1]
                _p2c, _d2c = _ot_cleaned[_ip2]
                if len(_p1c) >= 6 and len(_p2c) >= 6:
                    _p1_inv = _p1c[3:6] + _p1c[:3]
                    _opp_d  = "SELL" if _d1c == "BUY" else "BUY"
                    if _p1_inv == _p2c and _d2c == _opp_d:
                        _pp1 = open_trades[_ip1].get("pair","")
                        _pp2 = open_trades[_ip2].get("pair","")
                        sec.append(
                            f"⚠️ Risk warning: <b>{_pp1} {_d1c}</b> and <b>{_pp2} {_d2c}</b> are both open "
                            f"— combined risk is ~2% on a single currency relationship "
                            f"— consider closing one"
                        )
    except Exception:
        pass

    # Currency concentration warning — flag any currency appearing in 3+ open trades
    try:
        _ccy_count: dict = {}
        for _r in open_trades:
            _pc = _r.get("pair", "").upper().replace("/", "").replace("-", "")
            if len(_pc) >= 6:
                for _cc in [_pc[:3], _pc[3:6]]:
                    _ccy_count[_cc] = _ccy_count.get(_cc, 0) + 1
        for _cc, _cnt in sorted(_ccy_count.items(), key=lambda x: -x[1]):
            if _cnt >= 3:
                sec.append(
                    f"⚠️ Currency concentration: {_cc} appears in {_cnt} open trades "
                    f"— combined {_cc} risk exceeds 2% guideline — consider reducing"
                )
            elif _cnt == 2 and _cc == "HKD":
                sec.append(
                    f"⚠️ HKD concentration: {_cnt} open trades involve HKD — HKD pairs have wide spreads relative to ATR — monitor closely"
                )
    except Exception:
        pass

    _CCY_FULL = {
        "USD": "US Dollar",   "EUR": "Euro",         "GBP": "British Pound",
        "JPY": "Japanese Yen","AUD": "Australian Dollar","NZD": "New Zealand Dollar",
        "CAD": "Canadian Dollar","CHF": "Swiss Franc","HKD": "Hong Kong Dollar",
        "SGD": "Singapore Dollar","NOK": "Norwegian Krone","SEK": "Swedish Krona",
    }

    # Compact mode: one-line summary per trade for intraday messages
    if compact:
        for row in open_trades:
            _cp_row, _ = _fetch_live_price(row.get("pair", "?"), px_cache)
            _ce_row    = float(row.get("entry") or 0) or None
            _cs_row    = float(row.get("stop_loss") or 0) or None
            _ct_row    = float(row.get("target") or 0) or None
            _cd_row    = (row.get("direction") or "").upper()
            _cid_row   = row.get("id", "?")
            _cpair     = row.get("pair", "?")
            _buy_row   = _cd_row == "BUY"
            # Progress %
            _pct_row   = 0.0
            _gross_row = None
            try:
                if _cp_row and _ce_row and _cs_row and _ct_row:
                    _pr_pip = _pip_size(_cpair)
                    _pips_row = ((_cp_row - _ce_row) if _buy_row else (_ce_row - _cp_row)) / _pr_pip
                    if _buy_row and _ct_row > _ce_row:
                        _pct_row = min(100.0, max(0.0, (_cp_row - _ce_row) / (_ct_row - _ce_row) * 100))
                    elif not _buy_row and _ct_row < _ce_row:
                        _pct_row = min(100.0, max(0.0, (_ce_row - _cp_row) / (_ce_row - _ct_row) * 100))
                    # Dollar P&L approx (pip value * pips)
                    _stop_pips_row = abs(_ce_row - _cs_row) / _pr_pip
                    # simple approx: assume $1/pip for mini lot
                    _gross_row = _pips_row * 1.0
            except Exception:
                pass
            _bar_row   = int(_pct_row / 100 * 20)
            _prog_row  = "█" * _bar_row + "░" * (20 - _bar_row)
            if _gross_row is None:
                _gross_row = 0.0
            _pnl_icon  = "✅" if _gross_row > 0 else ("📉" if _gross_row < 0 else "➡️")
            _pnl_str   = (f"+${_gross_row:.0f}" if _gross_row > 0
                          else (f"-${abs(_gross_row):.0f}" if _gross_row < 0
                                else "$0.00"))
            sec.append("")
            sec.append(
                f"TRADE #{_cid_row} — {_cpair} {'Buying' if _buy_row else 'Selling'} · "
                f"{_pnl_str} {_pnl_icon} · {_pct_row:.0f}% to target {_prog_row}"
            )
            # Expiry warning
            try:
                _ot_exp = 5
                if _ce_row and _cs_row and _ct_row:
                    _sd_c = abs(_ce_row - _cs_row)
                    _td_c = abs(_ct_row - _ce_row)
                    _ot_exp = _compute_expiry_days_from_rr(_td_c / _sd_c if _sd_c > 0 else 0)
                _odt_c = datetime.strptime((row.get("timestamp") or "")[:10], "%Y-%m-%d")
                _days_c = (now_ak.replace(tzinfo=None) - _odt_c).days
                _rem_c = max(0, _ot_exp - _days_c)
                if _rem_c <= 2:
                    sec.append(f"⚠️ Expires in {_rem_c} day{'s' if _rem_c != 1 else ''}")
            except Exception:
                pass
            # 50% milestone reminder
            if _pct_row >= 50:
                sec.append(f"⚠️ Halfway — move stop loss to entry price to guarantee no loss")
            # Confidence monitoring (compact)
            if cur_conf_map is not None:
                _entry_conf_c = None
                try:
                    _entry_conf_c = int(float(row.get("confidence") or 0)) or None
                except (TypeError, ValueError):
                    pass
                _cur_conf_c = cur_conf_map.get(_cpair)
                if _entry_conf_c and _cur_conf_c is not None:
                    _cdelta_c = _cur_conf_c - _entry_conf_c
                    if _cdelta_c >= 1:
                        sec.append(f"✅ Thesis strengthening — confidence {_entry_conf_c}→{_cur_conf_c}/10")
                    elif _cdelta_c == 0:
                        sec.append(f"Confidence stable at {_entry_conf_c}/10 since entry")
                    elif _cdelta_c <= -2:
                        _urg_c = "🚨" if _cur_conf_c < 3 else "⚠️"
                        sec.append(f"{_urg_c} Confidence dropped {_entry_conf_c}→{_cur_conf_c}/10 — conditions have weakened — monitor closely")
            # News reminder (compact)
            try:
                from src import economic_calendar as _ec_ot_c
                _ot_evs_c = _ec_ot_c.events_for_pair(_cpair, hours=48)
                for _ot_ev_c in _ot_evs_c[:1]:
                    _ot_ccy_c = _ot_ev_c["currency"]
                    _ot_en_c  = _ot_ev_c["plain_name"]
                    _ot_ak_c  = _ot_ev_c["ak_display"]
                    sec.append(f"⚠️ {_ot_ccy_c} {_ot_en_c} within 48h ({_ot_ak_c}) — affects your {_cpair} trade")
            except Exception:
                pass
        return sec

    for row in open_trades:
        pair = row.get("pair", "?")
        dirn = (row.get("direction") or "").upper()
        tid  = row.get("id", "?")
        _base_c = pair.split("/")[0] if "/" in pair else pair[:3]
        _quot_c = pair.split("/")[1] if "/" in pair else pair[3:]
        _base_name = _CCY_FULL.get(_base_c, _base_c)
        _quot_name = _CCY_FULL.get(_quot_c, _quot_c)
        if dirn == "BUY":
            _dirn_plain = f"Buying {pair} (betting the {_base_name} will strengthen against the {_quot_name})"
        else:
            _dirn_plain = f"Selling {pair} (betting the {_base_name} will weaken against the {_quot_name})"

        sec.append("")
        sec.append(f"📊 <b>TRADE #{tid} — {_dirn_plain}</b>")

        cur, stale = _fetch_live_price(pair, px_cache)

        try:
            entry  = float(row.get("entry")     or 0) or None
            stop   = float(row.get("stop_loss") or 0) or None
            target = float(row.get("target")    or 0) or None
        except (TypeError, ValueError):
            entry, stop, target = None, None, None

        # Age / expiry — calibrated from R:R (stop ≈ 1x ATR ≈ ADR)
        days_open     = 0
        days_open_str = "?"
        expires_str   = "?"
        remaining     = 5
        try:
            _ot_entry  = float(row.get("entry")     or 0) or None
            _ot_stop   = float(row.get("stop_loss") or 0) or None
            _ot_target = float(row.get("target")    or 0) or None
            _ot_expiry = 5
            if _ot_entry and _ot_stop and _ot_target:
                _sd = abs(_ot_entry - _ot_stop)
                _td = abs(_ot_entry - _ot_target)
                _rr = _td / _sd if _sd > 0 else 0
                _ot_expiry = _compute_expiry_days_from_rr(_rr)
            opened_dt = datetime.strptime(row.get("timestamp", "")[:10], "%Y-%m-%d")
            days_open = (now_ak.replace(tzinfo=None) - opened_dt).days
            days_open_str = f"{days_open} day{'s' if days_open != 1 else ''}"
            remaining     = max(0, _ot_expiry - days_open)
            expires_str   = f"{remaining} day{'s' if remaining != 1 else ''}"
        except Exception:
            pass

        # Next key monitoring time
        _ew_t  = _entry_window_for_pair(pair)
        _eq_e, _ = _entry_quality(pair, now_ak)
        _sess_active, _sess_close = _session_status_for_pair(pair, now_ak)
        if _sess_active:
            _check_line = (
                f"⏰ <b>{_ew_t[6]} currently active — closes {_sess_close} Auckland</b>"
            )
        else:
            _tref_t = _time_ref_for_entry(_ew_t[0], _ew_t[1], now_ak)
            _check_line = (
                f"⏰ <b>Next key time:</b> {_ew_t[6]} "
                f"{_fmt_time_exact(_ew_t[0], _ew_t[1])} Auckland {_tref_t}"
            )

        # Compute entry date string
        _entry_date_str = ""
        try:
            _ots = row.get("timestamp", "")[:10]
            _odt = datetime.strptime(_ots, "%Y-%m-%d")
            _entry_date_str = f" on {_odt.day} {_odt.strftime('%B')}"
        except Exception:
            pass

        if entry:
            stale_note = " ⚠️ last known price" if (cur and stale) else ""
            sec.append(f"Entered at: {_fmt_price(entry)}{_entry_date_str}")
            if cur:
                sec.append(f"Current price: {_fmt_price(cur)}{stale_note}")

        # Confidence monitoring — improvement, stability, or deterioration since entry
        if cur_conf_map is not None:
            _entry_conf_f = None
            try:
                _entry_conf_f = int(float(row.get("confidence") or 0)) or None
            except (TypeError, ValueError):
                pass
            _cur_conf_f = cur_conf_map.get(pair)
            if _entry_conf_f and _cur_conf_f is not None:
                _conf_delta_f = _cur_conf_f - _entry_conf_f   # positive = improved
                if _conf_delta_f >= 1:
                    sec.append(
                        f"✅ Trade thesis strengthening — confidence improved from "
                        f"{_entry_conf_f}/10 at entry to {_cur_conf_f}/10 today"
                    )
                elif _conf_delta_f == 0:
                    sec.append(f"Confidence stable at {_entry_conf_f}/10 since entry")
                elif _conf_delta_f == -1:
                    sec.append(
                        f"Confidence eased from {_entry_conf_f}/10 at entry to "
                        f"{_cur_conf_f}/10 today — within normal variation"
                    )
                else:
                    _urg_f = "🚨" if _cur_conf_f < 3 else "⚠️"
                    sec.append(
                        f"{_urg_f} {pair} confidence dropped from {_entry_conf_f}/10 at entry to "
                        f"{_cur_conf_f}/10 today — the conditions that supported this trade have "
                        f"weakened — monitor closely"
                    )
        # Session monitoring reminder for this pair
        try:
            _stl_f     = _session_time_label(pair, now_ak)
            _dom_ccy_f = next(
                (c for c in _SESSION_PRIORITY
                 if c in {pair.replace("/", "")[:3].upper(),
                          pair.replace("/", "")[3:6].upper()}),
                None,
            )
            _sess_nm_f = _SESSION_AUCKLAND.get(_dom_ccy_f, ("London", ""))[0] if _dom_ccy_f else "London"
            if _dom_ccy_f:
                sec.append(
                    f"⏰ Check at {_stl_f} — "
                    f"{_dom_ccy_f} pairs most active during {_sess_nm_f} session"
                )
            else:
                sec.append(f"⏰ Check at {_stl_f}")
        except Exception:
            pass

        # News reminder for open trade
        try:
            from src import economic_calendar as _ec_ot_f
            _ot_evs_f = _ec_ot_f.events_for_pair(pair, hours=48)
            for _ot_ev_f in _ot_evs_f[:2]:
                _ot_ccy_f = _ot_ev_f["currency"]
                _ot_en_f  = _ot_ev_f["plain_name"]
                _ot_ak_f  = _ot_ev_f["ak_display"]
                _ot_hrs_f = _ot_ev_f.get("hours_away", 48)
                _timing_f = "very soon" if _ot_hrs_f <= 6 else "within 24 hours" if _ot_hrs_f <= 24 else "within 48 hours"
                sec.append(
                    f"⚠️ Reminder: {_ot_ccy_f} {_ot_en_f} releases {_timing_f} ({_ot_ak_f}) "
                    f"— directly affects your open {pair} trade"
                )
        except Exception:
            pass

        if stop:
            _stop_verb = "falls" if dirn == "BUY" else "rises"
            sec.append(
                f"Stop loss at: {_fmt_price(stop)} — if price {_stop_verb} here the trade closes automatically with a small loss"
            )
        if target:
            _tgt_verb = "rises" if dirn == "BUY" else "falls"
            sec.append(
                f"Target at: {_fmt_price(target)} — if price {_tgt_verb} here the trade closes automatically with a profit"
            )

        if entry and cur:
            # Compute dollar P&L using risk amount as reference
            pip_sz    = _pip_size(pair)
            raw       = (cur - entry) if dirn == "BUY" else (entry - cur)
            pips      = raw / pip_sz
            net_pips  = pips

            # Dollar P&L — use risk_amount if available, else 1:1 pip/dollar approx
            _risk_a   = 0.0
            _lots     = 0.0
            try:
                from src import risk_manager as _rm_inv2
                _sz2 = _rm_inv2.size_trade(
                    pair=pair, direction=dirn, entry=entry, stop=(stop or entry),
                    target=(target or entry),
                    confidence=int(float(row.get("confidence") or 8)),
                    profile=_rm_profile, risk_state=_rm_state,
                )
                _paper_scale2 = _rm_inv2.FUND_START / max(config.ACCOUNT_BALANCE, 1)
                _lots    = max(round(_sz2["lots"] * _paper_scale2, 2), 0.01)
                _risk_a  = round(_sz2["risk_amount"] * _paper_scale2, 2)
            except Exception:
                pass

            # Compute pip value per pip (dollar per pip)
            _pip_val = 0.0
            if _risk_a > 0 and stop and abs(entry - stop) > 0:
                _stop_pips = abs(entry - stop) / pip_sz
                _pip_val   = _risk_a / _stop_pips if _stop_pips > 0 else 0.0

            _gross_dollar = pips * _pip_val if _pip_val > 0 else None

            # Costs
            _cost_dollar  = 0.0
            _cost_pips    = 0.0
            try:
                from src import trade_costs as _tc_ot
                _ot_costs  = _tc_ot.compute_costs(pair, dirn, entry, float(days_open))
                _cost_pips = _ot_costs["total_cost_pips"]
                net_pips   = pips - _cost_pips
                _cost_dollar = _cost_pips * _pip_val if _pip_val > 0 else 0.0
            except Exception:
                pass

            _net_dollar = _gross_dollar - _cost_dollar if _gross_dollar is not None else None

            sec.append("")
            if _gross_dollar is not None:
                if pips > 2:
                    sec.append(f"Current profit: <b>+${_gross_dollar:.0f}</b> — the trade is moving in the right direction ✅")
                elif pips < -2:
                    sec.append(f"Current loss: <b>-${abs(_gross_dollar):.0f}</b> — the trade is moving against you")
                else:
                    sec.append("At breakeven — the trade is flat, watching for movement")
                if _cost_dollar > 0:
                    sec.append(f"Costs so far: -${_cost_dollar:.0f} spread and swap costs")
                if _net_dollar is not None and _cost_dollar > 0:
                    _net_sign = "+" if _net_dollar >= 0 else ""
                    sec.append(f"Net profit including costs: {_net_sign}${_net_dollar:.0f}")
            elif pips > 2:
                sec.append("Moving in the right direction ✅")
            elif pips < -2:
                sec.append("Moving against you — stay disciplined and let your stop do its job")

            if target and stop:
                try:
                    pct_tgt = pct_stp = 0.0
                    pips_to_target = pips_to_stop = 9999.0
                    if dirn == "BUY" and target > entry and entry > stop:
                        trade_range    = target - entry
                        risk_range     = entry  - stop
                        pct_tgt        = min(100.0, max(0.0, (cur - entry) / trade_range * 100))
                        pct_stp        = min(100.0, max(0.0, (entry - cur) / risk_range  * 100))
                        pips_to_target = (target - cur) / pip_sz
                        pips_to_stop   = (cur - stop)   / pip_sz
                    elif dirn == "SELL" and stop > entry and entry > target:
                        trade_range    = entry  - target
                        risk_range     = stop   - entry
                        pct_tgt        = min(100.0, max(0.0, (entry - cur) / trade_range * 100))
                        pct_stp        = min(100.0, max(0.0, (cur - entry) / risk_range  * 100))
                        pips_to_target = (cur - target) / pip_sz
                        pips_to_stop   = (stop - cur)   / pip_sz

                    bar_filled = int(pct_tgt / 100 * 20)
                    prog_bar   = "█" * bar_filled + "░" * (20 - bar_filled)
                    sec.append(f"Progress: {pct_tgt:.0f}% of the way to target {prog_bar}")

                    # "What to do" advice — most urgent condition first
                    _check_str = _check_line.replace("⏰ <b>", "").replace("</b>", "").replace("⏰ ", "")
                    if 0 < pips_to_target <= 20:
                        sec.append(f"⚠️ <b>What to do: Target almost reached — consider closing your position now to lock in profit</b>")
                    elif 0 < pips_to_stop <= 20:
                        sec.append(f"⚠️ <b>What to do: Stop loss is very close — be prepared. Let your stop do its job automatically</b>")
                    elif pct_tgt >= 75:
                        try:
                            trail_px = (
                                entry + (target - entry) * 0.75 if dirn == "BUY"
                                else entry - (entry - target) * 0.75
                            )
                            sec.append(f"What to do: Move your stop loss to {_fmt_price(trail_px)} to lock in gains — you are 75% of the way there")
                        except Exception:
                            sec.append("What to do: Move your stop to lock in profits — 75% to target")
                    elif pct_tgt >= 50:
                        sec.append(f"What to do: Move your stop loss to {_fmt_price(entry)} (your entry price) to protect your profit")
                        try:
                            halfway_px = (
                                entry + (target - entry) * 0.5 if dirn == "BUY"
                                else entry - (entry - target) * 0.5
                            )
                            sec.append(f"If price hits {_fmt_price(halfway_px)} (halfway to target) — your risk is now zero")
                        except Exception:
                            pass
                    elif days_open > 3:
                        sec.append(f"What to do: Trade has been open for {days_open_str} — if no clear momentum building, consider closing manually")
                    else:
                        sec.append(f"What to do: Nothing — let the trade run. Check again at {_check_str}")
                        try:
                            halfway_px2 = (
                                entry + (target - entry) * 0.5 if dirn == "BUY"
                                else entry - (entry - target) * 0.5
                            )
                            sec.append(
                                f"If price hits {_fmt_price(halfway_px2)} (halfway to target) — "
                                f"move your stop loss up to {_fmt_price(entry)} to protect your profit"
                            )
                        except Exception:
                            pass
                except Exception:
                    sec.append(_check_line)
            else:
                sec.append(_check_line)

        elif entry:
            if stop:
                _sv = "falls" if dirn == "BUY" else "rises"
                sec.append(f"Stop loss at: {_fmt_price(stop)} — if price {_sv} here the trade closes with a small loss")
            if target:
                _tv = "rises" if dirn == "BUY" else "falls"
                sec.append(f"Target at: {_fmt_price(target)} — if price {_tv} here the trade closes with a profit")
            sec.append("Current price: ⚠️ price unavailable — check your broker app")
            sec.append(f"What to do: Check again at {_check_line.replace('⏰ <b>','').replace('</b>','').replace('⏰ ','')}")
        else:
            sec.append("Trade details unavailable")

        if days_open > 0:
            if remaining <= 2:
                try:
                    _exp_date = (now_ak + timedelta(days=remaining)).strftime("%-d %B")
                except ValueError:
                    _exp_date = (now_ak + timedelta(days=remaining)).strftime("%d %B").lstrip("0")
                sec.append(
                    f"⚠️ This trade expires in {remaining} day{'s' if remaining != 1 else ''} — "
                    f"if target not reached by {_exp_date} it closes automatically at current price"
                )
            else:
                sec.append(f"Opened: {days_open_str} ago | Expires in: {expires_str}")

    return sec


# ── Drawdown-aware trade filter ───────────────────────────────────────────────

def _dd_allows_trade(r: dict, dd_mode: str, quality_grades: dict,
                     conf_threshold: int) -> bool:
    """Return True if the active drawdown protection tier permits this trade.

    Tier rules:
      halt         — no new trades under any circumstances
      preservation — A-grade only + all 3 core timeframes aligned
      defensive    — A-grade only
      caution      — A or B grade only
      normal       — A, B, or (C with effective confidence >= threshold)
    """
    grade = (quality_grades.get(r["pair"]) or {}).get("grade", "F")
    if dd_mode == "halt":
        return False
    if dd_mode == "preservation":
        if grade != "A":
            return False
        mtf = (r.get("bundle") or {}).get("mtf") or {}
        return mtf.get("agreeing_count", 0) >= 3
    if dd_mode == "defensive":
        return grade == "A"
    if dd_mode == "caution":
        return grade in ("A", "B")
    # normal
    return (grade in ("A", "B") or
            (grade == "C" and _eff_conf(r) >= conf_threshold))


# ── Main summary builder ───────────────────────────────────────────────────────

def _build_system_learning_report(date: str) -> list:
    """Build the SYSTEM LEARNING REPORT — Monday 6am scans only.

    Six sections: win rate trend, ML accuracy trend, MFE trend,
    best/worst pairs, confidence calibration, and overall verdict.
    Returns an empty list on any non-Monday day or when data is absent.
    """
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return []
    if dt.weekday() != 0:   # 0 = Monday
        return []

    sec = [
        "", "━━━━━━━━━━━━━━━━━━━━━",
        "🎓 <b>SYSTEM LEARNING REPORT</b>",
        "Is the system genuinely improving over time?",
    ]

    try:
        from src import research_tracker as _rtrk_lr
        rows = _rtrk_lr.load()
    except Exception:
        return []

    if not rows:
        sec.append("No research trades yet — report will appear once data is collected.")
        return sec

    def _f(v):
        try:
            return float(v) if v not in ("", None) else None
        except (TypeError, ValueError):
            return None

    decisive = [r for r in rows if r.get("status") in ("WIN", "LOSS")]
    n_dec    = len(decisive)

    def _trade_sort(r):
        return (r.get("date", ""), str(r.get("id", "")).zfill(6))

    dec_sorted = sorted(decisive, key=_trade_sort)
    any_added  = False

    # ── 1. WIN RATE TREND ──────────────────────────────────────────────────────
    if n_dec >= 20:
        groups = []
        for i in range(0, n_dec, 10):
            grp  = dec_sorted[i:i + 10]
            wins = sum(1 for r in grp if r.get("status") == "WIN")
            wr   = round(wins / len(grp) * 100) if grp else 0
            groups.append((i + 1, i + len(grp), wr))

        trend_str = " · ".join(f"Trades {s}-{e}: {w}%" for s, e, w in groups)
        improving = len(groups) >= 2 and groups[-1][2] > groups[0][2] + 4

        sec += ["", "<b>1. WIN RATE TREND</b>", trend_str]
        if improving:
            sec.append("✅ Win rate improving — system is learning")
        else:
            sec.append("⚠️ Win rate not improving — review needed")
        any_added = True

    # ── 2. ML ACCURACY TREND ──────────────────────────────────────────────────
    try:
        from src import ml_predictor as _mlp_lr
        _ml_meta  = _mlp_lr._load_meta()
        _acc_hist = _ml_meta.get("accuracy_history", [])
        if len(_acc_hist) >= 2:
            _recent   = _acc_hist[-4:]
            _acc_strs = [
                f"Week of {e.get('trained_at', '')[:10]}: {e.get('roc_auc', 0) * 100:.0f}%"
                for e in _recent
            ]
            _first_roc = _recent[0].get("roc_auc", 0.0)
            _last_roc  = _recent[-1].get("roc_auc", 0.0)
            sec += ["", "<b>2. ML ACCURACY TREND</b>", " · ".join(_acc_strs)]
            if _last_roc > _first_roc + 0.03:
                sec.append(
                    f"✅ ML model getting smarter — accuracy improved from "
                    f"{_first_roc * 100:.0f}% to {_last_roc * 100:.0f}% this week"
                )
            elif _last_roc < _first_roc - 0.05:
                sec.append(
                    f"⚠️ ML model may be overfitting — "
                    f"accuracy dropped from {_first_roc * 100:.0f}% to {_last_roc * 100:.0f}% — more data needed"
                )
            else:
                sec.append(
                    f"ML model stable — accuracy at {_last_roc * 100:.0f}% "
                    f"({_ml_meta.get('n_trades', '?')} trades)"
                )
            any_added = True
        elif _ml_meta.get("model_ready"):
            _roc = _ml_meta.get("roc_auc", 0)
            _n   = _ml_meta.get("n_trades", 0)
            sec += [
                "", "<b>2. ML ACCURACY TREND</b>",
                f"ML model active — prediction accuracy {_roc * 100:.0f}% on {_n} trades",
                "Trend will show after 2+ weekly retrains",
            ]
            any_added = True
    except Exception:
        pass

    # ── ML HEALTH CHECK ───────────────────────────────────────────────────────
    # Plain-English verdict on whether the model finds real patterns or memorises.
    try:
        from src import ml_predictor as _mlp_hc
        _hc = _mlp_hc._load_meta()
        if _hc.get("model_ready") and _hc.get("overfit_gap") is not None:
            _hc_roc    = _hc.get("roc_auc", 0.0)
            _hc_hold   = _hc.get("temporal_holdout_auc", 0.0)
            _hc_gap    = _hc.get("overfit_gap", 0.0)
            _hc_hlthy  = _hc.get("is_healthy", True)
            _hc_p_wr   = _hc.get("period_win_rates") or []
            _hc_stbl   = _hc.get("period_stable")
            _hc_consec = _hc.get("n_consecutive_reliable", 0)
            _hc_folds  = _hc.get("cv_fold_scores") or []

            sec.append("")
            sec.append("<b>ML HEALTH CHECK</b>")

            # Per-fold cross-validation scores (the true expected accuracy on new trades)
            if _hc_folds:
                _fold_strs = [f"fold {i+1} accuracy {round(s*100)}%" for i, s in enumerate(_hc_folds)]
                _avg_fold  = round(sum(_hc_folds) / len(_hc_folds) * 100, 1)
                sec.append(
                    f"ML cross-validation: {' · '.join(_fold_strs)} · average {_avg_fold}% — "
                    f"this is the true expected accuracy on new trades"
                )

            # Plain-English holdout verdict
            _hold_pct = round(_hc_hold * 100)
            _train_pct = round(_hc_roc * 100)
            if _hc_hlthy and _hc_gap < 0.05:
                sec.append(
                    f"✅ The AI model is healthy — {_hold_pct}% accuracy on trades it has never seen "
                    f"(matches training accuracy of {_train_pct}%) — genuine patterns found, not memorised."
                )
            elif _hc_hlthy:
                sec.append(
                    f"🟡 The AI model is learning — {_hold_pct}% accuracy on new trades "
                    f"vs {_train_pct}% on training data — small gap is normal for this dataset size."
                )
            else:
                sec.append(
                    f"⚠️ The AI model is memorising, not learning — {_train_pct}% accuracy on "
                    f"training data drops to {_hold_pct}% on new trades — more data needed before "
                    f"predictions can be trusted."
                )

            # Reliability gate status
            if _hc_consec >= 3:
                sec.append(
                    f"✅ Model reliability confirmed — {_hc_consec} consecutive retrains with "
                    f"holdout accuracy ≥ 65% — AI predictions are now considered reliable."
                )
            else:
                _needed = 3 - _hc_consec
                sec.append(
                    f"🤖 ML model active but accuracy not yet reliable — predictions shown for "
                    f"information only — not yet influencing confidence scores "
                    f"(need {_needed} more retrain{'s' if _needed != 1 else ''} with ≥ 65% holdout accuracy)"
                )

            if len(_hc_p_wr) == 3:
                _wr_strs = [f"{r * 100:.0f}%" for r in _hc_p_wr]
                if _hc_stbl:
                    sec.append(
                        f"✅ Data distribution stable across 3 time periods "
                        f"({' / '.join(_wr_strs)} win rates)"
                    )
                else:
                    sec.append(
                        f"⚠️ Win rate shifted between periods "
                        f"({' / '.join(_wr_strs)}) — historical patterns may not "
                        f"reflect current market conditions"
                    )
            any_added = True
        elif _hc.get("model_ready"):
            sec += [
                "", "<b>ML HEALTH CHECK</b>",
                "Health check will appear once 30+ closed trades are recorded.",
            ]
            any_added = True
    except Exception:
        pass

    # ── 3. MFE TREND ──────────────────────────────────────────────────────────
    closed_all  = [
        r for r in rows
        if r.get("status") in ("WIN", "LOSS", "BREAKEVEN", "EXPIRED", "PARTIAL_WIN")
    ]
    mfe_pairs   = [(r, _f(r.get("mfe_pips"))) for r in closed_all
                   if _f(r.get("mfe_pips")) is not None]
    mfe_sorted  = sorted(mfe_pairs, key=lambda x: _trade_sort(x[0]))

    if len(mfe_sorted) >= 40:
        mfe_groups = []
        for i in range(0, len(mfe_sorted), 20):
            grp = mfe_sorted[i:i + 20]
            avg = round(sum(v for _, v in grp) / len(grp)) if grp else 0
            mfe_groups.append((i + 1, i + len(grp), avg))

        mfe_str = " · ".join(
            f"Trades {s}-{e}: {avg}p avg MFE" for s, e, avg in mfe_groups
        )
        sec += ["", "<b>3. MFE TREND</b>", mfe_str]
        if len(mfe_groups) >= 2 and mfe_groups[-1][2] > mfe_groups[0][2] + 5:
            sec.append(
                f"✅ Entry quality improving — average MFE increased from "
                f"{mfe_groups[0][2]} pips to {mfe_groups[-1][2]} pips over last 20 trades"
            )
        else:
            sec.append("Entry quality consistent across recent trades")
        any_added = True

    # ── TARGET CALIBRATION ANALYSIS ───────────────────────────────────────────
    # Runs always (not gated on trade count) so it shows from the first Monday.
    try:
        def _pip_sz(pair_str: str) -> float:
            cl = pair_str.upper().replace("/", "").replace("-", "")
            if len(cl) >= 6:
                if cl[3:6] == "JPY": return 0.01
                if cl[:3] == "JPY": return 0.000001
            return 0.01 if "JPY" in pair_str.upper() else 0.0001

        _tc_closed = [
            r for r in rows
            if r.get("status") in ("WIN", "LOSS", "EXPIRED", "PARTIAL_WIN")
            and r.get("entry") and r.get("stop_loss") and r.get("target") and r.get("pips")
        ]
        _tc_results = []
        for _tcr in _tc_closed:
            try:
                _tc_entry  = float(_tcr["entry"])
                _tc_stop   = float(_tcr["stop_loss"])
                _tc_target = float(_tcr["target"])
                _tc_pips   = float(_tcr["pips"])
                _tc_ps     = _pip_sz(_tcr["pair"])
                _tc_stop_d = abs(_tc_entry - _tc_stop) / _tc_ps
                _tc_tgt_d  = abs(_tc_target - _tc_entry) / _tc_ps
                if _tc_stop_d <= 0 or _tc_tgt_d <= 0 or _tc_stop_d > 200 or _tc_tgt_d > 500:
                    continue
                _tc_results.append({
                    "status":       _tcr["status"],
                    "stop_pips":    round(_tc_stop_d, 1),
                    "target_pips":  round(_tc_tgt_d, 1),
                    "achieved":     round(_tc_pips, 1),
                    "mfe":          _f(_tcr.get("mfe_pips")),
                })
            except (TypeError, ValueError, ZeroDivisionError):
                pass

        if _tc_results:
            _tc_exp     = [r for r in _tc_results if r["status"] == "EXPIRED"]
            _tc_exp_pos = [r for r in _tc_exp if r["achieved"] > 0]
            _tc_all_tgt = [r["target_pips"] for r in _tc_results]
            _tc_all_stp = [r["stop_pips"] for r in _tc_results]
            _tc_mfe_raw = [r["mfe"] for r in _tc_results if r["mfe"] is not None and r["mfe"] > 0]

            _tc_avg_tgt = sum(_tc_all_tgt) / len(_tc_all_tgt) if _tc_all_tgt else 0
            _tc_avg_stp = sum(_tc_all_stp) / len(_tc_all_stp) if _tc_all_stp else 0
            _tc_avg_mfe = sum(_tc_mfe_raw) / len(_tc_mfe_raw) if _tc_mfe_raw else 0
            _tc_avg_ach = (
                sum(r["achieved"] for r in _tc_exp_pos) / len(_tc_exp_pos)
                if _tc_exp_pos else 0
            )

            # Old multiplier (from data) vs new
            _tc_old_mult = round(_tc_avg_tgt / _tc_avg_stp, 1) if _tc_avg_stp > 0 else 2.0
            _tc_new_mult = 1.0

            # Projected WIN conversion: EXPIRED positives that would WIN at new target
            _tc_new_tgt_p  = _tc_avg_stp * _tc_new_mult   # pips at 1.0x stop
            _tc_conv_would = sum(1 for r in _tc_exp_pos if r["achieved"] >= _tc_new_tgt_p)
            _tc_conv_pct   = (_tc_conv_would / len(_tc_exp_pos) * 100) if _tc_exp_pos else 0

            # PARTIAL_WIN trades that would become full WIN at new target
            _tc_pw_conv = sum(
                1 for r in _tc_results
                if r["status"] == "PARTIAL_WIN" and r["achieved"] >= _tc_new_tgt_p
            )

            # Current decisive rate (WIN+LOSS out of all closed)
            _tc_total  = len(_tc_results)
            _tc_dec    = sum(1 for r in _tc_results if r["status"] in ("WIN", "LOSS"))
            _tc_dec_pct = _tc_dec / _tc_total * 100 if _tc_total else 0
            _tc_proj_dec = _tc_dec + _tc_conv_would + _tc_pw_conv
            _tc_proj_pct = _tc_proj_dec / _tc_total * 100 if _tc_total else 0

            sec += ["", "<b>TARGET CALIBRATION ANALYSIS</b>"]
            if _tc_avg_mfe > 0:
                sec.append(
                    f"Average MFE: {_tc_avg_mfe:.0f} pips — "
                    f"average stop: {_tc_avg_stp:.0f} pips — "
                    f"average target: {_tc_avg_tgt:.0f} pips ({_tc_old_mult}x stop)"
                )
            else:
                sec.append(
                    f"Average stop: {_tc_avg_stp:.0f} pips — "
                    f"average target: {_tc_avg_tgt:.0f} pips ({_tc_old_mult}x stop)"
                )
            if _tc_exp_pos:
                sec.append(
                    f"EXPIRED trades moved avg {_tc_avg_ach:.0f} pips in right direction "
                    f"before expiry — target of {_tc_avg_tgt:.0f} pips was too far"
                )
            sec.append(
                f"Research target reduced: {_tc_old_mult}x ATR → {_tc_new_mult}x ATR "
                f"(fund trade targets unchanged)"
            )
            sec.append(
                f"Projected improvement: {_tc_dec} decisive outcomes → "
                f"~{_tc_proj_dec} decisive outcomes "
                f"({_tc_dec_pct:.0f}% → {_tc_proj_pct:.0f}% decisive rate)"
            )
            if _tc_conv_would > 0 or _tc_pw_conv > 0:
                sec.append(
                    f"✅ ~{_tc_conv_would} expired trades + {_tc_pw_conv} partial wins "
                    f"would convert to full WIN with tighter research target"
                )
            any_added = True
    except Exception:
        pass

    # ── ML TRAINING QUALITY REPORT ─────────────────────────────────────────────
    try:
        from src import ml_predictor as _mlp_bal
        _bal_meta   = _mlp_bal._load_meta()
        _bal_wins   = _bal_meta.get("n_wins_training")
        _bal_loss   = _bal_meta.get("n_losses_training")
        _bal_n      = _bal_meta.get("n_trades")
        _bal_biased = _bal_meta.get("is_biased", False)
        _bal_lpct   = _bal_meta.get("prediction_loss_pct")
        _bal_rw     = _bal_meta.get("n_real_wins")
        _bal_synth  = _bal_meta.get("n_synthetic_wins", 0)
        _bal_smote  = _bal_meta.get("smote_applied", False)
        _bal_soft   = _bal_meta.get("soft_labels_applied", False)
        _bal_nix    = _bal_meta.get("n_interaction_features", 0)
        _bal_hold   = _bal_meta.get("temporal_holdout_auc")
        _acc_hist   = _bal_meta.get("accuracy_history", [])
        _prev_hold  = _acc_hist[-2].get("roc_auc") if len(_acc_hist) >= 2 else None

        sec += ["", "<b>ML TRAINING QUALITY REPORT</b>"]
        if _bal_wins is not None and _bal_loss is not None and _bal_n is not None:
            if _bal_biased:
                sec.append(
                    f"⚠️ ML model biased — predicting LOSS on {_bal_lpct}% of training "
                    f"examples — class weights applied"
                )
            # SMOTE breakdown
            if _bal_smote and _bal_rw is not None and _bal_synth:
                _bal_r = round(_bal_loss / _bal_wins, 2) if _bal_wins > 0 else None
                _bal_r_str = f"{_bal_r:.2f}:1" if _bal_r else "?:1"
                sec.append(
                    f"ML training: {_bal_rw} genuine WIN trades + {_bal_synth} SMOTE "
                    f"synthetic WIN trades = {_bal_wins} effective WIN examples vs "
                    f"{_bal_loss} LOSS — class ratio {_bal_r_str} — "
                    f"model can now find genuine WIN patterns"
                )
            else:
                _bal_r = round(_bal_loss / _bal_wins, 1) if _bal_wins and _bal_wins > 0 else None
                _bal_r_str = f"{_bal_r}:1" if _bal_r else "?:1"
                _smote_note = " — SMOTE not applied (need imbalanced-learn)" if not _bal_smote else ""
                sec.append(
                    f"ML training data: {_bal_n} trades ({_bal_wins} WIN + {_bal_loss} LOSS) "
                    f"— ratio {_bal_r_str}{_smote_note}"
                )
            # Soft labels indicator
            if _bal_soft:
                sec.append(
                    "✅ Soft label weighting active — WIN=1.0, PARTIAL_WIN=0.7, "
                    "LOSS=1.0, EXPIRED weighted by target proximity (0.3–0.85)"
                )
            # Interaction features indicator
            if _bal_nix:
                sec.append(
                    f"✅ {_bal_nix} feature interaction terms added — "
                    "hhhl×monthly, rsi×fib, momentum×cot, killzone×confluence, ccy_strength×trend"
                )
            # Effective training set and AUC comparison
            _prev_auc_str = f"{round(_prev_hold * 100):.0f}%" if _prev_hold else "—"
            _curr_auc_str = f"{round(_bal_hold * 100):.0f}%" if _bal_hold else "—"
            sec.append(
                f"Effective training set: {_bal_n} examples — "
                f"holdout AUC: previous {_prev_auc_str} → current {_curr_auc_str}"
            )
            if _bal_r and _bal_r <= 3.0:
                sec.append("✅ Class ratio well balanced — model learning genuine patterns")
            elif _bal_r:
                sec.append(f"Class ratio {_bal_r_str} — class weights + SMOTE correcting imbalance")
        else:
            _raw_pw = sum(1 for r in rows if r.get("status") in ("WIN", "PARTIAL_WIN"))
            _raw_ls = sum(1 for r in rows if r.get("status") == "LOSS")
            sec.append(
                f"Model not yet trained — {_raw_pw} WIN/PARTIAL_WIN + {_raw_ls} LOSS recorded "
                f"— SMOTE + soft labels will activate on next training run"
            )
        any_added = True
    except Exception:
        pass

    # ── LEARNING VELOCITY ─────────────────────────────────────────────────────
    try:
        from datetime import timedelta as _lv_td
        _lv_cutoff = (datetime.now(timezone.utc) - _lv_td(days=7)).strftime("%Y-%m-%d")
        _lv_new = [
            r for r in rows
            if r.get("status") in ("WIN", "LOSS", "PARTIAL_WIN")
            and (r.get("closed_at") or r.get("date") or "")[:10] >= _lv_cutoff
        ]
        _lv_count = len(_lv_new)
        sec += ["", "<b>LEARNING VELOCITY</b>"]
        if _lv_count >= 5:
            sec.append(
                f"✅ This week {_lv_count} new decisive outcomes added — "
                f"learning velocity strong"
            )
        elif _lv_count >= 2:
            sec.append(
                f"This week {_lv_count} new decisive outcomes added — "
                f"learning velocity improving"
            )
        else:
            sec.append(
                f"⚠️ This week {_lv_count} new decisive outcome"
                f"{'s' if _lv_count != 1 else ''} added — learning too slow — "
                f"target calibration should improve this automatically"
            )
        any_added = True
    except Exception:
        pass

    # ── CASCADING TARGET PERFORMANCE ─────────────────────────────────────────
    try:
        _casc_rows = [
            r for r in rows
            if r.get("status") in ("WIN", "FULL_WIN", "LOSS", "PARTIAL_WIN")
            and r.get("cascading_total_pips_weighted") not in (None, "")
        ]
        if _casc_rows:
            _cn_fw  = sum(1 for r in _casc_rows if r.get("status") == "FULL_WIN")
            _cn_win = sum(1 for r in _casc_rows if r.get("status") == "WIN")
            _cn_pw  = sum(1 for r in _casc_rows if r.get("status") == "PARTIAL_WIN")
            _cn_ls  = sum(1 for r in _casc_rows if r.get("status") == "LOSS")
            _cn_tot = len(_casc_rows)
            _cn_pos = _cn_fw + _cn_win + _cn_pw
            _casc_pips_total = sum(
                float(r.get("cascading_total_pips_weighted") or 0)
                for r in _casc_rows
            )
            _casc_wr = round(_cn_pos / _cn_tot * 100) if _cn_tot else 0
            # Retroactive: how many old trades would have hit T1/T2?
            _old_rows = [
                r for r in rows
                if r.get("status") in ("WIN", "LOSS", "PARTIAL_WIN", "EXPIRED")
                and r.get("cascading_total_pips_weighted") in (None, "")
                and r.get("entry") and r.get("stop_loss") and r.get("mfe_pips")
            ]
            _retro_t1 = _retro_t2 = 0
            for _rr in _old_rows:
                try:
                    _rr_mfe = float(_rr.get("mfe_pips") or 0)
                    _rr_e   = float(_rr.get("entry") or 0)
                    _rr_s   = float(_rr.get("stop_loss") or 0)
                    _rr_dir = (_rr.get("direction") or "").upper()
                    _rr_ps  = 0.01 if "JPY" in _rr.get("pair", "") else 0.0001
                    _rr_atr = abs(_rr_e - _rr_s) / _rr_ps  # stop distance in pips
                    if _rr_atr > 0:
                        if _rr_mfe >= 0.4 * _rr_atr:
                            _retro_t1 += 1
                        if _rr_mfe >= 0.7 * _rr_atr:
                            _retro_t2 += 1
                except Exception:
                    pass
            sec += ["", "<b>CASCADING TARGET PERFORMANCE</b>"]
            sec.append(
                f"Cascade-tracked trades: {_cn_tot} — "
                f"{_cn_fw} FULL_WIN · {_cn_win} WIN · {_cn_pw} PARTIAL_WIN · {_cn_ls} LOSS "
                f"— {_casc_wr}% profitable outcomes — {_casc_pips_total:.0f}p total (weighted)"
            )
            if _old_rows:
                _r1_pct = round(_retro_t1 / len(_old_rows) * 100) if _old_rows else 0
                _r2_pct = round(_retro_t2 / len(_old_rows) * 100) if _old_rows else 0
                sec.append(
                    f"Retroactive (MFE analysis): {len(_old_rows)} historical trades — "
                    f"{_retro_t1} ({_r1_pct}%) would have banked T1 (+0.4×ATR) · "
                    f"{_retro_t2} ({_r2_pct}%) would have reached T2 (+0.7×ATR)"
                )
            any_added = True
    except Exception:
        pass

    # ── MONITOR PERFORMANCE (past 7 days) ─────────────────────────────────────
    try:
        from src.monitor import build_monitor_weekly_report as _mon_weekly_rpt
        _mon_log_lr = config.DATA_DIR / "monitor_log.json"
        _mon_last_str = "unknown"
        if _mon_log_lr.exists():
            try:
                _mon_lr     = json.loads(_mon_log_lr.read_text(encoding="utf-8"))
                _mon_last_ts = _mon_lr.get("timestamp", "")
                _mon_last_str = _mon_last_ts[:16].replace("T", " ") if _mon_last_ts else "unknown"
            except Exception:
                pass

        # Weekly milestone stats from closed trades in last 7 days
        _mon_cutoff   = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=7)).strftime("%Y-%m-%d")
        _mon_week_rows = [
            r for r in rows
            if r.get("closed_at", "")[:10] >= _mon_cutoff
            and r.get("status") in ("WIN", "FULL_WIN", "PARTIAL_WIN", "LOSS")
        ]
        _mon_t1_week   = sum(1 for r in _mon_week_rows if str(r.get("t1_hit", "")).upper() == "TRUE")
        _mon_t2_week   = sum(1 for r in _mon_week_rows if str(r.get("t2_hit", "")).upper() == "TRUE")
        _mon_wp_total  = sum(
            float(r.get("cascading_total_pips_weighted") or 0)
            for r in _mon_week_rows
            if r.get("cascading_total_pips_weighted") not in (None, "")
        )
        _mon_mfe_ct = sum(
            1 for r in rows
            if r.get("status") == "OPEN"
            and r.get("mfe_pips") not in (None, "")
            and float(r.get("mfe_pips") or 0) > 0
        )

        # Get the weekly summary from monitor_history.json
        _weekly_lines = _mon_weekly_rpt().splitlines()

        sec += ["", "<b>MONITOR PERFORMANCE THIS WEEK</b>"]
        sec.append(f"Last run: {_mon_last_str} UTC")
        for _wl in _weekly_lines[1:]:   # skip the header line
            sec.append(_wl.strip())

        if _mon_t1_week + _mon_t2_week > 0:
            sec.append(
                f"T1 milestones banked: {_mon_t1_week} trades — "
                f"T2 milestones banked: {_mon_t2_week} trades"
            )
            if _mon_wp_total > 0:
                sec.append(f"Additional weighted pips captured: +{_mon_wp_total:.0f}p total")

        if _mon_mfe_ct > 0:
            sec.append(f"MFE/MAE tracking active on {_mon_mfe_ct} open research trade(s)")

        any_added = True
    except Exception:
        pass

    # ── 4. BEST AND WORST PAIRS ────────────────────────────────────────────────
    pair_stats: dict = {}
    for r in decisive:
        p  = r.get("pair", "?")
        ps = pair_stats.setdefault(p, {"wins": 0, "total": 0})
        ps["total"] += 1
        if r.get("status") == "WIN":
            ps["wins"] += 1

    qualified = {p: v for p, v in pair_stats.items() if v["total"] >= 3}
    if qualified:
        def _pwr(v):
            return v["wins"] / v["total"] if v["total"] else 0

        by_wr   = sorted(qualified.items(), key=lambda x: (_pwr(x[1]), x[1]["total"]), reverse=True)
        best3   = by_wr[:3]
        worst3  = [x for x in by_wr[-3:] if x not in best3]

        sec.append("")
        sec.append("<b>4. BEST AND WORST PAIRS</b>")
        if best3:
            sec.append("✅ Best pairs: " + " · ".join(
                f"{p} {int(_pwr(v) * 100)}%" for p, v in best3
            ))
        if worst3:
            sec.append("❌ Weakest pairs: " + " · ".join(
                f"{p} {int(_pwr(v) * 100)}%" for p, v in worst3
            ))
        any_added = True

    # ── 5. CONFIDENCE CALIBRATION ──────────────────────────────────────────────
    conf_stats: dict = {}
    for r in decisive:
        try:
            cv = int(float(r.get("confidence", "") or 0))
        except (TypeError, ValueError):
            continue
        if cv < 5:
            continue
        cs = conf_stats.setdefault(cv, {"wins": 0, "total": 0})
        cs["total"] += 1
        if r.get("status") == "WIN":
            cs["wins"] += 1

    conf_q = {k: v for k, v in conf_stats.items() if v["total"] >= 3}
    _cal_checked = False
    _is_calibrated = False
    if len(conf_q) >= 2:
        def _cwr(v):
            return v["wins"] / v["total"] if v["total"] else 0

        conf_levels = sorted(conf_q.keys())
        conf_wrs    = [_cwr(conf_q[c]) for c in conf_levels]
        # Allow up to 5% tolerance for natural variance
        _is_calibrated = all(
            conf_wrs[i] <= conf_wrs[i + 1] + 0.05
            for i in range(len(conf_wrs) - 1)
        )
        _cal_checked = True
        conf_strs = [f"Conf {c}: {int(_cwr(conf_q[c]) * 100)}%" for c in conf_levels]
        sec += ["", "<b>5. CONFIDENCE CALIBRATION</b>", " · ".join(conf_strs)]
        if _is_calibrated:
            sec.append("✅ Confidence scoring is working")
        else:
            sec.append("⚠️ Confidence scoring needs review")
        any_added = True

    # ── DATA QUALITY REPORT ───────────────────────────────────────────────────
    try:
        from src import data_quality as _dq_lr
        _dq_lr_lines = _dq_lr.weekly_report(_dq_lr.load_state())
        if _dq_lr_lines:
            sec.extend(_dq_lr_lines)
            any_added = True
    except Exception:
        pass

    # ── 6. OVERALL LEARNING VERDICT ────────────────────────────────────────────
    if any_added or n_dec >= 10:
        parts = []
        if n_dec < 10:
            parts.append(
                f"The system has {n_dec} decisive trade outcome{'s' if n_dec != 1 else ''} so far. "
                "Too early to draw conclusions — a minimum of 20 decisive trades is needed before "
                "patterns become meaningful. Keep collecting data."
            )
        else:
            _tot_wins  = sum(1 for r in decisive if r.get("status") == "WIN")
            _overall_wr = _tot_wins / n_dec * 100 if n_dec else 0
            parts.append(
                f"The system has {n_dec} decisive trade outcomes with an overall win rate of "
                f"{_overall_wr:.0f}%."
            )
            if n_dec >= 20:
                _fw = sum(1 for r in dec_sorted[:10] if r.get("status") == "WIN") / 10 * 100
                _lw = sum(1 for r in dec_sorted[-10:] if r.get("status") == "WIN") / 10 * 100
                if _lw > _fw + 4:
                    parts.append(
                        f"Win rate improved from {_fw:.0f}% in the first 10 trades to "
                        f"{_lw:.0f}% in the most recent 10 — the system is learning."
                    )
                else:
                    parts.append(
                        f"Win rate has been {_fw:.0f}% early vs {_lw:.0f}% recently — "
                        "no clear improvement trend yet."
                    )
            if _cal_checked:
                if _is_calibrated:
                    parts.append("Confidence scoring is working as intended.")
                else:
                    parts.append(
                        "Confidence scoring is not yet well calibrated — higher confidence "
                        "setups are not reliably winning more often."
                    )
            if _overall_wr >= 50:
                parts.append(
                    "Overall verdict: system is performing above breakeven — continue operating "
                    "with current settings."
                )
            elif n_dec >= 30:
                parts.append(
                    "Overall verdict: win rate below 50% over 30+ trades — consider reviewing "
                    "the entry criteria for the weakest pairs."
                )
            else:
                parts.append(
                    "Overall verdict: system is still in early learning phase — "
                    "no strategy changes recommended until at least 30 decisive trades are recorded."
                )
        sec += ["", "<b>6. OVERALL LEARNING VERDICT</b>", " ".join(parts)]

    # ── FUND PROTECTION BLOCKED OPPORTUNITIES ─────────────────────────────────
    try:
        from src import fund_state as _fs_lr
        _fs_lr_state = _fs_lr.load()
        _fs_lr_lines = _fs_lr.build_blocked_opportunities_section(_fs_lr_state)
        if _fs_lr_lines:
            sec.extend(_fs_lr_lines)
            any_added = True
    except Exception:
        pass

    # ── MISSED OPPORTUNITY DETECTION STATS ────────────────────────────────────
    try:
        _ma_path = config.DATA_DIR / "movement_alerts.json"
        _wl_path = config.DATA_DIR / "watchlist_cache.json"
        _ma_weekly: dict = {}
        _wl_weekly: dict = {}
        if _ma_path.exists():
            _ma_weekly = json.loads(_ma_path.read_text(encoding="utf-8")).get("weekly_stats", {})
        if _wl_path.exists():
            _wl_weekly = json.loads(_wl_path.read_text(encoding="utf-8")).get("watchlist_weekly_stats", {})

        _ma_det   = int(_ma_weekly.get("movement_alerts_detected", 0))
        _ma_batch = int(_ma_weekly.get("already_in_batch",         0))
        _ma_swept = int(_ma_weekly.get("outside_batch_swept",      0))
        _ma_sigs  = int(_ma_weekly.get("swept_signals_found",      0))
        _ma_trade = int(_ma_weekly.get("swept_became_trades",      0))
        _wl_sent  = int(_wl_weekly.get("alerts_sent",              0))
        _wl_promo = int(_wl_weekly.get("promoted_to_deep",         0))
        _wl_trade = int(_wl_weekly.get("became_trade_alerts",      0))
        _est_missed = _ma_trade + _wl_trade

        if _ma_det > 0 or _wl_sent > 0:
            _batch_pct = f" ({round(_ma_batch / _ma_det * 100)}%)" if _ma_det else ""
            sec += [
                "", "<b>7. MISSED OPPORTUNITY DETECTION THIS WEEK</b>",
                f"Movement alerts detected: {_ma_det}",
                f"Already in analysis batch: {_ma_batch}{_batch_pct}",
                f"Outside batch — swept: {_ma_swept}",
                f"Of swept pairs — signals found: {_ma_sigs}",
                f"Of swept pairs — became trade alerts: {_ma_trade}",
                "",
                f"Watch list movement alerts sent: {_wl_sent}",
                f"Watch list pairs promoted to deep analysis: {_wl_promo}",
                f"Of those — became trade alerts: {_wl_trade}",
                "",
                f"Estimated opportunities missed without these systems: {_est_missed}",
            ]
            any_added = True
    except Exception:
        pass

    # ── THRESHOLD PERFORMANCE THIS WEEK ───────────────────────────────────────
    try:
        from src import dynamic_threshold as _dth_lr
        _thr_week_lines = _dth_lr.build_weekly_report_lines()
        if _thr_week_lines:
            sec.extend(_thr_week_lines)
            any_added = True
    except Exception:
        pass

    # ── ADAPTIVE TARGET SYSTEM ─────────────────────────────────────────────────
    try:
        from src import pair_statistics as _pstat_lr
        _pstat_lines = _pstat_lr.build_monday_report_lines()
        if _pstat_lines:
            sec.extend(_pstat_lines)
            any_added = True
    except Exception:
        pass

    # ── FILTER EFFECTIVENESS ───────────────────────────────────────────────────
    try:
        from src import filter_effectiveness as _fe_lr
        _fe_lines = _fe_lr.build_monday_report_lines()
        if _fe_lines:
            sec.extend(_fe_lines)
            any_added = True
    except Exception:
        pass

    # ── REGIME + TIME-OF-DAY WIN RATES ────────────────────────────────────────
    try:
        from src import regime_learning as _rl_lr
        _rl_lines = _rl_lr.build_monday_report_lines()
        if _rl_lines:
            sec.extend(_rl_lines)
            any_added = True
    except Exception:
        pass

    # ── ONLINE LEARNER STATUS ─────────────────────────────────────────────────
    try:
        from src import online_learner as _ol_lr
        _ol_n = _ol_lr.get_n_decisive()
        if _ol_n > 0:
            sec.append("")
            sec.append(f"<b>CONTINUOUS LEARNING</b>: {_ol_lr.build_status_line()}")
            any_added = True
    except Exception:
        pass

    # ── MONITOR DATA QUALITY ──────────────────────────────────────────────────
    try:
        from src import monitor as _mon_lr
        _mq_lines = _mon_lr.build_monitor_data_quality_report()
        if _mq_lines:
            sec.extend(_mq_lines)
            any_added = True
    except Exception:
        pass

    # ── MONITOR SOURCE RELIABILITY (7-day) ────────────────────────────────────
    try:
        from src import monitor as _mon_src_lr
        _msrc_lines = _mon_src_lr.build_monitor_source_report(days=7)
        if _msrc_lines:
            sec.extend(_msrc_lines)
            any_added = True
    except Exception:
        pass

    return sec


def _send_telegram_summary(
    date: str,
    universe_size: int,
    total_scanned: int,
    deep_results: list,
    closed_today: list = None,
    new_patterns: list = None,
    stats: dict = None,
    risk_data: dict = None,
    stage1_filtered: list = None,
    failed_pairs: list = None,
    credit_data: dict = None,
    run_stats: dict = None,
    td_calls: int = 0,
    run_duration_min: float = 0.0,
    scan_mode: str = "full",
    new_alerts: set = None,
    research_result: dict = None,
    threshold_revert_msg: str = None,
    cost_lines: list = None,
    opportunity_data: dict = None,
    all_ohlcv_failed: bool = False,
    movement_alert_data: dict = None,
    threshold_data: dict = None,
    log=None,
) -> None:
    """Build and send Telegram notifications with format tailored to each scan mode."""
    if log is None:
        import sys as _sys_log
        log = _sys_log.stdout
    closed_today    = closed_today    or []
    new_patterns    = new_patterns    or []
    stage1_filtered = stage1_filtered or []
    failed_pairs    = failed_pairs    or []
    run_stats       = run_stats       or {}
    credit_data     = credit_data     or {}
    cost_lines      = cost_lines      or []

    # Load open trades first — needed to filter already-open pairs from signals
    _ot_open_trades: list = []
    try:
        from src import tracker as _trk_ot
        _ot_open_trades = [r for r in _trk_ot.load() if r.get("status") == "OPEN"]
    except Exception:
        pass
    _open_pair_set = {r.get("pair", "").upper() for r in _ot_open_trades}

    def _is_inverse(p1: str, p2: str) -> bool:
        """True if p1 and p2 represent the same instrument quoted the other way (e.g. USD/CAD vs CAD/USD)."""
        c1 = p1.upper().replace("/", "").replace("-", "")
        c2 = p2.upper().replace("/", "").replace("-", "")
        return len(c1) == 6 and len(c2) == 6 and c1 == c2[3:] + c2[:3]

    # Pairs the analyst said YES but whose effective confidence (after MA ribbon
    # penalty) falls below the live-trade threshold are demoted to watch list.
    try:
        from src import threshold_manager as _tm_eff
        _trade_conf_thr = _tm_eff.get_confidence_threshold()
    except Exception:
        _trade_conf_thr = 7
    # Dynamic threshold overrides the static one when available
    if threshold_data and threshold_data.get("final_threshold"):
        _trade_conf_thr = float(threshold_data["final_threshold"])

    _demoted_pairs = {
        r["pair"] for r in deep_results
        if r["parsed"].get("trade_this") == "YES"
        and _eff_conf(r) < _trade_conf_thr
    }

    # Grade all results — used by display helpers and filtering below
    _quality_grades: dict = {r["pair"]: _trade_quality_grade(r) for r in deep_results}

    # Log pairs excluded due to zero ATR
    _atr_zero_pairs = [
        pair for pair, qg in _quality_grades.items()
        if qg.get("atr_zero")
    ]
    if _atr_zero_pairs:
        for _azp in _atr_zero_pairs:
            print(f"[ATR BLOCK] ⚠️ {_azp} excluded — ATR calculation failed — cannot set safe stop loss distance", file=sys.stderr)
        try:
            _atr_alert_lines = [
                f"⚠️ {p} excluded — ATR calculation returned zero — cannot set safe stop loss distance — pair skipped"
                for p in _atr_zero_pairs
            ]
            _telegram("⚠️ ATR zero exclusions this scan:\n" + "\n".join(_atr_alert_lines))
        except Exception:
            pass

    # Extract drawdown mode early (before _risk_state is fully initialised below)
    # so it can be applied in the yes_trades filter immediately.
    _dd_mode: str = (risk_data or {}).get("risk_state", {}).get("drawdown_mode", "normal")

    # Hard block: compute not-viable pairs (net R:R after costs < 1.3:1)
    _not_viable_pairs: set = set()
    try:
        from src import trade_costs as _tc_nv
        for _r_nv in deep_results:
            try:
                _p_nv = _r_nv["parsed"]
                _dir_nv   = (_p_nv.get("direction") or "").upper()
                _entry_nv = float(_p_nv.get("entry") or _p_nv.get("entry_price") or 0)
                _stop_nv  = float(_p_nv.get("stop_loss") or _p_nv.get("stop") or 0)
                _tgt_nv   = float(_p_nv.get("take_profit") or _p_nv.get("target") or 0)
                if _entry_nv and _stop_nv and _tgt_nv and _dir_nv:
                    _viab_nv = _tc_nv.check_viability(
                        _r_nv["pair"], _dir_nv, _entry_nv, _stop_nv, _tgt_nv
                    )
                    if _viab_nv.get("net_rr", 999) < 1.3:
                        _not_viable_pairs.add(_r_nv["pair"])
            except Exception:
                pass
    except Exception:
        pass

    # ── GLOBAL MARKET REGIME — prime cache and apply overrides ───────────────────
    # Run before _yes_raw assembly so conf_override and size_mult are in effect.
    try:
        from src import market_regime as _mr_scan
        _regime_macro_sigs: dict = {}
        for _r_regime in deep_results:
            try:
                _regime_macro_sigs = _r_regime["bundle"]["macro"]["signals"]
                if _regime_macro_sigs:
                    break
            except (KeyError, TypeError):
                pass
        _grd = _mr_scan.detect(_regime_macro_sigs or None)
        # Dynamic threshold (computed before _send_telegram_summary) already accounts
        # for regime — skip the per-scan conf_threshold override from market_regime.
    except Exception:
        pass

    # ── SECOND OPINION: devil's advocate for all 7+ raw-confidence pairs ─────────
    # Runs BEFORE _yes_raw assembly so a confidence reduction (7→6) correctly
    # removes the pair from trade alerts.
    for _da_r in deep_results:
        if _conf(_da_r) >= 7:
            try:
                from src import analyst as _da_analyst
                _da = _da_analyst.devil_advocate(
                    _da_r["pair"],
                    _da_r["parsed"],
                    _da_r.get("bundle", {}),
                )
                _da_r["second_opinion"] = _da
                if _da.get("has_objections"):
                    try:
                        _da_r["parsed"]["confidence"] = max(1, int(_da_r["parsed"]["confidence"]) - 1)
                    except (TypeError, ValueError):
                        pass
                    if _da.get("reasons"):
                        _existing_rf = (_da_r["parsed"].get("risk_factors") or "").strip()
                        _new_rf = "; ".join(_da["reasons"])
                        _da_r["parsed"]["risk_factors"] = (
                            _new_rf + ("; " + _existing_rf if _existing_rf else "")
                        )
                else:
                    _da_r["_da_boost"] = 0.5
            except Exception:
                pass

    # Issue 1: overall confidence is the deciding factor, not individual layer scores.
    # Any pair with 7+ effective confidence qualifies for a trade alert regardless of
    # what the analyst's trade_this field says — confidence overrides individual layers.
    _yes_raw = [
        r for r in deep_results
        if _eff_conf(r) >= _trade_conf_thr
        and r["pair"] not in _demoted_pairs
        and r["pair"] not in _not_viable_pairs
    ]
    # Drawdown tier + grade filtering: stricter tiers require higher grade / more confirmation.
    yes_trades = [
        r for r in _yes_raw
        if _dd_allows_trade(r, _dd_mode, _quality_grades, _trade_conf_thr)
    ]
    # Deduplicate inverse pairs (e.g. USD/CHF SELL + CHF/USD BUY) — keep higher-ranked
    _yt_seen: set = set()
    _yt_deduped = []
    for _r in yes_trades:
        _p = _r["pair"].upper().replace("/", "")
        _inv = _p[3:] + _p[:3]
        if _p not in _yt_seen and _inv not in _yt_seen:
            _yt_deduped.append(_r)
            _yt_seen.add(_p)
    yes_trades = _yt_deduped

    # ── Fund state: daily limits + circuit breaker ──────────────────────────────
    # Applies ONLY to fund trades (trades.csv). Research trades are never affected.
    _fund_st: dict = {}
    _fund_st_blocked: list = []   # [(trade_dict, reason_str), ...]
    _blocked_setups:  list = []   # high-conf setups blocked by capacity
    _swapped_setups:  list = []   # trades closed to free a slot
    _override_pairs: dict = {}    # pair → tier for 5th-slot overrides; used in Discord report
    try:
        from src import fund_state as _fs
        _cur_bal_fs = (risk_data.get("profile") or {}).get("estimated_balance") if risk_data else None
        _fund_st = _fs.load()
        _fund_st = _fs.reset_if_new_day(_fund_st, current_balance=_cur_bal_fs)
        _fund_st = _fs.reset_if_new_week(_fund_st)
        _open_fund_count = _get_open_fund_count()  # always fresh from CSV
        _yt_pass: list = []
        # Pre-compute regime and loss-streak filters for this scan
        _regime_str = str((threshold_data or {}).get("regime", "") or "").upper()
        _REGIME_BLOCK = ["RANGING_LOW_VOL", "RANGING_LOW_VOLATILITY", "RISK_OFF"]
        _consec_losses_fs = int(_fund_st.get("consecutive_losses", 0) or 0)
        MAX_CONSECUTIVE_LOSSES = 3
        if "TRENDING" in _regime_str:
            FUND_TRADE_MIN_CONF = 6.0
        elif "RANGING" in _regime_str:
            FUND_TRADE_MIN_CONF = 7.5
        else:
            FUND_TRADE_MIN_CONF = 7.0
        FUND_MIN_RR = 2.5
        _log_line(log, (
            f"[fund] Filters: min_conf={FUND_TRADE_MIN_CONF} "
            f"min_rr={FUND_MIN_RR} "
            f"regime={_regime_str!r} "
            f"consecutive_losses={_consec_losses_fs}"
        ))
        # Per-pair win-rate lookup from research history (computed once, used inside loop)
        _pair_wr_lkp: dict = {}
        try:
            import pandas as _prwlkp_pd
            _prwlkp_rt = _prwlkp_pd.read_csv(
                "data/research_trades.csv", encoding="utf-8-sig"
            )
            _prwlkp_cl = _prwlkp_rt[_prwlkp_rt["status"] != "OPEN"]
            for _prwlkp_pair in _prwlkp_cl["pair"].dropna().unique():
                _prwlkp_s = _prwlkp_cl[_prwlkp_cl["pair"] == _prwlkp_pair]
                _prwlkp_w = _prwlkp_s[
                    _prwlkp_s["status"].str.upper().isin(["WIN", "FULL_WIN", "PARTIAL_WIN"])
                ]
                _prwlkp_l = _prwlkp_s[
                    _prwlkp_s["status"].str.upper().isin(["LOSS"])
                ]
                _prwlkp_d = len(_prwlkp_w) + len(_prwlkp_l)
                if _prwlkp_d > 0:
                    _pair_wr_lkp[str(_prwlkp_pair).upper()] = {
                        "win_rate": len(_prwlkp_w) / _prwlkp_d,
                        "decisive": _prwlkp_d,
                    }
        except Exception:
            pass
        for _yt in yes_trades:
            _yt_pair   = _yt.get("pair", "")
            _yt_parsed = (_yt.get("parsed") or {})
            # ── CIRCUIT BREAKER — FIRST check, reads fresh from disk every trade ──
            try:
                with open("data/fund_state.json", encoding="utf-8") as _cb_f:
                    _cb_st   = json.load(_cb_f)
                _cb_losses = int(_cb_st.get("consecutive_losses", 0) or 0)
            except Exception:
                _cb_losses = 0
            if _cb_losses >= 3:
                _cb_reason = (
                    f"Circuit breaker active: {_cb_losses} consecutive losses"
                )
                _log_line(
                    log,
                    f"[circuit] BLOCKED {_yt_pair} — {_cb_losses} consecutive "
                    f"losses — no new trades until a win",
                )
                _yt_parsed["trade_this"] = "NO"
                _yt_parsed["block_reason"] = _cb_reason
                _fund_st_blocked.append((_yt, _cb_reason))
                _yt_conf_cb = _eff_conf(_yt)
                if _yt_conf_cb >= 6.0:
                    _blocked_setups.append({
                        "pair":      _yt_pair,
                        "direction": (_yt_parsed.get("direction") or "").upper(),
                        "conf":      _yt_conf_cb,
                        "reason":    f"Circuit breaker ({_cb_losses} losses)",
                    })
                try:
                    from src import tracker as _trk_cb
                    if _yt.get("id"):
                        _trk_cb.update_outcome(int(_yt["id"]), "SKIPPED",
                                               notes=f"Blocked: {_cb_reason}")
                except Exception:
                    pass
                continue
            # ── PAIR FILTER — block banned exotic pairs ────────────────────────
            _pair_upper = _yt_pair.upper()
            if _pair_upper in FUND_BANNED_PAIRS:
                _blk_pair = f"Pair banned: {_yt_pair} is an illiquid exotic"
                _log_line(log, f"[pair-filter] BLOCKING {_yt_pair} — banned exotic pair")
                _yt_parsed["trade_this"] = "NO"
                _yt_parsed["block_reason"] = _blk_pair
                _fund_st_blocked.append((_yt, _blk_pair))
                try:
                    from src import tracker as _trk_pf
                    if _yt.get("id"):
                        _trk_pf.update_outcome(int(_yt["id"]), "SKIPPED",
                                               notes=f"Blocked: {_blk_pair}")
                except Exception:
                    pass
                continue
            # ── DATA VALIDATION GATE — runs before any other market filter ────
            _yt_dir_dv = (_yt_parsed.get("direction") or "").upper()
            _data_val = _validate_trade_data(
                parsed=_yt_parsed,
                pair=_yt_pair,
                direction=_yt_dir_dv,
                bundle=(_yt.get("bundle") or {}),
                log_fn=lambda m: _log_line(log, m),
            )
            if not _data_val["valid"]:
                _log_line(log, (
                    f"[validate-data] BLOCKED {_yt_pair} "
                    f"— data quality failures: {_data_val['failures']}"
                ))
                _blk_dv = (
                    f"Data validation: {_data_val['failures'][0]}"
                    if _data_val["failures"] else "Data validation failed"
                )
                _yt_parsed["trade_this"] = "NO"
                _yt_parsed["block_reason"] = _blk_dv
                try:
                    from src import tracker as _trk_dv
                    if _yt.get("id"):
                        _trk_dv.update_outcome(int(_yt["id"]), "SKIPPED",
                                               notes=f"Blocked: {_blk_dv}")
                except Exception:
                    pass
                continue
            # Regime filter — no fund trades in ranging/risk-off
            if any(r in _regime_str for r in _REGIME_BLOCK):
                _blk_rgm = f"Regime: {_regime_str} — waiting for trending market"
                _log_line(log, f"[regime] BLOCKING fund trade {_yt_pair} — regime is {_regime_str}")
                _fund_st_blocked.append((_yt, _blk_rgm))
                try:
                    from src import tracker as _trk_rgm
                    if _yt.get("id"):
                        _trk_rgm.update_outcome(int(_yt["id"]), "SKIPPED",
                                                notes=f"Blocked: {_blk_rgm}")
                except Exception:
                    pass
                continue
            # Pair history confidence adjustment (based on research win rate for this pair)
            try:
                _ps_data = _pair_wr_lkp.get(_yt_pair.upper(), {})
                _ps_dec  = int(_ps_data.get("decisive", 0))
                _ps_wr   = float(_ps_data.get("win_rate", 0))
                if _ps_dec >= 10:
                    if _ps_wr >= 0.65:
                        _ps_boost = 0.3
                    elif _ps_wr <= 0.35:
                        _ps_boost = -0.3
                    else:
                        _ps_boost = 0.0
                    if _ps_boost != 0 and isinstance(_yt.get("parsed"), dict):
                        _ps_orig = float(_yt["parsed"].get("confidence") or 0)
                        _yt["parsed"]["confidence"] = round(
                            min(10.0, max(0.0, _ps_orig + _ps_boost)), 1
                        )
                        _yt["parsed"]["_pair_wr_boost"]  = _ps_boost
                        _yt["parsed"]["_pair_wr_sample"] = _ps_dec
                        _log_line(log, (
                            f"[pair] {_yt_pair} {_ps_wr*100:.0f}% WR ({_ps_dec} trades) "
                            f"→ conf {_ps_boost:+.1f} ({_ps_orig:.1f}→{_yt['parsed']['confidence']:.1f})"
                        ))
            except Exception as _ps_exc:
                _log_line(log, f"[pair] stats error: {_ps_exc}")
            # Improvement 2: Dynamic confidence threshold by regime
            _yt_conf_val = _eff_conf(_yt)
            if _yt_conf_val < FUND_TRADE_MIN_CONF:
                _blk_conf = f"Confidence {_yt_conf_val:.1f} < {FUND_TRADE_MIN_CONF:.1f} min (regime: {_regime_str})"
                _log_line(log, f"[fund] BLOCKING {_yt_pair} — {_blk_conf}")
                _fund_st_blocked.append((_yt, _blk_conf))
                try:
                    from src import tracker as _trk_conf
                    if _yt.get("id"):
                        _trk_conf.update_outcome(int(_yt["id"]), "SKIPPED",
                                                 notes=f"Blocked: {_blk_conf}")
                except Exception:
                    pass
                continue
            # Improvement 4: Weekly + Monthly trend alignment
            _yt_dir_ta = (_yt_parsed.get("direction") or "").upper()
            _trend_align = _get_trend_alignment(
                _yt_pair, _yt_dir_ta,
                log_fn=lambda m: _log_line(log, m),
            )
            # Hard block: BOTH weekly AND monthly oppose direction
            if (_trend_align.get("weekly_aligned") is False and
                    _trend_align.get("monthly_aligned") is False):
                _blk_trend = (
                    f"Trend opposed: weekly={_trend_align['weekly_trend']} "
                    f"monthly={_trend_align['monthly_trend']}"
                )
                _log_line(log, f"[trend] BLOCKING {_yt_pair} {_yt_dir_ta} — "
                          f"BOTH weekly ({_trend_align['weekly_trend']}) AND monthly "
                          f"({_trend_align['monthly_trend']}) oppose direction")
                _fund_st_blocked.append((_yt, _blk_trend))
                try:
                    from src import tracker as _trk_trnd
                    if _yt.get("id"):
                        _trk_trnd.update_outcome(int(_yt["id"]), "SKIPPED",
                                                 notes=f"Blocked: {_blk_trend}")
                except Exception:
                    pass
                continue
            # Soft block: in RANGING regime with any misaligned trend
            elif ("RANGING" in _regime_str and (
                    _trend_align.get("weekly_aligned") is False or
                    _trend_align.get("monthly_aligned") is False)):
                _blk_trend = "Ranging market + trend misaligned"
                _log_line(log, f"[trend] BLOCKING {_yt_pair} in RANGING — "
                          f"single trend misaligned")
                _fund_st_blocked.append((_yt, _blk_trend))
                try:
                    from src import tracker as _trk_trnd
                    if _yt.get("id"):
                        _trk_trnd.update_outcome(int(_yt["id"]), "SKIPPED",
                                                 notes=f"Blocked: {_blk_trend}")
                except Exception:
                    pass
                continue
            # Fallback: legacy monthly_trend_aligned from parsed analysis
            _mta_val = _yt_parsed.get("monthly_trend_aligned")
            if _mta_val is not None:
                _mta_bool = str(_mta_val).upper() in ("TRUE", "YES", "1", "T")
                if not _mta_bool and _trend_align.get("weekly_aligned") is False:
                    _blk_mta = "Monthly trend misaligned (both timeframes confirmed)"
                    _log_line(log, f"[trend] BLOCKING {_yt_pair} — monthly trend NOT aligned")
                    _fund_st_blocked.append((_yt, _blk_mta))
                    continue
            else:
                _log_line(log, f"[trend] WARNING {_yt_pair} — monthly_trend_aligned not available — allowing trade")
            # Loss journal — block or penalise setups matching past-loss patterns
            _jrnl_e  = float(_yt_parsed.get("entry") or _yt_parsed.get("entry_price") or 0)
            _jrnl_sl = float(_yt_parsed.get("stop_loss") or _yt_parsed.get("stop") or 0)
            _jrnl_ps = 0.01 if "JPY" in _yt_pair else 0.0001
            _jrnl_sp = abs(_jrnl_e - _jrnl_sl) / _jrnl_ps if _jrnl_e and _jrnl_sl else 0.0
            _journal_check = _check_loss_journal(
                pair=_yt_pair,
                direction=_yt_dir_ta,
                regime=_regime_str,
                session=",".join(_get_current_session()),
                weekly_trend=_trend_align.get("weekly_trend", "NEUTRAL"),
                rsi=float(
                    (_yt.get("bundle") or {})
                    .get("technical", {})
                    .get("daily", {})
                    .get("rsi14") or 0
                ),
                stop_pips=_jrnl_sp,
                confidence=_yt_conf_val,
                log_fn=lambda m: _log_line(log, m),
            )
            if _journal_check["blocked"]:
                _blk_journal = (
                    f"Loss journal: {_journal_check['warnings'][0][:80]}"
                    if _journal_check["warnings"] else "Loss journal: high risk pattern"
                )
                _log_line(log, (
                    f"[loss-journal] BLOCKED {_yt_pair} — matches loss patterns "
                    f"risk={_journal_check['risk_score']:.2f}"
                ))
                _fund_st_blocked.append((_yt, _blk_journal))
                continue
            elif _journal_check["risk_score"] > 0.2:
                _jrnl_penalty = round(_journal_check["risk_score"] * 0.5, 1)
                _jrnl_orig    = float(_yt_parsed.get("confidence") or 0)
                if isinstance(_yt.get("parsed"), dict):
                    _yt["parsed"]["confidence"] = max(0.0, _jrnl_orig - _jrnl_penalty)
                _log_line(log, (
                    f"[loss-journal] {_yt_pair} conf penalty -{_jrnl_penalty} "
                    f"for past loss pattern (score={_journal_check['risk_score']:.2f})"
                ))
            # Improvement 5: Minimum R:R for fund trades
            _yt_rr_val = float(_yt_parsed.get("reward_risk") or 0)
            if _yt_rr_val > 0 and _yt_rr_val < FUND_MIN_RR:
                _blk_rr = f"R:R {_yt_rr_val:.2f} < {FUND_MIN_RR} minimum"
                _log_line(log, f"[rr] BLOCKING {_yt_pair} — R:R {_yt_rr_val:.2f} below fund minimum {FUND_MIN_RR}")
                _fund_st_blocked.append((_yt, _blk_rr))
                continue
            # Improvement 6: ATR-based stop distance check (block if stop > 4x ATR)
            _yt_bndl  = (_yt.get("bundle") or {})
            _yt_atr14 = float(((_yt_bndl.get("technical") or {}).get("daily", {}).get("atr14") or 0))
            _yt_entry_v = float(_yt_parsed.get("entry") or _yt_parsed.get("entry_price") or 0)
            _yt_stop_v  = float(_yt_parsed.get("stop_loss") or _yt_parsed.get("stop") or 0)
            if _yt_atr14 > 0 and _yt_entry_v and _yt_stop_v:
                _yt_stop_dist = abs(_yt_entry_v - _yt_stop_v)
                if _yt_stop_dist > _yt_atr14 * 4.0:
                    _blk_atr = f"Stop too wide ({_yt_stop_dist:.5f}) vs 4xATR ({_yt_atr14 * 4.0:.5f})"
                    _log_line(log, f"[stop] BLOCKING {_yt_pair} — stop too wide")
                    _fund_st_blocked.append((_yt, _blk_atr))
                    continue
            # Tiered capacity check — supports 5th slot for conf ≥ 7.5
            _cap = _check_capacity_tiered(
                pair=_yt_pair,
                confidence=_yt_conf_val,
                regime=_regime_str,
                fund_state=_fund_st,
                log_fn=lambda m: _log_line(log, m),
            )
            if not _cap["allowed"]:
                _blk_rsn_cap = _cap["reason"]
                _swap_executed = False

                # CB blocks swaps entirely — no new directional exposure when losing
                try:
                    _swap_cb_l = int(_fund_st.get("consecutive_losses") or 0)
                except Exception:
                    _swap_cb_l = 0
                if _swap_cb_l >= 3:
                    _log_line(log, f"[swap] BLOCKED — circuit breaker active "
                              f"({_swap_cb_l} losses)")
                    _fund_st_blocked.append((_yt, f"Circuit breaker ({_swap_cb_l} losses)"))
                    continue

                if _yt_conf_val >= SWAP_MIN_NEW_CONF:
                    _log_line(log, f"[swap] {_yt_pair} conf {_yt_conf_val:.1f} "
                              f">= {SWAP_MIN_NEW_CONF} — evaluating swap...")
                    _swap_res = _find_swap_target(
                        new_pair=_yt_pair,
                        new_direction=_yt_parsed.get("direction", ""),
                        new_conf=_yt_conf_val,
                        open_trades=_ot_open_trades,
                        log_fn=lambda m: _log_line(log, m),
                    )
                    if _swap_res["should_swap"]:
                        try:
                            from src.trading.financials import (
                                close_fund_trade as _cft_sw,
                                sync_fund_state_json as _sfsj_sw,
                                load_prices as _lp_sw,
                                get_price as _gp_sw,
                                pip_size as _pips_sw,
                            )
                            import pandas as _sw_pd
                            _sw_prices  = _lp_sw()
                            _sw_tid     = int(float(str(_swap_res["target_id"])))
                            _sw_tpair   = _swap_res["target_pair"]
                            _sw_exit    = _gp_sw(_sw_prices, _sw_tpair)
                            _sw_df      = _sw_pd.read_csv("data/trades.csv")
                            _sw_df_row  = _sw_df[_sw_df["id"].astype(str) == str(_sw_tid)]
                            if _sw_exit and not _sw_df_row.empty:
                                _sw_r     = _sw_df_row.iloc[0]
                                _sw_entry = float(_sw_r.get("entry", 0) or 0)
                                _sw_dir   = str(_sw_r.get("direction", "")).upper()
                                _sw_t1h   = str(_sw_r.get("t1_hit", "")).upper() in ("TRUE", "YES", "1", "T")
                                _sw_ps    = _pips_sw(_sw_tpair)
                                _sw_pips  = ((_sw_exit - _sw_entry) / _sw_ps if _sw_dir == "BUY"
                                             else (_sw_entry - _sw_exit) / _sw_ps)
                                _sw_status = ("PARTIAL_WIN" if _sw_t1h and _sw_pips > 0
                                              else "WIN" if _sw_pips > 0 else "LOSS")
                                # Guard: if closing this trade as a LOSS would push
                                # consecutive_losses to 3 the CB would fire and block
                                # the replacement — cancel swap instead of leaving
                                # the fund with no position and a CB-triggered state.
                                _pre_cl = int(_fund_st.get("consecutive_losses") or 0)
                                if _sw_pips < 0 and _pre_cl >= 2:
                                    _log_line(log, (
                                        f"[swap] CANCELLED — closing {_sw_tpair} as LOSS "
                                        f"would trigger circuit breaker "
                                        f"({_pre_cl}→3 losses) with no replacement possible"
                                    ))
                                    continue
                                _cft_sw(
                                    df=_sw_df,
                                    trade_id=_sw_tid,
                                    status=_sw_status,
                                    exit_price=float(_sw_exit),
                                    pips=float(_sw_pips),
                                )
                                _sfsj_sw()
                                # Remove swapped trade from in-memory list so slot count is correct
                                _ot_open_trades = [
                                    r for r in _ot_open_trades
                                    if str(r.get("id", "")) != str(_sw_tid)
                                ]
                                # Reset _cap to reflect freed slot
                                _cap = {"allowed": True, "is_override": False,
                                        "risk_pct": None, "tier": "normal",
                                        "reason": "slot freed by swap"}
                                _swapped_setups.append({
                                    "closed_pair":    _sw_tpair,
                                    "closed_pips":    round(_sw_pips, 1),
                                    "closed_dollars": 0.0,
                                    "new_pair":       _yt_pair,
                                    "new_conf":       _yt_conf_val,
                                    "reason":         _swap_res["reason"],
                                })
                                # Immediate Discord alert for the swap
                                try:
                                    from src import discord_notifier as _dn_sw
                                    _dn_sw.send_swap_alert(
                                        closed_pair=_sw_tpair,
                                        closed_pips=round(_sw_pips, 1),
                                        closed_dollars=0.0,
                                        new_pair=_yt_pair,
                                        new_conf=_yt_conf_val,
                                        swap_reason=_swap_res["reason"],
                                    )
                                except Exception as _dn_sw_exc:
                                    _log_line(log, f"[swap] Discord alert failed: {_dn_sw_exc}")
                                _log_line(log, f"[swap] Slot freed — {_yt_pair} can open")
                                _swap_executed = True
                            else:
                                _log_line(log, f"[swap] No price for {_sw_tpair} — swap aborted")
                        except Exception as _sw_exc:
                            _log_line(log, f"[swap] Execution error: {_sw_exc}")
                    else:
                        _log_line(log, f"[swap] No swap: {_swap_res['reason']}")

                if not _swap_executed:
                    _fund_st_blocked.append((_yt, _blk_rsn_cap))
                    if _yt_conf_val >= 6.0:
                        _blocked_setups.append({
                            "pair":      _yt_pair,
                            "direction": (_yt_parsed.get("direction") or "").upper(),
                            "conf":      _yt_conf_val,
                            "reason":    _blk_rsn_cap,
                        })
                    try:
                        from src import tracker as _trk_cap
                        if _yt.get("id"):
                            _trk_cap.update_outcome(int(_yt["id"]), "SKIPPED",
                                                    notes=f"Blocked: {_blk_rsn_cap}")
                    except Exception:
                        pass
                    continue
            _blk, _blk_rsn, _blk_tp = _fs.is_trading_blocked(_fund_st)
            if _blk:
                _fund_st = _fs.record_missed_opportunity(
                    _fund_st, _yt.get("pair", ""),
                    (_yt.get("parsed") or {}).get("direction", ""),
                    _eff_conf(_yt),
                    float((_yt.get("screen") or {}).get("score") or 0),
                    _blk_tp,
                )
                _fund_st_blocked.append((_yt, _blk_rsn))
                try:
                    from src import tracker as _trk_fsb
                    if _yt.get("id"):
                        _trk_fsb.update_outcome(int(_yt["id"]), "SKIPPED",
                                                notes=f"Blocked: {_blk_rsn}")
                except Exception:
                    pass
                continue
            _ccy_blk, _ccy = _fs.check_currency_exposure(_yt.get("pair", ""), _ot_open_trades)
            if _ccy_blk:
                _blk_rsn_ccy = (
                    f"Currency concentration — {_ccy} at {_fs.MAX_CURRENCY_EXPOSURE} open trades"
                )
                _fund_st = _fs.record_missed_opportunity(
                    _fund_st, _yt.get("pair", ""),
                    (_yt.get("parsed") or {}).get("direction", ""),
                    _eff_conf(_yt),
                    float((_yt.get("screen") or {}).get("score") or 0),
                    "currency_exposure",
                )
                _fund_st_blocked.append((_yt, _blk_rsn_ccy))
                try:
                    from src import tracker as _trk_fsb
                    if _yt.get("id"):
                        _trk_fsb.update_outcome(int(_yt["id"]), "SKIPPED",
                                                notes=f"Blocked: {_blk_rsn_ccy}")
                except Exception:
                    pass
                continue
            # ── Correlation filter ────────────────────────────────────────────
            _yt_dir_corr = (_yt_parsed.get("direction") or "").upper()
            _corr = _check_correlation(
                new_pair=_yt_pair,
                new_direction=_yt_dir_corr,
                open_trades=_ot_open_trades,
                log_fn=lambda m: _log_line(log, m),
            )
            if _corr["blocked"]:
                _blk_rsn_corr = f"Correlation: {_corr['reason']}"
                _fund_st_blocked.append((_yt, _blk_rsn_corr))
                try:
                    from src import tracker as _trk_corr
                    if _yt.get("id"):
                        _trk_corr.update_outcome(int(_yt["id"]), "SKIPPED",
                                                  notes=f"Blocked: {_blk_rsn_corr}")
                except Exception:
                    pass
                continue
            # ── Session filter (advisory; hard block in RANGING) ──────────────
            _session_res = _check_session_filter(
                _yt_pair, log_fn=lambda m: _log_line(log, m))
            _yt["_session_optimal"] = _session_res["allowed"]
            if not _session_res["allowed"] and "RANGING" in _regime_str:
                _blk_rsn_sess = f"Ranging + wrong session: {_session_res['reason']}"
                _log_line(log, f"[session] BLOCKING {_yt_pair} — {_blk_rsn_sess}")
                _fund_st_blocked.append((_yt, _blk_rsn_sess))
                try:
                    from src import tracker as _trk_sess
                    if _yt.get("id"):
                        _trk_sess.update_outcome(int(_yt["id"]), "SKIPPED",
                                                  notes=f"Blocked: {_blk_rsn_sess}")
                except Exception:
                    pass
                continue
            # ── News blackout window ──────────────────────────────────────────
            _news_res = _has_upcoming_news(
                _yt_pair, log_fn=lambda m: _log_line(log, m))
            if _news_res["blocked"]:
                _blk_rsn_news = _news_res["reason"]
                _log_line(log, f"[news] BLOCKING {_yt_pair} — {_blk_rsn_news}")
                _fund_st_blocked.append((_yt, _blk_rsn_news))
                try:
                    from src import tracker as _trk_news
                    if _yt.get("id"):
                        _trk_news.update_outcome(int(_yt["id"]), "SKIPPED",
                                                  notes=f"Blocked: {_blk_rsn_news}")
                except Exception:
                    pass
                continue
            # ── Adaptive sizing ───────────────────────────────────────────────
            _szg_pct, _szg_mode, _szg_reason = _fs.compute_sizing(
                _fund_st,
                _cur_bal_fs or float(config.ACCOUNT_BALANCE),
                _eff_conf(_yt),
            )
            if _szg_pct is None:
                # Drawdown tier quality requirement not met — block this trade
                _fund_st = _fs.record_missed_opportunity(
                    _fund_st, _yt.get("pair", ""),
                    (_yt.get("parsed") or {}).get("direction", ""),
                    _eff_conf(_yt),
                    float((_yt.get("screen") or {}).get("score") or 0),
                    "drawdown_quality",
                )
                _fund_st_blocked.append((_yt, _szg_reason))
                try:
                    from src import tracker as _trk_fsb2
                    if _yt.get("id"):
                        _trk_fsb2.update_outcome(int(_yt["id"]), "SKIPPED",
                                                 notes=f"Blocked: {_szg_reason}")
                except Exception:
                    pass
                continue
            # ── Volatility-adjusted position sizing ───────────────────────────
            _add_sizing = _calculate_position_size(
                regime=_regime_str,
                consecutive_losses=_consec_losses_fs,
                drawdown_pct=float(_fund_st.get("current_drawdown_pct") or 0),
                base_pct=float(_szg_pct) if _szg_pct else BASE_RISK_PCT,
                log_fn=lambda m: _log_line(log, m),
            )
            if _add_sizing["risk_pct"] < float(_szg_pct or BASE_RISK_PCT):
                _szg_pct   = _add_sizing["risk_pct"]
                _szg_mode  = _add_sizing["sizing_mode"]
                _szg_reason = _add_sizing["reason"]
                _log_line(log, f"[sizing] {_yt_pair}: volatility-adjusted → "
                          f"{_szg_pct}% ({_szg_mode})")
            # 5th-slot override: cap risk to the override level
            if _cap.get("is_override") and _cap.get("risk_pct") is not None:
                if _szg_pct is None or float(_szg_pct) > float(_cap["risk_pct"]):
                    _szg_pct    = _cap["risk_pct"]
                    _szg_mode   = f"OVERRIDE_{_cap['tier']}"
                    _szg_reason = _cap["reason"]
                    _log_line(log, f"[capacity] {_yt_pair}: 5th-slot override → "
                              f"{_szg_pct}% ({_szg_mode})")
            _yt["_fs_sizing"] = {
                "pct":      _szg_pct,
                "mode":     _szg_mode,
                "reason":   _szg_reason,
                "balance":  _cur_bal_fs or float(config.ACCOUNT_BALANCE),
                "wins":     int(_fund_st.get("consecutive_wins") or 0),
                "drawdown": float(_fund_st.get("current_drawdown_pct") or 0.0),
            }
            # Stamp ML sizing + trend + session fields on the fund trade row
            # CRITICAL: position_size_pct_at_entry written first in its own isolated call
            # so it cannot be swallowed by a failure in the optional metadata fields.
            if _yt.get("id") and _szg_pct is not None:
                try:
                    from src import tracker as _trk_szg_pct
                    _trk_szg_pct.update_fields(
                        int(_yt["id"]),
                        position_size_pct_at_entry=float(_szg_pct),
                    )
                except Exception as _szg_pct_err:
                    _log_line(log, f"[sizing] ERROR stamping position_size_pct_at_entry "
                              f"for #{_yt['id']}: {_szg_pct_err}")
            # Stamp optional metadata fields (non-critical — failures are logged, not fatal)
            try:
                from src import tracker as _trk_szg
                if _yt.get("id"):
                    _trk_szg.update_fields(
                        int(_yt["id"]),
                        sizing_mode=_szg_mode,
                        consecutive_wins_at_entry=int(_fund_st.get("consecutive_wins") or 0),
                        drawdown_pct_at_entry=float(_fund_st.get("current_drawdown_pct") or 0.0),
                        threshold_at_entry=threshold_data.get("final_threshold", "") if threshold_data else "",
                        regime_base_at_entry=threshold_data.get("regime_base", "") if threshold_data else "",
                        win_rate_adjustment_at_entry=threshold_data.get("win_rate_adjustment", "") if threshold_data else "",
                        data_quality_adjustment_at_entry=threshold_data.get("data_quality_adjustment", "") if threshold_data else "",
                    )
            except Exception as _szg_meta_err:
                _log_line(log, f"[sizing] ERROR stamping metadata fields "
                          f"for #{_yt.get('id')}: {_szg_meta_err}")
            # Stamp rich entry-context features (PART 1 — ML fuel)
            try:
                from src import tracker as _trk_rich
                if _yt.get("id"):
                    _rich_bndl  = (_yt.get("bundle") or {})
                    _rich_daily = (_rich_bndl.get("technical") or {}).get("daily") or {}
                    _rich_entry = float(_yt_parsed.get("entry") or _yt_parsed.get("entry_price") or 0)
                    _rich_stop  = float(_yt_parsed.get("stop_loss") or _yt_parsed.get("stop") or 0)
                    _rich_atr   = float(_rich_daily.get("atr14") or 0)
                    _pip_sz     = 0.01 if "JPY" in _yt_pair else 0.0001
                    _rich_stop_pips = abs(_rich_entry - _rich_stop) / _pip_sz if _rich_entry and _rich_stop else 0
                    _rich_atr_mult  = abs(_rich_entry - _rich_stop) / _rich_atr if _rich_atr and _rich_stop and _rich_entry else 0
                    _rich_rsi    = float(_rich_daily.get("rsi14") or 0)
                    _rich_macd   = float(_rich_daily.get("macd_histogram") or 0)
                    _rich_bb_pos = str(_rich_daily.get("bb_position") or "")
                    _rich_rr     = float(_yt_parsed.get("reward_risk") or 0)
                    _trk_rich.update_fields(
                        int(_yt["id"]),
                        rsi_at_entry=_rich_rsi or "",
                        macd_at_entry=_rich_macd or "",
                        bb_position_at_entry=_rich_bb_pos,
                        atr_at_entry=_rich_atr or "",
                        regime_at_entry=_regime_str,
                        session_at_entry=",".join(_get_current_session()),
                        weekly_trend_at_entry=_trend_align.get("weekly_trend", ""),
                        monthly_trend_at_entry=_trend_align.get("monthly_trend", ""),
                        trend_score_at_entry=int(_trend_align.get("alignment_score") or 0),
                        technical_score=float(_yt_parsed.get("technical_score") or 0) or "",
                        fundamental_score=float(_yt_parsed.get("fundamental_score") or 0) or "",
                        sentiment_score=float(_yt_parsed.get("sentiment_score") or 0) or "",
                        cot_score=float(_yt_parsed.get("cot_score") or 0) or "",
                        momentum_score=float(_yt_parsed.get("momentum_score") or 0) or "",
                        stop_pips_at_entry=round(_rich_stop_pips, 1) or "",
                        rr_at_entry=_rich_rr or "",
                        atr_multiple_at_entry=round(_rich_atr_mult, 2) or "",
                        consecutive_losses_at_entry=_consec_losses_fs,
                        drawdown_at_entry=float(_fund_st.get("current_drawdown_pct") or 0.0),
                        open_trades_at_entry=_open_fund_count,
                    )
            except Exception as _rich_err:
                _log_line(log, f"[rich-features] ERROR stamping entry features "
                          f"for #{_yt.get('id')}: {_rich_err}")
            # ML win-probability display (PART 6)
            try:
                from src import online_learner as _ol_fund
                from src.feature_extractor import FEATURE_COLS as _ol_fund_cols
                _ol_feats = {c: 0.0 for c in _ol_fund_cols}
                _ol_feats["confidence"]     = float(_yt_parsed.get("confidence") or 0)
                _ol_feats["reward_risk"]    = float(_yt_parsed.get("reward_risk") or 0)
                _ol_feats["direction_buy"]  = 1.0 if _yt_dir_ta == "BUY" else 0.0
                _ol_feats["rsi14"]          = float((_yt.get("bundle") or {}).get("technical", {}).get("daily", {}).get("rsi14") or 50)
                _ol_prob = _ol_fund.predict_proba(_ol_feats)
                if _ol_prob is not None:
                    _ml_emoji = "\U0001f7e2" if _ol_prob >= 0.6 else ("\U0001f534" if _ol_prob <= 0.4 else "\U0001f7e1")
                    _log_line(log, (
                        f"[ML] {_yt_pair} {_yt_dir_ta}: "
                        f"P(win)={_ol_prob:.1%} {_ml_emoji} "
                        f"(conf={float(_yt_parsed.get('confidence', 0)):.1f})"
                    ))
                    # Stamp on trade row
                    if isinstance(_yt.get("parsed"), dict):
                        _yt["parsed"]["ml_win_probability"] = round(_ol_prob, 3)
                    try:
                        from src import tracker as _trk_mlp
                        if _yt.get("id"):
                            _trk_mlp.update_fields(int(_yt["id"]), ml_win_probability=round(_ol_prob, 3))
                    except Exception:
                        pass
                    # If AI says good but ML says bad — penalise confidence
                    if float(_yt_parsed.get("confidence") or 0) >= 7.0 and _ol_prob <= 0.35:
                        _ml_penalty = 0.5
                        if isinstance(_yt.get("parsed"), dict):
                            _yt["parsed"]["confidence"] = max(
                                0.0, float(_yt["parsed"].get("confidence", 0)) - _ml_penalty
                            )
                        _log_line(log, (
                            f"[ML] {_yt_pair} ML disagrees with AI — "
                            f"conf -{_ml_penalty} → {_yt_parsed.get('confidence', 0):.1f}"
                        ))
            except Exception as _ml_err:
                _log_line(log, f"[ML] prediction error for {_yt_pair}: {_ml_err}")
            _fund_st = _fs.increment_daily_trades(_fund_st)
            _open_fund_count = _get_open_fund_count()  # re-read after write
            _cap_max = MAX_FUND_TRADES_OVERRIDE if _cap.get("is_override") else MAX_FUND_TRADES_NORMAL
            _log_line(log, f"[capacity] {_yt_pair} opened → now {_open_fund_count}/{_cap_max}"
                      + (" ⚡ OVERRIDE" if _cap.get("is_override") else ""))
            _ensure_trade_data_complete(
                trade_row=_yt_parsed,
                pair=_yt_pair,
                log_fn=lambda m: _log_line(log, m),
            )
            # ML — store fund trade features for training on closure
            try:
                from src.online_learner import OnlineLearner as _OL_cls
                _ol_inst  = _OL_cls()
                _ol_bndl  = (_yt.get("bundle") or {}).get("technical", {}).get("daily") or {}
                _ol_entry = float(_yt_parsed.get("entry") or _yt_parsed.get("entry_price") or 0)
                _ol_stop  = float(_yt_parsed.get("stop_loss") or _yt_parsed.get("stop") or 0)
                _ol_ps    = 0.01 if "JPY" in _yt_pair else 0.0001
                _ol_sp    = abs(_ol_entry - _ol_stop) / _ol_ps if _ol_entry and _ol_stop else 0.0
                _ol_feats = {
                    "confidence":         float(_yt_parsed.get("confidence") or 0),
                    "rsi":                float(_ol_bndl.get("rsi14") or 0),
                    "regime":             str(_regime_str),
                    "session":            ",".join(_get_current_session()),
                    "weekly_trend":       str(_trend_align.get("weekly_trend", "")),
                    "monthly_trend":      str(_trend_align.get("monthly_trend", "")),
                    "stop_pips":          round(_ol_sp, 1),
                    "rr":                 float(_yt_parsed.get("reward_risk") or 0),
                    "consecutive_losses": int(_consec_losses_fs or 0),
                    "drawdown":           float(_fund_st.get("current_drawdown_pct") or 0.0),
                    "trend_score":        float(_trend_align.get("alignment_score") or 0),
                }
                _ol_tid = str(_yt.get("id", ""))
                if _ol_tid:
                    _ol_inst.store_fund_features(trade_id=_ol_tid, features=_ol_feats)
                    _log_line(log, f"[ML] Fund trade #{_ol_tid} features stored")
            except Exception as _ol_err:
                _log_line(log, f"[ML] Feature store error: {_ol_err}")
            _yt_pass.append(_yt)
            if _cap.get("is_override"):
                _override_pairs[_yt_pair] = _cap["tier"]
            _ot_open_trades.append(_yt)  # live update so next trade in same scan sees this currency
        # Stamp ML sizing fields on matching research trades
        try:
            from src import research_tracker as _rtrk_szg
            _fs_st_ref = _fund_st
            for _yt_pass_r in _yt_pass:
                _rr_id = _yt_pass_r.get("research_id")
                _fsz   = _yt_pass_r.get("_fs_sizing")
                if _rr_id and _fsz:
                    _rtrk_szg.update_fields(
                        int(_rr_id),
                        sizing_mode=_fsz["mode"],
                        consecutive_wins_at_entry=int(_fsz["wins"]),
                        drawdown_pct_at_entry=float(_fsz["drawdown"]),
                        position_size_pct_at_entry=float(_fsz["pct"]),
                        threshold_at_entry=threshold_data.get("final_threshold", "") if threshold_data else "",
                        regime_base_at_entry=threshold_data.get("regime_base", "") if threshold_data else "",
                        win_rate_adjustment_at_entry=threshold_data.get("win_rate_adjustment", "") if threshold_data else "",
                        data_quality_adjustment_at_entry=threshold_data.get("data_quality_adjustment", "") if threshold_data else "",
                    )
        except Exception:
            pass
        _fs.save(_fund_st)
        yes_trades = _yt_pass
    except Exception as _fs_exc:
        import traceback as _tb_fs
        print(f"[FUND_STATE] {_fs_exc}", file=sys.stderr)
        print(_tb_fs.format_exc(), file=sys.stderr)

    # ── Cascade target levels for YES fund trades ─────────────────────────────
    # Computed after deduplication so only real fund trades get cascade fields.
    try:
        from src import cascade as _casc_yt
        from src import tracker as _trk_casc_yt
        for _yt in yes_trades:
            _yt_id  = _yt.get("id")
            _yt_p   = _yt.get("parsed", {}) or {}
            _yt_e   = float((_yt_p.get("entry") or 0))
            _yt_s   = float((_yt_p.get("stop_loss") or 0))
            _yt_t   = float((_yt_p.get("target") or 0))
            _yt_d   = (_yt_p.get("direction") or "").upper()
            _yt_b   = _yt.get("bundle", {}) or {}
            _yt_atr = float(((_yt_b.get("technical") or {}).get("daily", {}).get("atr14") or 0))
            if _yt_id and _yt_e and _yt_s and _yt_t and _yt_d in ("BUY", "SELL"):
                _yt_pair_up  = (_yt.get("pair") or "").upper()
                _yt_pst      = _pair_stats_all.get(_yt_pair_up, {})
                _yt_adpt     = _yt_pst.get("adaptive_active") and _yt_pst.get("n_trades_with_mfe", 0) >= 10
                _yt_t1m      = _yt_pst.get("t1_mult") if _yt_adpt else None
                _yt_t2m      = _yt_pst.get("t2_mult") if _yt_adpt else None
                _yt_t3m      = _yt_pst.get("t3_mult") if _yt_adpt else None
                _yt_vol_tier = "NORMAL"
                _yt_atr_rt   = 1.0
                if _yt_adpt and _yt_t1m is not None:
                    try:
                        from src import pair_statistics as _pstat_vol_yt
                        _yt_atr_rt = float(
                            ((_yt_b.get("technical") or {}).get("daily") or {}).get("atr_percentile_6m") or 1.0
                        )
                        _yt_vol_tier, _yt_t1m, _yt_t2m, _yt_t3m = (
                            _pstat_vol_yt.apply_volatility_adjustment(
                                _yt_t1m, _yt_t2m, _yt_t3m, _yt_atr_rt
                            )
                        )
                    except Exception:
                        pass
                _yt_t1, _yt_t2, _yt_t3 = _casc_yt.compute_levels(
                    _yt_e, _yt_s, _yt_t, _yt_d, atr=_yt_atr or None,
                    t1_mult=_yt_t1m, t2_mult=_yt_t2m, t3_mult=_yt_t3m,
                )
                if _yt_t1 is not None:
                    _trk_casc_yt.update_fields(
                        int(_yt_id),
                        t1_price=_yt_t1, t2_price=_yt_t2, t3_price=_yt_t3,
                        effective_stop=_yt_s,
                        t1_was_adaptive="TRUE" if _yt_adpt else "FALSE",
                        t1_target_atr_multiple=_yt_t1m if _yt_t1m is not None else 0.4,
                        t2_target_atr_multiple=_yt_t2m if _yt_t2m is not None else 0.7,
                        t3_target_atr_multiple=_yt_t3m if _yt_t3m is not None else 1.0,
                        volatility_tier_at_entry=_yt_vol_tier,
                        atr_percentile_at_entry=_yt_atr_rt,
                    )
    except Exception:
        pass

    # ── Entry type detection for YES fund trades ──────────────────────────────
    try:
        from src import tracker as _trk_et
        from datetime import datetime as _dt_et, timezone as _tz_et, timedelta as _td_et
        for _yt in yes_trades:
            _yt_id     = _yt.get("id")
            if not _yt_id:
                continue
            _yt_parsed = _yt.get("parsed") or {}
            _yt_report = _yt.get("report") or _yt_parsed.get("key_thesis") or ""
            _yt_dir    = (_yt_parsed.get("direction") or "").upper()
            _yt_entry  = float(_yt_parsed.get("entry") or 0)
            _yt_pair   = _yt.get("pair", "")
            if not _yt_dir or not _yt_entry:
                continue
            _entry_info = _parse_entry_type(
                analysis_text=_yt_report,
                current_price=_yt_entry,
                direction=_yt_dir,
                log_fn=lambda m: _log_line(log, m),
            )
            _entry_kwargs = {
                "entry_type":          _entry_info["entry_type"],
                "entry_trigger_price": _entry_info["trigger_price"],
            }
            if _entry_info["entry_type"] != "IMMEDIATE":
                _expiry_h  = _entry_info["expiry_hours"] or 48
                _expiry_dt = _dt_et.now(_tz_et.utc) + _td_et(hours=_expiry_h)
                _entry_kwargs.update({
                    "status":                "PENDING",
                    "entry_trigger_direction": _entry_info["trigger_direction"] or "",
                    "entry_trigger_reason":    _entry_info["trigger_reason"] or "",
                    "entry_trigger_expiry":    _expiry_dt.strftime("%Y-%m-%d %H:%M:%S"),
                })
                _log_line(log, (
                    f"[entry] #{_yt_id} {_yt_pair} -> PENDING "
                    f"({_entry_info['entry_type']} at {_entry_info['trigger_price']} "
                    f"expires {_expiry_h}h)"
                ))
            _trk_et.update_fields(int(_yt_id), **_entry_kwargs)
    except Exception as _et_exc:
        import traceback as _et_tb
        _log_line(log, f"[entry] entry type detection failed: {_et_exc}")
        _et_tb.print_exc()

    # C-grade demoted to watchlist only in normal mode; restricted tiers skip C entirely
    _c_grade_yes = (
        [r for r in _yes_raw if _quality_grades.get(r["pair"], {}).get("grade") == "C"
         and _eff_conf(r) < _trade_conf_thr]
        if _dd_mode == "normal" else []
    )
    _df_grade_yes = [r for r in _yes_raw if _quality_grades.get(r["pair"], {}).get("grade") in ("D", "F")]

    _yes_pair_set = {r["pair"] for r in _yes_raw}

    watch_list = sorted(
        [r for r in deep_results
         if r["pair"] not in _yes_pair_set
         and 5 <= _eff_conf(r) <= 6
         and _quality_grades.get(r["pair"], _trade_quality_grade(r)).get("grade") not in ("D", "F")
        ] + _c_grade_yes,
        key=_eff_conf, reverse=True,
    )[:4]   # allow one extra slot for demoted C-grade alerts
    # Deduplicate inverse pairs from watch list vs yes_trades and within itself
    _wl_seen: set = {r["pair"].upper().replace("/", "") for r in yes_trades}
    _wl_deduped = []
    for _r in watch_list:
        _p = _r["pair"].upper().replace("/", "")
        _inv = _p[3:] + _p[:3]
        if _p not in _wl_seen and _inv not in _wl_seen:
            _wl_deduped.append(_r)
            _wl_seen.add(_p)
    watch_list = _wl_deduped

    near_misses = sorted(
        [r for r in deep_results
         if r["pair"] not in _yes_pair_set
        ] + _df_grade_yes,
        key=_eff_conf, reverse=True,
    )
    upcoming = sorted(
        [r for r in near_misses
         if 3 <= _eff_conf(r) <= 4
         and r["pair"].upper() not in _open_pair_set
         and not any(_is_inverse(r["pair"], op) for op in _open_pair_set)
        ],
        key=_eff_conf, reverse=True,
    )[:3]

    # ── End-of-scan price validation for IMMEDIATE entries ──────────────────
    try:
        from src.trading.financials import load_prices as _load_prices_vep
        _final_prices_vep = _load_prices_vep()
        yes_trades = _validate_entry_prices(
            yes_trades=yes_trades,
            current_prices=_final_prices_vep,
            max_adverse_pips=30.0,
            log_fn=lambda m: _log_line(log, m),
        )
    except Exception as _vep_exc:
        _log_line(log, f"[validate] price validation failed: {_vep_exc}")

    _sizes, _exposure, _risk_state = {}, {}, {}
    if risk_data:
        for s in (risk_data.get("sized_trades") or []):
            _sizes[s["pair"]] = s
        _exposure   = risk_data.get("exposure", {})
        _risk_state = risk_data.get("risk_state", {})

    _pair_stats_all: dict = {}
    try:
        from src import pair_statistics as _pstat_disp
        _pstat_disp.update()
        _pair_stats_all = _pstat_disp.load()
    except Exception:
        pass

    ctx         = _derive_market_context(deep_results, risk_data)
    now_ak      = _auckland_now()
    today_short = _fmt_date_short_nz(now_ak)
    n_deep      = len(deep_results)
    all_sections: list[list[str]] = []

    # Pre-compute drawdown banner (non-empty when in a protection tier)
    _dd_banner = ""
    try:
        if risk_data:
            from src import risk_manager as _rm_banner
            _dd_banner = _rm_banner.drawdown_header_line(
                _risk_state, risk_data.get("profile", {})
            )
    except Exception:
        pass

    # Load 6am morning confidence scores for change detection in intraday scans
    _morning_conf: dict = {}
    try:
        _morning_conf = json.loads(_MORNING_RANKED_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass

    # Pre-populate price cache from today's scan results (zero extra API calls)
    _ot_px_cache: dict = {}
    for _r in deep_results:
        try:
            _td = _r["bundle"]["technical"]["daily"]
            if isinstance(_td, dict):
                _p = float(_td.get("last_close") or _td.get("close") or 0)
                if _p > 0:
                    _ot_px_cache[_r["pair"]] = _p
        except (KeyError, TypeError, ValueError):
            pass

    # ── Badges ────────────────────────────────────────────────────────────────
    _badge_map = {
        "full":      "🌅 6AM FULL SCAN",
        "morning":   "🌏 9AM MORNING CHECK",
        "prelondon": "🌆 5PM PRE-LONDON CHECK",
        "preny":     "🌃 11PM PRE-NEW YORK CHECK",
    }
    _badge = _badge_map.get(scan_mode, "🤖 Forex AI")

    if yes_trades:
        setup_line = f"<b>🟢 {len(yes_trades)} setup{'s' if len(yes_trades) > 1 else ''} found</b>"
    elif _dd_mode == "halt":
        setup_line = "🚨 <b>HALT MODE — no new trades</b>"
    elif _dd_mode in ("preservation", "defensive", "caution"):
        _tier_limit = {"preservation": "A + all TFs", "defensive": "A-grade", "caution": "A/B-grade"}.get(_dd_mode, "")
        setup_line = f"No qualifying setups ({_tier_limit} required)"
    else:
        setup_line = "No setups today"

    # ── Nested helpers ────────────────────────────────────────────────────────

    def _fundamental_lines(result: dict, compact: bool = False) -> list:
        """Return Telegram lines for fundamental alignment.  Computes and caches alignment."""
        fa = result.get("_fundamental_alignment")
        if fa is None:
            _fa_p = result.get("pair", "")
            _fa_d = (result.get("parsed", {}).get("direction") or "").upper()
            if _fa_p and "/" in _fa_p and _fa_d:
                try:
                    from src import fundamentals as _fund_fl
                    _fb, _fq = _fa_p.split("/")
                    fa = _fund_fl.get_fundamental_alignment(_fb, _fq, _fa_d)
                    result["_fundamental_alignment"] = fa
                except Exception:
                    fa = {}
            else:
                fa = {}
        if not fa:
            return []

        alignment   = fa.get("alignment", "MIXED")
        conf_adj    = fa.get("conf_adj", 0)
        aligned     = fa.get("aligned", 0)
        opposed     = fa.get("opposed", 0)
        cb_s        = fa.get("cb_score", 0)
        carry_s     = fa.get("carry_score", 0)
        econ_s      = fa.get("econ_score", 0)

        def _tk(s):
            return "✅" if s > 0 else ("❌" if s < 0 else "➖")

        if alignment == "TAILWIND":
            hdr = f"🌊 <b>FUNDAMENTAL TAILWIND</b> — {aligned}/3 factors aligned (conf +1)"
        elif alignment == "HEADWIND":
            hdr = f"⛔ <b>FIGHTING THE FUNDAMENTALS</b> — {opposed}/3 factors opposed (conf −1)"
        else:
            hdr = f"📊 <b>Mixed fundamentals</b> — {aligned}/3 aligned"

        if compact:
            tick_str = f"CB {_tk(cb_s)} · Carry {_tk(carry_s)} · Econ {_tk(econ_s)}"
            cb_b = fa.get("cb_base",  "neutral")
            cb_q = fa.get("cb_quote", "neutral")
            pair = result.get("pair", "?")
            base_c = pair.split("/")[0] if "/" in pair else "?"
            quot_c = pair.split("/")[1] if "/" in pair else "?"
            adj_str = f" · conf {'−1' if conf_adj < 0 else ('+1' if conf_adj > 0 else '±0')}"
            return [
                f"{hdr}",
                f"   {tick_str} · {base_c} {cb_b} CB · {quot_c} {cb_q} CB{adj_str}",
            ]

        # Full detail for trade blocks — plain English
        pair   = result.get("pair", "?")
        dirn   = (result.get("parsed", {}).get("direction") or "?").upper()
        base_c = pair.split("/")[0] if "/" in pair else "?"
        quot_c = pair.split("/")[1] if "/" in pair else "?"

        cb_base_b  = fa.get("cb_base",  "neutral")
        cb_quot_b  = fa.get("cb_quote", "neutral")
        cb_note_b  = fa.get("cb_note_base",  "")
        cb_note_q  = fa.get("cb_note_quote", "")
        carry_diff = fa.get("carry_diff", 0)
        econ_base  = fa.get("econ_base",  "neutral")
        econ_quote = fa.get("econ_quote", "neutral")

        _CB_NAMES = {
            "USD": "US Federal Reserve", "EUR": "European Central Bank",
            "GBP": "Bank of England",    "JPY": "Bank of Japan",
            "AUD": "Reserve Bank of Australia", "NZD": "Reserve Bank of New Zealand",
            "CAD": "Bank of Canada",     "CHF": "Swiss National Bank",
        }
        _CCY_NAMES = {
            "USD": "US Dollar",       "EUR": "Euro",          "GBP": "British Pound",
            "JPY": "Japanese Yen",    "AUD": "Australian Dollar", "NZD": "New Zealand Dollar",
            "CAD": "Canadian Dollar", "CHF": "Swiss Franc",   "HKD": "Hong Kong Dollar",
            "SGD": "Singapore Dollar","NOK": "Norwegian Krone","SEK": "Swedish Krona",
        }

        def _cb_plain(bias, note, ccy):
            cb_name = _CB_NAMES.get(ccy, f"{ccy} central bank")
            if bias == "hawkish":
                act = note if note else "raising interest rates"
                return f"{cb_name} is {act} — favours {ccy}"
            if bias == "dovish":
                act = note if note else "cutting interest rates"
                return f"{cb_name} is {act} — works against {ccy}"
            return f"{cb_name} is holding rates steady — neutral for {ccy}"

        def _econ_plain(surprise, ccy):
            ccy_name = _CCY_NAMES.get(ccy, ccy)
            if surprise in ("strong", "positive"):
                return f"{ccy_name} economic data has been stronger than expected recently"
            if surprise in ("weak", "negative"):
                return f"{ccy_name} economic data has been weaker than expected recently"
            return f"{ccy_name} economic data has been mixed recently"

        carry_helps   = (carry_diff > 0 and dirn == "BUY") or (carry_diff < 0 and dirn == "SELL")
        carry_hurts   = (carry_diff < 0 and dirn == "BUY") or (carry_diff > 0 and dirn == "SELL")
        trade_ccy     = base_c if dirn == "BUY" else quot_c

        lines = [hdr, "📊 <b>Economic backdrop:</b>"]

        # Central bank
        _cb_desc = _cb_plain(cb_base_b, cb_note_b, base_c)
        lines.append(f"   {_tk(cb_s)} <b>Central bank:</b> {_cb_desc}")

        # Carry / interest rate advantage
        if carry_helps:
            lines.append(
                f"   {_tk(carry_s)} <b>Interest rate advantage:</b> You earn interest daily holding {trade_ccy} — adds to this trade"
            )
        elif carry_hurts:
            lines.append(
                f"   {_tk(carry_s)} <b>Interest rate advantage:</b> Holding {trade_ccy} costs money daily — works against this trade"
            )
        else:
            lines.append(
                f"   {_tk(carry_s)} <b>Interest rate advantage:</b> Carry is neutral — no daily cost advantage"
            )

        # Economic data
        _econ_desc = _econ_plain(econ_base, base_c)
        lines.append(f"   {_tk(econ_s)} <b>Economic data:</b> {_econ_desc}")
        return lines

    def _trade_block(r: dict) -> list:
        """Build the full trade alert block for one YES result."""
        p         = r["parsed"]
        pair      = r["pair"]
        direction = (p.get("direction") or "?").upper()
        conf      = p.get("confidence") or "?"
        entry_raw = p.get("entry")
        stop_raw  = p.get("stop_loss")
        tgt_raw   = p.get("target")
        sz        = _sizes.get(pair, {})
        adj_stop  = sz.get("adj_stop") or stop_raw
        adj_tgt   = sz.get("adj_target") or tgt_raw

        rr_num = None
        try:
            rr_num = abs(float(adj_tgt) - float(entry_raw)) / abs(float(entry_raw) - float(adj_stop))
            rr_str = f"{rr_num:.2f}:1"
        except (TypeError, ValueError, ZeroDivisionError):
            try:
                rr_num = float(p.get("reward_risk") or 0)
                rr_str = f"{rr_num:.2f}:1" if rr_num else "—"
            except (TypeError, ValueError):
                rr_str = "—"

        risk_amt   = float(sz.get("risk_amount") or 0)
        cur        = sz.get("currency", "USD")
        pct        = sz.get("risk_pct", 1.0)
        profit_amt = None
        try:
            profit_amt = round(risk_amt * rr_num) if rr_num else None
        except (TypeError, ValueError):
            pass

        pip_risk   = _fmt_pips_between(pair, entry_raw, adj_stop)
        pip_target = _fmt_pips_between(pair, entry_raw, adj_tgt)
        action_icon = "🟢" if direction == "BUY" else "🔴"
        _fp  = f"{pair}:{direction}"
        _pfx = "🆕 NEW: " if (new_alerts and _fp in new_alerts) else ""

        rf  = (p.get("risk_factors") or "").strip()
        rf_parts = [x.strip() for x in rf.replace(";", "\n").split("\n") if x.strip()][:2]

        # Precise entry window from currency group
        _ew_tb  = _entry_window_for_pair(pair)
        _ew_sh, _ew_sm = _ew_tb[0], _ew_tb[1]
        _ew_win = _ew_tb[4]   # "5:00pm–6:30pm"
        _ew_cut = _ew_tb[5]   # "9:00pm"
        _ew_ses = _ew_tb[6]   # "London open"
        _eq_em, _eq_lb = _entry_quality(pair, now_ak)
        _tref_tb = _time_ref_for_entry(_ew_sh, _ew_sm, now_ak)
        _ideal_verb = "pull back to" if direction == "BUY" else "push up to"

        # ATR display for trade block
        _tb_daily = r.get("bundle", {}).get("technical", {}).get("daily", {})
        _tb_atr14 = float(_tb_daily.get("atr14") or 0) if isinstance(_tb_daily, dict) else 0
        _tb_pip   = _pip_size(pair)
        _tb_atr_line = None
        if _tb_atr14 > 0:
            try:
                _stop_d   = abs(float(entry_raw) - float(adj_stop))
                _tgt_d    = abs(float(adj_tgt)   - float(entry_raw))
                _stop_m   = _stop_d / _tb_atr14
                _tgt_m    = _tgt_d  / _tb_atr14
                _atr_pips = round(_tb_atr14  / _tb_pip)
                _stop_p   = round(_stop_d    / _tb_pip)
                _tgt_p    = round(_tgt_d     / _tb_pip)
                _tb_atr_line = (
                    f"📏 Stop {_stop_m:.1f}x ATR ({_stop_p}p) · "
                    f"Target {_tgt_m:.1f}x ATR ({_tgt_p}p) · "
                    f"ATR={_atr_pips}p"
                )
            except (TypeError, ValueError, ZeroDivisionError):
                pass

        # Monthly trend filter — counter-trend trades penalised −2 on displayed confidence
        _monthly_trend_line = None
        _monthly_trend_data = {}
        try:
            from src import technical as _tech_mt
            if pair not in _monthly_trends:
                _monthly_trends[pair] = _tech_mt.get_monthly_trend(pair)
            _monthly_trend_data = _monthly_trends.get(pair, {})
        except Exception:
            pass

        # R:R < 1.5 → LOW QUALITY flag; reduce displayed confidence by 1
        _low_quality = rr_num is not None and rr_num < 1.5
        _conf_display = conf
        if _low_quality:
            try:
                _conf_display = str(max(1, int(conf) - 1))
            except (TypeError, ValueError):
                pass

        # Economic calendar: if either currency has a HIGH impact event within
        # 48 hours, reduce displayed confidence by 1 and store warning lines.
        _cal_warn_lines = []
        try:
            from src import economic_calendar as _ec_tb
            _cal_ev_48h = _ec_tb.events_for_pair(pair, hours=48)
            if _cal_ev_48h:
                try:
                    _conf_display = str(max(1, int(_conf_display) - 1))
                except (TypeError, ValueError):
                    pass
                _cal_warn_lines = _ec_tb.warning_lines_for_pair(pair, _cal_ev_48h)
        except Exception:
            pass

        # Second opinion boost: if DA found no compelling objections, show +0.5 on display
        _so_tb = r.get("second_opinion")
        if _so_tb and not _so_tb.get("has_objections"):
            try:
                _conf_display = f"{float(_conf_display) + 0.5:.1f}"
            except (TypeError, ValueError):
                pass

        # Currency strength alignment boost (+1) and note
        _cs_boost_line = None
        _cs_boost = 0
        try:
            from src import currency_strength as _cs_tb
            _tb_parts = pair.split("/")
            if len(_tb_parts) == 2 and _ccy_strength:
                _cs_boost, _cs_note = _cs_tb.alignment_note(
                    _tb_parts[0], _tb_parts[1], direction, _ccy_strength
                )
                if _cs_boost:
                    try:
                        _conf_display = str(min(10, int(float(_conf_display)) + _cs_boost))
                    except (TypeError, ValueError):
                        pass
                if _cs_note:
                    _cs_boost_line = _cs_note
        except Exception:
            pass

        # Monthly trend: apply −2 penalty for counter-trend trades; build display line
        _mt_trend    = _monthly_trend_data.get("trend", "NEUTRAL")
        _mt_strength = _monthly_trend_data.get("strength", "weak")
        _mt_aligned  = (
            (direction == "BUY"  and _mt_trend == "BUY")  or
            (direction == "SELL" and _mt_trend == "SELL")
        )
        _mt_neutral  = (_mt_trend == "NEUTRAL")
        if _mt_trend != "NEUTRAL":
            if not _mt_aligned:
                # Counter-trend: −2 confidence
                try:
                    _conf_display = str(max(1, round(float(_conf_display) - 2)))
                except (TypeError, ValueError):
                    pass
                _monthly_trend_line = (
                    f"⚠️ Monthly trend: {_mt_trend} — this {direction} trade is "
                    f"counter-trend — treat with extra caution — "
                    f"tighter position size recommended"
                )
            else:
                _str_label = f"{_mt_strength} " if _mt_strength != "weak" else ""
                _monthly_trend_line = (
                    f"📊 Monthly trend: {_mt_trend} — this trade is aligned with "
                    f"the dominant {_str_label}monthly trend — "
                    f"highest probability direction"
                )

        # Trend structure filter — HH/HL for BUY, LH/LL for SELL; penalty −2 if broken
        _ts_line = None
        _hhhl_aligned_tb = None
        try:
            from src import technical as _tech_ts
            if pair not in _trend_structures:
                _trend_structures[pair] = _tech_ts.get_trend_structure(pair)
            _ts = _trend_structures.get(pair, {})
            if _ts.get("status") == "ok":
                if direction == "BUY":
                    _ts_valid = _ts.get("buy_valid", False)
                    _hhhl_aligned_tb = 1 if _ts_valid else 0
                    if _ts_valid:
                        _ts_line = (
                            "📊 Trend structure: ✅ Higher highs and higher lows confirmed — "
                            "uptrend intact — BUY signal valid"
                        )
                    else:
                        _ts_specific = (
                            "price is making lower lows" if _ts.get("ll") else
                            "price is not making higher highs" if not _ts.get("hh") else
                            "price is not making higher lows"
                        )
                        _ts_line = (
                            f"⚠️ Trend structure broken — {_ts_specific} — "
                            f"BUY signal is counter-structure — higher risk"
                        )
                        try:
                            _conf_display = str(max(1, round(float(_conf_display) - 2)))
                        except (TypeError, ValueError):
                            pass
                elif direction == "SELL":
                    _ts_valid = _ts.get("sell_valid", False)
                    _hhhl_aligned_tb = 1 if _ts_valid else 0
                    if _ts_valid:
                        _ts_line = (
                            "📊 Trend structure: ✅ Lower highs and lower lows confirmed — "
                            "downtrend intact — SELL signal valid"
                        )
                    else:
                        _ts_specific = (
                            "price is making higher highs" if _ts.get("hh") else
                            "price is not making lower highs" if not _ts.get("lh") else
                            "price is not making lower lows"
                        )
                        _ts_line = (
                            f"⚠️ Trend structure broken — {_ts_specific} — "
                            f"SELL signal is counter-structure — higher risk"
                        )
                        try:
                            _conf_display = str(max(1, round(float(_conf_display) - 2)))
                        except (TypeError, ValueError):
                            pass
            else:
                _hhhl_aligned_tb = None  # insufficient data — no penalty
        except Exception:
            pass

        # Kill zone timing — +0.5 when pair is in an aligned session window; −0.5 outside all
        _kz_line = None
        _kz_key  = "OUTSIDE"
        _kz_result = {}
        try:
            from src import kill_zones as _kz_mod
            _kz_parts = pair.split("/")
            if len(_kz_parts) == 2:
                _kz_result = _kz_mod.check(_kz_parts[0], _kz_parts[1])
                _kz_key    = _kz_result.get("zone_key") or "OUTSIDE"
                _kz_delta  = _kz_result.get("conf_delta", 0.0)
                _kz_line   = _kz_result.get("display_line")
                if _kz_delta:
                    try:
                        _kz_new = max(1.0, min(10.0,
                            round((float(_conf_display) + _kz_delta) * 2) / 2))
                        _conf_display = (
                            str(int(_kz_new)) if _kz_new == int(_kz_new)
                            else f"{_kz_new:.1f}"
                        )
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass

        # Market structure break — +1.5 when break confirms trade direction
        _ms_line = None
        _ms_result_tb = "CONTINUATION"
        try:
            from src import technical as _tech_ms
            if pair not in _mstruct_cache:
                _mstruct_cache[pair] = _tech_ms.get_market_structure(pair)
            _ms = _mstruct_cache.get(pair, {})
            _ms_result_tb = _ms.get("result", "CONTINUATION")
            _ms_break_lvl = _ms.get("break_level")
            _ms_aligned   = (
                (direction == "BUY"  and _ms_result_tb == "BULLISH_BREAK") or
                (direction == "SELL" and _ms_result_tb == "BEARISH_BREAK")
            )
            if _ms_aligned and _ms_break_lvl is not None:
                try:
                    _ms_new = max(1.0, min(10.0, float(_conf_display) + 1.5))
                    _conf_display = (
                        str(int(_ms_new)) if _ms_new == int(_ms_new)
                        else f"{_ms_new:.1f}"
                    )
                except (TypeError, ValueError):
                    pass
                _ms_lvl_fmt = _fmt_price(_ms_break_lvl)
                if direction == "BUY":
                    _ms_line = (
                        f"📊 Market structure: ✅ Bullish break — price just closed "
                        f"above the previous swing high at {_ms_lvl_fmt} — "
                        f"this signals the downtrend has ended and confirms the BUY "
                        f"direction — confidence boosted"
                    )
                else:
                    _ms_line = (
                        f"📊 Market structure: ✅ Bearish break — price just closed "
                        f"below the previous swing low at {_ms_lvl_fmt} — "
                        f"this signals the uptrend has ended and confirms the SELL "
                        f"direction — confidence boosted"
                    )
            else:
                if direction == "BUY":
                    _ms_line = (
                        "📊 Market structure: ➡️ Continuation — existing downtrend "
                        "intact — no bullish structure break yet — standard confidence"
                    )
                else:
                    _ms_line = (
                        "📊 Market structure: ➡️ Continuation — existing uptrend "
                        "intact — no bearish structure break yet — standard confidence"
                    )
        except Exception:
            pass

        # RSI divergence — +1.0 regular (reversal), +0.5 hidden (continuation)
        _div_line = None
        _div_type_tb = "NONE"
        try:
            from src import technical as _tech_div
            if pair not in _rsi_div_cache:
                _rsi_div_cache[pair] = _tech_div.get_rsi_divergence(pair)
            _div = _rsi_div_cache.get(pair, {})

            if direction == "BUY":
                _div_type_tb = _div.get("low_divergence", "NONE")
                _div_det     = _div.get("low_details") or {}
            else:
                _div_type_tb = _div.get("high_divergence", "NONE")
                _div_det     = _div.get("high_details") or {}

            _div_boost = (
                1.0 if _div_type_tb in ("BULLISH", "BEARISH") else
                0.5 if _div_type_tb in ("HIDDEN_BULLISH", "HIDDEN_BEARISH") else
                0.0
            )
            if _div_boost and _div_det:
                try:
                    _div_new = max(1.0, min(10.0, float(_conf_display) + _div_boost))
                    _conf_display = (
                        str(int(_div_new)) if _div_new == int(_div_new)
                        else f"{_div_new:.1f}"
                    )
                except (TypeError, ValueError):
                    pass
                _div_pl = _fmt_price(_div_det.get("price_level", 0))
                _div_rl = _div_det.get("rsi_level", 0)
                if _div_type_tb == "BULLISH":
                    _div_line = (
                        f"📊 RSI divergence confirmed: Price made a lower low at "
                        f"{_div_pl} but RSI made a higher low at {_div_rl} — "
                        f"bullish momentum divergence — historically one of the most "
                        f"reliable reversal signals — confidence boosted by 1"
                    )
                elif _div_type_tb == "BEARISH":
                    _div_line = (
                        f"📊 RSI divergence confirmed: Price made a higher high at "
                        f"{_div_pl} but RSI made a lower high at {_div_rl} — "
                        f"bearish momentum divergence — historically one of the most "
                        f"reliable reversal signals — confidence boosted by 1"
                    )
                elif _div_type_tb == "HIDDEN_BULLISH":
                    _div_line = (
                        f"📊 RSI divergence confirmed: Price made a higher low at "
                        f"{_div_pl} but RSI made a lower low at {_div_rl} — "
                        f"hidden bullish divergence — confirms uptrend continuation — "
                        f"confidence boosted by 0.5"
                    )
                else:  # HIDDEN_BEARISH
                    _div_line = (
                        f"📊 RSI divergence confirmed: Price made a lower high at "
                        f"{_div_pl} but RSI made a higher high at {_div_rl} — "
                        f"hidden bearish divergence — confirms downtrend continuation — "
                        f"confidence boosted by 0.5"
                    )
            else:
                _div_line = (
                    "📊 RSI divergence: none detected — standard RSI signal only"
                )
        except Exception:
            pass

        _qg_tb = _quality_grades.get(pair, _trade_quality_grade(r))
        _tb_grade = (_qg_tb or {}).get("grade", "B")

        # ── Pre-trade quality checklist (10 criteria, +1 each) ──────────────
        _cl_weekly_sig     = (r.get("bundle", {}).get("mtf", {})
                               .get("signals", {}).get("weekly", "NEUTRAL"))
        _cl_weekly_aligned = (_cl_weekly_sig == direction)

        _cl_cot_base = (r.get("bundle", {}).get("positioning", {})
                         .get("base", {})) or {}
        _cl_cot_dir  = str(_cl_cot_base.get("direction", "")).upper()
        _cl_cot_mom  = _cl_cot_base.get("cot_momentum", "STABLE")
        _cl_cot_ok   = (
            (direction == "BUY"  and "LONG"  in _cl_cot_dir and _cl_cot_mom != "REVERSING") or
            (direction == "SELL" and "SHORT" in _cl_cot_dir and _cl_cot_mom != "REVERSING")
        )

        _cl_no_news      = True
        _cl_news_hrs_str = None
        try:
            from src import economic_calendar as _ec_cl
            _cl_news_ev = _ec_cl.events_for_pair(pair, hours=24)
            _cl_no_news = not bool(_cl_news_ev)
            if _cl_news_ev:
                _cl_news_hrs_str = str(round(_cl_news_ev[0].get("hours_away") or 0))
        except Exception:
            pass

        _cl_fib_near = bool((_qg_tb or {}).get("fib_near", False))
        _cl_rr_ok    = (rr_num is not None and rr_num >= 2.5)
        _cl_kz_ok    = bool((_kz_result or {}).get("aligned", False))
        _cl_hhhl_ok  = (_hhhl_aligned_tb == 1)
        _cl_div_ok   = (_div_type_tb not in ("NONE", None, ""))

        _cl_criteria = [
            _mt_aligned,           # 1. Monthly trend aligned
            _cl_weekly_aligned,    # 2. Weekly trend aligned
            _cl_hhhl_ok,           # 3. Daily HHHL structure confirmed
            _cl_fib_near,          # 4. Fibonacci entry zone
            _cl_div_ok,            # 5. RSI divergence confirmed
            (_cs_boost > 0),       # 6. Currency strength double-aligned
            _cl_cot_ok,            # 7. COT institutional aligned
            _cl_no_news,           # 8. No high impact news within 24h
            _cl_rr_ok,             # 9. R:R >= 2.0
            _cl_kz_ok,             # 10. Kill zone
        ]
        _cl_score = sum(1 for c in _cl_criteria if c)

        if _cl_score < 7:
            # Trade is already OPEN in CSV — cannot un-open it.
            # Log prominently so user knows about it; do NOT silently drop it.
            _log_line(log, (
                f"[checklist] WARNING {pair} opened with low checklist score "
                f"{_cl_score}/10 — trade is OPEN, monitoring closely"
            ))
            # Fall through so the trade appears in the Telegram report with its score.

        _cl_news_label = (
            f"No high impact news within 24h"
            if _cl_no_news
            else f"News event in {_cl_news_hrs_str}h — slight caution"
        )
        _cl_rr_label = (
            f"R:R {rr_str}"
            if _cl_rr_ok
            else f"R:R {rr_str} (below 2.5:1 target)"
        )
        _cl_item_labels = [
            "Monthly trend aligned",
            "Weekly trend aligned",
            "Daily trend structure confirmed",
            "Fibonacci entry zone",
            "RSI divergence confirmed",
            "Currency strength double-aligned",
            "COT institutional aligned",
            _cl_news_label,
            _cl_rr_label,
            "Kill zone entry",
        ]
        _cl_display = [f"📋 Pre-trade checklist: {_cl_score}/10"]
        for _cl_lbl, _cl_val in zip(_cl_item_labels, _cl_criteria):
            _cl_display.append(f"{'✅' if _cl_val else '❌'} {_cl_lbl}")
        if _cl_score >= 9:
            _cl_display.append(
                "🏆 Premium setup — 1.25-1.5% position size recommended"
            )
        else:
            _cl_display.append(
                "💼 Standard setup — 0.75-1.0% position size recommended"
            )

        # Plain English description of what the trade is betting on
        _CCY_FULL_TB = {
            "USD": "US Dollar", "EUR": "Euro", "GBP": "British Pound",
            "JPY": "Japanese Yen", "AUD": "Australian Dollar", "NZD": "New Zealand Dollar",
            "CAD": "Canadian Dollar", "CHF": "Swiss Franc", "HKD": "Hong Kong Dollar",
            "SGD": "Singapore Dollar", "NOK": "Norwegian Krone", "SEK": "Swedish Krona",
        }
        _base_tb = pair.split("/")[0] if "/" in pair else pair[:3]
        _quot_tb = pair.split("/")[1] if "/" in pair else pair[3:]
        _base_nm_tb = _CCY_FULL_TB.get(_base_tb, _base_tb)
        _quot_nm_tb = _CCY_FULL_TB.get(_quot_tb, _quot_tb)
        if direction == "BUY":
            _plain_desc_tb = f"Betting the {_base_nm_tb} will strengthen against the {_quot_nm_tb}"
        else:
            _plain_desc_tb = f"Betting the {_base_nm_tb} will weaken against the {_quot_nm_tb}"

        if _tb_grade == "A":
            _grade_alert_hdr = "🚨 <b>TRADE ALERT — GRADE A</b>"
            _action_line = f"{action_icon} <b>{_pfx}{direction} {pair} NOW — Grade A · Confidence {_conf_display}/10</b>"
        elif _tb_grade == "B":
            _grade_alert_hdr = "⚡ <b>TRADE ALERT — GRADE B</b>"
            _action_line = f"🟡 <b>{_pfx}{direction} {pair} — Grade B · Confidence {_conf_display}/10</b>"
        else:
            _grade_alert_hdr = None
            _action_line = f"👁 <b>{_pfx}WATCH ONLY: {direction} {pair} — Grade {_tb_grade}</b>"

        block = [""]
        if _cl_score >= 9:
            block.append("🏆 <b>PREMIUM SETUP</b>")
        if _grade_alert_hdr:
            block.append(_grade_alert_hdr)
            block.append("")
        block += [
            _action_line,
            f"({_plain_desc_tb})",
        ]
        if _monthly_trend_line:
            block.append(_monthly_trend_line)
        if _ts_line:
            block.append(_ts_line)
        if _kz_line:
            block.append(_kz_line)
        if _ms_line:
            block.append(_ms_line)
        if _div_line:
            block.append(_div_line)
        block += _cl_display
        block += [
            "━━━━━━━━━━━━━━━━━━━━━",
            _grade_display_line(_qg_tb),
            "━━━━━━━━━━━━━━━━━━━━━",
            f"💰 Entry: {_fmt_price(entry_raw)}",
            f"🛑 Stop Loss: {_fmt_price(adj_stop)}  ({pip_risk} risk)",
            f"🎯 Take Profit: {_fmt_price(adj_tgt)}  ({pip_target} target)",
        ]
        if _tb_atr_line:
            block.append(_tb_atr_line)

        # ── Adaptive / standard cascade targets display ───────────────────────
        if _tb_atr14 > 0:
            try:
                _pst_e = _pair_stats_all.get(pair.upper(), {})
                _pst_n = _pst_e.get("n_trades_with_mfe", 0)
                _e_f   = float(entry_raw)
                if _pst_e.get("adaptive_active") and _pst_n >= 10:
                    _t1m = _pst_e["t1_mult"]
                    _t2m = _pst_e["t2_mult"]
                    _t3m = _pst_e["t3_mult"]
                    _ap  = round(_tb_atr14 / _tb_pip)
                    _sgn = 1 if direction == "BUY" else -1
                    _at1 = _e_f + _sgn * _t1m * _tb_atr14
                    _at2 = _e_f + _sgn * _t2m * _tb_atr14
                    _at3 = _e_f + _sgn * _t3m * _tb_atr14
                    block += [
                        "",
                        f"📊 <b>Targets (adaptive — {_pst_n} {pair} trades):</b>",
                        f"T1: {_fmt_price(_at1)} ({_t1m:.2f}x ATR — {round(_t1m * _ap)}p)",
                        f"T2: {_fmt_price(_at2)} ({_t2m:.2f}x ATR — {round(_t2m * _ap)}p)",
                        f"T3: {_fmt_price(_at3)} ({_t3m:.2f}x ATR — {round(_t3m * _ap)}p)",
                        "Based on your own historical data for this pair ✅",
                    ]
                else:
                    _needed = max(0, 10 - _pst_n)
                    _ap     = round(_tb_atr14 / _tb_pip)
                    _sgn    = 1 if direction == "BUY" else -1
                    _st1    = _e_f + _sgn * 0.4 * _tb_atr14
                    _st2    = _e_f + _sgn * 0.7 * _tb_atr14
                    _st3    = _e_f + _sgn * 1.0 * _tb_atr14
                    block += [
                        "",
                        f"📊 <b>Targets (standard — need {_needed} more trades):</b>",
                        f"T1: {_fmt_price(_st1)} (0.40x ATR — {round(0.4 * _ap)}p)",
                        f"T2: {_fmt_price(_st2)} (0.70x ATR — {round(0.7 * _ap)}p)",
                        f"T3: {_fmt_price(_st3)} (1.00x ATR — {round(1.0 * _ap)}p)",
                    ]
            except Exception:
                pass

        if _low_quality:
            block.append(f"⚠️ <b>LOW QUALITY SETUP — R:R {rr_str} below 1.5:1 minimum (confidence −1)</b>")
        if profit_amt and risk_amt:
            block.append(f"📊 Risk ${risk_amt:,.0f} → Make ${profit_amt:,.0f}  ({rr_str} reward)")
        elif risk_amt:
            block.append(f"📊 Risk ${risk_amt:,.0f} {cur}  ({pct:.2f}% account)")
        if sz.get("lots"):
            _lots_display = sz["lots"]
            try:
                from src import market_regime as _mr_sz
                if _mr_sz.detect().get("regime") == "ranging_high_vol":
                    _lots_display = max(0.01, round(_lots_display * 0.5, 2))
            except Exception:
                pass
            block.append(f"📏 Position Size: {_lots_display} lots")

        # ── Cost viability ────────────────────────────────────────────────────
        try:
            from src import trade_costs as _tc_tb
            _viab_tb = _tc_tb.check_viability(pair, direction, entry_raw, adj_stop, adj_tgt)
            if not _viab_tb["viable"]:
                block.append(
                    f"🚫 <b>NOT VIABLE AFTER COSTS — net R:R {_viab_tb['net_rr']:.1f}:1</b> "
                    f"(gross {_viab_tb['gross_rr']:.1f}:1 minus "
                    f"{_viab_tb['total_cost_pips']:.1f}p costs)"
                )
            else:
                block.append(
                    f"✅ Net R:R {_viab_tb['net_rr']:.1f}:1 after "
                    f"{_viab_tb['total_cost_pips']:.1f}p costs "
                    f"(gross {_viab_tb['gross_rr']:.1f}:1 · "
                    f"{_viab_tb['gross_pips']:.0f}p target)"
                )
        except Exception:
            pass

        # ── Calendar news warning ────────────────────────────────────────────────
        if _cal_warn_lines:
            block.append("")
            for _cw in _cal_warn_lines:
                block.append(_cw)

        # ── Currency strength alignment note ─────────────────────────────────────
        if _cs_boost_line:
            block.append(_cs_boost_line)

        # ── Plain English outcome summary ─────────────────────────────────────
        if risk_amt and profit_amt:
            block += [
                "",
                "💡 <b>What this trade means:</b>",
                f"If price reaches target: <b>+${profit_amt:,.0f} profit</b> on this trade",
                f"If stop loss is hit: <b>-${risk_amt:,.0f} loss</b> on this trade",
                f"Net risk to reward: you risk ${risk_amt:,.0f} to potentially make ${profit_amt:,.0f}",
            ]

        # ── Fibonacci levels ──────────────────────────────────────────────────
        _fib = (r.get("bundle", {}).get("technical", {})
                 .get("daily", {}).get("fibonacci", {}))
        if isinstance(_fib, dict) and _fib.get("status") == "ok":
            _fib_above = _fib.get("nearest_above", [])
            _fib_below = _fib.get("nearest_below", [])
            _fib_near  = _fib.get("near_levels", [])
            _rng_p     = _fib.get("range_pips", "?")
            block.append("━━━━━━━━━━━━━━━━━━━━━")
            block.append(f"📐 <b>Fibonacci Levels</b>  (swing range: {_rng_p}p)")
            if _fib_above:
                block.append("  🔴 Resistance: " + " | ".join(
                    f"{lb} {_fmt_price(px)}" for lb, px in _fib_above))
            if _fib_below:
                block.append("  🟢 Support: " + " | ".join(
                    f"{lb} {_fmt_price(px)}" for lb, px in _fib_below))
            if _fib_near:
                _fn = _fib_near[0]
                block.append(
                    f"  ⭐ Price {_fn['distance_pips']:.0f}p from {_fn['label']}"
                    f" {_fn['type']} — triple confluence zone"
                )

        # For 7+ confidence: find the nearest Fibonacci support (BUY) or resistance
        # (SELL) closest to Claude's entry price and use it as the ideal entry level.
        _is_high_conf = False
        try:
            _is_high_conf = int(conf) >= 7
        except (TypeError, ValueError):
            pass

        _fib_entry_price = entry_raw
        _fib_entry_label = None
        if _is_high_conf and isinstance(_fib, dict) and _fib.get("status") == "ok":
            _fib_lvls = (
                _fib.get("nearest_below", []) if direction == "BUY"
                else _fib.get("nearest_above", [])
            )
            if _fib_lvls:
                try:
                    _best_fib = min(
                        _fib_lvls,
                        key=lambda lp: abs(float(lp[1]) - float(entry_raw)),
                    )
                    _fib_entry_price = _best_fib[1]
                    _fib_entry_label = str(_best_fib[0])
                except (TypeError, ValueError, IndexError):
                    pass

        _cur_price = None
        try:
            _cp_raw = (
                _tb_daily.get("last_close") or _tb_daily.get("close")
                if isinstance(_tb_daily, dict) else None
            )
            if _cp_raw:
                _cur_price = float(_cp_raw)
        except (TypeError, ValueError):
            pass

        # ── Confluence zone detection (7+ confidence only) ────────────────────
        if _is_high_conf and _fib_entry_price is not None:
            _cz_entry = None
            try:
                _cz_entry = float(_fib_entry_price)
            except (TypeError, ValueError):
                pass

            if _cz_entry is not None:
                _cz_window = _tb_pip * 10   # 10-pip window for technical levels
                _cz_win_rn = _tb_pip * 20   # 20-pip window for round numbers

                _cz_factors = []

                # Factor 1: Fibonacci retracement (38.2%, 50%, or 61.8%)
                try:
                    if isinstance(_fib, dict) and _fib.get("status") == "ok":
                        _cz_fib_pool = (
                            _fib.get("nearest_below", []) + _fib.get("nearest_above", [])
                        )
                        _cz_fib_hits = [
                            (lb, float(px)) for lb, px in _cz_fib_pool
                            if any(pct in str(lb) for pct in ("38.2", "50", "61.8"))
                            and abs(float(px) - _cz_entry) <= _cz_window
                        ]
                        if _cz_fib_hits:
                            _cz_fb = min(_cz_fib_hits, key=lambda x: abs(x[1] - _cz_entry))
                            _cz_factors.append(
                                f"Fibonacci {_cz_fb[0]} retracement at {_fmt_price(_cz_fb[1])}"
                            )
                except Exception:
                    pass

                # Factor 2: Recent swing high or low (direction-appropriate)
                try:
                    _cz_sw_key   = "recent_low_20"  if direction == "BUY" else "recent_high_20"
                    _cz_sw_label = "swing low"       if direction == "BUY" else "swing high"
                    _cz_sw_val   = (
                        float(_tb_daily.get(_cz_sw_key) or 0)
                        if isinstance(_tb_daily, dict) else 0
                    )
                    if _cz_sw_val > 0 and abs(_cz_sw_val - _cz_entry) <= _cz_window:
                        _cz_factors.append(
                            f"Recent 20-day {_cz_sw_label} at {_fmt_price(_cz_sw_val)}"
                        )
                except Exception:
                    pass

                # Factor 3: Round number (00 or 50 level — wider window)
                try:
                    _cz_pr_pips    = round(_cz_entry / _tb_pip)
                    _cz_nearest_50 = round(_cz_pr_pips / 50) * 50
                    _cz_rn_price   = round(_cz_nearest_50 * _tb_pip, 5)
                    if abs(_cz_rn_price - _cz_entry) <= _cz_win_rn:
                        _cz_rn_type = "00 level" if (_cz_nearest_50 % 100 == 0) else "50 level"
                        _cz_factors.append(
                            f"Round number {_fmt_price(_cz_rn_price)} ({_cz_rn_type})"
                            f" nearby — acts as price magnet"
                        )
                except Exception:
                    pass

                # Factor 4: 200 day moving average
                try:
                    _cz_sma200 = (
                        _tb_daily.get("sma200") if isinstance(_tb_daily, dict) else None
                    )
                    if _cz_sma200 and _cz_sma200 != "n/a":
                        if abs(float(_cz_sma200) - _cz_entry) <= _cz_window:
                            _cz_factors.append(
                                f"200 day moving average at {_fmt_price(_cz_sma200)}"
                            )
                except Exception:
                    pass

                # Factor 5: Bollinger Band (midline, upper, or lower — whichever is nearest)
                try:
                    _cz_bb_hits = []
                    for _cz_bb_key, _cz_bb_lbl in [
                        ("sma20",    "Bollinger Band midline (20 SMA)"),
                        ("bb_upper", "Bollinger upper band"),
                        ("bb_lower", "Bollinger lower band"),
                    ]:
                        _cz_bb_val = (
                            _tb_daily.get(_cz_bb_key) if isinstance(_tb_daily, dict) else None
                        )
                        if _cz_bb_val:
                            _cz_bb_dist = abs(float(_cz_bb_val) - _cz_entry)
                            if _cz_bb_dist <= _cz_window:
                                _cz_bb_hits.append((_cz_bb_dist, _cz_bb_lbl, float(_cz_bb_val)))
                    if _cz_bb_hits:
                        _cz_bb_best = min(_cz_bb_hits, key=lambda x: x[0])
                        _cz_factors.append(
                            f"{_cz_bb_best[1]} at {_fmt_price(_cz_bb_best[2])}"
                        )
                except Exception:
                    pass

                # Factor 6: Weekly range high or low
                try:
                    _cz_wk_tech  = (
                        r.get("bundle", {}).get("technical", {}).get("weekly", {})
                    )
                    if isinstance(_cz_wk_tech, dict):
                        _cz_wk_key   = "recent_low_20"   if direction == "BUY" else "recent_high_20"
                        _cz_wk_label = "weekly range low" if direction == "BUY" else "weekly range high"
                        _cz_wk_val   = float(_cz_wk_tech.get(_cz_wk_key) or 0)
                        if _cz_wk_val > 0 and abs(_cz_wk_val - _cz_entry) <= _cz_window:
                            _cz_factors.append(
                                f"Previous {_cz_wk_label} at {_fmt_price(_cz_wk_val)}"
                            )
                except Exception:
                    pass

                # Score and confidence adjustment
                _cz_count = len(_cz_factors)
                if _cz_count <= 1:
                    _cz_conf_delta  = -0.5
                    _cz_verdict_em  = "⚠️"
                    _cz_verdict_txt = (
                        "LOW CONFLUENCE — entering at a random price level — "
                        "lower probability setup — confidence reduced"
                    )
                elif _cz_count <= 3:
                    _cz_conf_delta  = 0
                    _cz_verdict_em  = "📊"
                    _cz_verdict_txt = (
                        "MODERATE CONFLUENCE — reasonable entry with "
                        "multiple confirming factors"
                    )
                else:
                    _cz_conf_delta  = 1
                    _cz_verdict_em  = "🎯"
                    _cz_verdict_txt = (
                        "HIGH CONFLUENCE ZONE — multiple independent reasons for "
                        "price to bounce here — highest probability entry available"
                    )

                if _cz_conf_delta != 0:
                    try:
                        _cz_new = max(1.0, round(float(_conf_display) + _cz_conf_delta, 1))
                        _conf_display = (
                            str(int(_cz_new)) if _cz_new == int(_cz_new) else str(_cz_new)
                        )
                    except (TypeError, ValueError):
                        pass

                block.append("━━━━━━━━━━━━━━━━━━━━━")
                block.append(
                    f"📐 <b>Entry confluence: {_cz_count} "
                    f"factor{'s' if _cz_count != 1 else ''} "
                    f"align at {_fmt_price(_cz_entry)}</b>"
                )
                for _cz_desc in _cz_factors:
                    block.append(f"✅ {_cz_desc}")
                if not _cz_factors:
                    block.append("❌ No key technical levels found near this entry")
                block.append(f"{_cz_verdict_em} {_cz_verdict_txt}")

        block.append("━━━━━━━━━━━━━━━━━━━━━")
        if _is_high_conf and _fib_entry_price is not None and _cur_price is not None:
            _pb_move_word   = "dip"    if direction == "BUY" else "rally"
            _pb_participant = "buyers" if direction == "BUY" else "sellers"
            _pb_chase_word  = "higher" if direction == "BUY" else "lower"

            # Pips between current price and pullback entry level
            _pb_dist_pips = None
            try:
                _pb_dist_pips = round(abs(_cur_price - float(_fib_entry_price)) / _tb_pip)
            except (TypeError, ValueError, ZeroDivisionError):
                pass

            # True when price already ran through the entry without pulling back
            _pb_past_entry = False
            try:
                _pb_past_entry = (
                    float(_cur_price) <= float(_fib_entry_price) if direction == "BUY"
                    else float(_cur_price) >= float(_fib_entry_price)
                )
            except (TypeError, ValueError):
                pass

            # R:R entering at current market price
            _pb_rr_market_str = None
            try:
                if direction == "BUY":
                    _pb_rr_mkt = abs(float(adj_tgt) - _cur_price) / abs(_cur_price - float(adj_stop))
                else:
                    _pb_rr_mkt = abs(_cur_price - float(adj_tgt)) / abs(float(adj_stop) - _cur_price)
                _pb_rr_market_str = f"{_pb_rr_mkt:.1f}:1"
            except (TypeError, ValueError, ZeroDivisionError):
                pass

            # R:R entering at the pullback level (only valid if pullback is inside the stop)
            _pb_rr_pullback_str = None
            try:
                _fep = float(_fib_entry_price)
                _ast = float(adj_stop)
                _at  = float(adj_tgt)
                if direction == "BUY" and _fep > _ast:
                    _pb_rr_pullback_str = f"{abs(_at - _fep) / abs(_fep - _ast):.1f}:1"
                elif direction == "SELL" and _fep < _ast:
                    _pb_rr_pullback_str = f"{abs(_fep - _at) / abs(_ast - _fep):.1f}:1"
            except (TypeError, ValueError, ZeroDivisionError):
                pass

            # Confluence: is the 20-period swing high/low within 15 pips of the fib level?
            _pb_why = None
            try:
                _pb_swing_key  = "recent_low_20"  if direction == "BUY" else "recent_high_20"
                _pb_swing_type = "previous swing low" if direction == "BUY" else "previous swing high"
                _pb_swing_val  = float(_tb_daily.get(_pb_swing_key) or 0)
                _pb_thresh     = _tb_pip * 15
                _fep_f = float(_fib_entry_price)
                if _pb_swing_val > 0 and abs(_pb_swing_val - _fep_f) <= _pb_thresh:
                    if _fib_entry_label:
                        _pb_why = (
                            f"Fibonacci {_fib_entry_label} retracement AND {_pb_swing_type} "
                            f"both sit at {_fmt_price(_fib_entry_price)} — this is where "
                            f"{_pb_participant} are likely to step in strongly"
                        )
                    else:
                        _pb_why = (
                            f"Key Fibonacci retracement AND {_pb_swing_type} "
                            f"both sit at {_fmt_price(_fib_entry_price)} — this is where "
                            f"{_pb_participant} are likely to step in strongly"
                        )
                else:
                    if _fib_entry_label:
                        _pb_why = (
                            f"Fibonacci {_fib_entry_label} retracement sits at "
                            f"{_fmt_price(_fib_entry_price)} — this is where "
                            f"{_pb_participant} are likely to step in"
                        )
                    else:
                        _pb_why = (
                            f"Key price level at {_fmt_price(_fib_entry_price)} — "
                            f"this is where {_pb_participant} are likely to step in"
                        )
            except (TypeError, ValueError):
                _pb_why = (
                    f"Key price level at {_fmt_price(_fib_entry_price)} — "
                    f"this is where {_pb_participant} are likely to step in"
                )

            if _pb_past_entry:
                block += [
                    "⏳ <b>PULLBACK ENTRY</b>",
                    "Price ran away without a pullback — skip this trade — "
                    "wait for the next pullback opportunity on this pair",
                ]
            else:
                _pb_dip_str = (
                    f"price needs to {_pb_move_word} {_pb_dist_pips} pips to the ideal level"
                    if _pb_dist_pips else
                    f"price needs to {_pb_move_word} to this level"
                )
                block += [
                    f"⏳ <b>PULLBACK ENTRY</b> — do not enter at current price",
                    f"Current price: {_fmt_price(_cur_price)}",
                    f"Ideal entry level: {_fmt_price(_fib_entry_price)} — {_pb_dip_str}",
                    f"Why this level: {_pb_why}",
                    "",
                    f"If price reaches {_fmt_price(_fib_entry_price)} — enter immediately",
                    f"If price does not reach {_fmt_price(_fib_entry_price)} by {_ew_cut} Auckland — skip this trade entirely",
                ]
                if _pb_rr_pullback_str and _pb_rr_market_str:
                    block.append(
                        f"Do not chase price {_pb_chase_word} — a patient entry at "
                        f"{_fmt_price(_fib_entry_price)} gives you R:R of {_pb_rr_pullback_str} "
                        f"vs only {_pb_rr_market_str} if you enter now"
                    )
                else:
                    block.append(
                        f"Do not chase price {_pb_chase_word} — "
                        "wait for the pullback to get the best risk to reward"
                    )
        else:
            block += [
                f"⏰ <b>EXACT ENTRY INSTRUCTIONS:</b>",
                f"{_eq_em} {_eq_lb}",
                f"⏰ ENTER TRADE: Between {_ew_win} Auckland {_tref_tb}",
                f"⛔ DO NOT ENTER after {_ew_cut} Auckland",
                f"⚡ IDEAL ENTRY: Wait for price to {_ideal_verb} {_fmt_price(entry_raw)} then enter",
                f"- Do NOT enter if price moves more than 30 pips from entry before {_ew_ses}",
                "- If price gaps past entry on open — skip this trade entirely",
            ]
        block += [
            "━━━━━━━━━━━━━━━━━━━━━",
            f"📈 Confidence: {_conf_display}/10  {_conf_bar(_conf_display)}",
        ]
        # MTF timeframe display line
        try:
            _mtf_tb      = r.get("bundle", {}).get("mtf", {})
            _mtf_gate_tb = _mtf_tb.get("mtf_gate", "") if isinstance(_mtf_tb, dict) else ""
            _mtf_sigs_tb = _mtf_tb.get("signals", {}) if isinstance(_mtf_tb, dict) else {}
            if _mtf_gate_tb and _mtf_sigs_tb:
                def _sig_word(sig, tf_for_neutral):
                    if sig in ("BUY", "SELL"):
                        return sig
                    return "consolidating (neutral)" if tf_for_neutral == "daily" else "neutral"
                _w_word   = _sig_word(_mtf_sigs_tb.get("weekly", "NEUTRAL"), "weekly")
                _d_word   = _sig_word(_mtf_sigs_tb.get("daily",  "NEUTRAL"), "daily")
                _h4_word  = _sig_word(_mtf_sigs_tb.get("h4",     "NEUTRAL"), "h4")
                _tf_bar   = f"Timeframes: Weekly {_w_word} · Daily {_d_word} · 4H {_h4_word}"
                _dir_word = "uptrend" if direction == "BUY" else "downtrend"
                if _mtf_gate_tb == "strong_all3":
                    _tf_tag = "all timeframes aligned — highest quality setup"
                elif _mtf_gate_tb == "strong_w_d":
                    _tf_tag = "weekly and daily aligned — valid setup"
                elif _mtf_gate_tb == "strong_w_4h":
                    _tf_tag = f"classic pullback entry within weekly {_dir_word} — high quality setup"
                elif _mtf_gate_tb in ("weak_weekly_only", "weak_mixed"):
                    _tf_tag = "weekly trend only — reduced confidence — watch list"
                else:
                    _tf_tag = None
                if _tf_tag:
                    block.append(f"📊 {_tf_bar} — {_tf_tag}")
        except Exception:
            pass
        # Second opinion summary line
        _so_tb = r.get("second_opinion")
        if _so_tb:
            if _so_tb.get("has_objections"):
                _so_nc = _so_tb.get("n_compelling", 0)
                block.append(f"⚠️ Second opinion flagged {_so_nc} concern(s) — see risk factors below.")
            else:
                block.append("🔍 Second opinion: No major red flags found — confidence boosted.")
        # ML win-probability
        try:
            from src import ml_predictor as _mlp_tb
            _wp_tb = _mlp_tb.get_win_prob(pair, pp, r.get("bundle", {}))
            if _wp_tb:
                block.append(f"🤖 <b>Predicted win probability: {_wp_tb}</b>")
        except Exception:
            pass
        # RSI divergence (Python-computed — reliable, not dependent on Claude)
        _daily_tech = r.get("bundle", {}).get("technical", {}).get("daily", {})
        _div        = _daily_tech.get("divergence", {}) if isinstance(_daily_tech, dict) else {}
        _div_bul    = _div.get("bullish")
        _div_ber    = _div.get("bearish")
        _div_confirms = (direction == "BUY" and _div_bul) or (direction == "SELL" and _div_ber)
        _div_conflicts = (direction == "BUY" and _div_ber) or (direction == "SELL" and _div_bul)
        if _div_confirms:
            _d = _div_bul if direction == "BUY" else _div_ber
            block.append(
                f"⚡ <b>RSI Divergence: {'Bullish' if direction == 'BUY' else 'Bearish'} CONFIRMED</b>"
                f" — price {_d['price_diff_pips']:.0f}p {'lower low' if direction == 'BUY' else 'higher high'}"
                f", RSI {_d['rsi_diff']:.0f}pt {'higher low' if direction == 'BUY' else 'lower high'}"
                f" ({_d['strength'].upper()}) +1 confidence"
            )
        elif _div_conflicts:
            _dc = _div_ber if direction == "BUY" else _div_bul
            block.append(
                f"⚠️ RSI Divergence CONFLICT: "
                f"{'bearish' if direction == 'BUY' else 'bullish'} divergence detected"
                f" ({_dc['price_diff_pips']:.0f}p / {_dc['rsi_diff']:.0f}pt RSI) — see risk factors"
            )
        # Oscillator confluence (RSI + Stochastic + CCI)
        _osc        = _daily_tech.get("oscillator_confluence", {}) if isinstance(_daily_tech, dict) else {}
        _osc_dir    = _osc.get("direction", "NONE")
        _osc_score  = _osc.get("score", 0)
        _osc_triple = _osc.get("triple", False)
        if _osc_dir == direction and _osc_score >= 2:
            _osc_state = "OVERSOLD" if direction == "BUY" else "OVERBOUGHT"
            _osc_boost = "+2 confidence" if _osc_triple else "+1 confidence"
            _osc_label = "TRIPLE" if _osc_triple else f"PARTIAL ({_osc_score}/3)"
            _osc_sigs  = " | ".join(filter(None, [
                f"RSI {_osc.get('stoch_k','?'):.0f}" if isinstance(_osc.get('stoch_k'), float) else None,
                f"Stoch %K {_osc.get('stoch_k','?')}" if _osc.get('stoch_k') is not None else None,
                f"CCI {_osc.get('cci','?')}" if _osc.get('cci') is not None else None,
            ]))
            _rsi_v  = _daily_tech.get("rsi14", "?")
            _stk_v  = _osc.get("stoch_k", "?")
            _cci_v  = _osc.get("cci", "?")
            block.append(
                f"📊 <b>Oscillator Confluence: {_osc_label} {_osc_state}</b> ({_osc_boost})"
            )
            block.append(
                f"   RSI {_rsi_v} | Stoch %K {_stk_v} | CCI {_cci_v}"
            )
        elif _osc_dir not in ("NONE", direction) and _osc_score >= 2:
            block.append(
                f"⚠️ Oscillator conflict: {_osc_dir.lower()} signals"
                f" (Stoch %K {_osc.get('stoch_k','?')} | CCI {_osc.get('cci','?')}) — see risk factors"
            )
        _mtf = r.get("bundle", {}).get("mtf", {})
        if _mtf and _mtf.get("agreeing_count", 0) > 0:
            _mtf_txt_tb = _mtf_plain_english(_mtf, trade_direction=direction)
            if _mtf_txt_tb:
                block.append(f"🕐 {_mtf_txt_tb}")
        _rib_line = _ribbon_display(r.get("bundle", {}))
        if _rib_line:
            block.append(_rib_line)
            _rib_status = (r.get("bundle", {}).get("technical", {}).get("daily", {}) or {}).get("ribbon", {}).get("status", "")
            _rib_bull = _rib_status in ("ALIGNED_BULL", "LEANING_BULL")
            _rib_bear = _rib_status in ("ALIGNED_BEAR", "LEANING_BEAR")
            if (_rib_bull and direction == "SELL") or (_rib_bear and direction == "BUY"):
                block.append("⚠️ <b>MA Ribbon conflict — ribbon aligned against trade direction, higher risk</b>")
                try:
                    _conf_adj = int(conf) - 1
                    if _rib_status in ("ALIGNED_BULL", "ALIGNED_BEAR"):
                        block.append(f"Adjusted confidence: {_conf_adj}/10 (−1 for strong ribbon conflict)")
                except (TypeError, ValueError):
                    pass
        # COT reversal warning
        if _cot_reversal_penalty(r) < 0:
            block.append(
                "🔄 <b>COT REVERSAL WARNING — institutional positioning just flipped against "
                "this trade direction — confidence penalised −1</b>"
            )
        # Smart Money Divergence (Layer 10) — plain English
        _smd_tb   = _smd_score(r)
        _smd_data = r.get("bundle", {}).get("smart_money", {})
        if isinstance(_smd_data, dict) and _smd_data.get("status") not in ("insufficient_data", None):
            _smd_bi  = float(_smd_data.get("base_inst",   0) or 0)
            _smd_qi  = float(_smd_data.get("quote_inst",  0) or 0)
            _smd_br  = float(_smd_data.get("base_retail", 0) or 0)
            _smd_qr  = float(_smd_data.get("quote_retail",0) or 0)
            _smd_base_ccy  = pair.split("/")[0] if "/" in pair else "BASE"
            _smd_quote_ccy = pair.split("/")[1] if "/" in pair else "QUOTE"

            def _smd_action(score):
                if score > 0.3:  return "buying"
                if score < -0.3: return "selling"
                return "neutral on"

            def _smd_retail_stance(score):
                if score > 0.3:  return "very bullish"
                if score > 0.1:  return "mildly bullish"
                if score < -0.3: return "very bearish"
                if score < -0.1: return "mildly bearish"
                return "neutral"

            # Describe the most prominent institutional signal
            if abs(_smd_bi) >= abs(_smd_qi):
                _smd_desc_ccy  = _smd_base_ccy
                _smd_desc_act  = _smd_action(_smd_bi)
                _smd_desc_ret  = _smd_retail_stance(_smd_br)
            else:
                _smd_desc_ccy  = _smd_quote_ccy
                _smd_desc_act  = _smd_action(_smd_qi)
                _smd_desc_ret  = _smd_retail_stance(_smd_qr)

            _smd_conflicts = (
                (_smd_tb >= 5 and direction == "SELL") or (_smd_tb <= -5 and direction == "BUY")
            )
            _smd_supports  = (
                (_smd_tb >= 5 and direction == "BUY")  or (_smd_tb <= -5 and direction == "SELL")
            )
            _smd_icon = "🐋" if abs(_smd_tb) >= 8 else ("📊" if abs(_smd_tb) >= 5 else "💡")

            block.append(
                f"{_smd_icon} <b>Smart Money Signal:</b> Big investors are {_smd_desc_act} "
                f"{_smd_desc_ccy} while regular traders are {_smd_desc_ret}"
            )
            if _smd_conflicts:
                block.append(f"This conflicts with our {direction} signal — treat with extra caution")
                block.append("When big money and our signal disagree the risk is higher")
            elif _smd_supports and abs(_smd_tb) >= 8:
                block.append(f"This strongly supports our {direction} signal — institutional confirmation")
                block.append("When big money and our signal agree the probability of success is higher")
            elif _smd_supports and abs(_smd_tb) >= 5:
                block.append(f"Mild institutional support for our {direction} signal")
        # Fundamental alignment
        block += _fundamental_lines(r, compact=False)
        block.append("🔍 <b>Why all data agrees:</b>")
        for why_line in _why_agrees_lines(r, ctx):
            block.append(why_line)
        block += [
            "━━━━━━━━━━━━━━━━━━━━━",
            "⚠️ <b>Risk Factors:</b>",
        ]
        for rf_line in (rf_parts or ([rf[:100]] if rf else ["—"])):
            block.append(f"- {rf_line}")
        block.append("━━━━━━━━━━━━━━━━━━━━━")
        block.append("📋 <b>Trade Management After Entry:</b>")
        try:
            for mgmt in _management_lines(
                direction, float(entry_raw), float(adj_stop), float(adj_tgt), pair
            ):
                block.append(mgmt)
        except (TypeError, ValueError):
            block.append(f"- Full target: {_fmt_price(adj_tgt)}")
        # ── Adaptive position sizing display ──────────────────────────────────
        _fs_sz_tb = r.get("_fs_sizing")
        if _fs_sz_tb:
            _as_pct_tb  = float(_fs_sz_tb.get("pct") or 1.0)
            _as_mode_tb = str(_fs_sz_tb.get("mode") or "normal")
            _as_rsn_tb  = str(_fs_sz_tb.get("reason") or "")
            _as_bal_tb  = float(_fs_sz_tb.get("balance") or 0)
            if _as_bal_tb > 0:
                _as_usd_tb = round(_as_bal_tb * _as_pct_tb / 100, 0)
                _mode_icon = (
                    "⚡" if _as_mode_tb == "win_streak"
                    else ("🛡️" if "drawdown" in _as_mode_tb else "💼")
                )
                block.append(
                    f"{_mode_icon} Adaptive position size: {_as_pct_tb:.2f}% of fund "
                    f"(${_as_usd_tb:,.0f} at risk)"
                )
                if _as_rsn_tb:
                    block.append(f"   Sizing reason: {_as_rsn_tb}")
        return [ln for ln in block if ln]

    def _watch_entry(rr: dict) -> list:
        """Build watch list entry (conf 5–6) with indicative levels and session time."""
        _qg_we = _quality_grades.get(rr["pair"]) or _trade_quality_grade(rr)
        if not _qg_we or _qg_we.get("grade") == "F":
            return []
        pp    = rr["parsed"]
        conf  = _eff_conf(rr)   # ribbon-adjusted confidence — consistent with threshold checks
        dirn  = (pp.get("direction") or "—").upper()
        arrow = "📈" if dirn == "BUY" else "📉"
        ntc   = _what_needs_to_change(pp)
        # Compute entry quality badge first so it appears in header
        _ew_we    = _entry_window_for_pair(rr["pair"])
        _eq_we_e, _eq_we_l = _entry_quality(rr["pair"], now_ak)
        _tref_we  = _time_ref_for_entry(_ew_we[0], _ew_we[1], now_ak)
        _start_we = _fmt_time_exact(_ew_we[0], _ew_we[1])
        lines = [
            "",
            f"{arrow} <b>{rr['pair']}</b> {dirn}  {conf}/10 {_conf_bar(conf)}  {_eq_we_e} {_eq_we_l}",
            _grade_display_line(_qg_we),
            f"{_score_breakdown_line(pp, smd=_smd_score(rr))}",
        ]
        # ML win-probability (only show when model is trained and ready)
        try:
            from src import ml_predictor as _mlp_we
            _wp_we = _mlp_we.get_win_prob(rr["pair"], pp, rr.get("bundle", {}))
            if _wp_we and "learning" not in _wp_we:
                lines.append(f"🤖 Predicted win probability: {_wp_we}")
        except Exception:
            pass
        _wl_hint = _weakest_layer_hint(pp)
        if _wl_hint:
            lines.append(f"↑ {_wl_hint}")
        _mtf_wl = rr.get("bundle", {}).get("mtf", {})
        if _mtf_wl and _mtf_wl.get("agreeing_count", 0) > 0:
            _mtf_txt = _mtf_plain_english(_mtf_wl, trade_direction=dirn)
            if _mtf_txt:
                lines.append(_mtf_txt)
        _rib_wl = _ribbon_display(rr.get("bundle", {}))
        if _rib_wl:
            lines.append(_rib_wl)
            _rib_wl_status = (rr.get("bundle", {}).get("technical", {}).get("daily", {}) or {}).get("ribbon", {}).get("status", "")
            _rib_wl_bull = _rib_wl_status in ("ALIGNED_BULL", "LEANING_BULL")
            _rib_wl_bear = _rib_wl_status in ("ALIGNED_BEAR", "LEANING_BEAR")
            if (_rib_wl_bull and dirn == "SELL") or (_rib_wl_bear and dirn == "BUY"):
                lines.append("⚠️ <b>MA Ribbon conflict — confidence penalised −1, higher risk</b>")
        # COT reversal warning
        if _cot_reversal_penalty(rr) < 0:
            lines.append(
                "🔄 <b>COT REVERSAL WARNING — institutional positioning just flipped — "
                "confidence penalised −1</b>"
            )
        # Smart Money Divergence (compact)
        _smd_we   = _smd_score(rr)
        _smd_d_we = rr.get("bundle", {}).get("smart_money", {})
        if isinstance(_smd_d_we, dict) and _smd_d_we.get("status") not in ("insufficient_data", None):
            _smd_sig_we = _smd_d_we.get("signal", "NEUTRAL")
            _smd_icon_we = "🐋" if abs(_smd_we) >= 8 else "📊"
            _smd_boost_we = " +1 conf" if (
                (_smd_we >= 8 and dirn == "BUY") or (_smd_we <= -8 and dirn == "SELL")
            ) else ""
            _smd_warn_we = " ⚠️ contradicts trade" if (
                (_smd_we >= 8 and dirn == "SELL") or (_smd_we <= -8 and dirn == "BUY")
            ) else ""
            lines.append(
                f"{_smd_icon_we} SMD: {_smd_we:+d}/10 [{_smd_sig_we}]{_smd_boost_we}{_smd_warn_we}"
            )
        # Fundamental alignment (compact)
        lines += _fundamental_lines(rr, compact=True)
        # D/F grade: monitoring only — suppress all entry instructions
        if (_qg_we or {}).get("grade") == "F":
            return []
        if (_qg_we or {}).get("grade") == "D":
            lines.append("⚠️ Grade D — worth monitoring — conditions improving but not ready to enter yet")
            return lines
        # Indicative entry/stop/target — always shown so investor knows the trade shape
        ind_e, ind_s, ind_t, ind_meta = _calc_indicative_levels(rr["pair"], pp, rr.get("bundle", {}))
        if ind_e and ind_s and ind_t:
            is_jpy = "JPY" in rr["pair"].upper()
            dec = 3 if is_jpy else 5
            try:
                rr_ratio   = abs(float(ind_t) - float(ind_e)) / abs(float(ind_e) - float(ind_s))
                rr_ratio   = max(rr_ratio, 1.3)
                risk_pips  = abs(float(ind_e) - float(ind_s)) / _pip_size(rr["pair"])
                profit_pips= abs(float(ind_t) - float(ind_e)) / _pip_size(rr["pair"])
                risk_usd   = max(1, round(risk_pips))
                profit_usd = max(1, round(profit_pips))
                lines.append("🟡 <b>READY TO TRADE IF CONFIRMED:</b>")
                if ind_meta.get("quality_flag"):
                    lines.append(f"⚠️ <b>{ind_meta['quality_flag']}</b> — R:R {rr_ratio:.1f}:1 below 1.5 minimum")
                lines.append(
                    f"Entry ~{ind_e:.{dec}f} | "
                    f"Stop ~{ind_s:.{dec}f} | Target ~{ind_t:.{dec}f}"
                )
                lines.append(f"Risk ${risk_usd} → Make ${profit_usd} ({rr_ratio:.1f}:1)")
                # Net R:R after costs
                try:
                    from src import trade_costs as _tc_we
                    _viab_we = _tc_we.check_viability(rr["pair"], dirn, ind_e, ind_s, ind_t)
                    if not _viab_we["viable"]:
                        lines.append(
                            f"🚫 <b>NOT VIABLE AFTER COSTS — net R:R {_viab_we['net_rr']:.1f}:1</b> "
                            f"(gross {rr_ratio:.1f}:1 minus {_viab_we['total_cost_pips']:.1f}p costs)"
                        )
                    elif _viab_we.get("net_rr", 0) > 0:
                        lines.append(
                            f"✅ Net R:R {_viab_we['net_rr']:.1f}:1 after "
                            f"{_viab_we['total_cost_pips']:.1f}p costs"
                        )
                except Exception:
                    pass
                # ATR multiples display
                _sm = ind_meta.get("stop_atr_mult")
                _tm = ind_meta.get("target_atr_mult")
                _atr_val = ind_meta.get("atr")
                if _sm is not None and _tm is not None and _atr_val:
                    _atr_pip = round(_atr_val / _pip_size(rr["pair"]))
                    lines.append(
                        f"📏 Stop {_sm}x ATR ({round(risk_pips):.0f}p) · "
                        f"Target {_tm}x ATR ({round(profit_pips):.0f}p) · "
                        f"ATR={_atr_pip}p"
                    )
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        _sess_we_active, _sess_we_close = _session_status_for_pair(rr["pair"], now_ak)
        if _sess_we_active:
            lines += [
                f"{_eq_we_e} <b>{_ew_we[6]} currently active — closes {_sess_we_close} Auckland</b>",
                f"If confidence reaches 7+ now — enter immediately at market price",
                f"If confidence has not reached 7+ by {_ew_we[5]} — skip this pair today",
                f"Needs: {ntc}",
            ]
        else:
            lines += [
                f"{_eq_we_e} <b>BE READY TO ENTER:</b> {_ew_we[6]} {_start_we} Auckland {_tref_we}",
                f"If confidence reaches 7+ before {_start_we} — enter immediately at market price",
                f"If confidence reaches 7+ during {_ew_we[6]} — enter within 30 minutes of the open",
                f"If confidence has not reached 7+ by {_ew_we[5]} {_tref_we} — skip this pair today",
                f"Needs: {ntc}",
            ]
        return lines

    def _approaching_entry(rr: dict) -> list:
        """Build approaching signal entry (conf 3–4) with indicative levels and entry alert time."""
        if (_quality_grades.get(rr["pair"]) or _trade_quality_grade(rr)).get("grade") == "F":
            return []
        pp   = rr["parsed"]
        conf = _eff_conf(rr)   # ribbon-adjusted confidence
        dirn = (pp.get("direction") or "—").upper()
        _qg_ap = _quality_grades.get(rr["pair"], _trade_quality_grade(rr))
        lines = [
            "",
            f"<b>{rr['pair']}</b> {conf}/10 {dirn} — if conditions improve:",
            _grade_display_line(_qg_ap),
        ]
        ind_e, ind_s, ind_t, ind_meta = _calc_indicative_levels(rr["pair"], pp, rr.get("bundle", {}))
        is_jpy = "JPY" in rr["pair"].upper()
        dec = 3 if is_jpy else 5
        if _qg_ap["grade"] == "D":
            _ntc_ap   = _what_needs_to_change(pp)
            _bar_n    = int(max(1, min(conf, 6)))
            _bar      = "█" * _bar_n + "░" * (7 - _bar_n)
            _rr_d_str = ""
            if ind_e and ind_s and ind_t:
                try:
                    _risk_d = abs(float(ind_e) - float(ind_s))
                    _prof_d = abs(float(ind_t) - float(ind_e))
                    if _risk_d:
                        _rr_d_str = f" · R:R {round(_prof_d / _risk_d, 1)}:1"
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
            _ew_d    = _entry_window_for_pair(rr["pair"])
            _ses_d   = _ew_d[6] if len(_ew_d) > 6 else ""
            _time_d  = _fmt_time_exact(_ew_d[0], _ew_d[1])
            return [
                "",
                f"<b>{rr['pair']}</b> · Grade D · {conf}/10 {_bar} — ({_ntc_ap}) — Getting closer — not ready yet{_rr_d_str} — ⏰ Check at {_ses_d} {_time_d} Auckland",
            ]
        lines.append("🟠 <b>POTENTIAL SETUP IF CONDITIONS IMPROVE:</b>")
        if ind_e and ind_s and ind_t:
            try:
                lines.append(
                    f"Entry ~{ind_e:.{dec}f} | "
                    f"Stop ~{ind_s:.{dec}f} | Target ~{ind_t:.{dec}f}"
                )
                _sm_ap = ind_meta.get("stop_atr_mult")
                _tm_ap = ind_meta.get("target_atr_mult")
                _atr_ap = ind_meta.get("atr")
                if _sm_ap is not None and _tm_ap is not None and _atr_ap:
                    _pip_ap = _pip_size(rr["pair"])
                    lines.append(
                        f"📏 Stop {_sm_ap}x ATR · Target {_tm_ap}x ATR · "
                        f"ATR={round(_atr_ap / _pip_ap)}p"
                    )
                # Net R:R viability
                try:
                    from src import trade_costs as _tc_ap
                    _viab_ap = _tc_ap.check_viability(rr["pair"], dirn, ind_e, ind_s, ind_t)
                    if not _viab_ap["viable"]:
                        lines.append(
                            f"🚫 NOT VIABLE AFTER COSTS — net R:R {_viab_ap['net_rr']:.1f}:1 "
                            f"(gross {_viab_ap['gross_rr']:.1f}:1 minus "
                            f"{_viab_ap['total_cost_pips']:.1f}p costs)"
                        )
                    elif _viab_ap.get("net_rr", 0) > 0:
                        lines.append(
                            f"Net R:R {_viab_ap['net_rr']:.1f}:1 after "
                            f"{_viab_ap['total_cost_pips']:.1f}p costs"
                        )
                except Exception:
                    pass
            except (TypeError, ValueError):
                lines.append("Price levels: insufficient data for this pair")
        else:
            lines.append("Price levels: insufficient data for this pair")
        _ew_ae = _entry_window_for_pair(rr["pair"])
        _eq_ae_e, _ = _entry_quality(rr["pair"], now_ak)
        _tref_ae = _time_ref_for_entry(_ew_ae[0], _ew_ae[1], now_ak)
        _start_ae = _fmt_time_exact(_ew_ae[0], _ew_ae[1])
        lines += [
            f"{_eq_ae_e} <b>SET ALERT FOR:</b> {_start_ae} Auckland {_tref_ae} — check if this pair improved",
            "If confidence reaches 6+ at any scan today — add to watch list",
        ]
        return lines

    # Currency strength scores — computed once in 6am block, used in _trade_block
    # and _log_one_research across all scan modes.  {} = no data (no boost applied).
    _ccy_strength: dict = {}
    # Monthly trend cache — lazy-populated per pair on first access in _trade_block
    _monthly_trends: dict = {}
    # Swing structure cache — lazy-populated per pair on first access in _trade_block
    _trend_structures: dict = {}
    # Market structure break cache — lazy-populated per pair on first access in _trade_block
    _mstruct_cache: dict = {}
    # RSI divergence cache — lazy-populated per pair on first access in _trade_block
    _rsi_div_cache: dict = {}

    # ═══════════════════════════════════════════════════════════════════════════
    # 6AM FULL SCAN — comprehensive morning briefing
    # ═══════════════════════════════════════════════════════════════════════════
    if scan_mode == "full":
        # ── DATA QUALITY ASSESSMENT — runs before message assembly so alerts fire first
        _dq_quality: dict = {}
        _dq_state:   dict = {}
        try:
            from src import data_quality as _dq
            _dq_quality = _dq.assess_scan(deep_results)
            _dq_state   = _dq.update_state(_dq_quality, scan_mode)
            # Immediate alert: >8 candle failures
            _dq_hv = _dq.high_volume_alert(_dq_quality)
            if _dq_hv:
                try:
                    _telegram(_dq_hv)
                except Exception:
                    pass
            # Immediate alerts: consecutive data source failures
            for _dq_alert in _dq.consecutive_alerts(_dq_state):
                try:
                    _telegram(_dq_alert)
                except Exception:
                    pass
            # Item 4: Critical data quality alert — fire before main report
            if _dq_quality.get("overall_pct", 100) < 30:
                try:
                    _telegram(
                        f"⚠️ CRITICAL DATA QUALITY — only {_dq_quality.get('overall_pct', 0):.0f}% "
                        f"of data sources responded — scan results may be unreliable"
                    )
                except Exception:
                    pass
        except Exception:
            pass

        _hdr_full = [
            f"<b>🤖 FOREX AI — 🌅 {_fmt_date_nz(now_ak)}</b>",
            f"Universe: {universe_size} · Deep analysed: <b>{n_deep}</b> · {setup_line}",
        ]
        all_sections.append(_hdr_full)
        if _dd_banner:
            all_sections.append(["", "━━━━━━━━━━━━━━━━━━━━━", _dd_banner])

        # DYNAMIC THRESHOLD — show beneath header for full scan
        try:
            from src import dynamic_threshold as _dth_disp
            _thr_lines = _dth_disp.build_telegram_lines(threshold_data)
            if _thr_lines:
                all_sections.append(["", "━━━━━━━━━━━━━━━━━━━━━"] + _thr_lines)
        except Exception:
            pass

        # OPEN TRADES — always at the top so it's the first thing seen
        _ot_conf_map = {r["pair"]: _eff_conf(r) for r in deep_results if r.get("pair")}
        # Confidence crisis: send immediate separate alert before main message
        for _cca_t in _ot_open_trades:
            _cca_p  = _cca_t.get("pair", "")
            _cca_ec = None
            try:
                _cca_ec = int(float(_cca_t.get("confidence") or 0)) or None
            except (TypeError, ValueError):
                pass
            _cca_cc = _ot_conf_map.get(_cca_p)
            if _cca_ec and _cca_cc is not None and _cca_cc < 3 and _cca_ec >= 3:
                try:
                    _telegram(
                        f"⚠️ Open trade warning — {_cca_p} confidence has fallen to "
                        f"{_cca_cc}/10 — the original trade rationale may no longer be valid "
                        f"— consider whether to exit early to avoid a larger loss"
                    )
                except Exception:
                    pass
        all_sections.append(_build_open_trades_section(_ot_open_trades, _ot_px_cache, now_ak, cur_conf_map=_ot_conf_map))

        # Currency strength — compute once here, reused in _trade_block + research logging
        try:
            from src import currency_strength as _cs_mod
            _ccy_strength = _cs_mod.compute()
        except Exception:
            _ccy_strength = {}

        # MARKET CONTEXT
        _patience = _compute_patience_score(ctx)
        # Detect regime once — 2h cache, shared by all downstream references
        _rd_ctx = {}
        try:
            from src import market_regime as _mr_ctx
            _rd_ctx = _mr_ctx.detect(ccy_strength=_ccy_strength)
        except Exception:
            pass
        _regime_key_ctx = (_rd_ctx.get("regime") or "").lower()
        ctx_lines = ["", "━━━━━━━━━━━━━━━━━━━━━", "🌍 <b>MARKET CONTEXT</b>"]
        vix_str = f"VIX {ctx['vix']:.1f}" if ctx["vix"] else ""
        env_str = ctx["risk_env"]
        ctx_lines.append(
            f"Environment: <b>{env_str}</b>"
            f"{' (' + vix_str + ')' if vix_str else ''}"
        )
        # Use _ccy_strength (same source as the strength table below) so the header
        # and the table always agree.  Fall back to ctx-derived scores if unavailable.
        if _ccy_strength:
            def _safe_cs_val(v):
                if isinstance(v, dict):
                    _vals = [x for x in v.values() if isinstance(x, (int, float))]
                    return float(_vals[0]) if _vals else 0.0
                try:
                    f = float(v or 0)
                    return f if f == f else 0.0
                except (TypeError, ValueError):
                    return 0.0
            _cs_flat = {k: _safe_cs_val(v) for k, v in _ccy_strength.items()}
            sc     = max(_cs_flat, key=_cs_flat.get) if _cs_flat else None
            wc     = min(_cs_flat, key=_cs_flat.get) if _cs_flat else None
            scores = _cs_flat
        else:
            sc     = ctx.get("strongest_ccy")
            wc     = ctx.get("weakest_ccy")
            scores = ctx.get("ccy_scores", {})
        _roff_ctx = "risk_off" in _regime_key_ctx or "risk-off" in env_str.lower()
        _safe_h = {"JPY", "CHF", "USD"}
        _risk_off_warn_ccys = {"AUD", "NZD", "GBP", "EUR", "NOK", "SEK", "CAD"}
        if sc:
            if _roff_ctx and sc in _risk_off_warn_ccys:
                # Risk-off: demote risk-on currencies — surface the strongest safe haven instead
                sc_reason = "data shows relative strength but regime favours safe havens — treat with caution"
                ctx_lines.append(f"⚠️ Strongest (risk-on skew): <b>{sc}</b> — {sc_reason} (+{scores.get(sc,0):.0f})")
                _sh_scores = {c: scores.get(c, 0) for c in _safe_h}
                _best_sh = max(_sh_scores, key=_sh_scores.get) if _sh_scores else None
                if _best_sh and _sh_scores.get(_best_sh, 0) > 0:
                    ctx_lines.append(f"💪 Regime favourite: <b>{_best_sh}</b> — safe haven demand (+{_sh_scores[_best_sh]:.0f})")
            elif _roff_ctx and sc in _safe_h:
                sc_reason = "safe haven demand — expected in risk-off environment"
                ctx_lines.append(f"💪 Strongest: <b>{sc}</b> — {sc_reason} (+{scores.get(sc,0):.0f})")
            else:
                sc_reason = "carry + risk-on" if "risk-on" in env_str else "carry + fundamentals"
                ctx_lines.append(f"💪 Strongest: <b>{sc}</b> — {sc_reason} (+{scores.get(sc,0):.0f})")
        if wc:
            if _roff_ctx:
                wc_reason = "risk-off selling pressure" if wc in _risk_off_warn_ccys else "weak fundamentals"
            else:
                wc_reason = "low rates + risk-on selling" if "risk-on" in env_str else "weak fundamentals"
            ctx_lines.append(f"📉 Weakest: <b>{wc}</b> — {wc_reason} ({scores.get(wc,0):.0f})")
        nw_list = []
        for r in deep_results:
            nw = (r["parsed"].get("news_warning") or "").strip()
            if nw and len(nw) > 5 and "none" not in nw.lower() and "n/a" not in nw.lower():
                _nw_trunc = nw if len(nw) <= 60 else nw[:60].rsplit(" ", 1)[0].rstrip(",;") + "…"
                nw_list.append(f"{r['pair']}: {_nw_trunc}")
        if nw_list:
            ctx_lines.append(f"⚡ {nw_list[0]}")
        # Currency strength meter — full ranking across all 12 currencies
        try:
            from src import currency_strength as _cs_ctx
            _cs_lines = _cs_ctx.strength_display_lines(_ccy_strength, scan_mode)
            if _cs_lines:
                ctx_lines.extend(_cs_lines)
        except Exception:
            pass
        if ctx.get("monthly_bias"):
            ctx_lines.append(f"📅 Monthly structural bias: <b>{ctx['monthly_bias']}</b> — background context only")
        # Market regime — authoritative plain-English environment + system behaviour
        try:
            for _reg_line in _mr_ctx.regime_display_lines(_rd_ctx):
                ctx_lines.append(_reg_line)
        except Exception:
            pass
        # Trading conditions — score is already capped by regime (no contradiction possible)
        _ps = _patience["score"]
        _pd = _patience["description"]
        ctx_lines.append(f"📊 <b>Today's trading conditions: {_ps}/10</b> — {_pd}")
        # Confidence threshold — use dynamic threshold (same value shown in Part 1 header)
        # to avoid contradicting the threshold displayed elsewhere in the message
        try:
            _conf_thr = float((threshold_data or {}).get("final_threshold") or 0)
            if not _conf_thr:
                _conf_thr = float(_rd_ctx.get("conf_threshold") or 0)
            if not _conf_thr:
                from src import threshold_manager as _tm_ctx
                _conf_thr = float(_tm_ctx.get_confidence_threshold())
            _thr_display = int(_conf_thr) if _conf_thr == int(_conf_thr) else _conf_thr
            ctx_lines.append(
                f"📊 <b>Today's confidence threshold: {_thr_display}/10</b>"
            )
        except Exception:
            pass

        # Opportunity gap detection: heatmap, gap alerts, velocity alerts
        try:
            _od = opportunity_data or {}
            _heatmap   = _od.get("heatmap", [])
            _gap_alerts = _od.get("gaps", [])
            _velocity  = _od.get("velocity", [])

            # Movement heatmap (top 3 movers)
            if _heatmap:
                _hm_parts = []
                for _hm in _heatmap[:3]:
                    _hm_parts.append(f"{_hm['pair']} {_hm['direction']}{_hm['pips']} pips")
                ctx_lines.append(
                    f"📊 <b>Biggest movers since last scan:</b> "
                    f"{' · '.join(_hm_parts)} — "
                    f"these pairs have been prioritised for analysis"
                )

            # Gap alerts
            for _gap in _gap_alerts:
                ctx_lines.append(
                    f"📊 Significant gap detected: {_gap['pair']} opened "
                    f"{_gap['pips']} pips {_gap['direction']} than last scan — "
                    f"this may represent a genuine breakout or a news reaction — "
                    f"forced into analysis"
                )

            # Velocity alerts (broad currency moves)
            for _vel in _velocity:
                ctx_lines.append(
                    f"📊 Broad {_vel['currency']} move detected — "
                    f"{_vel['count']} {_vel['currency']} pairs all {_vel['direction']} "
                    f"significantly since last scan — this suggests a macro "
                    f"{_vel['currency']} event — all {_vel['currency']} pairs have "
                    f"been prioritised"
                )
        except Exception:
            pass

        all_sections.append(ctx_lines)

        # ECONOMIC CALENDAR — 7-day high impact event timeline (every 6am scan)
        try:
            from src import economic_calendar as _ec_cal
            _cal_section = _ec_cal.build_calendar_section()
            if _cal_section:
                all_sections.append(_cal_section)
            else:
                _raw_ev = _ec_cal.get_events_7d()
                print(
                    f"[ECO-CAL] build_calendar_section() returned empty — "
                    f"get_events_7d() returned {len(_raw_ev)} events after HIGH-impact filter",
                    file=sys.stderr,
                )
                all_sections.append([
                    "", "━━━━━━━━━━━━━━━━━━━━━",
                    "📅 <b>UPCOMING HIGH IMPACT EVENTS</b>",
                    "No high-impact events scheduled for the next 7 days — clear sailing ahead",
                ])
        except Exception as _ec_err:
            import traceback as _tb_cal
            print(f"[ECO-CAL] Exception in economic calendar: {_ec_err}", file=sys.stderr)
            print(_tb_cal.format_exc(), file=sys.stderr)

        # MOVEMENT ALERTS (6am only — full-universe scan outside analysis batch)
        try:
            _mad = movement_alert_data or {}
            _ma_alerts = _mad.get("full_scan_alerts", [])
            _ma_weekly  = _mad.get("weekly", {})
            if _ma_alerts:
                _ma_sec = ["", "━━━━━━━━━━━━━━━━━━━━━",
                           "📊 <b>Movement alerts outside top 25:</b>"]
                for _ma in _ma_alerts[:5]:
                    _ma_sec.append(
                        f"⚡ {_ma['pair']} moved {_ma['pips']} pips since yesterday "
                        f"({_ma['atr_ratio']}x ATR) — added to priority sweep"
                    )
                all_sections.append(_ma_sec)
            elif _ma_weekly.get("movement_alerts_detected", 0) > 0:
                all_sections.append([
                    "", "━━━━━━━━━━━━━━━━━━━━━",
                    "📊 <b>Movement alerts:</b> ✅ All significant movers already in analysis batch",
                ])
        except Exception:
            pass

        # SYSTEM LEARNING REPORT (Monday 6am only)
        _slr = _build_system_learning_report(date)
        if _slr:
            all_sections.append(_slr)

        # FUND PROTECTION STATUS (every full scan)
        try:
            from src import fund_state as _fs_st
            _fs_st_lines = _fs_st.build_status_lines(_fund_st)
            _fs_sz_lines = _fs_st.build_sizing_status_lines(_fund_st)
            if _fund_st_blocked:
                _blk_pairs = ", ".join(
                    (_bt.get("pair") or "") for _bt, _ in _fund_st_blocked[:3]
                )
                _fs_st_lines.append(
                    f"\U0001f6ab {len(_fund_st_blocked)} setup(s) blocked by fund limits: {_blk_pairs}"
                )
            all_sections.append(
                ["", "━━━━━━━━━━━━━━━━━━━━━", "⚙️ <b>FUND PROTECTION STATUS</b>"]
                + _fs_st_lines
                + [""]
                + _fs_sz_lines
            )
        except Exception:
            pass

        # TRADE ALERTS
        if yes_trades:
            for r in yes_trades:
                all_sections.append(_trade_block(r))

        # WHY NO MORE SETUPS
        if not yes_trades:
            no_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "💤 <b>WHY NO MORE SETUPS TODAY</b>"]
            if _dd_mode == "halt":
                no_sec += [
                    "🚨 <b>HALT MODE ACTIVE</b> — all new trades suspended.",
                    "Account has reached 10%+ drawdown from peak. No new positions.",
                    "Review open trades and wait for drawdown recovery before resuming.",
                ]
            elif _dd_mode in ("preservation", "defensive", "caution"):
                _tier_req = {"preservation": "A-grade + all 3 TFs aligned",
                             "defensive": "A-grade setups", "caution": "A/B-grade setups"}.get(_dd_mode)
                _tier_icon = {"preservation": "🔴", "defensive": "🟠", "caution": "⚠️"}.get(_dd_mode, "⚠️")
                no_sec.append(f"{_tier_icon} Drawdown protection active — only {_tier_req} accepted. No qualifying setups found today.")
            elif _fund_st_blocked:
                _fsb_reason = _fund_st_blocked[0][1]
                no_sec += [
                    f"\U0001f6ab <b>Fund protection blocked {len(_fund_st_blocked)} setup(s)</b>",
                    f"Reason: {_fsb_reason}",
                    "Qualifying setups were found but fund protection limits prevented opening new trades.",
                    "This is working as intended — protecting the fund from overtrading.",
                ]
            else:
                def _s1_sort_key(rr):
                    return float(rr.get("screen", {}).get("score") or 0)
                combined = [
                    r for r in list(near_misses) + sorted(stage1_filtered, key=_s1_sort_key, reverse=True)
                    if r["pair"].upper() not in _open_pair_set
                    and not any(_is_inverse(r["pair"], op) for op in _open_pair_set)
                ]
                top3     = combined[:3]

                def _plain_rejection(rr) -> str:
                    """Convert rejection reason to plain English for non-traders."""
                    conf_v  = _eff_conf(rr)
                    dirn_v  = (rr.get("parsed", {}).get("direction") or "").upper()
                    parsed  = rr.get("parsed", {})
                    if rr.get("screened_out"):
                        return "Technical indicators not strong enough — the system needs more confirmation before considering this pair"
                    mtf = rr.get("bundle", {}).get("mtf", {})
                    if mtf and not mtf.get("qualifies", True):
                        cnt = mtf.get("agreeing_count", 0)
                        return (f"Nearly there at {conf_v}/10 confidence but the short-term and long-term charts "
                                f"need to agree ({cnt}/3 timeframes currently aligned) — waiting for alignment")
                    try:
                        from src import threshold_manager as _tm_pe
                        rr_min = _tm_pe.get_min_rr()
                        rr_act = float(parsed.get("reward_risk") or 0)
                        if rr_act > 0 and rr_act < rr_min:
                            return (f"Good setup but the potential profit does not justify the risk at current levels "
                                    f"(reward needs to be at least {rr_min:.1f}x the risk)")
                    except Exception:
                        pass
                    scores_pe = {
                        "technical indicators":  parsed.get("technical_score"),
                        "fundamental data":      parsed.get("fundamental_score"),
                        "market sentiment":      parsed.get("sentiment_score"),
                        "institutional positioning": parsed.get("positioning_score"),
                    }
                    below_pe = sorted(
                        [(k, v) for k, v in scores_pe.items() if v is not None and v < 7],
                        key=lambda x: x[1],
                    )
                    if below_pe:
                        k_pe, v_pe = below_pe[0]
                        if v_pe <= 4:
                            return f"Nearly there at {conf_v}/10 but {k_pe} are weak — need to improve before we enter"
                        return f"Nearly there at {conf_v}/10 confidence but {k_pe} need to strengthen before we enter"
                    return f"Reached {conf_v}/10 confidence but the analyst judged the setup is not clean enough to enter right now"

                if top3:
                    no_sec.append(f"The system considered {universe_size} pairs and analysed the top {n_deep} in depth. Here is why the best candidates did not fully qualify:")
                    for rr in top3:
                        reason_plain = _plain_rejection(rr)
                        no_sec.append(f"<b>{rr['pair']}:</b> {reason_plain}")
                    no_sec.append("The system is being selective — waiting for higher quality opportunities")
                elif failed_pairs:
                    no_sec.append(
                        f"⚠️ All {len(failed_pairs)} pairs failed — "
                        "check ANTHROPIC_API_KEY in GitHub secrets"
                    )
                else:
                    no_sec.append("No qualifying setups — staying in cash is a valid position.")
            all_sections.append(no_sec)

        # WATCH LIST with session info
        watch_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "👀 <b>WATCH LIST</b>"]
        if watch_list:
            for rr in watch_list:
                watch_sec.extend(_watch_entry(rr))
                # Missed opportunity: flag pairs with confidence ≥5 but data on fallback
                try:
                    from src import data_quality as _dq_wl
                    _mo_note = _dq_wl.missed_opportunity_note(rr)
                    if _mo_note:
                        watch_sec.append(_mo_note)
                except Exception:
                    pass
        else:
            watch_sec.append("Nothing on watch list today — the system is waiting for cleaner setups")
        all_sections.append(watch_sec)

        # APPROACHING SIGNALS with indicative levels and session time
        if upcoming:
            up_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "📡 <b>APPROACHING SIGNAL</b>"]
            for rr in upcoming:
                up_sec.extend(_approaching_entry(rr))
            all_sections.append(up_sec)

        # RESEARCH TRADES
        _rt_sec = _build_research_section(research_result=research_result)
        if _rt_sec:
            all_sections.append(_rt_sec)

        # FOREX AI FUND — expanded section with full performance stats
        if risk_data and risk_data.get("profile"):
            try:
                from src import risk_manager as _rm_dash
                from src import tracker as _trk_fund
                prof     = risk_data["profile"]
                rmode    = _risk_state.get("risk_mode", "normal")
                dd_mode_dash = _risk_state.get("drawdown_mode", "normal")
                rpct     = _rm_dash.DD_RISK_PCT.get(dd_mode_dash,
                           _rm_dash.MODE_RISK.get(rmode, 1.0))
                exp      = _exposure.get("total_pct", 0.0)
                fund     = prof.get("estimated_balance", _rm_dash.FUND_START)
                fund_pk  = prof.get("peak_balance", fund)
                fund_ret = (fund - _rm_dash.FUND_START) / _rm_dash.FUND_START * 100
                dd_pct   = max(0.0, (fund_pk - fund) / fund_pk * 100) if fund_pk > 0 else 0.0
                real     = config.ACCOUNT_BALANCE
                mode_icons = {
                    "halt":              "🚨",
                    "preservation":      "🔴",
                    "defensive":         "🟠",
                    "caution":           "⚠️",
                    "capital_protection": "⬇️",
                    "streak_protection":  "⬇️",
                    "reduced":            "➡️",
                    "normal":             "🟢",
                    "enhanced":           "⬆️",
                }
                icon = mode_icons.get(dd_mode_dash if dd_mode_dash != "normal" else rmode, "🟢")

                # Load all main fund trades for stats (YES rows only — not analysis sequence IDs)
                _raw_fund_all = list(_trk_fund.load())
                _all_fund_t = [r for r in _raw_fund_all if str(r.get("trade_this", "")).strip().upper() == "YES"]
                # Deduplicate: keep only the latest row per pair+direction (prevents multi-scan double counting)
                _seen_ft: dict = {}
                for _fr in _all_fund_t:
                    _fkey = (_fr.get("pair","").upper(), (_fr.get("direction") or "").upper(), _fr.get("status",""))
                    if _fr.get("status") == "OPEN":
                        _fkey = (_fr.get("pair","").upper(), (_fr.get("direction") or "").upper(), "OPEN")
                    _seen_ft[_fkey] = _fr
                _all_fund_t = list(_seen_ft.values())
                _open_ft    = [r for r in _all_fund_t if r.get("status") == "OPEN"]
                _closed_ft  = [r for r in _all_fund_t
                               if r.get("status") in ("WIN","LOSS","BREAKEVEN","EXPIRED")]
                _wins_ft    = [r for r in _closed_ft
                               if r.get("status") == "WIN" or
                               (r.get("status") in ("BREAKEVEN","EXPIRED") and
                                float(r.get("pips") or 0) > 0)]
                _losses_ft  = [r for r in _closed_ft
                               if r.get("status") == "LOSS" or
                               (r.get("status") in ("BREAKEVEN","EXPIRED") and
                                float(r.get("pips") or 0) < 0)]
                _expired_ft = [r for r in _closed_ft if r.get("status") == "EXPIRED"]
                _decisive_ft = _wins_ft + _losses_ft
                _n_total    = len(_all_fund_t)
                _n_open     = len(_open_ft)
                _n_closed   = len(_closed_ft)
                _n_wins     = len(_wins_ft)
                _n_losses   = len(_losses_ft)
                _n_expired  = len(_expired_ft)
                _wr_pct     = _n_wins / len(_decisive_ft) * 100 if _decisive_ft else 0.0

                # Sanity check — alert if fund trade count is implausibly large
                if _n_total > 20:
                    try:
                        _telegram(
                            f"⚠️ Fund trade count anomaly — showing {_n_total} trades "
                            f"but maximum expected is 20 — check trades.csv for data corruption"
                        )
                    except Exception:
                        pass

                # Best trade by pips
                _best_str = "None yet"
                try:
                    _wp = [(r, float(r.get("pips") or 0)) for r in _closed_ft if r.get("pips")]
                    if _wp:
                        _br, _bp = max(_wp, key=lambda x: x[1])
                        _rm_v = float(_br.get("r_multiple") or 0)
                        _usd  = round(rpct / 100 * fund * _rm_v) if _rm_v else round(abs(_bp))
                        _best_str = (f"{_br.get('pair')} {_br.get('direction')} "
                                     f"+{_bp:.1f} pips (+${_usd})")
                except Exception:
                    pass

                # Avg holding time
                _hold_str = "—"
                try:
                    _days = []
                    for _tr in _closed_ft:
                        _ts = (_tr.get("timestamp") or "")[:10]
                        _ca = (_tr.get("closed_at") or "")[:10]
                        if _ts and _ca:
                            _d = (datetime.strptime(_ca, "%Y-%m-%d") -
                                  datetime.strptime(_ts, "%Y-%m-%d")).days
                            _days.append(max(0, _d))
                    if _days:
                        _avg = sum(_days) / len(_days)
                        _hold_str = f"{_avg:.0f} day{'s' if _avg != 1 else ''}"
                except Exception:
                    pass

                # ML milestone — counts decisive FUND trades + decisive RESEARCH trades combined
                _decisive_rt_ml = 0
                try:
                    from src import research_tracker as _rt_ml_mod
                    _decisive_rt_ml = sum(
                        1 for _r in _rt_ml_mod.load()
                        if _r.get("status") in ("WIN", "LOSS")
                    )
                except Exception:
                    pass
                _decisive_combined_ml = len(_decisive_ft) + _decisive_rt_ml
                _ml_need = max(0, 10 - _decisive_combined_ml)
                _ml_str  = (f"Need {_ml_need} more closed trade{'s' if _ml_need != 1 else ''} "
                            f"for ML activation") if _ml_need > 0 else "ML model active"

                # Prop firm status — weeks since first YES trade (not calendar week)
                _week_n = 1
                try:
                    _ts_list = sorted(
                        r.get("timestamp", "")[:10]
                        for r in _all_fund_t if r.get("timestamp", "")[:10]
                    )
                    if _ts_list:
                        from datetime import date as _date
                        _first_dt = datetime.strptime(_ts_list[0], "%Y-%m-%d").date()
                        _days_since = (now_ak.date() - _first_dt).days
                        _week_n = max(1, (_days_since // 7) + 1)
                        print(f"[DEBUG WEEK] first_trade={_ts_list[0]} today={now_ak.date()} days={_days_since} week={_week_n}", file=sys.stderr)
                    else:
                        print(f"[DEBUG WEEK] _all_fund_t has {len(_all_fund_t)} rows but no timestamps — week defaulting to 1", file=sys.stderr)
                except Exception as _wk_err:
                    print(f"[DEBUG WEEK] exception: {_wk_err} — week defaulting to 1", file=sys.stderr)
                if fund_ret >= 5.0:
                    _prop_str = f"EXCELLENT — {fund_ret:.1f}% return in week {_week_n}"
                elif fund_ret >= 2.0:
                    _prop_str = f"STRONG — {fund_ret:.1f}% return in week {_week_n}"
                elif fund_ret >= 0:
                    _prop_str = f"On track — {fund_ret:.1f}% return in week {_week_n}"
                else:
                    _prop_str = f"Behind — {fund_ret:.1f}% return in week {_week_n}"

                # ML model plain English status (Rule 27)
                _ml_status_plain = (
                    f"learning — need {_ml_need} more decisive trade{'s' if _ml_need != 1 else ''} for activation"
                    if _ml_need > 0 else "active — correctly predicting from training data"
                )
                try:
                    from src import ml_predictor as _mlp_fd
                    _ml_line_raw = _mlp_fd.get_model_status_line()
                    if "accuracy" in _ml_line_raw.lower():
                        import re as _re_ml
                        _acc_m = _re_ml.search(r'(\d+)%', _ml_line_raw)
                        if _acc_m:
                            _ml_status_plain = f"active — correctly predicting {_acc_m.group(1)}% of trades (based on {_n_closed} closed trades)"
                except Exception:
                    pass

                _fund_sec = [
                    "", "━━━━━━━━━━━━━━━━━━━━━",
                    f"📈 <b>FOREX AI FUND</b>",
                    f"Balance: <b>${fund:,.0f} ({fund_ret:+.1f}%)</b> · Peak: ${fund_pk:,.0f}",
                    f"Fund trades: {_n_total} taken · {_n_closed} closed ({_n_wins} WIN · {_n_losses} LOSS) · {_n_open} open",
                    f"Fund capacity: {_n_open}/4 · {max(0, 4 - _n_open)} slot{'s' if max(0, 4 - _n_open) != 1 else ''} available",
                    f"Drawdown: {dd_pct:.1f}% · {icon} {dd_mode_dash.replace('_',' ').title()} · {rpct:.2f}% risk per trade",
                    f"🤖 ML model: {_ml_status_plain}",
                ]
                if _decisive_ft:
                    _early = f" ({len(_decisive_ft)} trade{'s' if len(_decisive_ft)!=1 else ''} — too early to judge)" if len(_decisive_ft) < 10 else ""
                    _fund_sec.append(f"Win rate: {_wr_pct:.0f}%{_early}")
                else:
                    _fund_sec.append("Win rate: — (no decisive outcomes yet)")
                _fund_sec += [
                    f"Best trade: {_best_str}",
                    f"Avg holding time: {_hold_str}",
                    f"PROP FIRM STATUS: {_prop_str}",
                ]
                all_sections.append(_fund_sec)

                # FUND TRADES — every main fund trade with full detail
                if _all_fund_t:
                    _ft_sec = [
                        "", "━━━━━━━━━━━━━━━━━━━━━",
                        "💼 <b>FUND TRADES</b>",
                    ]
                    _status_icons = {"WIN":"✅","LOSS":"❌","OPEN":"⏳","EXPIRED":"⏰",
                                     "BREAKEVEN":"➖","NO_TRADE":"•"}
                    # Load partial profit state for open-trade annotations
                    _pp_state_fund = {}
                    try:
                        from src import partial_profit_checker as _ppc_fund
                        _pp_state_fund = _ppc_fund.load_state()
                    except Exception:
                        pass
                    for _ftr in _all_fund_t:
                        _fs    = _ftr.get("status","?")
                        _fpair = _ftr.get("pair","?")
                        _fdir  = (_ftr.get("direction") or "?").upper()
                        _fid   = _ftr.get("id","?")
                        _fent  = _ftr.get("entry","?")
                        _fex   = _ftr.get("exit_price","")
                        _fpips = _ftr.get("pips","")
                        _frm   = _ftr.get("r_multiple","")
                        _fts   = (_ftr.get("timestamp") or "")[:10]
                        _fca   = (_ftr.get("closed_at") or "")[:10]
                        # Partial profit state for this trade
                        _pp_ts    = _pp_state_fund.get(str(_fid), {})
                        _pp_stage = int(_pp_ts.get("stage", 0))
                        # Icon and status label
                        if _fs == "OPEN" and _pp_stage >= 2:
                            _fi = "💰"
                            _fs_label = "OPEN · 50% Profit Taken"
                        elif _fs == "OPEN" and _pp_stage >= 1:
                            _fi = "🛡️"
                            _fs_label = "OPEN · Breakeven Protected"
                        else:
                            _fi = _status_icons.get(_fs, "•")
                            _fs_label = _fs

                        _ft_sec.append(f"{_fi} <b>#{_fid} {_fpair} {_fdir}</b> — {_fs_label}")
                        _price_line = f"   Entry: {_fent}"
                        if _fex:
                            _price_line += f" → Exit: {_fex}"
                        _price_line += f"  ({_fts}"
                        if _fca and _fca != _fts:
                            _price_line += f" → {_fca}"
                        _price_line += ")"
                        _ft_sec.append(_price_line)
                        # Partial profit detail lines for open trades
                        if _fs == "OPEN":
                            if _pp_stage >= 2:
                                _p_price = _pp_ts.get("partial_close_price")
                                _p_pips  = _pp_ts.get("partial_close_pips")
                                _p_ts    = (_pp_ts.get("partial_close_timestamp") or "")[:10]
                                if _p_price is not None and _p_pips is not None:
                                    _ft_sec.append(
                                        f"   💰 50% closed: {_p_price} "
                                        f"(+{float(_p_pips):.1f} pips) · {_p_ts}"
                                    )
                                _be = _pp_ts.get("breakeven_stop")
                                _ft_sec.append(
                                    f"   50% still running — stop at breakeven"
                                    + (f" ({float(_be):.5f})" if _be else "")
                                )
                            elif _pp_stage >= 1:
                                _be = _pp_ts.get("breakeven_stop")
                                if _be:
                                    _ft_sec.append(
                                        f"   🛡️ Stop at breakeven ({float(_be):.5f}) — no loss possible"
                                    )
                        if _fpips:
                            try:
                                _pp = float(_fpips)
                                _pips_line = f"   {_pp:+.1f} pips"
                                if _frm:
                                    _rm2 = float(_frm)
                                    _usd2 = round(rpct / 100 * fund * _rm2)
                                    _pips_line += f" ({_rm2:+.2f}R · ${_usd2:+})"
                                _ft_sec.append(_pips_line)
                            except (TypeError, ValueError):
                                pass
                    all_sections.append(_ft_sec)
            except Exception:
                pass

        # CREDIT BALANCE
        try:
            primary_bal = credit_data.get("primary_balance")
            backup_bal  = credit_data.get("backup_balance")
            total_bal   = (
                (primary_bal or 0.0)
                + (backup_bal or 0.0 if config.ANTHROPIC_API_KEY_2 else 0.0)
            )
            daily_cost = config.DAILY_COST_USD
            if total_bal > 0 and daily_cost > 0:
                runway = int(total_bal / daily_cost)
                all_sections.append([f"💳 ${total_bal:.2f} combined ({runway} days)"])
            elif primary_bal is not None:
                all_sections.append([f"💳 ${primary_bal:.2f} primary"])
        except Exception:
            pass

        # WEEKLY PERFORMANCE (Monday only)
        weekly = _weekly_performance_section(date)
        if weekly:
            all_sections.append(weekly)

        # SYSTEM HEALTH
        health_issues: list = []
        weak_tech = [r for r in deep_results if r["parsed"].get("technical_score") in (None, 1, 2)]
        if weak_tech and len(weak_tech) >= max(1, len(deep_results) // 2):
            _wt_pairs = ", ".join(r["pair"] for r in weak_tech[:5])
            health_issues.append(
                f"Technical scores suppressed — T:1/2/N/A on {len(weak_tech)} pairs "
                f"({_wt_pairs}) — check DIAG lines in Actions log"
            )
        if td_calls > 700:
            health_issues.append(
                f"⚠️ Twelve Data daily limit approaching — pair count automatically reduced"
            )
        try:
            primary_bal = credit_data.get("primary_balance")
            if primary_bal is not None:
                if primary_bal < 2.0:
                    health_issues.append(
                        f"🚨 PRIMARY CREDITS URGENT — ${primary_bal:.2f} remaining, top up NOW"
                    )
                elif primary_bal < 5.0:
                    health_issues.append(
                        f"Primary credits low — ${primary_bal:.2f} remaining, top up soon"
                    )
        except Exception:
            pass
        if failed_pairs:
            health_issues.append(
                f"{len(failed_pairs)} pair{'s' if len(failed_pairs) > 1 else ''} "
                "failed analysis — check GitHub Actions logs"
            )
        for _azp in _atr_zero_pairs:
            health_issues.append(
                f"⚠️ {_azp} excluded — ATR calculation failed — cannot set safe stop distance"
            )
        if run_duration_min > 20:
            health_issues.append(f"Run took {run_duration_min:.0f} minutes — longer than normal")
        if all_ohlcv_failed:
            health_issues.append(
                "⚠️ Pre-scoring data unavailable — pair ranking based on fundamental factors only — selection quality reduced this scan"
            )
        health_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "⚠️ <b>SYSTEM HEALTH</b>"]
        if threshold_revert_msg:
            health_sec.append(threshold_revert_msg)
        # Data quality scorecard
        try:
            from src import data_quality as _dq_hs
            if _dq_quality:
                health_sec.extend(_dq_hs.build_scorecard(_dq_quality))
        except Exception:
            pass
        # Always show Twelve Data API usage
        _td_remaining_hs = max(0, 800 - td_calls)
        health_sec.append(
            f"📊 Twelve Data API: {td_calls}/800 calls used today — {_td_remaining_hs} remaining"
        )
        for issue in health_issues:
            health_sec.append(f"- {issue}")
        # ML model status line
        try:
            from src import ml_predictor as _mlp_hs
            health_sec.append(_mlp_hs.get_model_status_line())
        except Exception:
            pass
        # Online learner status
        try:
            from src import online_learner as _ol_hs
            health_sec.append(_ol_hs.build_status_line())
        except Exception:
            pass
        # Filter effectiveness alerts
        try:
            from src import filter_effectiveness as _fe_hs
            for _fa in _fe_hs.get_alerts():
                health_sec.append(_fa)
        except Exception:
            pass
        # Monitor status line
        try:
            _mon_log_f = config.DATA_DIR / "monitor_log.json"
            if _mon_log_f.exists():
                _mon_data = json.loads(_mon_log_f.read_text(encoding="utf-8"))
                _mon_ts   = _mon_data.get("timestamp", "")
                _mon_ms   = _mon_data.get("milestones_hit", [])
                _mon_hot  = _mon_data.get("hot_zone_count", 0)
                _mon_skip = _mon_data.get("skipped_reason", "")
                if _mon_ts:
                    try:
                        _mon_dt  = datetime.fromisoformat(_mon_ts.replace("Z", "+00:00"))
                        _mon_ago = datetime.now(__import__("datetime").timezone.utc) - _mon_dt
                        _mon_ago_min = int(_mon_ago.total_seconds() / 60)
                        if _mon_ago_min < 60:
                            _mon_ago_str = f"{_mon_ago_min}m ago"
                        else:
                            _mon_ago_str = f"{_mon_ago_min // 60}h {_mon_ago_min % 60}m ago"
                    except Exception:
                        _mon_ago_str = "recently"
                    if _mon_skip in ("no_open_trades",):
                        health_sec.append(
                            f"✅ Last monitor: {_mon_ago_str} — no open trades — zero API calls used"
                        )
                    elif _mon_ms:
                        _ms_pairs = " · ".join(
                            f"{m['pair']} {m['level']} hit"
                            for m in _mon_ms[:3]
                        )
                        health_sec.append(
                            f"🔔 Last monitor: {_mon_ago_str} — {len(_mon_ms)} milestone(s) — {_ms_pairs}"
                        )
                    elif _mon_hot > 0:
                        health_sec.append(
                            f"⚠️ Last monitor: {_mon_ago_str} — {_mon_hot} trade(s) in hot zone — watching closely"
                        )
                    else:
                        health_sec.append(
                            f"✅ Last monitor: {_mon_ago_str} — all trades within normal range — no milestones hit"
                        )
                    # Warn if monitor ran more than 4 hours ago
                    if _mon_ago_min > 240:
                        health_sec.append(
                            f"⚠️ Last monitor: {_mon_ago_str} — possible GitHub Actions delay — "
                            f"checking now at next scan"
                        )
        except Exception:
            pass
        # Monitor price source reliability (today)
        try:
            from src import monitor as _mon_sr
            for _sr_line in _mon_sr.build_monitor_source_report(days=1):
                health_sec.append(_sr_line)
        except Exception:
            pass
        if cost_lines:
            health_sec.extend(cost_lines)
        # Run statistics
        _rt_open = sum(1 for r in deep_results if r.get("parsed", {}).get("trade_this") != "NO")
        health_sec.append(
            f"Run took {run_duration_min:.0f} minutes · {universe_size} pairs scanned · "
            f"{n_deep} deep analysed · {_rt_open} research trades opened"
        )
        # Currency coverage summary (6am full scan only)
        if scan_mode == "full":
            try:
                _cov: dict = {}
                for _r in deep_results:
                    _pts = _r["pair"].split("/")
                    if len(_pts) == 2:
                        for _c in _pts:
                            _cov[_c] = _cov.get(_c, 0) + 1
                if _cov:
                    _cov_parts = [
                        f"{ccy}({n})"
                        for ccy, n in sorted(_cov.items(), key=lambda x: (-x[1], x[0]))
                    ]
                    _g8 = {"USD", "EUR", "JPY", "GBP", "AUD", "CAD", "CHF", "NZD"}
                    _represented = _g8.intersection(_cov)
                    if _represented == _g8:
                        _cov_verdict = "all major currencies represented"
                    elif len(_represented) >= 6:
                        _missing = sorted(_g8 - _represented)
                        _cov_verdict = f"{', '.join(_missing)} not represented"
                    else:
                        _cov_verdict = f"{len(_represented)}/8 major currencies represented"
                    health_sec.append(
                        f"📊 Pair coverage: {' '.join(_cov_parts)} — {_cov_verdict}"
                    )
            except Exception:
                pass
        all_sections.append(health_sec)

        # RESEARCH THRESHOLD ANALYSIS
        if research_result:
            br   = research_result.get("band_results", {})
            rec  = research_result.get("recommendation", "")
            rsn  = research_result.get("reasoning", "")
            days = research_result.get("days_of_data", 0)
            rec_icon  = {"LOWER_TO_6": "✅", "KEEP_AT_7": "❌", "MARGINAL_EDGE": "⚠️"}.get(rec, "🔬")
            rec_label = {
                "LOWER_TO_6":        "Consider lowering threshold to 6",
                "KEEP_AT_7":         "Keep threshold at 7",
                "MARGINAL_EDGE":     "Marginal edge — collect more data",
                "INSUFFICIENT_DATA": "Insufficient data",
            }.get(rec, rec)
            def _fmt_band(s):
                if not s:
                    return "no data"
                wr  = f"{s['win_rate']*100:.0f}%"
                exp = f"{s['expectancy']:+.2f}R" if s.get("expectancy") is not None else "—R"
                return f"{s['n']} trades · {wr} win · {exp}"
            r_sec = [
                "", "━━━━━━━━━━━━━━━━━━━━━",
                f"🔬 <b>RESEARCH MODE — {days}-day threshold study</b>",
                f"Conf 5: {_fmt_band(br.get('5'))}",
                f"Conf 6: {_fmt_band(br.get('6'))}",
                f"Conf 7: {_fmt_band(br.get('7'))}",
                f"Conf 8-10: {_fmt_band(br.get('8-10'))}",
                f"{rec_icon} <b>{rec_label}</b>",
                f"<i>{rsn}</i>",
            ]
            all_sections.append(r_sec)

        # SESSION GUIDE
        all_sections.append([
            "",
            "━━━━━━━━━━━━━━━━━━━━━",
            "🕐 <b>TODAY'S SESSIONS (Auckland time)</b>",
            "Sydney: 9am–6pm · Tokyo: 12pm–9pm",
            "London: 7pm–4am · New York: 1am–10am",
            "Best window: 1am–4am Auckland (London/New York overlap — highest volume of the week)",
        ])

    # ═══════════════════════════════════════════════════════════════════════════
    # INTRADAY SCANS (9AM / 5PM / 11PM) — unified expanded format
    # ═══════════════════════════════════════════════════════════════════════════
    elif scan_mode in ("morning", "prelondon", "preny"):
        # ── DATA QUALITY ASSESSMENT (intraday)
        _dq_quality_id: dict = {}
        _dq_state_id:   dict = {}
        try:
            from src import data_quality as _dq_id_pre
            _dq_quality_id = _dq_id_pre.assess_scan(deep_results)
            _dq_state_id   = _dq_id_pre.update_state(_dq_quality_id, scan_mode)
            _dq_hv_id = _dq_id_pre.high_volume_alert(_dq_quality_id)
            if _dq_hv_id:
                try:
                    _telegram(_dq_hv_id)
                except Exception:
                    pass
            for _dq_al_id in _dq_id_pre.consecutive_alerts(_dq_state_id):
                try:
                    _telegram(_dq_al_id)
                except Exception:
                    pass
        except Exception:
            pass

        # Header — include session context line for 5pm and 11pm
        if scan_mode == "morning":
            _hdr_id = [f"<b>🤖 FOREX AI — 🌏 9AM MORNING CHECK — {today_short}</b>"]
        elif scan_mode == "prelondon":
            _hdr_id = [
                f"<b>🤖 FOREX AI — 🌆 5PM PRE-LONDON CHECK — {today_short}</b>",
                "London market opens in 2 hours at 7pm Auckland — best time for EUR GBP CHF pairs",
            ]
        else:  # preny
            _hdr_id = [
                f"<b>🤖 FOREX AI — 🌃 11PM PRE-NEW YORK CHECK — {today_short}</b>",
                "New York market opens in 2 hours at 1am Auckland",
                "London/New York overlap 1am–4am Auckland — highest volume of the entire week",
            ]
        all_sections.append(_hdr_id)
        if _dd_banner:
            all_sections.append(["", "━━━━━━━━━━━━━━━━━━━━━", _dd_banner])

        # OPEN TRADES — compact format for intraday (always first)
        _ot_conf_map_id = {r["pair"]: _eff_conf(r) for r in deep_results if r.get("pair")}
        # Confidence crisis: send immediate separate alert before main message
        for _cca_t in _ot_open_trades:
            _cca_p  = _cca_t.get("pair", "")
            _cca_ec = None
            try:
                _cca_ec = int(float(_cca_t.get("confidence") or 0)) or None
            except (TypeError, ValueError):
                pass
            _cca_cc = _ot_conf_map_id.get(_cca_p)
            if _cca_ec and _cca_cc is not None and _cca_cc < 3 and _cca_ec >= 3:
                try:
                    _telegram(
                        f"⚠️ Open trade warning — {_cca_p} confidence has fallen to "
                        f"{_cca_cc}/10 — the original trade rationale may no longer be valid "
                        f"— consider whether to exit early to avoid a larger loss"
                    )
                except Exception:
                    pass
        _ot_compact = _build_open_trades_section(_ot_open_trades, _ot_px_cache, now_ak, compact=True, cur_conf_map=_ot_conf_map_id)
        all_sections.append(_ot_compact)

        # ── SESSION FOCUS (5pm = London, 11pm = New York) ─────────────────────
        if scan_mode == "prelondon":
            _sf_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "🌆 <b>LONDON SESSION FOCUS</b>"]
            _sf_sec.append("London opens in 2 hours — EUR GBP CHF pairs are most active during this session")
            _london_pairs_raw = [r for r in (watch_list + upcoming)
                                 if any(c in r["pair"].upper() for c in ("EUR", "GBP", "CHF"))
                                 and _quality_grades.get(r["pair"], _trade_quality_grade(r)).get("grade") not in ("D", "F")]
            # Dedup inverse pairs within session focus list
            _sf_lseen: set = set()
            _london_pairs = []
            for _r in _london_pairs_raw:
                _p = _r["pair"].upper().replace("/", "")
                _inv = _p[3:] + _p[:3]
                if _p not in _sf_lseen and _inv not in _sf_lseen:
                    _london_pairs.append(_r)
                    _sf_lseen.add(_p)
            if _london_pairs:
                for _lp in _london_pairs[:3]:
                    _sf_sec.append(f"{_lp['pair']} entry window opens at 7pm Auckland — be ready")
            else:
                _sf_sec.append("No EUR or GBP pairs on watch list today — London session may be quiet for us")
            all_sections.append(_sf_sec)
        elif scan_mode == "preny":
            _sf_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "🌃 <b>NEW YORK SESSION FOCUS</b>"]
            _sf_sec.append("New York opens in 2 hours — USD CAD pairs are most active during this session")
            _sf_sec.append("London and New York overlap from 1am to 4am Auckland — highest volume period of the entire week — best time for EUR USD GBP pairs")
            _ny_pairs_raw = [r for r in (watch_list + upcoming)
                             if any(c in r["pair"].upper() for c in ("USD", "CAD"))
                             and _quality_grades.get(r["pair"], _trade_quality_grade(r)).get("grade") not in ("D", "F")]
            # Dedup inverse pairs within session focus list
            _sf_nyseen: set = set()
            _ny_pairs = []
            for _r in _ny_pairs_raw:
                _p = _r["pair"].upper().replace("/", "")
                _inv = _p[3:] + _p[:3]
                if _p not in _sf_nyseen and _inv not in _sf_nyseen:
                    _ny_pairs.append(_r)
                    _sf_nyseen.add(_p)
            if _ny_pairs:
                for _np in _ny_pairs[:3]:
                    _sf_sec.append(f"{_np['pair']} entry window opens at 1am Auckland — be ready when New York opens")
            else:
                _sf_sec.append("No USD or CAD pairs on watch list tonight — New York session may be quiet for us")
            all_sections.append(_sf_sec)

        # ── Market context (full plain-English format) ────────────────────────
        _ctx_id = ["", "━━━━━━━━━━━━━━━━━━━━━", "🌍 <b>MARKET CONTEXT</b>"]
        _vix_id   = ctx.get("vix")
        _env_id   = ctx["risk_env"]
        _vix_str_id = f"VIX {_vix_id:.1f}" if _vix_id else ""
        _ctx_id.append(
            f"Environment: <b>{_env_id}</b>"
            f"{' (' + _vix_str_id + ')' if _vix_str_id else ''}"
        )
        _sc_id     = ctx.get("strongest_ccy")
        _wc_id     = ctx.get("weakest_ccy")
        _scores_id = ctx.get("ccy_scores", {})
        _roff_id = "risk-off" in _env_id.lower()
        _safe_h_id = {"JPY", "CHF", "USD"}
        _risk_off_warn_ccys_id = {"AUD", "NZD", "GBP", "EUR", "NOK", "SEK", "CAD"}
        if _sc_id:
            if _roff_id and _sc_id in _risk_off_warn_ccys_id:
                _sc_rsn_id = "data shows strength but risk-off conditions favour safe havens"
                _ctx_id.append(f"💪 Strongest: <b>{_sc_id}</b> — {_sc_rsn_id} (+{_scores_id.get(_sc_id,0):.0f})")
                _ctx_id.append(f"⚠️ Risk-OFF environment — JPY, CHF, USD typically outperform — verify before trading {_sc_id}")
            elif _roff_id and _sc_id in _safe_h_id:
                _sc_rsn_id = "safe haven demand — expected in risk-off environment"
                _ctx_id.append(f"💪 Strongest: <b>{_sc_id}</b> — {_sc_rsn_id} (+{_scores_id.get(_sc_id,0):.0f})")
            else:
                _sc_rsn_id = "carry + risk-on" if "risk-on" in _env_id else "carry + fundamentals"
                _ctx_id.append(f"💪 Strongest: <b>{_sc_id}</b> — {_sc_rsn_id} (+{_scores_id.get(_sc_id,0):.0f})")
        if _wc_id:
            if _roff_id:
                _wc_rsn_id = "risk-off selling pressure" if _wc_id in _risk_off_warn_ccys_id else "weak fundamentals"
            else:
                _wc_rsn_id = "low rates + risk-on selling" if "risk-on" in _env_id else "weak fundamentals"
            _ctx_id.append(f"📉 Weakest: <b>{_wc_id}</b> — {_wc_rsn_id} ({_scores_id.get(_wc_id,0):.0f})")
        try:
            from src import market_regime as _mr_id
            _rd_id = _mr_id.detect()
            _rl_id = _mr_id.telegram_line(_rd_id)
            if _rl_id:
                _ctx_id.append(_rl_id)
        except Exception:
            pass
        _ps_id = _compute_patience_score(ctx)
        _ctx_id.append(f"📊 <b>Trading conditions: {_ps_id['score']}/10</b> — {_ps_id['description']}")
        all_sections.append(_ctx_id)

        # ECONOMIC CALENDAR — include on 5pm and 11pm scans (shows tonight's events)
        if scan_mode in ("prelondon", "preny"):
            try:
                from src import economic_calendar as _ec_id
                _cal_id = _ec_id.build_calendar_section()
                if _cal_id:
                    all_sections.append(_cal_id)
            except Exception:
                pass

        # ── YES trade alerts (full entry instructions) ────────────────────────
        for r in yes_trades:
            all_sections.append(_trade_block(r))

        # ── CHANGES SINCE LAST SCAN ───────────────────────────────────────────
        _yes_pairs = {r["pair"] for r in yes_trades}
        _prev_scan_label = {"morning": "6am", "prelondon": "9am", "preny": "5pm"}.get(scan_mode, "last")
        _changes_lines = []

        # New alerts (pairs that just reached YES/7+ that weren't before)
        _newly_yes = [r for r in yes_trades if new_alerts and f"{r['pair']}:{(r['parsed'].get('direction') or '').upper()}" in new_alerts]
        for _ny_r in _newly_yes[:3]:
            _ny_d = (_ny_r["parsed"].get("direction") or "").upper()
            _changes_lines.append(f"🆕 {_ny_r['pair']} {_ny_d} reached {_eff_conf(_ny_r)}/10 confidence — new trade alert — full details below")

        # Pairs that improved to 6+ since last scan
        _newly_6plus = [
            r for r in deep_results
            if r["pair"] not in _yes_pairs
            and _eff_conf(r) >= 6
            and _morning_conf.get(r["pair"], 10) < 6
        ]
        for r in _newly_6plus[:3]:
            curr = _eff_conf(r)
            prev = _morning_conf.get(r["pair"], 0)
            dirn = (r["parsed"].get("direction") or "").upper()
            arrow = "📈" if dirn == "BUY" else "📉"
            _changes_lines.append(f"⬆️ {r['pair']} improved from {prev} to {curr}/10 — getting closer to entry")
            _ew_ns = _entry_window_for_pair(r["pair"])
            _eq_ns_e, _ = _entry_quality(r["pair"], now_ak)
            _tref_ns = _time_ref_for_entry(_ew_ns[0], _ew_ns[1], now_ak)
            _start_ns = _fmt_time_exact(_ew_ns[0], _ew_ns[1])
            _changes_lines.append(
                f"  {_eq_ns_e} <b>BE READY TO ENTER:</b> "
                f"{_ew_ns[6]} {_start_ns} Auckland {_tref_ns}"
            )

        # Pairs that dropped since last scan
        _dropped = [
            r for r in deep_results
            if r["pair"] not in _yes_pairs
            and _morning_conf.get(r["pair"]) is not None
            and _morning_conf[r["pair"]] >= 6
            and _eff_conf(r) < 6
        ]
        for r in _dropped[:2]:
            curr = _eff_conf(r)
            prev = _morning_conf[r["pair"]]
            _changes_lines.append(f"⬇️ {r['pair']} dropped from {prev} to {curr}/10 — removed from watch list")

        # Conditions change
        _prev_patience = _morning_conf.get("__patience__")
        _cur_patience  = _compute_patience_score(ctx)["score"]
        if _prev_patience is not None and abs(_cur_patience - _prev_patience) >= 2:
            _changes_lines.append(f"📊 Conditions update: Trading conditions changed to {_cur_patience}/10")

        # Research trades closed since last scan
        _prev_scan_hours = {"morning": 3, "prelondon": 8, "preny": 6}.get(scan_mode, 8)
        try:
            from src import research_tracker as _rtrk_chg
            _rt_chg_rows = _rtrk_chg.load()
            _rt_cutoff = (now_ak - timedelta(hours=_prev_scan_hours)).replace(tzinfo=None)
            for _rt_r in _rt_chg_rows:
                if _rt_r.get("status") not in ("WIN", "LOSS"):
                    continue
                _rt_cat = _rt_r.get("closed_at", "")
                if not _rt_cat:
                    continue
                try:
                    _rt_ts = datetime.strptime(_rt_cat[:16], "%Y-%m-%dT%H:%M")
                    if _rt_ts >= _rt_cutoff:
                        _rt_pips = _rt_r.get("pips", "")
                        try:
                            _rt_pips_f = float(_rt_pips)
                            _rt_pips_str = f"{'+' if _rt_pips_f >= 0 else ''}{_rt_pips_f:.0f}"
                        except (TypeError, ValueError):
                            _rt_pips_str = "0"
                        _changes_lines.append(
                            f"🔬 Research update: {_rt_r['pair']} closed as {_rt_r['status']} {_rt_pips_str} pips"
                        )
                except (ValueError, TypeError):
                    pass
        except Exception:
            pass

        # News reminders for open trades — add to changes section
        try:
            from src import economic_calendar as _ec_chg
            for _ot_row_chg in _ot_open_trades:
                _ot_pair_chg = _ot_row_chg.get("pair", "")
                if not _ot_pair_chg:
                    continue
                _chg_evs = _ec_chg.events_for_pair(_ot_pair_chg, hours=48)
                for _chg_ev in _chg_evs[:2]:
                    _chg_ccy  = _chg_ev["currency"]
                    _chg_en   = _chg_ev["plain_name"]
                    _chg_ak   = _chg_ev["ak_display"]
                    _chg_hrs  = _chg_ev.get("hours_away", 48)
                    _chg_time = "very soon" if _chg_hrs <= 6 else "within 24 hours" if _chg_hrs <= 24 else "within 48 hours"
                    _changes_lines.append(
                        f"⚠️ Reminder: {_chg_ccy} {_chg_en} releases {_chg_time} ({_chg_ak}) "
                        f"— directly affects your open {_ot_pair_chg} trade"
                    )
        except Exception:
            pass

        chg_sec = ["", "━━━━━━━━━━━━━━━━━━━━━",
                   f"🔄 <b>CHANGES SINCE {_prev_scan_label.upper()} SCAN</b>"]
        if _changes_lines:
            chg_sec.extend(_changes_lines)
        else:
            chg_sec.append(f"No significant changes since {_prev_scan_label} — existing opportunities remain valid")
        all_sections.append(chg_sec)

        # ── Watch list (5–6) and approaching signals (3–4) with levels + session ─
        _all_candidates = sorted(
            [r for r in deep_results if r["pair"] not in _yes_pairs and _eff_conf(r) >= 3],
            key=_eff_conf, reverse=True,
        )
        _watch_items_raw    = [r for r in _all_candidates
                               if _eff_conf(r) >= 5
                               and _quality_grades.get(r["pair"], _trade_quality_grade(r)).get("grade") not in ("D", "F")]
        # Deduplicate inverse pairs vs yes_trades and within watch list
        _wi_seen: set = {r["pair"].upper().replace("/", "") for r in yes_trades}
        _watch_items = []
        for _r in _watch_items_raw:
            _p = _r["pair"].upper().replace("/", "")
            _inv = _p[3:] + _p[:3]
            if _p not in _wi_seen and _inv not in _wi_seen:
                _watch_items.append(_r)
                _wi_seen.add(_p)
        _watch_items = _watch_items[:3]
        _approaching_items = [
            r for r in _all_candidates
            if _eff_conf(r) <= 4
            and r["pair"].upper() not in _open_pair_set
            and not any(_is_inverse(r["pair"], op) for op in _open_pair_set)
            and _quality_grades.get(r["pair"], _trade_quality_grade(r)).get("grade") not in ("D", "F")
        ][:2]

        if _watch_items:
            wl_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "👀 <b>WATCH LIST</b>"]
            for rr in _watch_items:
                _rr_pair = rr["pair"]
                _rr_conf = _eff_conf(rr)
                _rr_dirn = (rr["parsed"].get("direction") or "").upper()
                _rr_arrow = "📈" if _rr_dirn == "BUY" else "📉"
                _rr_ew = _entry_window_for_pair(_rr_pair)
                _rr_eq_e, _ = _entry_quality(_rr_pair, now_ak)
                _rr_tref = _time_ref_for_entry(_rr_ew[0], _rr_ew[1], now_ak)
                _rr_start = _fmt_time_exact(_rr_ew[0], _rr_ew[1])
                _rr_qg = _quality_grades.get(_rr_pair, _trade_quality_grade(rr))
                _rr_grade = (_rr_qg or {}).get("grade", "C")
                if _rr_grade in ("D", "F"):
                    continue
                # Session label for London/NY relevance
                _rr_ses_label = ""
                if scan_mode == "prelondon" and any(c in _rr_pair.upper() for c in ("EUR", "GBP", "CHF")):
                    _rr_ses_label = "🌆 London session pair — entry window 7pm–8:30pm Auckland tonight"
                elif scan_mode == "preny":
                    if any(c in _rr_pair.upper() for c in ("USD", "CAD")):
                        _rr_ses_label = "🌃 New York session pair — entry window 1am–2:30am Auckland tonight"
                    elif any(c in _rr_pair.upper() for c in ("EUR", "GBP")):
                        _rr_ses_label = "🌃 London/NY overlap pair — peak entry window 1am–4am Auckland tonight"
                wl_sec.append("")
                if _rr_ses_label:
                    wl_sec.append(_rr_ses_label)
                wl_sec.append(
                    f"{_rr_arrow} <b>{_rr_pair}</b> · Grade {_rr_grade} · {_rr_conf}/10"
                )
                # Indicative levels
                _rr_ind_e, _rr_ind_s, _rr_ind_t, _ = _calc_indicative_levels(
                    _rr_pair, rr["parsed"], rr.get("bundle", {})
                )
                if _rr_ind_e and _rr_ind_s and _rr_ind_t:
                    try:
                        _rr_risk = round(abs(float(_rr_ind_e) - float(_rr_ind_s)) / _pip_size(_rr_pair))
                        _rr_prof = round(abs(float(_rr_ind_t) - float(_rr_ind_e)) / _pip_size(_rr_pair))
                        _is_jpy_rr = "JPY" in _rr_pair.upper()
                        _dec_rr = 3 if _is_jpy_rr else 5
                        wl_sec.append(
                            f"Ideal entry: {float(_rr_ind_e):.{_dec_rr}f} · "
                            f"Stop: {float(_rr_ind_s):.{_dec_rr}f} · "
                            f"Target: {float(_rr_ind_t):.{_dec_rr}f}"
                        )
                        _rr_rr = round(_rr_prof / _rr_risk, 1) if _rr_risk else 0
                        wl_sec.append(f"R:R {_rr_rr}:1 — Stop {_rr_risk}p · Target {_rr_prof}p")
                    except (TypeError, ValueError, ZeroDivisionError):
                        pass
                # Entry window
                _rr_sess_active, _rr_sess_close = _session_status_for_pair(_rr_pair, now_ak)
                if _rr_sess_active:
                    wl_sec.append(f"⏰ {_rr_ew[6]} currently active — closes {_rr_sess_close} Auckland")
                elif _rr_tref not in ("TODAY",):
                    wl_sec.append(f"⏰ Entry window passed for today — next opportunity: {_rr_ew[6]} {_rr_start} Auckland tomorrow")
                else:
                    wl_sec.append(f"⏰ Best entry window: {_rr_ew[6]} {_rr_start} Auckland {_rr_tref}")
            all_sections.append(wl_sec)
        else:
            all_sections.append(["", "━━━━━━━━━━━━━━━━━━━━━", "👀 <b>WATCH LIST</b>",
                                  "Nothing on watch list — system waiting for cleaner setups"])

        if _approaching_items:
            ap_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "📡 <b>APPROACHING SIGNAL</b>"]
            for rr in _approaching_items:
                ap_sec.extend(_approaching_entry(rr))
            all_sections.append(ap_sec)
        else:
            all_sections.append(["", "━━━━━━━━━━━━━━━━━━━━━", "📡 <b>APPROACHING SIGNAL</b>",
                                  "📡 No approaching signals today — all analysed pairs either qualified for watch list or scored below minimum threshold"])

        # RESEARCH TRADES — compact summary for all intraday scans
        _cpt_rt_sec = _build_compact_research_section()
        if _cpt_rt_sec:
            all_sections.append(_cpt_rt_sec)

        # FOREX AI FUND — compact version for intraday scans
        if risk_data and risk_data.get("profile"):
            try:
                from src import risk_manager as _rm_id
                from src import tracker as _trk_id
                _prof_id = risk_data["profile"]
                _rmode_id    = _risk_state.get("risk_mode", "normal")
                _dd_mode_id  = _risk_state.get("drawdown_mode", "normal")
                _rpct_id     = _rm_id.DD_RISK_PCT.get(_dd_mode_id,
                               _rm_id.MODE_RISK.get(_rmode_id, 1.0))
                _fund_id  = _prof_id.get("estimated_balance", _rm_id.FUND_START)
                _pk_id    = _prof_id.get("peak_balance", _fund_id)
                _ret_id   = (_fund_id - _rm_id.FUND_START) / _rm_id.FUND_START * 100
                _dd_id    = max(0.0, (_pk_id - _fund_id) / _pk_id * 100) if _pk_id > 0 else 0.0
                _icon_id  = {"halt":"🚨","preservation":"🔴","defensive":"🟠","caution":"⚠️",
                             "capital_protection":"⬇️","streak_protection":"⬇️",
                             "reduced":"➡️","normal":"🟢","enhanced":"⬆️"}.get(
                             _dd_mode_id if _dd_mode_id != "normal" else _rmode_id, "🟢")
                _all_ft_id = [r for r in _trk_id.load() if str(r.get("trade_this","")).strip().upper() == "YES"]
                _cls_ft_id = [r for r in _all_ft_id if r.get("status") in ("WIN","LOSS","BREAKEVEN","EXPIRED")]
                _opn_ft_id = [r for r in _all_ft_id if r.get("status") == "OPEN"]
                _w_ft_id   = [r for r in _cls_ft_id
                              if r.get("status") == "WIN" or
                              (r.get("status") in ("BREAKEVEN","EXPIRED") and float(r.get("pips") or 0) > 0)]
                _l_ft_id   = [r for r in _cls_ft_id
                              if r.get("status") == "LOSS" or
                              (r.get("status") in ("BREAKEVEN","EXPIRED") and float(r.get("pips") or 0) < 0)]
                _dec_id    = _w_ft_id + _l_ft_id
                _decisive_rt_id = 0
                try:
                    from src import research_tracker as _rt_ml_id
                    _decisive_rt_id = sum(
                        1 for _r in _rt_ml_id.load()
                        if _r.get("status") in ("WIN", "LOSS")
                    )
                except Exception:
                    pass
                _ml_need_id  = max(0, 10 - (len(_dec_id) + _decisive_rt_id))
                _ml_stat_id  = (
                    f"learning — need {_ml_need_id} more decisive trade{'s' if _ml_need_id != 1 else ''} for activation"
                    if _ml_need_id > 0 else "active — correctly predicting from training data"
                )
                try:
                    from src import ml_predictor as _mlp_id
                    _ml_raw_id = _mlp_id.get_model_status_line()
                    if "accuracy" in _ml_raw_id.lower():
                        import re as _re_id
                        _acc_id = _re_id.search(r'(\d+)%', _ml_raw_id)
                        if _acc_id:
                            _ml_stat_id = f"active — correctly predicting {_acc_id.group(1)}% of trades"
                except Exception:
                    pass
                _fund_id_sec = [
                    "", "━━━━━━━━━━━━━━━━━━━━━",
                    "📈 <b>FOREX AI FUND</b>",
                    f"Balance: ${_fund_id:,.0f} ({_ret_id:+.1f}%) · Peak: ${_pk_id:,.0f}",
                    f"Fund trades: {len(_all_ft_id)} taken · {len(_cls_ft_id)} closed ({len(_w_ft_id)} WIN · {len(_l_ft_id)} LOSS) · {len(_opn_ft_id)} open",
                    f"Fund capacity: {len(_opn_ft_id)}/4 · {max(0, 4 - len(_opn_ft_id))} slot{'s' if max(0, 4 - len(_opn_ft_id)) != 1 else ''} available",
                    f"Drawdown: {_dd_id:.1f}% · {_icon_id} {_dd_mode_id.replace('_',' ').title()} · {_rpct_id:.2f}% risk per trade",
                    f"🤖 ML model: {_ml_stat_id}",
                ]
                # Show any open trades inline
                for _ot_id in _opn_ft_id:
                    _pp_id = (_ot_id.get("pips") or "")
                    _pp_str = f" ({float(_pp_id):+.1f}p)" if _pp_id else ""
                    _fund_id_sec.append(
                        f"   ⏳ #{_ot_id.get('id')} {_ot_id.get('pair')} {(_ot_id.get('direction') or '').upper()}{_pp_str}"
                    )
                all_sections.append(_fund_id_sec)
            except Exception:
                pass

        # TONIGHT'S KEY TIMES (5pm and 11pm scans)
        if scan_mode == "prelondon":
            _kt_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "⏰ <b>TONIGHT'S KEY TIMES</b>"]
            _kt_sec.append("7:00pm Auckland — London market opens — EUR GBP CHF pairs become most active")
            # Add any scheduled news events for tonight
            _kt_events_listed = False
            try:
                from src import economic_calendar as _ec_kt
                _kt_cal = _ec_kt.build_calendar_section()
                if _kt_cal and len(_kt_cal) > 2:
                    _kt_sec.append("Check calendar section above for tonight's news events")
                    _kt_events_listed = True
            except Exception:
                pass
            if not _kt_events_listed:
                _kt_sec.append("No major economic events tonight — clean London session ahead")
            all_sections.append(_kt_sec)
        elif scan_mode == "preny":
            _kt_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "⏰ <b>OVERNIGHT KEY TIMES</b>"]
            _kt_sec.append("1:00am Auckland — New York market opens — USD and CAD pairs become most active")
            _kt_sec.append("1:00am–4:00am Auckland — London and New York both open simultaneously — highest volume window of the entire week — best time for EUR USD GBP pairs")
            _kt_events2_listed = False
            try:
                from src import economic_calendar as _ec_kt2
                _kt_cal2 = _ec_kt2.build_calendar_section()
                if _kt_cal2 and len(_kt_cal2) > 2:
                    _kt_sec.append("Check calendar section above for overnight news events")
                    _kt_events2_listed = True
            except Exception:
                pass
            if not _kt_events2_listed:
                _kt_sec.append("No major economic events overnight — clean New York session ahead")
            all_sections.append(_kt_sec)

        # SYSTEM HEALTH — always show on all intraday scans
        _intraday_health = ["", "━━━━━━━━━━━━━━━━━━━━━", "⚠️ <b>SYSTEM HEALTH</b>"]
        try:
            from src import data_quality as _dq_ih
            if _dq_quality_id:
                _intraday_health.extend(_dq_ih.build_scorecard(_dq_quality_id))
        except Exception:
            pass
        if cost_lines:
            _intraday_health.extend(cost_lines)
        else:
            _intraday_health.append("Cost tracking unavailable")
        try:
            from src import dynamic_threshold as _dth_id
            _thr_lines_id = _dth_id.build_telegram_lines(threshold_data)
            if _thr_lines_id:
                _intraday_health.extend([""] + _thr_lines_id)
        except Exception:
            pass
        try:
            from src import online_learner as _ol_id
            _ol_status = _ol_id.build_status_line()
            _intraday_health.append(_ol_status)
        except Exception:
            pass
        try:
            from src import filter_effectiveness as _fe_id
            _fe_alerts = _fe_id.get_alerts()
            for _fa in _fe_alerts:
                _intraday_health.append(_fa)
        except Exception:
            pass
        # Monitor price source reliability (today)
        try:
            from src import monitor as _mon_ih
            for _sr_line in _mon_ih.build_monitor_source_report(days=1):
                _intraday_health.append(_sr_line)
        except Exception:
            pass
        all_sections.append(_intraday_health)

        # Item 9: Daily system health digest — preny (11pm) only
        if scan_mode == "preny":
            _digest = ["", "━━━━━━━━━━━━━━━━━━━━━", "📊 <b>DAILY SYSTEM DIGEST</b>"]
            try:
                # Monitor heartbeat
                if _HEARTBEAT_FILE.exists():
                    _hb_d = json.loads(_HEARTBEAT_FILE.read_text(encoding="utf-8"))
                    _hb_ts_d = _hb_d.get("last_run", "")
                    if _hb_ts_d:
                        _digest.append(f"Last monitor check: {_hb_ts_d[11:16]} UTC")
                else:
                    _digest.append("Monitor heartbeat: no data yet")
            except Exception:
                pass
            try:
                # Monitor log — milestones today
                from src import monitor as _mon_dg
                if _mon_dg._MONITOR_LOG.exists():
                    _ml_dg = json.loads(_mon_dg._MONITOR_LOG.read_text(encoding="utf-8"))
                    _ms_n = len(_ml_dg.get("milestones_hit", []))
                    _yf_n = _ml_dg.get("yf_price_pairs", 0)
                    _digest.append(f"Monitor milestones today: {_ms_n}")
                    _digest.append(f"Prices fetched (last check): {_yf_n} pairs")
            except Exception:
                pass
            try:
                # Monitor source reliability today
                from src import monitor as _mon_preny
                for _ps_line in _mon_preny.build_monitor_source_report(days=1):
                    _digest.append(_ps_line)
            except Exception:
                pass
            try:
                # Scan status
                if _SCAN_STATUS_FILE.exists():
                    _ss_d = json.loads(_SCAN_STATUS_FILE.read_text(encoding="utf-8"))
                    _ss_mode = _ss_d.get("mode", "?")
                    _ss_done = _ss_d.get("completed", False)
                    _ss_fin  = _ss_d.get("finished", "")[:16]
                    if _ss_done:
                        _digest.append(f"Last scan: {_ss_mode} completed at {_ss_fin} UTC")
                    else:
                        _digest.append(f"Last scan: {_ss_mode} — did not complete cleanly")
            except Exception:
                pass
            try:
                # Open trades count
                from src import tracker as _trk_dg, research_tracker as _rt_dg
                _fd_open = sum(1 for r in _trk_dg.load() if r.get("status") == "OPEN")
                _rs_open = sum(1 for r in _rt_dg.load() if r.get("status") == "OPEN")
                _digest.append(f"Open trades: {_fd_open} fund · {_rs_open} research")
            except Exception:
                pass
            if len(_digest) > 3:
                all_sections.append(_digest)

    # ═══════════════════════════════════════════════════════════════════════════
    # SUNDAY GAP SCAN — brief weekend gap detection message
    # ═══════════════════════════════════════════════════════════════════════════
    elif scan_mode == "gap":
        _gap_hdr = [
            f"<b>🤖 FOREX AI — 🌏 SUNDAY GAP SCAN — {_fmt_date_nz(now_ak)}</b>",
            "Checking for significant price gaps since Friday's close",
        ]
        all_sections.append(_gap_hdr)

        # Detect gaps: compare current price to Friday's close from daily series
        _gap_found = []
        _gap_checked = []
        _gap_pairs_list = [
            r["pair"] for r in deep_results[:12]
            if r.get("pair")
        ] or ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD",
               "USDCHF", "EURJPY", "GBPJPY", "EURGBP"]
        for _gp in _gap_pairs_list:
            try:
                import requests as _rq_gap
                _gp_resp = _rq_gap.get(
                    "https://api.twelvedata.com/time_series",
                    params={
                        "symbol":     _gp,
                        "interval":   "1day",
                        "outputsize": 3,
                        "format":     "JSON",
                        "apikey":     config.TWELVE_DATA_KEY,
                    },
                    timeout=12,
                )
                _gp_data = _gp_resp.json().get("values", [])
                if len(_gp_data) < 2:
                    continue
                _gp_cur   = float(_gp_data[0]["close"])
                _gp_fri   = float(_gp_data[1]["close"])
                _gp_pips  = (_gp_cur - _gp_fri) / _pip_size(_gp)
                _gp_thresh = 10 if "JPY" in _gp.upper() else 20
                _gap_checked.append(_gp)
                if abs(_gp_pips) >= _gp_thresh:
                    _gp_sign = "+" if _gp_pips > 0 else ""
                    _gp_dir  = "⬆️" if _gp_pips > 0 else "⬇️"
                    _is_jpy_g = "JPY" in _gp.upper()
                    _dec_g    = 3 if _is_jpy_g else 5
                    _gap_found.append(
                        f"{_gp_dir} <b>{_gp}</b> — Friday: {_gp_fri:.{_dec_g}f} → Now: {_gp_cur:.{_dec_g}f} — Gap: {_gp_sign}{int(round(_gp_pips))} pips"
                    )
            except Exception:
                pass

        if _gap_found:
            _gap_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "⚡ <b>GAPS DETECTED</b>",
                        f"Checked {len(_gap_checked)} pairs — {len(_gap_found)} significant gap{'s' if len(_gap_found) != 1 else ''} found:"]
            _gap_sec.extend(_gap_found)
            _gap_sec.append("")
            _gap_sec.append("Consider waiting for gaps to fill before entering new trades — gaps often retrace in the first hours of Sunday trading")
            all_sections.append(_gap_sec)
        else:
            all_sections.append([
                "", "━━━━━━━━━━━━━━━━━━━━━",
                "✅ <b>NO SIGNIFICANT GAPS</b>",
                f"Checked {len(_gap_checked)} pairs — markets opened normally, no significant price gaps from Friday's close",
                "Normal trading conditions expected — Monday 6am full scan will identify opportunities",
            ])

        if cost_lines:
            all_sections.append(["", "━━━━━━━━━━━━━━━━━━━━━", "⚠️ <b>SYSTEM HEALTH</b>"] + cost_lines)

    # ═══════════════════════════════════════════════════════════════════════════
    # SATURDAY GAP CHECK — lightweight weekend scan to save API credits
    # ═══════════════════════════════════════════════════════════════════════════
    elif scan_mode == "saturday":
        _sat_hdr = [
            f"<b>🤖 FOREX AI — 🌏 SATURDAY CHECK — {_fmt_date_nz(now_ak)}</b>",
            "Lightweight Saturday gap check — saving API credits for Monday full scan",
        ]
        all_sections.append(_sat_hdr)

        # Brief gap check on major pairs only (minimal API usage)
        _sat_gaps = []
        _sat_checked = []
        _sat_pairs = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"]
        for _sp in _sat_pairs:
            try:
                import requests as _rq_sat
                _sp_resp = _rq_sat.get(
                    "https://api.twelvedata.com/time_series",
                    params={
                        "symbol":     _sp,
                        "interval":   "1day",
                        "outputsize": 2,
                        "format":     "JSON",
                        "apikey":     config.TWELVE_DATA_KEY,
                    },
                    timeout=12,
                )
                _sp_data = _sp_resp.json().get("values", [])
                if len(_sp_data) < 2:
                    continue
                _sp_cur  = float(_sp_data[0]["close"])
                _sp_prev = float(_sp_data[1]["close"])
                _sp_pips = (_sp_cur - _sp_prev) / _pip_size(_sp)
                _sat_checked.append(_sp)
                if abs(_sp_pips) >= 15:
                    _sp_sign = "+" if _sp_pips > 0 else ""
                    _sp_dir  = "⬆️" if _sp_pips > 0 else "⬇️"
                    _sat_gaps.append(
                        f"{_sp_dir} <b>{_sp}</b> — Gap: {_sp_sign}{int(round(_sp_pips))} pips"
                    )
            except Exception:
                pass

        if _sat_gaps:
            _sat_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "⚡ <b>SATURDAY GAPS</b>",
                        f"Checked {len(_sat_checked)} major pairs:"]
            _sat_sec.extend(_sat_gaps)
            all_sections.append(_sat_sec)
        else:
            all_sections.append([
                "", "━━━━━━━━━━━━━━━━━━━━━",
                "✅ <b>SATURDAY — NO SIGNIFICANT GAPS</b>",
                f"Checked {len(_sat_checked)} major pairs — markets calm",
                "Full analysis resumes Monday 6am Auckland",
            ])

    # ── FOOTER (all modes) ─────────────────────────────────────────────────────
    all_sections.append([
        "",
        f"<i>{_next_scan_footer(scan_mode, now_ak)}</i>",
    ])

    # Strip lines that contain unavailable / n/a noise
    def _is_ok_line(ln: str) -> bool:
        ll = ln.lower().strip()
        if not ll:
            return True
        bad = (" n/a", ": n/a", "=n/a", "check console")
        return not any(b in ll for b in bad)

    _clean_sections = []
    for _sc_sec in all_sections:
        _sec_has_f = any("Grade F" in str(_sc_ln) for _sc_ln in _sc_sec)
        if _sec_has_f:
            print(f"[CRITICAL] Grade F text found in section starting with: {_sc_sec[:2]} — dropping section", file=sys.stderr)
        else:
            _clean_sections.append(_sc_sec)
    all_sections = _clean_sections

    all_sections = [[ln for ln in sec if _is_ok_line(ln)] for sec in all_sections]

    # FIX 2 Part A: Write trend cache from 6am scan so intraday scans can use it.
    # _monthly_trends and _trend_structures are lazily populated during pair formatting above.
    if scan_mode == "full":
        try:
            from pathlib import Path as _Path_tc_w
            _tc_payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "monthly_trends": _monthly_trends,
                "trend_structures": {
                    k: str(v) if not isinstance(v, (str, dict, list, int, float, bool, type(None))) else v
                    for k, v in _trend_structures.items()
                },
            }
            _Path_tc_w(str(config.DATA_DIR) + "/trend_cache.json").write_text(
                json.dumps(_tc_payload, indent=2), encoding="utf-8"
            )
            _log_line(log, f"Trend cache written — {len(_monthly_trends)} pairs for intraday scans")
        except Exception as _tc_w_exc:
            _log_line(log, f"Trend cache write failed: {_tc_w_exc}")

    # Build short urgent pre-alerts for each YES trade — sent as individual messages first
    _urgent_pre = [
        f"🚨 NEW TRADE ALERT — {_yr['pair']} "
        f"{(_yr.get('parsed') or {}).get('direction', '').upper()} "
        f"— confidence {_eff_conf(_yr):.0f}/10 — full details in next message"
        for _yr in yes_trades
    ]
    _send_in_parts(all_sections, urgent_alerts=_urgent_pre)


# ── Daily run ──────────────────────────────────────────────────────────────────

_RUN_GUARD_FILE       = config.DATA_DIR / "run_guard.json"
_MOVEMENT_ALERTS_FILE = config.DATA_DIR / "movement_alerts.json"
_HEARTBEAT_FILE       = config.DATA_DIR / "heartbeat.json"
_SCAN_STATUS_FILE     = config.DATA_DIR / "scan_status.json"
_COOLDOWN_SECS         = 5400  # 90 minutes — legacy constant kept for reference
_DUPLICATE_GUARD_SECS  = 1200  # 20 minutes — only same-mode duplicates on GitHub Actions are blocked

_ALERTS_FILE         = config.DATA_DIR / "last_alerts.json"
_MORNING_RANKED_FILE = config.DATA_DIR / "morning_ranked.json"

# (label, session-currency filter set — empty = no filter)
_SCAN_MODES: dict = {
    "morning":   ("9am Morning Check",      set()),
    "prelondon": ("5pm Pre-London Check",   set()),
    "preny":     ("11pm Pre-New York Check", set()),
    "full":      ("6am Full Scan",          set()),
    "gap":       ("Sunday Gap Scan",        set()),
    "saturday":  ("Saturday Gap Check",     set()),
    "monitor":   ("Between-Scan Monitor",   set()),
}

# All 4 scans select from the full universe by 8-factor merit score — no mode filtering
_SCAN_TOP_N   = 20   # pairs selected and analysed from the full universe
_TD_CACHE_MAX = 35   # pairs to pre-warm in Twelve Data cache (research sweep uses the extras)

# Sonnet escalation threshold: 6 for 6am (higher quality), 7 for intraday (cheaper)
_SONNET_THRESH = {"full": 6, "morning": 7, "prelondon": 7, "preny": 7}


def _get_scan_mode() -> str:
    """Return scan mode from SCAN_MODE env var (cron-job.org input) or Auckland hour fallback."""
    import os as _os_
    mode = _os_.getenv("SCAN_MODE", "").lower().strip()
    if mode in _SCAN_MODES:
        print(f"[scan] SCAN_MODE={mode!r} from env var (cron-job.org input)", file=sys.stderr)
        return mode
    now = _auckland_now()
    # Saturday morning lightweight gap check (weekday 5 = Saturday, hours 5–8am)
    if now.weekday() == 5 and now.hour in (5, 6, 7, 8):
        print(f"[scan] SCAN_MODE=saturday from Auckland hour fallback (hour={now.hour})", file=sys.stderr)
        return "saturday"
    # Sunday morning gap scan (weekday 6 = Sunday, hours 5–8am)
    if now.weekday() == 6 and now.hour in (5, 6, 7, 8):
        print(f"[scan] SCAN_MODE=gap from Auckland hour fallback (hour={now.hour})", file=sys.stderr)
        return "gap"
    hour = now.hour
    if hour in (5, 6):    # 5am–7am   → 6am full scan
        print(f"[scan] SCAN_MODE=full from Auckland hour fallback (hour={hour})", file=sys.stderr)
        return "full"
    if hour in (8, 9):    # 8am–10am  → 9am morning check
        print(f"[scan] SCAN_MODE=morning from Auckland hour fallback (hour={hour})", file=sys.stderr)
        return "morning"
    if hour in (16, 17):  # 4pm–6pm   → 5pm pre-London check
        print(f"[scan] SCAN_MODE=prelondon from Auckland hour fallback (hour={hour})", file=sys.stderr)
        return "prelondon"
    if hour in (22, 23):  # 10pm–12am → 11pm pre-New York check
        print(f"[scan] SCAN_MODE=preny from Auckland hour fallback (hour={hour})", file=sys.stderr)
        return "preny"
    # Off-hours fallback — log a warning so it is visible in GitHub Actions logs
    print(
        f"[scan] WARNING — SCAN_MODE env var unset and Auckland hour={hour} outside all scan windows "
        f"(5-6=full, 8-9=morning, 16-17=prelondon, 22-23=preny). "
        f"Defaulting to 'full'. Set SCAN_MODE in the workflow env: section.",
        file=sys.stderr,
    )
    return "full"


def _filter_pairs_for_mode(pairs: list, mode: str) -> list:
    """Return only the pairs relevant to the given scan mode."""
    ccys = _SCAN_MODES.get(mode, ("", set()))[1]
    if not ccys:
        return pairs
    return [p for p in pairs if any(c in p.upper() for c in ccys)]


def _alert_fingerprints(results: list) -> set:
    """Build a set of 'PAIR:DIRECTION' strings for all YES trade alerts."""
    return {
        f"{r['pair']}:{(r['parsed'].get('direction') or '').upper()}"
        for r in results
        if r["parsed"].get("trade_this") == "YES"
    }


def _load_last_alerts() -> set:
    try:
        data = json.loads(_ALERTS_FILE.read_text(encoding="utf-8"))
        return set(data.get("alerts", []))
    except Exception:
        return set()


def _save_alerts(alerts: set) -> None:
    try:
        _ALERTS_FILE.write_text(
            json.dumps({"alerts": sorted(alerts)}), encoding="utf-8"
        )
    except Exception:
        pass


def _next_scan_footer(scan_mode: str, now_ak: datetime) -> str:
    """Return a localised 'next scan' line for the Telegram footer."""
    nxt = now_ak + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    nxt_short  = _fmt_date_short_nz(nxt)
    is_weekday = now_ak.weekday() < 5

    if scan_mode == "saturday":
        return "⏰ Next scan Sunday morning — gap check before markets open"
    if scan_mode == "gap":
        return "⏰ Next scan Monday 6am Auckland — full market analysis"
    if scan_mode == "full" and is_weekday:
        return "⏰ Next scan today at 9am Auckland"
    if scan_mode == "morning" and is_weekday:
        return "⏰ Next scan today at 5pm Auckland — Pre-London check"
    if scan_mode == "prelondon" and is_weekday:
        return "⏰ Next scan tonight at 11pm Auckland — Pre-New York check"
    if scan_mode == "preny":
        return "⏰ Next scan tomorrow 6am Auckland — have a good night."
    return f"⏰ Next full scan {nxt_short} at 6am Auckland"


def _detect_opportunity_gaps(
    ranked_all: list,
    prev_prices: dict,
    scan_mode: str,
    log=print,
) -> "tuple[list, dict]":
    """Detect significant price moves and gaps between scans.

    Compares cur_close (OHLCV) against prev scan snapshot for every ranked pair.
    Returns (forced_pairs, opportunity_data).

    forced_pairs    — pairs to inject into analysis regardless of merit rank
    opportunity_data — heatmap, gaps, velocity alerts for the Telegram message
    """
    from src.selector import _pip_size as _sel_pip

    _SIG_MOVE_ATR   = 1.5   # >1.5x ATR → forced into analysis
    _VEL_ATR        = 0.5   # >0.5x ATR counts toward velocity alert
    _VEL_COUNT      = 5     # 5+ pairs of same currency → velocity alert

    forced_pairs: list = []
    movements:    list = []   # {pair, pips, signed_pips, direction, atr_ratio}
    gaps:         list = []   # {pair, pips, direction_str}

    for pair, meta in ranked_all:
        cur_close  = meta.get("cur_close")
        atr5       = meta.get("atr5")
        open_0     = meta.get("open_0")
        prev_close = meta.get("prev_close")
        pip        = _sel_pip(pair)

        # ── Between-scan movement ───────────────────────────────────────────
        prev_scan = prev_prices.get(pair)
        if cur_close is not None and prev_scan is not None and atr5 and atr5 > 0:
            move     = cur_close - prev_scan
            move_abs = abs(move)
            pips     = round(move_abs / pip)
            ratio    = move_abs / atr5
            if pips > 0:
                movements.append({
                    "pair":        pair,
                    "pips":        pips,
                    "signed_pips": round(move / pip),
                    "direction":   "+" if move >= 0 else "-",
                    "atr_ratio":   ratio,
                })
            if ratio >= _SIG_MOVE_ATR:
                if pair not in forced_pairs:
                    forced_pairs.append(pair)
                log(
                    f"[GAP] {pair} moved {pips} pips since last scan "
                    f"({ratio:.1f}x ATR) — forced into analysis pool — "
                    f"significant move detected"
                )

        # ── Gap detection: today's open vs yesterday's close ────────────────
        if open_0 is not None and prev_close is not None:
            gap      = open_0 - prev_close
            gap_abs  = abs(gap)
            gap_pips = round(gap_abs / pip)
            threshold = 10 if pair.upper().endswith("JPY") else 30
            if gap_pips >= threshold:
                gap_dir = "higher" if gap > 0 else "lower"
                gaps.append({"pair": pair, "pips": gap_pips, "direction": gap_dir})
                if pair not in forced_pairs:
                    forced_pairs.append(pair)
                log(
                    f"[GAP] {pair} opened {gap_pips} pips {gap_dir} than last close — "
                    f"this may represent a genuine breakout or a news reaction — "
                    f"forced into analysis"
                )

    # ── Sort movements by absolute pips for heatmap ─────────────────────────
    movements.sort(key=lambda x: x["pips"], reverse=True)

    # ── Velocity alert: 5+ pairs of same currency moving same direction ──────
    ccy_counts: dict = {}   # {ccy: {"+": [pairs], "-": [pairs]}}
    for m in movements:
        if m["atr_ratio"] < _VEL_ATR:
            continue
        parts = m["pair"].split("/")
        if len(parts) != 2:
            continue
        base, quote = parts
        # base strengthens when pair goes up; quote weakens when pair goes up
        base_dir  = m["direction"]
        quote_dir = "-" if m["direction"] == "+" else "+"
        for ccy, ccy_dir in ((base, base_dir), (quote, quote_dir)):
            if ccy not in ccy_counts:
                ccy_counts[ccy] = {"+": [], "-": []}
            ccy_counts[ccy][ccy_dir].append(m["pair"])

    velocity_alerts: list = []
    for ccy, dirs in ccy_counts.items():
        for ccy_dir, pairs_list in dirs.items():
            if len(pairs_list) >= _VEL_COUNT:
                direction_word = "strengthened" if ccy_dir == "+" else "weakened"
                velocity_alerts.append({
                    "currency":  ccy,
                    "direction": direction_word,
                    "count":     len(pairs_list),
                    "pairs":     pairs_list,
                })
                log(
                    f"[GAP] Broad {ccy} move detected — {len(pairs_list)} {ccy} pairs all "
                    f"{direction_word} significantly since last scan — "
                    f"{', '.join(pairs_list[:6])}"
                )

    # ── Log the movement heatmap ─────────────────────────────────────────────
    if movements:
        top3 = movements[:3]
        hm_str = " · ".join(
            f"{m['pair']} {m['direction']}{m['pips']} pips" for m in top3
        )
        log(f"[GAP] Movement heatmap (top movers): {hm_str}")

    opportunity_data: dict = {
        "heatmap":       movements[:5],
        "gaps":          gaps,
        "velocity":      velocity_alerts,
        "forced_count":  len(forced_pairs),
    }
    return forced_pairs, opportunity_data


def _scan_all_pairs_movement(
    universe: list,
    ranked_all: list,
    pairs_today: list,
    scan_mode: str,
    log=print,
) -> "tuple[list, dict]":
    """Scan all eligible pairs for significant movement since the last 6am scan.

    Runs only during 6am full scans. Uses Yahoo Finance batch price fetch (free,
    no rate limits). Flags pairs that moved >1.0x daily ATR and are NOT already
    in today's top-25 analysis batch — adds them to a priority sweep queue.

    Returns (priority_pairs, alert_data).
    """
    if scan_mode != "full":
        return [], {}

    from src.yahoo_finance import batch_fetch_prices as _yf_batch
    from src.selector import _pip_size as _sel_pip_mvt

    _ATR_THRESHOLD = 1.0

    # Build ATR reference map from ranked_all metadata (covers ~30 pairs)
    _atr_map: dict = {}
    for _pair_r, _meta_r in ranked_all:
        _atr_r = _meta_r.get("atr5") or _meta_r.get("atr14")
        if _atr_r and _atr_r > 0:
            _atr_map[_pair_r] = float(_atr_r)

    log(f"[MOVEMENT] Fetching Yahoo Finance prices for {len(universe)} pairs")
    current_prices = _yf_batch(universe, log=log)
    log(f"[MOVEMENT] {len(current_prices)}/{len(universe)} prices fetched")

    # Load previous 6am prices
    _prev_data: dict = {}
    try:
        if _MOVEMENT_ALERTS_FILE.exists():
            _prev_data = json.loads(_MOVEMENT_ALERTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    prev_prices = _prev_data.get("last_scan_prices", {})

    # Weekly stats — reset each Monday
    now_ak_mvt  = _auckland_now()
    today_str   = now_ak_mvt.strftime("%Y-%m-%d")
    monday_str  = (now_ak_mvt - timedelta(days=now_ak_mvt.weekday())).strftime("%Y-%m-%d")
    weekly      = _prev_data.get("weekly_stats", {})
    if weekly.get("week_start", "") != monday_str:
        weekly = {
            "week_start":               monday_str,
            "movement_alerts_detected": 0,
            "already_in_batch":         0,
            "outside_batch_swept":      0,
            "swept_signals_found":      0,
            "swept_became_trades":      0,
        }

    pairs_today_norm = {p.upper().replace("/", "") for p in pairs_today}
    priority_pairs:  list = []
    alert_rows:      list = []

    for pair in universe:
        cur  = current_prices.get(pair)
        prev = prev_prices.get(pair)
        if cur is None or prev is None or cur <= 0:
            continue

        pip      = _sel_pip_mvt(pair)
        move_abs = abs(cur - prev)
        pips     = round(move_abs / pip)
        if pips <= 0:
            continue

        atr = _atr_map.get(pair)
        if not atr or atr <= 0:
            atr = cur * 0.0080 if pair.upper().endswith("JPY") else cur * 0.0008

        ratio = move_abs / atr if atr > 0 else 0.0
        if ratio < _ATR_THRESHOLD:
            continue

        weekly["movement_alerts_detected"] = weekly.get("movement_alerts_detected", 0) + 1

        pair_norm = pair.upper().replace("/", "")
        if pair_norm in pairs_today_norm:
            weekly["already_in_batch"] = weekly.get("already_in_batch", 0) + 1
            log(f"[MOVEMENT] {pair}: {pips} pips ({ratio:.1f}x ATR) — already in batch ✅")
        else:
            weekly["outside_batch_swept"] = weekly.get("outside_batch_swept", 0) + 1
            priority_pairs.append(pair)
            alert_rows.append({"pair": pair, "pips": pips, "atr_ratio": round(ratio, 1)})
            log(f"[MOVEMENT] {pair}: {pips} pips ({ratio:.1f}x ATR) — outside batch — adding to priority sweep")

    # Persist updated prices and stats
    try:
        _MOVEMENT_ALERTS_FILE.write_text(
            json.dumps({
                "last_scan_timestamp": now_ak_mvt.timestamp(),
                "last_scan_date":      today_str,
                "last_scan_prices":    {p: current_prices[p] for p in current_prices},
                "weekly_stats":        weekly,
            }, indent=2),
            encoding="utf-8",
        )
    except Exception as _sav_exc:
        log(f"[MOVEMENT] Save failed: {_sav_exc}")

    return priority_pairs, {
        "full_scan_alerts": alert_rows,
        "weekly":           weekly,
    }


def run() -> int:
    # ── Determine run environment first — affects guard and data-write behaviour ─
    IS_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"

    # ── Auckland startup log — very first line, before all guards and checks ──
    _startup_ak = _auckland_now()
    print(
        f"[startup] Auckland: {_startup_ak.strftime('%A')} "
        f"{_startup_ak.day} {_startup_ak.strftime('%B %Y')} "
        f"{_startup_ak.strftime('%H:%M')} NZT "
        f"(weekday={_startup_ak.weekday()}, hour={_startup_ak.hour})",
        file=sys.stderr, flush=True,
    )
    _run_start = time.time()

    # ── Startup timezone diagnostics ──────────────────────────────────────────
    _now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    print(
        f"[startup] UTC time:      {_now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        file=sys.stderr,
    )
    if IS_GITHUB_ACTIONS:
        print(
            "[startup] GITHUB ACTIONS — scheduled run via cron-job.org",
            file=sys.stderr,
        )
    else:
        print(
            "[startup] LOCAL RUN — data files (run_guard, scan_status) will not be modified",
            file=sys.stderr,
        )

    # ── Scan mode (needed by guard log and stored in guard state) ────────────
    scan_mode = _get_scan_mode()

    # ── Day-of-week safeguard — prevents wrong mode running on wrong day ──────
    # gap mode is Sunday-only; if cron-job.org sends the wrong body on a weekday
    # this catches it before any analysis runs.
    _dow_check = _startup_ak.weekday()  # 0=Mon … 6=Sun
    _valid_weekday_modes = {
        "full":      [0, 1, 2, 3, 4],        # Mon–Fri
        "morning":   [0, 1, 2, 3, 4],        # Mon–Fri
        "prelondon": [0, 1, 2, 3, 4],        # Mon–Fri
        "preny":     [0, 1, 2, 3, 4],        # Mon–Fri
        "gap":       [6],                     # Sunday only
        "saturday":  [5],                     # Saturday only
        "monitor":   [0, 1, 2, 3, 4, 5, 6],  # Every day
    }
    _allowed_days = _valid_weekday_modes.get(scan_mode, list(range(7)))
    if _dow_check not in _allowed_days:
        print(
            f"[scan] WARNING: {scan_mode!r} mode requested but today is "
            f"weekday {_dow_check} ({_startup_ak.strftime('%A')}) — "
            f"valid days: {_allowed_days} — overriding to morning",
            file=sys.stderr,
        )
        scan_mode = "morning"

    print(
        f"[scan] mode={scan_mode} ({_SCAN_MODES[scan_mode][0]})",
        file=sys.stderr,
    )

    # ── Schedule validation — flag timing mismatches immediately ─────────────
    if IS_GITHUB_ACTIONS and scan_mode != "monitor":
        _expected_hours = {
            "full":      [5, 6],
            "morning":   [8, 9, 10],
            "prelondon": [16, 17, 18],
            "preny":     [22, 23, 0],
            "gap":       [5, 6, 7, 8],
            "saturday":  [5, 6, 7, 8],
        }
        _exp = _expected_hours.get(scan_mode, list(range(24)))
        if _startup_ak.hour not in _exp:
            print(
                f"[schedule] WARNING — {scan_mode} scan running at {_startup_ak.hour}:xx Auckland "
                f"(expected {_exp}) — proceeding but check cron-job.org timing",
                file=sys.stderr,
            )
        else:
            print(
                f"[schedule] OK — {scan_mode} scan at correct time ({_startup_ak.hour}:xx Auckland)",
                file=sys.stderr,
            )

    # ── MONITOR MODE: lightweight between-scan check — bypass guard + full pipeline
    if scan_mode == "monitor":
        _mon_log = config.REPORTS_DIR / f"daily_{_startup_ak.strftime('%Y-%m-%d')}.log"
        with _mon_log.open("a", encoding="utf-8") as log:
            try:
                from src import monitor as _mon
                _mon.run(log=lambda m: _log_line(log, m))
            except Exception as _mon_exc:
                import traceback as _mon_tb
                _log_line(log, f"Monitor run failed: {_mon_exc}")
                _log_line(log, _mon_tb.format_exc())
        return 0

    # ── Duplicate-run guard ────────────────────────────────────────────────────
    # Source-aware guard: local runs never block or write — only GitHub Actions
    # runs are guarded.  On GitHub Actions, only block if the SAME scan mode ran
    # within 20 minutes (catches cron-job.org double-fires; never blocks different
    # scan modes or local developer runs from interfering).
    if not IS_GITHUB_ACTIONS:
        print(
            f"[guard] Local run — skipping run_guard — proceeding",
            file=sys.stderr,
        )
    else:
        try:
            _RUN_GUARD_FILE.parent.mkdir(parents=True, exist_ok=True)
            _guard_state: dict = {}
            if _RUN_GUARD_FILE.exists():
                try:
                    _guard_state = json.loads(_RUN_GUARD_FILE.read_text(encoding="utf-8"))
                except Exception:
                    _guard_state = {}

            _last_mode   = _guard_state.get("mode", "")
            _last_ts_str = _guard_state.get("timestamp_utc") or _guard_state.get("timestamp", "")
            print(
                f"[guard] GitHub Actions run — last={_last_mode!r} at {_last_ts_str!r}",
                file=sys.stderr,
            )
            if _last_ts_str and _last_mode == scan_mode:
                try:
                    _last_ts = datetime.fromisoformat(_last_ts_str)
                    _elapsed = (_now_utc - _last_ts).total_seconds()
                    _el_min  = int(_elapsed / 60)
                    if _elapsed < _DUPLICATE_GUARD_SECS:
                        _rem = int((_DUPLICATE_GUARD_SECS - _elapsed) / 60)
                        print(
                            f"[guard] Duplicate detected — {scan_mode} ran {_el_min}m ago "
                            f"(within {_DUPLICATE_GUARD_SECS // 60}m window) — BLOCKED "
                            f"({_rem}m remaining). Exiting.",
                            file=sys.stderr,
                        )
                        return 0
                    print(
                        f"[guard] Same mode {scan_mode} ran {_el_min}m ago — "
                        f"outside {_DUPLICATE_GUARD_SECS // 60}m window — PROCEEDING",
                        file=sys.stderr,
                    )
                except Exception:
                    pass
            elif _last_mode and _last_mode != scan_mode:
                print(
                    f"[guard] Last run was {_last_mode!r} — different mode — PROCEEDING",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[guard] No matching prior run on record — PROCEEDING",
                    file=sys.stderr,
                )

            # Write guard state immediately so any queued concurrent run sees it
            _RUN_GUARD_FILE.write_text(
                json.dumps({
                    "mode":          scan_mode,
                    "timestamp_utc": _now_utc.isoformat(),
                    "source":        "github_actions",
                }, indent=2),
                encoding="utf-8",
            )
        except Exception as _guard_err:
            print(f"[guard] run_guard.json check failed ({_guard_err}) — proceeding.", file=sys.stderr)

    # ── 45-minute run-time guard ───────────────────────────────────────────────
    # cron-job.org treats jobs that run > ~60min as failed and queues a retry.
    # If the scan is still running at 45 min we send a plain-English Telegram
    # warning so recipients know a report is coming, preventing a retry-triggered
    # duplicate scan.
    _MAX_RUN_SECS = 45 * 60
    _timeout_fired = [False]

    def _timeout_handler() -> None:
        _timeout_fired[0] = True
        _telegram(
            "⚠️ Scan is taking longer than expected — still running — full report coming shortly"
        )

    _timeout_timer = threading.Timer(_MAX_RUN_SECS, _timeout_handler)
    _timeout_timer.daemon = True
    _timeout_timer.start()

    # ── Item 2: Monitor heartbeat check ───────────────────────────────────────
    # Alert only if monitor hasn't run in >120 minutes (= 4 missed runs at 30-min cadence).
    try:
        if _HEARTBEAT_FILE.exists():
            _hb = json.loads(_HEARTBEAT_FILE.read_text(encoding="utf-8"))
            _hb_raw = (
                _hb.get("last_monitor_run") or
                _hb.get("last_run") or
                _hb.get("timestamp") or
                _hb.get("updated_at") or
                ""
            )
            if _hb_raw:
                _hb_ts = datetime.fromisoformat(str(_hb_raw).replace("Z", "+00:00"))
                if _hb_ts.tzinfo is not None:
                    _hb_ts = _hb_ts.replace(tzinfo=None)
                _hb_gap = int((_now_utc - _hb_ts).total_seconds() / 60)
                if _hb_gap > 120:
                    _telegram(
                        f"⚠️ MONITOR HEARTBEAT — last between-scan check was {_hb_gap} minutes ago "
                        f"(expected every 30 min) — cron-job.org monitor trigger may be down"
                    )
                    try:
                        if _discord:
                            _discord.send_monitor_gap_alert(float(_hb_gap))
                    except Exception:
                        pass
    except Exception:
        pass

    # ── Item 3: Scan completion marker — mark as in-progress ──────────────────
    if IS_GITHUB_ACTIONS:
        try:
            _SCAN_STATUS_FILE.write_text(
                json.dumps({"completed": False, "started": _now_utc.isoformat(), "mode": scan_mode}),
                encoding="utf-8",
            )
        except Exception:
            pass

    _telegram_test()

    # FIX 4: Discord webhook diagnostic — log which webhooks are configured
    _dsc_webhook_names = [
        "DISCORD_WEBHOOK_FUND",
        "DISCORD_WEBHOOK_RESEARCH",
        "DISCORD_WEBHOOK_MONITOR",
        "DISCORD_WEBHOOK_HEALTH",
        "DISCORD_WEBHOOK_CRITICAL",
    ]
    for _dsc_wh_name in _dsc_webhook_names:
        _dsc_wh_val = os.getenv(_dsc_wh_name)
        _dsc_wh_status = "✅" if _dsc_wh_val else "❌ MISSING"
        print(f"Discord {_dsc_wh_name}: {_dsc_wh_status}", file=sys.stderr)

    missing = config.missing_keys()
    if missing:
        print("ERROR: missing API keys in .env: " + ", ".join(missing), file=sys.stderr)
        return 2

    now_ak   = _auckland_now()
    date     = now_ak.strftime("%Y-%m-%d")
    log_path = config.REPORTS_DIR / f"daily_{date}.log"

    # Startup ping — only for the 6am full scan so Telegram delivery is confirmed before analysis.
    # Guard: if scan_mode resolved to 'full' but the clock is clearly outside the morning window
    # (e.g. 11pm), something triggered this run at the wrong time — skip the ping and warn instead.
    _FULL_SCAN_EXPECTED_HOURS = (5, 8)  # 5am–8am Auckland
    if scan_mode == "full":
        _exp_lo, _exp_hi = _FULL_SCAN_EXPECTED_HOURS
        if _exp_lo <= now_ak.hour <= _exp_hi:
            _telegram(
                "🤖 FOREX AI — 6am scan starting — full market analysis running — "
                "full report in approximately 20 minutes"
            )
        else:
            print(
                f"[startup-ping] WARNING — scan_mode='full' but Auckland hour={now_ak.hour} "
                f"is outside expected window {_exp_lo}h–{_exp_hi}h. "
                f"Skipping startup ping to avoid a confusing '6am full scan' message at {now_ak.hour}h. "
                f"Check cron-job.org trigger time and SCAN_MODE env var.",
                file=sys.stderr,
            )
    elif scan_mode == "gap":
        _telegram(
            "🤖 FOREX AI — Sunday gap scan starting — checking for weekend price gaps — "
            "brief report in a few minutes"
        )
        # Sunday: send weekly learning summary
        try:
            _send_weekly_learning_summary(log_fn=lambda m: print(m, file=sys.stderr))
        except Exception:
            pass

    # Reset per-run state
    try:
        from src import analyst as _anl, technical as _tech
        _anl.reset_key_state()
        _tech.reset_call_count()
    except Exception:
        pass

    with log_path.open("a", encoding="utf-8") as log:

        # 0a. Pre-fetch prices for all open trades before outcome checking.
        #     Uses /time_series?interval=1day&outputsize=2 — more reliable on
        #     the free tier than /price.  The resulting cache is passed to both
        #     outcome checkers so they skip per-trade API calls entirely, which
        #     means ALL open research trades can be priced regardless of whether
        #     they appeared in today's pair selection.
        _open_trade_prices: dict = {}
        try:
            from src import price_fetcher as _pf
            _open_trade_prices = _pf.fetch_prices_for_open_trades(
                log=lambda m: _log_line(log, m),
                scan_mode=scan_mode,
            )
        except Exception as _pf_exc:
            _log_line(
                log,
                f"Price pre-fetch failed ({_pf_exc}) — outcome checkers will use direct API.",
            )

        # 0. Automatic outcome detection
        closed_today = []
        new_patterns = []
        try:
            from src import outcome_checker, outcome_analyst
            closed_today = outcome_checker.check_open_trades(
                log=lambda m: _log_line(log, m),
                price_cache=_open_trade_prices,
            )
            if closed_today:
                new_patterns = outcome_analyst.run_outcome_analysis(
                    closed_today, log=lambda m: _log_line(log, m)
                )
        except Exception as exc:
            _log_line(log, f"Outcome step failed: {exc}")

        # Update fund state: consecutive losses after trade closures
        if closed_today:
            try:
                from src import fund_state as _fs_oc
                _fs_oc_st = _fs_oc.load()
                for _ct in closed_today:
                    _ct_status = (_ct.get("status") or "").upper()
                    if _ct_status in ("WIN", "LOSS", "BREAKEVEN", "FULL_WIN", "PARTIAL_WIN"):
                        _fs_oc_st, _fs_oc_alert = _fs_oc.update_after_close(_fs_oc_st, _ct_status)
                        if _fs_oc_alert:
                            _telegram(_fs_oc_alert)
                _fs_oc.save(_fs_oc_st)
                _log_line(log, (
                    f"Fund state updated: {len(closed_today)} trade(s) closed — "
                    f"consecutive_losses={_fs_oc_st.get('consecutive_losses', 0)}"
                ))
            except Exception as _fs_oc_exc:
                _log_line(log, f"Fund state trade-close update failed: {_fs_oc_exc}")

        try:
            from src import research_outcome_checker
            research_outcome_checker.check_open_research_trades(
                log=lambda m: _log_line(log, m),
                price_cache=_open_trade_prices,
            )
            research_outcome_checker.check_post_close_trades(
                log=lambda m: _log_line(log, m),
                price_cache=_open_trade_prices,
            )
        except Exception as exc:
            _log_line(log, f"Research outcome check failed: {exc}")

        # 1. Learn from prior outcomes
        learning_stats = None
        try:
            learning_stats = learning.update_memory()
            _log_line(
                log,
                f"Learning refreshed: {learning_stats['closed']} closed trades, "
                f"win rate {('%.0f%%' % (learning_stats['win_rate']*100)) if learning_stats['win_rate'] is not None else 'n/a'}, "
                f"{learning_stats['patterns_written']} auto-patterns written.",
            )
        except Exception as exc:
            _log_line(log, f"Learning step failed: {exc}")

        # Retrain ML win-probability model if > 7 days old (weekly cadence)
        try:
            from src import ml_predictor as _mlp
            _ml_meta = _mlp.retrain_if_stale(quiet=True)
            if _ml_meta:
                _hold_pct = round((_ml_meta.get("temporal_holdout_auc") or 0) * 100)
                _n_consec = _ml_meta.get("n_consecutive_reliable", 0)
                _log_line(log, f"ML model retrained: {_ml_meta.get('n_trades',0)} trades, "
                                 f"holdout accuracy {_hold_pct}% on new trades "
                                 f"(reliable retrains: {_n_consec}/3)")
                # Item 7: ML retraining failure alert
                if _hold_pct < 50:
                    try:
                        _telegram(
                            f"⚠️ ML MODEL QUALITY — retrained model accuracy is only {_hold_pct}% "
                            f"(below 50% threshold) — predictions may be unreliable"
                        )
                    except Exception:
                        pass
            else:
                _log_line(log, f"ML model: {_mlp.get_model_status_line()}")
        except Exception as _ml_exc:
            _log_line(log, f"ML model step: {_ml_exc}")

        # Online learner retrain — every full scan, bulk-trains on all closed research trades
        try:
            from src import online_learner as _ol_rt
            _ol_stats = _ol_rt.retrain_all_from_feature_store(
                log=lambda m: _log_line(log, m)
            )
            _ol_wr = _ol_stats.get("win_rate")
            _ol_wr_str = f", recent win rate {_ol_wr:.0%}" if _ol_wr is not None else ""
            _log_line(
                log,
                f"Online learner retrained: {_ol_stats.get('trained', 0)} trades"
                f"{_ol_wr_str} ({_ol_stats.get('skipped', 0)} skipped)"
            )
        except Exception as _ol_exc:
            _log_line(log, f"Online learner retrain failed: {_ol_exc}")

        # Item 6: GitHub token expiry warning — Monday 6am full scan only
        if scan_mode == "full" and now_ak.weekday() == 0:
            try:
                import os as _os_gh
                import requests as _req_gh
                _gh_token = _os_gh.environ.get("GITHUB_TOKEN", "")
                if _gh_token:
                    _gh_resp = _req_gh.get(
                        "https://api.github.com/user",
                        headers={"Authorization": f"Bearer {_gh_token}"},
                        timeout=10,
                    )
                    if _gh_resp.status_code == 200:
                        _log_line(log, "GitHub token valid ✅")
                    elif _gh_resp.status_code == 401:
                        _telegram(
                            "⚠️ GITHUB TOKEN INVALID — GitHub API authentication returned 401 — "
                            "push/checkout operations may fail — check GITHUB_TOKEN secret"
                        )
                    elif _gh_resp.status_code == 403:
                        _log_line(log,
                            "⚠️ GitHub token returns 403 — token exists but may lack "
                            "permissions — monitoring may be affected")
                    else:
                        _log_line(log, f"GitHub token status: HTTP {_gh_resp.status_code}")
                else:
                    _log_line(log, "GitHub token check skipped — GITHUB_TOKEN not in environment")
            except Exception as _gh_exc:
                _log_line(log, f"GitHub token check failed: {_gh_exc}")

        # COT cache diagnostics — log status for all tracked markets, delete stale entries
        try:
            from src import positioning as _pos_cot
            _cot_summary = _pos_cot.log_and_clean_cot_status(
                log_fn=lambda m: _log_line(log, m)
            )
            if _cot_summary.get("deleted_stale", 0):
                _log_line(
                    log,
                    f"[COT] Deleted {_cot_summary['deleted_stale']} stale cache "
                    f"entries — fresh data will be fetched during pair analysis"
                )
        except Exception as _cot_diag_exc:
            _log_line(log, f"[COT] Cache diagnostics failed: {_cot_diag_exc}")

        # Threshold auto-adjust: revert conf 6→7 / R:R 1.3→1.5 if win rate < 45% after 50 trades
        threshold_revert_msg = None
        try:
            from src import threshold_manager as _thresh_check
            threshold_revert_msg = _thresh_check.check_and_adjust(log=lambda m: _log_line(log, m))
        except Exception as exc:
            _log_line(log, f"Threshold check failed: {exc}")

        # Load previous scan price snapshot BEFORE select_pairs() overwrites it
        # Used by both opportunity gap detection and the dynamic boosts in selector
        _prev_scan_prices: dict = {}
        try:
            from src.selector import _load_scan_snapshot as _lss
            _prev_scan_prices = _lss()
            if _prev_scan_prices:
                _log_line(log, f"Previous scan price snapshot loaded: {len(_prev_scan_prices)} pairs")
        except Exception as _psp_exc:
            _log_line(log, f"Prev price snapshot unavailable ({_psp_exc}) — gap detection inactive this scan")

        # Twelve Data health check — single test request before any analysis
        _td_healthy = _check_twelvedata_health(log=lambda m: _log_line(log, m))

        # Log Twelve Data daily call status at scan start
        _td_used_start = 0  # safe default — updated in try block below
        try:
            _au_start = json.loads(_API_USAGE_FILE.read_text(encoding="utf-8")) if _API_USAGE_FILE.exists() else {}
            from datetime import date as _date_start
            _td_used_start = int(_au_start.get("calls", 0)) if _au_start.get("date") == str(_date_start.today()) else 0
            _td_remaining  = max(0, 800 - _td_used_start)
            _log_line(log, f"Twelve Data calls today: {_td_used_start}/800 — {_td_remaining} remaining")
        except Exception:
            pass

        # 2. Smart pair selection
        from src import threshold_manager as _thresh_mgr
        _data_collection_mode = _thresh_mgr.is_data_collection_mode()
        _trade_conf_raw = _thresh_mgr.get_confidence_threshold()
        # FIX 1: In data collection mode, use max(dynamic_threshold, floor=6)
        # so that a ranging market's 7.0 threshold is respected, not overridden.
        _dynamic_threshold_early = float(_trade_conf_raw)
        if _data_collection_mode:
            try:
                from src import dynamic_threshold as _dth_early
                from src import research_tracker as _rt_early_thr
                _early_dth_data = _dth_early.compute(
                    quality_pct=100.0,
                    scan_mode=scan_mode,
                    research_trades=_rt_early_thr.load(),
                )
                _dynamic_threshold_early = _early_dth_data.get("final_threshold", float(_trade_conf_raw))
            except Exception:
                pass
            _data_collection_floor = 6.0
            _trade_conf = int(max(_dynamic_threshold_early, _data_collection_floor))
        else:
            _trade_conf = _trade_conf_raw
        # Cap pair count based on daily API usage
        _at_td_limit = _twelvedata_call_limit_reached()
        if scan_mode != "full" and _td_used_start > 400:
            # Afternoon scan with >400 calls: preserve the daily limit
            _top_n  = 10
            _td_cap = 15
            _log_line(log,
                f"⚠️ Afternoon scan with {_td_used_start} TD calls used — "
                f"reducing pair count to 10 to preserve daily limit")
        elif _at_td_limit:
            _top_n  = 15
            _td_cap = 30
        else:
            _top_n  = 25 if scan_mode == "full" else _SCAN_TOP_N
            # 6am: 25 deep + 15 research sweep = 40; afternoon: 20 deep + 5 sweep = 25
            _td_cap = 40 if scan_mode == "full" else 25
        # Sonnet threshold equals trade threshold so every potential TRADE_THIS YES
        # pair gets entry/stop/target — essential when threshold is 6 (data collection).
        sonnet_thresh = _trade_conf
        if _data_collection_mode:
            _coll_note = (
                f" [DATA COLLECTION MODE — floor 6.0, dynamic {_dynamic_threshold_early:.1f}, effective {_trade_conf}]"
            )
        else:
            _coll_note = ""
        _log_line(log,
            f"Active threshold: {_trade_conf}/10 "
            f"(dynamic: {_dynamic_threshold_early:.1f}"
            f"{' · data collection floor: 6.0' if _data_collection_mode else ''}"
            f" · effective: {_trade_conf})")
        _log_line(log, f"Active thresholds: conf>={_trade_conf}, R:R>={_thresh_mgr.get_min_rr()}{_coll_note}")

        universe_size     = len(selector.UNIVERSE)
        ranked_all        = []
        pairs_today       = []
        _all_ohlcv_failed = False
        try:
            selection     = selector.select_pairs(top_n=_top_n, log=lambda m: _log_line(log, m), scan_mode=scan_mode)
            pairs_today   = selection["selected"]
            ranked_all    = selection["ranked"]
            universe_size = selection["universe_size"]
            _all_ohlcv_failed = selection.get("all_ohlcv_failed", False)
            if _all_ohlcv_failed:
                _log_line(log, "⚠️ All OHLCV snapshots failed — pair ranking based on fundamental factors only — selection quality reduced this scan")
            _log_line(
                log,
                f"Scanning full universe of {universe_size} pairs — selecting top pairs by merit score. "
                f"Top {len(pairs_today)}: {', '.join(pairs_today)}",
            )
        except Exception as exc:
            _log_line(log, f"Smart selection failed ({exc}) — falling back to watchlist.")
            pairs_today = list(config.WATCHLIST)

        # Inject pairs flagged by watchlist movement alerts in monitor.py
        _priority_from_wl: list = []
        _wl_cache_path_pns = config.DATA_DIR / "watchlist_cache.json"
        try:
            if _wl_cache_path_pns.exists():
                _pns_data = json.loads(_wl_cache_path_pns.read_text(encoding="utf-8"))
                _pns_list = _pns_data.get("priority_for_next_scan", [])
                for _pns_p in (_pns_list or []):
                    if _pns_p and _pns_p not in pairs_today:
                        pairs_today.append(_pns_p)
                        _priority_from_wl.append(_pns_p)
                if _priority_from_wl:
                    _log_line(log, f"Watchlist priority: {len(_priority_from_wl)} pair(s) from movement alert injected — {', '.join(_priority_from_wl)}")
                    # Clear the flag — consumed this scan
                    _pns_data["priority_for_next_scan"] = []
                    _wl_cache_path_pns.write_text(json.dumps(_pns_data, indent=2), encoding="utf-8")
        except Exception as _pns_exc:
            _log_line(log, f"Watchlist priority injection failed ({_pns_exc}) — continuing")

        # Filter unsuitable pairs (spread > 33% of ATR — e.g. EUR/HKD, NZD/HKD)
        try:
            from src import trade_costs as _tc_suit
            _suit_excluded = []
            _suit_kept = []
            for _sp in pairs_today:
                _ok, _reason = _tc_suit.is_pair_suitable(_sp)
                if _ok:
                    _suit_kept.append(_sp)
                else:
                    _suit_excluded.append(_sp)
                    _log_line(log, f"[SUIT] Excluding {_sp}: {_reason}")
            if _suit_excluded:
                pairs_today = _suit_kept
                _log_line(log, f"Suitability filter: {len(_suit_excluded)} pair(s) excluded — {', '.join(_suit_excluded)}")
        except Exception as _suit_exc:
            _log_line(log, f"Suitability filter skipped ({_suit_exc})")

        # 2b-i. Opportunity gap detection — compare ranked pairs to last scan prices
        _opportunity_data: dict = {}
        try:
            _forced_gap_pairs, _opportunity_data = _detect_opportunity_gaps(
                ranked_all, _prev_scan_prices, scan_mode,
                log=lambda m: _log_line(log, m),
            )
            if _forced_gap_pairs:
                _log_line(log, f"Gap detection: {len(_forced_gap_pairs)} pair(s) forced into analysis: {', '.join(_forced_gap_pairs)}")
                for _gp in _forced_gap_pairs:
                    if _gp not in pairs_today:
                        pairs_today.append(_gp)
        except Exception as _gap_exc:
            _log_line(log, f"Gap detection failed ({_gap_exc}) — continuing without it")

        # 2b-ii. Full-universe movement scan (6am only) — Yahoo Finance, free, all 130 pairs
        _movement_alert_data: dict = {}
        _movement_priority_set: set = set()
        if scan_mode == "full":
            try:
                _mvt_universe = getattr(selector, "UNIVERSE", list(config.WATCHLIST))
                # Extend universe from ranked_all (covers pairs added via TD universe)
                _ranked_norm = {p for p, _ in ranked_all}
                _mvt_universe = list(_mvt_universe) + [p for p in _ranked_norm if p not in _mvt_universe]
                _mvt_pairs, _movement_alert_data = _scan_all_pairs_movement(
                    _mvt_universe, ranked_all, pairs_today, scan_mode,
                    log=lambda m: _log_line(log, m),
                )
                for _mp in _mvt_pairs:
                    if _mp not in pairs_today:
                        pairs_today.append(_mp)
                    _movement_priority_set.add(_mp)
                if _mvt_pairs:
                    _log_line(log, f"Movement scan: {len(_mvt_pairs)} pair(s) outside batch added to priority sweep: {', '.join(_mvt_pairs)}")
                else:
                    _alerts_det = (_movement_alert_data.get("weekly") or {}).get("movement_alerts_detected", 0)
                    _log_line(log, f"Movement scan: {_alerts_det} alert(s) detected, all already in analysis batch")
            except Exception as _mvt_exc:
                _log_line(log, f"Full-universe movement scan failed ({_mvt_exc}) — continuing")

        # 2b. COST OPTIMISATION — Pre-filter using free data before Twelve Data fetch
        try:
            if ranked_all:
                pre_filtered = _pre_filter_pairs(
                    ranked_all, top_n=_td_cap, log=lambda m: _log_line(log, m)
                )
                for p in pairs_today:
                    if p not in pre_filtered:
                        pre_filtered.insert(0, p)
                pre_filtered = pre_filtered[:_td_cap]
            else:
                pre_filtered = pairs_today
            _log_line(log, f"Pre-filtered pool: {len(pre_filtered)} pairs for Twelve Data (cap={_td_cap})")
        except Exception as exc:
            _log_line(log, f"Pre-filter failed ({exc}) — using selector output.")
            pre_filtered = pairs_today

        # Dedup inverse pairs BEFORE analysis to avoid wasting API credits.
        # Prefer the more standard direction: USD/XXX over XXX/USD, AUD/XXX over XXX/AUD, etc.
        # Standard-direction preference order (base currency preference):
        _STD_BASE_ORDER = ["EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY",
                           "NOK", "SEK", "SGD", "HKD"]

        def _is_more_standard(p1: str, p2: str) -> bool:
            """Return True if p1 is more conventionally standard than p2 (its inverse)."""
            b1 = p1.split("/")[0].upper() if "/" in p1 else p1[:3].upper()
            b2 = p2.split("/")[0].upper() if "/" in p2 else p2[:3].upper()
            r1 = _STD_BASE_ORDER.index(b1) if b1 in _STD_BASE_ORDER else 99
            r2 = _STD_BASE_ORDER.index(b2) if b2 in _STD_BASE_ORDER else 99
            return r1 <= r2

        _pf_seen: set = set()
        _pf_deduped: list = []
        for _pp in pre_filtered:
            _pc = _pp.upper().replace("/", "")
            _pi = _pc[3:] + _pc[:3]
            if _pc in _pf_seen or _pi in _pf_seen:
                # inverse already queued — keep more standard direction
                if _pi in _pf_seen:
                    # our pair IS the inverse — check if we should swap
                    _existing = next((x for x in _pf_deduped if x.upper().replace("/","") == _pi), None)
                    if _existing and not _is_more_standard(_existing, _pp):
                        _pf_deduped = [x for x in _pf_deduped if x.upper().replace("/","") != _pi]
                        _pf_deduped.append(_pp)
                        _pf_seen.discard(_pi)
                        _pf_seen.add(_pc)
                        _log_line(log, f"Analysis dedup: {_existing} replaced by {_pp} — more standard direction")
                    else:
                        _log_line(log, f"Analysis dedup: {_pp} removed — {_existing or _pc} is more standard direction and already queued")
                continue
            _pf_seen.add(_pc)
            _pf_deduped.append(_pp)
        if len(_pf_deduped) < len(pre_filtered):
            _log_line(log, f"Analysis dedup: {len(pre_filtered)} → {len(_pf_deduped)} pairs after removing inverse duplicates")
        pre_filtered = _pf_deduped

        # 3. Batch pre-fetch shared data (FRED, COT, macro) once — all cached 12h
        _shared_fund: dict = {}
        _shared_macro      = None
        try:
            _shared_fund, _shared_macro = _pre_fetch_shared_data(
                pre_filtered, log=lambda m: _log_line(log, m)
            )
        except Exception as exc:
            _log_line(log, f"Shared data pre-fetch failed ({exc}) — each pair will fetch independently.")

        # 4. Pre-fetch Twelve Data candles — 20 pairs for 6am, 15 for afternoon scans
        # Reducing from 25×4TF=100 to 15×4TF=60 calls on afternoon scans saves ~6-7 min.
        _warm_cap = 25 if scan_mode == "full" else 15
        try:
            from src import technical as _tech
            _tech.warm_cache(pre_filtered[:_warm_cap], log=lambda m: _log_line(log, m))
        except Exception as exc:
            _log_line(log, f"Technical pre-fetch failed (analysis will still run): {exc}")

        # Diagnostic: log indicator snapshot — all AUD crosses + one per currency group
        _log_line(log, "[DIAG] Technical indicator snapshot (from cache):")
        _DIAG_PAIRS = [
            # AUD crosses — full set for debugging AUD-specific issues
            "AUD/CHF", "AUD/JPY", "AUD/USD", "AUD/NZD", "AUD/CAD",
            "EUR/AUD", "GBP/AUD",
            # One representative per other currency group
            "EUR/USD", "EUR/GBP", "EUR/JPY", "EUR/CHF",
            "GBP/USD", "GBP/JPY",
            "USD/JPY", "USD/CHF", "USD/CAD",
            "NZD/USD", "NZD/JPY",
        ]
        for _diag_pair in _DIAG_PAIRS:
            try:
                from src import technical as _tech
                _ind = _tech.read_cached_indicators(_diag_pair)
                if _ind:
                    _ts = _ind.get("tech_signal", {})
                    _log_line(log, (
                        f"  {_diag_pair}: RSI={_ind['rsi14']}  "
                        f"MACDh={_ind['macd_hist']}  "
                        f"SMA50={_ind.get('sma50','?')}  "
                        f"BB={(_ind.get('bb_state') or 'inside bands').split('(')[0].strip()[:25]}  "
                        f"→ T_sig={_ts.get('direction','?')} {_ts.get('score','?')}/10"
                    ))
                else:
                    _log_line(log, f"  {_diag_pair}: NOT IN CACHE (candles not fetched this run)")
            except Exception as _de:
                _log_line(log, f"  {_diag_pair}: DIAG ERROR — {_de}")

        _log_line(log, f"=== {scan_mode.upper()} run {date} | universe: {universe_size} pairs | Sonnet threshold: {sonnet_thresh}/10 ===")
        _log_line(log, f"[DIAG] CLAUDE_MODEL={repr(config.CLAUDE_MODEL)}")
        _log_line(log, f"[DIAG] HAIKU_MODEL={repr(config.HAIKU_MODEL)}")
        _log_line(log, f"[DIAG] pairs_today ({len(pairs_today)}): {pairs_today}")

        # 5. Analyse pairs
        filtered_count  = 0
        stage1_filtered: list = []
        failed_pairs:    list = []
        deep_results          = []
        analysed_pairs: set   = set()
        _scan_start_time      = time.time()
        _SCAN_TIMEOUT_MIN     = 90
        _scan_timed_out       = [False]

        def _check_scan_timeout() -> bool:
            elapsed = (time.time() - _scan_start_time) / 60
            if elapsed > _SCAN_TIMEOUT_MIN and not _scan_timed_out[0]:
                _scan_timed_out[0] = True
                _log_line(log,
                    f"⚠️ Scan timeout — {elapsed:.0f} minutes elapsed — "
                    f"skipping remaining pairs and sending partial report")
                try:
                    _telegram(
                        f"⚠️ Scan timeout after {elapsed:.0f} minutes — "
                        f"{len(analysed_pairs)} of {len(pre_filtered)} pairs analysed — "
                        f"COT or other data source caused excessive delays — "
                        f"full analysis continues next scan"
                    )
                except Exception:
                    pass
            return _scan_timed_out[0]

        # Load pair performance map for per-pair threshold overrides
        _pair_perf_map: dict = {}
        try:
            from src import selector as _sel_pm
            _pair_perf_map = _sel_pm.load_pair_performance()
        except Exception:
            pass

        # Pre-load once per scan for ML feature enrichment (avoids repeated file reads)
        _ml_fund_state: dict = {}
        _ml_open_count: int  = 0
        _ml_regime:     str  = "ranging_low_vol"
        try:
            from src import fund_state as _fs_ml
            _ml_fund_state = _fs_ml.load()
        except Exception:
            pass
        try:
            from src import tracker as _trk_ml
            _ml_open_count = sum(1 for _r in _trk_ml.load() if _r.get("status") == "OPEN")
        except Exception:
            pass
        try:
            from src import market_regime as _mr_ml
            _ml_regime = _mr_ml.detect().get("regime", "ranging_low_vol")
        except Exception:
            pass

        def _process_batch(pairs, force_deep=False):
            nonlocal filtered_count
            for pair in pairs:
                if _check_scan_timeout():
                    break
                if pair in analysed_pairs:
                    _log_line(log, f"  {pair}: CACHE HIT (already analysed this run)")
                    continue
                analysed_pairs.add(pair)
                # Per-pair threshold: 70%+ win rate from 10+ trades → lower threshold to 5.5
                _pth_override = None
                _pp = _pair_perf_map.get(pair, {})
                if isinstance(_pp, dict) and _pp.get("wr", 0) >= 0.70 and _pp.get("n", 0) >= 10:
                    _pth_override = 5.5
                    _log_line(log, f"  {pair}: proven edge (wr={_pp['wr']:.0%}, n={_pp['n']}) — threshold lowered to 5.5")
                result = _analyse_pair(
                    pair, log,
                    force_deep=force_deep,
                    shared_fundamental=_shared_fund.get(pair),
                    shared_macro=_shared_macro,
                    sonnet_threshold=sonnet_thresh,
                    pair_threshold_override=_pth_override,
                )
                if result is None:
                    failed_pairs.append(pair)
                    continue
                if result.get("screened_out"):
                    filtered_count += 1
                    s = result["screen"]
                    stage1_filtered.append(result)
                    _log_line(
                        log,
                        f"  {result['pair']}: FILTERED stage-1 "
                        f"(score {s['score']}/5 — {s['reason']})",
                    )
                    continue
                # Send Telegram alert if inverse fund trade was blocked
                if result.get("inverse_blocked"):
                    _bl_pair = result.get("pair", "?")
                    _bl_dir  = (result.get("parsed") or {}).get("direction", "?")
                    try:
                        _telegram(
                            f"⚠️ Trade blocked — <b>{_bl_pair} {_bl_dir}</b> would duplicate existing "
                            f"inverse open trade — same directional bet — new trade not opened.\n"
                            f"{result['inverse_blocked']}"
                        )
                    except Exception:
                        pass
                    _log_line(log, f"  {pair}: INVERSE BLOCKED — {result['inverse_blocked']}")
                # Log and alert on currency concentration warning
                if result.get("concentration_warning"):
                    _log_line(log, f"  {pair}: {result['concentration_warning']}")
                    try:
                        _telegram(
                            f"⚠️ Currency concentration — <b>{pair}</b>\n"
                            f"{result['concentration_warning']}"
                        )
                    except Exception:
                        pass
                pp = result["parsed"]
                skipped = "SKIP-UNCHANGED " if result.get("skipped_unchanged") else ""
                verdict = f"{pp['trade_this']} | conf {pp['confidence']} | {pp['direction']}"
                _log_line(log, f"#{result['id']} {result['pair']}: {skipped}{verdict}")
                deep_results.append(result)
                # Capture ML feature snapshot for future win-probability training
                try:
                    from src import feature_extractor as _fe, feature_store as _fs
                    _mb_fe   = result.get("bundle", {})
                    _mbd_fe  = (_mb_fe.get("technical", {}) or {}).get("daily", {}) or {}
                    _extra_main = {
                        "grade":                   (pp.get("grade") or ""),
                        "cot_momentum":            result.get("_cot_signal", ""),
                        "market_regime":           _ml_regime,
                        "ribbon_state":            (_mbd_fe.get("ribbon") or {}).get("status", ""),
                        "bb_position":             _mbd_fe.get("bb_position", ""),
                        "price_vs_200ma":          _mbd_fe.get("price_vs_200ma", ""),
                        "vix_at_entry":            _fe.extra_vix_from_bundle(_mb_fe),
                        "fund_consecutive_losses": _ml_fund_state.get("consecutive_losses", 0),
                        "fund_drawdown_pct":       _ml_fund_state.get("current_drawdown_pct", 0.0),
                        "fund_sizing_mode":        _ml_fund_state.get("sizing_mode", "normal"),
                        "open_trades_count":       _ml_open_count,
                    }
                    try:
                        _mp_fe = _pair_perf_map.get(result["pair"], {})
                        if isinstance(_mp_fe, dict):
                            _extra_main["pair_win_rate"]  = _mp_fe.get("wr", 0.5)
                            _extra_main["pair_n_samples"] = _mp_fe.get("n", 0)
                    except Exception:
                        pass
                    try:
                        from src import market_regime as _mr_fe
                        _fe_parts = result["pair"].split("/")
                        if len(_fe_parts) == 2:
                            _extra_main["regime_pair_bonus"] = _mr_fe.regime_currency_bonus(
                                _ml_regime, _fe_parts[0].upper(), _fe_parts[1].upper()
                            )
                    except Exception:
                        pass
                    try:
                        _extra_main.update(_fe.compute_entry_context(
                            result["pair"], _mb_fe, pp.get("direction", "BUY")
                        ))
                    except Exception:
                        pass
                    _feat = _fe.extract(result["pair"], pp, _mb_fe, extra_data=_extra_main)
                    _fs.save("main", result["id"], _feat)
                except Exception:
                    pass

        # Haiku analyses all pairs; Sonnet only called for conf >= sonnet_thresh
        # Uses pre_filtered (deduped) not pairs_today to avoid analysing inverse pairs twice
        _log_line(log, f"Analysing {len(pre_filtered)} pairs (Haiku all, Sonnet if conf>={sonnet_thresh}): {', '.join(pre_filtered)}")
        _process_batch(pre_filtered, force_deep=True)

        # Auto-expand if fewer than 3 meaningful results
        meaningful = [r for r in deep_results if _conf(r) >= 5]
        next_idx   = len(pre_filtered)

        while len(meaningful) < 3 and len(deep_results) < 25 and next_idx < len(ranked_all) and not _scan_timed_out[0]:
            extra_pairs = [p for p, _ in ranked_all[next_idx:next_idx + 5]
                           if p in pre_filtered]  # only pre-filtered pairs (already cached)
            if not extra_pairs:
                # Fall back to un-filtered expansion if pre-filtered pool exhausted
                extra_pairs = [p for p, _ in ranked_all[next_idx:next_idx + 5]]
            next_idx += 5
            if extra_pairs:
                _log_line(log, f"Expanding: {len(deep_results)} results, {len(meaningful)} conf>=5. Adding {len(extra_pairs)} more.")
                _process_batch(extra_pairs)
                meaningful = [r for r in deep_results if _conf(r) >= 5]

        # Minimum guarantee
        if len(deep_results) == 0:
            _log_line(log, "WARNING: deep_results=0. Activating minimum guarantee.")
            fallback_pairs = [p for p, _ in ranked_all[:5]] if ranked_all else list(config.WATCHLIST[:5])
            _process_batch(fallback_pairs, force_deep=True)

        # Cross-pair currency consensus adjustment (±1 confidence based on directional agreement)
        try:
            _apply_currency_consensus(deep_results, log)
            # Recompute meaningful after consensus adjustments may have shifted confidences
            meaningful = [r for r in deep_results if _conf(r) >= 5]
        except Exception as _cons_exc:
            _log_line(log, f"[CONSENSUS] Skipped: {_cons_exc}")

        passed = len(deep_results)
        _log_line(
            log,
            f"Analysis complete: universe={universe_size} · "
            f"stage-1 filtered={filtered_count} · deep-analysed={passed} · "
            f"meaningful(conf>=5)={len(meaningful)} · failed={len(failed_pairs)}",
        )

        # Update movement alert stats for priority-swept pairs
        if _movement_priority_set and _MOVEMENT_ALERTS_FILE.exists():
            try:
                _ma_data = json.loads(_MOVEMENT_ALERTS_FILE.read_text(encoding="utf-8"))
                _ma_weekly = _ma_data.get("weekly_stats", {})
                for _ma_r in deep_results:
                    if _ma_r.get("pair") in _movement_priority_set:
                        if (_conf(_ma_r) or 0) >= 5:
                            _ma_weekly["swept_signals_found"] = _ma_weekly.get("swept_signals_found", 0) + 1
                        if (_conf(_ma_r) or 0) >= 7 or ((_ma_r.get("parsed") or {}).get("trade_this") == "YES"):
                            _ma_weekly["swept_became_trades"] = _ma_weekly.get("swept_became_trades", 0) + 1
                _ma_data["weekly_stats"] = _ma_weekly
                _MOVEMENT_ALERTS_FILE.write_text(json.dumps(_ma_data, indent=2), encoding="utf-8")
            except Exception:
                pass

        # Update watchlist weekly stats for injected priority pairs
        if _priority_from_wl:
            try:
                _wl_st_path = config.DATA_DIR / "watchlist_cache.json"
                if _wl_st_path.exists():
                    _wl_st_data = json.loads(_wl_st_path.read_text(encoding="utf-8"))
                    _wl_st = _wl_st_data.get("watchlist_weekly_stats", {})
                    for _wl_r in deep_results:
                        if _wl_r.get("pair") in _priority_from_wl:
                            if (_conf(_wl_r) or 0) >= 7 or ((_wl_r.get("parsed") or {}).get("trade_this") == "YES"):
                                _wl_st["became_trade_alerts"] = _wl_st.get("became_trade_alerts", 0) + 1
                    _wl_st_data["watchlist_weekly_stats"] = _wl_st
                    _wl_st_path.write_text(json.dumps(_wl_st_data, indent=2), encoding="utf-8")
            except Exception:
                pass

        # Save watchlist cache for next scan's dynamic boosters
        # Includes: watchlist pairs (conf 5.0–6.9), near-miss pairs (conf 5.0–5.9),
        # and momentum_counts tracking consecutive scan appearances for Booster 6.
        try:
            import time as _wc_time
            _wl_pairs = [r["pair"] for r in deep_results if 5.0 <= (_conf(r) or 0) <= 6.9]
            _nm_pairs = {r["pair"]: _conf(r) for r in deep_results if 5.0 <= (_conf(r) or 0) <= 5.9}
            _wl_cache_path = config.DATA_DIR / "watchlist_cache.json"

            # Load previous momentum state to build rolling consecutive-scan counts
            _prev_momentum:      dict = {}
            _prev_grace:         dict = {}
            _prev_wl_weekly_sts: dict = {}
            try:
                if _wl_cache_path.exists():
                    _prev_wl_data        = json.loads(_wl_cache_path.read_text(encoding="utf-8"))
                    _prev_momentum       = _prev_wl_data.get("momentum_counts", {})
                    _prev_grace          = _prev_wl_data.get("momentum_grace",  {})
                    _prev_wl_weekly_sts  = _prev_wl_data.get("watchlist_weekly_stats", {})
            except Exception:
                pass

            # Signal direction for near-miss pairs (used by monitor.py for watchlist alerts)
            _nm_dirs = {
                r["pair"]: (r.get("parsed") or {}).get("direction", "")
                for r in deep_results if 5.0 <= (_conf(r) or 0) <= 5.9
            }

            _wl_set         = set(_wl_pairs)
            _new_momentum:  dict = {}
            _new_grace:     dict = {}
            _momentum_gains: list = []

            # Increment count for pairs currently in the watchlist
            for _mp in _wl_pairs:
                _old_cnt = _prev_momentum.get(_mp, 0)
                _new_cnt = _old_cnt + 1
                _new_momentum[_mp] = _new_cnt
                # No grace needed — pair is present this scan
                if _new_cnt >= 2:
                    _momentum_gains.append((_mp, _new_cnt))

            # Apply 1-scan grace period for pairs absent from watchlist this scan
            for _mp, _old_cnt in _prev_momentum.items():
                if _mp in _wl_set:
                    continue   # already handled above
                _old_grace = _prev_grace.get(_mp, 0)
                if _old_grace < 1:
                    # First miss — preserve count for one more scan
                    _new_momentum[_mp] = _old_cnt
                    _new_grace[_mp]    = _old_grace + 1
                # Second consecutive miss → drop from dict (count resets to 0 next scan)

            _wl_cache_path.write_text(
                json.dumps({
                    "timestamp":               _wc_time.time(),
                    "watchlist_pairs":          _wl_pairs,
                    "near_miss":               _nm_pairs,
                    "near_miss_dirs":           _nm_dirs,
                    "momentum_counts":          _new_momentum,
                    "momentum_grace":           _new_grace,
                    "watchlist_weekly_stats":   _prev_wl_weekly_sts,
                    "priority_for_next_scan":   [],
                }, indent=2),
                encoding="utf-8",
            )

            _mom_log = ""
            if _momentum_gains:
                _mom_log = (
                    " — momentum streaks: "
                    + ", ".join(
                        f"{p}×{c}" for p, c in sorted(_momentum_gains, key=lambda x: -x[1])
                    )
                )
                for _mp, _mc in _momentum_gains:
                    _log_line(
                        log,
                        f"[momentum] {_mp} appeared in watch list for {_mc} consecutive "
                        f"scans — momentum accumulation boost +{min(_mc * 3, 12)} merit "
                        f"points will apply next scan.",
                    )
            _log_line(
                log,
                f"Scan state saved for dynamic boosters: "
                f"{len(_wl_pairs)} watchlist pairs, {len(_nm_pairs)} near-miss pairs, "
                f"{len(_new_momentum)} momentum-tracked pairs → watchlist_cache.json{_mom_log}",
            )
        except Exception as _wc_exc:
            _log_line(log, f"Watchlist cache save failed: {_wc_exc}")

        # Save morning-ranked state so intraday scans can pick the closest-to-trigger pairs
        if scan_mode == "full":
            try:
                morning_confs = {
                    r["pair"]: (_conf(r) or 0)
                    for r in deep_results
                }
                _MORNING_RANKED_FILE.write_text(
                    json.dumps(morning_confs), encoding="utf-8"
                )
                _log_line(log, f"Morning ranked state saved ({len(morning_confs)} pairs).")
            except Exception as _mr_exc:
                _log_line(log, f"Morning ranked save failed: {_mr_exc}")

        # Pair statistics for adaptive cascade targets (read once; used by research trade logging)
        _pair_stats_all: dict = {}
        try:
            from src import pair_statistics as _pstat_run
            _pair_stats_all = _pstat_run.load()
        except Exception:
            pass

        # Dynamic confidence threshold — computed once here so research trades
        # can stamp it as an ML feature before _send_telegram_summary is called.
        _threshold_data: dict = {}
        try:
            from src import dynamic_threshold as _dth
            from src import data_quality as _dq_thr
            from src import research_tracker as _rt_thr_load
            _dq_pre = _dq_thr.assess_scan(deep_results)
            _threshold_data = _dth.compute(
                quality_pct=_dq_pre.get("overall_pct", 100.0),
                scan_mode=scan_mode,
                research_trades=_rt_thr_load.load(),
            )
            _dth.append_history(_threshold_data)
            _log_line(
                log,
                f"Dynamic threshold: {_threshold_data['final_threshold']:.1f} "
                f"(regime {_threshold_data.get('regime','?')} base "
                f"{_threshold_data.get('regime_base','?')}, "
                f"wr_adj {_threshold_data.get('win_rate_adjustment', 0):+.1f}, "
                f"dq_adj {_threshold_data.get('data_quality_adjustment', 0):+.1f})",
            )
        except Exception as _thr_exc:
            _log_line(log, f"Dynamic threshold computation failed: {_thr_exc}")

        # FIX 2 Part B: Init _monthly_trends / _trend_structures in run() scope so
        # _log_one_research closure can access them (they live in _send_telegram_summary
        # scope which is different). Load from trend cache written by 6am scan.
        _monthly_trends: dict = {}
        _trend_structures: dict = {}
        try:
            from pathlib import Path as _Path_tc_r
            _tc_r_file = _Path_tc_r(str(config.DATA_DIR) + "/trend_cache.json")
            if _tc_r_file.exists():
                _tc_r_data = json.loads(_tc_r_file.read_text(encoding="utf-8"))
                _tc_r_age_h = (
                    datetime.now(timezone.utc) -
                    datetime.fromisoformat(_tc_r_data["timestamp"])
                ).total_seconds() / 3600
                if _tc_r_age_h < 24:
                    _monthly_trends = _tc_r_data.get("monthly_trends", {})
                    _trend_structures = _tc_r_data.get("trend_structures", {})
                    _log_line(log,
                        f"Trend cache loaded — {_tc_r_age_h:.1f}h old — "
                        f"{len(_monthly_trends)} pairs")
                else:
                    _log_line(log, "Trend cache stale (>24h) — using neutral values")
        except Exception as _tc_r_exc:
            _log_line(log, f"Trend cache load failed: {_tc_r_exc}")

        # Research trading mode: paper-trade conf>=4 pairs (0.01 lots)
        # conf-4 "borderline" setups are tracked separately — they build ML training
        # data and reveal where the real profitable threshold actually sits.
        try:
            from src import research_tracker as _rt
            _rt_today = {
                (r["pair"], (r.get("direction") or "").upper())
                for r in _rt.load()
                if r.get("date") == date
            }
            _rt_logged = 0

            def _log_one_research(r_result, src_override=None, mode_override=None):
                """Log one analysis result as a research trade. Returns True if logged."""
                nonlocal _rt_logged
                _rconf = _conf(r_result)
                if _rconf < 4:
                    return False
                _rp   = r_result["parsed"]
                _rdir = (_rp.get("direction") or "").upper()
                if (r_result["pair"], _rdir) in _rt_today:
                    return False
                _rsrc = src_override or (
                    "sonnet"
                    if all(_rp.get(k) for k in ("entry", "stop_loss", "target"))
                    else "haiku"
                )
                if _rsrc in ("haiku", "haiku_sweep"):
                    _ind_e, _ind_s, _ind_t, _ = _calc_indicative_levels(
                        r_result["pair"], _rp, r_result.get("bundle", {}),
                        research_mode=True,   # tighter 1.0x ATR target for faster ML signal
                    )
                    if _ind_e and _ind_s and _ind_t:
                        _rp = dict(_rp)
                        _rp["entry"]     = _rp.get("entry")     or _ind_e
                        _rp["stop_loss"] = _rp.get("stop_loss") or _ind_s
                        _rp["target"]    = _rp.get("target")    or _ind_t
                        _rsrc = ("indicative" if _rconf >= 5 else "indicative_borderline") \
                                if _rsrc == "haiku" else "haiku_sweep"
                    elif _rconf < 5:
                        _rsrc = "haiku_borderline" if _rsrc == "haiku" else "haiku_sweep_borderline"
                _smode = mode_override or scan_mode

                # ── Collect extended entry-context fields ─────────────────────
                _bundle_rt = r_result.get("bundle", {})
                _tech_rt   = (_bundle_rt.get("technical") or {}) if isinstance(_bundle_rt, dict) else {}
                _daily_rt  = (_tech_rt.get("daily") or {})       if isinstance(_tech_rt, dict) else {}
                _rib_rt    = (_daily_rt.get("ribbon") or {})      if isinstance(_daily_rt, dict) else {}
                _mtf_rt    = (_bundle_rt.get("mtf") or {})        if isinstance(_bundle_rt, dict) else {}
                _fa_rt     = r_result.get("_fundamental_alignment") or {}
                _fa_rt     = _fa_rt if isinstance(_fa_rt, dict) else {}

                # Grade — recompute directly (avoids _quality_grades scope issue)
                _qg_rt     = _trade_quality_grade(r_result)
                _grade_rt  = _qg_rt.get("grade", "")

                # Hard gate: never open research trade when ATR=0 — stop/target cannot be set
                if _qg_rt.get("atr_zero"):
                    _log_line(log,
                        f"[research] {r_result['pair']} skipped — ATR=0 — cannot set safe stop distance")
                    return False

                # Correlation agreement: count other pairs with same base CCY and direction
                _pair_base_rt = r_result["pair"].split("/")[0].upper() if "/" in r_result["pair"] else ""
                _corr_count   = sum(
                    1 for _r2 in deep_results
                    if _r2.get("pair", "") != r_result["pair"]
                    and "/" in _r2.get("pair", "")
                    and _r2["pair"].split("/")[0].upper() == _pair_base_rt
                    and (_r2.get("parsed", {}).get("direction") or "").upper() == _rdir
                )

                # MTF count — defined unconditionally so _extra_rt can always reference it
                _mtf_cnt_rt = int(_mtf_rt.get("agreeing_count", 0) or 0) \
                              if isinstance(_mtf_rt, dict) else 0

                # Market regime — use global macro detector (cached; accurate for ML features)
                try:
                    from src import market_regime as _mr_rt
                    _regime_rt = _mr_rt.detect().get("regime", "ranging_low_vol")
                except Exception:
                    # Fallback: MTF-based heuristic
                    _risk_ccys  = {"AUD", "NZD", "CAD", "EUR", "GBP"}
                    _safe_ccys  = {"JPY", "CHF"}
                    _regime_rt  = "ranging_low_vol"
                    if _mtf_cnt_rt >= 2:
                        if (_rdir == "BUY" and _pair_base_rt in _risk_ccys) or \
                           (_rdir == "SELL" and _pair_base_rt in _safe_ccys):
                            _regime_rt = "trending_risk_on"
                        else:
                            _regime_rt = "trending_risk_off"

                # Auckland time context
                try:
                    _aknow_rt = _auckland_now()
                    _dow_rt   = _aknow_rt.isoweekday()   # 1=Mon … 5=Fri
                    _hour_rt  = _aknow_rt.hour
                except Exception:
                    _dow_rt   = datetime.now(timezone.utc).isoweekday()
                    _hour_rt  = datetime.now(timezone.utc).hour

                _extra_rt = {
                    # Score breakdown
                    "tech_score":             _rp.get("technical_score", ""),
                    "fund_score":             _rp.get("fundamental_score", ""),
                    "sent_score":             _rp.get("sentiment_score", ""),
                    "pos_score":              _rp.get("positioning_score", ""),
                    "macro_score":            _rp.get("macro_score", ""),
                    "mtf_count":              _mtf_cnt_rt,
                    "cot_momentum":           r_result.get("_cot_signal", ""),
                    "fundamental_alignment":  _fa_rt.get("alignment", ""),
                    "fund_aligned_count":     _fa_rt.get("aligned", ""),
                    "grade":                  _grade_rt,
                    "ribbon_state":           _rib_rt.get("status", ""),
                    "divergence_type":        _daily_rt.get("divergence", ""),
                    # Entry quality from technical bundle
                    "rsi_at_entry":           _daily_rt.get("rsi14", ""),
                    "bb_position":            _daily_rt.get("bb_position", ""),
                    "price_vs_200ma":         _daily_rt.get("price_vs_200ma", ""),
                    # Market context
                    "market_regime":          _regime_rt,
                    "patience_score_at_entry": r_result.get("_patience_score", ""),
                    "day_of_week":            _dow_rt,
                    "hour_auckland":          _hour_rt,
                    "corr_agreement_count":   _corr_count,
                    # Dynamic threshold context
                    "threshold_at_entry":              _threshold_data.get("final_threshold", ""),
                    "regime_base_at_entry":            _threshold_data.get("regime_base", ""),
                    "win_rate_adjustment_at_entry":    _threshold_data.get("win_rate_adjustment", ""),
                    "data_quality_adjustment_at_entry": _threshold_data.get("data_quality_adjustment", ""),
                }
                # ─────────────────────────────────────────────────────────────
                # Augment with OHLCV-based entry-context features (no new API calls)
                try:
                    from src import feature_extractor as _fe_ctx
                    _ctx = _fe_ctx.compute_entry_context(
                        r_result["pair"], _bundle_rt,
                        _rp.get("direction", "BUY"),
                    )
                    _extra_rt.update(_ctx)
                except Exception:
                    pass

                # Currency strength at entry — used as ML features
                try:
                    _rt_parts = r_result["pair"].split("/")
                    if len(_rt_parts) == 2 and _ccy_strength:
                        _rt_base_ccy  = _rt_parts[0].upper()
                        _rt_quote_ccy = _rt_parts[1].upper()
                        _extra_rt["base_currency_strength"]  = (
                            _ccy_strength.get(_rt_base_ccy,  {}).get("score", "")
                        )
                        _extra_rt["quote_currency_strength"] = (
                            _ccy_strength.get(_rt_quote_ccy, {}).get("score", "")
                        )
                except Exception:
                    pass

                # Monthly trend at entry — used as ML feature
                try:
                    from src import technical as _tech_rt
                    if r_result["pair"] not in _monthly_trends:
                        _monthly_trends[r_result["pair"]] = _tech_rt.get_monthly_trend(
                            r_result["pair"]
                        )
                    _mt_rt = _monthly_trends.get(r_result["pair"], {})
                    _mt_trend_rt = _mt_rt.get("trend", "NEUTRAL")
                    _rt_dir = _rp.get("direction", "").upper()
                    if _mt_trend_rt == "NEUTRAL":
                        _mt_aligned_rt = 0.5
                    elif (_rt_dir == "BUY" and _mt_trend_rt == "BUY") or \
                         (_rt_dir == "SELL" and _mt_trend_rt == "SELL"):
                        _mt_aligned_rt = 1
                    else:
                        _mt_aligned_rt = 0
                    _extra_rt["monthly_trend"]         = _mt_trend_rt
                    _extra_rt["monthly_trend_aligned"] = _mt_aligned_rt
                except Exception as _e_mt_rt:
                    import sys as _sys_mt
                    print(f"[rt-ml] monthly_trend_aligned failed for {r_result['pair']}: {_e_mt_rt}", file=_sys_mt.stderr)

                # HHHL trend structure at entry — used as ML feature
                try:
                    from src import technical as _tech_ts_rt
                    if r_result["pair"] not in _trend_structures:
                        _trend_structures[r_result["pair"]] = _tech_ts_rt.get_trend_structure(
                            r_result["pair"]
                        )
                    _ts_rt = _trend_structures.get(r_result["pair"], {})
                    _rt_dir_ts = _rp.get("direction", "").upper()
                    if _ts_rt.get("status") != "ok":
                        _hhhl_rt = 0.5  # insufficient data
                    elif _rt_dir_ts == "BUY":
                        _hhhl_rt = 1.0 if _ts_rt.get("buy_valid") else 0.0
                    elif _rt_dir_ts == "SELL":
                        _hhhl_rt = 1.0 if _ts_rt.get("sell_valid") else 0.0
                    else:
                        _hhhl_rt = 0.5
                    _extra_rt["hhhl_aligned"] = _hhhl_rt
                except Exception as _e_hhhl_rt:
                    import sys as _sys_hhhl
                    print(f"[rt-ml] hhhl_aligned failed for {r_result['pair']}: {_e_hhhl_rt}", file=_sys_hhhl.stderr)

                # Kill zone at entry — used as ML feature
                try:
                    from src import kill_zones as _kz_rt
                    _rt_kz_parts = r_result["pair"].split("/")
                    if len(_rt_kz_parts) == 2:
                        _rt_kz = _kz_rt.check(_rt_kz_parts[0], _rt_kz_parts[1])
                        _extra_rt["kill_zone_entry"] = _rt_kz.get("zone_key") or "OUTSIDE"
                except Exception as _e_kz_rt:
                    import sys as _sys_kz
                    print(f"[rt-ml] kill_zone_entry failed for {r_result['pair']}: {_e_kz_rt}", file=_sys_kz.stderr)

                # Market structure break at entry — used as ML feature
                try:
                    from src import technical as _tech_ms_rt
                    if r_result["pair"] not in _mstruct_cache:
                        _mstruct_cache[r_result["pair"]] = _tech_ms_rt.get_market_structure(
                            r_result["pair"]
                        )
                    _ms_rt = _mstruct_cache.get(r_result["pair"], {})
                    _extra_rt["market_structure_break"] = _ms_rt.get("result", "CONTINUATION")
                except Exception:
                    pass

                # RSI divergence at entry — used as ML feature
                try:
                    from src import technical as _tech_div_rt
                    if r_result["pair"] not in _rsi_div_cache:
                        _rsi_div_cache[r_result["pair"]] = _tech_div_rt.get_rsi_divergence(
                            r_result["pair"]
                        )
                    _div_rt  = _rsi_div_cache.get(r_result["pair"], {})
                    _rt_dir_div = _rp.get("direction", "").upper()
                    if _rt_dir_div == "BUY":
                        _extra_rt["rsi_divergence_type"] = _div_rt.get("low_divergence", "NONE")
                    elif _rt_dir_div == "SELL":
                        _extra_rt["rsi_divergence_type"] = _div_rt.get("high_divergence", "NONE")
                    else:
                        _extra_rt["rsi_divergence_type"] = "NONE"
                except Exception:
                    pass

                # Pre-trade quality checklist score — ML feature
                try:
                    _rt_dir_cl   = _rp.get("direction", "").upper()
                    # 1. Monthly trend
                    _cl_rt_mt    = (_extra_rt.get("monthly_trend_aligned", 0.5) == 1)
                    # 2. Weekly trend
                    _cl_rt_wk    = (r_result.get("bundle", {}).get("mtf", {})
                                    .get("signals", {}).get("weekly", "NEUTRAL") == _rt_dir_cl)
                    # 3. HHHL
                    _cl_rt_hhhl  = (_extra_rt.get("hhhl_aligned", 0.5) == 1.0)
                    # 4. Fibonacci
                    _cl_rt_fib   = bool((_qg_rt or {}).get("fib_near", False))
                    # 5. RSI divergence
                    _cl_rt_div   = _extra_rt.get("rsi_divergence_type", "NONE") not in ("NONE", None, "")
                    # 6. Currency strength double-aligned
                    _cl_rt_cs    = False
                    try:
                        from src import currency_strength as _cs_cl_rt
                        _rt_pcl = r_result["pair"].split("/")
                        if len(_rt_pcl) == 2 and _ccy_strength:
                            _cl_rt_cs_boost, _ = _cs_cl_rt.alignment_note(
                                _rt_pcl[0], _rt_pcl[1], _rt_dir_cl, _ccy_strength
                            )
                            _cl_rt_cs = bool(_cl_rt_cs_boost)
                    except Exception:
                        pass
                    # 7. COT institutional aligned
                    _cl_rt_cot_b   = (r_result.get("bundle", {}).get("positioning", {})
                                      .get("base", {})) or {}
                    _cl_rt_cot_dir = str(_cl_rt_cot_b.get("direction", "")).upper()
                    _cl_rt_cot_mom = _cl_rt_cot_b.get("cot_momentum", "STABLE")
                    _cl_rt_cot     = (
                        (_rt_dir_cl == "BUY"  and "LONG"  in _cl_rt_cot_dir and _cl_rt_cot_mom != "REVERSING") or
                        (_rt_dir_cl == "SELL" and "SHORT" in _cl_rt_cot_dir and _cl_rt_cot_mom != "REVERSING")
                    )
                    # 8. No high impact news within 24h
                    _cl_rt_no_news = True
                    try:
                        from src import economic_calendar as _ec_cl_rt
                        _cl_rt_no_news = not bool(
                            _ec_cl_rt.events_for_pair(r_result["pair"], hours=24)
                        )
                    except Exception:
                        pass
                    # 9. R:R >= 2.0
                    _cl_rt_rr = False
                    try:
                        _rt_e_cl = float(_rp.get("entry") or 0)
                        _rt_s_cl = float(_rp.get("stop_loss") or 0)
                        _rt_t_cl = float(_rp.get("target") or 0)
                        if _rt_e_cl and _rt_s_cl and abs(_rt_e_cl - _rt_s_cl) > 0:
                            _cl_rt_rr = (abs(_rt_t_cl - _rt_e_cl) / abs(_rt_e_cl - _rt_s_cl) >= 2.5)
                    except (TypeError, ValueError, ZeroDivisionError):
                        pass
                    # 10. Kill zone
                    _cl_rt_kz    = (_extra_rt.get("kill_zone_entry", "OUTSIDE") != "OUTSIDE")
                    _extra_rt["checklist_score"] = sum(1 for c in [
                        _cl_rt_mt, _cl_rt_wk, _cl_rt_hhhl, _cl_rt_fib, _cl_rt_div,
                        _cl_rt_cs, _cl_rt_cot, _cl_rt_no_news, _cl_rt_rr, _cl_rt_kz,
                    ] if c)
                except Exception as _e_cl_rt:
                    import sys as _sys_cl
                    print(f"[rt-ml] checklist_score failed for {r_result['pair']}: {_e_cl_rt}", file=_sys_cl.stderr)

                # ── Enrich _extra_rt with fund stress, regime bonus, and pair stats ──
                # Fund stress at the moment this trade is being considered
                _extra_rt["fund_consecutive_losses"] = _ml_fund_state.get("consecutive_losses", 0)
                _extra_rt["fund_drawdown_pct"]       = _ml_fund_state.get("current_drawdown_pct", 0.0)
                _extra_rt["fund_sizing_mode"]        = _ml_fund_state.get("sizing_mode", "normal")
                _extra_rt["open_trades_count"]       = _ml_open_count

                # Regime alignment bonus for this specific pair
                try:
                    _rt_bonus_parts = r_result["pair"].split("/")
                    if len(_rt_bonus_parts) == 2:
                        from src import market_regime as _mr_bonus_rt
                        _extra_rt["regime_pair_bonus"] = _mr_bonus_rt.regime_currency_bonus(
                            _regime_rt,
                            _rt_bonus_parts[0].upper(),
                            _rt_bonus_parts[1].upper(),
                        )
                except Exception:
                    pass

                # Per-pair historical win rate and sample count
                try:
                    _rt_pair_perf = _pair_perf_map.get(r_result["pair"], {})
                    if isinstance(_rt_pair_perf, dict):
                        _extra_rt["pair_win_rate"]  = _rt_pair_perf.get("wr", 0.5)
                        _extra_rt["pair_n_samples"] = _rt_pair_perf.get("n", 0)
                except Exception:
                    pass

                # VIX level from macro bundle (fills the always-zero gap)
                try:
                    from src.feature_extractor import extra_vix_from_bundle as _evib_rt
                    _extra_rt["vix_at_entry"] = _evib_rt(_bundle_rt)
                except Exception:
                    pass

                # DXY direction derived from USD currency strength score
                try:
                    if _ccy_strength and "USD" in _ccy_strength:
                        _usd_score = float(_ccy_strength["USD"].get("score", 0) or 0)
                        _extra_rt["dxy_direction"] = (
                            "rising" if _usd_score > 20 else
                            "falling" if _usd_score < -20 else "flat"
                        )
                except Exception:
                    pass

                # Check for inverse pair conflict — BLOCK research trade if inverse already open
                _rt_inv_blocked = False
                try:
                    _inv_warn_rt = _rt.check_inverse_open_research(r_result["pair"], _rdir)
                    if _inv_warn_rt:
                        _log_line(log, f"[research_tracker] BLOCKING — {_inv_warn_rt}")
                        _rt_inv_blocked = True
                except Exception:
                    pass

                if _rt_inv_blocked:
                    return False  # skip log_research_trade() — inverse already open

                # ── Cascade target levels at entry (adaptive when data available) ──
                try:
                    from src import cascade as _casc_rt_log
                    _casc_e_rt   = float((_rp.get("entry") or 0))
                    _casc_s_rt   = float((_rp.get("stop_loss") or 0))
                    _casc_t_rt   = float((_rp.get("target") or 0))
                    _casc_d_rt   = (_rp.get("direction") or "").upper()
                    _casc_atr_rt = float((_daily_rt.get("atr14") or 0))
                    if _casc_e_rt and _casc_s_rt and _casc_t_rt and _casc_d_rt in ("BUY", "SELL"):
                        _rt_pair_up  = r_result["pair"].upper()
                        _rt_pst      = _pair_stats_all.get(_rt_pair_up, {})
                        _rt_adpt     = (_rt_pst.get("adaptive_active")
                                        and _rt_pst.get("n_trades_with_mfe", 0) >= 10)
                        _rt_t1m      = _rt_pst.get("t1_mult") if _rt_adpt else None
                        _rt_t2m      = _rt_pst.get("t2_mult") if _rt_adpt else None
                        _rt_t3m      = _rt_pst.get("t3_mult") if _rt_adpt else None
                        _rt_vol_tier = "NORMAL"
                        _rt_atr_rt   = 1.0
                        if _rt_adpt and _rt_t1m is not None:
                            try:
                                from src import pair_statistics as _pstat_vol_rt
                                _rt_atr_rt = float(_extra_rt.get("atr_percentile_6m") or 1.0)
                                _rt_vol_tier, _rt_t1m, _rt_t2m, _rt_t3m = (
                                    _pstat_vol_rt.apply_volatility_adjustment(
                                        _rt_t1m, _rt_t2m, _rt_t3m, _rt_atr_rt
                                    )
                                )
                            except Exception:
                                pass
                        _ct1_rt, _ct2_rt, _ct3_rt = _casc_rt_log.compute_levels(
                            _casc_e_rt, _casc_s_rt, _casc_t_rt, _casc_d_rt,
                            atr=_casc_atr_rt or None,
                            t1_mult=_rt_t1m, t2_mult=_rt_t2m, t3_mult=_rt_t3m,
                        )
                        if _rt_adpt:
                            _rt_n = _rt_pst.get("n_trades_with_mfe", 0)
                            _log_line(log,
                                f"[research] {r_result['pair']} adaptive targets from "
                                f"{_rt_n} trades: T1 {_rt_t1m:.2f}x · T2 {_rt_t2m:.2f}x"
                                f" · T3 {_rt_t3m:.2f}x ATR ({_rt_vol_tier})")
                        else:
                            _rt_needed = max(0, 10 - _rt_pst.get("n_trades_with_mfe", 0))
                            _log_line(log,
                                f"[research] {r_result['pair']} standard targets "
                                f"(need {_rt_needed} more trades before adaptive activates)")
                        if _ct1_rt is not None:
                            _extra_rt.update({
                                "t1_price":                 _ct1_rt,
                                "t2_price":                 _ct2_rt,
                                "t3_price":                 _ct3_rt,
                                "effective_stop":           _casc_s_rt,
                                "t1_was_adaptive":          "TRUE" if _rt_adpt else "FALSE",
                                "t1_target_atr_multiple":   _rt_t1m if _rt_t1m is not None else 0.4,
                                "t2_target_atr_multiple":   _rt_t2m if _rt_t2m is not None else 0.7,
                                "t3_target_atr_multiple":   _rt_t3m if _rt_t3m is not None else 1.0,
                                "volatility_tier_at_entry": _rt_vol_tier,
                                "atr_percentile_at_entry":  _rt_atr_rt,
                            })
                except Exception:
                    pass

                _rt_id = _rt.log_research_trade(
                    r_result["pair"], _rp, _rsrc, _smode,
                    extra_fields=_extra_rt,
                )
                try:
                    from src import feature_extractor as _fe, feature_store as _fs
                    _feat = _fe.extract(
                        r_result["pair"],
                        r_result["parsed"],
                        _bundle_rt,
                        extra_data=_extra_rt,
                    )
                    _fs.save("research", _rt_id, _feat)
                except Exception:
                    pass
                _rt_today.add((r_result["pair"], _rdir))
                _rt_logged += 1
                return True

            # Pass 1: deep analysis results (conf>=4, includes borderline conf-4)
            for _r in deep_results:
                _log_one_research(_r)
            if _rt_logged:
                _log_line(log, f"Research mode: {_rt_logged} trade(s) from deep analysis (conf>=4).")

            # Pass 2: research sweep — Haiku-only on all pre-filtered pairs not yet analysed.
            # Uses Twelve Data data already cached by warm_cache (no extra API calls).
            # _TD_CACHE_MAX controls how many pairs are pre-warmed; increase it for wider coverage.
            _already_in_deep = {r["pair"] for r in deep_results}
            # 6am: 15 extra pairs for research sweep; afternoon: 5 (cost control)
            _sweep_limit = 15 if scan_mode == "full" else 5
            _sweep_candidates = [p for p in pre_filtered if p not in _already_in_deep][:_sweep_limit]
            if _sweep_candidates:
                _log_line(log,
                    f"Research sweep: Haiku-only scan of {len(_sweep_candidates)} additional "
                    f"pre-warmed pairs (limit={_sweep_limit} for {scan_mode})...")
                _sweep_new = 0
                for _sp in _sweep_candidates:
                    try:
                        _sr = _analyse_pair(
                            _sp, log,
                            force_deep=False,
                            shared_fundamental=_shared_fund.get(_sp),
                            shared_macro=_shared_macro,
                            sonnet_threshold=99,
                        )
                        if _sr and not _sr.get("screened_out") and _conf(_sr) >= 4:
                            if _log_one_research(_sr, src_override="haiku_sweep",
                                                 mode_override=f"{scan_mode}_sweep"):
                                _sweep_new += 1
                    except Exception:
                        pass
                if _sweep_new:
                    _log_line(log, f"Research sweep: {_sweep_new} additional trade(s) logged.")
                if scan_mode == "full":
                    _total_cov = len(deep_results) + len(_sweep_candidates)
                    _log_line(log,
                        f"Morning scan analysed top {len(deep_results)} pairs in depth — "
                        f"{len(_sweep_candidates)} additional pairs screened by research sweep — "
                        f"{_total_cov} pairs total coverage"
                    )

            # Running total — visible in every GitHub Actions run
            try:
                _rt_all  = _rt.load()
                _rt_open = sum(1 for r in _rt_all if r.get("status") == "OPEN")
                _log_line(log,
                    f"✅ Research trade logger: working — {_rt_logged} new trades opened this scan "
                    f"(total: {len(_rt_all)} | open: {_rt_open})")
            except Exception:
                _log_line(log, f"✅ Research trade logger: working — {_rt_logged} new trades opened this scan")

            # Item 5: Research trade logging verification
            # yes_trades is defined in _send_telegram_summary (called later); compute here from deep_results
            _threshold_final_run = float((_threshold_data or {}).get("final_threshold", 7.0))
            _yes_for_check = [r for r in deep_results if _eff_conf(r) >= _threshold_final_run]
            if _yes_for_check and _rt_logged == 0:
                try:
                    _telegram(
                        f"⚠️ RESEARCH TRADE LOGGING — {len(_yes_for_check)} YES trade(s) found but "
                        f"0 research trades were logged — ML training data gap — "
                        f"check GitHub Actions logs"
                    )
                except Exception:
                    pass

        except Exception as exc:
            _log_line(log, f"Research trade logging failed: {exc}")
            try:
                _telegram(
                    "🚨 Research trade logging failed — ML training data not being collected "
                    "— check GitHub Actions log immediately\n"
                    f"Error: {exc}"
                )
            except Exception:
                pass

        # 6. Rebuild dashboard
        try:
            path = dashboard.generate()
            _log_line(log, f"Dashboard updated: {path}")
        except Exception as exc:
            _log_line(log, f"Dashboard step failed: {exc}")

        actionable = [
            f"{r['pair']} {r['parsed']['direction']} (conf {r['parsed']['confidence']})"
            for r in deep_results if r["parsed"].get("trade_this") == "YES"
        ]
        if actionable:
            _log_line(log, "ACTIONABLE TODAY: " + "; ".join(actionable))
        else:
            _log_line(log, "No actionable setups today.")
        _log_line(log, "=== Daily run complete ===")

        # 7. Risk management
        risk_data = {}
        _dd_tier_alert_lines = []
        try:
            from src import risk_manager
            risk_profile    = risk_manager.load_profile()
            _old_dd_mode    = risk_profile.get("drawdown_mode", "normal")
            risk_state      = risk_manager.compute_risk_state(risk_profile)
            exposure        = risk_manager.compute_open_exposure(risk_profile)
            sized = [
                risk_manager.size_trade_from_result(r, risk_profile, risk_state)
                for r in deep_results if r["parsed"].get("trade_this") == "YES"
            ]
            sized = risk_manager.apply_correlation_checks(sized)
            risk_profile.update({
                "risk_mode":               risk_state["risk_mode"],
                "drawdown_mode":           risk_state["drawdown_mode"],
                "dd_tier_entered_balance": risk_state["dd_tier_entered_balance"],
                "dd_tier_entered_peak":    risk_state["dd_tier_entered_peak"],
                "consecutive_losses":      risk_state["consecutive_losses"],
                "consecutive_wins":        risk_state["consecutive_wins"],
                "last_5_win_rate":         risk_state["last_5_win_rate"],
                "total_open_pct":          exposure["total_pct"],
            })
            risk_manager.save_profile(risk_profile)
            risk_data = {
                "profile":      risk_profile,
                "risk_state":   risk_state,
                "exposure":     exposure,
                "sized_trades": sized,
            }
            # Build tier transition alert if drawdown mode changed
            _new_dd_mode = risk_state.get("drawdown_mode", "normal")
            if risk_state.get("drawdown_mode_changed") and _new_dd_mode != _old_dd_mode:
                _fund_bal  = risk_profile.get("estimated_balance", risk_manager.FUND_START)
                _fund_peak = risk_profile.get("peak_balance", _fund_bal)
                _dd_tier_alert_lines = risk_manager.drawdown_tier_alert_lines(
                    _old_dd_mode, _new_dd_mode, risk_state, _fund_bal, _fund_peak
                )
                _log_line(log, f"[DRAWDOWN] Tier transition: {_old_dd_mode} → {_new_dd_mode} "
                          f"(dd={risk_state.get('drawdown_pct',0)*100:.1f}%)")
        except Exception as exc:
            _log_line(log, f"Risk management step failed: {exc}")

        # Fund state: circuit breaker, peak/drawdown, sizing state, weekend alert
        try:
            from src import fund_state as _fs_cb
            _fs_cb_st = _fs_cb.load()
            _cb_bal = (risk_data.get("profile") or {}).get("estimated_balance") if risk_data else None
            if _cb_bal:
                # 1. Daily P&L circuit breaker
                _fs_cb_st, _cb_alert = _fs_cb.check_circuit_breaker(_fs_cb_st, _cb_bal)
                if _cb_alert:
                    _telegram(_cb_alert)
                    try:
                        if _discord:
                            _discord.send_circuit_breaker(
                                _cb_alert,
                                float(_cb_bal or 0),
                                float(_fs_cb_st.get("daily_pnl_pct", 0) or 0),
                                float(_fs_cb_st.get("daily_pnl_usd", 0) or 0),
                            )
                    except Exception:
                        pass
                # 2. Peak balance / drawdown tracking — may fire pause or resume alert
                _fs_cb_st, _dd_pause_alert, _dd_resume_alert = _fs_cb.update_peak_and_drawdown(
                    _fs_cb_st, _cb_bal
                )
                if _dd_pause_alert:
                    _telegram(_dd_pause_alert)
                if _dd_resume_alert:
                    _telegram(_dd_resume_alert)
                # 3. Refresh sizing state for display in next scan
                _fs_cb_st = _fs_cb.update_sizing_state(_fs_cb_st, _cb_bal)
                # 4. Weekend position alert (fires once per Friday 2–4pm Auckland)
                try:
                    from src import tracker as _trk_wka
                    _wka_open = [r for r in _trk_wka.load() if r.get("status") == "OPEN"]
                    _wka_send, _wka_msg = _fs_cb.check_weekend_alert(_fs_cb_st, _wka_open)
                    if _wka_send:
                        _telegram(_wka_msg)
                        try:
                            from zoneinfo import ZoneInfo as _ZI_wka
                        except ImportError:
                            from backports.zoneinfo import ZoneInfo as _ZI_wka
                        from datetime import datetime as _dt_wka
                        _fs_cb_st["weekend_alert_sent_date"] = (
                            _dt_wka.now(_ZI_wka("Pacific/Auckland")).strftime("%Y-%m-%d")
                        )
                except Exception:
                    pass
                _fs_cb.save(_fs_cb_st)
        except Exception as _fs_cb_exc:
            _log_line(log, f"Fund state circuit breaker check failed: {_fs_cb_exc}")

        # 8. Fetch API credit balances
        credit_data = {}
        try:
            from src import billing as _bill
            primary_bal = _bill.fetch_balance(config.ANTHROPIC_API_KEY)
            backup_bal  = _bill.fetch_balance(config.ANTHROPIC_API_KEY_2)
            credit_data = {"primary_balance": primary_bal, "backup_balance": backup_bal}
        except Exception as exc:
            _log_line(log, f"Credit balance check failed: {exc}")

        # 9. Cost summary
        run_duration_min = (time.time() - _run_start) / 60.0
        try:
            from src import analyst as _anl, technical as _tech_mod
            run_stats = _anl.get_run_stats()
            _tech_td  = _tech_mod.get_call_count()
        except Exception:
            run_stats, _tech_td = {}, 0
        # Read total daily call count from api_usage.json (includes selector OHLCV calls)
        try:
            _au = json.loads(_API_USAGE_FILE.read_text(encoding="utf-8")) if _API_USAGE_FILE.exists() else {}
            from datetime import date as _date_td
            td_calls = int(_au.get("calls", 0)) if _au.get("date") == str(_date_td.today()) else _tech_td
        except Exception:
            td_calls = _tech_td

        _log_line(log, (
            f"[COST] Haiku in={run_stats.get('haiku_input',0)} out={run_stats.get('haiku_output',0)} · "
            f"Sonnet in={run_stats.get('sonnet_input',0)} out={run_stats.get('sonnet_output',0)} · "
            f"TD calls={td_calls} · est=${run_stats.get('estimated_usd',0):.4f} · "
            f"cache_hits={run_stats.get('cache_hits',0)} · "
            f"duration={run_duration_min:.1f}m"
        ))

        # Record scan cost and prepare formatted lines for Telegram
        _cost_lines = []
        try:
            from src import cost_tracker as _ct
            _cost_lines = _ct.record_and_get_lines(
                run_stats.get("estimated_usd", 0.0),
                _auckland_now(),
            )
        except Exception as _ct_exc:
            _log_line(log, f"Cost tracking failed: {_ct_exc}")

        # Research threshold analysis (only meaningful after 30 days of paper trades)
        research_result = None
        try:
            from src import research_analyst as _ra
            research_result = _ra.analyse(log=lambda m: _log_line(log, m))
        except Exception as exc:
            _log_line(log, f"Research analysis failed: {exc}")

        # Alert deduplication: intraday scans only notify when new trade alerts appear
        _last_alerts    = _load_last_alerts()
        _current_alerts = _alert_fingerprints(deep_results)
        _new_alerts     = _current_alerts - _last_alerts
        _save_alerts(_current_alerts)
        _should_notify = True
        _log_line(log, f"[{scan_mode}] Sending Telegram. New alerts: {sorted(_new_alerts) if _new_alerts else 'none'}")

        # 10. Send drawdown tier transition alert (separate message, sent before main summary)
        if _dd_tier_alert_lines:
            try:
                _telegram("\n".join(_dd_tier_alert_lines))
            except Exception as _dda_exc:
                _log_line(log, f"Drawdown tier alert send failed: {_dda_exc}")

        # 11. Send Telegram summary
        if _should_notify:
            try:
                _send_telegram_summary(
                    date=date,
                    universe_size=universe_size,
                    total_scanned=len(analysed_pairs),
                    deep_results=deep_results,
                    closed_today=closed_today,
                    new_patterns=new_patterns,
                    stats=learning_stats,
                    risk_data=risk_data,
                    stage1_filtered=stage1_filtered,
                    failed_pairs=failed_pairs,
                    credit_data=credit_data,
                    run_stats=run_stats,
                    td_calls=td_calls,
                    run_duration_min=run_duration_min,
                    scan_mode=scan_mode,
                    new_alerts=_new_alerts,
                    research_result=research_result,
                    threshold_revert_msg=threshold_revert_msg,
                    cost_lines=_cost_lines,
                    opportunity_data=_opportunity_data,
                    all_ohlcv_failed=_all_ohlcv_failed,
                    movement_alert_data=_movement_alert_data,
                    threshold_data=_threshold_data,
                    log=log,
                )
            except Exception as _tg_exc:
                print(f"[telegram] _send_telegram_summary CRASHED: {_tg_exc}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                _telegram(
                    f"⚠️ <b>{scan_mode.upper()} scan complete but summary build failed</b>\n"
                    f"{type(_tg_exc).__name__}: {str(_tg_exc)[:200]}"
                )
        # These are locals of _send_telegram_summary — init safe defaults for Discord report.
        _blocked_setups = []
        _swapped_setups = []
        _override_pairs = {}
        try:
            if _discord:
                _log_line(log, "[discord] Preparing scan report data...")

                # ── Fund state ───────────────────────────────────────────────────
                try:
                    with open(config.DATA_DIR / "fund_state.json", encoding="utf-8") as _dsc_fsf:
                        _dsc_fs = json.load(_dsc_fsf)
                except Exception as _dsc_fse:
                    _log_line(log, f"[discord] fund_state read error: {_dsc_fse}")
                    _dsc_fs = _fs_cb_st if isinstance(locals().get("_fs_cb_st"), dict) else {}

                def _dsc_tof(v, d=0.0):
                    try:
                        r = float(v)
                        return r if r == r else d
                    except Exception:
                        return d

                _dsc_open_bal = _dsc_tof(_dsc_fs.get("daily_opening_balance"), 10000)
                _dsc_dpnl_d   = _dsc_tof(_dsc_fs.get("daily_pnl_dollars"), 0)
                _dsc_bal      = _dsc_tof(_dsc_fs.get("balance"), _dsc_open_bal + _dsc_dpnl_d)
                _dsc_peak     = _dsc_tof(_dsc_fs.get("peak_balance"), _dsc_bal)
                _dsc_dd       = _dsc_tof(_dsc_fs.get("current_drawdown_pct"), 0)
                _dsc_dpnl_pct = _dsc_tof(_dsc_fs.get("daily_pnl_pct"), 0)
                _dsc_szpct    = _dsc_tof(_dsc_fs.get("current_sizing_pct"), 1.0)
                _dsc_cw       = int(_dsc_fs.get("consecutive_wins") or 0)
                _dsc_cl       = int(_dsc_fs.get("consecutive_losses") or 0)

                # ── Fund performance from trades.csv ─────────────────────────────
                import pandas as _dsc_pd
                _dsc_fopen_cnt = 0
                _dsc_fund_tot  = 0
                _dsc_dec       = 0
                _dsc_fwr       = 0.0
                _dsc_fw        = _dsc_fp = _dsc_fl = 0
                _dsc_awp = _dsc_awd = _dsc_alp = _dsc_ald = _dsc_pfd = 0.0
                _dsc_best_pr   = ""
                _dsc_best_pp   = 0.0
                _dsc_total_ret = 0.0
                _dsc_expiry_al = []
                try:
                    _dsc_df    = _dsc_pd.read_csv(config.DATA_DIR / "trades.csv", encoding="utf-8-sig")
                    _dsc_fund  = _dsc_df[_dsc_df["trade_this"].astype(str) == "YES"].copy()
                    _dsc_fo    = _dsc_fund[_dsc_fund["status"].astype(str).str.upper() == "OPEN"]
                    _dsc_fc    = _dsc_fund[_dsc_fund["status"].astype(str).str.upper() != "OPEN"]
                    _dsc_fopen_cnt = len(_dsc_fo)
                    _dsc_fund_tot  = len(_dsc_fund)
                    _dsc_pip_n = _dsc_pd.to_numeric(_dsc_fc["pips"], errors="coerce").fillna(0)
                    # Win/loss determined purely by pips — status labels are display only
                    _dsc_wins_m   = _dsc_pip_n > 0
                    _dsc_losses_m = _dsc_pip_n < 0
                    _dsc_fw_tot   = int(_dsc_wins_m.sum())    # all positive-pip trades
                    _dsc_fl_tot   = int(_dsc_losses_m.sum())  # all negative-pip trades
                    _dsc_dec      = _dsc_fw_tot + _dsc_fl_tot
                    _dsc_fwr      = _dsc_fw_tot / _dsc_dec * 100 if _dsc_dec > 0 else 0.0
                    # Cascade breakdown (subset of wins) for display transparency
                    _dsc_status_u = _dsc_fc["status"].str.upper()
                    _dsc_fp  = int((_dsc_wins_m & _dsc_status_u.isin(["PARTIAL_WIN", "PROTECTED"])).sum())
                    _dsc_fw  = _dsc_fw_tot - _dsc_fp   # full wins
                    _dsc_fl  = _dsc_fl_tot             # = all losses
                    _log_line(log, (
                        f"[discord] Fund perf: "
                        f"wins={_dsc_fw_tot} "
                        f"(full={_dsc_fw} protected={_dsc_fp}) "
                        f"losses={_dsc_fl} "
                        f"decisive={_dsc_dec} "
                        f"wr={_dsc_fwr:.0f}%"
                    ))

                    def _dsc_dollar(row):
                        try:
                            pair   = str(row.get("pair", ""))
                            ps     = 0.01 if "JPY" in pair else 0.0001
                            entry  = _dsc_tof(row.get("entry"))
                            stop   = _dsc_tof(row.get("stop_loss"))
                            pips   = _dsc_tof(row.get("pips"))
                            szp    = _dsc_tof(row.get("position_size_pct_at_entry"), 0)
                            if szp <= 0 or szp != szp:
                                szp = _dsc_szpct
                            sp = abs(entry - stop) / ps if entry and stop else 100
                            return pips * (_dsc_bal * szp / 100 / sp) if sp > 0 else 0.0
                        except Exception:
                            return 0.0

                    _dsc_wrows = _dsc_fc[_dsc_wins_m]
                    _dsc_lrows = _dsc_fc[_dsc_losses_m]
                    _dsc_wd = [_dsc_dollar(r) for _, r in _dsc_wrows.iterrows()]
                    _dsc_ld = [abs(_dsc_dollar(r)) for _, r in _dsc_lrows.iterrows()]
                    if len(_dsc_wrows):
                        _dsc_awp = float(_dsc_pd.to_numeric(_dsc_wrows["pips"], errors="coerce").mean() or 0)
                        _dsc_awd = sum(_dsc_wd) / len(_dsc_wd) if _dsc_wd else 0
                    if len(_dsc_lrows):
                        _dsc_alp = abs(float(_dsc_pd.to_numeric(_dsc_lrows["pips"], errors="coerce").mean() or 0))
                        _dsc_ald = sum(_dsc_ld) / len(_dsc_ld) if _dsc_ld else 0
                    _tw = sum(_dsc_wd)
                    _tl = sum(_dsc_ld) or 0.01
                    _dsc_pfd = _tw / _tl
                    if len(_dsc_fc):
                        _bi = _dsc_pd.to_numeric(_dsc_fc["pips"], errors="coerce").idxmax()
                        _br = _dsc_fc.loc[_bi]
                        _dsc_best_pr = str(_br.get("pair", ""))
                        _dsc_best_pp = _dsc_tof(_br.get("pips"))
                    _dsc_total_ret = (_dsc_bal - 10000) / 10000 * 100
                    # Expiry alerts
                    for _, _dsc_ot in _dsc_fo.iterrows():
                        for _ecol in ("expiry_date", "expiry"):
                            _ev = str(_dsc_ot.get(_ecol, "") or "")
                            if _ev and _ev not in ("nan", "None", ""):
                                try:
                                    _edt = datetime.strptime(_ev[:10], "%Y-%m-%d")
                                    _dl  = (_edt - datetime.now(timezone.utc).replace(tzinfo=None)).days
                                    if _dl <= 2:
                                        _dsc_expiry_al.append(
                                            f"⚠️ {_dsc_ot.get('pair', '?')} expires in {_dl}d"
                                        )
                                except Exception:
                                    pass
                                break
                except Exception as _dsc_fe:
                    _log_line(log, f"[discord] fund trades error: {_dsc_fe}")

                # ── Research trades ──────────────────────────────────────────────
                _dsc_rt_open = _dsc_rt_clsd = _dsc_rt_dec = 0
                _dsc_rt_wr   = _dsc_rt_pf   = 0.0
                _dsc_wr45 = _dsc_wr56 = _dsc_wr67 = _dsc_wr7p = 0.0
                _dsc_n45  = _dsc_n56  = _dsc_n67  = _dsc_n7p  = 0
                _dsc_best_pairs_s = ""
                _dsc_hot_str      = ""
                _dsc_adapt_cnt    = 0
                try:
                    _dsc_rt   = _dsc_pd.read_csv(config.DATA_DIR / "research_trades.csv", encoding="utf-8-sig")
                    _dsc_rt_open = int((_dsc_rt["status"] == "OPEN").sum())
                    _dsc_rtc  = _dsc_rt[_dsc_rt["status"] != "OPEN"]
                    _dsc_rt_clsd = len(_dsc_rtc)
                    _dsc_rtp  = _dsc_pd.to_numeric(_dsc_rtc["pips"], errors="coerce").fillna(0)
                    _dsc_rtw  = _dsc_rtc[
                        _dsc_rtc["status"].str.upper().isin(["WIN", "FULL_WIN", "PARTIAL_WIN"]) |
                        (_dsc_rtc["status"].str.upper().isin(["EXPIRED"]) & (_dsc_rtp > 0))
                    ]
                    _dsc_rtl  = _dsc_rtc[
                        _dsc_rtc["status"].str.upper().isin(["LOSS"]) |
                        (_dsc_rtc["status"].str.upper().isin(["EXPIRED"]) & (_dsc_rtp <= 0))
                    ]
                    _dsc_rt_dec = len(_dsc_rtw) + len(_dsc_rtl)
                    if _dsc_rt_dec > 0:
                        _dsc_rt_wr = len(_dsc_rtw) / _dsc_rt_dec * 100
                    _rwp = _dsc_pd.to_numeric(_dsc_rtw["pips"], errors="coerce").sum() if len(_dsc_rtw) else 0
                    _rlp = abs(_dsc_pd.to_numeric(_dsc_rtl["pips"], errors="coerce").sum()) if len(_dsc_rtl) else 0.01
                    _dsc_rt_pf = _rwp / _rlp

                    def _dsc_wrband(lo, hi):
                        _cn = _dsc_pd.to_numeric(_dsc_rtc["confidence"], errors="coerce").fillna(0)
                        _s  = _dsc_rtc[(_cn >= lo) & (_cn < hi)]
                        if not len(_s):
                            return 0.0, 0
                        _sw = _s[_s["status"].str.upper().isin(["WIN", "FULL_WIN", "PARTIAL_WIN"])]
                        _sl = _s[_s["status"].str.upper().isin(["LOSS"])]
                        _d  = len(_sw) + len(_sl)
                        return (len(_sw) / _d * 100 if _d else 0.0), _d

                    _dsc_wr45, _dsc_n45 = _dsc_wrband(4, 5)
                    _dsc_wr56, _dsc_n56 = _dsc_wrband(5, 6)
                    _dsc_wr67, _dsc_n67 = _dsc_wrband(6, 7)
                    _dsc_wr7p, _dsc_n7p = _dsc_wrband(7, 10)

                    _dsc_pstat = {}
                    for _dsc_pair in _dsc_rtc["pair"].dropna().unique():
                        _pt = _dsc_rtc[_dsc_rtc["pair"] == _dsc_pair]
                        _pw = _pt[_pt["status"].str.upper().isin(["WIN", "FULL_WIN", "PARTIAL_WIN"])]
                        _pl = _pt[_pt["status"].str.upper().isin(["LOSS"])]
                        _pd = len(_pw) + len(_pl)
                        if _pd >= 5:
                            _dsc_pstat[_dsc_pair] = (len(_pw) / _pd * 100, _pd)
                    _dsc_bp = sorted(_dsc_pstat.items(), key=lambda x: x[1][0], reverse=True)[:3]
                    _dsc_best_pairs_s = " · ".join(f"{p} {v[0]:.0f}% ({v[1]})" for p, v in _dsc_bp)

                    # Hot zone: open research trades that have hit T1
                    _dsc_rt_hot = _dsc_rt[
                        (_dsc_rt["status"] == "OPEN") &
                        (_dsc_rt["t1_hit"].astype(str).str.upper().isin(["TRUE", "1"]))
                    ]
                    if len(_dsc_rt_hot) > 0:
                        _dsc_hot_lines = []
                        for _, _hr in _dsc_rt_hot.head(5).iterrows():
                            _hr_pair = str(_hr.get("pair", ""))
                            _hr_dir  = str(_hr.get("direction", "")).upper()
                            _hr_t1p  = float(_hr.get("t1_hit_pips") or 0)
                            _hr_t2h  = str(_hr.get("t2_hit", "")).upper() in ("TRUE", "1")
                            _hr_icon = "\U0001f525\U0001f525" if _hr_t2h else "\U0001f525"
                            _dsc_hot_lines.append(
                                f"{_hr_icon} {_hr_pair} {_hr_dir} "
                                f"+{_hr_t1p:.0f}p "
                                f"{'(T2 hit)' if _hr_t2h else '(T1 hit)'}"
                            )
                        _remaining = len(_dsc_rt_hot) - min(5, len(_dsc_rt_hot))
                        _dsc_hot_str = (
                            f"Hot zone ({len(_dsc_rt_hot)} at T1+):\n"
                            + "\n".join(_dsc_hot_lines)
                            + (f"\n_…and {_remaining} more_" if _remaining > 0 else "")
                        )

                    try:
                        with open(config.DATA_DIR / "pair_statistics.json", encoding="utf-8") as _dsc_psf:
                            _dsc_ps = json.load(_dsc_psf)
                        _dsc_adapt_cnt = sum(
                            1 for v in _dsc_ps.values()
                            if isinstance(v, dict) and v.get("total_trades", 0) >= 10
                        )
                    except Exception:
                        pass
                except Exception as _dsc_rte:
                    _log_line(log, f"[discord] research trades error: {_dsc_rte}")

                # ── ML system ────────────────────────────────────────────────────
                _dsc_ml_n   = 0
                _dsc_ml_acc = _dsc_ml_rwr = _dsc_ml_owr = 0.0
                _dsc_ml_act = False
                _dsc_ml_lr  = "never"
                _dsc_mta    = _dsc_hhhl = 0.0
                try:
                    _dsc_ml_path = config.DATA_DIR / "online_model_meta.json"
                    if _dsc_ml_path.exists():
                        _dsc_ml_meta = json.loads(_dsc_ml_path.read_text(encoding="utf-8"))
                        _dsc_ml_n   = int(_dsc_ml_meta.get("n_decisive") or 0)
                        _dsc_ml_rwr = float(_dsc_ml_meta.get("recent_win_rate") or 0) * 100
                        _dsc_ml_owr = float(_dsc_ml_meta.get("overall_win_rate") or 0) * 100
                        _dsc_ml_acc = _dsc_ml_owr
                        # Model is ACTIVE only when all three gates pass
                        _n_reliable  = int(_dsc_ml_meta.get("n_consecutive_reliable", 0) or 0)
                        _model_ready = bool(_dsc_ml_meta.get("model_ready", False))
                        _dsc_ml_act  = (
                            _model_ready
                            and _n_reliable  >= 3      # 3+ consecutive reliable retrains
                            and _dsc_ml_rwr  > 50.0    # recent win rate > 50%
                            and _dsc_ml_n    >= 50     # at least 50 decisive trades
                        )
                        _lr_ts = str(_dsc_ml_meta.get("last_updated") or "")
                        if _lr_ts:
                            try:
                                _lr_dt = datetime.fromisoformat(_lr_ts[:19])
                                _hrs   = (datetime.now(timezone.utc).replace(tzinfo=None) - _lr_dt).total_seconds() / 3600
                                _dsc_ml_lr = f"{_hrs:.0f}h ago" if _hrs < 24 else f"{_hrs/24:.0f}d ago"
                            except Exception:
                                _dsc_ml_lr = _lr_ts[:10]
                    _dsc_rt_check = _dsc_pd.read_csv(config.DATA_DIR / "research_trades.csv", encoding="utf-8-sig")
                    if "monthly_trend_aligned" in _dsc_rt_check.columns:
                        _dsc_mta = float(_dsc_rt_check["monthly_trend_aligned"].notna().mean() * 100)
                    if "hhhl_aligned" in _dsc_rt_check.columns:
                        _dsc_hhhl = float(_dsc_rt_check["hhhl_aligned"].notna().mean() * 100)
                except Exception as _dsc_mle:
                    _log_line(log, f"[discord] ML data error: {_dsc_mle}")

                # ── Context from scan ────────────────────────────────────────────
                _dsc_regime    = str((_threshold_data or {}).get("regime") or "")
                _dsc_threshold = float((_threshold_data or {}).get("final_threshold") or 6.0)
                _dsc_dqpct     = float((_threshold_data or {}).get("data_quality_pct") or 99)
                _dsc_cost      = float((run_stats or {}).get("estimated_usd") or 0)
                try:
                    _dsc_dur = float(run_duration_min or 0)
                except Exception:
                    _dsc_dur = 0.0
                try:
                    _dsc_td_n = int(td_calls or 0)
                except Exception:
                    _dsc_td_n = 0
                try:
                    _dsc_pairs_n = len(analysed_pairs or [])
                except Exception:
                    _dsc_pairs_n = 0

                # New setups — read directly from CSV (yes_trades is a local var of _send_telegram_summary and is not in scope here)
                try:
                    import pandas as _dsc_pd2
                    from datetime import timezone as _tz2, timedelta as _td2
                    _df_new = _dsc_pd2.read_csv(config.DATA_DIR / "trades.csv", encoding="utf-8-sig")
                    _cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
                    _fund_new = _df_new[_df_new["trade_this"].astype(str) == "YES"]
                    _dsc_yes_raw = []
                    for _, _nt in _fund_new.iterrows():
                        _ts_nt = str(_nt.get("timestamp", "") or "")
                        try:
                            _dt_nt = datetime.strptime(_ts_nt[:19], "%Y-%m-%d %H:%M:%S")
                            if _dt_nt > _cutoff:
                                _dsc_yes_raw.append({
                                    "pair":         str(_nt.get("pair", "")),
                                    "direction":    str(_nt.get("direction", "")),
                                    "conf":         float(_nt.get("confidence", 0) or 0),
                                    "entry":        float(_nt.get("entry", 0) or 0),
                                    "stop":         float(_nt.get("stop_loss", 0) or 0),
                                    "t1":           float(_nt.get("t1_price") or _nt.get("target", 0) or 0),
                                    "t2":           float(_nt.get("t2_price", 0) or 0),
                                    "rr":           float(_nt.get("reward_risk", 0) or 0),
                                    "status":       str(_nt.get("status", "")),
                                    "override_tier": _override_pairs.get(str(_nt.get("pair", "")), None),
                                })
                        except Exception:
                            pass
                    _log_line(log, (
                        f"[discord] New trades from CSV: {len(_dsc_yes_raw)} — "
                        f"{[t['pair'] for t in _dsc_yes_raw]}"
                    ))
                except Exception as _e_yes:
                    _log_line(log, f"[discord] New trade read failed: {_e_yes}")
                    _dsc_yes_raw = []

                # Blocked trades
                try:
                    _dsc_blk_raw = [
                        {
                            "pair":      _bt.get("pair", ""),
                            "direction": (_bt.get("parsed") or {}).get("direction", ""),
                            "conf":      float((_bt.get("parsed") or {}).get("confidence") or 0),
                            "reason":    str(_br),
                        }
                        for _bt, _br in (_fund_st_blocked or [])
                    ]
                except NameError:
                    _dsc_blk_raw = []

                # Watch list from cache
                _dsc_watch_raw = []
                try:
                    _wl_path_dsc = config.DATA_DIR / "watchlist_cache.json"
                    if _wl_path_dsc.exists():
                        _dsc_wl_data = json.loads(_wl_path_dsc.read_text(encoding="utf-8"))
                        for _dsc_wp in (_dsc_wl_data if isinstance(_dsc_wl_data, list) else []):
                            _dsc_watch_raw.append({
                                "pair":      _dsc_wp.get("pair", ""),
                                "direction": _dsc_wp.get("direction", ""),
                                "conf":      float(_dsc_wp.get("confidence") or 0),
                                "reason":    str(_dsc_wp.get("reason") or ""),
                            })
                except Exception:
                    pass

                # Sizing mode from fund state
                _dsc_sizing_mode = str(_dsc_fs.get("sizing_mode") or "normal")

                # Active sessions
                try:
                    _dsc_active_sess = _get_current_session()
                except Exception:
                    _dsc_active_sess = []

                # News blackout pairs (pairs with upcoming HIGH-impact news)
                _dsc_news_blk = []
                try:
                    for _, _dsc_ot2 in _dsc_fo.iterrows():
                        _dsc_np = str(_dsc_ot2.get("pair", ""))
                        if _dsc_np:
                            _dsc_nr = _has_upcoming_news(_dsc_np, log_fn=None)
                            if _dsc_nr.get("blocked"):
                                _dsc_news_blk.append(_dsc_np)
                    # Also check freshly blocked trades
                    for _bt2, _br2 in (_fund_st_blocked or []):
                        if "news" in str(_br2).lower() or "blackout" in str(_br2).lower():
                            _dsc_np2 = _bt2.get("pair", "")
                            if _dsc_np2 and _dsc_np2 not in _dsc_news_blk:
                                _dsc_news_blk.append(_dsc_np2)
                except Exception:
                    pass

                # Open trades for trailing stop display
                _dsc_open_trades = []
                try:
                    for _, _dsc_ot3 in _dsc_fo.iterrows():
                        _dsc_open_trades.append({
                            "pair":           str(_dsc_ot3.get("pair", "")),
                            "direction":      str(_dsc_ot3.get("direction", "")),
                            "entry":          float(_dsc_ot3.get("entry") or 0),
                            "stop_loss":      float(_dsc_ot3.get("stop_loss") or 0),
                            "effective_stop": float(_dsc_ot3.get("effective_stop") or _dsc_ot3.get("stop_loss") or 0),
                            "t1_hit":         str(_dsc_ot3.get("t1_hit", "")).upper() in ("TRUE", "1", "YES"),
                            "t2_hit":         str(_dsc_ot3.get("t2_hit", "")).upper() in ("TRUE", "1", "YES"),
                            "t3_hit":         str(_dsc_ot3.get("t3_hit", "")).upper() in ("TRUE", "1", "YES"),
                        })
                except Exception:
                    pass

                # Auckland time for header
                try:
                    _dsc_auck = _auckland_now().strftime("%H:%M NZST")
                except Exception:
                    _dsc_auck = ""
                try:
                    _dsc_date_str = str(date)
                except Exception:
                    _dsc_date_str = ""

                _log_line(log, (
                    f"[discord] Data ready: bal=${_dsc_bal:,.0f} "
                    f"open={_dsc_fopen_cnt} rt_open={_dsc_rt_open} "
                    f"rt_wr={_dsc_rt_wr:.0f}% ml_trained={_dsc_ml_n} "
                    f"ml_active={_dsc_ml_act}"
                ))

                _log_line(log, "Sending Discord scan report...")
                _discord.send_master_scan_report(
                    scan_mode=scan_mode,
                    auckland_time=_dsc_auck,
                    scan_date=_dsc_date_str,
                    yes_trades=_dsc_yes_raw,
                    blocked_trades=_dsc_blk_raw,
                    watch_list=_dsc_watch_raw,
                    fund_balance=_dsc_bal,
                    daily_pnl_dollars=_dsc_dpnl_d,
                    daily_pnl_pct=_dsc_dpnl_pct,
                    drawdown_pct=_dsc_dd,
                    peak_balance=_dsc_peak,
                    open_count=_dsc_fopen_cnt,
                    consecutive_wins=_dsc_cw,
                    consecutive_losses=_dsc_cl,
                    risk_pct=_dsc_szpct,
                    fund_total=_dsc_fund_tot,
                    fund_decisive=_dsc_dec,
                    fund_win_rate=_dsc_fwr,
                    fund_wins=_dsc_fw,
                    fund_protected=_dsc_fp,
                    fund_losses=_dsc_fl,
                    avg_win_pips=_dsc_awp,
                    avg_win_dollars=_dsc_awd,
                    avg_loss_pips=_dsc_alp,
                    avg_loss_dollars=_dsc_ald,
                    profit_factor_dollars=_dsc_pfd,
                    best_pair=_dsc_best_pr,
                    best_pips=_dsc_best_pp,
                    total_return_pct=_dsc_total_ret,
                    research_open=_dsc_rt_open,
                    research_closed=_dsc_rt_clsd,
                    research_win_rate=_dsc_rt_wr,
                    research_decisive=_dsc_rt_dec,
                    research_pf=_dsc_rt_pf,
                    wr_band_45=_dsc_wr45,
                    wr_band_56=_dsc_wr56,
                    wr_band_67=_dsc_wr67,
                    wr_band_7p=_dsc_wr7p,
                    n_band_45=_dsc_n45,
                    n_band_56=_dsc_n56,
                    n_band_67=_dsc_n67,
                    n_band_7p=_dsc_n7p,
                    best_pairs_str=_dsc_best_pairs_s,
                    hot_research_str=_dsc_hot_str,
                    adaptive_count=_dsc_adapt_cnt,
                    ml_trained=_dsc_ml_n,
                    ml_accuracy=_dsc_ml_acc,
                    ml_recent_wr=_dsc_ml_rwr,
                    ml_overall_wr=_dsc_ml_owr,
                    ml_active=_dsc_ml_act,
                    ml_reliable_retrains=_n_reliable,
                    ml_last_retrain=_dsc_ml_lr,
                    mta_pct=_dsc_mta,
                    hhhl_pct=_dsc_hhhl,
                    regime=_dsc_regime,
                    threshold=_dsc_threshold,
                    dq_pct=_dsc_dqpct,
                    td_calls=_dsc_td_n,
                    scan_cost=_dsc_cost,
                    scan_duration=_dsc_dur,
                    pairs_analysed=_dsc_pairs_n,
                    expiry_alerts=_dsc_expiry_al,
                    sizing_mode=_dsc_sizing_mode,
                    active_sessions=_dsc_active_sess,
                    news_blackout_pairs=_dsc_news_blk,
                    open_trades=_dsc_open_trades,
                    blocked_setups=_blocked_setups,
                    swapped_setups=_swapped_setups,
                )
                _log_line(log, "Discord scan report sent ✅")
        except Exception as _dsc_exc:
            import traceback as _dsc_tb
            _log_line(log,
                f"Discord scan report FAILED: {type(_dsc_exc).__name__}: {_dsc_exc}")
            _log_line(log, _dsc_tb.format_exc())

    # FIX 12: Write scan completion marker AFTER all Discord/Telegram sends complete.
    # Uses same keys as the digest reader at line 7281 (completed, finished, mode).
    if IS_GITHUB_ACTIONS:
        try:
            _ss_fin_ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            _ss_data_final: dict = {}
            if _SCAN_STATUS_FILE.exists():
                try:
                    _ss_data_final = json.loads(_SCAN_STATUS_FILE.read_text(encoding="utf-8"))
                except Exception:
                    pass
            _ss_data_final.update({
                "completed": True,
                "finished": _ss_fin_ts,
                "mode": scan_mode,
                "scan_completed": True,
                "finished_utc": _ss_fin_ts,
            })
            _SCAN_STATUS_FILE.write_text(json.dumps(_ss_data_final, indent=2), encoding="utf-8")
        except Exception:
            pass

    _timeout_timer.cancel()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
