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
    print(f'❌ {r["issue_count"]} issues:')
    for iss in r['issues']:
        print(f'  #{iss["trade_id"]} {iss["pair"]}: {iss["issue"]}')

print()
print('=' * 50)
print('2 — LIVE FUND STATE')
print('=' * 50)
sync_fund_state_json()
prices = load_prices()

# Enrich cache with live prices for open pairs missing from it (e.g. GBP/SEK)
try:
    import pathlib as _pl
    _df_open = pd.read_csv('data/trades.csv', encoding='utf-8-sig')
    _open_pairs = _df_open[
        (_df_open['trade_this'].astype(str) == 'YES') & (_df_open['status'] == 'OPEN')
    ]['pair'].tolist()
    _missing = [p for p in _open_pairs if not any(
        prices.get(k) for k in (p, p.replace('/', ''), p.upper(), p.upper().replace('/', ''))
    )]
    if _missing:
        try:
            import yfinance as yf
            for p in _missing:
                ticker = p.replace('/', '') + '=X'
                d = yf.download(ticker, period='1d', interval='5m', progress=False, auto_adjust=True)
                if not d.empty:
                    _val = d['Close'].iloc[-1]
                    if hasattr(_val, 'item'):
                        _val = _val.item()
                    prices[p] = float(_val)
                    print(f'  (fetched live {p}: {prices[p]:.5f})')
                else:
                    print(f'  (no data from yfinance for {ticker})')
        except Exception as _fe:
            print(f'  (live fetch failed: {_fe})')
except Exception as _ee:
    print(f'  (price enrichment failed: {_ee})')

state = calculate_fund_state(prices=prices)
if 'error' in state:
    print(f'❌ ERROR: {state["error"]}')
else:
    bal   = state.get('balance', 0)
    unr   = state.get('unrealised_dollars', state.get('unrealised', 0))
    eq    = state.get('total_equity', 0)
    dpnl  = state.get('daily_pnl_pct', 0)
    dd    = state.get('current_drawdown_pct', state.get('drawdown_pct', 0))
    wr    = state.get('win_rate', 0)
    oc    = state.get('open_count', 0)
    cl    = state.get('consecutive_losses', 0)
    print(f'Balance:    ${bal:,.2f}')
    print(f'Unrealised: ${unr:+,.2f}')
    print(f'Total eq:   ${eq:,.2f}')
    print(f'Daily PnL:  {dpnl:+.2f}%')
    print(f'Drawdown:   {dd:.2f}%')
    print(f'Win rate:   {wr:.0f}%')
    print(f'Open:       {oc}')
    print(f'Streak:     {cl} losses')
    print()

    daily = abs(dpnl)
    total = abs(dd)
    print('FTMO:')
    print(f'  Daily: {daily:.2f}%/5%  {"🚨" if daily>=5 else "⚠️" if daily>3.5 else "✅"}')
    print(f'  Total: {total:.2f}%/10% {"🚨" if total>=10 else "⚠️" if total>7 else "✅"}')
    print()

    print('Open trade P&L:')
    all_ok = True
    for t in state.get('open_trades', []):
        cur     = t.get('current', t.get('entry', 0))
        entry   = t.get('entry', 0)
        pips    = t.get('pips', t.get('pips_unrealised', 0))
        dollars = t.get('dollars', t.get('dollars_unrealised', 0))
        prog    = t.get('progress_pct', 0)
        days    = t.get('days_open', 0)
        risk    = t.get('risk_dollars', 0)

        issues = []
        if prog == 0 and pips != 0:
            issues.append('progress stuck 0%')
        if days < 0:
            issues.append('negative days')
        if cur == entry:
            issues.append('price not loading')
        if dollars == 0 and pips != 0:
            issues.append('dollars=0')

        icon = '✅' if not issues else '❌'
        if issues:
            all_ok = False

        print(f'  {icon} {t["pair"]} {t["direction"]} cur={cur:.5f} {pips:+.1f}p ${dollars:+.2f} prog={prog:.0f}% {days}d risk=${risk:.0f}')
        if issues:
            print(f'    ⚠️  {issues}')

    if all_ok:
        print('  ✅ All P&L values correct')

