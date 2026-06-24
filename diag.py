import json

print('=== PRICE CACHE FORMAT ===')
try:
    with open('data/price_cache.json') as f:
        cache = json.load(f)
    if 'prices' in cache:
        prices = cache['prices']
        print(f'Nested -- {len(prices)} pairs')
        print('Sample:', list(prices.items())[:3])
    else:
        pairs = {k: v for k, v in cache.items() if '/' in str(k)}
        print(f'Flat -- {len(pairs)} pairs')
        print('Sample:', list(pairs.items())[:3])
        # show all keys
        print('All keys sample:', list(cache.keys())[:10])
except Exception as e:
    print(f'Error: {e}')

print()
print('=== CURRENT LIVE P&L ===')
try:
    from src.trading.financials import calculate_fund_state, load_prices
    prices = load_prices()
    print(f'load_prices() returned {len(prices)} prices')
    print(f'Price sample: {list(prices.items())[:3]}')
    state = calculate_fund_state(prices=prices)
    print(f'Balance: ${state["balance"]:,.2f}')
    print(f'State keys: {list(state.keys())}')
    for t in state.get('open_trades', []):
        print(
            f'  {t["pair"]} '
            f'current={t.get("current","?")} '
            f'pips={t.get("pips","?")} '
            f'dollars={t.get("dollars","?")} '
            f'progress={t.get("progress_pct","?")} '
            f'days={t.get("days_open","?")}'
        )
except Exception as e:
    import traceback
    print(f'Error: {e}')
    traceback.print_exc()
