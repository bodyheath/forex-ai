import pandas as pd
import json

df = pd.read_csv('data/trades.csv')
eur_hkd = df[df.pair == 'EUR/HKD']
print(f'Total EUR/HKD rows: {len(eur_hkd)}')
for i, (_, row) in enumerate(eur_hkd.iterrows()):
    rid = row.get('id')
    print(f'\n--- Row {i+1} (id={rid}) ---')
    for col in df.columns:
        val = row.get(col)
        if str(val) not in ('nan', 'None', ''):
            print(f'  {col}: {repr(val)}')

print('\n\n=== fund_state.json ===')
with open('data/fund_state.json') as f:
    fs = json.load(f)
print(f'balance: {fs.get("balance")}')
print(f'daily_pnl_dollars: {fs.get("daily_pnl_dollars")}')
print(f'daily_pnl_pct: {fs.get("daily_pnl_pct")}')
print(f'weekly_opening_balance: {fs.get("weekly_opening_balance")}')
print(f'daily_opening_balance: {fs.get("daily_opening_balance")}')
print(f'peak_balance: {fs.get("peak_balance")}')
