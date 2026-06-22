"""Discord notification layer — secondary channel alongside Telegram.

Graceful degradation: missing webhook URLs or Discord errors are logged
and ignored.  The scan is never slowed, Telegram is never blocked.
"""
import os
import time as _time_mod
from datetime import datetime, timezone

import requests

WEBHOOK_FUND     = os.getenv("DISCORD_WEBHOOK_FUND")
WEBHOOK_RESEARCH = os.getenv("DISCORD_WEBHOOK_RESEARCH")
WEBHOOK_MONITOR  = os.getenv("DISCORD_WEBHOOK_MONITOR")
WEBHOOK_HEALTH   = os.getenv("DISCORD_WEBHOOK_HEALTH")
WEBHOOK_CRITICAL = os.getenv("DISCORD_WEBHOOK_CRITICAL")

COLOR_FUND_WIN       = 0x00FF88
COLOR_FUND_LOSS      = 0xFF3333
COLOR_FUND_PROTECTED = 0x00AA44
COLOR_FUND_NEW       = 0x0099FF
COLOR_FUND_HOT       = 0xFF8800
COLOR_RESEARCH_WIN   = 0x27AE60
COLOR_RESEARCH_LOSS  = 0xC0392B
COLOR_RESEARCH_BATCH = 0x3498DB
COLOR_RESEARCH_HOT   = 0xE67E22
COLOR_HEALTH         = 0x9B59B6
COLOR_CRITICAL       = 0xFF0000
COLOR_WARNING        = 0xF39C12
COLOR_INFO           = 0x95A5A6

# Legacy aliases so existing callers keep working
COLOR_WIN        = COLOR_FUND_WIN
COLOR_LOSS       = COLOR_FUND_LOSS
COLOR_APPROACHING = COLOR_FUND_HOT
COLOR_RESEARCH   = COLOR_RESEARCH_BATCH


def _progress_bar(current_pct, width=20):
    if current_pct > 100:
        return "█" * width  # full bar — target already crossed
    capped = max(0, min(current_pct, 100))
    filled = int((capped / 100) * width)
    return "█" * filled + "░" * (width - filled)


def _price_position_bar(entry, current, stop, target, direction="BUY"):
    try:
        if direction == "BUY":
            total_range = target - stop
            if total_range <= 0:
                return ""
            current_pos = (current - stop) / total_range
            entry_pos   = (entry - stop)   / total_range
        else:
            total_range = stop - target
            if total_range <= 0:
                return ""
            current_pos = (stop - current) / total_range
            entry_pos   = (stop - entry)   / total_range

        width = 24
        bar   = list("░" * width)

        entry_idx   = int(max(0, min(1, entry_pos))   * (width - 1))
        current_idx = int(max(0, min(1, current_pos)) * (width - 1))

        for i in range(min(current_idx, width)):
            bar[i] = "█"

        if 0 <= entry_idx < width:
            bar[entry_idx] = "◆"
        if 0 <= current_idx < width:
            bar[current_idx] = "▲"

        bar_str = "".join(bar)

        if direction == "BUY":
            return (
                f"`SL {stop:.5f} |{bar_str}| T {target:.5f}`\n"
                f"`{'':12}▲ Now: {current:.5f}`"
            )
        else:
            return (
                f"`T {target:.5f} |{bar_str}| SL {stop:.5f}`\n"
                f"`{'':12}▲ Now: {current:.5f}`"
            )
    except Exception:
        return ""


def _cascade_progress_bar(t1_hit, t2_hit, t3_hit):
    t1 = "\U0001f7e2" if t1_hit else "⬜"
    t2 = "\U0001f7e2" if t2_hit else "⬜"
    t3 = "\U0001f7e2" if t3_hit else "⬜"
    t1_pct = "40%" if t1_hit else "░░"
    t2_pct = "30%" if t2_hit else "░░"
    t3_pct = "30%" if t3_hit else "░░"
    return f"{t1} T1 ({t1_pct})  {t2} T2 ({t2_pct})  {t3} T3 ({t3_pct})"


def _get_tradingview_url(pair):
    symbol = pair.replace("/", "")
    return f"https://www.tradingview.com/chart/?symbol=FX:{symbol}"


def _trade_type_label(is_fund):
    if is_fund:
        return "\U0001f4bc **FUND TRADE** — real money position"
    return "\U0001f52c **RESEARCH TRADE** — ML training position"


def _send_embed(webhook_url, title, description, color, fields=None):
    if not webhook_url:
        return False
    embed = {
        "title":       title,
        "description": description,
        "color":       color,
        "fields":      fields or [],
        "footer":      {"text": "Forex AI System"},
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }
    for attempt in range(3):
        try:
            r = requests.post(
                webhook_url,
                json={"embeds": [embed]},
                timeout=10,
            )
            if r.status_code == 204:
                return True
            elif r.status_code == 429:
                _time_mod.sleep(5)
        except Exception:
            _time_mod.sleep(2)
    return False


