import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
import pandas as pd, json, numpy as np

print("=" * 60)
print("PROFITABILITY DIAGNOSIS")
print("=" * 60)

df = pd.read_csv("data/trades.csv")
fund = df[df.trade_this.astype(str) == "YES"]
closed = fund[~fund.status.isin(["OPEN", "PENDING", "SKIPPED", "CANCELLED"])].copy()
closed["_pips"] = pd.to_numeric(closed.pips, errors="coerce").fillna(0)
ps_map = lambda p: 0.01 if "JPY" in str(p) else 0.0001

print()
print("── OUTCOME DISTRIBUTION ─────────────────────")
status_counts = closed.status.value_counts()
for s, c in status_counts.items():
    print(f"  {s}: {c} ({c/len(closed)*100:.1f}%)")
total_closed = len(closed)
wins = len(closed[closed._pips > 0])
losses = len(closed[closed._pips < 0])
decisive = wins + losses
wr = wins / decisive * 100 if decisive > 0 else 0
print(f"  WR: {wr:.1f}% ({wins}W {losses}L)")

print()
print("── T1/T2/T3 HIT RATES ─────────────────────────")
def is_hit(col):
    return closed[col].astype(str).str.upper().isin(["TRUE", "YES", "1", "T"])
t1h = closed[is_hit("t1_hit")] if "t1_hit" in closed.columns else pd.DataFrame()
t2h = closed[is_hit("t2_hit")] if "t2_hit" in closed.columns else pd.DataFrame()
t3h = closed[closed.status == "FULL_WIN"]
print(f"  T1 hit: {len(t1h)}/{total_closed} ({len(t1h)/total_closed*100:.1f}%)")
print(f"  T2 hit: {len(t2h)}/{total_closed} ({len(t2h)/total_closed*100:.1f}%)")
print(f"  T3 hit (FULL_WIN): {len(t3h)}/{total_closed} ({len(t3h)/total_closed*100:.1f}%)")

print()
print("── T1/T2/T3 PRICES — PIPS FROM ENTRY ─────────")
rr_rows = []
for _, t in closed.iterrows():
    pair  = str(t.get("pair", ""))
    entry = float(t.get("entry", 0) or 0)
    stop  = float(t.get("stop_loss", 0) or 0)
    t1v   = float(t.get("t1_price", 0) or t.get("target", 0) or 0)
    t2v   = float(t.get("t2_price", 0) or 0)
    t3v   = float(t.get("t3_price", 0) or 0)
    ps    = ps_map(pair)
    pips  = float(t._pips)
    status = str(t.get("status", ""))
    if not (entry and stop and t1v):
        continue
    sp  = abs(entry - stop) / ps
    t1p = abs(t1v - entry) / ps
    t2p = abs(t2v - entry) / ps if t2v else 0
    t3p = abs(t3v - entry) / ps if t3v else 0
    rr1 = t1p / sp if sp else 0
    rr2 = t2p / sp if sp else 0
    rr3 = t3p / sp if sp else 0
    rr_rows.append((int(t.get("id", 0)), pair, sp, t1p, t2p, t3p, rr1, rr2, rr3, pips, status))
    icon = "OK " if rr1 >= 1.5 else "LOW" if rr1 >= 1.0 else "BAD"
    print(f"  {icon} #{int(t.get('id',0))} {pair} "
          f"stop={sp:.0f}p T1={t1p:.0f}p({rr1:.2f}R) "
          f"T2={t2p:.0f}p({rr2:.2f}R) T3={t3p:.0f}p({rr3:.2f}R) "
          f"actual={pips:+.0f}p [{status}]")
if rr_rows:
    avg_rr1 = sum(r[6] for r in rr_rows) / len(rr_rows)
    avg_t1p = sum(r[3] for r in rr_rows) / len(rr_rows)
    avg_t2p = sum(r[4] for r in rr_rows if r[4]) / max(1, sum(1 for r in rr_rows if r[4]))
    avg_sp  = sum(r[2] for r in rr_rows) / len(rr_rows)
    print(f"  Avg stop: {avg_sp:.0f}p  Avg T1: {avg_t1p:.0f}p ({avg_rr1:.2f}R)  Avg T2: {avg_t2p:.0f}p")

