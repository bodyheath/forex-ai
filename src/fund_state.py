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
    """Load fund_state.json, merging defaults for any missing keys.

    Creates the file with defaults if it does not yet exist so that
    ``git add data/`` always finds a committed version of the file.
    """
    try:
        if _FILE.exists():
            data = json.loads(_FILE.read_text(encoding="utf-8"))
            for k, v in _DEFAULTS.items():
                if k not in data:
                    data[k] = v
            return data
    except Exception:
        pass
    state = dict(_DEFAULTS)
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        _FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass
    return state


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
    # Recalculate sizing after clearing the circuit breaker — prevents stale
    # "drawdown_pause" mode persisting from a previous day's false alarm.
    state = update_sizing_state(state, bal)
    return state


def reset_if_new_week(state: dict, current_fund_balance: float | None = None) -> dict:
    """Reset weekly fields at Monday 6am Auckland."""
    now   = _auckland_now()
    today = now.strftime("%Y-%m-%d")
    if not (now.weekday() == 0 and now.hour >= 6):
        return state
    if state.get("weekly_start_date") == today:
        return state
    state = dict(state)
    state["weekly_loss_pct"]        = 0.0
    # Track the FUND balance as weekly baseline (not the broker account balance).
    # Priority: explicit fund balance > yesterday's daily_opening_balance > config fallback.
    fund_bal = (
        current_fund_balance
        or float(state.get("daily_opening_balance") or 0)
        or float(config.ACCOUNT_BALANCE)
    )
    state["weekly_opening_balance"] = fund_bal
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
    import sys as _sys_cb
    opening = float(state.get("daily_opening_balance") or float(config.ACCOUNT_BALANCE))
    if opening <= 0:
        return state, None

    pnl_pct = (current_balance - opening) / opening * 100
    pnl_usd = current_balance - opening

    # Sanity check: if the calculated loss exceeds 50% of the current fund balance,
    # the P&L calculation is almost certainly wrong (e.g. pips×lot_size passed instead
    # of dollars, or daily_opening_balance is stale/wrong).  Skip the circuit breaker
    # rather than fire a false alarm.
    if abs(pnl_usd) > current_balance * 0.5:
        print(
            f"[FUND_STATE] ERROR: Daily P&L calculation appears wrong — "
            f"${abs(pnl_usd):,.2f} loss on ${current_balance:,.2f} fund "
            f"(opening balance used: ${opening:,.2f}) — skipping circuit breaker",
            file=_sys_cb.stderr,
        )
        return state, None

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


def update_peak_and_drawdown(state: dict, current_balance: float) -> tuple:
    """Update peak_balance, current_drawdown_pct, max_drawdown_seen.

    Returns (updated_state, pause_alert_or_None, resume_alert_or_None).
    pause_alert fires when drawdown first hits 10%; resume_alert fires when it recovers below 9%.
    """
    state = dict(state)
    peak = float(state.get("peak_balance") or 0.0)
    if peak <= 0:
        peak = current_balance or float(config.ACCOUNT_BALANCE)
    peak = max(peak, current_balance)
    state["peak_balance"] = round(peak, 2)

    dd_pct = (peak - current_balance) / peak * 100 if peak > 0 else 0.0
    state["current_drawdown_pct"] = round(dd_pct, 2)
    max_dd = max(float(state.get("max_drawdown_seen") or 0.0), dd_pct)
    state["max_drawdown_seen"] = round(max_dd, 2)

    pause_alert = None
    resume_alert = None

    if dd_pct >= 10.0 and not state.get("drawdown_paused"):
        state["drawdown_paused"] = True
        loss_usd = peak - current_balance
        pause_alert = (
            f"🚨 <b>URGENT: Fund has reached 10% drawdown — trading paused</b>\n\n"
            f"${loss_usd:,.0f} loss ({dd_pct:.1f}%) from peak of ${peak:,.0f}.\n"
            f"All new fund trade entries paused automatically.\n"
            f"Existing trades remain open and monitored.\n"
            f"System will auto-resume when drawdown recovers below 9%."
        )
    elif dd_pct < 9.0 and state.get("drawdown_paused"):
        state["drawdown_paused"] = False
        resume_alert = (
            f"✅ <b>Fund recovering — drawdown now below 9%</b>\n\n"
            f"Current drawdown: {dd_pct:.1f}% from peak.\n"
            f"Resuming new trade entries at reduced 0.25% position size.\n"
            f"Monitoring closely for continued recovery."
        )

    return state, pause_alert, resume_alert


