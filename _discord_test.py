import os
import sys
import time
sys.path.insert(0, '.')

# Load .env file manually so the test works locally without setting env vars by hand
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

from src.discord_notifier import (
    send_fund_trade_opened, send_fund_milestone, send_fund_stop_hit,
    send_fund_approaching, send_research_batch, send_full_scan_report,
    send_system_health, send_circuit_breaker, send_workflow_failure,
    send_monitor_gap_alert, send_watch_list_movement,
    _send_embed, WEBHOOK_FUND, WEBHOOK_CRITICAL, COLOR_WARNING,
)

def ok(r):
    return "Sent" if r else "FAILED (no webhook?)"

print("Sending test messages to Discord...")
print("Check all 5 Discord channels")
print()

print("Test 1: New fund trade...")
r = send_fund_trade_opened(
    pair="AUD/JPY", direction="BUY", conf=7,
    entry=113.250, stop=112.680, t1=113.478, t2=113.649, t3=113.820,
    risk_pct=1.0, risk_dollars=100.23, rr=2.4, checklist_score=8,
    kill_zone="London", regime="Trending Risk-On", rsi=45.2, atr=65,
    monthly_trend="Bullish", hhhl="Confirmed", ccy_strength="AUD+2 JPY-3", adx=28,
)
print(f"  -> {ok(r)} to #fund-alerts")
time.sleep(2)

print("Test 2: Fund T1 milestone...")
r = send_fund_milestone(
    pair="AUD/JPY", direction="BUY", milestone="T1", pips=22.8,
    entry=113.250, current=113.478, stop=112.680,
    t1=113.478, t2=113.649, t3=113.820,
    t1_hit=True, t2_hit=False, t3_hit=False, dollars=35.20,
)
print(f"  -> {ok(r)} to #fund-alerts")
time.sleep(2)

print("Test 3: Fund T2 milestone...")
r = send_fund_milestone(
    pair="AUD/JPY", direction="BUY", milestone="T2", pips=39.9,
    entry=113.250, current=113.649, stop=113.250,
    t1=113.478, t2=113.649, t3=113.820,
    t1_hit=True, t2_hit=True, t3_hit=False, dollars=61.50,
)
print(f"  -> {ok(r)} to #fund-alerts")
time.sleep(2)

print("Test 4: Fund FULL WIN (T3)...")
r = send_fund_milestone(
    pair="AUD/JPY", direction="BUY", milestone="T3", pips=57.0,
    entry=113.250, current=113.820, stop=113.250,
    t1=113.478, t2=113.649, t3=113.820,
    t1_hit=True, t2_hit=True, t3_hit=True, dollars=88.00,
)
print(f"  -> {ok(r)} to #fund-alerts")
time.sleep(2)

print("Test 5: Fund stop hit (T1+T2 cascade — WIN)...")
r = send_fund_stop_hit(
    pair="EUR/HKD", direction="BUY",
    t1_hit=True, t2_hit=True,
    t1_pips=161.7, t2_pips=210.3,
    t1_dollars=19.69, t2_dollars=12.82,
    net_pips=161.7 * 0.40 + 210.3 * 0.30,
    net_dollars=32.51,
    cascade_label="T1+T2",
)
print(f"  -> {ok(r)} to #fund-alerts")
time.sleep(2)

print("Test 6: Fund stop hit (T1 only — PROTECTED)...")
r = send_fund_stop_hit(
    pair="GBP/CAD", direction="SELL",
    t1_hit=True, t2_hit=False,
    t1_pips=28.0, t2_pips=0.0,
    t1_dollars=11.20, t2_dollars=0.0,
    net_pips=28.0 * 0.40,
    net_dollars=11.20,
    cascade_label="T1",
)
print(f"  -> {ok(r)} to #fund-alerts")
time.sleep(2)

print("Test 6b: Fund stop hit (genuine loss)...")
r = send_fund_stop_hit(
    pair="USD/CHF", direction="SELL",
    t1_hit=False, t2_hit=False,
    t1_pips=0.0, t2_pips=0.0,
    t1_dollars=0.0, t2_dollars=0.0,
    net_pips=0.0, net_dollars=0.0,
    cascade_label="",
)
print(f"  -> {ok(r)} to #fund-alerts")
time.sleep(2)

print("Test 7: Fund HOT zone approaching T1...")
r = send_fund_approaching(
    pair="AUD/JPY", direction="BUY", progress_pct=74,
    target_price=113.478, current_price=113.369, distance_pips=10.9,
    stop_price=112.680, milestone="T1", entry_price=113.250, is_fund=True,
)
print(f"  -> {ok(r)} to #monitor")
time.sleep(2)

print("Test 8: Research HOT zone approaching T2...")
r = send_fund_approaching(
    pair="EUR/AUD", direction="SELL", progress_pct=75,
    target_price=1.63361, current_price=1.63492, distance_pips=13.1,
    stop_price=1.63886, milestone="T2", entry_price=1.64200, is_fund=False,
)
print(f"  -> {ok(r)} to #monitor")
time.sleep(2)

print("Test 9: Stop approaching alert...")
r = send_fund_approaching(
    pair="NZD/CAD", direction="BUY", progress_pct=-74,
    target_price=0.81727, current_price=0.81212, distance_pips=14.0,
    stop_price=0.81072, milestone="STOP", entry_price=0.81900, is_fund=True,
)
print(f"  -> {ok(r)} to #monitor")
time.sleep(2)