def send_fund_trade_opened(pair, direction, conf, entry, stop, t1, t2, t3,
                            risk_pct, risk_dollars, rr, checklist_score,
                            kill_zone="", regime="", rsi=0, atr=0,
                            monthly_trend="", hhhl="", ccy_strength="", adx=0):
    pip_sz    = 0.01 if "JPY" in pair else 0.0001
    stop_pips = abs(entry - stop) / pip_sz
    t1_pips   = abs(t1 - entry)   / pip_sz
    t2_pips   = abs(t2 - entry)   / pip_sz
    t3_pips   = abs(t3 - entry)   / pip_sz
    tv_url    = _get_tradingview_url(pair)
    dir_emoji = "\U0001f4c8" if direction == "BUY" else "\U0001f4c9"

    fields = [
        {"name": "Trade Type",          "value": _trade_type_label(True),             "inline": False},
        {"name": f"{dir_emoji} Entry",  "value": f"`{entry:.5f}`",                    "inline": True},
        {"name": "\U0001f6d1 Stop Loss","value": f"`{stop:.5f}` ({stop_pips:.1f}p)",  "inline": True},
        {"name": "\U0001f4ca R:R Ratio","value": f"`{rr}:1`",                         "inline": True},
        {"name": "\U0001f3af T1 (40%)", "value": f"`{t1:.5f}` +{t1_pips:.1f}p",      "inline": True},
        {"name": "\U0001f3af T2 (30%)", "value": f"`{t2:.5f}` +{t2_pips:.1f}p",      "inline": True},
        {"name": "\U0001f3af T3 (30%)", "value": f"`{t3:.5f}` +{t3_pips:.1f}p",      "inline": True},
        {"name": "\U0001f4b0 Position Size", "value": f"{risk_pct}% of fund\n${risk_dollars:.2f} at risk", "inline": True},
        {"name": "\U0001f916 Confidence",    "value": f"{conf}/10\nChecklist: {checklist_score}/10",       "inline": True},
        {"name": "\U0001f550 Session",       "value": kill_zone or "Any",             "inline": True},
        {"name": "\U0001f4c8 Signal Filters","value": (
            f"{'✅' if monthly_trend else '⬜'} Monthly trend\n"
            f"{'✅' if hhhl else '⬜'} HHHL structure\n"
            f"{'✅' if kill_zone else '⬜'} Kill zone\n"
            f"{'✅' if ccy_strength else '⬜'} Currency strength\n"
            f"ADX: {adx:.0f} · RSI: {rsi:.1f}"
        ), "inline": True},
        {"name": "\U0001f30d Market Regime", "value": regime or "Unknown",            "inline": True},
        {"name": "\U0001f4ca Price Position","value": _price_position_bar(entry, entry, stop, t1, direction) or "Chart below", "inline": False},
        {"name": "\U0001f517 Chart",         "value": f"[View {pair} on TradingView]({tv_url})", "inline": False},
    ]
    return _send_embed(
        WEBHOOK_FUND,
        f"\U0001f4bc NEW FUND TRADE — {pair} {dir_emoji} {direction}",
        f"New position opened at {entry:.5f}",
        COLOR_FUND_NEW,
        fields=fields,
    )


def send_fund_milestone(pair, direction, milestone, pips, entry, current, stop,
                         t1=None, t2=None, t3=None,
                         t1_hit=False, t2_hit=False, t3_hit=False, dollars=0):
    tv_url        = _get_tradingview_url(pair)
    dir_emoji     = "\U0001f4c8" if direction == "BUY" else "\U0001f4c9"
    milestone_num = int(milestone[1]) if len(milestone) > 1 and milestone[1].isdigit() else 1
    pct_banked    = 40 if milestone_num == 1 else (70 if milestone_num == 2 else 100)
    next_tgt_val  = t2 or t1 or current

    fields = [
        {"name": "Trade Type",              "value": _trade_type_label(True),                                      "inline": False},
        {"name": "✅ Milestone Hit",         "value": f"**{milestone}** at `{current:.5f}`",                       "inline": True},
        {"name": "\U0001f4b0 Pips Captured","value": f"**+{pips:.1f} pips**",                                     "inline": True},
        {"name": "\U0001f4b5 Profit",        "value": f"**+${dollars:.2f}**" if dollars else "Calculating...",    "inline": True},
        {"name": "\U0001f6e1️ Stop Protection", "value": f"Stop moved to BREAKEVEN\n`{entry:.5f}` — no loss possible", "inline": True},
        {"name": "\U0001f4ca Cascade Progress","value": _cascade_progress_bar(t1_hit, t2_hit, t3_hit),            "inline": False},
        {"name": "\U0001f4c8 Price Position","value": _price_position_bar(entry, current, stop, next_tgt_val, direction) or "", "inline": False},
        {"name": "\U0001f4ca Total Banked",  "value": f"**{pct_banked}%** of position secured",                   "inline": True},
        {"name": "⏭️ Next Target", "value": (f"T{milestone_num+1} at `{t2:.5f}`" if milestone_num == 1 and t2 else "Final 30% running"), "inline": True},
        {"name": "\U0001f517 Chart",         "value": f"[View {pair} on TradingView]({tv_url})",                  "inline": False},
    ]
    return _send_embed(
        WEBHOOK_FUND,
        f"\U0001f3af {pair} {dir_emoji} — {milestone} HIT ✅",
        "\U0001f4bc FUND TRADE — Partial profit locked in",
        COLOR_FUND_WIN,
        fields=fields,
    )


