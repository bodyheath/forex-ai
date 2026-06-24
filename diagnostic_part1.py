import pandas as pd
import json
import os
import subprocess
from datetime import datetime, timezone

STARTING_BALANCE = 10000.0

def safe_float(v, d=0.0):
    try:
        f = float(v if v is not None else d)
        return f if f == f else d
    except:
        return d

def pip_size(pair):
    return 0.01 if 'JPY' in str(pair) else 0.0001

# ─── SECTION 1 ───────────────────────────────────────────────
print('=' * 60)
print('SECTION 1 -- FUND TRADE INTEGRITY')
print('=' * 60)

df = pd.read_csv('data/trades.csv')
fund = df[df.trade_this.astype(str)=='YES']
open_f = fund[fund.status=='OPEN']
closed_f = fund[fund.status!='OPEN']

print(f'Total fund trades: {len(fund)}')
print(f'Open: {len(open_f)}')
print(f'Closed: {len(closed_f)}')
print()

all_issues = []

for _, t in fund.iterrows():
    tid = t.get('id','?')
    pair = str(t.get('pair',''))
    status = str(t.get('status',''))
    direction = str(t.get('direction',''))
    entry = safe_float(t.get('entry'))
    stop = safe_float(t.get('stop_loss'))
    exit_p = t.get('exit_price')
    pips = safe_float(t.get('pips'))
    closed_at = str(t.get('closed_at',''))
    t1_hit = t.get('t1_hit')
    t2_hit = t.get('t2_hit')
    pos_pct = t.get('position_size_pct_at_entry')
    ts = str(t.get('timestamp',''))
    outcome = str(t.get('outcome',''))
    t1_price = safe_float(t.get('t1_price'))

    issues = []

    if status == 'OPEN':
        ep = str(exit_p)
        if ep not in ('','nan','None','0','0.0'):
            issues.append(f'OPEN but exit_price={exit_p}')

    if status in ('CLOSED','WIN','LOSS','PARTIAL_WIN','FULL_WIN'):
        ep = str(exit_p)
        if ep in ('','nan','None','0','0.0'):
            issues.append(f'CLOSED but no exit_price')

    if status in ('CLOSED','WIN','LOSS','PARTIAL_WIN','FULL_WIN'):
        if closed_at in ('','nan','None'):
            issues.append(f'CLOSED but no closed_at')

    try:
        pp = safe_float(pos_pct, -1)
        if pp <= 0 or pp != pp:
            issues.append(f'Invalid pos_size={pos_pct}')
    except:
        issues.append(f'Cannot parse pos_size={pos_pct}')

    if entry <= 0:
        issues.append(f'Invalid entry={entry}')

    if entry > 0 and stop > 0:
        if direction=='BUY' and stop >= entry:
            issues.append(f'BUY stop {stop} >= entry {entry}')
        if direction=='SELL' and stop <= entry:
            issues.append(f'SELL stop {stop} <= entry {entry}')

    if ts and ts not in ('','nan','None'):
        try:
            dt = datetime.strptime(ts[:19],'%Y-%m-%d %H:%M:%S')
            now_n = datetime.now(timezone.utc).replace(tzinfo=None)
            diff_h = (now_n - dt).total_seconds()/3600
            if diff_h < -1:
                issues.append(f'Timestamp in future {diff_h:.1f}h -- NZT not UTC')
        except Exception as e:
            issues.append(f'Cannot parse timestamp {ts}: {e}')

    t1h = str(t1_hit).upper() in ('TRUE','YES','1','T')
    if t1h and t1_price <= 0:
        issues.append('T1 hit=True but no t1_price')

    if (status == 'OPEN' and outcome not in ('','nan','None','OPEN','open','running')):
        issues.append(f'OPEN but outcome={outcome}')

    if (closed_at not in ('','nan','None') and ts not in ('','nan','None')):
        try:
            dt_o = datetime.strptime(ts[:19],'%Y-%m-%d %H:%M:%S')
            dt_c = datetime.strptime(closed_at[:19],'%Y-%m-%d %H:%M:%S')
            hold_h = (dt_c-dt_o).total_seconds()/3600
            if hold_h < 0:
                issues.append(f'Negative hold {hold_h:.1f}h -- NZT/UTC mismatch')
        except:
            pass

    icon = 'OK' if not issues else 'ERR'
    print(f'[{icon}] #{tid} {pair} {direction} {status}')
    for iss in issues:
        print(f'   ISSUE: {iss}')
        all_issues.append(f'#{tid} {pair}: {iss}')

