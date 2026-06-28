import sys, io, re, json, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

def grep(pattern, fname, flags=re.IGNORECASE):
    try:
        with open(fname, encoding='utf-8', errors='replace') as f:
            content = f.read()
        return bool(re.search(pattern, content)), content
    except FileNotFoundError:
        return False, ''

results = []
def chk(name, pattern, fname, expect=True):
    found, _ = grep(pattern, fname)
    ok = found == expect
    results.append((ok, name))
    icon = 'OK  ' if ok else 'FAIL'
    print(f'  [{icon}] {name}')

print('=' * 55)
print('SETTINGS AUDIT')
print('=' * 55)
print()

# Cascade
print('CASCADE:')
chk('T1_SIZE = 0.50 (cascade.py)', r'T1_SIZE\s*=\s*0\.50', 'src/cascade.py')
chk('T2_SIZE = 0.30 (cascade.py)', r'T2_SIZE\s*=\s*0\.30', 'src/cascade.py')
chk('T3_SIZE = 0.20 (cascade.py)', r'T3_SIZE\s*=\s*0\.20', 'src/cascade.py')
chk('T1_SIZE = 0.50 (financials.py)', r'T1_SIZE\s*=\s*0\.50', 'src/trading/financials.py')
chk('No old 0.35 T1 in cascade', r'T1_SIZE\s*=\s*0\.35', 'src/cascade.py', expect=False)
chk('R:R floor T1>=1R', r't1\s*=\s*max\(t1.*_sd', 'src/cascade.py')
chk('R:R floor T2>=1.5R', r'_sd\s*\*\s*1\.5', 'src/cascade.py')
chk('R:R floor T3>=2R', r'_sd\s*\*\s*2\.0', 'src/cascade.py')

print()
print('RISK MANAGEMENT:')
chk('BASE_RISK_PCT = 0.75', r'BASE_RISK_PCT\s*=\s*0\.75', 'daily.py')
chk('ATR stop cap 2.0x', r'_yt_atr14\s*\*\s*2\.0', 'daily.py')
chk('FUND_MIN_RR = 2.5', r'FUND_MIN_RR\s*=\s*2\.5', 'daily.py')
chk('Circuit breaker -2.0%', r'CIRCUIT_BREAKER_PCT\s*=\s*-2\.0', 'src/fund_state.py')
chk('Weekly loss limit -5.0%', r'WEEKLY_LOSS_LIMIT_PCT\s*=\s*-5\.0', 'src/fund_state.py')
chk('ML hard block 0.35', r'_ol_prob.*<=.*0\.35|0\.35.*_ol_prob', 'daily.py')
chk('Exotic pairs banned', r'FUND_BANNED_PAIRS', 'daily.py')
chk('HKD in banned list', r'HKD', 'daily.py')
chk('Loss streak sizing', r'LOSS_STREAK_SIZING', 'daily.py')

print()
print('MONITOR MESSAGES (cascade % updated):')
chk('T1 Telegram = 50%', r'50%.*position banked', 'src/monitor.py')
chk('T2 Telegram = 30%/80%', r'30%.*position banked', 'src/monitor.py')
chk('T3 Telegram = 50%/30%/20%', r'\(50%\).*\(30%\).*\(20%\)', 'src/monitor.py')
chk('T1 NOT 35% in Telegram', r'35%.*position banked', 'src/monitor.py', expect=False)

print()
print('DISCORD EMBED (cascade % updated):')
chk('pct_banked 50 for T1', r'pct_banked\s*=\s*50', 'src/discord_notifier.py')
chk('pct_banked 80 for T2', r'80 if milestone_num == 2', 'src/discord_notifier.py')
chk('T1 banked (50%)', r'T1 banked \(50%\)', 'src/discord_notifier.py')
chk('Remainder (20%)', r'Remainder \(20%\)', 'src/discord_notifier.py')
chk('NOT old pct_banked 40', r'pct_banked\s*=\s*40', 'src/discord_notifier.py', expect=False)

print()
print('WORKFLOWS (security):')
chk('price_check SHA-pinned checkout', r'actions/checkout@[0-9a-f]{40}', '.github/workflows/price_check.yml')
chk('price_check SHA-pinned setup-python', r'actions/setup-python@[0-9a-f]{40}', '.github/workflows/price_check.yml')
chk('daily.yml SHA-pinned checkout', r'actions/checkout@[0-9a-f]{40}', '.github/workflows/daily.yml')
chk('monitor.yml SHA-pinned checkout', r'actions/checkout@[0-9a-f]{40}', '.github/workflows/monitor.yml')
chk('-X ours in price_check', r'-X ours', '.github/workflows/price_check.yml')
chk('-X ours in daily', r'-X ours', '.github/workflows/daily.yml')
chk('-X ours in monitor', r'-X ours', '.github/workflows/monitor.yml')
chk('No bare @v4 in price_check', r'checkout@v4|setup-python@v5', '.github/workflows/price_check.yml', expect=False)

print()
print('THRESHOLD CONFIG:')
try:
    with open('data/threshold_config.json') as f:
        tc = json.load(f)
    min_rr = tc.get('min_rr', 'MISSING')
    conf = tc.get('confidence_threshold', 'MISSING')
    dc = tc.get('data_collection_mode', 'MISSING')
    print(f'  min_rr:               {min_rr} {"OK" if float(str(min_rr)) >= 2.5 else "WARN"}')
    print(f'  confidence_threshold: {conf} {"OK" if int(str(conf)) >= 7 else "WARN"}')
    print(f'  data_collection_mode: {dc} {"OK" if not dc else "WARN - should be False"}')
except Exception as e:
    print(f'  Error: {e}')

print()
passed = sum(1 for ok, _ in results if ok)
failed = [(n) for ok, n in results if not ok]
print(f'RESULT: {passed}/{len(results)} checks passed')
if failed:
    print(f'FAILED: {failed}')
else:
    print('ALL SETTINGS CORRECT')
