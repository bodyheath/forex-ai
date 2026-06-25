import pandas as pd

df = pd.read_csv('data/trades.csv')
fund = df[df.trade_this.astype(str) == 'YES']

print('=== DATA QUALITY AT ENTRY ===')
print()

critical_fields = [
    'stop_loss',
    'entry',
    'confidence',
    'rsi_at_entry',
    'regime_at_entry',
    'weekly_trend_at_entry',
    'stop_pips_at_entry',
    'rr_at_entry',
]

for field in critical_fields:
    if field not in fund.columns:
        print(f'MISSING_COL  {field}: COLUMN MISSING')
        continue

    zero_or_null = 0
    for _, t in fund.iterrows():
        val = t.get(field)
        try:
            f = float(val or 0)
            if f == 0 or f != f:
                zero_or_null += 1
        except Exception:
            zero_or_null += 1

    pct = zero_or_null / len(fund) * 100
    icon = 'OK  ' if pct == 0 else 'WARN' if pct < 50 else 'BAD '
    print(f'{icon} {field}: {zero_or_null}/{len(fund)} zero/null ({pct:.0f}%)')

print()
print('=== TRADES WITH ZERO STOP ===')
zero_stop = fund[pd.to_numeric(fund.stop_loss, errors='coerce').fillna(0) == 0]
print(f'Trades with stop=0: {len(zero_stop)}')
for _, t in zero_stop.iterrows():
    print(f'  id={t["id"]}  pair={t["pair"]}  status={t["status"]}  pips={t["pips"]}')

print()
print('=== TRADES WITH ZERO RSI ===')
if 'rsi_at_entry' in fund.columns:
    zero_rsi = fund[pd.to_numeric(fund.rsi_at_entry, errors='coerce').fillna(0) == 0]
    print(f'Trades with RSI=0: {len(zero_rsi)}')
    for _, t in zero_rsi.iterrows():
        print(f'  id={t["id"]}  pair={t["pair"]}  status={t["status"]}')
else:
    print('rsi_at_entry column not found')