def send_fund_stop_hit(pair, direction, pips, was_cascade_protected,
                        entry=0.0, stop=0.0, exit_price=0.0,
                        t1_pips=0, dollars=0, consecutive_losses=0,
                        pattern_learned=""):
    tv_url    = _get_tradingview_url(pair)
    dir_emoji = "\U0001f4c8" if direction == "BUY" else "\U0001f4c9"

    if was_cascade_protected:
        fields = [
            {"name": "Trade Type",                   "value": _trade_type_label(True),           "inline": False},
            {"name": "\U0001f6e1️ Cascade Protected", "value": "T1 was hit — stop at breakeven", "inline": False},
            {"name": "✅ T1 Profit (40%)",            "value": f"+{t1_pips:.1f}p",               "inline": True},
            {"name": "\U0001f512 Remainder",          "value": "Closed at breakeven",            "inline": True},
            {"name": "\U0001f4b5 Net Result",         "value": f"+${dollars:.2f} profit" if dollars > 0 else "Breakeven", "inline": True},
        ]
        return _send_embed(
            WEBHOOK_FUND,
            f"\U0001f6e1️ {pair} {dir_emoji} — Stop Hit — PROTECTED",
            "\U0001f4bc FUND TRADE — Cascade protection saved this trade",
            COLOR_FUND_PROTECTED,
            fields=fields,
        )
    else:
        fields = [
            {"name": "Trade Type",                "value": _trade_type_label(True),                             "inline": False},
            {"name": "\U0001f4cd Entry",           "value": f"`{entry:.5f}`" if entry else "—",            "inline": True},
            {"name": "\U0001f6d1 Stop Hit",        "value": f"`{exit_price:.5f}`" if exit_price else "—",  "inline": True},
            {"name": "\U0001f4c9 Loss",            "value": f"-{pips:.1f}p / -${dollars:.2f}",                 "inline": True},
            {"name": "\U0001f522 Consecutive Losses","value": f"{consecutive_losses}/3",                        "inline": True},
            {"name": "\U0001f9e0 Pattern Learned", "value": pattern_learned or "Logged for ML",                 "inline": False},
        ]
        return _send_embed(
            WEBHOOK_FUND,
            f"❌ {pair} {dir_emoji} — STOP HIT — LOSS",
            "\U0001f4bc FUND TRADE — Loss recorded and logged",
            COLOR_FUND_LOSS,
            fields=fields,
        )


def send_fund_approaching(pair, direction, progress_pct, target_price,
                           current_price, distance_pips, stop_price, milestone,
                           entry_price=0, is_fund=True):
    tv_url    = _get_tradingview_url(pair)
    dir_emoji = "\U0001f4c8" if direction == "BUY" else "\U0001f4c9"
    color     = COLOR_FUND_HOT if is_fund else COLOR_RESEARCH_HOT

    # Target already crossed — price went past the milestone level
    if progress_pct > 100:
        overshoot_pips = abs(distance_pips)
        fields = [
            {"name": "Trade Type",               "value": _trade_type_label(is_fund),                               "inline": False},
            {"name": "\U0001f4ca Status",        "value": f"`{'█' * 20}` 100%+ (target crossed)",                  "inline": False},
            {"name": "\U0001f3af Target Was",    "value": f"`{target_price:.5f}`",                                  "inline": True},
            {"name": "\U0001f4cd Current Price", "value": f"`{current_price:.5f}`",                                 "inline": True},
            {"name": "\U0001f4cf Past Target",   "value": f"`{overshoot_pips:.1f} pips beyond {milestone}`",       "inline": True},
            {"name": "\U0001f6e1️ Stop",          "value": f"`{stop_price:.5f}` Protected",                        "inline": True},
            {"name": "⚡ Action",                "value": "Milestone detection triggered immediately\nWill be recorded this run", "inline": False},
            {"name": "\U0001f517 Chart",         "value": f"[View {pair} on TradingView]({tv_url})",               "inline": False},
        ]
        return _send_embed(
            WEBHOOK_MONITOR,
            f"\U0001f3af {pair} {dir_emoji} — {milestone} ALREADY CROSSED",
            f"{'💼 FUND' if is_fund else '🔬 RESEARCH'} — Price past target — recording milestone now",
            color,
            fields=fields,
        )

    bar = _progress_bar(progress_pct)

    fields = [
        {"name": "Trade Type",                 "value": _trade_type_label(is_fund),                           "inline": False},
        {"name": "\U0001f4ca Progress to Target","value": f"`{bar}` {max(progress_pct, 0):.0f}%",             "inline": False},
        {"name": "\U0001f3af Target",           "value": f"`{target_price:.5f}`",                             "inline": True},
        {"name": "\U0001f4cd Current",          "value": f"`{current_price:.5f}`",                            "inline": True},
        {"name": "\U0001f4cf Distance",         "value": f"`{distance_pips:.1f} pips`",                       "inline": True},
        {"name": "\U0001f6d1 Stop Loss",        "value": f"`{stop_price:.5f}` \U0001f6e1️ Protected",         "inline": True},
    ]

    if entry_price:
        price_bar = _price_position_bar(entry_price, current_price, stop_price, target_price, direction)
        if price_bar:
            fields.append({"name": "\U0001f4c8 Price Position", "value": price_bar, "inline": False})

    fields.append({"name": "\U0001f517 Chart", "value": f"[View {pair} on TradingView]({tv_url})", "inline": False})

    return _send_embed(
        WEBHOOK_MONITOR,
        f"\U0001f525 {pair} {dir_emoji} — Approaching {milestone}",
        f"{'💼 FUND' if is_fund else '🔬 RESEARCH'} — Monitor checking every 30 minutes ✅",
        color,
        fields=fields,
    )


