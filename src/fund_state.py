"""Fund state: daily trade limits, circuit breaker, consecutive loss tracking.

All limits apply ONLY to main fund trades (trades.csv). Research trades are never blocked.
State persists across GitHub Actions runs via data/fund_state.json.
"""
import json
from datetime import datetime, timedelta

import config

_FILE = config.DATA_DIR / "fund_state.json"

DAILY_TRADE_LIMIT           = 3
CIRCUIT_BREAKER_PCT         = -2.0     # daily % loss that trips circuit breaker
WEEKLY_LOSS_LIMIT_PCT       = -5.0     # weekly % loss → observation mode
CONSECUTIVE_LOSS_LIMIT      = 3        # consecutive losses before 48h pause
CONSECUTIVE_LOSS_PAUSE_HRS  = 48
MAX_CURRENCY_EXPOSURE       = 2        # max open fund trades per currency

_DEFAULTS: dict = {
    "daily_trades_date":      "",
    "daily_trades_count":     0,
    "daily_opening_balance":  0.0,
    "daily_pnl_dollars":      0.0,
    "daily_pnl_pct":          0.0,
    "circuit_breaker_active": False,
    "circuit_breaker_reason": None,
    "consecutive_losses":     0,
    "pause_until":            None,
    "weekly_loss_pct":        0.0,
    "weekly_opening_balance": 0.0,
    "weekly_start_date":      "",
    "observation_mode":       False,
    "observation_mode_until": None,
    "missed_opportunities":   [],
    # ── Adaptive sizing fields ────────────────────────────────────────────────
    "consecutive_wins":        0,
    "peak_balance":            0.0,
    "current_drawdown_pct":    0.0,
    "current_sizing_pct":      1.0,
    "sizing_mode":             "normal",
    "sizing_reason":           "standard 1% risk",
    "weekend_alert_sent_date": None,
    "drawdown_paused":         False,
    "max_drawdown_seen":       0.0,
}


def _auckland_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Pacific/Auckland")).replace(tzinfo=None)
    except Exception:
        from datetime import timezone
        utc = datetime.now(timezone.utc)
        off = 13 if utc.month in (10, 11, 12, 1, 2, 3) else 12
        return (utc + timedelta(hours=off)).replace(tzinfo=None)


def load() -> dict:
    """Load fund_state.json, merging defaults for any missing keys."""
    try:
        if _FILE.exists():
            data = json.loads(_FILE.read_text(encoding="utf-8"))
            for k, v in _DEFAULTS.items():
                if k not in data:
                    data[k] = v
            return data
    except Exception:
        pass
    return dict(_DEFAULTS)


def save(state: dict) -> None:
    try:
        _FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def reset_if_new_day(state: dict, current_balance: float | None = None) -> dict:
    """Reset daily fields when the Auckland date changes."""
    today = _auckland_now().strftime("%Y-%m-%d")
    if state.get("daily_trades_date") == today:
        return state
    state = dict(state)
    state["daily_trades_date"]  = today
    state["daily_trades_count"] = 0
    state["daily_pnl_dollars"]  = 0.0
    state["daily_pnl_pct"]      = 0.0
    if state.get("circuit_breaker_active"):
        state["circuit_breaker_active"] = False
        state["circuit_breaker_reason"] = None
    bal = current_balance or float(config.ACCOUNT_BALANCE)
    state["daily_opening_balance"] = bal
    return state


def reset_if_new_week(state: dict) -> dict:
    """Reset weekly fields at Monday 6am Auckland."""
    now   = _auckland_now()
    today = now.strftime("%Y-%m-%d")
    if not (now.weekday() == 0 and now.hour >= 6):
        return state
    if state.get("weekly_start_date") == today:
        return state
    state = dict(state)
    state["weekly_loss_pct"]        = 0.0
    state["weekly_opening_balance"] = float(config.ACCOUNT_BALANCE)
    state["weekly_start_date"]      = today
    if state.get("observation_mode"):
        obs_until = state.get("observation_mode_until")
        if obs_until:
            try:
                exp = datetime.strptime(obs_until[:19], "%Y-%m-%dT%H:%M:%S")
                if now >= exp:
                    state["observation_mode"]       = False
                    state["observation_mode_until"] = None
            except Exception:
                state["observation_mode"]       = False
                state["observation_mode_until"] = None
    return state


def is_trading_blocked(state: dict) -> tuple:
    """Return (blocked, reason_str, type_str).

    type_str: 'none' | 'circuit_breaker' | 'daily_limit' | 'pause' | 'observation_mode'
    """
    now = _auckland_now()

    if state.get("circuit_breaker_active"):
        reason = state.get("circuit_breaker_reason") or "Daily loss limit reached"
        return True, reason, "circuit_breaker"

    if state.get("daily_trades_count", 0) >= DAILY_TRADE_LIMIT:
        return (True,
                f"Daily limit: {DAILY_TRADE_LIMIT} fund trades already opened today",
                "daily_limit")

    pause_until = state.get("pause_until")
    if pause_until:
        try:
            pu = datetime.strptime(pause_until[:19], "%Y-%m-%dT%H:%M:%S")
            if now < pu:
                return (True,
                        f"3 consecutive losses — paused until {pu.strftime('%a %d %b %H:%M')} Auckland",
                        "pause")
        except Exception:
            pass

    if state.get("observation_mode"):
        obs_until = state.get("observation_mode_until")
        if obs_until:
            try:
                ou = datetime.strptime(obs_until[:19], "%Y-%m-%dT%H:%M:%S")
                if now < ou:
                    return (True,
                            f"Weekly loss limit — observation until {ou.strftime('%a %d %b %H:%M')} Auckland",
                            "observation_mode")
            except Exception:
                pass

    return False, "", "none"


