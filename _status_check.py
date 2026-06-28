import pandas as pd, json

df = pd.read_csv('data/trades.csv')
fund = df[df.trade_this.astype(str)=='YES']
open_f = fund[fund.status=='OPEN']
closed = fund[~fund.status.isin(['OPEN','PENDING'])].copy()
closed['_pips'] = pd.to_numeric(closed.pips, errors='coerce').fillna(0)

print('=' * 50)
print('OPEN TRADES')
print('=' * 50)
banned = ['hkd','nok','dkk','sgd','zar','mxn','try']
for _, t in open_f.iterrows():
    pair = str(t.get('pair',''))
    is_banned = any(c in pair.lower() for c in banned)
    print(f'  #{t.get("id")} {pair} {t.get("direction")} '
          f'conf={t.get("confidence")} '
          f'opened={str(t.get("timestamp",""))[:16]} '
          f'{"BANNED" if is_banned else "OK"}')

print()
print('=' * 50)
print('CLOSED TRADES (all)')
print('=' * 50)
wins = 0
losses = 0
for _, t in closed.sort_values('closed_at').iterrows():
    pips = float(t._pips)
    status = str(t.get('status',''))
    if pips > 0: wins += 1
    elif pips < 0: losses += 1
    icon = 'WIN ' if pips > 0 else 'LOSS' if pips < 0 else 'FLAT'
    print(f'  {icon} #{t.get("id")} {t.get("pair")} [{status}] {pips:+.1f}p {str(t.get("closed_at",""))[:10]}')

dec = wins + losses
wr = wins/dec*100 if dec>0 else 0
print()
print(f'Wins: {wins}  Losses: {losses}  WR: {wr:.1f}%')

print()
print('=' * 50)
print('FUND STATE')
print('=' * 50)
with open('data/fund_state.json') as f:
    fs = json.load(f)
print(f'Balance:   ${fs.get("balance"):,.2f}')
print(f'Drawdown:  {fs.get("drawdown_pct"):.2f}%')
print(f'Streak:    {fs.get("consecutive_losses")} L / {fs.get("consecutive_wins")} W')
print(f'Opening:   ${fs.get("opening_balance"):,.2f}')
