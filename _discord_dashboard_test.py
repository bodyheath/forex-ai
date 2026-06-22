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

from src.discord_notifier import update_fund_dashboard, DASHBOARD_STATE_FILE

# Clear any existing dashboard state so we get a fresh post
if DASHBOARD_STATE_FILE.exists():
    DASHBOARD_STATE_FILE.unlink()
    print("Cleared existing dashboard state — will post fresh message")

print()
print("Test 1: Posting initial dashboard (3 open trades, 1 recently closed)...")
r = update_fund_dashboard(
    open_fund_trades=[
        {
            "pair": "AUD/JPY", "direction": "BUY",
            "entry": 113.250, "current": 113.380,
            "stop": 112.680, "t1": 113.478, "t2": 113.649, "t3": 113.820,
            "t1_hit": False, "t2_hit": False, "t3_hit": False,
            "progress_pct": 74, "next_target": "T1",
            "pips_unrealised": 13.0, "dollars_unrealised": 20.10,
            "days_open": 2, "conf": 7, "checklist_score": 8, "id": "977",
        },
        {
            "pair": "EUR/HKD", "direction": "BUY",
            "entry": 8.9515, "current": 8.9680,
            "stop": 8.9515, "t1": 8.9682, "t2": 8.9786, "t3": 8.9890,
            "t1_hit": True, "t2_hit": True, "t3_hit": False,
            "progress_pct": 82, "next_target": "T3",
            "pips_unrealised": 110.0, "dollars_unrealised": 55.20,
            "days_open": 3, "conf": 8, "checklist_score": 9, "id": "1030",
        },
        {
            "pair": "USD/CHF", "direction": "SELL",
            "entry": 0.80640, "current": 0.80580,
            "stop": 0.81060, "t1": 0.80220, "t2": 0.79920, "t3": 0.79620,
            "t1_hit": False, "t2_hit": False, "t3_hit": False,
            "progress_pct": 14, "next_target": "T1",
            "pips_unrealised": 6.0, "dollars_unrealised": 7.43,
            "days_open": 1, "conf": 7, "checklist_score": 7, "id": "1073",
        },
    ],
    fund_balance=10023.00, fund_return_pct=0.23,
    daily_pnl_pct=0.15, daily_pnl_dollars=15.03,
    drawdown_pct=0.0, sizing_mode="Normal", risk_pct=1.0,
    consecutive_wins=1, consecutive_losses=0,
    ftmo_current_pct=0.23,
    recently_closed=[
        {"pair": "CAD/AUD", "direction": "BUY", "outcome": "EXPIRED",
         "pips": -35.6, "dollars": -43.70, "days_open": 5},
    ],
)
print(f"  -> {'Sent (new message)' if r else 'FAILED (no webhook?)'} to #fund-alerts")

# Check state file was created
if DASHBOARD_STATE_FILE.exists():
    import json
    state = json.loads(DASHBOARD_STATE_FILE.read_text(encoding="utf-8"))
    msg_id = state.get("fund_dashboard_message_id")
    print(f"  -> discord_dashboard.json created with message_id: {msg_id}")
else:
    print("  -> WARNING: discord_dashboard.json NOT created")

print()
time.sleep(3)

print("Test 2: Updating dashboard (AUD/JPY T1 hit, EUR/HKD closed) — should EDIT same message...")
r = update_fund_dashboard(
    open_fund_trades=[
        {
            "pair": "AUD/JPY", "direction": "BUY",
            "entry": 113.250, "current": 113.445,
            "stop": 113.250, "t1": 113.478, "t2": 113.649, "t3": 113.820,
            "t1_hit": True, "t2_hit": False, "t3_hit": False,
            "progress_pct": 45, "next_target": "T2",
            "pips_unrealised": 19.5, "dollars_unrealised": 30.12,
            "days_open": 2, "conf": 7, "checklist_score": 8, "id": "977",
        },
        {
            "pair": "USD/CHF", "direction": "SELL",
            "entry": 0.80640, "current": 0.80510,
            "stop": 0.81060, "t1": 0.80220, "t2": 0.79920, "t3": 0.79620,
            "t1_hit": False, "t2_hit": False, "t3_hit": False,
            "progress_pct": 30, "next_target": "T1",
            "pips_unrealised": 13.0, "dollars_unrealised": 16.10,
            "days_open": 1, "conf": 7, "checklist_score": 7, "id": "1073",
        },
    ],
    fund_balance=10038.00, fund_return_pct=0.38,
    daily_pnl_pct=0.23, daily_pnl_dollars=23.10,
    drawdown_pct=0.0, sizing_mode="Normal", risk_pct=1.0,
    consecutive_wins=2, consecutive_losses=0,
    ftmo_current_pct=0.38,
    recently_closed=[
        {"pair": "EUR/HKD", "direction": "BUY", "outcome": "PARTIAL_WIN",
         "pips": 82.5, "dollars": 67.40, "days_open": 3},
    ],
)
print(f"  -> {'Edited existing message' if r else 'FAILED (no webhook?)'}")
print()
print("Checks:")
print("  Test 1: New message posted to #fund-alerts with 3 open trades")
print("           EUR/HKD shows T1+T2 banked (shields), AUD/JPY 74% progress bar")
print("           USD/CHF 14% progress bar, recently closed CAD/AUD EXPIRED")
print("  Test 2: SAME message edited in Discord (not a new message)")
print("           AUD/JPY now shows T1 banked (1 shield), 45% to T2")
print("           EUR/HKD moved to Recently Closed as PARTIAL_WIN")
