import subprocess, json, os, math
import pandas as pd

print('=== GIT STATUS ===')
r = subprocess.run(['git', 'log', '--oneline', '-5'], capture_output=True, text=True)
print(r.stdout.strip())
r2 = subprocess.run(['git', 'status', '--short'], capture_output=True, text=True)
uncommitted = r2.stdout.strip() or 'none'
print('Uncommitted:', uncommitted)

print()
print('=== WORKFLOW -X OURS CHECK ===')
for wf in ['daily.yml', 'intraday.yml', 'monitor.yml']:
    path = f'.github/workflows/{wf}'
    with open(path) as f:
        content = f.read()
    theirs = '-X theirs' in content
    ours   = '-X ours'   in content
    ok_str  = 'OK -X ours'      if ours   else 'MISSING -X ours'
    bad_str = '  STILL HAS -X theirs!' if theirs else ''
    print(f'  {wf}: {ok_str}{bad_str}')

print()
print('=== WEBHOOKS IN WORKFLOWS ===')
for wf in ['daily.yml', 'intraday.yml', 'monitor.yml']:
    path = f'.github/workflows/{wf}'
    with open(path) as f:
        content = f.read()
    count = content.count('DISCORD_WEBHOOK')
    ok = 'OK' if count >= 5 else 'LOW'
    print(f'  {wf}: {ok} ({count} webhook refs)')

print()
print('=== CRITICAL DATA FILES ===')
files = [
    ('data/trades.csv',              'trades'),
    ('data/fund_state.json',         'fund state'),
    ('data/discord_dashboard.json',  'discord ids'),
    ('data/research_trades.csv',     'research'),
    ('data/online_model_meta.json',  'ML meta'),
    ('data/trend_cache.json',        'trend cache'),
]
for path, name in files:
    exists = os.path.exists(path)
    size   = os.path.getsize(path) if exists else 0
    status = 'OK' if exists else 'MISSING'
    print(f'  {status}  {name}: {size:,} bytes')

print()
print('=== FUND STATE ===')
with open('data/fund_state.json') as f:
    fs = json.load(f)
bal = fs.get('balance') or (fs.get('daily_opening_balance', 0) + fs.get('daily_pnl_dollars', 0))
print(f'  balance:              ${bal:,.2f}')
print(f'  daily_opening_balance:${fs.get("daily_opening_balance", 0):,.2f}')
print(f'  daily_pnl_dollars:    ${fs.get("daily_pnl_dollars", 0):,.2f}')
print(f'  daily_pnl_pct:        {fs.get("daily_pnl_pct", 0):.2f}%')
print(f'  current_drawdown_pct: {fs.get("current_drawdown_pct", 0):.2f}%')
print(f'  consecutive_losses:   {fs.get("consecutive_losses", 0)}')
print(f'  consecutive_wins:     {fs.get("consecutive_wins", 0)}')
print(f'  sizing_mode:          {fs.get("sizing_mode", "?")}')
print(f'  current_sizing_pct:   {fs.get("current_sizing_pct", "?")}')

print()
print('=== OPEN FUND TRADES ===')
df = pd.read_csv('data/trades.csv', encoding='utf-8-sig')
fund_open = df[(df['trade_this'].astype(str) == 'YES') & (df['status'] == 'OPEN')]
print(f'  Open: {len(fund_open)}/4')
for _, t in fund_open.iterrows():
    pos = t.get('position_size_pct_at_entry', '')
    try:
        nan_flag = ' [NaN pos_size]' if (str(pos) in ('nan', '') or (isinstance(pos, float) and math.isnan(pos))) else ''
    except Exception:
        nan_flag = ''
    print(f'  #{t["id"]:>4}  {t["pair"]:<12} {t["direction"]}  stop={t.get("stop_loss")}{nan_flag}')

print()
print('=== DISCORD MESSAGE IDs ===')
with open('data/discord_dashboard.json') as f:
    d = json.load(f)
fid = d.get('fund_dashboard_message_id')
cid = d.get('closed_trades_message_id')
fid_str = (str(fid)[:14] + '...') if fid else 'NULL - first run will create'
cid_str = (str(cid)[:14] + '...') if cid else 'NULL - will post new'
print(f'  Dashboard msg:      {fid_str}')
print(f'  Closed trades msg:  {cid_str}')

