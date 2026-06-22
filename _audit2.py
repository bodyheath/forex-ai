import csv
with open('data/trades.csv', encoding='utf-8-sig', newline='') as f:
    rows = [r for r in csv.DictReader(f) if r.get('trade_this') == 'YES' and r.get('status') == 'OPEN']

print('=== OPEN FUND TRADES (via DictReader - same as tracker.load()) ===')
for r in rows:
    pair = r.get('pair', '')
    print(f'\n{pair}:')
    print(f'  entry: {repr(r.get("entry"))}')
    print(f'  stop_loss: {repr(r.get("stop_loss"))}')
    print(f'  effective_stop: {repr(r.get("effective_stop"))}')
    print(f'  t1_price: {repr(r.get("t1_price"))}')
    print(f'  t2_price: {repr(r.get("t2_price"))}')
    print(f'  t3_price: {repr(r.get("t3_price"))}')
    print(f'  t1_hit: {repr(r.get("t1_hit"))}')
    print(f'  t2_hit: {repr(r.get("t2_hit"))}')
    print(f'  t3_hit: {repr(r.get("t3_hit"))}')
    print(f'  timestamp: {repr(r.get("timestamp"))}')
    print(f'  confidence: {repr(r.get("confidence"))}')
    print(f'  position_size_pct_at_entry: {repr(r.get("position_size_pct_at_entry"))}')
    print(f'  direction: {repr(r.get("direction"))}')
    # compute current progress as monitor.py does:
    entry_d = float(r.get("entry") or 0)
    stop_d = float(r.get("effective_stop") or r.get("stop_loss") or 0)
    t1_d = float(r.get("t1_price") or 0)
    t2_d = float(r.get("t2_price") or 0)
    t3_d = float(r.get("t3_price") or r.get("target") or 0)
    t1h = str(r.get("t1_hit", "")).upper() == "TRUE"
    t2h = str(r.get("t2_hit", "")).upper() == "TRUE"
    t3h = str(r.get("t3_hit", "")).upper() == "TRUE"
    dir_d = r.get("direction", "")
    pip_d = 0.01 if "JPY" in pair else 0.0001
    print(f'  entry_d={entry_d}, stop_d={stop_d}')
    print(f'  t1_d={t1_d}, t2_d={t2_d}, t3_d={t3_d}')
    print(f'  t1h={t1h}, t2h={t2h}, t3h={t3h}')
    if not t1h:
        next_d, tgt_d = "T1", t1_d
    elif not t2h:
        next_d, tgt_d = "T2", t2_d
    elif not t3h:
        next_d, tgt_d = "T3", t3_d
    else:
        next_d, tgt_d = "Complete", t3_d
    prog_d = 0.0
    if tgt_d and entry_d and tgt_d != entry_d:
        prog_d = ((entry_d - tgt_d) / (entry_d - tgt_d) * 100 if dir_d != "BUY"
                  else (entry_d - tgt_d) / (entry_d - tgt_d) * 100)
        # actually compute correctly:
        if dir_d == "BUY":
            # Use entry price as current_sim to test formula
            current_sim = entry_d
            prog_d = (current_sim - entry_d) / (tgt_d - entry_d) * 100
        else:
            current_sim = entry_d
            prog_d = (entry_d - current_sim) / (entry_d - tgt_d) * 100
    print(f'  next={next_d}, target={tgt_d}, base_progress_at_entry={prog_d:.1f}%')

print('\n=== TIMESTAMP EXAMPLES ===')
for r in rows:
    pair = r.get('pair', '')
    ts = r.get('timestamp', '')
    print(f'  {pair}: timestamp={repr(ts)}')
