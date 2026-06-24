"""Discord notification layer — secondary channel alongside Telegram.

Graceful degradation: missing webhook URLs or Discord errors are logged
and ignored.  The scan is never slowed, Telegram is never blocked.
"""
import json
import os
import time as _time_mod
from datetime import datetime, timezone
from pathlib import Path

import requests

DASHBOARD_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "discord_dashboard.json"

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
    if current_pct <= 0:
        return "░" * width
    if current_pct > 100:
        return "█" * width
    filled = max(1, round((min(current_pct, 100) / 100) * width))
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


def send_fund_stop_hit(pair, direction,
                        t1_hit=False, t2_hit=False,
                        t1_pips=0.0, t2_pips=0.0,
                        t1_dollars=0.0, t2_dollars=0.0,
                        net_pips=0.0, net_dollars=0.0,
                        cascade_label=""):
    dir_emoji = "\U0001f4c8" if direction == "BUY" else "\U0001f4c9"

    if t2_hit:
        fields = [
            {"name": "Trade Type",              "value": _trade_type_label(True),                             "inline": False},
            {"name": "\U0001f3c6 Cascade",      "value": cascade_label or "T1 + T2 both banked",             "inline": False},
            {"name": "✅ T1 banked (40%)",      "value": f"+{t1_pips:.1f}p / +${t1_dollars:.2f}",            "inline": True},
            {"name": "✅ T2 banked (30%)",      "value": f"+{t2_pips:.1f}p / +${t2_dollars:.2f}",            "inline": True},
            {"name": "\U0001f512 Remainder (30%)", "value": "Stopped at breakeven",                          "inline": True},
            {"name": "\U0001f4b0 Net Result",   "value": f"**+{net_pips:.1f}p / +${net_dollars:.2f}**",      "inline": False},
        ]
        return _send_embed(
            WEBHOOK_FUND,
            f"\U0001f3c6 {pair} {dir_emoji} — Stop Hit — WIN",
            "\U0001f4bc FUND TRADE — T1 + T2 banked before stop",
            0x2ECC71,
            fields=fields,
        )
    elif t1_hit:
        fields = [
            {"name": "Trade Type",              "value": _trade_type_label(True),                             "inline": False},
            {"name": "\U0001f6e1️ Cascade",      "value": cascade_label or "T1 banked · 60% breakeven",      "inline": False},
            {"name": "✅ T1 banked (40%)",      "value": f"+{t1_pips:.1f}p / +${t1_dollars:.2f}",            "inline": True},
            {"name": "\U0001f512 Remainder (60%)", "value": "Stopped at breakeven",                          "inline": True},
            {"name": "\U0001f4b0 Net Result",   "value": f"**+{net_pips:.1f}p / +${net_dollars:.2f}**",      "inline": False},
        ]
        return _send_embed(
            WEBHOOK_FUND,
            f"\U0001f6e1️ {pair} {dir_emoji} — Stop Hit — PROTECTED",
            "\U0001f4bc FUND TRADE — Cascade protection saved this trade",
            0xF39C12,
            fields=fields,
        )
    else:
        fields = [
            {"name": "Trade Type",              "value": _trade_type_label(True),                             "inline": False},
            {"name": "❌ Result",               "value": f"{net_pips:.1f}p / -${abs(net_dollars):.2f}",      "inline": True},
            {"name": "ℹ️ Note",                "value": cascade_label or "No cascade protection",            "inline": True},
        ]
        return _send_embed(
            WEBHOOK_FUND,
            f"❌ {pair} {dir_emoji} — Stop Hit — LOSS",
            "\U0001f4bc FUND TRADE — Loss recorded and logged",
            COLOR_FUND_LOSS,
            fields=fields,
        )