def compute_sizing(state: dict, current_balance: float,
                   checklist_score: float = 10.0) -> tuple:
    """Compute adaptive position size based on win streak and drawdown state.

    Returns (size_pct, mode, reason).
    size_pct is None when the trade is blocked by a drawdown tier quality requirement.

    Sizing table (drawdown from peak):
      0–3%:   1.0% base (win streak boost eligible)
      3–5%:   0.75%
      5–7%:   0.5% — requires confidence 8+
      7–10%:  0.25% — requires confidence 9+
      >=10%:  0.0 (trading paused)
    """
    drawdown  = float(state.get("current_drawdown_pct") or 0.0)
    paused    = state.get("drawdown_paused", False) or state.get("circuit_breaker_active", False)

    if paused or drawdown >= 10.0:
        return 0.0, "drawdown_pause", f"Drawdown {drawdown:.1f}% — trading paused"

    if drawdown >= 7.0:
        if checklist_score < 9.0:
            return None, "drawdown_blocked", f"Drawdown {drawdown:.1f}% from peak — requires confidence 9+"
        base = 0.25
        mode = "drawdown_protection"
        reason = f"Drawdown protection — fund {drawdown:.1f}% from peak (0.25% risk)"
    elif drawdown >= 5.0:
        if checklist_score < 8.0:
            return None, "drawdown_blocked", f"Drawdown {drawdown:.1f}% from peak — requires confidence 8+"
        base = 0.5
        mode = "drawdown_protection"
        reason = f"Drawdown protection — fund {drawdown:.1f}% from peak (0.5% risk)"
    elif drawdown >= 3.0:
        base = 0.75
        mode = "drawdown_caution"
        reason = f"Drawdown caution — fund {drawdown:.1f}% from peak (0.75% risk)"
    else:
        base = 1.0
        mode = "normal"
        reason = "Standard 1% risk"

    # Recovery boost: +0.05% per 1% recovered from max_drawdown, capped at 0.20%
    max_dd = float(state.get("max_drawdown_seen") or 0.0)
    if max_dd > drawdown and max_dd > 0.1:
        recovery_boost = min((max_dd - drawdown) * 0.05, 0.20)
        if recovery_boost >= 0.02:
            base = round(base + recovery_boost, 3)
            reason += f" (+{recovery_boost:.2f}% recovery)"

    # Win streak boost — only when all conditions are met
    wins          = int(state.get("consecutive_wins") or 0)
    circuit_break = state.get("circuit_breaker_active", False)
    fund_ok       = current_balance >= 10200.0

    can_boost = (
        drawdown < 3.0 and
        fund_ok and
        not circuit_break and
        not paused and
        not state.get("pause_until")
    )

    if can_boost and wins >= 3:
        if wins >= 7:
            boost = 0.35
        elif wins >= 5:
            boost = 0.20
        else:
            boost = 0.10
        base  = base + boost
        mode  = "win_streak"
        reason = f"{wins} consecutive wins — streak bonus +{boost:.2f}%"

    return min(round(base, 3), 1.5), mode, reason


def update_sizing_state(state: dict, current_balance: float) -> dict:
    """Refresh current_sizing_pct and sizing_mode for display (uses score=10 as baseline)."""
    pct, mode, reason = compute_sizing(state, current_balance, checklist_score=10.0)
    state = dict(state)
    state["current_sizing_pct"] = float(pct) if pct is not None else 0.0
    state["sizing_mode"]        = mode
    state["sizing_reason"]      = reason
    return state


def check_weekend_alert(state: dict, open_trades: list) -> tuple:
    """Return (should_send, alert_msg). Fires once per Friday 2–4pm Auckland."""
    if not open_trades:
        return False, ""
    now = _auckland_now()
    if now.weekday() != 4:        # not Friday
        return False, ""
    if not (14 <= now.hour < 16): # not 2pm–4pm Auckland
        return False, ""
    today = now.strftime("%Y-%m-%d")
    if state.get("weekend_alert_sent_date") == today:
        return False, ""

    trade_lines = []
    for t in open_trades[:5]:
        pair      = t.get("pair", "?")
        direction = (t.get("direction") or "?").upper()
        trade_lines.append(f"• {pair} {direction}")

    msg = (
        f"💡 <b>Weekend approaching — markets close in approximately 2 hours</b>\n\n"
        f"Open fund trades:\n" + "\n".join(trade_lines) + "\n\n"
        f"Consider reducing positions by 50% before market close to protect against "
        f"weekend gap risk — full positions can be restored Monday if setups remain valid."
    )
    return True, msg


def build_sizing_status_lines(state: dict) -> list:
    """Telegram-ready position sizing status for every scan."""
    drawdown  = float(state.get("current_drawdown_pct") or 0.0)
    wins      = int(state.get("consecutive_wins") or 0)
    mode      = str(state.get("sizing_mode") or "normal")
    pct       = float(state.get("current_sizing_pct") or 1.0)
    cur_bal   = float(state.get("daily_opening_balance") or float(config.ACCOUNT_BALANCE))
    fund_ok   = cur_bal >= 10200.0
    dd_icon   = "✅" if drawdown < 3.0 else ("⚠️" if drawdown < 7.0 else "🚨")

    if state.get("drawdown_paused"):
        return [
            "\U0001f4ca <b>Position sizing:</b>",
            "Mode: 🚨 PAUSED — drawdown >= 10%",
            f"Drawdown from peak: {drawdown:.1f}% 🚨",
            "All new fund trade entries suspended — monitoring recovery",
        ]

    if mode == "win_streak":
        return [
            "\U0001f4ca <b>Position sizing:</b>",
            f"Mode: ⚡ Win streak boost ({pct:.2f}% risk per trade)",
            f"{wins} consecutive wins — streak bonus active",
            f"Drawdown from peak: {drawdown:.1f}% — within normal range ✅",
        ]

    if mode in ("drawdown_protection", "drawdown_caution"):
        min_conf = 9 if drawdown >= 7.0 else (8 if drawdown >= 5.0 else None)
        lines = [
            "\U0001f4ca <b>Position sizing:</b>",
            f"Mode: 🛡️ Drawdown protection ({pct:.2f}% risk per trade)",
            f"Drawdown from peak: {drawdown:.1f}% {dd_icon}",
        ]
        if min_conf:
            lines.append(f"Minimum confidence for new trades: {min_conf}/10")
        return lines

    # Normal mode
    needs     = max(0, 3 - wins)
    fund_str  = "yes ✅" if fund_ok else "no ❌ (need $10,200)"
    lines = [
        "\U0001f4ca <b>Position sizing:</b>",
        f"Mode: Normal ({pct:.2f}% risk per trade)",
        f"Drawdown from peak: {drawdown:.1f}% ✅",
        f"Consecutive wins: {wins}" + (
            f" (need {needs} more for first boost)" if needs else " — ⚡ streak boost eligible"
        ),
        f"Fund above boost threshold: {fund_str}",
    ]
    return lines


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
