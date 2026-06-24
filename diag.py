import json, pandas as pd
from src.trading.financials import calculate_fund_state, load_prices, TRADES_CSV

print('=== PRICE CACHE FORMAT ===')
prices = load_prices()
print(f'load_prices() returned {len(prices)} prices')
print('Sample:', list(prices.items())[:3])

print()
print('=== CURRENT LIVE P&L (using correct API) ===')
df = pd.read_csv(str(TRADES_CSV), encoding='utf-8-sig')
state = calculate_fund_state(df, prices)
print(f'Balance: ${state["balance"]:,.2f}')
print(f'Daily PnL: {state["daily_pnl_pct"]:+.4f}%')
print(f'State keys: {list(state.keys())}')
print()
print('Open trades:')
for t in state.get('open_trades', []):
    print(f'  {t.get("pair","?")} keys={list(t.keys())}')
    print(f'    pips_unrealised={t.get("pips_unrealised","?")} '
          f'dollars_unrealised={t.get("dollars_unrealised","?")} '
          f'progress_pct={t.get("progress_pct","?")} '
          f'entry={t.get("entry","?")}')
    print(f'    is_protected={t.get("is_protected","?")}')
    # Check what the current price lookup returns
    pair = t.get('pair', '')
    from src.trading.financials import get_price
    cp = get_price(prices, pair)
    print(f'    get_price result: {cp}')

print()
print('=== DASHBOARD PRICE LOOKUP CHECK ===')
# Simulating what monitor.py does at line 3021
fund = df[(df['trade_this'].astype(str) == 'YES') & (df['status'] == 'OPEN')]
for _, row in fund.iterrows():
    pair = str(row.get('pair', ''))
    entry = float(row.get('entry', 0) or 0)
    t1 = float(row.get('t1_price', 0) or 0)
    # exact lookup from monitor.py line 3021
    cur = float(prices.get(pair) or prices.get(pair.replace('/', '')) or entry)
    progress = 0.0
    if t1 and entry and t1 != entry:
        rng = t1 - entry
        mv  = cur - entry
        progress = (mv / rng * 100) if rng > 0 else 0.0
    print(f'  {pair}: entry={entry:.5f} t1={t1:.5f} cur={cur:.5f} progress={progress:.1f}%')
    if cur == entry:
        print(f'    ** PRICE NOT LOADING — cur == entry **')
    if t1 == 0:
        print(f'    ** t1_price is 0/missing — progress will always be 0% **')
