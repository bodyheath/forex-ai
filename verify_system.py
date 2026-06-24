import pandas as pd, json, subprocess
import re
from datetime import datetime, timezone
import sys
sys.stdout.reconfigure(encoding='utf-8')

print('=' * 55)
print('SECTION 1 — TRADE DATA INTEGRITY')
print('=' * 55)

df = pd.read_csv('data/trades.csv')
fund = df[df.trade_this.astype(str)=='YES']
open_f = fund[fund.status.isin(
    ['OPEN','PENDING'])]
closed_f = fund[~fund.status.isin(
    ['OPEN','PENDING'])]

print(f'Total fund trades: {len(fund)}')
print(f'Open/Pending:      {len(open_f)}')
print(f'Closed:            {len(closed_f)}')
print()

# Bad IDs
bad_ids = []
for _, t in fund.iterrows():
    try:
        int(float(str(t.get('id','') or 0)))
    except:
        bad_ids.append(t.get('pair'))
print(f'Bad trade IDs: {len(bad_ids)} '
      f'{"OK" if not bad_ids else "FAIL " + str(bad_ids)}')

# NaN position sizes
bad_pos = []
for _, t in fund.iterrows():
    val = t.get('position_size_pct_at_entry')
    try:
        f = float(val or 0)
        if f <= 0 or f != f:
            bad_pos.append(
                f'#{t.get("id")} {t.get("pair")}')
    except:
        bad_pos.append(
            f'#{t.get("id")} {t.get("pair")}')
print(f'NaN pos sizes: {len(bad_pos)} '
      f'{"OK" if not bad_pos else "FAIL " + str(bad_pos)}')

# Ghost trades
ghost_pairs = ['CHF/HKD','GBP/USD','EUR/SGD']
ghosts = fund[
    (fund.pair.isin(ghost_pairs)) &
    (fund.status.isin(['OPEN','PENDING']))]
print(f'Ghost trades:  {len(ghosts)} '
      f'{"OK" if len(ghosts)==0 else "FAIL still open"}')

