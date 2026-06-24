"""Verification test for _check_capacity_tiered — 11 cases, all must pass."""
import sys, importlib, types

# ── Stub _get_open_fund_count so we control the open count ───────────────────
import daily as _daily_mod

_ORIGINAL_GET = _daily_mod._get_open_fund_count

def _stub(n):
    def _fn():
        return n
    return _fn

def run_check(open_count, conf, regime, fund_state, label, expected_allowed,
              expected_override=None, expected_tier=None, expected_risk=None):
    _daily_mod._get_open_fund_count = _stub(open_count)
    result = _daily_mod._check_capacity_tiered(
        pair="TST/TST",
        confidence=conf,
        regime=regime,
        fund_state=fund_state,
        log_fn=lambda m: None,  # silence
    )
    _daily_mod._get_open_fund_count = _ORIGINAL_GET

    ok = True
    errors = []

    if result["allowed"] != expected_allowed:
        ok = False
        errors.append(f"allowed={result['allowed']} want={expected_allowed}")
    if expected_override is not None and result["is_override"] != expected_override:
        ok = False
        errors.append(f"is_override={result['is_override']} want={expected_override}")
    if expected_tier is not None and result["tier"] != expected_tier:
        ok = False
        errors.append(f"tier={result['tier']} want={expected_tier}")
    if expected_risk is not None and result["risk_pct"] != expected_risk:
        ok = False
        errors.append(f"risk_pct={result['risk_pct']} want={expected_risk}")

    status = "✅" if ok else "❌"
    print(f"{status} [{label}] {result['reason']}")
    if not ok:
        for e in errors:
            print(f"   ERROR: {e}")
    return ok

HEALTHY_FS = {"daily_pnl_pct": -1.0, "unrealised_pnl_pct": 0.0}
DAILY_HIT  = {"daily_pnl_pct": -2.5, "unrealised_pnl_pct": 0.0}
UNREAL_HIT = {"daily_pnl_pct": -0.5, "unrealised_pnl_pct": -4.0}

cases = [
    # open, conf, regime, fund_state, label, allowed, is_override, tier, risk_pct
    (0, 6.0, "TRENDING", HEALTHY_FS, "01 empty slots → normal open",
     True, False, "NORMAL", None),
    (3, 7.0, "RANGING",  HEALTHY_FS, "02 3/4 used → normal open",
     True, False, "NORMAL", None),
    (4, 7.0, "TRENDING", HEALTHY_FS, "03 4/4, conf 7.0 < 7.5 → blocked",
     False, False, "BLOCKED", None),
    (4, 7.5, "TRENDING", HEALTHY_FS, "04 4/4, conf 7.5, healthy → STRONG override",
     True, True, "STRONG", 0.75),
    (4, 8.5, "TRENDING", HEALTHY_FS, "05 4/4, conf 8.5, healthy → ELITE override",
     True, True, "ELITE", 0.50),
    (4, 9.0, "TRENDING", HEALTHY_FS, "06 4/4, conf 9.0 → ELITE override",
     True, True, "ELITE", 0.50),
    (4, 7.5, "RANGING",  HEALTHY_FS, "07 4/4, RANGING → no override",
     False, False, "BLOCKED", None),
    (4, 7.5, "RANGING_LOW_VOL", HEALTHY_FS, "08 4/4, RANGING_LOW_VOL → no override",
     False, False, "BLOCKED", None),
    (4, 8.0, "TRENDING", DAILY_HIT, "09 4/4, daily P&L -2.5% → no override",
     False, False, "BLOCKED", None),
    (4, 8.0, "TRENDING", UNREAL_HIT, "10 4/4, unrealised -4.0% → no override",
     False, False, "BLOCKED", None),
    (5, 9.5, "TRENDING", HEALTHY_FS, "11 5/5 hard cap → blocked",
     False, False, "BLOCKED", None),
]

passed = 0
for case in cases:
    if run_check(*case):
        passed += 1

print(f"\n{passed}/{len(cases)} tests passed")
if passed < len(cases):
    sys.exit(1)