def send_research_batch(milestones_list, scan_mode, total_open=0, win_rate=0, decisive=0):
    if not milestones_list:
        return False

    wins   = [m for m in milestones_list if "WIN" in m.get("outcome", "") or "T1" in m.get("milestone", "") or "T2" in m.get("milestone", "")]
    losses = [m for m in milestones_list if "LOSS" in m.get("outcome", "")]

    fields = []
    for m in milestones_list[:10]:
        pair      = m.get("pair", "")
        milestone = m.get("milestone", "")
        pips      = m.get("pips", 0)
        outcome   = m.get("outcome", "")

        if "LOSS" in outcome:
            emoji = "❌"
            value = f"-{abs(pips):.1f}p — LOSS"
        elif "protected" in outcome.lower() or "cascade" in outcome.lower():
            emoji = "\U0001f6e1️"
            value = f"+{pips:.1f}p — Protected exit"
        elif "T3" in milestone or "FULL" in outcome:
            emoji = "\U0001f3c6"
            value = f"+{pips:.1f}p — FULL WIN"
        elif "T2" in milestone:
            emoji = "\U0001f3af"
            value = f"+{pips:.1f}p — T2 hit (70% banked)"
        else:
            emoji = "✅"
            value = f"+{pips:.1f}p — Partial WIN"

        fields.append({"name": f"{emoji} {pair} — {milestone}", "value": value, "inline": True})

    if len(milestones_list) > 10:
        fields.append({"name": f"And {len(milestones_list) - 10} more...", "value": "See full log on GitHub", "inline": False})

    fields.append({
        "name":   "\U0001f4ca Research Stats",
        "value":  f"Open trades: {total_open}\nWin rate: {win_rate:.0f}%\nDecisive outcomes: {decisive}",
        "inline": True,
    })
    fields.append({"name": "\U0001f9e0 ML Update", "value": "Training data updated ✅", "inline": True})

    return _send_embed(
        WEBHOOK_RESEARCH,
        f"\U0001f52c Research Milestones — {scan_mode} check · {len(milestones_list)} detected",
        f"\U0001f52c **RESEARCH TRADES** — ML training positions\n✅ {len(wins)} wins · ❌ {len(losses)} losses",
        COLOR_RESEARCH_BATCH,
        fields=fields,
    )


def send_full_scan_report(date, scan_mode, universe_size, pairs_analysed,
                           new_alerts, threshold, regime,
                           open_fund_trades, research_open,
                           win_rate, profit_factor=0.0,
                           cost_usd=0.0, run_minutes=0.0,
                           data_quality_pct=0, cot_status="",
                           calendar_status="", api_calls_used=0,
                           ml_status=""):
    scan_emoji = {"full": "\U0001f305", "morning": "☀️", "prelondon": "\U0001f306", "preny": "\U0001f303", "monitor": "\U0001f4e1"}.get(scan_mode, "\U0001f916")
    scan_name  = {"full": "6am Full Scan", "morning": "9am Morning Scan", "prelondon": "5pm Pre-London", "preny": "11pm Pre-New York"}.get(scan_mode, scan_mode)

    # Accept new_alerts as a list of dicts or a plain integer count
    if isinstance(new_alerts, (int, float)):
        alerts_text      = f"{int(new_alerts)} new setups" if new_alerts else "No new setups today"
        new_alerts_count = int(new_alerts)
    elif new_alerts:
        new_alerts_count = len(new_alerts)
        lines = []
        for alert in (new_alerts if isinstance(new_alerts, list) else [])[:5]:
            if isinstance(alert, dict):
                p = alert.get("pair", "")
                d = alert.get("direction", "")
                c = alert.get("conf", "")
                lines.append(f"\U0001f4bc {p} {d} (conf {c}/10)")
            else:
                lines.append(str(alert))
        alerts_text = "\n".join(lines) if lines else "No new setups today"
    else:
        alerts_text      = "No new setups today"
        new_alerts_count = 0

    color = COLOR_FUND_WIN if new_alerts_count else COLOR_INFO

    # Accept open_fund_trades as a list of dicts or a plain integer count
    if isinstance(open_fund_trades, (int, float)):
        open_trades_text = f"{int(open_fund_trades)} open fund trades" if open_fund_trades else "No open fund trades"
    elif open_fund_trades:
        lines = []
        for trade in (open_fund_trades if isinstance(open_fund_trades, list) else [])[:6]:
            if isinstance(trade, dict):
                p  = trade.get("pair", "")
                d  = trade.get("direction", "")
                pg = trade.get("progress_pct", 0)
                nt = trade.get("next_target", "T1")
                lines.append(f"{'📈' if d == 'BUY' else '📉'} {p}: {pg:.0f}% to {nt}")
            else:
                lines.append(str(trade))
        open_trades_text = "\n".join(lines) if lines else "No open fund trades"
    else:
        open_trades_text = "No open fund trades"

    wr_str = f"{win_rate:.0f}%" if win_rate is not None else "n/a"
    pf_str = f"{profit_factor:.2f}" if profit_factor else "n/a"

    fields = [
        {"name": "\U0001f4ca Scan Summary",    "value": f"Universe: {universe_size:,} pairs\nDeep analysed: {pairs_analysed}\nDuration: {run_minutes:.0f} minutes", "inline": True},
        {"name": "\U0001f3af Entry Threshold", "value": f"{threshold}/10\nRegime: {regime}",     "inline": True},
        {"name": "\U0001f4bc New Alerts",      "value": alerts_text,                             "inline": False},
        {"name": "\U0001f4c8 Open Fund Trades","value": open_trades_text,                        "inline": False},
        {"name": "\U0001f52c Research",        "value": f"Open: {research_open}\nWin rate: {wr_str}\nProfit factor: {pf_str}", "inline": True},
        {"name": "\U0001f4e1 Data Quality",    "value": f"Overall: {data_quality_pct:.0f}%\nCOT: {cot_status or 'N/A'}\nCalendar: {calendar_status or 'N/A'}\nAPI: {api_calls_used}/800 calls", "inline": True},
        {"name": "\U0001f916 ML System",       "value": ml_status or "Active",                  "inline": True},
        {"name": "\U0001f4b0 Cost",            "value": f"${cost_usd:.4f} USD",                  "inline": True},
    ]
    return _send_embed(
        WEBHOOK_HEALTH,
        f"{scan_emoji} Forex AI — {date} — {scan_name}",
        f"Scan complete · {pairs_analysed} pairs analysed",
        color,
        fields=fields,
    )