print()
print("── ACTUAL EV CALCULATION ───────────────────────")
p_win  = len(closed[closed.status.isin(["WIN", "FULL_WIN"])])
p_part = len(closed[closed.status.isin(["PARTIAL_WIN", "PROTECTED"])])
p_full = len(closed[closed.status == "FULL_WIN"])
p_loss = losses
p_total = p_win + p_part + p_loss  # decisive (no FLAT/EXPIRED)

if p_total == 0:
    p_total = decisive

# Cascade: 35%/35%/30%, floors T1=1R, T2=1.5R, T3=2R
T1P, T2P, T3P = 0.35, 0.35, 0.30
r1, r2, r3 = 1.0, 1.5, 2.0
partial_r = T1P * r1                         # 0.35R
win_r     = T1P * r1 + T2P * r2             # 0.875R
full_r    = T1P * r1 + T2P * r2 + T3P * r3  # 1.475R

part_frac = p_part / p_total if p_total else 0
win_frac  = p_win  / p_total if p_total else 0
loss_frac = p_loss / p_total if p_total else 0

ev = part_frac * partial_r + win_frac * win_r - loss_frac
print(f"  PARTIAL_WIN: {p_part}/{p_total} ({part_frac*100:.1f}%) × {partial_r:.2f}R = {part_frac*partial_r:.3f}R")
print(f"  WIN:         {p_win}/{p_total}  ({win_frac*100:.1f}%) × {win_r:.2f}R  = {win_frac*win_r:.3f}R")
print(f"  LOSS:        {p_loss}/{p_total} ({loss_frac*100:.1f}%) × -1.0R = {-loss_frac:.3f}R")
print(f"  ACTUAL EV:   {ev:+.3f}R per trade")
print(f"  ('WIN' = T1+T2 hit; 'PARTIAL_WIN' = T1 only)")

print()
print("── RESEARCH T2/T3 HIT RATES ───────────────────")
try:
    rdf = pd.read_csv("data/research_trades.csv")
    rc  = rdf[~rdf.status.isin(["OPEN", "PENDING", "NO_PRICE_LEVELS"])].copy()
    if "t1_hit" in rc.columns:
        rt1 = rc[rc.t1_hit.astype(str).str.upper().isin(["TRUE", "YES", "1"])]
        rt2 = rc[rc.t2_hit.astype(str).str.upper().isin(["TRUE", "YES", "1"])] if "t2_hit" in rc.columns else pd.DataFrame()
        rfw = rc[rc.status == "FULL_WIN"]
        rtot = len(rc)
        rw = len(rc[rc.pips.fillna(0).astype(float) > 0]) if "pips" in rc.columns else 0
        rl = len(rc[rc.pips.fillna(0).astype(float) < 0]) if "pips" in rc.columns else 0
        print(f"  Research closed: {rtot}  W:{rw} L:{rl} WR:{rw/(rw+rl)*100:.1f}%" if rw+rl else f"  Research closed: {rtot}")
        print(f"  T1 hit: {len(rt1)}/{rtot} ({len(rt1)/rtot*100:.1f}%)")
        print(f"  T2 hit: {len(rt2)}/{rtot} ({len(rt2)/rtot*100:.1f}%)")
        print(f"  FULL_WIN: {len(rfw)}/{rtot} ({len(rfw)/rtot*100:.1f}%)")
    else:
        print("  No t1_hit column in research_trades")
except Exception as e:
    print(f"  Error: {e}")

print()
print("── CONFIDENCE BAND ANALYSIS ────────────────────")
print("  Win rate by confidence band:")
for lo, hi in [(5,6),(6,7),(7,8),(8,9),(9,10)]:
    conf_col = pd.to_numeric(closed.confidence, errors="coerce").fillna(0)
    band = closed[(conf_col >= lo) & (conf_col < hi)]
    w = len(band[band._pips > 0])
    l = len(band[band._pips < 0])
    d = w + l
    wr_b = w/d*100 if d else 0
    print(f"  conf {lo}-{hi}: {wr_b:.0f}% WR ({w}W {l}L {d} trades)")

