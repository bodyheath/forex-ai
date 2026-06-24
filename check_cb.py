import json, sys
sys.stdout.reconfigure(encoding='utf-8')

with open('data/fund_state.json') as f:
    fs = json.load(f)

cl = int(fs.get('consecutive_losses', 0))
cw = int(fs.get('consecutive_wins', 0))
bal = float(fs.get('balance', 0))

print(f'Balance: ${bal:,.2f}')
print(f'Consecutive losses: {cl}')
print(f'Consecutive wins:   {cw}')
print()

if cl >= 3:
    print('CIRCUIT BREAKER ACTIVE')
    print(f'   {cl} losses in a row')
    print('   No new trades until a win')
elif cl == 2:
    print('WARNING: 2 losses in a row')
    print('   One more triggers pause')
elif cl == 1:
    print('1 loss - watching')
elif cw >= 1:
    print('CIRCUIT BREAKER CLEAR')
    print(f'   {cw} wins in a row')
    print('   New trades CAN open')
else:
    print('CIRCUIT BREAKER CLEAR')
    print('   New trades CAN open')