print()
print('=' * 50)
print('3 — CODE FEATURE CHECKS')
print('=' * 50)
checks = [
    ('Tiered capacity',        'OVERRIDE_MIN_CONF',           'daily.py',                        True),
    ('Trend alignment',        '_get_trend_alignment',        'daily.py',                        True),
    ('Correlation filter',     '_check_correlation',          'daily.py',                        True),
    ('Dynamic sizing',         '_calculate_position_size',    'daily.py',                        True),
    ('Session filter',         '_check_session_filter',       'daily.py',                        True),
    ('News blackout',          '_has_upcoming_news',          'daily.py',                        True),
    ('Entry validation',       '_validate_entry_price',       'daily.py',                        True),
    ('Trailing stop',          '_update_trailing_stop',       'src/monitor.py',                  True),
    ('Pending entries',        '_check_pending_trades',       'src/monitor.py',                  True),
    ('Close before alert',     '_emergency_close_trade',      'src/monitor.py',                  True),
    ('Duplicate stop guard',   '_already_sent_stop_alert',    'src/monitor.py',                  True),
    ('Live dashboard',         'build_fund_dashboard_embed',  'src/monitor.py',                  True),
    ('Financials sync',        'sync_fund_state_json',        'src/monitor.py',                  True),
    ('NaN guard',              'safe_pos_pct',                'src/trading/financials.py',        True),
    ('FTMO daily metric',      'daily_pnl_pct',               'src/discord_notifier.py',          True),
    ('logf removed',           r'\blogf\b',                   'daily.py',                        False),
    ('-X ours monitor',        '-X ours',                     '.github/workflows/monitor.yml',    True),
    ('-X ours daily',          '-X ours',                     '.github/workflows/daily.yml',      True),
]

all_pass = True
for name, pattern, fname, expect in checks:
    try:
        r = subprocess.run(
            ['grep', '-rn', pattern, fname],
            capture_output=True, text=True
        )
        found = len(r.stdout.strip()) > 0
    except FileNotFoundError:
        # grep not available — use Python
        import re, pathlib
        try:
            txt = pathlib.Path(fname).read_text(encoding='utf-8', errors='replace')
            found = bool(re.search(pattern, txt))
        except FileNotFoundError:
            found = False

    ok = found == expect
    if not ok:
        all_pass = False
    print(f'  {"✅" if ok else "❌"} {name}')

print()
print(f'All checks: {"✅ ALL PASS" if all_pass else "❌ SOME FAILED"}')

print()
print('=' * 50)
print('4 — POSITION SIZE CHECK')
print('=' * 50)
df = pd.read_csv('data/trades.csv', encoding='utf-8-sig')
fund = df[df.trade_this.astype(str) == 'YES']
bad = 0
for _, t in fund.iterrows():
    val = t.get('position_size_pct_at_entry')
    try:
        f = float(val or 0)
        if f <= 0 or f != f:
            print(f'  ❌ #{t["id"]} {t["pair"]} pos={val}')
            bad += 1
    except Exception:
        bad += 1
if bad == 0:
    print(f'  ✅ All {len(fund)} trades have valid position size')

print()
print('=' * 50)
print('5 — GIT STATUS')
print('=' * 50)
r = subprocess.run(['git', 'log', '--oneline', '-5'], capture_output=True, text=True)
print(r.stdout)
r2 = subprocess.run(['git', 'status', '--short'], capture_output=True, text=True)
uncommitted = r2.stdout.strip()
print(f'Uncommitted: {uncommitted or "none ✅"}')

print()
print('=' * 50)
print('6 — DASHBOARD MESSAGE IDs')
print('=' * 50)
try:
    with open('data/discord_dashboard.json') as f:
        dd = json.load(f)
    fid = dd.get('fund_dashboard_message_id') or dd.get('message_id')
    cid = dd.get('closed_trades_message_id')
    print(f'  Dashboard:     {"✅ " + str(fid)[:8] + "..." if fid else "❌ NULL"}')
    print(f'  Closed trades: {"✅ " + str(cid)[:8] + "..." if cid else "❌ NULL — will post new"}')
except Exception as e:
    print(f'  ❌ Error: {e}')