def check_currency_exposure(new_pair: str, open_trades: list) -> tuple:
    """Return (blocked, blocking_currency_str)."""
    parts = new_pair.upper().replace(" ", "").split("/")
    if len(parts) != 2:
        return False, ""
    new_ccys = set(parts)
    exposure: dict = {}
    for row in open_trades:
        pair = (row.get("pair") or "").upper().replace(" ", "")
        pp = pair.split("/")
        if len(pp) == 2:
            for ccy in pp:
                exposure[ccy] = exposure.get(ccy, 0) + 1
    for ccy in new_ccys:
        if exposure.get(ccy, 0) >= MAX_CURRENCY_EXPOSURE:
            return True, ccy
    return False, ""


def increment_daily_trades(state: dict) -> dict:
    state = dict(state)
    state["daily_trades_count"] = state.get("daily_trades_count", 0) + 1
    return state


def update_after_close(state: dict, outcome: str,
                       balance: float | None = None) -> tuple:
    """Update consecutive losses after a fund trade closes.

    Returns (updated_state, alert_msg_or_None).
    alert_msg is set only when the 3-loss pause triggers.
    """
    state = dict(state)
    outcome_up = outcome.upper()
    is_loss = outcome_up == "LOSS"
    is_win  = outcome_up in ("WIN", "FULL_WIN", "PARTIAL_WIN", "BREAKEVEN")

    if is_loss:
        state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
        state["consecutive_wins"]   = 0
    elif is_win:
        state["consecutive_losses"] = 0
        state["consecutive_wins"]   = state.get("consecutive_wins", 0) + 1

    alert = None
    if state.get("consecutive_losses", 0) >= CONSECUTIVE_LOSS_LIMIT:
        if not state.get("pause_until"):
            now = _auckland_now()
            pause_ts = now + timedelta(hours=CONSECUTIVE_LOSS_PAUSE_HRS)
            state["pause_until"] = pause_ts.strftime("%Y-%m-%dT%H:%M:%S")
            pause_str = pause_ts.strftime("%A %d %b %Y %I:%M%p")
            alert = (
                f"⚠️ <b>Three consecutive losses — 48-hour pause activated</b>\n\n"
                f"No new fund trades until <b>{pause_str} Auckland</b>.\n"
                f"Existing trades remain open and monitored.\n"
                f"System continues scanning and sending watch list updates."
            )

    if balance is not None:
        weekly_open = float(state.get("weekly_opening_balance") or float(config.ACCOUNT_BALANCE))
        if weekly_open > 0:
            state["weekly_loss_pct"] = round((balance - weekly_open) / weekly_open * 100, 2)

    return state, alert


def check_circuit_breaker(state: dict, current_balance: float) -> tuple:
    """Check daily P&L and weekly loss limits.

    Returns (updated_state, alert_msg_or_None).
    """
    opening = float(state.get("daily_opening_balance") or float(config.ACCOUNT_BALANCE))
    if opening <= 0:
        return state, None

    pnl_pct = (current_balance - opening) / opening * 100
    pnl_usd = current_balance - opening

    state = dict(state)
    state["daily_pnl_dollars"] = round(pnl_usd, 2)
    state["daily_pnl_pct"]     = round(pnl_pct, 2)

    if pnl_pct <= CIRCUIT_BREAKER_PCT and not state.get("circuit_breaker_active"):
        state["circuit_breaker_active"] = True
        state["circuit_breaker_reason"] = (
            f"Daily loss limit reached — fund down {abs(pnl_pct):.1f}% today"
        )
        alert = (
            f"⚠️ <b>Daily loss limit reached</b>\n\n"
            f"Fund is down {abs(pnl_pct):.1f}% today (${abs(pnl_usd):,.2f}).\n"
            f"No new trades until tomorrow 6am Auckland.\n\n"
            f"Existing trades remain open and monitored."
        )
        return state, alert

    weekly_open = float(state.get("weekly_opening_balance") or opening)
    if weekly_open > 0:
        weekly_pct = (current_balance - weekly_open) / weekly_open * 100
        state["weekly_loss_pct"] = round(weekly_pct, 2)

        if weekly_pct <= WEEKLY_LOSS_LIMIT_PCT and not state.get("observation_mode"):
            now = _auckland_now()
            days_until_monday = (7 - now.weekday()) % 7 or 7
            next_monday = (now + timedelta(days=days_until_monday)).replace(
                hour=6, minute=0, second=0, microsecond=0
            )
            state["observation_mode"]       = True
            state["observation_mode_until"] = next_monday.strftime("%Y-%m-%dT%H:%M:%S")
            obs_str = next_monday.strftime("%A %d %b at %I:%M%p")
            alert = (
                f"⚠️ <b>Weekly loss limit reached</b>\n\n"
                f"Fund is down {abs(weekly_pct):.1f}% this week.\n"
                f"Switching to observation mode — no new trades until "
                f"<b>{obs_str} Auckland</b>.\n\n"
                f"System continues scanning and sending watch list updates."
            )
            return state, alert

    return state, None


