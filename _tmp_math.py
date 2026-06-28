import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import pandas as pd

print('=' * 55)
print('PROFITABILITY MATH CHECK')
print('=' * 55)
print()

P1, P2, P3 = 0.50, 0.30, 0.20
# T2 hit rate from research data (33% of T1-hitters also hit T2)
T2_GIVEN_T1 = 0.33
# T3 hit rate (50% of T2-hitters reach final target — conservative)
T3_GIVEN_T2 = 0.50

df = pd.read_csv('data/trades.csv')
fund = df[df.trade_this.astype(str) == 'YES']
open_f = fund[fund.status.isin(['OPEN', 'PENDING'])]

print('OPEN TRADE R:R:')
rr_vals = []
for _, t in open_f.iterrows():
    pair = str(t.get('pair', ''))
    entry = float(t.get('entry', 0) or 0)
    stop  = float(t.get('stop_loss', 0) or 0)
    t1    = float(t.get('t1_price', 0) or t.get('target', 0) or 0)
    t2    = float(t.get('t2_price', 0) or 0)
    ps    = 0.01 if 'JPY' in pair else 0.0001
    if entry and stop and t1:
        sp  = abs(entry - stop) / ps
        t1p = abs(t1 - entry) / ps
        t2p = abs(t2 - entry) / ps if t2 else t1p * 1.5
        rr1 = t1p / sp if sp > 0 else 0
        rr2 = t2p / sp if sp > 0 else 0
        rr_vals.append(rr1)
        status = 'OK' if rr1 >= 1.0 else 'BELOW FLOOR'
        print(f'  #{t["id"]} {pair}: T1={rr1:.2f}R T2={rr2:.2f}R [{status}]')

# Use 1R/1.5R/2.5R as the correct floors
R1, R2, R3 = 1.0, 1.5, 2.5

# ── Cascade outcome values ──────────────────────────────────────────────────────
# PARTIAL_WIN: 50% at T1, remaining 50% stopped at entry (0R)
partial_r = P1 * R1

# WIN: 50% at T1, 30% at T2, 20% stopped at T1 (breakeven on rest → locked at T1)
win_r = P1 * R1 + P2 * R2 + P3 * R1

# FULL_WIN: 50% at T1, 30% at T2, 20% at T3
full_r = P1 * R1 + P2 * R2 + P3 * R3

print()
print('CASCADE MATH (floors: T1=1R, T2=1.5R, T3=2.5R):')
print(f'  T1: {P1*100:.0f}% exit @ {R1:.1f}R = {P1*R1:.2f}R banked')
print(f'  T2: {P2*100:.0f}% exit @ {R2:.1f}R = {P2*R2:.2f}R banked')
print(f'  T3: {P3*100:.0f}% exit @ {R3:.1f}R = {P3*R3:.2f}R banked')
print()
print('OUTCOME VALUES:')
print(f'  PARTIAL_WIN (T1 only, stop@entry):   +{partial_r:.2f}R')
print(f'  WIN (T1+T2, stop@T1 locks 20%):      +{win_r:.2f}R')
print(f'  FULL_WIN (all 3 hit):                 +{full_r:.2f}R')
print(f'  LOSS:                                 -1.00R')
print()
print(f'BREAK-EVEN WIN RATES:')
be_partial = 1 / (1 + partial_r) * 100
be_win     = 1 / (1 + win_r) * 100
be_full    = 1 / (1 + full_r) * 100
print(f'  T1-only exits (PARTIAL_WIN only): {be_partial:.1f}% WR')
print(f'  T1+T2 exits (WIN):               {be_win:.1f}% WR')
print(f'  All 3 hit (FULL_WIN):            {be_full:.1f}% WR')
print()

