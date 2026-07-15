"""Automated risk management and position sizing.

Reads account parameters from config (ACCOUNT_BALANCE, ACCOUNT_CURRENCY) and
maintains a risk_profile.json that evolves with every trade outcome.

DRAWDOWN PROTECTION TIERS (highest priority — applied before performance modes):
  halt         — 10 %+ drawdown → STOP all new trades, emergency alert
  preservation —  8-10% drawdown → 0.25% risk, A-grade + all TFs aligned only
  defensive    —  5-8%  drawdown → 0.50% risk, A-grade setups only
  caution      —  3-5%  drawdown → 0.75% risk, A/B-grade only, warning sent
  normal       —  0-3%  drawdown → 1.00% risk, normal grade filtering

RECOVERY: when account recovers 50% of the drawdown amount from when it entered
the current tier, it automatically steps up one tier.

PERFORMANCE MODES (applied when drawdown tier is normal):
  capital_protection  — legacy 10% trigger (preserved for backward compat) → 0.25%
  streak_protection   — 3+ consecutive losses                               → 0.25%
  reduced             — last-5 win rate < 40%                               → 0.50%
  normal              — default                                              → 1.00%
  enhanced            — last-5 win rate > 70%                               → 1.50%

Confidence multiplier (applied after mode, capped at 1.5%):
  7/10 → × 0.75   8/10 → × 1.00   9/10 → × 1.25   10/10 → × 1.50
"""

import json
from datetime import datetime, timezone

import config
from src import tracker

# ── Drawdown tier thresholds ───────────────────────────────────────────────────
DD_CAUTION      = 0.03    # 3%  → caution mode
DD_DEFENSIVE    = 0.05    # 5%  → defensive mode
DD_PRESERVATION = 0.08    # 8%  → preservation mode
DD_HALT         = 0.10    # 10% → halt mode
RECOVERY_STEP   = 0.50    # recover 50% of drawdown amount to step up one tier

# Tier order: index 0 = worst, index 4 = best
_DD_TIERS = ["halt", "preservation", "defensive", "caution", "normal"]

# Risk % per drawdown tier
DD_RISK_PCT = {
    "halt":         0.00,
    "preservation": 0.25,
    "defensive":    0.50,
    "caution":      0.75,
    "normal":       1.00,
}

# ── Legacy performance thresholds ─────────────────────────────────────────────
DRAWDOWN_ENTER  = 0.10    # legacy 10% threshold (preserved for backward compat)
DRAWDOWN_EXIT   = 0.05    # legacy recovery threshold
STREAK_TRIGGER  = 3
WIN_RATE_HIGH   = 0.70
WIN_RATE_LOW    = 0.40
MAX_DAILY_RISK  = 5.0

# ── Risk % per mode (performance modes + drawdown tiers) ──────────────────────
MODE_RISK = {
    # Drawdown tiers
    "halt":              0.00,
    "preservation":      0.25,
    "defensive":         0.50,
    "caution":           0.75,
    # Performance modes
    "capital_protection": 0.25,
    "streak_protection":  0.25,
    "reduced":            0.50,
    "normal":             1.00,
    "enhanced":           1.50,
}

# ── Confidence multipliers ────────────────────────────────────────────────────
CONF_MULT    = {7: 0.75, 8: 1.00, 9: 1.25, 10: 1.50}
MAX_RISK_PCT = 1.50

# ── FOREX AI FUND ─────────────────────────────────────────────────────────────
FUND_START = 10_000.0

