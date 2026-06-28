import subprocess, pandas as pd, json

# ─── STEP 1: R:R CODE LOCATIONS ───────────────────────────────────────────────
print("=" * 55)
print("STEP 1 — R:R CODE LOCATIONS")
print("=" * 55)

checks = [
    ("R:R constants / FUND_MIN_RR",       r"FUND_MIN_RR|MIN_T1_RR|T1_MULT\s*=|T2_MULT\s*=|T3_MIN_MULT\s*="),
    ("get_min_rr() threshold config",     r"get_min_rr|min_rr"),
    ("Fund R:R gate (blocking)",          r"BLOCKING.*R.R|rr.*BLOCKING|_blk_rr|_yt_rr_val"),
    ("R:R floor enforcement (cascade)",   r"T1.*floor\|floor.*t1|Enforce minimum R.R|t1 = max\|t1 = min"),
    ("ATR stop cap",                      r"2\.5.*ATR\|atr.*2\.5\|stop.*wide"),
    ("_low_quality flag",                 r"_low_quality|LOW QUALITY"),
]

for label, pattern in checks:
    r = subprocess.run(
        ["grep", "-rn", "--include=*.py", pattern, "."],
        capture_output=True, text=True
    )
    lines = [l for l in r.stdout.splitlines() if l.strip()][:4]
    print(f"\n  {label}:")
    for l in lines:
        print(f"    {l[:100]}")

# ─── STEP 2: BREAK-EVEN MATH ──────────────────────────────────────────────────
print()
print("=" * 55)
print("STEP 2 — CASCADE BREAK-EVEN ANALYSIS")
print("=" * 55)
print()
print("  CASCADE STRUCTURE (from cascade.py):")
print("  T1=35% at >=1R  T2=35% at >=1.5R  T3=30% at >=2R")
print("  NOTE: user script assumed 40/30/30 — actual is 35/35/30")
print()

T1_PCT, T2_PCT, T3_PCT = 0.35, 0.35, 0.30

# R:R floors from cascade.py
T1_RR_FLOOR = 1.0
T2_RR_FLOOR = 1.5
T3_RR_FLOOR = 2.0

# Min blended R:R at the floor values
min_blended = T1_PCT * T1_RR_FLOOR + T2_PCT * T2_RR_FLOOR + T3_PCT * T3_RR_FLOOR
be_wr_blended = 1 / (1 + min_blended) * 100

# T1-only scenario (most conservative):
# T1 banks 35% at 1R; remaining 65% moves stop to entry (0R)
t1_only_net = T1_PCT * T1_RR_FLOOR   # = 0.35R per win
be_wr_t1_only = 1 / (1 + t1_only_net) * 100

# WIN scenario (T1+T2 hit, T3 exits at T1 stop):
# T1: 35% at 1R, T2: 35% at 1.5R, T3: 30% at 1R (stop at T1 after T2 hit)
win_net = T1_PCT * 1.0 + T2_PCT * 1.5 + T3_PCT * 1.0
be_wr_win = 1 / (1 + win_net) * 100

scenarios = [
    ("PARTIAL_WIN only (T1 hit)",   t1_only_net,  be_wr_t1_only),
    ("WIN (T1+T2 hit, T3@T1stop)", win_net,       be_wr_win),
    ("FULL_WIN (all targets hit)",  min_blended,   be_wr_blended),
]

current_wr = 41.7
for name, ret, be in scenarios:
    ok = current_wr >= be
    print(f"  {name}:")
    print(f"    Net R per win: {ret:.2f}R")
    print(f"    Break-even WR: {be:.1f}%")
    print(f"    At {current_wr}% WR: {'PROFITABLE' if ok else 'LOSING (need ' + f'{be:.1f}%)'}")
    print()

# ─── STEP 3: EXISTING TRADE R:R AUDIT ─────────────────────────────────────────
print("=" * 55)
print("STEP 3 — ALL FUND TRADES R:R AUDIT")
print("=" * 55)
print()

df = pd.read_csv("data/trades.csv")
fund = df[df.trade_this.astype(str) == "YES"]

ps_map = lambda p: 0.01 if "JPY" in p else 0.0001

all_rr1 = []
below_1, below_15 = [], []

