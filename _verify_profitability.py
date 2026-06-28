import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import pandas as pd, json

print("=" * 60)
print("PROFITABILITY VERIFICATION — POST CHANGES")
print("=" * 60)

# ── Step 1: Confirm cascade constants ────────────────────────────────────────
print()
print("1. CASCADE CONSTANTS (from src/cascade.py):")
import importlib.util, sys as _sys
spec = importlib.util.spec_from_file_location("cascade", "src/cascade.py")
casc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(casc)
print(f"   T1_SIZE = {casc.T1_SIZE}  ({casc.T1_SIZE*100:.0f}% exit at T1)")
print(f"   T2_SIZE = {casc.T2_SIZE}  ({casc.T2_SIZE*100:.0f}% exit at T2)")
print(f"   T3_SIZE = {casc.T3_SIZE}  ({casc.T3_SIZE*100:.0f}% exit at T3)")
assert casc.T1_SIZE + casc.T2_SIZE + casc.T3_SIZE == 1.0, "SIZES DON'T SUM TO 1!"
print(f"   Sum = {casc.T1_SIZE+casc.T2_SIZE+casc.T3_SIZE:.1f}  OK")

# ── Step 2: Confirm financials.py constants ─────────────────────────────────
print()
print("2. FINANCIALS CONSTANTS (from src/trading/financials.py):")
import re
with open("src/trading/financials.py") as f:
    fin_text = f.read()
for const in ["T1_SIZE", "T2_SIZE", "T3_SIZE"]:
    m = re.search(rf"{const}\s*=\s*([0-9.]+)", fin_text)
    if m:
        print(f"   {const} = {m.group(1)}")
    else:
        print(f"   {const} NOT FOUND!")

# ── Step 3: Confirm daily.py changes ────────────────────────────────────────
print()
print("3. DAILY.PY RISK SETTINGS:")
with open("daily.py", encoding="utf-8", errors="replace") as f:
    daily_text = f.read()
m_risk = re.search(r"BASE_RISK_PCT\s*=\s*([0-9.]+)", daily_text)
m_atr  = re.search(r"_yt_atr14\s*\*\s*([0-9.]+)", daily_text)
print(f"   BASE_RISK_PCT = {m_risk.group(1) if m_risk else 'NOT FOUND'}")
print(f"   ATR stop cap  = {m_atr.group(1) if m_atr else 'NOT FOUND'}x")

# ── Step 4: Break-even math with new cascade ─────────────────────────────────
print()
print("4. BREAK-EVEN MATH — NEW CASCADE (50/30/20):")
P1, P2, P3 = casc.T1_SIZE, casc.T2_SIZE, casc.T3_SIZE
R1, R2, R3 = 1.0, 1.5, 2.0   # minimum R:R floors

# After T1: stop moves to entry. Remaining (P2+P3) risks 0R.
# After T2: stop moves to T1. Remaining P3 exits at ≥1R.
partial_r = P1 * R1
win_r     = P1 * R1 + P2 * R2 + P3 * R1   # P3 exits at T1 stop (=1R min)
full_r    = P1 * R1 + P2 * R2 + P3 * R3

be_partial = 1 / (1 + partial_r) * 100
be_win     = 1 / (1 + win_r)     * 100
be_full    = 1 / (1 + full_r)    * 100

print(f"   PARTIAL_WIN (T1 only): net +{partial_r:.2f}R  BE WR = {be_partial:.1f}%")
print(f"   WIN (T1+T2):           net +{win_r:.2f}R  BE WR = {be_win:.1f}%")
print(f"   FULL_WIN (all):        net +{full_r:.2f}R  BE WR = {be_full:.1f}%")