def send_system_health(date, scans_completed, scans_expected, monitor_runs,
                        monitor_expected, milestones_today, data_quality_pct,
                        api_calls_used, api_calls_remaining, ml_status, cot_status,
                        last_monitor_gap_min, fund_balance=0, return_pct=0,
                        open_fund=0, open_research=0, daily_pnl=0, drawdown_pct=0,
                        win_rate=0, decisive=0, sharpe=0, ftmo_pct=0):
    scans_ok   = scans_completed >= scans_expected
    monitor_ok = monitor_runs >= monitor_expected - 2
    fields = [
        {"name": "\U0001f4c5 Scans Today",    "value": f"{'✅' if scans_ok else '⚠️'} {scans_completed}/{scans_expected} completed",                                       "inline": True},
        {"name": "\U0001f4e1 Monitor Runs",   "value": f"{'✅' if monitor_ok else '⚠️'} {monitor_runs}/{monitor_expected} runs\nLast: {last_monitor_gap_min:.0f}m ago",    "inline": True},
        {"name": "\U0001f3af Milestones",     "value": f"{milestones_today} detected today",                                                                               "inline": True},
        {"name": "\U0001f4bc Fund Performance","value": f"Balance: ${fund_balance:,.2f}\nReturn: {return_pct:+.2f}%\nDaily P&L: {daily_pnl:+.2f}%\nDrawdown: {drawdown_pct:.2f}%", "inline": True},
        {"name": "\U0001f4ca Trade Stats",    "value": f"Open fund: {open_fund}\nOpen research: {open_research}\nWin rate: {win_rate:.0f}%\nDecisive: {decisive}",        "inline": True},
        {"name": "\U0001f3c6 FTMO Progress",  "value": f"Target: 10% profit\nCurrent: {ftmo_pct:+.2f}%\nSharpe: {sharpe:.2f}",                                           "inline": True},
        {"name": "\U0001f4e1 Data Sources",   "value": f"Data quality: {data_quality_pct:.0f}%\nCOT: {cot_status}\nAPI: {api_calls_used}/800 calls\nRemaining: {api_calls_remaining}", "inline": True},
        {"name": "\U0001f916 ML System",      "value": ml_status or "Active",                                                                                             "inline": True},
        {"name": "✅ System Status",          "value": "All systems operational" if (scans_ok and monitor_ok) else "⚠️ Check logs",                                       "inline": True},
    ]
    return _send_embed(
        WEBHOOK_HEALTH,
        f"\U0001f4ca Daily System Health — {date}",
        "Forex AI autonomous system — end of day summary",
        COLOR_HEALTH,
        fields=fields,
    )