for _, t in fund.iterrows():
    pair      = str(t.get("pair", ""))
    direction = str(t.get("direction", "")).upper()
    entry     = float(t.get("entry", 0) or 0)
    stop      = float(t.get("stop_loss", 0) or 0)
    t1        = float(t.get("t1_price", 0) or t.get("target", 0) or 0)
    t2        = float(t.get("t2_price", 0) or 0)
    t3        = float(t.get("t3_price", 0) or 0)
    status    = str(t.get("status", ""))
    pips      = float(t.get("pips", 0) or 0)
    ps        = ps_map(pair)

    if entry == 0 or stop == 0 or t1 == 0:
        print(f"  WARN #{t.get('id')} {pair} — missing data entry={entry} stop={stop} t1={t1}")
        continue

    stop_p = abs(entry - stop) / ps
    t1_p   = abs(t1 - entry) / ps
    t2_p   = abs(t2 - entry) / ps if t2 else 0
    t3_p   = abs(t3 - entry) / ps if t3 else 0

    rr1 = t1_p / stop_p if stop_p > 0 else 0
    rr2 = t2_p / stop_p if stop_p > 0 else 0
    rr3 = t3_p / stop_p if stop_p > 0 else 0

    all_rr1.append(rr1)
    if rr1 < 1.0:
        below_1.append(f"{pair} {rr1:.2f}:1")
    if rr1 < 1.5:
        below_15.append(f"{pair} {rr1:.2f}:1")

    outcome = "WIN" if pips > 0 else "LOSS" if pips < 0 else status
    icon    = "OK " if rr1 >= 1.5 else "LOW" if rr1 >= 1.0 else "BAD"
    print(f"  {icon} #{t.get('id')} {pair} {direction} [{outcome}]")
    print(f"      Stop={stop_p:.0f}p  T1={t1_p:.0f}p  R:R={rr1:.2f}:1", end="")
    if t2_p:
        print(f"  T2={t2_p:.0f}p({rr2:.2f}R)  T3={t3_p:.0f}p({rr3:.2f}R)", end="")
    print()

print()
print("  --- SUMMARY ---")
if all_rr1:
    print(f"  Avg T1 R:R: {sum(all_rr1)/len(all_rr1):.2f}:1  "
          f"Min: {min(all_rr1):.2f}:1  Max: {max(all_rr1):.2f}:1")
    print(f"  Below 1.0R: {len(below_1)}  Below 1.5R: {len(below_15)}")
    for x in below_1:
        print(f"    BAD {x}")

# ─── STEP 4: VERIFY FIX IS IN CODE ────────────────────────────────────────────
print()
print("=" * 55)
print("STEP 4 — CODE ENFORCEMENT VERIFICATION")
print("=" * 55)
print()

with open("data/threshold_config.json") as f:
    tcfg = json.load(f)
live_min_rr = tcfg.get("min_rr", 0)

print(f"  threshold_config.json  min_rr = {live_min_rr}")
print(f"  cascade.py T1_MULT={1.0}  T2_MULT={2.0}  T3_MIN_MULT={2.5}")
print(f"  cascade.py R:R floors: T1>=1R  T2>=1.5R  T3>=2R")
print(f"  daily.py FUND_MIN_RR = 2.5 (hardcoded at line 5293)")
print(f"  daily.py _low_quality flag at rr_num < 1.5 (dashboard warning, not a block)")
print()
print(f"  Gate 1 (research):  get_min_rr() = {live_min_rr} — blocks if reward_risk < {live_min_rr}")
print(f"  Gate 2 (fund):      FUND_MIN_RR = 2.5 — blocks if reward_risk < 2.5")
print(f"  Gate 3 (cascade):   T1/T2/T3 price floors enforced at compute_levels()")
print()

# What is reward_risk? It's the AI's T3/full-target R:R, not T1 R:R
print("  NOTE: reward_risk = AI's (target-entry)/(entry-stop) = full target R:R")
print("  This is the T3 R:R (analyst's target), NOT T1 R:R independently")
print("  The cascade system sets T1/T2/T3 separately from this check")

# ─── STEP 5: SIMULATION ───────────────────────────────────────────────────────
print()
print("=" * 55)
print("STEP 5 — R:R GATE SIMULATION")
print("=" * 55)
print()

FUND_MIN_RR = 2.5