print("Test 10: Research batch milestones...")
r = send_research_batch(
    milestones_list=[
        {"pair": "AUD/JPY", "milestone": "T2",   "pips": 45.5,  "outcome": "PARTIAL_WIN"},
        {"pair": "GBP/CAD", "milestone": "T1",   "pips": 28.0,  "outcome": "PARTIAL_WIN"},
        {"pair": "EUR/AUD", "milestone": "T2",   "pips": 52.5,  "outcome": "PARTIAL_WIN"},
        {"pair": "AUD/CAD", "milestone": "STOP", "pips": -38.0, "outcome": "LOSS"},
        {"pair": "CAD/NOK", "milestone": "T1",   "pips": 90.2,  "outcome": "cascade_protected"},
        {"pair": "AUD/SEK", "milestone": "T1",   "pips": 154.8, "outcome": "PARTIAL_WIN"},
    ],
    scan_mode="5pm check", total_open=63, win_rate=42, decisive=89,
)
print(f"  -> {ok(r)} to #research")
time.sleep(2)

print("Test 11: Watch list movement...")
r = send_watch_list_movement(
    pair="USD/SEK", confidence=5, direction="SELL", pips_moved=76.0, atr_multiple=1.2,
)
print(f"  -> {ok(r)} to #monitor")
time.sleep(2)

print("Test 12: 6am full scan report...")
r = send_full_scan_report(
    date="Monday 22 June 2026", scan_mode="full",
    universe_size=1399, pairs_analysed=22,
    new_alerts=[
        {"pair": "USD/CHF", "direction": "SELL", "conf": 7},
        {"pair": "EUR/USD", "direction": "BUY",  "conf": 6},
    ],
    threshold=7.0, regime="Trending Risk-On",
    open_fund_trades=[
        {"pair": "AUD/JPY", "direction": "BUY",  "progress_pct": 74,  "next_target": "T1"},
        {"pair": "EUR/HKD", "direction": "BUY",  "progress_pct": 100, "next_target": "T2"},
        {"pair": "USD/CHF", "direction": "SELL", "progress_pct": 15,  "next_target": "T1"},
        {"pair": "EUR/USD", "direction": "BUY",  "progress_pct": 8,   "next_target": "T1"},
    ],
    research_open=63, win_rate=42, profit_factor=0.89,
    cost_usd=0.091, run_minutes=45, data_quality_pct=80,
    cot_status="7/8 currencies", calendar_status="Live data",
    api_calls_used=31, ml_status="Active — 76% AUC (not yet influencing)",
)
print(f"  -> {ok(r)} to #system-health")
time.sleep(2)

print("Test 13: 5pm pre-London scan report...")
r = send_full_scan_report(
    date="Monday 22 June 2026", scan_mode="prelondon",
    universe_size=1399, pairs_analysed=18, new_alerts=[],
    threshold=7.0, regime="Trending Risk-On",
    open_fund_trades=[
        {"pair": "AUD/JPY", "direction": "BUY", "progress_pct": 80, "next_target": "T2"},
    ],
    research_open=61, win_rate=42, profit_factor=0.89,
    cost_usd=0.062, run_minutes=32, data_quality_pct=75,
    cot_status="Unavailable", calendar_status="Live data",
    api_calls_used=62, ml_status="Active",
)
print(f"  -> {ok(r)} to #system-health")
time.sleep(2)

print("Test 14: 11pm system health digest...")
r = send_system_health(
    date="Monday 22 June 2026",
    scans_completed=3, scans_expected=4,
    monitor_runs=46, monitor_expected=48,
    milestones_today=15, data_quality_pct=78,
    api_calls_used=547, api_calls_remaining=253,
    ml_status="Active — 76% AUC", cot_status="Unavailable today",
    last_monitor_gap_min=8, fund_balance=10023.00, return_pct=0.23,
    open_fund=6, open_research=63, daily_pnl=0.15, drawdown_pct=0.0,
    win_rate=42, decisive=89, sharpe=1.2, ftmo_pct=0.23,
)
print(f"  -> {ok(r)} to #system-health")
time.sleep(2)

print("Test 15: Circuit breaker...")
r = send_circuit_breaker(
    reason="Daily loss limit reached — fund down 2%",
    fund_balance=9823.00, daily_pnl_pct=-2.0, daily_pnl_dollars=-200.00,
    resets_at="Tuesday 23 June 6:05am Auckland",
)
print(f"  -> {ok(r)} to #critical")
time.sleep(2)

print("Test 16: Workflow failure...")
r = send_workflow_failure(
    workflow_name="Daily Forex Analysis",
    run_url="https://github.com/bodyheath/forex-ai/actions/runs/12345",
    scan_mode="full",
)
print(f"  -> {ok(r)} to #critical")
time.sleep(2)

print("Test 17: Monitor git push failed...")
r = _send_embed(
    WEBHOOK_CRITICAL,
    "Warning — Monitor Git Push FAILED",
    "Trade state may not have saved to GitHub",
    COLOR_WARNING,
    fields=[
        {"name": "Run URL", "value": "[View failed run](https://github.com/bodyheath/forex-ai/actions/runs/12345)", "inline": False},
        {"name": "Action",  "value": "Data preserved in memory\nWill retry on next run", "inline": False},
    ],
)
print(f"  -> {ok(r)} to #critical")
time.sleep(2)

print("Test 18: Monitor gap alert...")
r = send_monitor_gap_alert(gap_minutes=127, last_run_time="2026-06-22 07:01 UTC")
print(f"  -> {ok(r)} to #critical")

print()
print("All 18 test messages done!")
print("Check all 5 Discord channels:")
print("  #fund-alerts:   Tests 1-6")
print("  #monitor:       Tests 7-9 and 11")
print("  #research:      Test 10")
print("  #system-health: Tests 12-14")
print("  #critical:      Tests 15-18")