def record_missed_opportunity(state: dict, pair: str, direction: str,
                               confidence: float, checklist_score: float,
                               reason: str) -> dict:
    state = dict(state)
    now = _auckland_now()
    opps = list(state.get("missed_opportunities") or [])
    opps.append({
        "pair":            pair,
        "direction":       direction,
        "confidence":      round(float(confidence), 1),
        "checklist_score": round(float(checklist_score), 1),
        "blocked_reason":  reason,
        "timestamp":       now.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    state["missed_opportunities"] = opps[-100:]
    return state


def build_status_lines(state: dict) -> list:
    """Telegram-ready status lines for every scan."""
    blocked, _reason, btype = is_trading_blocked(state)

    if btype == "circuit_breaker":
        pnl_pct = state.get("daily_pnl_pct", 0)
        return [
            "⚠️ <b>CIRCUIT BREAKER ACTIVE</b>",
            f"Fund down {abs(pnl_pct):.1f}% today — no new trades",
            "Resets tomorrow at 6am Auckland",
        ]
    if btype == "daily_limit":
        return [
            f"\U0001f6ab Daily limit: {DAILY_TRADE_LIMIT}/{DAILY_TRADE_LIMIT} trades taken today",
            "Resets tomorrow at 6am Auckland",
        ]
    if btype == "pause":
        pause_until = state.get("pause_until", "")
        try:
            pu = datetime.strptime(pause_until[:19], "%Y-%m-%dT%H:%M:%S")
            pu_str = pu.strftime("%a %d %b %H:%M")
        except Exception:
            pu_str = str(pause_until)
        return [
            "⏸️ Paused — 3 consecutive losses",
            f"No new trades until {pu_str} Auckland",
        ]
    if btype == "observation_mode":
        obs_until = state.get("observation_mode_until", "")
        try:
            ou = datetime.strptime(obs_until[:19], "%Y-%m-%dT%H:%M:%S")
            ou_str = ou.strftime("%a %d %b %H:%M")
        except Exception:
            ou_str = str(obs_until)
        return [
            "\U0001f4ca Observation mode — weekly loss limit",
            f"No new trades until {ou_str} Auckland",
        ]
    count   = state.get("daily_trades_count", 0)
    pnl_pct = state.get("daily_pnl_pct", 0.0)
    pnl_usd = state.get("daily_pnl_dollars", 0.0)
    consec  = state.get("consecutive_losses", 0)
    pnl_sign = "+" if pnl_pct >= 0 else ""
    usd_sign = "+" if pnl_usd >= 0 else ""
    return [
        f"Trades today: {count}/{DAILY_TRADE_LIMIT}",
        f"Daily P&L: {usd_sign}${abs(pnl_usd):,.2f} ({pnl_sign}{pnl_pct:.2f}%)",
        f"Circuit breaker: inactive ✅",
        f"Consecutive losses: {consec}/{CONSECUTIVE_LOSS_LIMIT}",
    ]


def build_blocked_opportunities_section(state: dict) -> list:
    """Section for Monday learning report: blocked opportunities in last 7 days."""
    opps = state.get("missed_opportunities") or []
    now = _auckland_now()
    cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    recent = [o for o in opps if o.get("timestamp", "")[:10] >= cutoff]
    if not recent:
        return []

    by_reason: dict = {}
    for o in recent:
        r = o.get("blocked_reason", "unknown")
        by_reason[r] = by_reason.get(r, 0) + 1

    avg_conf = sum(o.get("confidence", 0) for o in recent) / len(recent)
    _label_map = {
        "daily_limit":       "Daily limit (3/day)",
        "circuit_breaker":   "Circuit breaker (2% daily loss)",
        "pause":             "3-loss pause (48h)",
        "observation_mode":  "Weekly loss observation mode",
        "currency_exposure": "Currency concentration (max 2)",
    }
    sec = [
        "", "\U0001f4ca <b>FUND PROTECTION — BLOCKED TRADES THIS WEEK</b>",
        f"Total blocked: {len(recent)} trade opportunity(s)",
    ]
    for reason, count in sorted(by_reason.items(), key=lambda x: -x[1]):
        label = _label_map.get(reason, reason)
        sec.append(f"  {label}: {count}")
    sec.append(f"Average confidence of blocked setups: {avg_conf:.1f}/10")
    sec.append("Review these to assess whether limits need adjusting")
    return sec
