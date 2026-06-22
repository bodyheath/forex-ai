import os, sys, time
sys.path.insert(0, '.')

_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env):
    with open(_env, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

from src.discord_notifier import send_fund_approaching, _progress_bar

# Verify progress bar capping locally first
assert _progress_bar(50)  == "██████████░░░░░░░░░░", f"50% bar wrong: {_progress_bar(50)!r}"
assert _progress_bar(100) == "████████████████████", f"100% bar wrong"
assert _progress_bar(224) == "████████████████████", f">100% should be full bar"
assert _progress_bar(0)   == "░░░░░░░░░░░░░░░░░░░░", f"0% bar wrong"
print("_progress_bar() capping: OK")
print()

print("Test A: Normal HOT zone (74% progress) — should show progress bar + 'Approaching T1'")
r = send_fund_approaching(
    pair="AUD/JPY", direction="BUY", progress_pct=74,
    target_price=113.478, current_price=113.369, distance_pips=10.9,
    stop_price=112.680, milestone="T1", entry_price=113.250, is_fund=True,
)
print(f"  -> {'Sent' if r else 'FAILED (no webhook?)'} to #monitor")
time.sleep(2)

print("Test B: HOT zone at exactly 100% — full bar, still shows 'Approaching T1' (not crossed yet)")
r = send_fund_approaching(
    pair="EUR/USD", direction="BUY", progress_pct=100,
    target_price=1.09500, current_price=1.09500, distance_pips=0.0,
    stop_price=1.08800, milestone="T1", entry_price=1.08900, is_fund=True,
)
print(f"  -> {'Sent' if r else 'FAILED (no webhook?)'} to #monitor")
time.sleep(2)

print("Test C: TARGET ALREADY CROSSED (224% progress) — should show 'TARGET ALREADY CROSSED' title")
r = send_fund_approaching(
    pair="GBP/USD", direction="BUY", progress_pct=224,
    target_price=1.26500, current_price=1.28300, distance_pips=-18.0,
    stop_price=1.25200, milestone="T1", entry_price=1.25800, is_fund=True,
)
print(f"  -> {'Sent' if r else 'FAILED (no webhook?)'} to #monitor")
time.sleep(2)

print("Test D: Research trade TARGET ALREADY CROSSED (150%)")
r = send_fund_approaching(
    pair="EUR/AUD", direction="SELL", progress_pct=150,
    target_price=1.63000, current_price=1.62400, distance_pips=-6.0,
    stop_price=1.64000, milestone="T2", entry_price=1.63800, is_fund=False,
)
print(f"  -> {'Sent' if r else 'FAILED (no webhook?)'} to #monitor")
time.sleep(2)

print("Test E: STOP approaching — negative progress (-74%) — should show 'Approaching STOP LOSS' (red)")
r = send_fund_approaching(
    pair="NZD/CAD", direction="BUY", progress_pct=-74,
    target_price=0.81727, current_price=0.81212, distance_pips=14.0,
    stop_price=0.81072, milestone="STOP", entry_price=0.81900, is_fund=True,
)
print(f"  -> {'Sent' if r else 'FAILED (no webhook?)'} to #monitor")
time.sleep(2)

print("Test F: Research trade approaching stop (-30% progress)")
r = send_fund_approaching(
    pair="USD/CHF", direction="SELL", progress_pct=-30,
    target_price=0.80220, current_price=0.80640, distance_pips=25.0,
    stop_price=0.81060, milestone="STOP", entry_price=0.80640, is_fund=False,
)
print(f"  -> {'Sent' if r else 'FAILED (no webhook?)'} to #monitor")

print()
print("All 6 HOT zone tests sent!")
print("  #monitor: Tests A-F")
print("  Expected:")
print("    A = 74% green bar, title: Approaching T1")
print("    B = 100% full bar, title: Approaching T1 (not crossed — exactly at)")
print("    C = TARGET ALREADY CROSSED (orange), 224% past")
print("    D = TARGET ALREADY CROSSED (orange), research trade")
print("    E = Approaching STOP LOSS (red), fund trade, -74%")
print("    F = Approaching STOP LOSS (red), research trade, -30%")