def send_circuit_breaker(reason, fund_balance, daily_pnl_pct,
                          daily_pnl_dollars, resets_at=""):
    fields = [
        {"name": "\U0001f6a8 Reason",       "value": reason,                                                        "inline": False},
        {"name": "\U0001f4b0 Fund Balance", "value": f"${fund_balance:,.2f}",                                       "inline": True},
        {"name": "\U0001f4c9 Daily P&L",    "value": f"{daily_pnl_pct:+.2f}% (${daily_pnl_dollars:+.2f})",         "inline": True},
        {"name": "⏰ Resets At",            "value": resets_at or "Tomorrow 6am Auckland",                         "inline": True},
        {"name": "ℹ️ Action Taken",         "value": "No new fund trades until reset\nExisting trades remain open and monitored", "inline": False},
    ]
    return _send_embed(
        WEBHOOK_CRITICAL,
        "⚠️ Circuit Breaker ACTIVE",
        "\U0001f4bc FUND PROTECTION — Automatic safety system triggered",
        COLOR_CRITICAL,
        fields=fields,
    )


def send_workflow_failure(workflow_name, run_url, scan_mode=""):
    fields = [
        {"name": "\U0001f534 Workflow",      "value": workflow_name,                       "inline": True},
        {"name": "\U0001f4cb Scan Mode",     "value": scan_mode or "Unknown",              "inline": True},
        {"name": "\U0001f517 View Run",      "value": f"[Click to view error]({run_url})", "inline": False},
        {"name": "ℹ️ Action Required",       "value": "Check GitHub Actions for error details\nNext scheduled run will attempt automatically", "inline": False},
    ]
    return _send_embed(
        WEBHOOK_CRITICAL,
        f"\U0001f6a8 SYSTEM ALERT — {workflow_name} FAILED",
        "A scheduled workflow did not complete successfully",
        COLOR_CRITICAL,
        fields=fields,
    )


def send_monitor_gap_alert(gap_minutes, last_run_time=""):
    fields = [
        {"name": "⏰ Gap Duration", "value": f"{gap_minutes:.0f} minutes", "inline": True},
        {"name": "✅ Expected",     "value": "Every 30 minutes",           "inline": True},
        {"name": "\U0001f550 Last Run", "value": last_run_time or "Unknown", "inline": True},
        {"name": "\U0001f527 Action",   "value": "Check cron-job.org dashboard\nVerify monitor job is active", "inline": False},
    ]
    return _send_embed(
        WEBHOOK_CRITICAL,
        "⚠️ Monitor Gap Detected",
        "Between-scan monitor has not run as expected",
        COLOR_WARNING,
        fields=fields,
    )


def send_watch_list_movement(pair, confidence, direction, pips_moved, atr_multiple=0):
    tv_url    = _get_tradingview_url(pair)
    dir_emoji = "\U0001f4c8" if direction == "BUY" else "\U0001f4c9"
    mov_text  = f"{pips_moved:.1f} pips\n{atr_multiple:.2f}x ATR" if atr_multiple else f"{pips_moved:.1f} pips"
    fields = [
        {"name": "\U0001f4ca Confidence",      "value": f"{confidence}/10",  "inline": True},
        {"name": f"{dir_emoji} Direction",     "value": direction,            "inline": True},
        {"name": "\U0001f4cf Movement",        "value": mov_text,             "inline": True},
        {"name": "⏰ Next Scan",               "value": "Added to priority queue\nWill be deep-analysed at next 6am scan", "inline": False},
        {"name": "\U0001f517 Chart",           "value": f"[View {pair} on TradingView]({tv_url})", "inline": False},
    ]
    return _send_embed(
        WEBHOOK_MONITOR,
        f"\U0001f514 Watch List Movement — {pair} {dir_emoji}",
        "Significant price movement detected on watch list pair",
        COLOR_WARNING,
        fields=fields,
    )


