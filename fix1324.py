"""Fix trade #1324 AUD/NZD SELL:
- Timestamp corrected from NZT to UTC
- T1 hit recorded (T1=1.21598, +29.2 pips)
- Status set to PARTIAL_WIN (T1 banked at 40%, remainder stopped at BE)
- exit_price = entry = 1.2189 (breakeven stop)
- pips = 11.7 (weighted: 29.2 * 0.40)
- position_size_pct_at_entry = 1.0 (default)
- closed_at = current UTC
"""
import shutil
import pandas as pd
from datetime import datetime, timezone

df = pd.read_csv('data/trades.csv', encoding='utf-8-sig')
idx = df.index[df['id'] == 1324]
if len(idx) == 0:
    print('ERROR: trade #1324 not found')
    exit(1)

i = idx[0]
now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

df.at[i, 'timestamp']                  = '2026-06-23 21:50:09'
df.at[i, 'status']                     = 'PARTIAL_WIN'
df.at[i, 'exit_price']                 = 1.2189
df.at[i, 'pips']                       = 11.7
df.at[i, 'closed_at']                  = now_utc
df.at[i, 'position_size_pct_at_entry'] = 1.0
df.at[i, 't1_hit']                     = 'TRUE'
df.at[i, 't1_price']                   = 1.21598
df.at[i, 't1_hit_pips']                = 29.2
df.at[i, 'effective_stop']             = 1.2189

# Atomic write
tmp = 'data/trades.tmp'
df.to_csv(tmp, index=False, encoding='utf-8')
shutil.move(tmp, 'data/trades.csv')

# Verify
df2 = pd.read_csv('data/trades.csv', encoding='utf-8-sig')
row = df2[df2['id'] == 1324].iloc[0]
print('Verification:')
for col in ['id', 'pair', 'direction', 'status', 'timestamp', 'entry',
            'exit_price', 'pips', 'closed_at', 'position_size_pct_at_entry',
            't1_hit', 't1_price', 't1_hit_pips', 'effective_stop']:
    if col in df2.columns:
        print(f'  {col}: {row[col]}')
print('Done.')
