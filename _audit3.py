import csv

with open('data/trades.csv', encoding='utf-8-sig', newline='') as f:
    rows = [r for r in csv.DictReader(f) if r.get('trade_this') == 'YES']

print(f'Total YES trades: {len(rows)}')
print()
for r in rows:
    status = r.get('status', '')
    pair = r.get('pair', '')
    pips = r.get('pips', '')
    cascw = r.get('cascading_total_pips_weighted', '')
    t1h = r.get('t1_hit', '')
    t2h = r.get('t2_hit', '')
    closed = r.get('closed_at', '')
    print(f"id={r.get('id'):>5}  {pair:<10}  status={status:<12}  pips={pips:<10}  "
          f"casc_w={cascw:<8}  t1h={t1h}  t2h={t2h}  closed={closed[:10] if closed else ''}")

# Also recalculate stats exactly as monitor.py does
import math
st_wins = st_losses = st_partial = st_breakeven = 0
win_pips = []
loss_pips = []
best = 0.0
best_pair = ""

for r in rows:
    out = r.get('status', '').upper()
    raw = (r.get('cascading_total_pips_weighted') or r.get('cascading_total_pips') or r.get('pips') or '')
    try:
        fp = float(raw) if raw != '' else 0.0
    except:
        fp = 0.0
    if math.isnan(fp):
        fp = 0.0

    if out in ('WIN', 'FULL_WIN'):
        st_wins += 1; win_pips.append(fp)
    elif out == 'PARTIAL_WIN':
        st_partial += 1; win_pips.append(fp)
    elif out == 'BREAKEVEN':
        st_breakeven += 1
    elif out in ('LOSS', 'EXPIRED', 'EXPIRED_LOSS', 'STALE_EXIT'):
        if fp > 0:
            st_partial += 1; win_pips.append(fp)
        else:
            st_losses += 1; loss_pips.append(abs(fp))
    if fp > best:
        best = fp; best_pair = r.get('pair', '')

decisive = st_wins + st_losses + st_partial
wr = (st_wins + st_partial) / decisive * 100 if decisive else 0
print(f'\nSTATISTICS:')
print(f'  wins={st_wins}  protected={st_partial}  losses={st_losses}  breakeven={st_breakeven}')
print(f'  decisive={decisive}  win_rate={wr:.1f}%')
print(f'  best: {best_pair} {best:.1f}p')
