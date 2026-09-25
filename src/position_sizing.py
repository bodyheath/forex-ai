"""Volatility/loss-streak/drawdown-adjusted position sizing.

Extracted from daily.py 2026-09-2X so fund_state.py's display-facing
update_sizing_state() can run the exact same chained calculation real
trade-creation already uses (daily.py's real call site: fund_state.py's
own drawdown-tier compute_sizing() first, then calculate_position_size()
below layered on top, keeping whichever result is lower) -- previously the
display path only ever ran the first half of that chain, so fund_state.
json's sizing_mode/current_sizing_pct could show a LESS conservative
number than what a real new trade would actually receive (e.g. a real
loss streak pushes the real sizing down to MINIMAL/0.25%, while the
display kept showing the drawdown-only figure, drawdown_caution/0.75%).

Pure arithmetic, no I/O, no API calls -- safe to import from anywhere,
unlike daily.py itself, which exits the whole process on import outside
GitHub Actions / ALLOW_LOCAL_RUN=YES (see daily.py's own top-of-file
guard). daily.py imports these same names back from here so every other
reference to them (verify_all.py's presence checks included) keeps
working unchanged.
"""

BASE_RISK_PCT = 0.75  # conservative until edge is proven (was 1.0%)

SIZING_RULES = {
    "RANGING_LOW_VOL":    0.5,
    "RANGING_HIGH_VOL":   0.5,
    "RISK_OFF":           0.5,
    "TRENDING_RISK_ON":   1.0,
    "TRENDING_RISK_OFF":  0.7,
}

LOSS_STREAK_SIZING = {
    0: 1.0,
    1: 1.0,
    2: 0.75,
    3: 0.5,
    4: 0.25,
}

DRAWDOWN_SIZING = {
    2.0:  1.0,
    4.0:  0.75,
    6.0:  0.5,
    8.0:  0.25,
    10.0: 0.0,
}


def calculate_position_size(regime: str, consecutive_losses: int,
                             drawdown_pct: float, base_pct: float = None,
                             log_fn=None) -> dict:
    """Calculate position size based on regime, loss streak, and drawdown."""
    _log = log_fn or print
    if base_pct is None:
        base_pct = BASE_RISK_PCT
    multipliers = {}
    regime_clean = str(regime).upper()
    regime_mult = 1.0
    for key, mult in SIZING_RULES.items():
        if key in regime_clean:
            regime_mult = mult
            break
    multipliers["regime"] = regime_mult
    streak_mult = 1.0
    for streak, mult in sorted(LOSS_STREAK_SIZING.items(), reverse=True):
        if consecutive_losses >= streak:
            streak_mult = mult
            break
    multipliers["loss_streak"] = streak_mult
    dd_mult = 1.0
    for dd_level, mult in sorted(DRAWDOWN_SIZING.items()):
        if drawdown_pct <= dd_level:
            dd_mult = mult
            break
    multipliers["drawdown"] = dd_mult
    final_mult = min(regime_mult, streak_mult, dd_mult)
    risk_pct = max(0.25, min(2.0, round(base_pct * final_mult, 2)))
    if final_mult >= 1.0:
        mode = "NORMAL"
    elif final_mult >= 0.75:
        mode = "REDUCED"
    elif final_mult >= 0.5:
        mode = "DEFENSIVE"
    else:
        mode = "MINIMAL"
    reason_parts = []
    if regime_mult < 1.0:
        reason_parts.append(f"regime={regime_clean}")
    if streak_mult < 1.0:
        reason_parts.append(f"losses={consecutive_losses}")
    if dd_mult < 1.0:
        reason_parts.append(f"drawdown={drawdown_pct:.1f}%")
    reason = ", ".join(reason_parts) if reason_parts else "normal"
    _log(f"[sizing] {mode}: {base_pct}% × {final_mult:.2f} = {risk_pct}% ({reason})")
    return {"risk_pct": risk_pct, "sizing_mode": mode, "reason": reason,
            "multipliers": multipliers, "final_multiplier": final_mult}