# ── Display config per tier ───────────────────────────────────────────────────
_TIER_META = {
    "halt":         {"icon": "🚨", "label": "HALT",         "color": "red"},
    "preservation": {"icon": "🔴", "label": "PRESERVATION", "color": "red"},
    "defensive":    {"icon": "🟠", "label": "DEFENSIVE",    "color": "orange"},
    "caution":      {"icon": "⚠️",  "label": "CAUTION",      "color": "yellow"},
    "normal":       {"icon": "🟢", "label": "NORMAL",       "color": "green"},
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _pip_size(pair: str) -> float:
    return 0.01 if pair.upper().replace("/", "")[-3:] == "JPY" else 0.0001


def _decimals(pair: str) -> int:
    return 3 if pair.upper().replace("/", "")[-3:] == "JPY" else 5


def _pip_value_per_lot(pair: str, entry: float) -> float:
    """Approximate USD pip value per standard lot (100,000 base units).

    For non-USD/non-JPY quote currencies (HKD, NOK, SEK, SGD, CAD crosses etc.),
    1 pip = 10 quote units → USD value ≈ 10 / entry.  This approximation uses the
    pair's own rate as a proxy for the quote/USD rate — accurate for USD-base pairs,
    slightly off for crosses (within 15%) but far better than the old flat $10 default.
    """
    clean = pair.upper().replace("/", "")
    base, quote = clean[:3], clean[3:6]
    if quote == "USD":
        return 10.0
    elif quote == "JPY":
        return (1000.0 / entry) if entry > 0 else 10.0
    elif base == "USD":
        return (10.0 / entry) if entry > 0 else 10.0
    else:
        # Cross pair: neither base nor quote is USD (e.g. EUR/NOK, GBP/HKD, AUD/SGD)
        # 1 pip = 0.0001 × 100,000 = 10 quote units; use entry as proxy for quote/USD
        return (10.0 / entry) if entry > 0 else 10.0


def _currency_exposure(pair: str, direction: str) -> dict:
    clean = pair.upper().replace("/", "")
    base, quote = clean[:3], clean[3:6]
    if direction.upper() == "BUY":
        return {base: "long", quote: "short"}
    return {base: "short", quote: "long"}


# ── Drawdown tier computation ─────────────────────────────────────────────────

def _compute_drawdown_tier(profile: dict, est: float, peak: float) -> dict:
    """Compute the new drawdown tier with recovery step-up logic.

    Stepping DOWN (getting worse): immediate — applies the correct tier as soon
    as the drawdown threshold is crossed.

    Stepping UP (recovery): only when the account has recovered at least 50% of
    the dollar drawdown that existed when the current tier was entered. One step
    at a time, per scan.

    Returns a dict with keys:
      drawdown_mode           — new tier name
      drawdown_mode_changed   — True when the tier is different from profile
      dd_tier_entered_balance — balance at time of tier entry (new or unchanged)
      dd_tier_entered_peak    — peak at time of tier entry (new or unchanged)
    """
    dd = (peak - est) / peak if peak > 0 else 0.0

    # Natural tier for current drawdown level
    if dd >= DD_HALT:
        natural = "halt"
    elif dd >= DD_PRESERVATION:
        natural = "preservation"
    elif dd >= DD_DEFENSIVE:
        natural = "defensive"
    elif dd >= DD_CAUTION:
        natural = "caution"
    else:
        natural = "normal"

    prev = profile.get("drawdown_mode", "normal")
    prev_entry_bal  = profile.get("dd_tier_entered_balance", est)
    prev_entry_peak = profile.get("dd_tier_entered_peak",    peak)

    nat_idx  = _DD_TIERS.index(natural)
    prev_idx = _DD_TIERS.index(prev)

    if nat_idx < prev_idx:
        # Getting worse — step DOWN immediately to the natural tier
        return {
            "drawdown_mode":           natural,
            "drawdown_mode_changed":   True,
            "dd_tier_entered_balance": est,
            "dd_tier_entered_peak":    peak,
        }

    if nat_idx == prev_idx:
        # Same tier — no change
        return {
            "drawdown_mode":           prev,
            "drawdown_mode_changed":   False,
            "dd_tier_entered_balance": prev_entry_bal,
            "dd_tier_entered_peak":    prev_entry_peak,
        }

    # nat_idx > prev_idx → natural tier is better, check recovery
    entry_dd_amount = prev_entry_peak - prev_entry_bal
    if entry_dd_amount <= 0:
        # No drawdown when tier was entered — allow immediate step up
        new_tier = _DD_TIERS[min(len(_DD_TIERS) - 1, prev_idx + 1)]
        return {
            "drawdown_mode":           new_tier,
            "drawdown_mode_changed":   (new_tier != prev),
            "dd_tier_entered_balance": est,
            "dd_tier_entered_peak":    peak,
        }

    recovered_pct = (est - prev_entry_bal) / entry_dd_amount
    if recovered_pct >= RECOVERY_STEP:
        # Step up exactly ONE tier
        new_tier = _DD_TIERS[min(len(_DD_TIERS) - 1, prev_idx + 1)]
        return {
            "drawdown_mode":           new_tier,
            "drawdown_mode_changed":   (new_tier != prev),
            "dd_tier_entered_balance": est,
            "dd_tier_entered_peak":    peak,
        }

    # Not recovered enough — stay in current tier
    return {
        "drawdown_mode":           prev,
        "drawdown_mode_changed":   False,
        "dd_tier_entered_balance": prev_entry_bal,
        "dd_tier_entered_peak":    prev_entry_peak,
    }


# ── Profile load / save ───────────────────────────────────────────────────────

def _defaults() -> dict:
    return {
        "account_balance":       config.ACCOUNT_BALANCE,
        "account_currency":      config.ACCOUNT_CURRENCY,
        "estimated_balance":     FUND_START,
        "peak_balance":          FUND_START,
        "risk_mode":             "normal",
        "drawdown_mode":         "normal",
        "dd_tier_entered_balance": FUND_START,
        "dd_tier_entered_peak":    FUND_START,
        "consecutive_losses":    0,
        "consecutive_wins":      0,
        "last_5_win_rate":       None,
        "total_open_pct":        0.0,
        "weekly_snapshots":      [],
        "updated_at":            _now(),
    }


def load_profile() -> dict:
    """Load risk_profile.json.

    account_balance  — always set from env (real account, GitHub secret).
    estimated_balance — FOREX AI FUND, starts at FUND_START, drifts with P&L.
    """
    if not config.RISK_PROFILE_FILE.exists():
        d = _defaults()
        save_profile(d)
        return d
    try:
        data = json.loads(config.RISK_PROFILE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _defaults()

    data["account_balance"] = config.ACCOUNT_BALANCE
    data.setdefault("account_currency",      config.ACCOUNT_CURRENCY)
    data.setdefault("estimated_balance",     FUND_START)
    data.setdefault("peak_balance",          data.get("estimated_balance", FUND_START))
    data.setdefault("weekly_snapshots",      [])
    data.setdefault("drawdown_mode",         "normal")
    data.setdefault("dd_tier_entered_balance", data.get("estimated_balance", FUND_START))
    data.setdefault("dd_tier_entered_peak",    data.get("peak_balance", FUND_START))
    return data


def save_profile(profile: dict) -> None:
    profile["updated_at"] = _now()
    config.RISK_PROFILE_FILE.write_text(json.dumps(profile, indent=2), encoding="utf-8")


# ── Risk state computation ────────────────────────────────────────────────────

def compute_risk_state(profile: dict) -> dict:
    """Derive current risk mode, drawdown tier, and stats from closed trades + profile."""
    rows   = tracker.load()
    closed = [r for r in rows if r.get("status") in ("WIN", "LOSS")]

    # Consecutive streak
    cons_loss = cons_win = 0
    for r in reversed(closed):
        if r["status"] == "LOSS":
            if cons_win:
                break
            cons_loss += 1
        else:
            if cons_loss:
                break
            cons_win += 1

    # Last-5 win rate
    last5    = closed[-5:]
    last5_wr = (sum(1 for r in last5 if r["status"] == "WIN") / len(last5)
                if last5 else None)

    # Overall win rate
    decisive   = [r for r in closed if r["status"] in ("WIN", "LOSS")]
    overall_wr = (sum(1 for r in decisive if r["status"] == "WIN") / len(decisive)
                  if decisive else None)

    # Drawdown
    est  = profile.get("estimated_balance", config.ACCOUNT_BALANCE)
    peak = max(profile.get("peak_balance", est), est)
    dd   = (peak - est) / peak if peak > 0 else 0.0

    # ── Drawdown tier (5-tier system) ─────────────────────────────────────────
    tier_info = _compute_drawdown_tier(profile, est, peak)

    # ── Performance mode (applied when drawdown tier is normal or caution) ────
    prev_mode = profile.get("risk_mode", "normal")
    if cons_loss >= STREAK_TRIGGER:
        perf_mode = "streak_protection"
    elif prev_mode == "streak_protection" and cons_win == 0:
        perf_mode = "streak_protection"
    elif last5_wr is not None and last5_wr < WIN_RATE_LOW:
        perf_mode = "reduced"
    elif last5_wr is not None and last5_wr > WIN_RATE_HIGH:
        perf_mode = "enhanced"
    else:
        perf_mode = "normal"

    # ── Effective risk % = min(drawdown tier risk, performance mode risk) ─────
    dd_mode   = tier_info["drawdown_mode"]
    dd_risk   = DD_RISK_PCT.get(dd_mode, 1.00)
    perf_risk = MODE_RISK.get(perf_mode, 1.00)
    base_risk = min(dd_risk, perf_risk)   # most conservative wins

    # Expose the "combined" mode label for display: drawdown tier takes precedence
    # unless we're in normal drawdown territory (let performance mode show)
    if dd_mode != "normal":
        display_mode = dd_mode
    else:
        display_mode = perf_mode

    return {
        # Drawdown tier
        "drawdown_mode":           dd_mode,
        "drawdown_mode_changed":   tier_info["drawdown_mode_changed"],
        "dd_tier_entered_balance": tier_info["dd_tier_entered_balance"],
        "dd_tier_entered_peak":    tier_info["dd_tier_entered_peak"],
        "drawdown_risk_pct":       dd_risk,
        # Performance mode
        "risk_mode":               display_mode,
        "perf_mode":               perf_mode,
        "base_risk_pct":           base_risk,
        # Stats
        "consecutive_losses":      cons_loss,
        "consecutive_wins":        cons_win,
        "last_5_win_rate":         last5_wr,
        "overall_win_rate":        overall_wr,
        "decisive_count":          len(decisive),
        "drawdown_pct":            dd,
        "peak_balance":            peak,
        "estimated_balance":       est,
    }


# ── ATR stop adjustment ───────────────────────────────────────────────────────

def adjust_stop_for_atr(pair: str, direction: str,
                        entry: float, stop: float, target: float,
                        atr_daily: float) -> dict:
    """Widen/tighten stop based on daily ATR; preserve original R:R on target."""
    if not atr_daily or atr_daily <= 0 or not all([entry, stop, target]):
        return {"stop": stop, "target": target, "note": ""}

    orig_dist = abs(entry - stop)
    if orig_dist <= 0:
        return {"stop": stop, "target": target, "note": ""}

    orig_rr  = abs(target - entry) / orig_dist
    min_dist = 1.0 * atr_daily
    max_dist = 2.5 * atr_daily
    dec      = _decimals(pair)
    note     = ""

    if orig_dist < min_dist:
        new_dist = min_dist
        note     = f"widened to 1.0x ATR {atr_daily:.{dec}f}"
    elif orig_dist > max_dist:
        new_dist = max_dist
        note     = f"tightened vs ATR {atr_daily:.{dec}f}"
    else:
        return {"stop": stop, "target": target, "note": ""}

    if direction.upper() == "BUY":
        new_stop   = round(entry - new_dist, dec)
        new_target = round(entry + new_dist * orig_rr, dec)
    else:
        new_stop   = round(entry + new_dist, dec)
        new_target = round(entry - new_dist * orig_rr, dec)

    return {"stop": new_stop, "target": new_target, "note": note}


# ── Position sizing ───────────────────────────────────────────────────────────

def size_trade(pair: str, direction: str, entry: float, stop: float,
               target: float, confidence: int, profile: dict,
               risk_state: dict, atr_daily: float = None,
               correlated: bool = False) -> dict:
    """Compute full position sizing for one trade alert."""
    balance  = config.ACCOUNT_BALANCE
    currency = profile.get("account_currency", "USD")

    atr_adj  = adjust_stop_for_atr(pair, direction, entry, stop, target, atr_daily)
    adj_stop = atr_adj["stop"]
    adj_tgt  = atr_adj["target"]
    atr_note = atr_adj["note"]

    # Drawdown tier caps risk — most conservative of tier vs performance mode
    base_pct = risk_state["base_risk_pct"]   # already min(dd_risk, perf_risk)
    conf_m   = CONF_MULT.get(int(confidence) if confidence else 8, 1.0)
    eff_pct  = min(base_pct * conf_m, MAX_RISK_PCT)
    if correlated:
        eff_pct = eff_pct * 0.50

    # Halt mode: force zero lots
    if risk_state.get("drawdown_mode") == "halt":
        eff_pct = 0.0

    risk_amount  = round(balance * eff_pct / 100.0, 2)
    pip_sz       = _pip_size(pair)
    stop_dist    = abs(entry - (adj_stop or stop))
    stop_pips    = stop_dist / pip_sz if pip_sz > 0 else 1
    pip_val      = _pip_value_per_lot(pair, entry)
    lots_raw     = risk_amount / (stop_pips * pip_val) if stop_pips * pip_val > 0 else 0
    lots         = max(round(lots_raw, 2), 0.01) if risk_amount > 0 else 0.0

    return {
        "pair":            pair,
        "direction":       direction,
        "lots":            lots,
        "risk_amount":     risk_amount,
        "risk_pct":        eff_pct,
        "adj_stop":        adj_stop,
        "adj_target":      adj_tgt,
        "atr_note":        atr_note,
        "correlated":      correlated,
        "risk_mode":       risk_state["risk_mode"],
        "drawdown_mode":   risk_state.get("drawdown_mode", "normal"),
        "currency":        currency,
        "balance":         balance,
    }


def size_trade_from_result(result: dict, profile: dict,
                           risk_state: dict, correlated: bool = False) -> dict:
    p   = result.get("parsed", {})
    atr = None
    try:
        atr = float(result["bundle"]["technical"]["daily"]["atr14"])
    except (KeyError, TypeError, ValueError):
        pass
    return size_trade(
        pair=result["pair"],
        direction=(p.get("direction") or "BUY"),
        entry=_to_float(p.get("entry")) or 1.0,
        stop=_to_float(p.get("stop_loss")) or 1.0,
        target=_to_float(p.get("target")) or 1.0,
        confidence=int(p.get("confidence") or 8),
        profile=profile,
        risk_state=risk_state,
        atr_daily=atr,
        correlated=correlated,
    )


# ── Correlation check ─────────────────────────────────────────────────────────

def apply_correlation_checks(sized_trades: list) -> list:
    """Flag and halve positions for trades that share directional currency exposure."""
    n = len(sized_trades)
    if n < 2:
        return sized_trades

    corr_pairs = set()
    for i in range(n):
        for j in range(i + 1, n):
            t1, t2 = sized_trades[i], sized_trades[j]
            exp1 = _currency_exposure(t1["pair"], t1["direction"])
            exp2 = _currency_exposure(t2["pair"], t2["direction"])
            if any(exp1.get(c) == exp2.get(c) for c in exp1):
                corr_pairs.add(i)
                corr_pairs.add(j)

    for i in corr_pairs:
        t = sized_trades[i]
        if not t.get("correlated"):
            t["correlated"]  = True
            t["lots"]        = max(round(t["lots"] * 0.50, 2), 0.01)
            t["risk_amount"] = round(t["risk_amount"] * 0.50, 2)
            t["risk_pct"]    = round(t["risk_pct"] * 0.50, 3)

    return sized_trades


# ── Open exposure ─────────────────────────────────────────────────────────────

def compute_open_exposure(profile: dict) -> dict:
    """Estimate total open risk % across all live YES trades."""
    rows   = tracker.load()
    opened = [r for r in rows if r.get("status") == "OPEN" and r.get("trade_this") == "YES"]
    mode   = profile.get("risk_mode", "normal")
    total  = 0.0
    for row in opened:
        conf  = int(_to_float(row.get("confidence")) or 8)
        b_pct = MODE_RISK.get(mode, 1.0)
        m     = CONF_MULT.get(conf, 1.0)
        total += min(b_pct * m, MAX_RISK_PCT)
    return {
        "open_count":    len(opened),
        "total_pct":     round(total, 2),
        "limit_pct":     MAX_DAILY_RISK,
        "limit_reached": total >= MAX_DAILY_RISK,
    }


# ── Balance update from outcome ───────────────────────────────────────────────

def update_balance_from_outcome(trade: dict) -> None:
    # No-op: estimated_balance is now synced from calculate_fund_state() every
    # scan (daily.py Site 1, ~line 11762).  Kept as a shell so any remaining
    # callers do not raise AttributeError.
    return


# ── Telegram / display helpers ────────────────────────────────────────────────

def fmt_currency(amount: float, currency: str) -> str:
    symbols = {"USD": "$", "EUR": "€", "GBP": "£"}
    sym = symbols.get(currency, f"{currency} ")
    return f"{sym}{amount:,.2f}"


def drawdown_header_line(risk_state: dict, profile: dict) -> str:
    """Drawdown warning banner for the top of every Telegram message.

    Returns empty string when drawdown is under 3% in normal mode.
    Template format:
      ⚠️ RISK MODE ACTIVE: [mode] — account down X% from peak
      Position sizes automatically reduced to X% per trade — system protecting your capital
    """
    dd_mode = risk_state.get("drawdown_mode", "normal")
    dd_pct  = risk_state.get("drawdown_pct", 0.0) * 100
    r_pct   = DD_RISK_PCT.get(dd_mode, 1.00)
    meta    = _TIER_META.get(dd_mode, _TIER_META["normal"])
    label   = meta["label"]

    if dd_mode == "halt":
        return (
            f"🚨 <b>RISK MODE ACTIVE: {label} — account down {dd_pct:.1f}% from peak</b>\n"
            f"All new trades suspended — system protecting your capital"
        )
    elif dd_mode in ("preservation", "defensive", "caution"):
        return (
            f"⚠️ <b>RISK MODE ACTIVE: {label} — account down {dd_pct:.1f}% from peak</b>\n"
            f"Position sizes automatically reduced to {r_pct:.2f}% per trade — system protecting your capital"
        )
    elif dd_pct > 3.0:
        return (
            f"⚠️ <b>RISK MODE ACTIVE: Normal — account down {dd_pct:.1f}% from peak</b>\n"
            f"Position sizes automatically reduced to {r_pct:.2f}% per trade — system protecting your capital"
        )
    return ""


def drawdown_tier_alert_lines(old_mode: str, new_mode: str,
                               risk_state: dict, fund: float, peak: float) -> list:
    """Generate Telegram alert lines for a drawdown tier transition.

    Returned list is non-empty only when the transition is meaningful.
    """
    dd_pct = risk_state.get("drawdown_pct", 0.0) * 100
    r_pct  = DD_RISK_PCT.get(new_mode, 1.00)
    meta   = _TIER_META.get(new_mode, _TIER_META["normal"])
    icon   = meta["icon"]
    label  = meta["label"]

    old_meta  = _TIER_META.get(old_mode, _TIER_META["normal"])
    old_label = old_meta["label"]

    old_idx = _DD_TIERS.index(old_mode) if old_mode in _DD_TIERS else 4
    new_idx = _DD_TIERS.index(new_mode) if new_mode in _DD_TIERS else 4
    stepping_down = new_idx < old_idx   # getting worse

    lines = ["", "━━━━━━━━━━━━━━━━━━━━━"]

    if stepping_down:
        if new_mode == "halt":
            lines += [
                f"🚨 <b>EMERGENCY: HALT MODE ACTIVATED</b>",
                f"Account drawdown has reached {dd_pct:.1f}% from peak",
                f"Peak fund value: ${peak:,.0f} | Current: ${fund:,.0f}",
                "",
                "<b>ALL NEW TRADES ARE HALTED IMMEDIATELY.</b>",
                "No new trade alerts will be generated until drawdown recovers.",
                "Review system performance and identify the source of losses.",
                f"Recovery needed: account must recover 50% of drawdown to resume.",
                "",
                "Action required: review open trades, check for correlated losses.",
            ]
        elif new_mode == "preservation":
            lines += [
                f"🔴 <b>URGENT: Entering PRESERVATION MODE</b>",
                f"Drawdown: {dd_pct:.1f}% | Threshold: {DD_PRESERVATION*100:.0f}%",
                f"Fund: ${fund:,.0f} (peak: ${peak:,.0f})",
                "",
                f"Risk per trade reduced to {r_pct:.2f}%",
                "Only A-grade setups with all 3 timeframes aligned will be taken.",
                "Recovery: need 50% drawdown recovery to step up to Defensive mode.",
            ]
        elif new_mode == "defensive":
            lines += [
                f"🟠 <b>ALERT: Entering DEFENSIVE MODE</b>",
                f"Drawdown: {dd_pct:.1f}% | Threshold: {DD_DEFENSIVE*100:.0f}%",
                f"Fund: ${fund:,.0f} (peak: ${peak:,.0f})",
                "",
                f"Risk per trade reduced to {r_pct:.2f}%",
                "Only A-grade setups will be taken.",
                "Recovery: need 50% drawdown recovery to step up to Caution mode.",
            ]
        elif new_mode == "caution":
            lines += [
                f"⚠️ <b>WARNING: Entering CAUTION MODE</b>",
                f"Drawdown: {dd_pct:.1f}% | Threshold: {DD_CAUTION*100:.0f}%",
                f"Fund: ${fund:,.0f} (peak: ${peak:,.0f})",
                "",
                f"Risk per trade reduced to {r_pct:.2f}%",
                "Only A/B-grade setups will be taken.",
                "Recovery: need 50% drawdown recovery to return to Normal mode.",
            ]
    else:
        # Stepping up (recovering)
        lines += [
            f"✅ <b>Recovery: Stepping up to {label} MODE</b>",
            f"Account has recovered 50%+ of drawdown from {old_label} entry",
            f"Drawdown: {dd_pct:.1f}% | Fund: ${fund:,.0f} (peak: ${peak:,.0f})",
            f"Risk per trade now: {r_pct:.2f}%",
        ]
        if new_mode == "normal":
            lines.append("Full normal trading conditions restored.")
        else:
            next_tier = _DD_TIERS[_DD_TIERS.index(new_mode) + 1] if new_mode != "normal" else "normal"
            next_meta = _TIER_META.get(next_tier, _TIER_META["normal"])
            lines.append(f"Continue recovery → {next_meta['icon']} {next_meta['label']} mode next.")

    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    return lines


def risk_dashboard_lines(profile: dict, risk_state: dict,
                         exposure: dict) -> list:
    """Return Telegram-formatted lines for the Risk Dashboard section."""
    fund    = profile.get("estimated_balance", FUND_START)
    fund_pk = profile.get("peak_balance", fund)
    fund_ret = (fund - FUND_START) / FUND_START * 100
    real    = config.ACCOUNT_BALANCE
    cur     = profile.get("account_currency", "USD")
    dd_mode = risk_state.get("drawdown_mode", "normal")
    mode    = risk_state["risk_mode"]
    wr      = risk_state.get("overall_win_rate")
    dec     = risk_state.get("decisive_count", 0)
    wr_txt  = f"{wr*100:.0f}%" if wr is not None else "n/a"
    cl      = risk_state.get("consecutive_losses", 0)
    cw      = risk_state.get("consecutive_wins", 0)
    dd      = risk_state.get("drawdown_pct", 0.0)
    b_pct   = risk_state["base_risk_pct"]
    tot     = exposure.get("total_pct", 0.0)
    lim     = exposure.get("limit_pct", MAX_DAILY_RISK)
    meta    = _TIER_META.get(dd_mode, _TIER_META["normal"])

    if cw > 1:
        streak_txt = f"🔥 {cw} consecutive wins"
    elif cl > 0:
        streak_txt = f"⚠️ {cl} consecutive loss{'es' if cl > 1 else ''}"
    else:
        streak_txt = "No active streak"

    lines = [
        "📊 <b>RISK DASHBOARD</b>",
        f"📈 FOREX AI FUND: <b>{fmt_currency(fund, cur)}</b> ({fund_ret:+.1f}%) | Peak: {fmt_currency(fund_pk, cur)}",
        f"{wr_txt} wins ({dec} decisive trades)",
        f"Risk/trade: <b>{b_pct:.2f}%</b> ({mode.replace('_', ' ')}) | Open exposure: {tot:.1f}% / {lim:.0f}%",
        streak_txt,
    ]

    # Drawdown tier banner
    if dd_mode == "halt":
        lines.append(
            f"🚨 <b>HALT MODE — drawdown {dd*100:.1f}% | NO NEW TRADES</b>"
        )
    elif dd_mode == "preservation":
        lines.append(
            f"🔴 <b>PRESERVATION MODE — drawdown {dd*100:.1f}% "
            f"| A-grade + all TFs only | {DD_RISK_PCT['preservation']:.2f}%/trade</b>"
        )
    elif dd_mode == "defensive":
        lines.append(
            f"🟠 <b>DEFENSIVE MODE — drawdown {dd*100:.1f}% "
            f"| A-grade only | {DD_RISK_PCT['defensive']:.2f}%/trade</b>"
        )
    elif dd_mode == "caution":
        lines.append(
            f"⚠️ <b>CAUTION MODE — drawdown {dd*100:.1f}% "
            f"| A/B-grade only | {DD_RISK_PCT['caution']:.2f}%/trade</b>"
        )
    # Performance mode warnings (when not in drawdown tier)
    elif mode == "streak_protection":
        lines.append("🔴 <b>STREAK PROTECTION</b> — risk halved until next win")
    elif mode == "reduced":
        lines.append("🟡 Reduced risk mode — recent win rate below 40%")
    elif mode == "enhanced":
        lines.append("🟢 Enhanced risk mode — recent win rate above 70%")

    if exposure.get("limit_reached"):
        lines.append(
            f"🔴 <b>RISK LIMIT REACHED</b> — total open exposure {tot:.1f}% "
            f"≥ {lim:.0f}% limit. New trades not recommended."
        )

    return lines
