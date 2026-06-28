"""Fix all open trades:
1. Update T1/T2 to correct R:R floors (1R/1.5R) for all open trades
2. Fix NaN position sizes
3. Flag banned exotic pair
"""
import sys, io, shutil, pathlib
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import pandas as pd

CSV = pathlib.Path('data/trades.csv')
df = pd.read_csv(CSV)

BANNED_CCYS = {'hkd', 'nok', 'dkk', 'sgd', 'zar', 'mxn', 'try', 'huf', 'czk', 'pln', 'sek'}
changed = []

for idx, row in df.iterrows():
    status = str(row.get('status', '')).upper()
    if status not in ('OPEN', 'PENDING'):
        continue

    pair = str(row.get('pair', ''))
    direction = str(row.get('direction', '')).upper()
    entry = float(row.get('entry') or 0)
    stop  = float(row.get('stop_loss') or 0)
    target = row.get('target')
    tid = str(row.get('id', ''))

    is_banned = any(c in pair.lower() for c in BANNED_CCYS)
    if is_banned:
        print(f'  WARNING: #{tid} {pair} is a BANNED exotic pair — already open, cannot close programmatically')
        print(f'           Check if it was placed before the exotic ban was committed.')

    sd = abs(entry - stop)
    if sd <= 0 or not entry or not stop:
        print(f'  SKIP #{tid} {pair}: cannot compute (entry={entry} stop={stop})')
        continue

    if direction == 'BUY':
        correct_t1 = round(entry + sd, 5)
        correct_t2 = round(entry + sd * 1.5, 5)
        correct_t3 = round(entry + sd * 2.0, 5)
    elif direction == 'SELL':
        correct_t1 = round(entry - sd, 5)
        correct_t2 = round(entry - sd * 1.5, 5)
        correct_t3 = round(entry - sd * 2.0, 5)
    else:
        print(f'  SKIP #{tid} {pair}: unknown direction {direction}')
        continue

    # Use analyst target as T3 if it already clears the 2R floor
    try:
        tgt_f = float(target)
        if direction == 'BUY' and tgt_f >= correct_t3:
            use_t3 = tgt_f
        elif direction == 'SELL' and tgt_f <= correct_t3:
            use_t3 = tgt_f
        else:
            use_t3 = correct_t3
    except (TypeError, ValueError):
        use_t3 = correct_t3

    old_t1 = row.get('t1_price')
    old_t2 = row.get('t2_price')

    df.at[idx, 't1_price'] = correct_t1
    df.at[idx, 't2_price'] = correct_t2
    if pd.isna(row.get('t3_price')):
        df.at[idx, 't3_price'] = use_t3
    changed.append(f'  #{tid} {pair} {direction}: t1 {old_t1}->{correct_t1}, t2 {old_t2}->{correct_t2}')

    # Fix NaN position size
    pos = row.get('position_size_pct_at_entry')
    try:
        pv = float(pos)
        if pd.isna(pv) or pv <= 0:
            df.at[idx, 'position_size_pct_at_entry'] = 0.75
            changed.append(f'  #{tid} {pair}: pos_size nan->0.75')
    except (TypeError, ValueError):
        df.at[idx, 'position_size_pct_at_entry'] = 0.75
        changed.append(f'  #{tid} {pair}: pos_size nan->0.75')

print(f'Changes:')
for c in changed:
    print(c)

tmp = CSV.with_suffix('.tmp')
df.to_csv(tmp, index=False)
shutil.move(str(tmp), str(CSV))
print(f'\nSaved. {len(changed)} changes written.')
