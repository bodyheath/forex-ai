import os, sys, time
sys.path.insert(0, '.')

_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env):
    with open(_env, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

from src.discord_notifier import send_master_scan_report

print("Test 1: 6am Full Scan — 2 new alerts, 2 open fund trades, 3 closed...")
r = send_master_scan_report(
    scan_mode="full",
    date="Monday 22 June 2026",
    auckland_time="06:05 NZST",
    threshold=7.0,
    regime="Trending Risk-On",
    vix=18.5,
    fund_balance=10023.45,
    fund_return_pct=0.23,
    fund_peak=10050.00,
    drawdown_pct=0.26,
    daily_pnl_pct=0.23,
    daily_pnl_dollars=23.45,
    consecutive_wins=2,
    consecutive_losses=0,
    sizing_mode="standard",
    risk_pct=1.0,
    open_fund_trades=[
        {
            "pair": "AUD/JPY", "direction": "BUY",
            "t1_hit": True, "t2_hit": False,
            "next_target": "T2", "progress_pct": 74,
            "pips_unrealised": 22.8, "dollars_unrealised": 35.20, "days_open": 1,
        },
        {
            "pair": "EUR/HKD", "direction": "BUY",
            "t1_hit": False, "t2_hit": False,
            "next_target": "T1", "progress_pct": 15,
            "pips_unrealised": 8.1, "dollars_unrealised": 10.50, "days_open": 0,
        },
    ],
    new_fund_alerts=[
        {
            "pair": "USD/CHF", "direction": "SELL", "conf": 7,
            "entry": 0.80640, "stop": 0.81060, "t1": 0.79800, "rr": 2.1, "checklist": 8,
        },
        {
            "pair": "EUR/USD", "direction": "BUY", "conf": 6,
            "entry": 1.08900, "stop": 1.08400, "t1": 1.09900, "rr": 2.0, "checklist": 7,
        },
    ],
    newly_closed_trades=[
        {"pair": "GBP/CAD", "outcome": "PARTIAL_WIN", "pips": 28.0,  "dollars": 35.00},
        {"pair": "AUD/CAD", "outcome": "LOSS",         "pips": -38.0, "dollars": -47.50},
        {"pair": "USD/SEK", "outcome": "FULL_WIN",     "pips": 57.0,  "dollars": 71.25},
    ],
    research_open=63,
    research_closed=312,
    research_decisive=89,
    research_win_rate=42.0,
    research_profit_factor=0.89,
    research_avg_win_pips=45.2,
    research_avg_loss_pips=38.1,
    research_best_trade="AUD/JPY +154p",
    research_worst_trade="EUR/GBP -92p",
    recent_research_milestones=[
        {"pair": "AUD/JPY", "milestone": "T2",   "pips": 45.5,  "outcome": "PARTIAL_WIN"},
        {"pair": "EUR/AUD", "milestone": "T1",   "pips": 28.2,  "outcome": "PARTIAL_WIN"},
        {"pair": "CAD/NOK", "milestone": "STOP", "pips": -38.0, "outcome": "LOSS"},
    ],
    universe_size=1399,
    pairs_analysed=22,
    watch_list_pairs=[
        {"pair": "GBP/USD", "direction": "BUY",  "conf": 6, "grade": "B", "reason": "Strong bullish momentum off 4H support"},
        {"pair": "AUD/NZD", "direction": "SELL", "conf": 5, "grade": "C", "reason": "Resistance confluence at 1.0920"},
    ],
    approaching_signals=[],
    data_quality_pct=80.0,
    td_calls_used=31,
    cot_status="7/8 currencies",
    calendar_status="Live data",
    fred_status="OK",
    yahoo_status="OK",
    ml_gate_status="Active",
    ml_auc=0.763,
    ml_decisive_count=89,
    online_model_updates=3,
    ftmo_target_pct=10.0,
    ftmo_current_pct=0.23,
    ftmo_daily_limit_pct=5.0,
    sharpe_ratio=1.24,
    last_monitor_gap_min=7.0,
    scan_minutes=45.2,
    scan_cost_usd=0.0912,
    monitor_sources="TD/OHLCV+FX+macro",
)
print(f"  -> {'Sent' if r else 'FAILED (no webhook?)'} to #system-health")
time.sleep(3)

print("Test 2: 5pm Pre-London — no new alerts, 1 open fund trade...")
r = send_master_scan_report(
    scan_mode="prelondon",
    date="Monday 22 June 2026",
    auckland_time="17:05 NZST",
    threshold=7.0,
    regime="Ranging",
    vix=0,
    fund_balance=10023.45,
    fund_return_pct=0.23,
    fund_peak=10050.00,
    drawdown_pct=0.26,
    daily_pnl_pct=0.23,
    daily_pnl_dollars=23.45,
    consecutive_wins=2,
    consecutive_losses=0,
    sizing_mode="standard",
    risk_pct=1.0,
    open_fund_trades=[
        {
            "pair": "AUD/JPY", "direction": "BUY",
            "t1_hit": True, "t2_hit": False,
            "next_target": "T2", "progress_pct": 85,
            "pips_unrealised": 31.2, "dollars_unrealised": 48.10, "days_open": 1,
        },
    ],
    new_fund_alerts=[],
    newly_closed_trades=[],
    research_open=61,
    research_closed=312,
    research_decisive=89,
    research_win_rate=42.0,
    research_profit_factor=0.89,
    research_avg_win_pips=45.2,
    research_avg_loss_pips=38.1,
    research_best_trade="",
    research_worst_trade="",
    recent_research_milestones=[],
    universe_size=1399,
    pairs_analysed=18,
    watch_list_pairs=[],
    approaching_signals=[],
    data_quality_pct=75.0,
    td_calls_used=62,
    cot_status="Unavailable",
    calendar_status="Live data",
    fred_status="OK",
    yahoo_status="OK",
    ml_gate_status="Active",
    ml_auc=0.763,
    ml_decisive_count=89,
    online_model_updates=0,
    ftmo_target_pct=10.0,
    ftmo_current_pct=0.23,
    ftmo_daily_limit_pct=5.0,
    sharpe_ratio=1.24,
    last_monitor_gap_min=12.0,
    scan_minutes=32.1,
    scan_cost_usd=0.0612,
    monitor_sources="",
)
print(f"  -> {'Sent' if r else 'FAILED (no webhook?)'} to #system-health")
time.sleep(3)

print("Test 3: 11pm Pre-NY — daily loss, circuit breaker risk...")
r = send_master_scan_report(
    scan_mode="preny",
    date="Monday 22 June 2026",
    auckland_time="23:05 NZST",
    threshold=8.5,
    regime="High Volatility",
    vix=0,
    fund_balance=9823.00,
    fund_return_pct=-1.77,
    fund_peak=10050.00,
    drawdown_pct=2.26,
    daily_pnl_pct=-1.77,
    daily_pnl_dollars=-177.00,
    consecutive_wins=0,
    consecutive_losses=2,
    sizing_mode="reduced",
    risk_pct=0.5,
    open_fund_trades=[],
    new_fund_alerts=[],
    newly_closed_trades=[
        {"pair": "USD/CHF", "outcome": "LOSS", "pips": -42.0, "dollars": -52.10},
        {"pair": "EUR/USD", "outcome": "LOSS", "pips": -38.0, "dollars": -47.50},
    ],
    research_open=58,
    research_closed=318,
    research_decisive=91,
    research_win_rate=38.0,
    research_profit_factor=0.71,
    research_avg_win_pips=42.0,
    research_avg_loss_pips=40.5,
    research_best_trade="",
    research_worst_trade="",
    recent_research_milestones=[],
    universe_size=1399,
    pairs_analysed=16,
    watch_list_pairs=[],
    approaching_signals=[],
    data_quality_pct=68.0,
    td_calls_used=94,
    cot_status="Unavailable",
    calendar_status="Stale (5d)",
    fred_status="OK",
    yahoo_status="Timeout",
    ml_gate_status="Inactive (AUC<0.60)",
    ml_auc=0.581,
    ml_decisive_count=91,
    online_model_updates=0,
    ftmo_target_pct=10.0,
    ftmo_current_pct=-1.77,
    ftmo_daily_limit_pct=5.0,
    sharpe_ratio=-0.42,
    last_monitor_gap_min=25.0,
    scan_minutes=28.7,
    scan_cost_usd=0.0502,
    monitor_sources="",
)
print(f"  -> {'Sent' if r else 'FAILED (no webhook?)'} to #system-health")

print()
print("All 3 master scan report tests done!")
print("Check #system-health in Discord:")
print("  Test 1: Green (positive P&L) — 2 alerts, 2 open, 3 closed, watch list")
print("  Test 2: Green — no alerts, 1 open, pre-london minimal data")
print("  Test 3: Red (loss day) — no trades, daily loss, high threshold")