print()
print('=== ML STATUS ===')
ml_path = 'data/online_model_meta.json'
if os.path.exists(ml_path):
    with open(ml_path) as f:
        ml = json.load(f)
    n   = ml.get('n_decisive', ml.get('n_samples', ml.get('n_trades', 0)))
    rwr = ml.get('recent_win_rate', 0)
    owr = ml.get('overall_win_rate', 0)
    last = ml.get('last_updated', '?')
    src  = ml.get('retrain_source', '?')
    active = n >= 30
    print(f'  n_decisive:       {n}')
    print(f'  recent_win_rate:  {rwr:.1%}')
    print(f'  overall_win_rate: {owr:.1%}')
    print(f'  last_updated:     {last}')
    print(f'  retrain_source:   {src}')
    print(f'  active (>=30):    {active}')
else:
    print('  MISSING - no model file')

pkl_exists = os.path.exists('data/online_model.pkl')
feat_exists = os.path.exists('data/trade_features.csv')
print(f'  online_model.pkl:  {"OK" if pkl_exists else "MISSING"}')
print(f'  trade_features.csv:{"OK" if feat_exists else "MISSING"}')
if feat_exists:
    ft = pd.read_csv('data/trade_features.csv', encoding='utf-8-sig')
    print(f'  feature rows:     {len(ft)}')

print()
print('=== SYNTAX CHECK ===')
for fname in ['daily.py', 'src/monitor.py', 'src/discord_notifier.py', 'src/online_learner.py']:
    r = subprocess.run(['python', '-m', 'py_compile', fname], capture_output=True, text=True)
    status = 'OK' if r.returncode == 0 else 'FAIL'
    err    = r.stderr.strip()[:100] if r.stderr else ''
    suffix = ('  -- ' + err) if err else ''
    print(f'  {status}  {fname}{suffix}')

print()
print('=== CONCENTRATION CHECK ===')
fund_pairs = fund_open['pair'].astype(str)
over_limit = False
for ccy in ['HKD', 'USD', 'EUR', 'GBP', 'JPY', 'AUD', 'NZD', 'CAD', 'CHF', 'SEK']:
    cnt = int(fund_pairs.str.contains(ccy).sum())
    if cnt > 0:
        flag = '  OVER LIMIT!' if cnt > 2 else ''
        if cnt > 2:
            over_limit = True
        print(f'  {ccy}: {cnt}/2{flag}')
if not over_limit:
    print('  All currencies within 2-trade limit')

print()
print('=== RESEARCH SUMMARY ===')
rt = pd.read_csv('data/research_trades.csv', encoding='utf-8-sig')
rt_open   = (rt['status'] == 'OPEN').sum()
rt_closed = (rt['status'] != 'OPEN').sum()
rt_cls    = rt[rt['status'] != 'OPEN']
rt_pip_n  = pd.to_numeric(rt_cls['pips'], errors='coerce').fillna(0)
rt_wins   = rt_cls[
    rt_cls['status'].str.upper().isin(['WIN','FULL_WIN','PARTIAL_WIN']) |
    (rt_cls['status'].str.upper().isin(['EXPIRED']) & (rt_pip_n > 0))
]
rt_losses = rt_cls[
    rt_cls['status'].str.upper().isin(['LOSS']) |
    (rt_cls['status'].str.upper().isin(['EXPIRED']) & (rt_pip_n <= 0))
]
rt_dec = len(rt_wins) + len(rt_losses)
rt_wr  = len(rt_wins) / rt_dec * 100 if rt_dec > 0 else 0
print(f'  Total research trades: {len(rt)}')
print(f'  Open: {rt_open}  Closed: {rt_closed}')
print(f'  Decisive: {rt_dec}  Wins: {len(rt_wins)}  Losses: {len(rt_losses)}')
print(f'  Win rate: {rt_wr:.1f}%')

print()
print('=== TOMORROW SCHEDULE (Auckland NZST) ===')
print('  06:05  6am Full Scan      — full universe, all 8 checks')
print('  09:05  9am Morning Scan   — London open')
print('  17:05  5pm Pre-London     — European session setup')
print('  23:05  11pm Pre-New York  — NY session setup')
print('  Every 30min — Monitor     — stop/target checks')

print()
print('=== READY FOR TOMORROW? ===')
issues = []
if '-X theirs' in open('.github/workflows/daily.yml').read():
    issues.append('daily.yml still has -X theirs')
if '-X theirs' in open('.github/workflows/monitor.yml').read():
    issues.append('monitor.yml still has -X theirs')
if len(fund_open) > 4:
    issues.append(f'Too many open fund trades: {len(fund_open)}')
n_ml = ml.get('n_decisive', 0) if os.path.exists(ml_path) else 0
if n_ml == 0:
    issues.append('ML model untrained')
if issues:
    print('  ISSUES:')
    for i in issues:
        print(f'    - {i}')
else:
    print('  All checks passed - system ready')