print()
print("   Compare OLD cascade (35/35/30):")
old_partial = 0.35 * 1.0
old_win     = 0.35*1.0 + 0.35*1.5 + 0.30*1.0
old_full    = 0.35*1.0 + 0.35*1.5 + 0.30*2.0
print(f"   OLD PARTIAL_WIN net: {old_partial:.2f}R  BE WR = {1/(1+old_partial)*100:.1f}%")
print(f"   OLD WIN net:         {old_win:.2f}R  BE WR = {1/(1+old_win)*100:.1f}%")
print(f"   OLD FULL_WIN net:    {old_full:.2f}R  BE WR = {1/(1+old_full)*100:.1f}%")

# ── Step 5: EV at different WRs ─────────────────────────────────────────────
print()
print("5. EXPECTED VALUE AT DIFFERENT WIN RATES:")
print("   (assumes T2 hits 33% of T1-hitters, based on research data)")
t2_rate = 0.33
for wr in [0.42, 0.50, 0.55, 0.60, 0.65]:
    # T1-only wins: wr × (1-t2_rate)
    # T1+T2 wins: wr × t2_rate
    # Losses: (1-wr)
    ev = wr*(1-t2_rate)*partial_r + wr*t2_rate*win_r - (1-wr)*1.0
    ev_old = wr*(1-t2_rate)*old_partial + wr*t2_rate*old_win - (1-wr)*1.0
    flag = "PROFITABLE" if ev > 0 else "losing    "
    print(f"   WR {wr*100:.0f}%: NEW EV={ev:+.3f}R [{flag}]  OLD EV={ev_old:+.3f}R")

# ── Step 6: Backtest on historical data ─────────────────────────────────────
print()
print("6. BACKTEST — Historical trades with new cascade:")
print("   (comparing actual pips vs what new system would return)")
df   = pd.read_csv("data/trades.csv")
fund = df[df.trade_this.astype(str) == "YES"]
closed = fund[~fund.status.isin(["OPEN","PENDING","SKIPPED","CANCELLED"])].copy()
closed["_pips"] = pd.to_numeric(closed.pips, errors="coerce").fillna(0)
ps_map = lambda p: 0.01 if "JPY" in str(p) else 0.0001

balance_old = 10000.0
balance_new = 10000.0
old_risk = 1.0   # old BASE_RISK_PCT
new_risk = 0.75  # new BASE_RISK_PCT

for _, t in closed.sort_values("closed_at").iterrows():
    pair   = str(t.get("pair",""))
    entry  = float(t.get("entry", 0) or 0)
    stop   = float(t.get("stop_loss", 0) or 0)
    t1v    = float(t.get("t1_price", 0) or t.get("target", 0) or 0)
    t1h    = str(t.get("t1_hit","")).upper() in ("TRUE","YES","1")
    t2h    = str(t.get("t2_hit","")).upper() in ("TRUE","YES","1")
    status = str(t.get("status",""))
    pips   = float(t._pips)
    ps     = ps_map(pair)
    if not (entry and stop):
        continue
    sp = abs(entry - stop) / ps

    # OLD: risk 1.0% of balance, old cascade 35/35/30
    risk_d_old = balance_old * old_risk / 100
    dpp_old    = risk_d_old / sp if sp else 0
    if pips > 0:  # was a win
        t1p = abs(t1v - entry) / ps if t1v else 0
        if t1h and t2h:
            pnl_old = dpp_old * (0.35*t1p + 0.35*min(pips, t1p*2) + 0.30*pips)
        elif t1h:
            pnl_old = dpp_old * 0.35 * t1p
        else:
            pnl_old = dpp_old * pips * 0.35
    else:
        pnl_old = -risk_d_old
    balance_old += pnl_old

    # NEW: risk 0.75% of balance, new cascade 50/30/20
    risk_d_new = balance_new * new_risk / 100
    dpp_new    = risk_d_new / sp if sp else 0
    if pips > 0:
        t1p = abs(t1v - entry) / ps if t1v else 0
        if t1h and t2h:
            pnl_new = dpp_new * (0.50*t1p + 0.30*min(pips, t1p*2) + 0.20*pips)
        elif t1h:
            pnl_new = dpp_new * 0.50 * t1p
        else:
            pnl_new = dpp_new * pips * 0.50
    else:
        pnl_new = -risk_d_new

    balance_new += pnl_new
    print(f"   #{int(t.get('id',0))} {pair} [{status}] "
          f"pips={pips:+.0f}  "
          f"OLD=${pnl_old:+.2f} (bal={balance_old:,.2f})  "
          f"NEW=${pnl_new:+.2f} (bal={balance_new:,.2f})")

