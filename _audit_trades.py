import pandas as pd
df = pd.read_csv('data/trades.csv')
fund = df[df.trade_this == 'YES']
open_trades = fund[fund.status == 'OPEN']

print('=== OPEN TRADE COLUMN VALUES ===')
for _, trade in open_trades.iterrows():
    pair = trade.get('pair', '')
    print(f'{pair}:')
    print(f'  entry_price: {repr(trade.get("entry_price"))}')
    print(f'  stop_loss: {repr(trade.get("stop_loss"))}')
    print(f'  effective_stop_loss: {repr(trade.get("effective_stop_loss"))}')
    print(f'  t1_price: {repr(trade.get("t1_price"))}')
    print(f'  t2_price: {repr(trade.get("t2_price"))}')
    print(f'  t3_price: {repr(trade.get("t3_price"))}')
    print(f'  t1_hit: {repr(trade.get("t1_hit"))} type={type(trade.get("t1_hit")).__name__}')
    print(f'  t2_hit: {repr(trade.get("t2_hit"))} type={type(trade.get("t2_hit")).__name__}')
    print(f'  t3_hit: {repr(trade.get("t3_hit"))} type={type(trade.get("t3_hit")).__name__}')
    print(f'  entry_datetime: {repr(trade.get("entry_datetime"))}')
    print(f'  checklist_score: {repr(trade.get("checklist_score"))}')
    print(f'  pre_trade_checklist: {repr(trade.get("pre_trade_checklist"))}')
    print(f'  direction: {repr(trade.get("direction"))}')
    print(f'  risk_dollars: {repr(trade.get("risk_dollars"))}')
    print(f'  dollar_risk: {repr(trade.get("dollar_risk"))}')
    print()

print('=== ALL COLUMN NAMES ===')
print(df.columns.tolist())