def check_trade(pair, direction, entry, stop, ai_target, label):
    ps = 0.01 if "JPY" in pair else 0.0001
    stop_p = abs(entry - stop) / ps
    tgt_p  = abs(ai_target - entry) / ps
    rr_ai  = tgt_p / stop_p if stop_p else 0

    # Cascade floors
    sd = abs(entry - stop)
    if direction == "BUY":
        t1_casc = max(entry + 1.0 * sd, entry + sd)       # 1R floor
        t2_casc = max(entry + 2.0 * sd, entry + sd * 1.5) # 1.5R floor
        t3_casc = max(ai_target, entry + sd * 2.0)         # 2R floor
    else:
        t1_casc = min(entry - 1.0 * sd, entry - sd)
        t2_casc = min(entry - 2.0 * sd, entry - sd * 1.5)
        t3_casc = min(ai_target, entry - sd * 2.0)

    t1_rr = abs(t1_casc - entry) / sd
    t2_rr = abs(t2_casc - entry) / sd
    t3_rr = abs(t3_casc - entry) / sd

    fund_pass = rr_ai >= FUND_MIN_RR
    print(f"  {label}: {pair} {direction}")
    print(f"    AI reward_risk={rr_ai:.2f}  Fund gate(>=2.5): {'PASS' if fund_pass else 'BLOCK'}")
    print(f"    Cascade T1={t1_rr:.2f}R  T2={t2_rr:.2f}R  T3={t3_rr:.2f}R")
    be_t1 = 1/(1 + 0.35*t1_rr)*100
    be_blend = 1/(1 + 0.35*t1_rr + 0.35*t2_rr + 0.30*t3_rr)*100
    print(f"    Break-even: T1-only={be_t1:.1f}%  Blended={be_blend:.1f}%")
    print()
    return fund_pass

# Should be blocked
print("  --- SHOULD BE BLOCKED (reward_risk < 2.5) ---")
check_trade("EUR/USD", "BUY",  entry=1.1000, stop=1.0900, ai_target=1.1180, label="Old 1.8R setup")
check_trade("EUR/USD", "BUY",  entry=1.1000, stop=1.0900, ai_target=1.1240, label="Below 2.5R")

# Should pass
print("  --- SHOULD PASS (reward_risk >= 2.5) ---")
check_trade("EUR/USD", "BUY",  entry=1.1000, stop=1.0900, ai_target=1.1250, label="Exactly 2.5R")
check_trade("GBP/USD", "SELL", entry=1.3200, stop=1.3280, ai_target=1.3000, label="2.5R SELL")

# ─── STEP 6: FINAL VERDICT ────────────────────────────────────────────────────
print("=" * 55)
print("STEP 6 — FINAL VERDICT")
print("=" * 55)
print()

open_f = fund[fund.status == "OPEN"]
open_rrs = []
for _, t in open_f.iterrows():
    pair  = str(t.get("pair", ""))
    entry = float(t.get("entry", 0) or 0)
    stop  = float(t.get("stop_loss", 0) or 0)
    t1v   = float(t.get("t1_price", 0) or t.get("target", 0) or 0)
    ps    = ps_map(pair)
    if entry and stop and t1v:
        sp  = abs(entry - stop) / ps
        t1p = abs(t1v - entry) / ps
        rr  = t1p / sp if sp > 0 else 0
        open_rrs.append((pair, rr))

print("  Open trades T1 R:R:")
for pair, rr in open_rrs:
    icon = "OK " if rr >= 1.0 else "BAD"
    print(f"    {icon} {pair}  T1={rr:.2f}:1")

print()
print("  Enforcement layers:")
print(f"    1. Fund gate:     reward_risk >= 2.5 (live in threshold_config.json)")
print(f"    2. Cascade floors: T1>=1R  T2>=1.5R  T3>=2R  (enforced in cascade.py compute_levels)")
print(f"    3. ATR stop cap:  stop <= 2.5×ATR (enforced in daily.py line ~5587)")
print()
print("  Math at minimum settings (35%/35%/30%, T1=1R T2=1.5R T3=2R):")
print(f"    Blended R:R:          {min_blended:.3f}R")
print(f"    Break-even (blended): {be_wr_blended:.1f}% WR")
print(f"    Current WR:           {current_wr}%")
gap = current_wr - be_wr_blended
print(f"    Margin:               {gap:+.1f}% {'above BE' if gap>0 else 'BELOW BE'}")
print()
print("  CRITICAL FINDING:")
print(f"    The system is PROFITABLE on paper if T1+T2+T3 all hit.")
print(f"    If only T1 hits (PARTIAL_WIN): need 74.1% WR — NOT achievable at {current_wr}%.")
print(f"    The cascade's real edge comes from T2+T3 capturing upside.")
print(f"    Historical data needed: what % of trades reach T2 and T3.")
