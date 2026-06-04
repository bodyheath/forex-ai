"""Automated risk management and position sizing.

Reads account parameters from config (ACCOUNT_BALANCE, ACCOUNT_CURRENCY) and
maintains a risk_profile.json that evolves with every trade outcome.

Risk mode priority (highest wins):
  capital_protection  — 10 % drawdown from peak          → 0.25 %
  streak_protection   — 3+ consecutive losses             → 0.25 %
  reduced             — last-5 win rate < 40 %            → 0.50 %
  normal              — default                           → 1.00 %
  enhanced            — last-5 win rate > 70 %            → 1.50 %

Confidence multiplier (applied after mode, capped at 1.5 %):
  7/10 → × 0.75   8/10 → × 1.00   9/10 → × 1.25   10/10 → × 1.50
"""

import json
from datetime import datetime

import config
from src import tracker

# ── Thresholds ────────────────────────────────────────────────────────────────
DRAWDOWN_ENTER  = 0.10    # 10 % from peak → capital protection
DRAWDOWN_EXIT   = 0.05    # recover to within 5 % to exit protection
STREAK_TRIGGER  = 3       # consecutive losses → streak protection
WIN_RATE_HIGH   = 0.70    # last-5 win rate > 70 % → enhanced
WIN_RATE_LOW    = 0.40    # last-5 win rate < 40 % → reduced
MAX_DAILY_RISK  = 5.0     # total open exposure % before halting new trades

# ── Risk % per mode ───────────────────────────────────────────────────────────
MODE_RISK = {
    "capital_protection": 0.25,
    "streak_protection":  0.25,
    "reduced":            0.50,
    "normal":             1.00,
    "enhanced":           1.50,
}

# ── Confidence multipliers ────────────────────────────────────────────────────
CONF_MULT = {7: 0.75, 8: 1.00, 9: 1.25, 10: 1.50}
MAX_RISK_PCT = 1.50


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
    """Approximate USD pip value per standard lot.

    Accurate for USD-account holders on major pairs; approximate for
    non-USD accounts (NZD users: actual value ~60 % of figure shown).
    """
    clean = pair.upper().replace("/", "")
    base, quote = clean[:3], clean[3:6]
    if quote == "USD":
        return 10.0
    elif quote == "JPY":
        return (1000.0 / entry) if entry > 0 else 10.0
    elif base == "USD":
        return (10.0 / entry) if entry > 0 else 10.0
    return 10.0  # cross pairs — rough estimate


def _currency_exposure(pair: str, direction: str) -> dict:
    """Return {ccy: 'long'/'short'} for the trade."""
    clean = pair.upper().replace("/", "")
    base, quote = clean[:3], clean[3:6]
    if direction.upper() == "BUY":
        return {base: "long", quote: "short"}
    return {base: "short", quote: "long"}


# ── Profile load / save ───────────────────────────────────────────────────────

def _defaults() -> dict:
    bal = config.ACCOUNT_BALANCE
    return {
        "account_balance":    bal,
        "account_currency":   config.ACCOUNT_CURRENCY,
        "estimated_balance":  bal,
        "peak_balance":       bal,
        "risk_mode":          "normal",
        "consecutive_losses": 0,
        "consecutive_wins":   0,
        "last_5_win_rate":    None,
        "total_open_pct":     0.0,
        "weekly_snapshots":   [],
        "updated_at":         _now(),
    }


