"""Part 7 verification: confirm all fixes are in place."""
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from src.trading import financials as fin

PASS = 'PASS'
FAIL = 'FAIL'
issues = []

def chk(label, condition, detail=''):
    status = PASS if condition else FAIL
    print(f'[{status}] {label}' + (f': {detail}' if detail else ''))
    if not condition:
        issues.append(f'{label}: {detail}')

print('=' * 60)
print('VERIFY 1 — financials.py module loads')
print('=' * 60)
try:
    assert callable(fin.safe_float)
    assert callable(fin.calculate_dpp)
    assert callable(fin.calculate_pnl)
    assert callable(fin.close_fund_trade)
    assert callable(fin.calculate_fund_state)
    assert callable(fin.sync_fund_state_json)
    assert callable(fin.verify_trade_integrity)
    chk('financials.py imports and all functions present', True)
except Exception as e:
    chk('financials.py imports and all functions present', False, str(e))

print()
print('=' * 60)
print('VERIFY 2 — safe_float guarantees')
print('=' * 60)
for v, expected in [
    (float('nan'), 0.0), (float('inf'), 0.0), (None, 0.0),
    ('bad', 0.0), (1.5, 1.5), ('1.5', 1.5),
]:
    result = fin.safe_float(v)
    chk(f'safe_float({v!r})=={expected}', result == expected, f'got {result}')

print()
print('=' * 60)
print('VERIFY 3 — pip_size correctness')
print('=' * 60)
for pair, expected in [
    ('USD/JPY', 0.01), ('EUR/JPY', 0.01), ('AUD/NZD', 0.0001),
    ('GBP/SEK', 0.0001), ('USD/HKD', 0.0001), ('EUR/NOK', 0.0001),
    ('USD/SGD', 0.0001), ('EUR/USD', 0.0001),
]:
    result = fin.pip_size(pair)
    chk(f'pip_size({pair})=={expected}', result == expected, f'got {result}')

print()
print('=' * 60)
print('VERIFY 4 — calculate_dpp formula')
print('=' * 60)
dpp = fin.calculate_dpp(10000.0, 1.0, 100.0)
chk('dpp(10000,1%,100p)==1.00', abs(dpp - 1.00) < 0.001, f'got {dpp}')
dpp2 = fin.calculate_dpp(10000.0, 1.0, 70.0)
chk('dpp(10000,1%,70p)==1.4286', abs(dpp2 - 10000/100/70) < 0.0001, f'got {dpp2}')
chk('dpp with 0 stop==0.0', fin.calculate_dpp(10000, 1, 0) == 0.0)
chk('dpp never NaN', not math.isnan(fin.calculate_dpp(0, 0, 0)))

print()
print('=' * 60)
print('VERIFY 5 — trade #1324 fixed')
print('=' * 60)
df = pd.read_csv('data/trades.csv', encoding='utf-8-sig')
t1324 = df[df['id'] == 1324]
chk('#1324 exists', len(t1324) == 1)
if len(t1324) == 1:
    row = t1324.iloc[0]
    chk('#1324 status=PARTIAL_WIN', row['status'] == 'PARTIAL_WIN', f'got {row["status"]}')
    chk('#1324 exit_price=1.2189', abs(float(row['exit_price']) - 1.2189) < 0.0001, f'got {row["exit_price"]}')
    chk('#1324 pips=11.7', abs(float(row['pips']) - 11.7) < 0.1, f'got {row["pips"]}')
    chk('#1324 t1_hit=True', str(row['t1_hit']).upper() in ('TRUE', '1', 'YES'), f'got {row["t1_hit"]}')
    chk('#1324 t1_price=1.21598', abs(float(row['t1_price']) - 1.21598) < 0.0001, f'got {row["t1_price"]}')
    chk('#1324 pos_pct not NaN', float(row['position_size_pct_at_entry']) > 0, f'got {row["position_size_pct_at_entry"]}')
    ts = str(row['timestamp'])
    ts_dt = datetime.strptime(ts[:19], '%Y-%m-%d %H:%M:%S')
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    hours_ahead = (ts_dt - now_utc).total_seconds() / 3600
    chk('#1324 timestamp not in future', hours_ahead < 1.0, f'{ts} is {hours_ahead:.1f}h ahead')

print()
print('=' * 60)
print('VERIFY 6 — calculate_fund_state')
print('=' * 60)
prices = fin.load_prices()
state = fin.calculate_fund_state(df, prices)
chk('balance is float', isinstance(state['balance'], float))
chk('balance > 0', state['balance'] > 0, f'got {state["balance"]}')
chk('no NaN in state', all(not (isinstance(v, float) and math.isnan(v)) for v in state.values() if isinstance(v, (int, float))))
chk('daily_pnl_pct within FTMO limits', abs(state['daily_pnl_pct']) < 6.0, f'got {state["daily_pnl_pct"]}')
print(f'  Balance:       ${state["balance"]:,.2f}')
print(f'  Daily PnL:     ${state["daily_pnl_dollars"]:+.2f} ({state["daily_pnl_pct"]:+.4f}%)')
print(f'  Drawdown:      {state["current_drawdown_pct"]:.4f}%')
print(f'  Peak:          ${state["peak_balance"]:,.2f}')
print(f'  Open trades:   {len(state["open_trades"])}')

