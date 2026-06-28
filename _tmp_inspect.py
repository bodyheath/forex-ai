import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import pandas as pd

df = pd.read_csv('data/trades.csv')
fund = df[df.trade_this.astype(str) == 'YES']
open_f = fund[fund.status.isin(['OPEN', 'PENDING'])]

print('=== OPEN TRADES ===')
for _, t in open_f.iterrows():
    entry = float(t.get('entry') or 0)
    stop  = float(t.get('stop_loss') or 0)
    t1    = t.get('t1_price')
    t2    = t.get('t2_price')
    t3    = t.get('t3_price')
    tgt   = t.get('target')
    pos   = t.get('position_size_pct_at_entry')
    direction = str(t.get('direction', '')).upper()
    pair  = str(t.get('pair', ''))
    ps    = 0.01 if 'JPY' in pair else 0.0001
    stop_d = abs(entry - stop)
    stop_p = stop_d / ps
    print(f'ID={t["id"]} {pair} {direction}')
    print(f'  entry={entry} stop={stop} stop_dist={stop_d:.5f} ({stop_p:.0f}p)')
    print(f'  t1={t1}  t2={t2}  t3={t3}  target={tgt}')
    print(f'  pos_pct={pos}')
    # Compute correct R:R floors
    if stop_d > 0:
        if direction == 'BUY':
            ideal_t1 = round(entry + stop_d, 5)
            ideal_t2 = round(entry + stop_d * 1.5, 5)
            ideal_t3 = round(entry + stop_d * 2.0, 5)
        else:
            ideal_t1 = round(entry - stop_d, 5)
            ideal_t2 = round(entry - stop_d * 1.5, 5)
            ideal_t3 = round(entry - stop_d * 2.0, 5)
        print(f'  CORRECT floors: T1={ideal_t1} T2={ideal_t2} T3={ideal_t3}')
    print()

print('=== NaN POSITION SIZE TRADES ===')
import numpy as np
nan_trades = []
for _, t in fund.iterrows():
    v = t.get('position_size_pct_at_entry')
    try:
        f = float(v)
        if pd.isna(f) or f <= 0:
            nan_trades.append(t)
    except (TypeError, ValueError):
        nan_trades.append(t)

for t in nan_trades:
    print(f'  ID={t["id"]} {t["pair"]} status={t["status"]} pos={t.get("position_size_pct_at_entry")}')