# Negative days open
neg_days = []
for _, t in open_f.iterrows():
    try:
        ts = str(t.get('timestamp',''))
        if ts and ts not in ('nan','None',''):
            dt = datetime.fromisoformat(
                ts.replace('Z','+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(
                    tzinfo=timezone.utc)
            days = (
                datetime.now(timezone.utc)
                - dt).total_seconds() / 86400
            if days < 0:
                neg_days.append(
                    f'{t.get("pair")} '
                    f'{days:.1f}d')
    except:
        pass
print(f'Negative days: {len(neg_days)} '
      f'{"OK" if not neg_days else "FAIL " + str(neg_days)}')

print()
print('=' * 55)
print('SECTION 2 — FINANCIAL CALCULATIONS')
print('=' * 55)

closed_f = closed_f.copy()
closed_f['_pips'] = pd.to_numeric(
    closed_f.pips, errors='coerce').fillna(0)
closed_f['_nd'] = pd.to_numeric(
    closed_f['net_dollars']
    if 'net_dollars' in closed_f.columns
    else pd.Series([0]*len(closed_f), index=closed_f.index),
    errors='coerce').fillna(0)
closed_sorted = closed_f.sort_values(
    'closed_at', ascending=True,
    na_position='first')

from src.trading.financials import (
    calculate_fund_state, load_prices)
_live_state = calculate_fund_state(
    prices=load_prices())
drawdown = float(
    _live_state.get('drawdown_pct', 0))
peak = float(
    _live_state.get('peak_balance', 10000))
balance = float(
    _live_state.get('balance', 0))

# Win/loss by pips
wins = closed_f[closed_f._pips > 0]
losses = closed_f[closed_f._pips < 0]
decisive = len(wins) + len(losses)
wr = len(wins)/decisive*100 \
    if decisive > 0 else 0

# Full vs cascade wins
full_wins = wins[wins.status.isin(
    ['WIN','FULL_WIN','EXPIRED'])]
cascade_wins = wins[~wins.status.isin(
    ['WIN','FULL_WIN','EXPIRED'])]

# Avg pips (from pips column; dollars from financials)
avg_win_p = (wins._pips.mean()
             if len(wins) > 0 else 0)
avg_loss_p = (abs(losses._pips.mean())
              if len(losses) > 0 else 0)
avg_win_d  = float(_live_state.get('avg_win_dollars', 0))
avg_loss_d = float(_live_state.get('avg_loss_dollars', 0))
pf         = float(_live_state.get('profit_factor', 0))

# Net pips
net_pips = closed_f._pips.sum()

# Consecutive streak
cons_l = 0
cons_w = 0
for _, t in closed_sorted.iloc[::-1].iterrows():
    pips = float(t._pips)
    if cons_l == 0 and cons_w == 0:
        if pips < 0: cons_l = 1
        elif pips > 0: cons_w = 1
    elif cons_l > 0:
        if pips < 0: cons_l += 1
        else: break
    elif cons_w > 0:
        if pips > 0: cons_w += 1
        else: break

print(f'Balance (calc): ${balance:.2f}')
print(f'Net pips:       {net_pips:+.1f}p')
print(f'Peak balance:   ${peak:.2f}')
print(f'Drawdown:       {drawdown:.2f}%')
print()
print(f'Wins:           {len(wins)} '
      f'({len(full_wins)} full + '
      f'{len(cascade_wins)} cascade)')
print(f'Losses:         {len(losses)}')
print(f'Decisive:       {decisive}')
print(f'Win rate:       {wr:.1f}%')
print()
print(f'Avg win pips:   +{avg_win_p:.1f}p')
print(f'Avg loss pips:  -{avg_loss_p:.1f}p')
print(f'Avg win $:      +${avg_win_d:.2f}')
print(f'Avg loss $:     -${avg_loss_d:.2f}')
print(f'Profit factor:  {pf:.3f}')
print()
print(f'Streak:         {cons_l} losses / '
      f'{cons_w} wins')

# Load fund_state.json and compare
with open('data/fund_state.json') as f:
    fs = json.load(f)

stored_bal = float(fs.get('balance',0))
stored_cl = int(fs.get('consecutive_losses',0))
stored_cw = int(fs.get('consecutive_wins',0))
stored_wr = float(fs.get('win_rate',0))
stored_dd = float(fs.get('drawdown_pct',0))

live_bal = balance  # already from _live_state

print()
print('Fund state consistency:')
bal_ok = abs(stored_bal - live_bal) < 1.0
print(f'  Balance: ${stored_bal:.2f} '
      f'{"OK" if bal_ok else "FAIL calc=" + str(round(live_bal,2))}')
cl_ok = stored_cl == cons_l
print(f'  Consec losses: {stored_cl} '
      f'{"OK" if cl_ok else "FAIL calc=" + str(cons_l)}')
cw_ok = stored_cw == cons_w
print(f'  Consec wins: {stored_cw} '
      f'{"OK" if cw_ok else "FAIL calc=" + str(cons_w)}')
wr_ok = abs(stored_wr - wr) < 1.0
print(f'  Win rate: {stored_wr}% '
      f'{"OK" if wr_ok else "FAIL calc=" + str(round(wr,1))}')
dd_ok = abs(stored_dd - drawdown) < 0.1
print(f'  Drawdown: {stored_dd}% '
      f'{"OK" if dd_ok else "FAIL live=" + str(round(drawdown,4))}')

print()
print('=' * 55)
print('SECTION 3 — CIRCUIT BREAKER')
print('=' * 55)

if cons_l >= 3:
    print(f'CIRCUIT BREAKER ACTIVE')
    print(f'   {cons_l} consecutive losses')
    print(f'   No new trades until a win')
elif cons_l == 2:
    print(f'WARNING: {cons_l} losses in a row')
    print(f'   One more triggers pause')
elif cons_l == 1:
    print(f'{cons_l} loss — watching')
elif cons_w >= 1:
    print(f'CLEAR: {cons_w} consecutive wins')
    print(f'   New trades CAN open')
else:
    print(f'CLEAR: neutral streak')
    print(f'   New trades CAN open')

print()
print('FTMO limits:')
daily_pct = abs(float(
    fs.get('daily_pnl_pct',0)))
total_dd = abs(drawdown)
print(f'  Daily:  {daily_pct:.2f}% / 5.0% '
      f'{"BREACH" if daily_pct>=5 else "WARN" if daily_pct>3.5 else "OK"}')
print(f'  Total:  {total_dd:.2f}% / 10.0% '
      f'{"BREACH" if total_dd>=10 else "WARN" if total_dd>7 else "OK"}')

print()
print('=' * 55)
print('SECTION 4 — OPEN TRADES')
print('=' * 55)

print(f'{len(open_f)} open trade(s):')
for _, t in open_f.iterrows():
    pair = str(t.get('pair',''))
    direction = str(t.get('direction','')).upper()
    entry = float(t.get('entry',0) or 0)
    stop = float(t.get('stop_loss',0) or 0)
    eff = float(t.get('effective_stop',0) or stop)
    t1 = float(t.get('t1_price',0) or t.get('target',0) or 0)
    t1_hit = str(t.get('t1_hit','')).upper() in ('TRUE','YES','1','T')
    pos_pct = float(t.get('position_size_pct_at_entry',1.0) or 1.0)
    ps = 0.01 if 'JPY' in pair else 0.0001
    stop_p = abs(entry-eff)/ps if entry and eff else 0
    t1_p = abs(t1-entry)/ps if t1 and entry else 0
    rr = t1_p/stop_p if stop_p > 0 else 0

    try:
        ts = str(t.get('timestamp',''))
        if ts and ts not in ('nan','None',''):
            dt = datetime.fromisoformat(
                ts.replace('Z','+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - dt).total_seconds()/86400
        else:
            days = 0
    except:
        days = 0

    t1_badge = 'PROTECTED' if t1_hit else 'LIVE'
    print(f'  [{t1_badge}] #{t.get("id")} {pair} {direction}')
    print(f'     Entry={entry:.5f} '
          f'Stop={eff:.5f} '
          f'({stop_p:.0f}p)')
    print(f'     T1={t1:.5f} '
          f'({t1_p:.0f}p) '
          f'R:R {rr:.1f} '
          f'Risk={pos_pct:.2f}%')
    print(f'     Open: {days:.1f}d '
          f'Conf: {t.get("confidence")}')
    if days < 0:
        print(f'     WARNING: NEGATIVE DAYS — timestamp bug')

print()
print('=' * 55)
print('SECTION 5 — CODE FEATURE CHECKS')
print('=' * 55)

checks = [
    # Critical fixes
    ('Circuit breaker first',
     '_cb_losses.*>=.*3',
     'daily.py', True),
    ('No bare datetime.now()',
     r'datetime\.now\(\)',
     'daily.py', False),
    ('_safe_id in monitor',
     r'_safe_id|_safe_int_id',
     'src/monitor.py', True),
    ('_log_line None fixed',
     '_log_line.None',
     'daily.py', False),
    ('logf removed',
     r'\blogf\b',
     'daily.py', False),
    ('100000 removed',
     r'\b100000\b',
     'src/trading/financials.py', False),
    # Trading filters
    ('Tiered capacity',
     'OVERRIDE_MIN_CONF',
     'daily.py', True),
    ('Smart swap',
     'SWAP_MIN_NEW_CONF',
     'daily.py', True),
    ('Keep score',
     '_calculate_keep_score',
     'daily.py', True),
    ('Swap finder',
     '_find_swap_target',
     'daily.py', True),
    ('Trend alignment',
     '_get_trend_alignment',
     'daily.py', True),
    ('Correlation filter',
     '_check_correlation',
     'daily.py', True),
    ('Dynamic sizing',
     '_calculate_position_size',
     'daily.py', True),
    ('Session filter',
     '_check_session_filter',
     'daily.py', True),
    ('News blackout',
     'news_blackout_pairs',
     'daily.py', True),
    ('Entry validation',
     '_validate_entry_price',
     'daily.py', True),
    # Monitor features
    ('Trailing stop',
     '_update_trailing_stop',
     'src/monitor.py', True),
    ('Pending entries',
     '_check_pending_trades',
     'src/monitor.py', True),
    ('Close before alert',
     '_emergency_close_trade',
     'src/monitor.py', True),
    ('Duplicate stop guard',
     '_already_sent_stop_alert',
     'src/monitor.py', True),
    ('Live dashboard',
     'build_fund_dashboard_embed',
     'src/monitor.py', True),
    ('Financials sync',
     'sync_fund_state_json',
     'src/monitor.py', True),
    # Discord/reporting
    ('Blocked setups report',
     'blocked_setups',
     'src/discord_notifier.py', True),
    ('Swap alert',
     'send_swap_alert',
     'src/discord_notifier.py', True),
    ('Circuit breaker report',
     'Circuit breaker',
     'src/discord_notifier.py', True),
    ('FTMO daily metric',
     'daily_pnl_pct',
     'src/discord_notifier.py', True),
    ('Pips-based win calc',
     'avg_win_pips',
     'src/discord_notifier.py', True),
    # Financials
    ('NaN guard',
     'safe_pos_pct',
     'src/trading/financials.py', True),
    ('Pips-based in financials',
     r'pips.*> 0',
     'src/trading/financials.py', True),
    ('ML learning gates',
     'retrain_if_stale',
     'daily.py', True),
    # Git workflows
    ('-X ours monitor',
     '-X ours',
     '.github/workflows/monitor.yml', True),
    ('-X ours daily',
     '-X ours',
     '.github/workflows/daily.yml', True),
]

all_pass = True
failed = []
for name, pattern, fname, expect in checks:
    try:
        with open(fname, encoding='utf-8', errors='replace') as _fh:
            content = _fh.read()
        found = bool(re.search(pattern, content))
    except FileNotFoundError:
        found = False
    ok = found == expect
    if not ok:
        all_pass = False
        failed.append(name)
    print(f'  {"OK  " if ok else "FAIL"} {name}')

print()
if all_pass:
    print('ALL CODE CHECKS PASS')
else:
    print(f'FAILED: {failed}')

print()
print('=' * 55)
print('SECTION 6 — DISCORD MESSAGE IDs')
print('=' * 55)

try:
    with open(
        'data/discord_dashboard.json'
    ) as f:
        dd = json.load(f)
    fid = dd.get('fund_dashboard_message_id')
    cid = dd.get('closed_trades_message_id')
    print(f'  Dashboard:     '
          f'{"OK " + str(fid)[:10] + "..." if fid else "NULL (will create on next run)"}')
    print(f'  Closed trades: '
          f'{"OK " + str(cid)[:10] + "..." if cid else "NULL"}')
except Exception as e:
    print(f'  Error: {e}')

print()
print('=' * 55)
print('SECTION 7 — GIT STATUS')
print('=' * 55)

r = subprocess.run(
    ['git','log','--oneline','-5'],
    capture_output=True, text=True)
print(r.stdout)
r2 = subprocess.run(
    ['git','status','--short'],
    capture_output=True, text=True)
uncommitted = r2.stdout.strip()
print(f'Uncommitted: '
      f'{uncommitted or "none OK"}')

print()
print('=' * 55)
print('FINAL SUMMARY')
print('=' * 55)

# Collect all pass/fail
state_checks = [
    bal_ok, cl_ok, cw_ok, wr_ok, dd_ok]
data_checks = [
    len(bad_ids)==0,
    len(bad_pos)==0,
    len(ghosts)==0,
    len(neg_days)==0]

all_state_ok = all(state_checks)
all_data_ok = all(data_checks)
all_code_ok = all_pass
no_uncommitted = not uncommitted

overall = all([
    all_state_ok,
    all_data_ok,
    all_code_ok,
    no_uncommitted])

print(f'Trade data:    '
      f'{"PASS" if all_data_ok else "FAIL"}')
print(f'Financials:    '
      f'{"PASS" if all_state_ok else "FAIL"}')
print(f'Code features: '
      f'{"PASS" if all_code_ok else "FAIL"}')
print(f'Git clean:     '
      f'{"PASS" if no_uncommitted else "FAIL"}')
print()
print(f'Balance:  ${stored_bal:,.2f}')
print(f'Drawdown: {drawdown:.2f}%')
print(f'Streak:   {cons_l} L / {cons_w} W')
print(f'Open:     {len(open_f)} trades')
print()

if overall:
    print('SYSTEM READY')
    if cons_l >= 3:
        print()
        print('Circuit breaker active')
        print('Waiting for a win to release')
    elif cons_w >= 1:
        print()
        print(f'Circuit breaker clear')
        print(f'{cons_w} consecutive wins')
        print('New trades CAN open')
    print()
    print('Open trade T1 targets:')
    for _, t in open_f.iterrows():
        pair = str(t.get('pair',''))
        t1 = float(t.get('t1_price',0) or
                   t.get('target',0) or 0)
        direction = str(
            t.get('direction','')).upper()
        entry = float(t.get('entry',0) or 0)
        ps = 0.01 if 'JPY' in pair else 0.0001
        t1_pips = abs(t1-entry)/ps if t1 and entry else 0
        t1_hit = str(t.get('t1_hit','')).upper() in ('TRUE','YES','1','T')
        if t1_hit:
            print(f'  [T1 HIT] {pair}')
        else:
            print(f'  [TARGET] {pair} T1={t1:.5f} '
                  f'({t1_pips:.0f}p away)')
else:
    print('ISSUES FOUND — fix before sleeping')
    if not all_data_ok:
        print('  -> Fix trade data issues')
    if not all_state_ok:
        print('  -> Sync fund_state.json')
    if not all_code_ok:
        print(f'  -> Fix code: {failed}')
    if not no_uncommitted:
        print('  -> Commit uncommitted changes')
