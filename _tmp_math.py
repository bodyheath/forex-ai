import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import pandas as pd

print('=' * 55)
print('PROFITABILITY MATH CHECK')
print('=' * 55)
print()

P1, P2, P3 = 0.50, 0.30, 0.20

df = pd.read_csv('data/trades.csv')
fund = df[df.trade_this.astype(str) == 'YES']
open_f = fund[fund.status == 'OPEN']

print('OPEN TRADE R:R:')
rr_vals = []
for _, t in open_f.iterrows():
    pair = str(t.get('pair', ''))
    entry = float(t.get('entry', 0) or 0)
    stop  = float(t.get('stop_loss', 0) or 0)
    t1    = float(t.get('t1_price', 0) or t.get('target', 0) or 0)
    ps    = 0.01 if 'JPY' in pair else 0.0001
    if entry and stop and t1:
        sp  = abs(entry - stop) / ps
        t1p = abs(t1 - entry) / ps
        rr  = t1p / sp if sp > 0 else 0
        rr_vals.append(rr)
        status = 'OK' if rr >= 1.0 else 'BELOW FLOOR'
        print(f'  #{t["id"]} {pair}: T1={rr:.2f}R [{status}]')

R1 = max(1.0, sum(rr_vals) / len(rr_vals)) if rr_vals else 1.0
R2 = R1 * 1.5
R3 = R1 * 2.5

partial_r = P1 * R1   # net R from T1-only exits
win_r     = P1*R1 + P2*R2 + P3*R1    # after T2, remainder stops at T1 (locked)
full_r    = P1*R1 + P2*R2 + P3*R3

be_partial = 1 / (1 + partial_r) * 100

print()
print('CASCADE MATH (floors: T1=1R, T2=1.5R, T3=2.5R):')
print(f'  T1: {P1*100:.0f}% exit @ {R1:.1f}R = {P1*R1:.2f}R banked')
print(f'  T2: {P2*100:.0f}% exit @ {R2:.1f}R = {P2*R2:.2f}R banked')
print(f'  T3: {P3*100:.0f}% exit @ {R3:.1f}R = {P3*R3:.2f}R banked')
print()
print('OUTCOMES:')
print(f'  PARTIAL_WIN (T1 only): +{partial_r:.2f}R  break-even WR = {be_partial:.1f}%')
print(f'  WIN (T1+T2, stop@T1):  +{win_r:.2f}R  break-even WR = {1/(1+win_r)*100:.1f}%')
print(f'  FULL_WIN (all 3):       +{full_r:.2f}R  break-even WR = {1/(1+full_r)*100:.1f}%')
print()
print('EV AT DIFFERENT WIN RATES:')
for wr_pct in [42, 50, 55, 59, 60, 65]:
    wr = wr_pct / 100
    ev = wr * partial_r - (1 - wr) * 1.0
    status = 'PROFIT' if ev > 0 else 'LOSS  '
    print(f'  WR={wr_pct}%: EV={ev:+.3f}R per trade [{status}]')

print()
print('CURRENT SYSTEM STATS:')
closed = fund[~fund.status.isin(['OPEN', 'PENDING', 'SKIPPED', 'CANCELLED'])].copy()
closed['_pips'] = pd.to_numeric(closed.pips, errors='coerce').fillna(0)
wins   = len(closed[closed._pips > 0])
losses = len(closed[closed._pips < 0])
dec    = wins + losses
wr_cur = wins / dec * 100 if dec > 0 else 0
ev_cur = (wr_cur/100) * partial_r - (1 - wr_cur/100) * 1.0

print(f'  Win rate:       {wr_cur:.1f}% ({wins}W {losses}L, {dec} trades)')
print(f'  EV at cur WR:   {ev_cur:+.3f}R per trade')
print(f'  T1-BE needed:   {be_partial:.1f}% WR')
print(f'  Gap to BE:      {be_partial - wr_cur:+.1f}%')
print()

# Research WR (G8 quality)
ev_59 = 0.59 * partial_r - 0.41 * 1.0
ev_60 = 0.60 * partial_r - 0.41 * 1.0
print('PROJECTION (with exotic-pair contamination removed):')
print(f'  At 59% WR (research baseline):  EV={ev_59:+.3f}R  [{"PROFIT" if ev_59>0 else "LOSS"}]')
print(f'  At 60% WR (attainable target):  EV={ev_60:+.3f}R  [{"PROFIT" if ev_60>0 else "LOSS"}]')
print()

if ev_59 > 0:
    print('VERDICT: System is MATHEMATICALLY PROFITABLE at research WR')
    print('         with current cascade (50/30/20, floors 1R/1.5R/2R)')
else:
    needed = be_partial
    print(f'VERDICT: Need {needed:.1f}% WR to break even on T1-only exits')
    print(f'         T2 hits add {win_r - partial_r:.2f}R per occurrence — critical')

print()
print('FTMO RUNWAY:')
with open('data/fund_state.json') as f:
    fs = json.load(f)
balance  = float(fs.get('balance', 10000))
max_loss = 10000 * 0.10  # 10% total drawdown limit
remaining_loss_room = max_loss - (10000 - balance)
risk_pct = 0.75
trades_left = remaining_loss_room / (balance * risk_pct / 100)
print(f'  Balance:          ${balance:,.2f}')
print(f'  Max total loss:   ${max_loss:,.2f}')
print(f'  Remaining room:   ${remaining_loss_room:,.2f}')
print(f'  @ {risk_pct}% risk:       ~{trades_left:.0f} full losses before FTMO breach')
print(f'  SAFE TO TRADE:    {"YES" if remaining_loss_room > 200 else "CAUTION"}')