print()
print(f"   OLD system end balance: ${balance_old:,.2f}  ({balance_old-10000:+.2f})")
print(f"   NEW system end balance: ${balance_new:,.2f}  ({balance_new-10000:+.2f})")
print(f"   Improvement: ${balance_new-balance_old:+.2f}")

# ── Step 7: Open trade quality check ────────────────────────────────────────
print()
print("7. OPEN TRADE QUALITY CHECK:")
open_f = fund[fund.status == "OPEN"]
for _, t in open_f.iterrows():
    pair  = str(t.get("pair",""))
    entry = float(t.get("entry", 0) or 0)
    stop  = float(t.get("stop_loss", 0) or 0)
    t1v   = float(t.get("t1_price", 0) or t.get("target", 0) or 0)
    t2v   = float(t.get("t2_price", 0) or 0)
    ps    = ps_map(pair)
    if not (entry and stop):
        continue
    sp   = abs(entry - stop) / ps
    t1p  = abs(t1v - entry) / ps if t1v else 0
    t2p  = abs(t2v - entry) / ps if t2v else 0
    rr1  = t1p / sp if sp else 0
    rr2  = t2p / sp if sp else 0
    ok   = "OK " if rr1 >= 1.0 else "BAD"
    print(f"   {ok} #{int(t.get('id',0))} {pair}: stop={sp:.0f}p  T1={t1p:.0f}p({rr1:.2f}R)  T2={t2p:.0f}p({rr2:.2f}R)")

# ── Step 8: Summary ──────────────────────────────────────────────────────────
print()
print("=" * 60)
print("SUMMARY")
print("=" * 60)
print()
print(f"  Cascade: 50/30/20 (was 35/35/30)")
print(f"  PARTIAL_WIN payout: {partial_r:.2f}R (was {old_partial:.2f}R — +{(partial_r-old_partial)*100:.0f}% improvement)")
print(f"  WIN payout:         {win_r:.2f}R (was {old_win:.2f}R)")
print(f"  PARTIAL_WIN break-even WR: {be_partial:.1f}% (was {1/(1+old_partial)*100:.1f}%)")
print(f"  ATR stop cap: 2.0x (was 2.5x)")
print(f"  Default risk: 0.75% (was 1.0%)")
print()
print("  TO REACH GENUINE PROFITABILITY:")
print(f"  Need T1 to hit ~60%+ of trades (plausible — T1 at 1R)")
print(f"  Research WR = 59% on similar G8 setups (encouraging)")
print(f"  Exotic pairs now banned — removes worst losers")
print()
print("  MINIMUM SAMPLE FOR CONFIDENCE:")
print("  Need 30+ closed fund trades to validate the edge")
print("  At ~2-4 trades/week: 8-15 weeks of data needed")
print()
print("  FTMO READINESS:")
dd = float(json.load(open("data/fund_state.json")).get("drawdown_pct", 0))
buf = 10 - dd
print(f"  Current drawdown: {dd:.2f}%  Buffer: {buf:.2f}%")
print(f"  At 0.75% risk: each loss = ~${10000*0.0075:.0f}")
print(f"  Buffer allows ~{int(buf*100/75)} more full losses before breach")
print()
if buf > 3:
    print("  RECOMMENDATION: Continue paper trading to build 30-trade")
    print("  sample. Do NOT attempt FTMO Phase 1 yet.")
    print("  Target: 30 fund trades, 55%+ WR, then assess.")
else:
    print("  WARNING: Buffer is thin. Ultra-conservative mode.")