print()
print("── SESSION ANALYSIS ────────────────────────────")
if "session_at_entry" in closed.columns:
    for sess in closed.session_at_entry.dropna().unique():
        band = closed[closed.session_at_entry == sess]
        w = len(band[band._pips > 0]); l = len(band[band._pips < 0])
        d = w+l
        print(f"  {sess}: {w/d*100:.0f}% WR ({d} trades)")
else:
    print("  session_at_entry column not yet populated")

print()
print("── REGIME ANALYSIS ─────────────────────────────")
if "regime_at_entry" in closed.columns:
    for reg in closed.regime_at_entry.dropna().unique():
        if str(reg).lower() in ("nan","none",""): continue
        band = closed[closed.regime_at_entry == reg]
        w = len(band[band._pips > 0]); l = len(band[band._pips < 0])
        d = w+l
        if d: print(f"  {reg}: {w/d*100:.0f}% WR ({d} trades)")
else:
    print("  regime_at_entry not yet populated")

print()
print("── WEEKLY TREND ANALYSIS ───────────────────────")
if "weekly_trend_at_entry" in closed.columns:
    for wt in ["UP","DOWN","NEUTRAL"]:
        band = closed[closed.weekly_trend_at_entry == wt]
        w = len(band[band._pips > 0]); l = len(band[band._pips < 0])
        d = w+l
        if d: print(f"  Weekly {wt}: {w/d*100:.0f}% WR ({d} trades)")
else:
    print("  weekly_trend_at_entry not yet populated")

print()
print("── LOSS DETAIL ─────────────────────────────────")
for _, t in closed[closed._pips < 0].iterrows():
    pair = str(t.get("pair",""))
    pips = float(t._pips)
    conf = float(t.get("confidence", 0) or 0)
    entry = float(t.get("entry", 0) or 0)
    stop  = float(t.get("stop_loss", 0) or 0)
    t1v   = float(t.get("t1_price", 0) or t.get("target", 0) or 0)
    ps    = ps_map(pair)
    sp    = abs(entry-stop)/ps if entry and stop else 0
    t1p   = abs(t1v-entry)/ps  if entry and t1v else 0
    rr1   = t1p/sp if sp else 0
    print(f"  LOSS #{int(t.get('id',0))} {pair} {pips:+.0f}p conf={conf:.0f} "
          f"stop={sp:.0f}p T1={t1p:.0f}p({rr1:.2f}R) "
          f"[{t.get('status','')}]")

print()
print("── OPEN TRADE T2 DISTANCE ──────────────────────")
open_f = fund[fund.status == "OPEN"]
for _, t in open_f.iterrows():
    pair   = str(t.get("pair",""))
    entry  = float(t.get("entry",0) or 0)
    stop   = float(t.get("stop_loss",0) or 0)
    t1v    = float(t.get("t1_price",0) or 0)
    t2v    = float(t.get("t2_price",0) or 0)
    t3v    = float(t.get("t3_price",0) or 0)
    ps     = ps_map(pair)
    if not (entry and stop):
        continue
    sp  = abs(entry-stop)/ps
    t1p = abs(t1v-entry)/ps if t1v else 0
    t2p = abs(t2v-entry)/ps if t2v else 0
    t3p = abs(t3v-entry)/ps if t3v else 0
    rr1 = t1p/sp if sp else 0
    rr2 = t2p/sp if sp else 0
    rr3 = t3p/sp if sp else 0
    print(f"  #{int(t.get('id',0))} {pair}: stop={sp:.0f}p "
          f"T1={t1p:.0f}p({rr1:.2f}R) T2={t2p:.0f}p({rr2:.2f}R) T3={t3p:.0f}p({rr3:.2f}R)")
