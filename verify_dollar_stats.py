import csv, json, math

with open('data/fund_state.json') as f:
    fs = json.load(f)

daily_opening_balance = float(fs.get('daily_opening_balance') or 10023.25)
fs_sizing_pct = float(fs.get('current_sizing_pct') or 1.0)
risk_usd_base = daily_opening_balance * fs_sizing_pct / 100.0
print('daily_opening_balance:', daily_opening_balance)
print('current_sizing_pct:', fs_sizing_pct)
print('1R risk: $' + str(round(risk_usd_base, 2)))
print()

with open('data/trades.csv', encoding='utf-8-sig', newline='') as f:
    fund_rows = [r for r in csv.DictReader(f) if r.get('trade_this') == 'YES']

closed = [r for r in fund_rows if r.get('status','').upper() not in ('OPEN','')]
win_dollars, loss_dollars = [], []

for fr in closed:
    status = fr.get('status','').upper()
    raw_pip = fr.get('cascading_total_pips_weighted') or fr.get('cascading_total_pips') or fr.get('pips') or ''
    try:
        fp = float(raw_pip) if raw_pip != '' else 0.0
    except:
        fp = 0.0
    if math.isnan(fp):
        fp = 0.0

    pair = fr.get('pair','')
    pip_sz = 0.01 if pair.upper().endswith('JPY') else 0.0001
    try:
        sz_raw = fr.get('position_size_pct_at_entry','')
        sz_pct = float(sz_raw) if sz_raw and str(sz_raw).strip() not in ('','nan') else fs_sizing_pct
        risk_usd = daily_opening_balance * sz_pct / 100.0
        entry_v = float(fr.get('entry') or 0)
        stop_v = float(fr.get('stop_loss') or 0)
        stop_pips = abs(entry_v - stop_v) / pip_sz if stop_v and entry_v else 0.0
        dpp = risk_usd / stop_pips if stop_pips > 0 else 0.0
        dollar_pnl = fp * dpp
    except:
        dollar_pnl = 0.0

    is_win = status in ('WIN','FULL_WIN','PARTIAL_WIN') or (status in ('EXPIRED','EXPIRED_LOSS') and fp > 0)
    is_loss = status == 'LOSS' or (status in ('EXPIRED','EXPIRED_LOSS') and fp <= 0)

    cat = 'WIN' if is_win else ('LOSS' if is_loss else '?')
    print(pair, status, 'pips='+str(round(fp,1)), 'stop_pips='+str(round(stop_pips,1)),
          'dollar_pnl=$'+str(round(dollar_pnl,2)), cat)
    if is_win and dollar_pnl > 0:
        win_dollars.append(dollar_pnl)
    elif is_loss and dollar_pnl < 0:
        loss_dollars.append(abs(dollar_pnl))

print()
tot_win_d = sum(win_dollars)
tot_loss_d = sum(loss_dollars)
avg_win_d = tot_win_d/len(win_dollars) if win_dollars else 0
avg_loss_d = tot_loss_d/len(loss_dollars) if loss_dollars else 0
pf_d = tot_win_d/tot_loss_d if tot_loss_d > 0 else 0
print('win_dollars:', [round(x,2) for x in win_dollars])
print('loss_dollars:', [round(x,2) for x in loss_dollars])
print('avg_win: $'+str(round(avg_win_d,2)), 'avg_loss: $'+str(round(avg_loss_d,2)), 'dollar_pf:', round(pf_d,2))
