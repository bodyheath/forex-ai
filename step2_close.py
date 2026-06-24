import pandas as pd
from pathlib import Path
from src.trading.financials import (
    close_fund_trade,
    calculate_fund_state,
    sync_fund_state_json,
    verify_trade_integrity,
    load_prices,
    TRADES_CSV,
)

print("=== BEFORE ===")
df = pd.read_csv(str(TRADES_CSV), encoding="utf-8-sig")
row = df[df["id"].astype(str) == "1208"]
if not row.empty:
    r = row.iloc[0]
    print(f"#1208 EUR/HKD: status={r.get('status')} entry={r.get('entry')} stop={r.get('stop_loss')}")
else:
    print("Trade #1208 NOT FOUND")

print()
print("Closing #1208 as LOSS at stop 8.90770, pips=-479.0...")
df2 = close_fund_trade(
    df=df,
    trade_id=1208,
    status="LOSS",
    exit_price=8.90770,
    pips=-479.0,
)

row2 = df2[df2["id"].astype(str) == "1208"]
if not row2.empty:
    r2 = row2.iloc[0]
    print(f"#1208 after close: status={r2.get('status')} exit={r2.get('exit_price')} pips={r2.get('pips')} closed_at={r2.get('closed_at')}")
else:
    print("Trade not found after close")

print()
print("Syncing fund_state.json...")
prices = load_prices()
state = calculate_fund_state(df2, prices)
ok = sync_fund_state_json(state)
print(f"Sync: {'OK' if ok else 'FAILED'}")
print(f"Balance: ${state.get('balance', 0):,.2f}")
print(f"Daily PnL: {state.get('daily_pnl_dollars', 0):+.2f}")
print(f"Consecutive losses: {state.get('consecutive_losses', 0)}")
print(f"Open trades: {state.get('open_count', 0)}")

print()
print("Verifying integrity...")
issues = verify_trade_integrity(df2)
if issues:
    for issue in issues:
        print(f"  ISSUE: {issue}")
else:
    print("  No integrity issues")

print()
print("=== FINAL OPEN FUND TRADES ===")
open_f = df2[(df2["trade_this"].astype(str) == "YES") & (df2["status"].astype(str) == "OPEN")]
print(f"Count: {len(open_f)}")
for _, t in open_f.iterrows():
    print(f"  #{int(t['id'])} {t['pair']} {t['direction']}")