def send_research_monitor_batch(hot: list, near_stop: list) -> bool:
    """Send a single batch report for research HOT-zone trades to #research.

    hot       — list of dicts: pair, direction, progress_pct, target_label, distance_pips
    near_stop — list of dicts: pair, direction, distance_pips
    """
    if not WEBHOOK_RESEARCH:
        return False
    if not hot and not near_stop:
        return False

    fields = []
    if hot:
        target_lines = []
        for t in hot:
            pair     = t.get("pair") or t.get("symbol") or "?"
            progress = float(t.get("progress_pct") or t.get("pct") or t.get("progress") or 0)
            label    = t.get("target_label") or t.get("label") or t.get("target") or "T?"
            dist     = float(t.get("distance_pips") or t.get("dist") or t.get("distance") or 0)
            target_lines.append(f"· {pair}: {progress:.0f}% → {label} ({dist:.1f}p away)")
        fields.append({"name": "\U0001f3af Approaching Targets", "value": "\n".join(target_lines)[:1000], "inline": False})
    if near_stop:
        stop_lines = []
        for t in near_stop:
            pair = t.get("pair") or t.get("symbol") or "?"
            dist = float(t.get("stop_distance_pips") or t.get("distance_pips") or
                         t.get("dist") or t.get("distance") or 0)
            stop_lines.append(f"· {pair}: {dist:.1f}p to stop")
        fields.append({"name": "⚠️ Near Stop Loss", "value": "\n".join(stop_lines)[:1000], "inline": False})

    updated_at = datetime.now(timezone.utc).strftime("%H:%M")
    embed = {
        "title":     "📊 Research — Active Alerts",
        "color":     0x3498DB,
        "fields":    fields,
        "footer":    {
            "text": (
                f"Research only — not real money · "
                f"{len(hot)} HOT · {len(near_stop)} near stop · {updated_at} UTC"
            )
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    for attempt in range(3):
        try:
            r = requests.post(WEBHOOK_RESEARCH, json={"embeds": [embed]}, timeout=10)
            if r.status_code == 204:
                return True
            if r.status_code == 429:
                _time_mod.sleep(5)
        except Exception:
            _time_mod.sleep(2)
    return False


def send_fund_approaching(pair, direction, progress_pct, target_price,
                           current_price, distance_pips, stop_price, milestone,
                           entry_price=0, is_fund=True, warning_pips=None):
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

    # Price moved against trade — approaching stop loss
    if progress_pct <= 0:
        _stop_dist = abs(distance_pips)
        _fields_stop = [
            {"name": "Trade Type",               "value": _trade_type_label(is_fund),                          "inline": False},
            {"name": "⚠️ Risk",                  "value": "Price has moved **against** this trade",            "inline": False},
            {"name": "\U0001f6d1 Stop Loss",     "value": f"`{stop_price:.5f}`",                               "inline": True},
            {"name": "\U0001f4cd Current",       "value": f"`{current_price:.5f}`",                            "inline": True},
            {"name": "\U0001f4cf Distance",      "value": f"`{_stop_dist:.1f} pips to stop`",                  "inline": True},
        ]
        if warning_pips is not None:
            _fields_stop.append({
                "name":   "⚠️ Warning",
                "value":  f"`{warning_pips:.0f}p warning zone triggered`",
                "inline": True,
            })
        if entry_price:
            _pb = _price_position_bar(entry_price, current_price, stop_price, target_price, direction)
            if _pb:
                _fields_stop.append({"name": "\U0001f4c8 Price Position", "value": _pb, "inline": False})
        _fields_stop.append({"name": "\U0001f517 Chart", "value": f"[View {pair} on TradingView]({tv_url})", "inline": False})
        return _send_embed(
            WEBHOOK_MONITOR,
            f"⚠️ {pair} {dir_emoji} — Approaching STOP LOSS",
            f"{'💼 FUND' if is_fund else '🔬 RESEARCH'} — Trade in danger zone — monitor watching closely",
            0xFF3333,
            fields=_fields_stop,
        )

    bar = _progress_bar(progress_pct)

    fields = [
        {"name": "Trade Type",                 "value": _trade_type_label(is_fund),                           "inline": False},
        {"name": "\U0001f4ca Progress to Target","value": f"`{bar}` {progress_pct:.0f}%",                     "inline": False},
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
    scan_mode: str,
    auckland_time: str = "",
    scan_date: str = "",
    yes_trades: list = None,
    blocked_trades: list = None,
    watch_list: list = None,
    fund_balance: float = 10000,
    daily_pnl_dollars: float = 0,
    daily_pnl_pct: float = 0,
    drawdown_pct: float = 0,
    peak_balance: float = 10000,
    open_count: int = 0,
    consecutive_wins: int = 0,
    consecutive_losses: int = 0,
    risk_pct: float = 1.0,
    fund_total: int = 0,
    fund_decisive: int = 0,
    fund_win_rate: float = 0,
    fund_wins: int = 0,
    fund_protected: int = 0,
    fund_losses: int = 0,
    avg_win_pips: float = 0,
    avg_win_dollars: float = 0,
    avg_loss_pips: float = 0,
    avg_loss_dollars: float = 0,
    profit_factor_dollars: float = 0,
    best_pair: str = "",
    best_pips: float = 0,
    total_return_pct: float = 0,
    research_open: int = 0,
    research_closed: int = 0,
    research_win_rate: float = 0,
    research_decisive: int = 0,
    research_pf: float = 0,
    wr_band_45: float = 0,
    wr_band_56: float = 0,
    wr_band_67: float = 0,
    wr_band_7p: float = 0,
    n_band_45: int = 0,
    n_band_56: int = 0,
    n_band_67: int = 0,
    n_band_7p: int = 0,
    best_pairs_str: str = "",
    adaptive_count: int = 0,
    ml_trained: int = 0,
    ml_accuracy: float = 0,
    ml_recent_wr: float = 0,
    ml_overall_wr: float = 0,
    ml_active: bool = False,
    ml_last_retrain: str = "never",
    mta_pct: float = 0,
    hhhl_pct: float = 0,
    regime: str = "",
    vix: float = 0,
    threshold: float = 6.0,
    strongest_ccy: str = "",
    weakest_ccy: str = "",
    dq_pct: float = 99,
    td_calls: int = 0,
    td_limit: int = 800,
    scan_cost: float = 0,
    scan_duration: float = 0,
    pairs_analysed: int = 0,
    expiry_alerts: list = None,
    monitor_gap_mins: int = 0,
    sizing_mode: str = "normal",
    active_sessions: list = None,
    news_blackout_pairs: list = None,
    open_trades: list = None,
):
    if not WEBHOOK_HEALTH:
        return False

    yes_trades     = yes_trades or []
    blocked_trades = blocked_trades or []
    watch_list     = watch_list or []
    expiry_alerts  = expiry_alerts or []

    # ── Color: green=new trade, red=issue, orange=watchlist, blue=clean ──────────
    has_issues = bool(expiry_alerts or monitor_gap_mins > 120 or td_calls > 600)
    if yes_trades:
        color = 0x2ECC71
    elif has_issues:
        color = 0xE74C3C
    elif watch_list:
        color = 0xE67E22
    else:
        color = 0x3498DB

    # ── Title ────────────────────────────────────────────────────────────────────
    _mode_emoji = {
        "full": "\U0001f305", "morning": "☀️",
        "prelondon": "\U0001f306", "preny": "\U0001f303",
    }.get(scan_mode, "\U0001f916")
    _mode_name = {
        "full": "6am Full Scan", "morning": "9am Morning Scan",
        "prelondon": "5pm Pre-London", "preny": "11pm Pre-New York",
    }.get(scan_mode, scan_mode.title())
    if not auckland_time:
        try:
            import zoneinfo as _zi
            auckland_time = datetime.now(_zi.ZoneInfo("Pacific/Auckland")).strftime("%H:%M NZST")
        except Exception:
            auckland_time = datetime.now(timezone.utc).strftime("%H:%M UTC")
    if not scan_date:
        scan_date = datetime.now(timezone.utc).strftime("%d %b %Y")
    _title = f"{_mode_emoji} {_mode_name} · {auckland_time} · {scan_date}"

    # ── Description (one-liner summary) ─────────────────────────────────────────
    if yes_trades:
        _desc_lead = f"\U0001f7e2 {len(yes_trades)} new fund trade(s) opened"
    elif blocked_trades:
        _desc_lead = f"⛔ {len(blocked_trades)} trade(s) blocked"
    elif watch_list:
        _desc_lead = f"\U0001f440 {len(watch_list)} pair(s) on watch list"
    else:
        _desc_lead = "No new setups — waiting for cleaner opportunities"
    _description = (
        f"{_desc_lead}\n"
        f"Fund: **${fund_balance:,.2f}** ({daily_pnl_pct:+.2f}% today) · "
        f"{open_count}/4 slots used"
    )

    fields = []

    # ── SECTION 1: NEW SETUPS ─────────────────────────────────────────────────────
    _setup_lines = []
    # Status banners: regime pause and loss streak pause
    _RANGING_REGIMES_DSC = ["RANGING_LOW_VOL", "RANGING_LOW_VOLATILITY", "RISK_OFF"]
    _regime_upper_dsc = str(regime or "").upper()
    if any(r in _regime_upper_dsc for r in _RANGING_REGIMES_DSC):
        _setup_lines.append(
            "⏸️ **Fund paused — Ranging market**\n"
            "   Threshold raised to 7.5/10\n"
            "   Waiting for trending conditions"
        )
    if consecutive_losses >= 3:
        _setup_lines.append(
            f"⚠️ **LOSS STREAK PAUSE — {consecutive_losses} consecutive losses**\n"
            "   No new trades until existing positions recover"
        )
    for _t in yes_trades:
        _p   = _t.get("pair", "")
        _d   = ((_t.get("parsed") or {}).get("direction") or _t.get("direction", ""))
        _c   = float((_t.get("parsed") or {}).get("confidence") or _t.get("conf") or 0)
        _en  = float((_t.get("parsed") or {}).get("entry") or _t.get("entry") or 0)
        _sl  = float((_t.get("parsed") or {}).get("stop_loss") or _t.get("stop") or 0)
        _t1  = float((_t.get("parsed") or {}).get("target") or _t.get("t1") or 0)
        _t2  = float((_t.get("parsed") or {}).get("t2_price") or _t.get("t2") or 0)
        _rr  = float((_t.get("parsed") or {}).get("reward_risk") or _t.get("rr") or 0)
        _szp = float((_t.get("_fs_sizing") or {}).get("pct") or risk_pct)
        _rd  = round(fund_balance * _szp / 100)
        _de  = "\U0001f4c8" if str(_d).upper() == "BUY" else "\U0001f4c9"
        _t2s = f" · T2: `{_t2:.5f}`" if _t2 else ""
        _setup_lines.append(
            f"✅ {_de} **{_p} {_d}** · conf {_c:.1f}/10\n"
            f"   Entry: `{_en:.5f}` · Stop: `{_sl:.5f}`\n"
            f"   T1: `{_t1:.5f}`{_t2s} · R:R {_rr:.1f} · Risk: ${_rd}"
        )
    for _b in blocked_trades:
        _bp  = _b.get("pair", "")
        _bd  = _b.get("direction", "")
        _bc  = float(_b.get("conf") or 0)
        _br  = str(_b.get("reason") or "blocked")
        _br_lo = _br.lower()
        if any(x in _br_lo for x in ("correlation", "correlated", "exposure")):
            _b_icon = "\U0001f517"  # 🔗 correlation
        elif any(x in _br_lo for x in ("trend", "weekly", "monthly", "aligned", "opposing")):
            _b_icon = "\U0001f4ca"  # 📊 trend
        elif any(x in _br_lo for x in ("session", "tokyo", "london", "new_york", "new york")):
            _b_icon = "\U0001f551"  # 🕑 session
        elif any(x in _br_lo for x in ("news", "blackout", "impact", "calendar")):
            _b_icon = "\U0001f4f0"  # 📰 news
        else:
            _b_icon = "⛔"
        _setup_lines.append(
            f"{_b_icon} **{_bp} {_bd}** · conf {_bc:.1f}/10\n"
            f"   {_br[:90]}"
        )
    if not _setup_lines:
        _setup_lines.append(
            "No new setups this scan\n"
            "Waiting for cleaner opportunities"
        )
    fields.append({
        "name": "\U0001f3af New Fund Trades",
        "value": "\n\n".join(_setup_lines)[:1024],
        "inline": False,
    })

    # ── SECTION 2: WATCH LIST (only if populated) ─────────────────────────────────
    if watch_list:
        _wl_lines = []
        for _w in watch_list[:5]:
            _wp  = _w.get("pair", "")
            _wd  = _w.get("direction", "")
            _wc  = float(_w.get("conf") or _w.get("confidence") or 0)
            _wr  = str(_w.get("reason") or "")[:55]
            _wde = "\U0001f4c8" if str(_wd).upper() == "BUY" else "\U0001f4c9"
            _wl_lines.append(f"· {_wde} **{_wp}** conf {_wc:.1f}/10 — {_wr}")
        if len(watch_list) > 5:
            _wl_lines.append(f"_…and {len(watch_list) - 5} more_")
        fields.append({
            "name": f"\U0001f440 Watch List ({len(watch_list)} pairs)",
            "value": "\n".join(_wl_lines)[:1024],
            "inline": False,
        })

    # ── SECTION 3: FUND HEALTH ────────────────────────────────────────────────────
    _slots_free  = max(0, 4 - open_count)
    _risk_d      = round(fund_balance * risk_pct / 100)
    _ftmo_used   = abs(daily_pnl_pct)
    _dd_e        = "✅" if drawdown_pct < 3 else ("⚠️" if drawdown_pct < 7 else "\U0001f6a8")
    if consecutive_wins >= 2:
        _streak = f"\U0001f525 {consecutive_wins} wins in a row"
    elif consecutive_losses >= 2:
        _streak = f"⚠️ {consecutive_losses} losses in a row"
    else:
        _streak = "➡️ Neutral"
    _szm_lower = str(sizing_mode or "normal").lower()
    _szm_icons = {"normal": "✅", "conservative": "⚠️", "minimal": "\U0001f534", "pause": "\U0001f6d1"}
    _szm_icon  = _szm_icons.get(_szm_lower, "\U0001f4b2")
    fields.append({
        "name": "\U0001f4b0 Fund Health",
        "value": (
            f"Balance: **${fund_balance:,.2f}** ({daily_pnl_pct:+.2f}% today)\n"
            f"Peak: ${peak_balance:,.2f} · {_dd_e} Drawdown: {drawdown_pct:.2f}%\n"
            f"Capacity: {open_count}/4 open · {_slots_free} slot(s) free\n"
            f"Sizing: {risk_pct:.1f}% per trade (${_risk_d} risk) · {_szm_icon} Mode: **{sizing_mode or 'normal'}**\n"
            f"FTMO: {_ftmo_used:.2f}% of 5% daily limit\n"
            f"Streak: {_streak}\n"
            f"Full details → #fund-alerts ↑"
        ),
        "inline": False,
    })

    # ── SECTION 3b: OPEN POSITIONS (trailing stop status) ────────────────────────
    _open_trades = open_trades or []
    if _open_trades:
        _pos_lines = []
        for _ot in _open_trades[:4]:
            _ot_pair = _ot.get("pair", "")
            _ot_dir  = _ot.get("direction", "")
            _ot_ent  = float(_ot.get("entry") or 0)
            _ot_t1h  = str(_ot.get("t1_hit", "")).upper() in ("TRUE", "1", "YES")
            _ot_t2h  = str(_ot.get("t2_hit", "")).upper() in ("TRUE", "1", "YES")
            _ot_t3h  = str(_ot.get("t3_hit", "")).upper() in ("TRUE", "1", "YES")
            _ot_eff  = float(_ot.get("effective_stop") or _ot.get("stop_loss") or 0)
            _ot_orig = float(_ot.get("stop_loss") or 0)
            _ot_de   = "\U0001f4c8" if str(_ot_dir).upper() == "BUY" else "\U0001f4c9"
            _ot_milestones = []
            if _ot_t3h: _ot_milestones.append("T3✔")
            elif _ot_t2h: _ot_milestones.append("T2✔")
            elif _ot_t1h: _ot_milestones.append("T1✔")
            _ot_ms_str = " · ".join(_ot_milestones) if _ot_milestones else "awaiting T1"
            _ot_trailed = _ot_eff and _ot_orig and abs(_ot_eff - _ot_orig) > 1e-8
            if _ot_trailed:
                _ot_stop_str = f"\U0001f512 Stop trailed → `{_ot_eff:.5f}`"
            elif _ot_t1h:
                _ot_stop_str = f"\U0001f512 Breakeven stop `{_ot_eff:.5f}`"
            else:
                _ot_stop_str = f"Stop: `{_ot_eff:.5f}`"
            _pos_lines.append(
                f"{_ot_de} **{_ot_pair} {_ot_dir}** · {_ot_ms_str}\n"
                f"   Entry: `{_ot_ent:.5f}` · {_ot_stop_str}"
            )
        fields.append({
            "name": "\U0001f4bc Open Positions",
            "value": "\n\n".join(_pos_lines)[:1024],
            "inline": False,
        })

    # ── SECTION 4: FUND PERFORMANCE ───────────────────────────────────────────────
    if fund_decisive > 0:
        _wr_e = "🟢" if fund_win_rate >= 50 else ("🟡" if fund_win_rate >= 35 else "🔴")
        _perf_val = (
            f"Trades: **{fund_total}** total · {fund_decisive} decisive\n"
            f"{_wr_e} Win rate: **{fund_win_rate:.0f}%** "
            f"({fund_wins}W {fund_protected}P {fund_losses}L)\n"
            f"Avg win: +{avg_win_pips:.1f}p / +${avg_win_dollars:.0f}\n"
            f"Avg loss: -{avg_loss_pips:.1f}p / -${avg_loss_dollars:.0f}\n"
            f"Profit factor: **{profit_factor_dollars:.2f}** (dollar basis)"
            + (f"\nBest: {best_pair} +{best_pips:.1f}p" if best_pips > 0 else "")
            + f"\nReturn: {total_return_pct:+.2f}% since inception"
        )
    else:
        _perf_val = (
            f"Trades taken: {fund_total}\n"
            "Insufficient data — need decisive outcomes"
        )
    fields.append({
        "name": "\U0001f4c8 Fund Performance",
        "value": _perf_val[:1024],
        "inline": False,
    })

    # ── SECTION 5: ML SYSTEM ──────────────────────────────────────────────────────
    _ML_THRESHOLD = 30
    if ml_trained == 0:
        _ml_status = "❌ UNTRAINED — no model yet"
        _ml_detail = "Needs first training run"
    elif ml_active or ml_trained >= _ML_THRESHOLD:
        _ml_status = "✅ ACTIVE — influencing scores"
        _ml_detail = (
            f"Win rate: {ml_accuracy:.0f}% · Trained on: {ml_trained} trades\n"
            f"Last retrain: {ml_last_retrain}"
        )
    else:
        _n_more = _ML_THRESHOLD - ml_trained
        _ml_status = "⏳ LEARNING — not yet active"
        _ml_detail = (
            f"Win rate: {ml_recent_wr:.0f}% on {ml_trained} trades\n"
            f"Need {_n_more} more decisive trades to activate"
        )
    _feat_avg  = (mta_pct + hhhl_pct) / 2 if (mta_pct or hhhl_pct) else 0
    _feat_note = (
        "⚠️ Features low — ML training on incomplete data"
        if _feat_avg < 50 and _feat_avg > 0
        else ("✅ Features good — ML has clean data" if _feat_avg >= 80 else "")
    )
    if ml_recent_wr > 0 and ml_overall_wr > 0:
        _wr_diff = ml_recent_wr - ml_overall_wr
        _trend   = "📈 improving" if _wr_diff > 5 else ("📉 declining" if _wr_diff < -5 else "➡️ stable")
        _trend_l = f"\nRecent WR: {ml_recent_wr:.0f}% ({_trend} vs {ml_overall_wr:.0f}% overall)"
    elif ml_recent_wr > 0:
        _trend_l = f"\nRecent WR: {ml_recent_wr:.0f}%"
    else:
        _trend_l = ""
    _ml_val = (
        f"{_ml_status}\n{_ml_detail}\n"
        f"\nFeatures: monthly_trend {mta_pct:.0f}% · hhhl {hhhl_pct:.0f}%"
        + (f"\n{_feat_note}" if _feat_note else "")
        + _trend_l
    )
    fields.append({
        "name": "\U0001f916 ML System",
        "value": _ml_val.strip()[:1024],
        "inline": False,
    })

    # ── SECTION 6: RESEARCH TRADES ───────────────────────────────────────────────
    _rt_wr_e = "🟢" if research_win_rate >= 50 else ("🟡" if research_win_rate >= 35 else "🔴")
    _conf_tbl = (
        f"`conf 4-5` {wr_band_45:.0f}% ({n_band_45} trades)\n"
        f"`conf 5-6` {wr_band_56:.0f}% ({n_band_56} trades)\n"
        f"`conf 6-7` {wr_band_67:.0f}% ({n_band_67} trades)\n"
        f"`conf 7+ ` {wr_band_7p:.0f}% ({n_band_7p} trades)"
    )
    _rt_val = (
        f"Open: **{research_open}** · Closed: **{research_closed}**\n"
        f"{_rt_wr_e} Win rate: **{research_win_rate:.0f}%** ({research_decisive} decisive)\n"
        f"Profit factor: {research_pf:.2f}\n"
        f"\n**Win rate by confidence:**\n{_conf_tbl}"
        + (f"\n\n**Best pairs:** {best_pairs_str}" if best_pairs_str else "")
        + (f"\nAdaptive targets: {adaptive_count} pairs" if adaptive_count > 0 else "")
    )
    fields.append({
        "name": "\U0001f52c Research Trades",
        "value": _rt_val[:1024],
        "inline": False,
    })

    # ── SECTION 7: MARKET CONTEXT ────────────────────────────────────────────────
    _regime_e = {
        "trending": "\U0001f4c8", "ranging": "↔️", "volatile": "⚡",
        "risk-on": "\U0001f7e2", "risk-off": "\U0001f534",
    }.get((regime or "").lower(), "\U0001f30d")
    _ctx_parts = [
        f"Regime: {_regime_e} **{regime or 'Unknown'}**",
        f"Threshold: **{threshold:.1f}/10**" + (f" · VIX: {vix:.1f}" if vix > 0 else ""),
    ]
    if strongest_ccy:
        _ctx_parts.append(f"Strongest: **{strongest_ccy}**")
    if weakest_ccy:
        _ctx_parts.append(f"Weakest: **{weakest_ccy}**")
    _next_scan = {
        "full": "9am morning scan", "morning": "5pm pre-London",
        "prelondon": "11pm pre-New York", "preny": "6am full scan",
    }.get(scan_mode, "next scheduled scan")
    _ctx_parts.append(f"Next scan: {_next_scan} NZST")
    fields.append({
        "name": "\U0001f30d Market Context",
        "value": "\n".join(_ctx_parts)[:1024],
        "inline": False,
    })

    # ── SECTION 8: SYSTEM HEALTH ─────────────────────────────────────────────────
    _sys_alerts = list(expiry_alerts)
    if abs(daily_pnl_pct) > 4:
        _sys_alerts.append(f"\U0001f6a8 Fund near FTMO daily limit ({abs(daily_pnl_pct):.2f}%/5%)")
    if monitor_gap_mins > 120:
        _sys_alerts.append(f"⚠️ Monitor gap {monitor_gap_mins:.0f}m — check cron-job.org")
    if td_calls > 600:
        _sys_alerts.append(f"⚠️ API calls {td_calls}/{td_limit} — approaching limit")
    _alert_str   = "\n".join(_sys_alerts) if _sys_alerts else "✅ All systems normal"
    _cost_nzd    = scan_cost * 2.2
    fields.append({
        "name": "⚙️ System Health",
        "value": (
            f"Data quality: {dq_pct:.0f}% · Pairs analysed: {pairs_analysed}\n"
            f"API: {td_calls}/{td_limit} calls\n"
            f"Cost: ${scan_cost:.4f} (~${_cost_nzd:.3f} NZD)\n"
            f"Scan duration: {scan_duration:.0f}m\n"
            + _alert_str
        )[:1024],
        "inline": False,
    })

    return _send_embed(
        WEBHOOK_HEALTH,
        _title,
        _description,
        color,
        fields=fields,
    )


def _load_closed_trades_state() -> dict:
    try:
        data = json.loads(DASHBOARD_STATE_FILE.read_text(encoding="utf-8"))
        mid = data.get("closed_trades_message_id")
        return {"message_id": mid}
    except Exception:
        return {"message_id": None}


def _save_closed_trades_state(state: dict) -> None:
    try:
        try:
            data = json.loads(DASHBOARD_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data["closed_trades_message_id"] = state.get("message_id")
        DASHBOARD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        DASHBOARD_STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"[closed-trades] State saved to discord_dashboard.json: message_id={state.get('message_id')}")
    except Exception as e:
        print(f"[closed-trades] SAVE FAILED: {e}")


def update_closed_trades_log(
    closed_trades: list,
    fund_balance: float,
    total_realised_pips: float,
    total_realised_dollars: float,
    win_streak: int,
    loss_streak: int,
) -> bool:
    if not WEBHOOK_FUND:
        return False

    state = _load_closed_trades_state()
    existing_message_id = state.get("message_id")

    n = len(closed_trades)

    # Color
    if n == 0:
        color = 0x95A5A6
    elif total_realised_pips >= 0:
        color = 0x2ECC71
    else:
        color = 0xE74C3C

    # Streak text
    if win_streak >= 2:
        streak_text = f"\U0001f525 {win_streak} wins in a row"
    elif loss_streak >= 2:
        streak_text = f"⚠️ {loss_streak} losses in a row"
    else:
        streak_text = "—"

    fields = []

    # Section 1 — Realised Summary
    fields.append({
        "name": "Realised Performance",
        "value": (
            f"Total closed: {n} trades\n"
            f"Realised P&L: {total_realised_pips:+.1f}p / ${total_realised_dollars:+.2f}\n"
            f"Current streak: {streak_text}"
        ),
        "inline": False,
    })

    # Section 2 — One field per closed trade (most recent first)
    for t in closed_trades:
        outcome = t.get("outcome", "")
        status  = t.get("status", "")
        pips    = t.get("pips", 0.0)

        # Outcome emoji
        if outcome in ("WIN", "FULL_WIN") or outcome == "WIN (expired)":
            emoji = "✅"
        elif outcome == "PARTIAL_WIN":
            emoji = "\U0001f6e1️"
        elif outcome == "LOSS" or outcome == "LOSS (expired)":
            emoji = "❌"
        elif status == "EXPIRED":
            emoji = "✅" if pips > 0 else "❌"
        else:
            emoji = "⚪"

        # Outcome label
        label_map = {
            "WIN":           "WIN",
            "FULL_WIN":      "FULL WIN",
            "PARTIAL_WIN":   "PROTECTED",
            "LOSS":          "LOSS",
            "WIN (expired)": "WIN (expired)",
            "LOSS (expired)":"LOSS (expired)",
        }
        outcome_label = label_map.get(outcome, outcome)

        pair      = t.get("pair", "")
        trade_id  = t.get("id", "")
        direction = t.get("direction", "")
        conf      = t.get("conf", 0.0)
        entry     = t.get("entry", 0.0)
        exit_p    = t.get("exit_price", 0.0)
        dollars   = t.get("dollars", 0.0)
        cascade   = t.get("cascade")
        closed_at = t.get("closed_at", "")
        hold_time = t.get("hold_time", "?")

        field_name = f"{emoji} {pair} #{trade_id} · {outcome_label}"

        field_lines = [
            f"{direction} · Conf: {conf:.1f}/10",
            f"Entry: {entry} → Exit: {exit_p}",
            f"Result: {pips:+.1f}p / ${dollars:+.2f}",
        ]
        if cascade:
            field_lines.append(cascade)
        field_lines.append(f"Closed: {closed_at} · Hold: {hold_time}")

        fields.append({
            "name":   field_name,
            "value":  "\n".join(field_lines),
            "inline": False,
        })

    updated_at = datetime.now(timezone.utc).strftime("%H:%M")
    embed = {
        "title":  "📋 Closed Fund Trades",
        "color":  color,
        "fields": fields,
        "footer": {
            "text": (
                f"Forex AI · {n} closed · "
                f"Net: {total_realised_pips:+.1f}p / ${total_realised_dollars:+.2f} · "
                f"Updated {updated_at} UTC"
            )
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    import sys as _sys

    payload = {"embeds": [embed]}

    if existing_message_id:
        edit_url = f"{WEBHOOK_FUND}/messages/{existing_message_id}?wait=true"
        try:
            resp = requests.patch(edit_url, json=payload, timeout=10)
        except Exception as exc:
            print(f"[closed-trades] Edit request failed: {exc}", file=_sys.stdout)
            return False

        if resp.status_code == 200:
            print(f"[closed-trades] Edited message {existing_message_id} ✅", file=_sys.stdout)
            return True
        elif resp.status_code == 404:
            print(f"[closed-trades] Message {existing_message_id} not found — posting new", file=_sys.stdout)
            existing_message_id = None
        else:
            print(f"[closed-trades] Edit failed {resp.status_code}: {resp.text[:200]}", file=_sys.stdout)
            return False

    # POST new message — ?wait=true required to get message id back
    post_url = f"{WEBHOOK_FUND}?wait=true"
    try:
        resp = requests.post(post_url, json=payload, timeout=10)
    except Exception as exc:
        print(f"[closed-trades] Post request failed: {exc}", file=_sys.stdout)
        return False

    print(f"[closed-trades] POST response: status={resp.status_code} body={resp.text[:300]}", file=_sys.stdout)
    if resp.status_code == 200:
        try:
            data = resp.json()
            new_id = data.get("id")
            print(f"[closed-trades] Parsed id: {new_id}", file=_sys.stdout)
        except Exception as e:
            print(f"[closed-trades] Parse error: {e} — raw: {resp.text[:200]}", file=_sys.stdout)
            new_id = None
        if new_id:
            state["message_id"] = str(new_id)
            _save_closed_trades_state(state)
            print(f"[closed-trades] Posted new message id={new_id} ✅", file=_sys.stdout)
        else:
            print(f"[closed-trades] WARNING: no id in response — full response: {resp.text[:300]}", file=_sys.stdout)
        return True

    print(f"[closed-trades] Post failed {resp.status_code}: {resp.text[:200]}", file=_sys.stdout)
    return False


def _load_dashboard_state() -> dict:
    if DASHBOARD_STATE_FILE.exists():
        try:
            return json.loads(DASHBOARD_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_dashboard_state(state: dict) -> None:
    try:
        DASHBOARD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        DASHBOARD_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def update_fund_dashboard(
    open_fund_trades,
    fund_balance, fund_return_pct,
    daily_pnl_pct, daily_pnl_dollars,
    drawdown_pct, sizing_mode, risk_pct,
    consecutive_wins, consecutive_losses,
    ftmo_current_pct, ftmo_target_pct=10.0,
    recently_closed=None,
    fund_total_trades=0,
    fund_wins=0,
    fund_losses=0,
    fund_partial_wins=0,
    fund_win_rate=0.0,
    fund_avg_win_pips=0.0,
    fund_avg_loss_pips=0.0,
    fund_profit_factor=0.0,
    fund_avg_win_dollars=0.0,
    fund_avg_loss_dollars=0.0,
    fund_dollar_profit_factor=0.0,
    fund_best_trade_pips=0.0,
    fund_best_trade_pair="",
    fund_total_pips=0.0,
    fund_breakeven=0,
    # FIX 7: Live equity parameters
    unrealised_pnl_dollars=0.0,
    unrealised_pnl_pips=0.0,
    total_equity=0.0,
    # FIX 11: Data collection mode display
    data_collection_mode=False,
    effective_threshold=7.0,
    dynamic_threshold=7.0,
):
    if not WEBHOOK_FUND:
        return False

    if total_equity == 0.0:
        total_equity = fund_balance

    state = _load_dashboard_state()
    existing_message_id = state.get("fund_dashboard_message_id")

    # ── FUND HEADER ────────────────────────────────────────────────────────────
    fund_emoji = "\U0001f4c8" if daily_pnl_pct >= 0 else "\U0001f4c9"
    dd_emoji   = ("✅" if drawdown_pct < 3 else ("⚠️" if drawdown_pct < 7 else "\U0001f6a8"))
    if consecutive_wins >= 2:
        streak = f"\U0001f525 {consecutive_wins} win streak"
    elif consecutive_losses >= 2:
        streak = f"❄️ {consecutive_losses} loss streak"
    else:
        streak = "➡️ Neutral"

    # FIX 8: FTMO uses total equity not cash balance
    if ftmo_current_pct >= 0:
        ftmo_progress = min((ftmo_current_pct / ftmo_target_pct) * 100, 100) if ftmo_target_pct > 0 else 0
        ftmo_bar      = _progress_bar(ftmo_progress, width=15)
    elif ftmo_current_pct >= -5:
        # Within 5% daily loss limit — show how much of limit is used
        _loss_bar = _progress_bar(min(abs(ftmo_current_pct) / 5 * 100, 100), width=15)
        ftmo_bar = _loss_bar
    else:
        ftmo_bar = "█" * 15

    # FIX 7: Show cash + unrealised + total equity
    unreal_emoji  = "\U0001f7e2" if unrealised_pnl_dollars >= 0 else "\U0001f534"
    equity_emoji  = "\U0001f4c8" if total_equity >= fund_balance else "\U0001f4c9"
    equity_return = (total_equity - 10000.0) / 10000.0 * 100

    fields = []
    fields.append({
        "name":  "\U0001f4b0 Fund Status",
        "value": (
            f"\U0001f4b5 Cash: **${fund_balance:,.2f}** ({fund_return_pct:+.2f}%)\n"
            f"{unreal_emoji} Unrealised: **${unrealised_pnl_dollars:+.2f}** ({unrealised_pnl_pips:+.1f}p)\n"
            f"{'─' * 20}\n"
            f"{equity_emoji} **Total equity: ${total_equity:,.2f}** ({equity_return:+.2f}%)\n"
            f"{fund_emoji} Today: {daily_pnl_pct:+.2f}% (${daily_pnl_dollars:+.2f})\n"
            f"{dd_emoji} Drawdown: {drawdown_pct:.2f}% · {streak}\n"
            f"Sizing: {sizing_mode} ({risk_pct:.2f}% per trade)"
        ),
        "inline": False,
    })
    if ftmo_current_pct >= 0:
        _ftmo_val = f"`{ftmo_bar}` {ftmo_current_pct:+.2f}% / {ftmo_target_pct:.0f}% target"
    elif ftmo_current_pct >= -5:
        _ftmo_val = (f"`{ftmo_bar}` {ftmo_current_pct:+.2f}% "
                     f"(⚠️ {abs(ftmo_current_pct):.2f}% of 5% daily limit used)")
    else:
        _ftmo_val = f"`{ftmo_bar}` {ftmo_current_pct:+.2f}% 🚨 Daily loss limit approaching"
    fields.append({
        "name":  "\U0001f3c6 FTMO Progress",
        "value": _ftmo_val,
        "inline": False,
    })

    # ── FUND STATISTICS ────────────────────────────────────────────────────────
    _decisive = fund_wins + fund_losses + fund_partial_wins
    if _decisive > 0:
        _wr_e   = ("🟢" if fund_win_rate >= 50 else ("🟡" if fund_win_rate >= 35 else "🔴"))
        _be_str = (f" · {fund_breakeven} Breakeven" if fund_breakeven > 0 else "")
        _stats_val = (
            f"Trades: **{fund_total_trades}** total · {_decisive} decisive\n"
            f"{_wr_e} Win rate: **{fund_win_rate:.0f}%**\n"
            f"Wins: {fund_wins} · Protected: {fund_partial_wins} · Losses: {fund_losses}{_be_str}\n"
            f"Avg win: +{fund_avg_win_pips:.1f}p"
            + (f" / +${fund_avg_win_dollars:.0f}" if fund_avg_win_dollars > 0 else "")
            + f" · Avg loss: -{fund_avg_loss_pips:.1f}p"
            + (f" / -${fund_avg_loss_dollars:.0f}" if fund_avg_loss_dollars > 0 else "")
            + "\n"
            f"Profit factor: {fund_profit_factor:.2f} (pips)"
            + (f" · **{fund_dollar_profit_factor:.2f}** (dollars)" if fund_dollar_profit_factor > 0 else "")
            + (f"\nBest: {fund_best_trade_pair} +{fund_best_trade_pips:.1f}p"
               if fund_best_trade_pips > 0 else "")
        )
    else:
        _stats_val = (
            f"Trades taken: {fund_total_trades}\n"
            f"Insufficient data for win rate\n"
            f"Need decisive outcomes to calculate"
        )
    fields.append({
        "name":  "\U0001f4ca Fund Trade Statistics",
        "value": _stats_val,
        "inline": False,
    })

    # ── OPEN TRADES ────────────────────────────────────────────────────────────
    if open_fund_trades:
        for t in open_fund_trades:
            pair       = t.get("pair", "")
            direction  = t.get("direction", "")
            entry      = t.get("entry", 0) or 0
            current    = t.get("current", 0) or 0
            stop       = t.get("stop", 0) or 0
            t1         = t.get("t1", 0) or 0
            t2         = t.get("t2", 0) or 0
            t3         = t.get("t3", 0) or 0
            t1_hit     = t.get("t1_hit", False)
            t2_hit     = t.get("t2_hit", False)
            t3_hit     = t.get("t3_hit", False)
            progress   = t.get("progress_pct", 0)
            next_tgt   = t.get("next_target", "T1")
            pips       = t.get("pips_unrealised", 0)
            dollars    = t.get("dollars_unrealised", 0)
            days       = t.get("days_open", 0)
            open_str   = t.get("open_str") or (f"{days}d" if days else "?d")
            conf       = t.get("conf", 0)
            checklist  = t.get("checklist_score", 0)
            trade_id   = t.get("id", "")
            pnl_note   = t.get("pnl_note", "")

            dir_emoji = "\U0001f4c8" if direction == "BUY" else "\U0001f4c9"
            pnl_emoji = "🟢" if pips >= 0 else "🔴"

            if t3_hit:
                protection = "\U0001f6e1️\U0001f6e1️\U0001f6e1️ Full cascade"
            elif t2_hit:
                protection = "\U0001f6e1️\U0001f6e1️ T2 banked (70%)"
            elif t1_hit:
                protection = "\U0001f6e1️ T1 banked (40%)"
            else:
                protection = "⚡️ Running"

            bar = _progress_bar(min(max(progress, 0), 100), width=12)

            active_target = (t3 if t2_hit else (t2 if t1_hit else t1))
            price_bar = _price_position_bar(entry, current, stop, active_target, direction)

            cascade_dots = (
                f"{'🟢' if t1_hit else '⚪'} T1  "
                f"{'🟢' if t2_hit else '⚪'} T2  "
                f"{'🟢' if t3_hit else '⚪'} T3"
            )

            tv_url = _get_tradingview_url(pair)

            # FIX 10: Trade health status
            progress_pct = t.get("progress_pct", 0)
            if progress_pct >= 100:
                health = "\U0001f3af Target crossed"
            elif progress_pct >= 75:
                health = "\U0001f525 Hot zone"
            elif progress_pct >= 50:
                health = "✅ Good progress"
            elif progress_pct >= 25:
                health = "⏳ Early stage"
            elif progress_pct >= 0:
                health = "↔️ Watching"
            elif progress_pct >= -30:
                health = "⚠️ Moving against"
            else:
                health = "\U0001f6a8 Significant adverse move"

            trade_value = (
                f"{dir_emoji} **{direction}** · Entry: `{entry:.5f}`\n"
                f"Current: `{current:.5f}` · Stop: `{stop:.5f}`\n"
                f"T1: `{t1:.5f}`  T2: `{t2:.5f}`  T3: `{t3:.5f}`\n"
                f"\n"
                f"{cascade_dots}\n"
                f"{protection}\n"
                f"\n"
                f"Progress → {next_tgt}:\n"
                f"`{bar}` {max(progress, 0):.0f}%\n"
            )
            if price_bar:
                trade_value += f"{price_bar}\n"
            # FIX 10: health line after cascade dots
            trade_value += f"{health}\n"
            # P&L includes all banked cascade levels
            trade_value += (
                f"\n"
                f"{pnl_emoji} P&L: **{pips:+.1f}p** / **${dollars:+.2f}**"
                + (f"\n_{pnl_note}_" if pnl_note else "") +
                f"\nOpen: {open_str} · Conf: {conf}/10"
                + (f" · Check: {checklist:.0f}/10" if checklist else "") +
                "\n"
            )
            # FIX 9: Flag trades exceeding intended risk
            risk_limit = float(t.get("risk_dollars", 100) or 100)
            if abs(dollars) > risk_limit * 1.1:
                trade_value += (
                    f"⚠️ Loss exceeds risk limit — "
                    f"${abs(dollars):.2f} vs ${risk_limit:.2f} max\n"
                )
            trade_value += f"[\U0001f4ca TradingView]({tv_url})"

            fields.append({
                "name":  f"\U0001f4bc {pair} #{trade_id}",
                "value": trade_value,
                "inline": False,
            })
    else:
        fields.append({
            "name":  "\U0001f4ca Open Fund Trades",
            "value": "No open fund trades\nWaiting for next signal...",
            "inline": False,
        })

    # ── RECENTLY CLOSED ────────────────────────────────────────────────────────
    if recently_closed:
        closed_text = ""
        for t in recently_closed[:3]:
            outcome = t.get("outcome", "")
            pips_c  = t.get("pips", 0)
            usd_c   = t.get("dollars", 0)
            days_c  = t.get("days_open", 0)
            if "WIN" in outcome or "PARTIAL" in outcome:
                e = "✅"
            elif "LOSS" in outcome:
                e = "❌"
            elif "EXPIRED" in outcome:
                e = "⏱️"
            else:
                e = "⚪"
            closed_text += (
                f"{e} **{t.get('pair', '')}** — {outcome}\n"
                f"{pips_c:+.1f}p · ${usd_c:+.2f} · {days_c}d\n\n"
            )
        fields.append({
            "name":  "\U0001f4cb Recently Closed",
            "value": closed_text.strip(),
            "inline": False,
        })

    # FIX 11: System mode field — show data collection mode status
    if data_collection_mode:
        if effective_threshold > dynamic_threshold:
            _mode_value = (
                f"⚠️ DATA COLLECTION MODE\n"
                f"Override threshold: {effective_threshold}/10\n"
                f"Dynamic threshold: {dynamic_threshold}/10\n"
                f"Lower quality trades active\n"
                f"Disable after 200 decisive trades"
            )
        else:
            _mode_value = (
                f"ℹ️ Data collection mode\n"
                f"Threshold: {effective_threshold}/10"
                f" (dynamic threshold respected)"
            )
        fields.append({
            "name":  "⚙️ System Mode",
            "value": _mode_value,
            "inline": False,
        })

    # ── BUILD EMBED ────────────────────────────────────────────────────────────
    color = (0x00FF88 if daily_pnl_pct >= 0.5
             else 0x27AE60 if daily_pnl_pct >= 0
             else 0xF39C12 if daily_pnl_pct >= -1
             else 0xFF3333)

    updated_at = datetime.now(timezone.utc).strftime("%H:%M UTC")
    n_open = len(open_fund_trades) if open_fund_trades else 0
    embed = {
        "title": "\U0001f4bc Fund Trades Dashboard",
        "description": (
            f"**{n_open} open fund trade(s)** · Last updated: {updated_at}\n"
            f"Auto-updates every monitor run (~30 min)"
        ),
        "color": color,
        "fields": fields,
        "footer": {
            "text": (
                f"Forex AI · ${fund_balance:,.2f} balance · "
                f"Updates on every monitor run"
            )
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # ── EDIT existing message or POST new one ──────────────────────────────────
    if existing_message_id:
        try:
            _wh_tail = WEBHOOK_FUND.split("webhooks/")[1]
            edit_url = f"https://discord.com/api/webhooks/{_wh_tail}/messages/{existing_message_id}"
            resp = requests.patch(edit_url, json={"embeds": [embed]}, timeout=10)
            if resp.status_code == 200:
                return True
        except Exception:
            pass

    # POST new message with ?wait=true to receive the message ID
    try:
        url  = WEBHOOK_FUND + "?wait=true"
        resp = requests.post(url, json={"embeds": [embed]}, timeout=10)
        if resp.status_code == 200:
            msg_id = resp.json().get("id")
            if msg_id:
                state["fund_dashboard_message_id"] = msg_id
                _save_dashboard_state(state)
            return True
    except Exception:
        pass

    return False


def send_trailing_stop_update(trade_id: int, pair: str, direction: str,
                               old_stop: float, new_stop: float,
                               reason: str) -> bool:
    """Send trailing stop update to #monitor channel."""
    webhook = WEBHOOK_MONITOR
    if not webhook:
        return False
    pip_size    = 0.01 if "JPY" in pair else 0.0001
    pips_locked = abs(new_stop - old_stop) / pip_size
    embed = {
        "title": f"\U0001f512 {pair} — Stop Trailed",
        "color": 0x2ECC71,
        "fields": [
            {"name": "Trade",       "value": f"#{trade_id} {direction}", "inline": True},
            {"name": "Old Stop",    "value": f"{old_stop:.5f}",          "inline": True},
            {"name": "New Stop",    "value": f"{new_stop:.5f}",          "inline": True},
            {"name": "Profit Locked", "value": f"+{pips_locked:.1f}p secured", "inline": True},
            {"name": "Reason",      "value": reason,                     "inline": False},
        ],
        "footer": {"text": "Forex AI — Trailing Stop System"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        import requests as _req
        r = _req.post(webhook, json={"embeds": [embed]}, timeout=10)
        return r.status_code == 204
    except Exception:
        return False
