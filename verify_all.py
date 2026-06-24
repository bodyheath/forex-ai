from src.trading.financials import (
    calculate_fund_state,
    sync_fund_state_json,
    verify_trade_integrity,
    load_prices,
)
import subprocess, pandas as pd, json

print('=' * 50)
print('1 — TRADE INTEGRITY')
print('=' * 50)
r = verify_trade_integrity()
if r['issue_count'] == 0:
    print(f'✅ All {r["trade_count"]} trades valid')
else:
    for iss in r['issues']:
        print(f'  ❌ #{iss["trade_id"]} {iss["pair"]}: {iss["issue"]}')

print()
print('=' * 50)
print('2 — FUND STATE')
print('=' * 50)
sync_fund_state_json()
state = calculate_fund_state(prices=load_prices())
if 'error' in state:
    print(f'ERROR: {state["error"]}')
else:
    print(f'Balance:    ${state["balance"]:,.2f}')
    print(f'Daily PnL:  {state["daily_pnl_pct"]:+.2f}%')
    print(f'Drawdown:   {state["drawdown_pct"]:.2f}%')
    print(f'Open:       {state["open_count"]}')
    print(f'Win rate:   {state["win_rate"]:.0f}%')
    print(f'Streak:     {state["consecutive_losses"]} L')
    print()
    daily = abs(state['daily_pnl_pct'])
    total = abs(state['drawdown_pct'])
    print(f'FTMO Daily: {daily:.2f}%/5% {"🚨" if daily>=5 else "⚠️" if daily>3.5 else "✅"}')
    print(f'FTMO Total: {total:.2f}%/10% {"🚨" if total>=10 else "⚠️" if total>7 else "✅"}')
    print()
    print('Open trades:')
    for t in state['open_trades']:
        print(f'  {t["pair"]} {t["direction"]} {t["pips"]:+.1f}p ${t["dollars"]:+.2f} {t["days_open"]}d risk=${t["risk_dollars"]:.0f}')

print()
print('=' * 50)
print('3 — CRITICAL STATE CHECKS')
print('=' * 50)
with open('data/fund_state.json') as f:
    fs = json.load(f)
ob = float(fs.get('opening_balance', 0))
cl = int(fs.get('consecutive_losses', 0))
print(f'Opening balance: ${ob:,.2f} {"✅" if ob <= 15000 else "❌ WRONG"}')
print(f'Consecutive losses: {cl} {"⚠️ CIRCUIT BREAKER ACTIVE" if cl>=3 else "✅"}')

df = pd.read_csv('data/trades.csv')
fund = df[df.trade_this.astype(str) == 'YES']
ghost_pairs = ['CHF/HKD', 'GBP/USD', 'EUR/SGD']
ghosts = fund[(fund.pair.isin(ghost_pairs)) & (fund.status.isin(['OPEN', 'PENDING']))]
print(f'Ghost trades: {len(ghosts)} {"✅" if len(ghosts)==0 else "❌"}')

open_f = fund[fund.status == 'OPEN']
print(f'Open count: {len(open_f)} (should be 1 — GBP/SEK)')

bad_ids = 0
for _, t in fund.iterrows():
    try:
        int(float(str(t.get('id', '') or 0)))
    except Exception:
        bad_ids += 1
print(f'Bad trade IDs: {bad_ids} {"✅" if bad_ids==0 else "❌"}')