def send_master_scan_report(
    scan_mode, date, auckland_time,
    threshold, regime, vix,
    fund_balance, fund_return_pct,
    fund_peak, drawdown_pct,
    daily_pnl_pct, daily_pnl_dollars,
    consecutive_wins, consecutive_losses,
    sizing_mode, risk_pct,
    open_fund_trades,
    new_fund_alerts,
    newly_closed_trades,
    research_open, research_closed,
    research_decisive, research_win_rate,
    research_profit_factor,
    research_avg_win_pips, research_avg_loss_pips,
    research_best_trade, research_worst_trade,
    recent_research_milestones,
    universe_size, pairs_analysed,
    watch_list_pairs,
    approaching_signals,
    data_quality_pct, td_calls_used,
    cot_status, calendar_status,
    fred_status, yahoo_status,
    ml_gate_status, ml_auc,
    ml_decisive_count, online_model_updates,
    ftmo_target_pct, ftmo_current_pct,
    ftmo_daily_limit_pct, sharpe_ratio,
    last_monitor_gap_min, scan_minutes,
    scan_cost_usd, monitor_sources,
):
    scan_emoji = {
        "full": "\U0001f305", "morning": "☀️",
        "prelondon": "\U0001f306", "preny": "\U0001f303",
    }.get(scan_mode, "\U0001f916")
    scan_name = {
        "full": "6am Full Scan", "morning": "9am Morning Scan",
        "prelondon": "5pm Pre-London", "preny": "11pm Pre-New York",
    }.get(scan_mode, scan_mode)

    # Color driven by fund daily P&L
    if daily_pnl_pct > 1.0:
        color = 0x00FF88
    elif daily_pnl_pct >= 0:
        color = 0x27AE60
    elif daily_pnl_pct > -1.0:
        color = 0xF39C12
    else:
        color = 0xFF3333

    fields = []

    # ── SECTION 1: FUND PERFORMANCE ─────────────────────────────────────────────
    fund_emoji   = "\U0001f4c8" if daily_pnl_pct >= 0 else "\U0001f4c9"
    dd_emoji     = "✅" if drawdown_pct < 3 else ("⚠️" if drawdown_pct < 7 else "\U0001f6a8")
    fields.append({
        "name":  "\U0001f4b0 Fund Performance",
        "value": (
            f"Balance: **${fund_balance:,.2f}** ({fund_return_pct:+.2f}% today)\n"
            f"{fund_emoji} Daily P&L: {daily_pnl_pct:+.2f}% (${daily_pnl_dollars:+.2f})\n"
            f"{dd_emoji} Drawdown: {drawdown_pct:.2f}% from peak (${fund_peak:,.2f})\n"
            f"Sizing: {sizing_mode} ({risk_pct:.2f}% per trade)"
        ),
        "inline": False,
    })

    # ── SECTION 2: FTMO PROGRESS ────────────────────────────────────────────────
    _ftmo_pct_of_target = (ftmo_current_pct / ftmo_target_pct * 100) if ftmo_target_pct > 0 else 0
    ftmo_bar = _progress_bar(_ftmo_pct_of_target, width=15)
    streak_txt = (
        f"\U0001f525 {consecutive_wins} wins" if consecutive_wins > 0
        else f"❄️ {consecutive_losses} losses"
    )
    fields.append({
        "name":  "\U0001f3c6 FTMO Progress",
        "value": (
            f"`{ftmo_bar}` {ftmo_current_pct:+.2f}% / {ftmo_target_pct}%\n"
            f"Daily limit: {ftmo_daily_limit_pct}% | Sharpe: {sharpe_ratio:.2f}\n"
            f"Streak: {streak_txt}"
        ),
        "inline": False,
    })

    # ── SECTION 3: OPEN FUND TRADES ─────────────────────────────────────────────
    if open_fund_trades:
        trades_text = ""
        for t in open_fund_trades:
            _pair_t    = t.get("pair", "")
            _dir_t     = t.get("direction", "")
            _de_t      = "\U0001f4c8" if _dir_t == "BUY" else "\U0001f4c9"
            _prog_t    = t.get("progress_pct", 0)
            _next_t    = t.get("next_target", "T1")
            _pips_t    = t.get("pips_unrealised", 0)
            _usd_t     = t.get("dollars_unrealised", 0)
            _days_t    = t.get("days_open", 0)
            _t1h_t     = t.get("t1_hit", False)
            _t2h_t     = t.get("t2_hit", False)
            _casc_t    = ("\U0001f6e1️\U0001f6e1️" if _t2h_t
                          else ("\U0001f6e1️" if _t1h_t else ""))
            _bar_t     = _progress_bar(min(max(_prog_t, 0), 100), width=10)
            _pnl_e_t   = "\U0001f7e2" if _pips_t >= 0 else "\U0001f534"
            trades_text += (
                f"{_de_t} **{_pair_t}** {_casc_t}\n"
                f"`{_bar_t}` {max(_prog_t, 0):.0f}% → {_next_t}\n"
                f"{_pnl_e_t} {_pips_t:+.1f}p (${_usd_t:+.2f}) · {_days_t}d open\n\n"
            )
        fields.append({
            "name":  f"\U0001f4ca Open Fund Trades ({len(open_fund_trades)})",
            "value": trades_text.strip() or "None",
            "inline": False,
        })
    else:
        fields.append({
            "name": "\U0001f4ca Open Fund Trades", "value": "No open fund trades", "inline": False,
        })

    # ── SECTION 4: NEW FUND ALERTS ──────────────────────────────────────────────
    if new_fund_alerts:
        alerts_text = ""
        for a in new_fund_alerts:
            _pair_a  = a.get("pair", "")
            _dir_a   = a.get("direction", "")
            _conf_a  = a.get("conf", 0)
            _entry_a = a.get("entry", 0)
            _stop_a  = a.get("stop", 0)
            _t1_a    = a.get("t1", 0)
            _rr_a    = a.get("rr", 0)
            _chk_a   = a.get("checklist", 0)
            _de_a    = "\U0001f4c8" if _dir_a == "BUY" else "\U0001f4c9"
            _tv_a    = _get_tradingview_url(_pair_a)
            alerts_text += (
                f"{_de_a} **{_pair_a} {_dir_a}**\n"
                f"Entry: `{_entry_a:.5f}` · Stop: `{_stop_a:.5f}`\n"
                f"T1: `{_t1_a:.5f}` · R:R {_rr_a}:1\n"
                f"Conf: {_conf_a}/10 · Checklist: {_chk_a}/10\n"
                f"[\U0001f4ca Chart]({_tv_a})\n\n"
            )
        fields.append({
            "name":  f"\U0001f6a8 NEW TRADE ALERTS ({len(new_fund_alerts)})",
            "value": alerts_text.strip(),
            "inline": False,
        })

    # ── SECTION 5: RECENTLY CLOSED TRADES ───────────────────────────────────────
    if newly_closed_trades:
        closed_text = ""
        for t in newly_closed_trades[:5]:
            _pair_c    = t.get("pair", "")
            _out_c     = t.get("outcome", "")
            _pips_c    = t.get("pips", 0)
            _usd_c     = t.get("dollars", 0)
            _e_c = ("✅" if ("WIN" in _out_c or "PARTIAL" in _out_c)
                    else ("❌" if "LOSS" in _out_c else "⏱️"))
            closed_text += (
                f"{_e_c} **{_pair_c}** — {_out_c}\n"
                f"{_pips_c:+.1f}p · ${_usd_c:+.2f}\n"
            )
        fields.append({
            "name": "\U0001f4cb Recently Closed", "value": closed_text.strip(), "inline": False,
        })

    # ── SECTION 6: WATCH LIST ────────────────────────────────────────────────────
    if watch_list_pairs:
        watch_text = ""
        for w in watch_list_pairs[:5]:
            _pair_w  = w.get("pair", "")
            _dir_w   = w.get("direction", "")
            _conf_w  = w.get("conf", 0)
            _grade_w = w.get("grade", "")
            _rsn_w   = w.get("reason", "")
            _de_w    = "\U0001f4c8" if _dir_w == "BUY" else "\U0001f4c9"
            _tv_w    = _get_tradingview_url(_pair_w)
            watch_text += (
                f"{_de_w} **{_pair_w}** Grade {_grade_w} · {_conf_w}/10\n"
                f"{_rsn_w[:55] if _rsn_w else ''}\n"
                f"[\U0001f4ca Chart]({_tv_w})\n\n"
            )
        fields.append({
            "name":  f"\U0001f440 Watch List ({len(watch_list_pairs)} pairs)",
            "value": watch_text.strip() or "Nothing approaching",
            "inline": False,
        })

    # ── SECTION 7: RESEARCH SUMMARY ─────────────────────────────────────────────
    _wr_e = ("\U0001f4c8" if research_win_rate > 45
             else ("⚠️" if research_win_rate > 30 else "\U0001f534"))
    _recent_ms_txt = ""
    for m in (recent_research_milestones or [])[:5]:
        _pair_m = m.get("pair", "")
        _ms_m   = m.get("milestone", "")
        _pips_m = m.get("pips", 0)
        _out_m  = m.get("outcome", "")
        _e_m = ("✅" if ("WIN" in _out_m or "T" in _ms_m)
                else ("\U0001f6e1️" if "cascade" in _out_m.lower() else "❌"))
        _recent_ms_txt += f"{_e_m} {_pair_m} {_ms_m}: {_pips_m:+.1f}p\n"
    fields.append({
        "name": "\U0001f52c Research Trades",
        "value": (
            f"Open: **{research_open}** · Closed: **{research_closed}**\n"
            f"{_wr_e} Win rate: **{research_win_rate:.0f}%** ({research_decisive} decisive)\n"
            f"Avg win: +{research_avg_win_pips:.0f}p · Avg loss: -{research_avg_loss_pips:.0f}p\n"
            f"Profit factor: {research_profit_factor:.2f}"
            + (f"\n\n**Recent milestones:**\n{_recent_ms_txt}" if _recent_ms_txt else "")
        ),
        "inline": False,
    })

    # ── SECTIONS 8-10: INLINE CONTEXT TRIO ──────────────────────────────────────
    fields.append({
        "name": "\U0001f30d Market Context",
        "value": (
            f"Regime: **{regime}**\n"
            f"VIX: {vix} · Threshold: **{threshold}/10**\n"
            f"Analysed: {pairs_analysed} of {universe_size:,}"
        ),
        "inline": True,
    })
    fields.append({
        "name": "\U0001f916 ML System",
        "value": (
            f"AUC: {ml_auc:.3f}\n"
            f"Gate: {ml_gate_status or 'n/a'}\n"
            f"Decisive: {ml_decisive_count}\n"
            f"Updates: {online_model_updates}"
        ),
        "inline": True,
    })
    fields.append({
        "name": "\U0001f4e1 System Health",
        "value": (
            f"Data quality: {data_quality_pct:.0f}%\n"
            f"API: {td_calls_used}/800 calls\n"
            f"COT: {cot_status or 'n/a'}\n"
            f"Monitor gap: {last_monitor_gap_min:.0f}m\n"
            f"Scan: {scan_minutes:.0f}m · ${scan_cost_usd:.4f}"
        ),
        "inline": True,
    })

    alert_summary  = f"\U0001f6a8 {len(new_fund_alerts)} new alert(s) — " if new_fund_alerts else "No new alerts — "
    closed_summary = f"{len(newly_closed_trades)} trade(s) closed" if newly_closed_trades else "no trades closed"
    description    = (
        f"{alert_summary}{closed_summary}\n"
        f"Fund: **${fund_balance:,.2f}** ({daily_pnl_pct:+.2f}% today) · "
        f"{len(open_fund_trades) if open_fund_trades else 0} open trades"
    )

    return _send_embed(
        WEBHOOK_HEALTH,
        f"{scan_emoji} Forex AI — {scan_name} · {auckland_time}",
        description,
        color,
        fields=fields,
    )
