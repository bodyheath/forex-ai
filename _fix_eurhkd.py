"""One-shot script to correct EUR/HKD id=1030 outcome and fund_state.json."""
import sys, json, math
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from src import tracker as _trk, fund_state as _fs

REC_ID = 1030

# ── 1. Verify row state ────────────────────────────────────────────────────────
rows = _trk.load()
row = next((r for r in rows if str(r.get("id")) == str(REC_ID)), None)
if row is None:
    print(f"ERROR: id={REC_ID} not found"); sys.exit(1)

print(f"id={REC_ID} current state:")
for k in ["status","t1_hit","t2_hit","t1_hit_pips","t2_hit_pips",
          "entry","stop_loss","effective_stop","exit_price","cascading_total_pips_weighted"]:
    print(f"  {k}: {row.get(k, '')!r}")

t1_hit  = str(row.get("t1_hit","")).strip().upper() in ("TRUE","YES","1","T")
t2_hit  = str(row.get("t2_hit","")).strip().upper() in ("TRUE","YES","1","T")
if not (t1_hit and t2_hit):
    print("ERROR: T1/T2 not both hit in CSV — aborting"); sys.exit(1)

# ── 2. Calculate cascading pips ───────────────────────────────────────────────
def sf(v, d=0.0):
    try:
        r = float(v); return r if r == r else d
    except: return d

t1_pips = sf(row.get("t1_hit_pips"))  # 174.8
t2_pips = sf(row.get("t2_hit_pips"))  # 305.9
total   = round(t1_pips + t2_pips, 1)
weighted = round(0.40 * t1_pips + 0.30 * t2_pips, 1)
print(f"\nCascade pips: total={total}, weighted={weighted}")

# ── 3. Update cascading fields first ─────────────────────────────────────────
_trk.update_fields(REC_ID,
    cascading_total_pips=total,
    cascading_total_pips_weighted=weighted,
)
print(f"update_fields done: cascading_total_pips={total}, weighted={weighted}")

# ── 4. Close with WIN outcome ─────────────────────────────────────────────────
exit_price = sf(row.get("effective_stop") or row.get("stop_loss"))
updated = _trk.update_outcome(
    REC_ID, "WIN",
    exit_price=exit_price,
    notes=f"Manual fix: WIN T1+T2 hit, stop at {exit_price} | cascade {weighted:.1f}p weighted",
    cascading_pips=weighted,
)
print(f"update_outcome done: status={updated.get('status')} pips={updated.get('pips')} r_mult={updated.get('r_multiple')}")

# ── 5. Update fund_state.json ──────────────────────────────────────────────────
state = _fs.load()
entry     = sf(row.get("entry"))
stop_loss = sf(row.get("stop_loss"))
pip_size  = 0.01 if "JPY" in row.get("pair","") else 0.0001
stop_pips = abs(entry - stop_loss) / pip_size if entry and stop_loss else 0
sz_pct    = sf(row.get("position_size_pct_at_entry"), 1.0) or 1.0
bal       = sf(state.get("daily_opening_balance"), 10000.0) or 10000.0
risk_usd  = sz_pct / 100.0 * max(bal, 1)
dpp       = risk_usd / stop_pips if stop_pips > 0 else 1.0
profit    = round(weighted * dpp, 2)

print(f"\nFund P&L calc:")
print(f"  balance={bal}, stop_pips={stop_pips:.1f}, sz_pct={sz_pct}")
print(f"  dpp={dpp:.6f}, weighted={weighted}p, profit=${profit:.2f}")

old_pnl = sf(state.get("daily_pnl_dollars"), 0.0)
state["daily_pnl_dollars"] = round(old_pnl + profit, 2)
if bal > 0:
    state["daily_pnl_pct"] = round(state["daily_pnl_dollars"] / bal * 100, 4)

_fs.save(state)
print(f"\nfund_state saved:")
print(f"  daily_pnl_dollars: {old_pnl} -> {state['daily_pnl_dollars']}")
print(f"  daily_pnl_pct: {state.get('daily_pnl_pct')}")

# ── 6. Verify ─────────────────────────────────────────────────────────────────
rows2 = _trk.load()
r2 = next((r for r in rows2 if str(r.get("id")) == str(REC_ID)), None)
print(f"\nVerification id={REC_ID}:")
for k in ["status","pips","r_multiple","cascading_total_pips_weighted","exit_price","closed_at"]:
    print(f"  {k}: {r2.get(k,'')!r}")

state2 = _fs.load()
print(f"\nVerification fund_state:")
print(f"  daily_pnl_dollars: {state2.get('daily_pnl_dollars')}")
print(f"  daily_pnl_pct: {state2.get('daily_pnl_pct')}")
print("\nDone.")