def load_profile() -> dict:
    """Load risk_profile.json; sync account balance from env on significant change."""
    if not config.RISK_PROFILE_FILE.exists():
        d = _defaults()
        save_profile(d)
        return d
    try:
        data = json.loads(config.RISK_PROFILE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _defaults()

    # If env ACCOUNT_BALANCE was manually updated (>5 % delta), trust env.
    env_bal  = config.ACCOUNT_BALANCE
    est_bal  = data.get("estimated_balance", env_bal)
    if est_bal > 0 and abs(env_bal - est_bal) / est_bal > 0.05:
        data["account_balance"]   = env_bal
        data["estimated_balance"] = env_bal
        data["peak_balance"]      = max(data.get("peak_balance", 0), env_bal)

    data.setdefault("account_currency",  config.ACCOUNT_CURRENCY)
    data.setdefault("estimated_balance", data.get("account_balance", config.ACCOUNT_BALANCE))
    data.setdefault("peak_balance",      data.get("estimated_balance"))
    data.setdefault("weekly_snapshots",  [])
    return data


def save_profile(profile: dict) -> None:
    profile["updated_at"] = _now()
    config.RISK_PROFILE_FILE.write_text(json.dumps(profile, indent=2), encoding="utf-8")


# ── Risk state computation ────────────────────────────────────────────────────

def compute_risk_state(profile: dict) -> dict:
    """Derive current risk mode and stats from closed trades + profile."""
    rows   = tracker.load()
    closed = [r for r in rows if r.get("status") in ("WIN", "LOSS")]

    # Consecutive streak (reading newest-first)
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
    decisive = [r for r in closed if r["status"] in ("WIN", "LOSS")]
    overall_wr = (sum(1 for r in decisive if r["status"] == "WIN") / len(decisive)
                  if decisive else None)

    # Drawdown
    est   = profile.get("estimated_balance", config.ACCOUNT_BALANCE)
    peak  = max(profile.get("peak_balance", est), est)
    dd    = (peak - est) / peak if peak > 0 else 0.0

    # Determine mode
    prev_mode = profile.get("risk_mode", "normal")
    if dd >= DRAWDOWN_ENTER:
        mode = "capital_protection"
    elif prev_mode == "capital_protection" and dd > DRAWDOWN_EXIT:
        mode = "capital_protection"   # stay until truly recovered
    elif cons_loss >= STREAK_TRIGGER:
        mode = "streak_protection"
    elif prev_mode == "streak_protection" and cons_win == 0:
        mode = "streak_protection"    # stay until a win
    elif last5_wr is not None and last5_wr < WIN_RATE_LOW:
        mode = "reduced"
    elif last5_wr is not None and last5_wr > WIN_RATE_HIGH:
        mode = "enhanced"
    else:
        mode = "normal"

    return {
        "risk_mode":          mode,
        "base_risk_pct":      MODE_RISK[mode],
        "consecutive_losses": cons_loss,
        "consecutive_wins":   cons_win,
        "last_5_win_rate":    last5_wr,
        "overall_win_rate":   overall_wr,
        "decisive_count":     len(decisive),
        "drawdown_pct":       dd,
        "peak_balance":       peak,
        "estimated_balance":  est,
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

    orig_rr   = abs(target - entry) / orig_dist
    min_dist  = 1.2 * atr_daily   # tighter than this → widen (noise stop)
    max_dist  = 2.5 * atr_daily   # wider than this   → tighten (over-exposed)
    dec       = _decimals(pair)
    note      = ""

    if orig_dist < min_dist:
        new_dist = min_dist
        note     = f"widened vs ATR {atr_daily:.{dec}f}"
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
    """Compute full position sizing for one trade alert.

    Returns dict with lots, risk_amount, risk_pct, adj_stop, adj_target,
    atr_note, correlated_flag, risk_mode, and limit_flag.
    """
    balance  = profile.get("estimated_balance", config.ACCOUNT_BALANCE)
    currency = profile.get("account_currency", "USD")

    # ATR-adjust stops first
    atr_adj  = adjust_stop_for_atr(pair, direction, entry, stop, target, atr_daily)
    adj_stop = atr_adj["stop"]
    adj_tgt  = atr_adj["target"]
    atr_note = atr_adj["note"]

    # Effective risk %: mode × confidence multiplier, capped at MAX
    base_pct = risk_state["base_risk_pct"]
    conf_m   = CONF_MULT.get(int(confidence) if confidence else 8, 1.0)
    eff_pct  = min(base_pct * conf_m, MAX_RISK_PCT)
    if correlated:
        eff_pct = eff_pct * 0.50   # halve for correlated pair

    risk_amount  = round(balance * eff_pct / 100.0, 2)
    pip_sz       = _pip_size(pair)
    stop_dist    = abs(entry - (adj_stop or stop))
    stop_pips    = stop_dist / pip_sz if pip_sz > 0 else 1
    pip_val      = _pip_value_per_lot(pair, entry)
    lots_raw     = risk_amount / (stop_pips * pip_val) if stop_pips * pip_val > 0 else 0
    lots         = max(round(lots_raw, 2), 0.01)   # minimum 0.01 lots (micro)

    return {
        "pair":           pair,
        "direction":      direction,
        "lots":           lots,
        "risk_amount":    risk_amount,
        "risk_pct":       eff_pct,
        "adj_stop":       adj_stop,
        "adj_target":     adj_tgt,
        "atr_note":       atr_note,
        "correlated":     correlated,
        "risk_mode":      risk_state["risk_mode"],
        "currency":       currency,
        "balance":        balance,
    }


def size_trade_from_result(result: dict, profile: dict,
                           risk_state: dict, correlated: bool = False) -> dict:
    """Convenience wrapper that unpacks a deep_results entry."""
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
    """Flag and halve positions for trades that share directional currency exposure.

    Two trades are considered correlated (> 0.7 threshold) if they are
    simultaneously long or short the same currency — e.g. EUR/USD BUY and
    GBP/USD BUY are both USD-short.
    """
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
        conf   = int(_to_float(row.get("confidence")) or 8)
        b_pct  = MODE_RISK.get(mode, 1.0)
        m      = CONF_MULT.get(conf, 1.0)
        total += min(b_pct * m, MAX_RISK_PCT)
    return {
        "open_count":    len(opened),
        "total_pct":     round(total, 2),
        "limit_pct":     MAX_DAILY_RISK,
        "limit_reached": total >= MAX_DAILY_RISK,
    }


# ── Balance update from outcome ───────────────────────────────────────────────

def update_balance_from_outcome(trade: dict) -> None:
    """Adjust estimated_balance and peak_balance in risk_profile after a close.

    Uses the trade's R-multiple × estimated risk_amount to approximate P&L.
    """
    profile = load_profile()
    r_mult  = _to_float(trade.get("r_multiple")) or 0.0
    if r_mult == 0.0:
        return   # EXPIRED at breakeven or no data — skip

    est     = profile.get("estimated_balance", config.ACCOUNT_BALANCE)
    mode    = profile.get("risk_mode", "normal")
    b_pct   = MODE_RISK.get(mode, 1.0)
    conf    = int(_to_float(trade.get("confidence")) or 8)
    c_mult  = CONF_MULT.get(conf, 1.0)
    eff_pct = min(b_pct * c_mult, MAX_RISK_PCT)
    risk_amt = est * eff_pct / 100.0

    new_est  = max(round(est + risk_amt * r_mult, 2), 1.0)
    profile["estimated_balance"] = new_est
    profile["peak_balance"]      = max(profile.get("peak_balance", 0), new_est)

    # Weekly snapshot
    snaps = profile.get("weekly_snapshots", [])
    if not snaps or (datetime.now() - datetime.strptime(
            snaps[-1]["date"][:10], "%Y-%m-%d")).days >= 7:
        snaps.append({"date": _now(), "balance": new_est})
    profile["weekly_snapshots"] = snaps[-52:]   # keep one year

    save_profile(profile)


# ── Telegram / display helpers ────────────────────────────────────────────────

def fmt_currency(amount: float, currency: str) -> str:
    symbols = {"USD": "$", "EUR": "€", "GBP": "£"}
    sym = symbols.get(currency, f"{currency} ")
    return f"{sym}{amount:,.2f}"


def risk_dashboard_lines(profile: dict, risk_state: dict,
                         exposure: dict) -> list:
    """Return Telegram-formatted lines for the Risk Dashboard section."""
    bal    = profile.get("estimated_balance", config.ACCOUNT_BALANCE)
    cur    = profile.get("account_currency", "USD")
    mode   = risk_state["risk_mode"]
    wr     = risk_state.get("overall_win_rate")
    dec    = risk_state.get("decisive_count", 0)
    wr_txt = f"{wr*100:.0f}%" if wr is not None else "n/a"
    cl     = risk_state.get("consecutive_losses", 0)
    cw     = risk_state.get("consecutive_wins", 0)
    dd     = risk_state.get("drawdown_pct", 0.0)
    peak   = risk_state.get("peak_balance", bal)
    b_pct  = MODE_RISK.get(mode, 1.0)
    tot    = exposure.get("total_pct", 0.0)
    lim    = exposure.get("limit_pct", MAX_DAILY_RISK)

    if cw > 1:
        streak_txt = f"🔥 {cw} consecutive wins"
    elif cl > 0:
        streak_txt = f"⚠️ {cl} consecutive loss{'es' if cl > 1 else ''}"
    else:
        streak_txt = "No active streak"

    lines = [
        "📊 <b>RISK DASHBOARD</b>",
        f"Account: <b>{fmt_currency(bal, cur)}</b> | Win rate: {wr_txt} ({dec} decisive trades)",
        f"Risk/trade: <b>{b_pct:.2f}%</b> ({mode.replace('_',' ')}) | Open exposure: {tot:.1f}% / {lim:.0f}%",
        streak_txt,
    ]

    # Active warnings
    if mode == "capital_protection":
        lines.append(
            f"🔴 <b>CAPITAL PROTECTION MODE</b> — drawdown {dd*100:.1f}% "
            f"(peak {fmt_currency(peak, cur)})"
        )
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
