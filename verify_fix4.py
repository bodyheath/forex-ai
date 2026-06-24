import sys
sys.stdout.reconfigure(encoding='utf-8')
from src.trading.financials import calculate_fund_state, sync_fund_state_json, load_prices

sync_fund_state_json()
state = calculate_fund_state(prices=load_prices())

if 'error' in state:
    print('ERROR:', state['error'])
    sys.exit(1)

print('=== FUND STATE AFTER FIX ===')
print(f'Balance:         ${state["balance"]:,.2f}')
print(f'Win count:       {state["win_count"]}  (full wins)')
print(f'Protected count: {state["protected_count"]}  (cascade wins)')
print(f'Loss count:      {state["loss_count"]}')
print(f'Win rate:        {state["win_rate"]}%')
print(f'Profit factor:   {state["profit_factor"]}')
print(f'Avg win pips:    {state["avg_win_pips"]}')
print(f'Avg loss pips:   {state["avg_loss_pips"]}')
print(f'Avg win USD:     ${state["avg_win_dollars"]}')
print(f'Avg loss USD:    ${state["avg_loss_dollars"]}')
print(f'Cons losses:     {state["consecutive_losses"]}')
print(f'Cons wins:       {state["consecutive_wins"]}')
print(f'Best pair:       {state["best_pair"]} {state["best_pips"]}p')
print(f'Total trades:    {state["fund_total_trades"]}')
decisive = state['win_count'] + state['protected_count'] + state['loss_count']
print(f'Display line:    Wins:{state["win_count"]} Protected:{state["protected_count"]} Losses:{state["loss_count"]} (decisive={decisive})')
print()

# Verify expected values
checks = [
    ('win_count == 2',              state['win_count'] == 2),
    ('protected_count == 3',        state['protected_count'] == 3),
    ('loss_count == 7',             state['loss_count'] == 7),
    ('decisive == 12',              decisive == 12),
    ('win_rate > 40',               state['win_rate'] > 40.0),
    ('avg_loss_dollars > 50',       state['avg_loss_dollars'] > 50.0),
    ('profit_factor > 0',           state['profit_factor'] > 0),
    ('consecutive_losses == 0',     state['consecutive_losses'] == 0),
    ('consecutive_wins == 1',       state['consecutive_wins'] == 1),
    ('best_pair in state',          bool(state['best_pair'])),
]

all_ok = True
for name, ok in checks:
    icon = 'OK  ' if ok else 'FAIL'
    if not ok:
        all_ok = False
    print(f'  {icon}  {name}')

print()
print('ALL PASS' if all_ok else 'SOME FAILED')