print()
print('=' * 50)
print('4 — ALL FEATURE CHECKS')
print('=' * 50)
checks = [
    ('_safe_id crash fix',       r'_safe_id|_safe_int_id',           'src/monitor.py',               True),
    ('_log_line None fixed',     r'_log_line(None',                   'daily.py',                     False),
    ('100000 removed',           r'\b100000\b',                       'src/trading/financials.py',    False),
    ('logf removed',             r'\blogf\b',                         'daily.py',                     False),
    ('Tiered capacity',          r'OVERRIDE_MIN_CONF',                'daily.py',                     True),
    ('Smart swap system',        r'SWAP_MIN_NEW_CONF',                'daily.py',                     True),
    ('Keep score function',      r'_calculate_keep_score',            'daily.py',                     True),
    ('Swap target finder',       r'_find_swap_target',                'daily.py',                     True),
    ('Blocked setups tracked',   r'_blocked_setups',                  'daily.py',                     True),
    ('Swapped setups tracked',   r'_swapped_setups',                  'daily.py',                     True),
    ('Swap Discord alert',       r'send_swap_alert',                  'src/discord_notifier.py',      True),
    ('Trend alignment',          r'_get_trend_alignment',             'daily.py',                     True),
    ('Correlation filter',       r'_check_correlation',               'daily.py',                     True),
    ('Dynamic sizing',           r'_get_position_size',               'daily.py',                     True),
    ('Session filter',           r'_session_ok',                      'daily.py',                     True),
    ('News blackout',            r'_has_news_blackout',               'daily.py',                     True),
    ('Entry validation',         r'_validate_entry_price',            'daily.py',                     True),
    ('Trailing stop',            r'_update_trailing_stop',            'src/monitor.py',               True),
    ('Pending entries',          r'_check_pending_entries',           'src/monitor.py',               True),
    ('Close before alert',       r'close_fund_trade',                 'src/monitor.py',               True),
    ('Duplicate stop guard',     r'_already_sent_stop_alert',         'src/monitor.py',               True),
    ('Emergency close',          r'_emergency_close_trade',           'src/monitor.py',               True),
    ('Live dashboard',           r'build_fund_dashboard_embed',       'src/monitor.py',               True),
    ('Financials sync',          r'sync_fund_state_json',             'src/monitor.py',               True),
    ('NaN guard',                r'safe_pos_pct',                     'src/trading/financials.py',    True),
    ('FTMO daily metric',        r'daily_pnl_pct',                    'src/discord_notifier.py',      True),
    ('-X ours monitor',          r'-X ours',                          '.github/workflows/monitor.yml',True),
    ('-X ours daily',            r'-X ours',                          '.github/workflows/daily.yml',  True),
]

all_pass = True
for name, pattern, fname, expect in checks:
    r = subprocess.run(
        ['grep', '-rn', pattern, fname],
        capture_output=True, text=True
    )
    found = len(r.stdout.strip()) > 0
    ok = found == expect
    if not ok:
        all_pass = False
    icon = '✅' if ok else '❌'
    print(f'  {icon} {name}')

print()
print(f'All checks: {"✅ ALL PASS" if all_pass else "❌ SOME FAILED"}')

print()
print('=' * 50)
print('5 — POSITION SIZE CHECK')
print('=' * 50)
bad = 0
for _, t in fund.iterrows():
    val = t.get('position_size_pct_at_entry')
    try:
        fv = float(val or 0)
        if fv <= 0 or fv != fv:
            print(f'  ❌ #{t["id"]} {t["pair"]} NaN/zero')
            bad += 1
    except Exception:
        bad += 1
if bad == 0:
    print(f'  ✅ All {len(fund)} trades have valid position size')

print()
print('=' * 50)
print('6 — DISCORD MESSAGE IDs')
print('=' * 50)
try:
    with open('data/discord_dashboard.json') as f:
        dd = json.load(f)
    fid = dd.get('fund_dashboard_message_id')
    cid = dd.get('closed_trades_message_id')
    print(f'  Dashboard:     {"✅ " + str(fid)[:8] + "..." if fid else "❌ NULL"}')
    print(f'  Closed trades: {"✅ " + str(cid)[:8] + "..." if cid else "⚠️ NULL (will post new)"}')
except Exception as e:
    print(f'  Error: {e}')

print()
print('=' * 50)
print('7 — GIT STATUS')
print('=' * 50)
r = subprocess.run(['git', 'log', '--oneline', '-5'], capture_output=True, text=True)
print(r.stdout)
r2 = subprocess.run(['git', 'status', '--short'], capture_output=True, text=True)
uncommitted = r2.stdout.strip()
print(f'Uncommitted: {uncommitted or "none ✅"}')

print()
print('=' * 50)
print('SUMMARY')
print('=' * 50)
print(f'Balance:  ${fs.get("balance"):,.2f}')
print(f'Drawdown: {fs.get("drawdown_pct", 0):.2f}%')
print(f'Streak:   {cl} consecutive losses')
print(f'Open:     {len(open_f)} trade')
print(f'Circuit:  {"⚠️ ACTIVE" if cl >= 3 else "✅ OK"}')
print()
print('GBP/SEK needs to hit T1 at')
print('12.81234 to release circuit breaker')
print()
if all_pass:
    print('✅ SYSTEM READY FOR TOMORROW')
else:
    print('❌ FIX FAILURES BEFORE SLEEPING')