print()
print('=' * 60)
print('VERIFY 7 — sync_fund_state_json (atomic write + verify)')
print('=' * 60)
ok = fin.sync_fund_state_json(state)
chk('sync_fund_state_json returns True', ok, 'write failed')
if ok:
    fs_data = json.loads(Path('data/fund_state.json').read_text(encoding='utf-8'))
    chk('fund_state.json balance matches', abs(fs_data['balance'] - state['balance']) < 0.01)
    chk('fund_state.json no open_trades key', 'open_trades' not in fs_data)

print()
print('=' * 60)
print('VERIFY 8 — NZT bug fixes in daily.py')
print('=' * 60)
with open('daily.py', encoding='utf-8') as f:
    daily_content = f.read()
remaining_nzt = []
for i, line in enumerate(daily_content.splitlines(), 1):
    if 'datetime.now()' in line and 'utc' not in line.lower() and not line.strip().startswith('#'):
        remaining_nzt.append(f'  line {i}: {line.strip()}')
chk('daily.py: no datetime.now() without UTC', len(remaining_nzt) == 0,
    f'{len(remaining_nzt)} remaining: ' + (remaining_nzt[0] if remaining_nzt else ''))

print()
print('=' * 60)
print('VERIFY 9 — NZT bug fixes in online_learner.py')
print('=' * 60)
with open('src/online_learner.py', encoding='utf-8') as f:
    ol_content = f.read()
remaining_ol = []
for i, line in enumerate(ol_content.splitlines(), 1):
    if 'datetime.now()' in line and 'utc' not in line.lower() and not line.strip().startswith('#'):
        remaining_ol.append(f'  line {i}: {line.strip()}')
chk('online_learner.py: no datetime.now() without UTC', len(remaining_ol) == 0,
    f'{len(remaining_ol)} remaining: ' + (remaining_ol[0] if remaining_ol else ''))

print()
print('=' * 60)
print('VERIFY 10 — integrity check all fund trades')
print('=' * 60)
integrity_issues = fin.verify_trade_integrity(df)
nan_pct_issues = [i for i in integrity_issues if 'position_size_pct' in i]
ts_future_issues = [i for i in integrity_issues if 'future' in i]
other_issues = [i for i in integrity_issues if i not in nan_pct_issues and i not in ts_future_issues]
print(f'  NaN pos_pct issues (expected, using {fin.DEFAULT_RISK_PCT}% default): {len(nan_pct_issues)}')
print(f'  Future timestamp issues: {len(ts_future_issues)}')
print(f'  Other issues: {len(other_issues)}')
for iss in other_issues:
    print(f'    {iss}')
for iss in ts_future_issues:
    print(f'    {iss}')
chk('No future timestamps', len(ts_future_issues) == 0)
chk('No other structural issues', len(other_issues) == 0)

print()
print('=' * 60)
print('VERIFY 11 — open trades P&L detail')
print('=' * 60)
fund = df[df['trade_this'].astype(str) == 'YES']
open_fund = fund[fund['status'] == 'OPEN']
print(f'Open fund trades: {len(open_fund)}')
for _, row in open_fund.iterrows():
    pair = str(row['pair'])
    cp = fin.get_price(prices, pair)
    pnl = fin.calculate_pnl(
        pair=pair,
        direction=str(row['direction']),
        entry=row['entry'], stop_loss=row['stop_loss'],
        pos_pct=row['position_size_pct_at_entry'],
        balance=state['balance'],
        t1_price=row.get('t1_price'), t2_price=row.get('t2_price'),
        t3_price=row.get('t3_price'), t1_hit=row.get('t1_hit'),
        t2_hit=row.get('t2_hit'), t3_hit=row.get('t3_hit'),
        status='OPEN', exit_price=None, current_price=cp,
    )
    ts = str(row.get('timestamp', ''))
    ts_dt = datetime.strptime(ts[:19], '%Y-%m-%d %H:%M:%S') if ts and ts != 'nan' else None
    days = (datetime.now(timezone.utc).replace(tzinfo=None) - ts_dt).days if ts_dt else 0
    price_str = f'{cp:.5f}' if cp else 'NO_PRICE'
    print(f'  #{int(row["id"])} {pair} {row["direction"]}: '
          f'price={price_str}  pips={pnl["pips_unrealised"]:+.1f}  '
          f'${pnl["dollars"]:+.2f}  progress={pnl["progress_pct"]:+.1f}%  '
          f'protected={pnl["is_protected"]}  days={days}')

print()
print('=' * 60)
if issues:
    print(f'VERIFICATION COMPLETE — {len(issues)} FAILURES:')
    for iss in issues:
        print(f'  FAIL: {iss}')
    sys.exit(1)
else:
    print('VERIFICATION COMPLETE — ALL CHECKS PASSED')
