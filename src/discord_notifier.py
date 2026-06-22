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

COLOR_WIN        = 0x00FF88
COLOR_LOSS       = 0xFF3333
COLOR_APPROACHING = 0xFF8800
COLOR_INFO       = 0x0099FF
COLOR_WARNING    = 0xFFFF00
COLOR_CRITICAL   = 0xFF0000
COLOR_HEALTH     = 0x9B59B6
COLOR_RESEARCH   = 0x3498DB


def _send_embed(webhook_url: str, title: str, description: str,
                color: int, fields: list = None) -> bool:
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


def send_fund_milestone(pair: str, direction: str, milestone: str,
                        pips: float, entry: float, current: float,
                        stop: float) -> bool:
    return _send_embed(
        WEBHOOK_FUND,
        f"🎯 {pair} {milestone} HIT — +{pips:.1f}p",
        f"{direction} trade milestone reached",
        COLOR_WIN,
        [
            {"name": "Entry",   "value": str(entry),   "inline": True},
            {"name": "Target",  "value": milestone,    "inline": True},
            {"name": "Current", "value": str(current), "inline": True},
            {"name": "Stop",    "value": str(stop),    "inline": True},
            {"name": "Pips",    "value": f"+{pips:.1f}", "inline": True},
        ],
    )


def send_fund_stop_hit(pair: str, direction: str, pips: float,
                       was_cascade_protected: bool) -> bool:
    if was_cascade_protected:
        title = f"🛡️ {pair} stop — breakeven protected"
        color = COLOR_WIN
    else:
        title = f"❌ {pair} stop hit — LOSS {pips:.1f}p"
        color = COLOR_LOSS
    return _send_embed(
        WEBHOOK_FUND, title,
        f"{direction} | {'Breakeven stop — capital protected' if was_cascade_protected else f'Loss: {pips:.1f} pips'}",
        color,
    )


def send_fund_trade_opened(pair: str, direction: str, conf: float,
                            entry: float, stop: float, t1: float,
                            t2: float, t3: float, risk_pct: float,
                            risk_dollars: float, rr: float,
                            checklist_score: int) -> bool:
    return _send_embed(
        WEBHOOK_FUND,
        f"💼 New fund trade: {pair} {direction}",
        f"Confidence {conf}/10 | Checklist {checklist_score}/10",
        COLOR_INFO,
        [
            {"name": "Entry",     "value": str(entry),                         "inline": True},
            {"name": "Stop",      "value": str(stop),                          "inline": True},
            {"name": "T1",        "value": str(t1),                            "inline": True},
            {"name": "T2",        "value": str(t2),                            "inline": True},
            {"name": "T3",        "value": str(t3),                            "inline": True},
            {"name": "R:R",       "value": f"{rr:.1f}",                        "inline": True},
            {"name": "Risk",      "value": f"{risk_pct:.2f}% (${risk_dollars:.2f})", "inline": True},
            {"name": "Checklist", "value": f"{checklist_score}/10",            "inline": True},
        ],
    )


def send_fund_approaching(pair: str, direction: str, progress_pct: float,
                           target_price: float, current_price: float,
                           distance_pips: float, stop_price: float,
                           milestone: str) -> bool:
    return _send_embed(
        WEBHOOK_MONITOR,
        f"🔥 {pair} approaching {milestone}",
        f"{direction} | {progress_pct:.0f}% of the way to {milestone}",
        COLOR_APPROACHING,
        [
            {"name": "Progress",  "value": f"{progress_pct:.0f}%",     "inline": True},
            {"name": "Target",    "value": str(target_price),           "inline": True},
            {"name": "Current",   "value": str(current_price),          "inline": True},
            {"name": "Distance",  "value": f"{distance_pips:.1f} pips", "inline": True},
            {"name": "Stop",      "value": str(stop_price),             "inline": True},
        ],
    )


def send_research_batch(milestones_list: list, scan_mode: str) -> bool:
    if not milestones_list:
        return False
    fields = []
    for m in milestones_list[:10]:
        fields.append({
            "name":   m.get("pair", "?"),
            "value":  f"{m.get('level','?')} — +{m.get('pips', 0):.1f}p",
            "inline": True,
        })
    desc = f"{len(milestones_list)} milestone(s) detected"
    if len(milestones_list) > 10:
        desc += f" (and {len(milestones_list) - 10} more...)"
    return _send_embed(
        WEBHOOK_RESEARCH,
        f"🔬 Research milestones — {scan_mode}",
        desc,
        COLOR_RESEARCH,
        fields,
    )