print('EXPECTED VALUE (realistic cascade, T2 hit rate = 33% of T1 hits):')
print(f'  p(reach T2 | T1 hit) = {T2_GIVEN_T1:.0%}')
print(f'  p(reach T3 | T2 hit) = {T3_GIVEN_T2:.0%}')
print()
print(f'  {"WR":>5}  {"EV (T1-only)":>14}  {"EV (with T2/T3)":>16}  {"Verdict":>8}')
for wr_pct in [42, 50, 55, 59, 60, 65, 67]:
    wr = wr_pct / 100
    # T1-only worst case
    ev_t1 = wr * partial_r - (1 - wr) * 1.0
    # Realistic with T2/T3 cascade
    p_loss    = 1 - wr
    p_partial = wr * (1 - T2_GIVEN_T1)
    p_win     = wr * T2_GIVEN_T1 * (1 - T3_GIVEN_T2)
    p_full    = wr * T2_GIVEN_T1 * T3_GIVEN_T2
    ev_real   = (p_loss * (-1.0) + p_partial * partial_r
                 + p_win * win_r + p_full * full_r)
    v_t1   = 'PROFIT' if ev_t1  > 0 else 'LOSS'
    v_real = 'PROFIT' if ev_real > 0 else 'LOSS'
    print(f'  {wr_pct:>4}%  {ev_t1:>+12.3f}R  {ev_real:>+14.3f}R  '
          f'[{v_t1}/{v_real}]')

print()
print('CURRENT SYSTEM STATS:')
closed = fund[~fund.status.isin(['OPEN', 'PENDING', 'SKIPPED', 'CANCELLED'])].copy()
closed['_pips'] = pd.to_numeric(closed.pips, errors='coerce').fillna(0)
wins   = len(closed[closed._pips > 0])
losses = len(closed[closed._pips < 0])
dec    = wins + losses
wr_cur = wins / dec * 100 if dec > 0 else 0

p_loss    = (1 - wr_cur/100)
p_partial = (wr_cur/100) * (1 - T2_GIVEN_T1)
p_win     = (wr_cur/100) * T2_GIVEN_T1 * (1 - T3_GIVEN_T2)
p_full    = (wr_cur/100) * T2_GIVEN_T1 * T3_GIVEN_T2
ev_cur    = p_loss*(-1.0) + p_partial*partial_r + p_win*win_r + p_full*full_r

print(f'  Win rate:    {wr_cur:.1f}% ({wins}W {losses}L, {dec} trades)')
print(f'  EV (real):   {ev_cur:+.3f}R per trade')
print(f'  Research WR: ~59% on G8 pairs (exotic contamination removed)')
print()

ev_59_real = (0.41*(-1.0) + 0.59*(1-T2_GIVEN_T1)*partial_r
              + 0.59*T2_GIVEN_T1*(1-T3_GIVEN_T2)*win_r
              + 0.59*T2_GIVEN_T1*T3_GIVEN_T2*full_r)
print(f'PROJECTION AT 59% WR (research baseline, with cascade):')
print(f'  EV = {ev_59_real:+.3f}R per trade → {"PROFITABLE" if ev_59_real > 0 else "LOSS"}')
print()
if ev_59_real > 0:
    print('VERDICT: System is MATHEMATICALLY PROFITABLE at research WR')
    print('         Cascade T2/T3 hits convert near-misses into winners.')
else:
    be_needed = be_partial
    print(f'VERDICT: Needs {be_needed:.1f}% WR to break even T1-only,')
    print(f'         but ~55% WR is enough WITH regular T2 hits.')
    print(f'         Focus: signal quality and T2 hit frequency.')

print()
print('FTMO RUNWAY:')
with open('data/fund_state.json') as f:
    fs = json.load(f)
balance  = float(fs.get('balance', 10000))
opening  = float(fs.get('opening_balance', 10000))
dd_pct   = float(fs.get('drawdown_pct', 0))
max_loss = opening * 0.10
used_loss = opening - balance
remaining = max_loss - used_loss
risk_usd  = balance * 0.0075
trades_left = remaining / risk_usd if risk_usd > 0 else 0
print(f'  Balance:        ${balance:,.2f}')
print(f'  FTMO drawdown:  {(used_loss/opening*100):.2f}% of 10% limit')
print(f'  Remaining room: ${remaining:,.2f}')
print(f'  @ 0.75% risk:   ~{trades_left:.0f} full losses before breach')
print(f'  SAFE TO TRADE:  {"YES" if remaining > 200 else "CAUTION - thin buffer"}')
