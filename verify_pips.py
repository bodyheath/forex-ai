import pandas as pd, json, re, sys
sys.stdout.reconfigure(encoding='utf-8')

df = pd.read_csv('data/trades.csv')
fund = df[df.trade_this.astype(str)=='YES']
closed = fund[~fund.status.isin(['OPEN','PENDING'])].copy()
closed['_pips'] = pd.to_numeric(closed.pips, errors='coerce').fillna(0)

wins   = len(closed[closed._pips > 0])
losses = len(closed[closed._pips < 0])
dec    = wins + losses
wr     = wins/dec*100 if dec>0 else 0

with open('data/fund_state.json') as f:
    fs = json.load(f)

print('=== VERIFIED STATS ===')
print(f'Wins:    {wins}  (stored win_count={fs.get("win_count")})')
print(f'Losses:  {losses}  (stored loss_count={fs.get("loss_count")})')
print(f'WR:      {wr:.1f}%  (stored win_rate={fs.get("win_rate")})')
cl = fs.get('consecutive_losses', 0)
cw = fs.get('consecutive_wins', 0)
print(f'Streak:  {cl} losses, {cw} wins')
print(f'CB:      {"ACTIVE" if cl >= 3 else "clear (OK)"}')

print()
checks = [
    ('Pips cons_losses in financials', r'pips_v < 0', 'src/trading/financials.py', True),
    ('Pips win rate in financials',    r'_all_pips.*to_numeric', 'src/trading/financials.py', True),
    ('win_count in return dict',       r'"win_count"', 'src/trading/financials.py', True),
    ('Pips masks in daily.py',         r'_dsc_wins_m', 'daily.py', True),
    ('Old _dsc_wm removed from daily', r'_dsc_wm\s*=\s*_dsc', 'daily.py', False),
    ('wrows uses wins_m',              r'_dsc_wrows.*_dsc_wins_m', 'daily.py', True),
]
all_ok = True
for name, pat, fname, expect in checks:
    try:
        with open(fname, encoding='utf-8-sig') as f:
            content = f.read()
        found = bool(re.search(pat, content))
    except FileNotFoundError:
        found = False
    ok = found == expect
    if not ok:
        all_ok = False
    print(f'  {"OK" if ok else "FAIL"}  {name}')
print()
print('ALL PASS' if all_ok else 'SOME FAILED')