def send_system_health(scans_completed: int, monitor_runs: int,
                        milestones_today: int, data_quality_pct: float,
                        api_calls_used: int, api_calls_remaining: int,
                        ml_status: str, cot_status: str,
                        last_monitor_gap_min: float) -> bool:
    return _send_embed(
        WEBHOOK_HEALTH,
        f"📊 Daily system health — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "End of day health summary",
        COLOR_HEALTH,
        [
            {"name": "Scans completed",  "value": str(scans_completed),           "inline": True},
            {"name": "Monitor runs",     "value": str(monitor_runs),              "inline": True},
            {"name": "Milestones today", "value": str(milestones_today),          "inline": True},
            {"name": "Data quality",     "value": f"{data_quality_pct:.0f}%",     "inline": True},
            {"name": "API calls used",   "value": str(api_calls_used),            "inline": True},
            {"name": "API remaining",    "value": str(api_calls_remaining),       "inline": True},
            {"name": "ML status",        "value": ml_status,                      "inline": True},
            {"name": "COT status",       "value": cot_status,                     "inline": True},
            {"name": "Max monitor gap",  "value": f"{last_monitor_gap_min:.0f}m", "inline": True},
        ],
    )


def send_full_scan_report(date: str, universe_size: int, pairs_analysed: int,
                           new_alerts: int, threshold: float, regime: str,
                           open_fund_trades: int, research_open: int,
                           win_rate: float, cost_usd: float,
                           run_minutes: float) -> bool:
    wr_str = f"{win_rate*100:.0f}%" if win_rate is not None else "n/a"
    return _send_embed(
        WEBHOOK_HEALTH,
        f"🤖 Forex AI — {date} scan complete",
        f"Universe: {universe_size} pairs | Analysed: {pairs_analysed}",
        COLOR_INFO,
        [
            {"name": "New alerts",    "value": str(new_alerts),       "inline": True},
            {"name": "Threshold",     "value": str(threshold),        "inline": True},
            {"name": "Regime",        "value": regime,                "inline": True},
            {"name": "Fund open",     "value": str(open_fund_trades), "inline": True},
            {"name": "Research open", "value": str(research_open),    "inline": True},
            {"name": "Win rate",      "value": wr_str,                "inline": True},
            {"name": "Cost",          "value": f"${cost_usd:.3f}",    "inline": True},
            {"name": "Run time",      "value": f"{run_minutes:.1f}m", "inline": True},
        ],
    )


def send_circuit_breaker(reason: str, fund_balance: float,
                          daily_pnl_pct: float, daily_pnl_dollars: float) -> bool:
    return _send_embed(
        WEBHOOK_CRITICAL,
        "⚠️ Circuit breaker ACTIVE",
        reason,
        COLOR_CRITICAL,
        [
            {"name": "Fund balance", "value": f"${fund_balance:,.2f}",      "inline": True},
            {"name": "Daily P&L",    "value": f"{daily_pnl_pct:+.2f}%",     "inline": True},
            {"name": "Daily loss $", "value": f"${daily_pnl_dollars:,.2f}", "inline": True},
        ],
    )


def send_workflow_failure(workflow_name: str, run_url: str) -> bool:
    return _send_embed(
        WEBHOOK_CRITICAL,
        f"🚨 {workflow_name} FAILED",
        "GitHub Actions workflow did not complete",
        COLOR_CRITICAL,
        [{"name": "Run URL", "value": run_url, "inline": False}],
    )


def send_watch_list_movement(pair: str, confidence: float,
                              direction: str, pips_moved: float) -> bool:
    return _send_embed(
        WEBHOOK_MONITOR,
        f"🔔 Watch list movement: {pair}",
        f"{direction} | Conf {confidence}/10",
        COLOR_WARNING,
        [
            {"name": "Direction",  "value": direction,           "inline": True},
            {"name": "Confidence", "value": str(confidence),     "inline": True},
            {"name": "Pips moved", "value": f"{pips_moved:.1f}", "inline": True},
        ],
    )


def send_monitor_gap_alert(gap_minutes: float) -> bool:
    return _send_embed(
        WEBHOOK_CRITICAL,
        "⚠️ Monitor gap detected",
        f"No monitor run for {gap_minutes:.0f} minutes — check cron-job.org",
        COLOR_WARNING,
        [{"name": "Gap", "value": f"{gap_minutes:.0f} minutes", "inline": True}],
    )