print()
print(f'Trade issues found: {len(all_issues)}')


# ─── SECTION 2 ───────────────────────────────────────────────
print()
print('=' * 60)
print('SECTION 2 -- FINANCIAL CALCULATION AUDIT')
print('=' * 60)

with open('data/fund_state.json') as f:
    fs = json.load(f)

stored_bal = safe_float(fs.get('balance'), STARTING_BALANCE)
stored_daily = safe_float(fs.get('daily_pnl_dollars'), 0)
stored_dd = safe_float(fs.get('drawdown_pct', fs.get('current_drawdown_pct', 0)), 0)
stored_peak = safe_float(fs.get('peak_balance'), STARTING_BALANCE)

print(f'Stored balance:    ${stored_bal:,.2f}')
print(f'Stored daily PnL:  ${stored_daily:+,.2f}')
print(f'Stored drawdown:   {stored_dd:.2f}%')
print(f'Stored peak:       ${stored_peak:,.2f}')
print()

realised = 0.0
running_bal = STARTING_BALANCE
try:
    closed_s = closed_f.sort_values('closed_at', na_position='last')
except:
    closed_s = closed_f

for _, t in closed_s.iterrows():
    pair = str(t.get('pair',''))
    entry = safe_float(t.get('entry'))
    stop = safe_float(t.get('stop_loss'))
    pips_v = safe_float(t.get('pips'))
    pos_pct = safe_float(t.get('position_size_pct_at_entry'), -1)
    if pos_pct <= 0 or pos_pct != pos_pct:
        pos_pct = 1.0
    ps = pip_size(pair)
    stop_p = (abs(entry-stop)/ps if entry and stop else 100)
    risk_d = running_bal * pos_pct / 100
    dpp = risk_d/stop_p if stop_p > 0 else 0
    dollars = pips_v * dpp
    status_v = str(t.get('status',''))
    if status_v == 'LOSS' and pips_v < 0:
        dollars = -(risk_d)
    realised += dollars
    running_bal = STARTING_BALANCE + realised
    print(f'  #{t["id"]} {pair} {status_v} {pips_v:+.1f}p ${dollars:+.2f} -> bal ${running_bal:,.2f}  (pos_pct={pos_pct}%)')

expected_bal = STARTING_BALANCE + realised
print()
print(f'Recalculated balance: ${expected_bal:,.2f}')
print(f'Stored balance:       ${stored_bal:,.2f}')
diff = stored_bal - expected_bal
if abs(diff) > 0.10:
    print(f'DISCREPANCY: ${diff:+.2f}')
else:
    print(f'Balance correct')


# ─── SECTION 3 ───────────────────────────────────────────────
print()
print('=' * 60)
print('SECTION 3 -- PRICE CACHE AUDIT')
print('=' * 60)

