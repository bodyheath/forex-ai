import json
import re
from datetime import datetime, timezone
from pathlib import Path

print('=' * 60)
print('SECTION 6 -- MONITOR CONSISTENCY')
print('=' * 60)

search_patterns = [
    r'close.*trade', r'CLOSED', r'trade_this.*YES',
    r'fund_pairs', r'research.*fund', r'fund.*research'
]
pattern = re.compile('|'.join(search_patterns), re.IGNORECASE)

with open('src/monitor.py', encoding='utf-8') as f:
    lines = f.readlines()
matches = [(i+1, l.rstrip()) for i, l in enumerate(lines) if pattern.search(l)]
for lineno, line in matches[:40]:
    print(f'  line {lineno}: {line[:120]}')

print()
print('SECTION 7 -- HEARTBEAT')
try:
    with open('data/heartbeat.json') as f:
        hb = json.load(f)
    print(json.dumps(hb, indent=2))
    for k, v in hb.items():
        if '2026' in str(v) or 'T' in str(v):
            try:
                ts_str = str(v).replace('Z', '+00:00')
                dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                mins = (datetime.now(timezone.utc) - dt).total_seconds() / 60
                print(f'{k}: {mins:.0f} minutes ago')
            except:
                pass
except Exception as e:
    print(f'Heartbeat error: {e}')

print()
print('=' * 60)
print('SECTION 8 -- OPEN TRADES DETAIL')
print('=' * 60)

import pandas as pd

df = pd.read_csv('data/trades.csv')
fund = df[df.trade_this.astype(str) == 'YES']
open_f = fund[fund.status == 'OPEN']

with open('data/price_cache.json') as f:
    cache = json.load(f)
prices = cache.get('prices', cache)

for _, t in open_f.iterrows():
    pair = str(t['pair'])
    entry = float(t['entry']) if str(t['entry']) not in ('nan','','None') else 0
    stop = float(t['stop_loss']) if str(t['stop_loss']) not in ('nan','','None') else 0
    direction = str(t['direction'])
    t1 = float(t['t1_price']) if str(t['t1_price']) not in ('nan','','None') else 0
    pos_pct = t['position_size_pct_at_entry']
    ts = str(t['timestamp'])
    print(f'  #{t["id"]} {pair} {direction}')
    print(f'    entry={entry} stop={stop} t1={t1}')
    print(f'    pos_pct={pos_pct}')
    print(f'    timestamp={ts}')
    price = prices.get(pair)
    print(f'    cached_price={price}')
    print()

print('=' * 60)
print('SECTION 9 -- CLOSED TRADES DETAIL')
print('=' * 60)

closed_f = fund[fund.status != 'OPEN']
for _, t in closed_f.iterrows():
    print(f'  #{t["id"]} {t["pair"]} {t["direction"]} {t["status"]}')
    print(f'    entry={t["entry"]} stop={t["stop_loss"]} exit={t["exit_price"]} pips={t["pips"]}')
    print(f'    pos_pct={t["position_size_pct_at_entry"]}')
    print(f'    closed_at={t["closed_at"]}')
    print()
