"""Quick import + open_trade_summary field check."""
import pandas as pd
from src.trading.financials import calculate_fund_state, load_prices, TRADES_CSV

prices = load_prices()
df = pd.read_csv(str(TRADES_CSV), encoding='utf-8-sig')
state = calculate_fund_state(df, prices)

REQUIRED = [
    'id', 'pair', 'direction', 'entry', 'current', 'stop',
    't1', 't2', 't3', 't1_price', 't2_price', 't3_price',
    't1_hit', 't2_hit', 't3_hit', 'next_target',
    'pips_unrealised', 'dollars_unrealised', 'progress_pct',
    'days_open', 'open_str', 'conf', 'risk_dollars', 'pnl_note',
]
REQUIRED_STATE = ['unrealised_dollars', 'unrealised_pips', 'total_equity']

print('=== STATE KEYS ===')
for k in REQUIRED_STATE:
    v = state.get(k)
    icon = '✅' if v is not None else '❌'
    print(f'  {icon} {k} = {v}')

print()
print('=== OPEN TRADES ===')
for t in state.get('open_trades', []):
    print(f'\n  Pair: {t.get("pair")}')
    for k in REQUIRED:
        v = t.get(k)
        icon = '✅' if v is not None else '❌'
        print(f'    {icon} {k} = {v}')

print()
print('=== build_fund_dashboard_embed import test ===')
try:
    from src.discord_notifier import build_fund_dashboard_embed
    embed = build_fund_dashboard_embed(state)
    print(f'✅ embed built — title={embed["title"]!r}')
    print(f'   fields: {len(embed["fields"])} (3 header + {len(embed["fields"])-3} trades)')
except Exception as e:
    import traceback
    print(f'❌ {e}')
    traceback.print_exc()
