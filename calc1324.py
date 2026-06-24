from datetime import datetime, timezone, timedelta

nzt_str = '2026-06-24 09:50:09'
nzt_dt = datetime.strptime(nzt_str, '%Y-%m-%d %H:%M:%S')
utc_dt = nzt_dt - timedelta(hours=12)
print('NZT:', nzt_str)
print('UTC:', utc_dt.strftime('%Y-%m-%d %H:%M:%S'))

entry = 1.2189
stop = 1.2262
pip_size = 0.0001
atr = abs(entry - stop)
t1 = entry - 0.4 * atr
t2 = entry - 0.7 * atr
t1_pips = (entry - t1) / pip_size
t2_pips = (entry - t2) / pip_size
print('ATR=%.4f  stop_pips=%.1f' % (atr, (stop-entry)/pip_size))
print('T1=%.5f  T1_pips=%.1f' % (t1, t1_pips))
print('T2=%.5f  T2_pips=%.1f' % (t2, t2_pips))
print('Weighted pips (PARTIAL_WIN): %.1f' % (t1_pips * 0.40,))
print('exit_price (BE):', entry)