try:
    with open('data/price_cache.json') as f:
        raw_cache = json.load(f)

    print(f'Cache type: {type(raw_cache).__name__}')
    print(f'Top-level keys: {list(raw_cache.keys())[:5]}')

    if 'prices' in raw_cache:
        prices = raw_cache['prices']
        print(f'Nested format -- {len(prices)} pairs in prices')
        print(f'Sample: {list(prices.items())[:3]}')
    elif isinstance(raw_cache, dict):
        has_slash = any('/' in str(k) for k in raw_cache.keys())
        print(f'Has forex keys: {has_slash}')
        prices = raw_cache
    else:
        prices = {}

    print()
    print('Price lookup test:')
    for pair in ['EUR/USD','USD/CHF','GBP/SEK','CAD/HKD']:
        found = False
        for fmt in [
            pair,
            pair.replace('/',''),
            pair.replace('/','') + '=X',
            pair.replace('/','-'),
            pair.lower(),
            pair.replace('/','').lower(),
        ]:
            v = prices.get(fmt)
            if v:
                try:
                    f = float(v)
                    if f > 0:
                        print(f'  FOUND {pair} as {repr(fmt)}: {v}')
                        found = True
                        break
                except:
                    pass
        if not found:
            print(f'  NOT FOUND {pair} in any format')

except Exception as e:
    print(f'Price cache error: {e}')


# ─── SECTION 4 ───────────────────────────────────────────────
print()
print('=' * 60)
print('SECTION 4 -- TIMESTAMP AUDIT')
print('=' * 60)

for fname in ['daily.py', 'src/monitor.py', 'src/discord_notifier.py', 'src/online_learner.py', 'src/fund_state.py']:
    try:
        with open(fname, encoding='utf-8') as f:
            lines = f.readlines()
        issues = []
        for i, line in enumerate(lines, 1):
            if ('datetime.now()' in line and
                    'utc' not in line.lower() and
                    not line.strip().startswith('#')):
                issues.append(f'  line {i}: {line.strip()}')
        if issues:
            print(f'{fname} NZT bugs:')
            for iss in issues:
                print(iss)
        else:
            print(f'{fname}: OK no NZT bugs')
    except Exception as e:
        print(f'{fname}: ERROR {e}')


# ─── SECTION 5 ───────────────────────────────────────────────
print()
print('=' * 60)
print('SECTION 5 -- GIT WORKFLOW AUDIT')
print('=' * 60)

for wf in ['daily.yml', 'intraday.yml', 'monitor.yml']:
    path = f'.github/workflows/{wf}'
    with open(path) as f:
        content = f.read()
    checks = {
        '-X ours':          '-X ours' in content,
        '-X theirs':        '-X theirs' in content,
        'trades.csv':       'data/trades.csv' in content,
        'fund_state':       'fund_state.json' in content,
        'research_trades':  'research_trades.csv' in content,
        '3-attempt retry':  ('attempt' in content or 'for i in' in content),
    }
    print(f'{wf}:')
    for k, v in checks.items():
        if k == '-X theirs':
            icon = 'BAD' if v else 'OK'
        else:
            icon = 'OK' if v else 'MISSING'
        print(f'  [{icon}] {k}: {v}')


# ─── SECTION 6 ───────────────────────────────────────────────
print()
print('=' * 60)
print('SECTION 6 -- MONITOR CONSISTENCY (grep)')
print('=' * 60)

r2 = subprocess.run(
    ['grep', '-n',
     r'close.*trade|CLOSED|trade_this.*YES|fund_pairs|research.*fund|fund.*research',
     'src/monitor.py'],
    capture_output=True, text=True, shell=False
)
print(r2.stdout[:3000] if r2.stdout else '(no matches)')


# ─── SECTION 7 ───────────────────────────────────────────────
print()
print('SECTION 7 -- HEARTBEAT')
try:
    with open('data/heartbeat.json') as f:
        hb = json.load(f)
    print(json.dumps(hb, indent=2))
    for k, v in hb.items():
        if '2026' in str(v) or 'T' in str(v):
            try:
                ts_str = str(v).replace('Z','+00:00')
                dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                mins = (datetime.now(timezone.utc) - dt).total_seconds()/60
                print(f'{k}: {mins:.0f} minutes ago')
            except:
                pass
except Exception as e:
    print(f'Heartbeat error: {e}')
