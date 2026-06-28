import json, shutil

with open('data/fund_state.json') as f:
    fs = json.load(f)

print(f'Current opening_balance: ${fs.get("opening_balance"):,.2f}')
print(f'Current balance:         ${fs.get("balance"):,.2f}')
print(f'Current drawdown:        {fs.get("drawdown_pct"):.2f}%')

TRUE_START = 10000.00
true_dd = (TRUE_START - fs['balance']) / TRUE_START * 100

print()
print(f'TRUE starting balance:   ${TRUE_START:,.2f}')
print(f'TRUE drawdown:           {true_dd:.2f}%')
print(f'TRUE buffer remaining:   {10 - true_dd:.2f}%')
print()

if abs(fs.get('opening_balance', 0) - TRUE_START) > 1.0:
    print('WARNING: opening_balance is WRONG')
    print('Fixing...')
    fs['opening_balance'] = TRUE_START
    fs['drawdown_pct'] = round(true_dd, 4)
    tmp = 'data/fund_state.json.tmp'
    with open(tmp, 'w') as f:
        json.dump(fs, f, indent=2)
    shutil.move(tmp, 'data/fund_state.json')
    print('Fixed.')
else:
    print('OK: opening_balance correct')
