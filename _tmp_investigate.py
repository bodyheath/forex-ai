import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import pandas as pd, json

df = pd.read_csv('data/trades.csv')
fund = df[df.trade_this.astype(str) == 'YES']
open_f = fund[fund.status.isin(['OPEN', 'PENDING'])]

print('=== ALL OPEN/PENDING FUND TRADES ===')
for _, t in open_f.iterrows():
    pair = str(t.get('pair', ''))
    entry = float(t.get('entry', 0) or 0)
    stop  = float(t.get('stop_loss', 0) or 0)
    t1    = float(t.get('t1_price', 0) or t.get('target', 0) or 0)
    t2    = float(t.get('t2_price', 0) or 0)
    pos   = t.get('position_size_pct_at_entry')
    direction = str(t.get('direction', '')).upper()
    ps    = 0.01 if 'JPY' in pair else 0.0001
    sd    = abs(entry - stop)
    sp    = sd / ps
    t1p   = abs(t1 - entry) / ps if t1 else 0
    rr    = t1p / sp if sp > 0 else 0
    banned_ccys = {'hkd', 'nok', 'dkk', 'sgd', 'zar', 'mxn', 'try', 'huf', 'czk', 'pln', 'sek'}
    is_banned = any(c in pair.lower() for c in banned_ccys)
    print(f'  #{t["id"]} {pair} {direction} status={t["status"]}')
    print(f'     entry={entry} stop={stop} ({sp:.0f}p) t1={t1} ({t1p:.0f}p, {rr:.2f}R)')
    print(f'     pos={pos}  conf={t.get("confidence")}  ts={str(t.get("timestamp",""))[:16]}')
    if is_banned:
        print(f'     !!! BANNED PAIR - should have been blocked !!!')
    print()

print('=== RECENT CLOSED TRADES (last 5) ===')
closed = fund[~fund.status.isin(['OPEN', 'PENDING'])].copy()
closed['_tid'] = pd.to_numeric(closed.id, errors='coerce').fillna(0)
recent = closed.nlargest(5, '_tid')
for _, t in recent.iterrows():
    print(f'  #{t["id"]} {t["pair"]} {t["direction"]} status={t["status"]} pips={t.get("pips")} ts={str(t.get("closed_at",""))[:16]}')

print()
print('=== FUND STATE ===')
with open('data/fund_state.json') as f:
    fs = json.load(f)
print(f'  balance:            ${fs.get("balance"):,.2f}')
print(f'  consecutive_losses: {fs.get("consecutive_losses")}')
print(f'  consecutive_wins:   {fs.get("consecutive_wins")}')
print(f'  drawdown_pct:       {fs.get("drawdown_pct")}%')
print(f'  circuit_breaker:    {fs.get("circuit_breaker_active")}')
print(f'  pause_until:        {fs.get("pause_until")}')
