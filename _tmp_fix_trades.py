"""Fix two open trade issues:
1. Trade #1342 NZD/USD: T1/T2 below R:R floor (pre-fix trade). Update to correct floors.
2. Trade #1357 USD/CHF: NaN position_size_pct_at_entry. Set to 0.75 (BASE_RISK_PCT).
"""
import sys, io, shutil, tempfile, os, pathlib
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import pandas as pd
import numpy as np

CSV = pathlib.Path('data/trades.csv')
df = pd.read_csv(CSV)

changed = []

for idx, row in df.iterrows():
    tid = str(row.get('id', ''))

    # Fix #1342 NZD/USD: T1/T2 to correct 1R/1.5R floors
    if tid == '1342' or str(row.get('id')) == '1342':
        entry = float(row['entry'])
        stop  = float(row['stop_loss'])
        direction = str(row.get('direction', '')).upper()
        sd = abs(entry - stop)
        if sd > 0 and direction == 'BUY':
            correct_t1 = round(entry + sd, 5)
            correct_t2 = round(entry + sd * 1.5, 5)
            # T3/target already at 0.578 (2.4R — above 2R floor, keep it)
            old_t1 = row.get('t1_price')
            old_t2 = row.get('t2_price')
            df.at[idx, 't1_price'] = correct_t1
            df.at[idx, 't2_price'] = correct_t2
            changed.append(f'  #1342 NZD/USD: t1 {old_t1}→{correct_t1}, t2 {old_t2}→{correct_t2}')

    # Fix #1357 USD/CHF: set position size
    if str(row.get('id')) == '1357':
        pos = row.get('position_size_pct_at_entry')
        try:
            v = float(pos)
            if pd.isna(v) or v <= 0:
                df.at[idx, 'position_size_pct_at_entry'] = 0.75
                changed.append(f'  #1357 USD/CHF: position_size_pct_at_entry nan→0.75')
        except (TypeError, ValueError):
            df.at[idx, 'position_size_pct_at_entry'] = 0.75
            changed.append(f'  #1357 USD/CHF: position_size_pct_at_entry nan→0.75')

if changed:
    tmp = CSV.with_suffix('.tmp')
    df.to_csv(tmp, index=False)
    shutil.move(str(tmp), str(CSV))
    print(f'Fixed {len(changed)} issues:')
    for c in changed:
        print(c)
else:
    print('No changes needed.')
