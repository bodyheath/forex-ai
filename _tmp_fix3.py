"""Fix NaN position sizes on closed trades and sync fund state."""
import sys, io, shutil, pathlib
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import pandas as pd

CSV = pathlib.Path('data/trades.csv')
df = pd.read_csv(CSV)
fund = df[df.trade_this.astype(str) == 'YES']

# Show closed trades with NaN pos size
SKIP = {'SKIPPED', 'BLOCKED', 'EXPIRED_BEFORE_ENTRY', 'CANCELLED'}
active = fund[~fund.status.isin(SKIP)]
print('Closed trades with NaN position size:')
changes = []
for idx, row in active.iterrows():
    v = row.get('position_size_pct_at_entry')
    try:
        fv = float(v)
        if pd.isna(fv) or fv <= 0:
            raise ValueError
    except (TypeError, ValueError):
        tid = str(row.get('id', ''))
        pair = str(row.get('pair', ''))
        status = str(row.get('status', ''))
        print(f'  #{tid} {pair} status={status} - setting to 1.0 (pre-optimization default)')
        df.at[idx, 'position_size_pct_at_entry'] = 1.0
        changes.append(tid)

if changes:
    tmp = CSV.with_suffix('.tmp')
    df.to_csv(tmp, index=False)
    shutil.move(str(tmp), str(CSV))
    print(f'Fixed {len(changes)} trades: {changes}')
else:
    print('  None found.')

# Sync fund state
print()
print('Syncing fund state...')
import sys as _sys
_sys.path.insert(0, '.')
from src.trading.financials import calculate_fund_state, sync_fund_state_json, load_prices
prices = load_prices()
state = calculate_fund_state(prices=prices)
ok = sync_fund_state_json(state)
print(f'Fund state synced: {"OK" if ok else "FAIL"}')
print(f'  Balance: ${state["balance"]:,.2f}')
print(f'  Opening: ${state["opening_balance"]:,.2f}')
print(f'  Drawdown: {state["drawdown_pct"]:.4f}%')
print(f'  Win rate: {state["win_rate"]:.1f}%')
print(f'  Consec losses: {state["consecutive_losses"]}')
