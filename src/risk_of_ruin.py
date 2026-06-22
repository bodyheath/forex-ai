"""Risk of Ruin and Kelly Criterion calculator.

All calculations are based on closed trade history (WIN / LOSS outcomes).

Risk of Ruin formula (Vince / Thorp normalised-edge method):
  edge  = p × R − q                    where p = win_rate, q = 1−p, R = avg win R
  var   = p × R² + q − edge²           variance of per-trade outcome
  Z     = edge / √(edge² + var)         normalised edge
  ratio = (1−Z) / (1+Z)                always in (0,1) when edge > 0
  C     = ln(ruin_threshold) / ln(1−f)  consecutive-loss units to threshold
  RoR   = ratio^C

Ruin threshold defaults to 50 % drawdown (you're "ruined" if you lose half the
account — the point at which most retail traders stop trading).

Kelly Criterion:
  f* = (p × R − q) / R = p − q/R       full Kelly
  Quarter Kelly (f*/4) is the recommended practical maximum for retail traders.
"""

import math


# ── FTMO challenge rule constants ─────────────────────────────────────────────
FTMO_MAX_DAILY_LOSS_PCT   = 5.0    # % — FTMO Phase 1 & 2 daily loss limit
FTMO_MAX_TOTAL_LOSS_PCT   = 10.0   # % — FTMO Phase 1 & 2 max total drawdown
FTMO_PROFIT_TARGET_PCT    = 10.0   # % — FTMO Phase 1 profit target
FTMO_PHASE2_TARGET_PCT    = 5.0    # % — FTMO Phase 2 profit target


# ── Core calculations ─────────────────────────────────────────────────────────

def risk_of_ruin(win_rate: float, avg_rr: float, risk_pct: float,
                  ruin_threshold: float = 0.50) -> float:
    """Return probability (0–1) of losing *ruin_threshold* fraction of the account.

    Parameters
    ----------
    win_rate        fraction of trades that close as WIN (e.g. 0.55)
    avg_rr          average R-multiple on winning trades (e.g. 2.0 for 2:1)
    risk_pct        fraction of account risked per trade (e.g. 0.01 for 1 %)
    ruin_threshold  account fraction that defines "ruin" (default 0.50 = 50 % loss)
    """
    p = float(win_rate)
    q = 1.0 - p
    R = float(avg_rr)
    f = float(risk_pct)

    if not (0 < p < 1) or R <= 0 or not (0 < f < 1):
        return 1.0

    edge = p * R - q
    if edge <= 0:
        return 1.0  # zero / negative expectancy → ruin is certain over time

    variance = p * R ** 2 + q - edge ** 2
    if variance <= 1e-12:
        return 0.0

    Z     = edge / math.sqrt(edge ** 2 + variance)
    ratio = (1.0 - Z) / (1.0 + Z)   # in (0,1) when edge > 0 and ratio < 1

    if ratio <= 0:
        return 0.0
    if ratio >= 1:
        return 1.0

    # C = number of consecutive-loss steps to reach the ruin threshold
    # (1 − f)^C = ruin_threshold  →  C = ln(threshold) / ln(1 − f)
    C = math.log(ruin_threshold) / math.log(1.0 - f)

    return ratio ** C


def kelly_criterion(win_rate: float, avg_rr: float) -> float:
    """Full Kelly fraction: f* = p − q/R.

    Returns the theoretically optimal fraction to risk per trade.
    Negative means negative expectancy (do not trade this system).
    Quarter Kelly (result × 0.25) is the recommended practical limit.
    """
    p = float(win_rate)
    q = 1.0 - p
    R = float(avg_rr)
    if R <= 0:
        return 0.0
    return p - q / R


# ── Stats from trade history ──────────────────────────────────────────────────

def compute_trade_stats() -> dict:
    """Load all closed trades and return win_rate + avg_rr.

    Returns
    -------
    dict with keys:
      win_rate      — WIN / (WIN + LOSS), or None if fewer than 10 trades
      avg_win_rr    — average R-multiple for WIN trades (avg_rr for Kelly/RoR)
      avg_all_rr    — expectancy: average R across all decisive trades
      wins          — count of WIN trades
      losses        — count of LOSS trades
      decisive      — total WIN + LOSS
    """
    try:
        from src import tracker as _trk
        rows     = _trk.load()
        decisive = [r for r in rows if r.get("status") in ("WIN", "LOSS")]
    except Exception:
        return {"win_rate": None, "avg_win_rr": None, "avg_all_rr": None,
                "wins": 0, "losses": 0, "decisive": 0}

    wins   = [r for r in decisive if r["status"] == "WIN"]
    losses = [r for r in decisive if r["status"] == "LOSS"]
    n      = len(decisive)

    if n < 10:  # too few trades for reliable statistics
        return {"win_rate": None, "avg_win_rr": None, "avg_all_rr": None,
                "wins": len(wins), "losses": len(losses), "decisive": n}

    def _r(row):
        try:
            return float(row.get("r_multiple") or 0)
        except (TypeError, ValueError):
            return 0.0

    win_rate   = len(wins) / n
    avg_win_rr = (sum(_r(r) for r in wins)   / len(wins))  if wins   else 1.0
    avg_all_rr = (sum(_r(r) for r in decisive) / n)

    return {
        "win_rate":   win_rate,
        "avg_win_rr": max(avg_win_rr, 0.1),   # floor at 0.1 to avoid divide-by-zero
        "avg_all_rr": avg_all_rr,
        "wins":       len(wins),
        "losses":     len(losses),
        "decisive":   n,
    }


# ── Sharpe ratio ─────────────────────────────────────────────────────────────

def sharpe_ratio(r_multiples: list, trades_per_year: float = 50.0) -> float | None:
    """Annualised Sharpe ratio from a list of R-multiple outcomes.

    Uses risk-free rate = 0 (standard for leveraged trading).
    Annualises by assuming *trades_per_year* trades (default 50 = ~1/week).

    Returns None when fewer than 5 trades or std deviation is zero.
    """
    if len(r_multiples) < 5:
        return None
    n    = len(r_multiples)
    mean = sum(r_multiples) / n
    variance = sum((x - mean) ** 2 for x in r_multiples) / (n - 1)
    std  = math.sqrt(variance)
    if std == 0:
        return None
    per_trade_sharpe = mean / std
    return round(per_trade_sharpe * math.sqrt(trades_per_year), 2)


def compute_sharpe_from_history() -> dict:
    """Load all closed trades and compute Sharpe ratio.

    Returns dict with: sharpe, n_trades, mean_r, std_r, verdict.
    """
    try:
        from src import tracker as _trk
        rows     = _trk.load()
        decisive = [r for r in rows if r.get("status") in ("WIN", "LOSS")]
    except Exception:
        return {"sharpe": None, "n_trades": 0, "verdict": "insufficient data"}

    def _r(row):
        try:
            return float(row.get("r_multiple") or 0)
        except (TypeError, ValueError):
            return 0.0

    r_multiples = [_r(r) for r in decisive]
    n = len(r_multiples)

    if n < 5:
        return {"sharpe": None, "n_trades": n, "verdict": f"need 5+ trades (have {n})"}

    mean_r = sum(r_multiples) / n
    variance = sum((x - mean_r) ** 2 for x in r_multiples) / max(n - 1, 1)
    std_r = math.sqrt(variance)

    sr = sharpe_ratio(r_multiples)

    if sr is None:
        verdict = "cannot compute (zero variance)"
    elif sr >= 2.0:
        verdict = "excellent — institutional grade"
    elif sr >= 1.0:
        verdict = "good — above average retail"
    elif sr >= 0.5:
        verdict = "acceptable"
    elif sr >= 0:
        verdict = "marginal — monitor closely"
    else:
        verdict = "negative — system destroys risk-adjusted value"

    return {
        "sharpe":      sr,
        "n_trades":    n,
        "mean_r":      round(mean_r, 3),
        "std_r":       round(std_r, 3),
        "verdict":     verdict,
    }


# ── FTMO compliance metrics ───────────────────────────────────────────────────

def compute_ftmo_metrics(fund_start: float, current_balance: float,
                          peak_balance: float, max_daily_loss_pct: float,
                          max_total_dd_pct: float) -> dict:
    """Compute FTMO challenge rule compliance metrics.

    Parameters
    ----------
    fund_start          starting balance at beginning of challenge
    current_balance     current estimated fund balance
    peak_balance        highest balance seen during the challenge
    max_daily_loss_pct  worst single-day loss seen (positive = loss)
    max_total_dd_pct    maximum drawdown from peak seen (positive = drawdown)

    Returns dict with FTMO rule status for each metric.
    """
    profit_pct = (current_balance - fund_start) / fund_start * 100 if fund_start > 0 else 0.0
    total_dd_pct = (peak_balance - current_balance) / peak_balance * 100 if peak_balance > 0 else 0.0

    daily_ok   = max_daily_loss_pct < FTMO_MAX_DAILY_LOSS_PCT
    total_ok   = max_total_dd_pct   < FTMO_MAX_TOTAL_LOSS_PCT
    target_ok  = profit_pct         >= FTMO_PROFIT_TARGET_PCT
    p2_ok      = profit_pct         >= FTMO_PHASE2_TARGET_PCT

    daily_headroom  = FTMO_MAX_DAILY_LOSS_PCT  - max_daily_loss_pct
    total_headroom  = FTMO_MAX_TOTAL_LOSS_PCT  - max_total_dd_pct
    profit_to_go_p1 = max(0.0, FTMO_PROFIT_TARGET_PCT - profit_pct)
    profit_to_go_p2 = max(0.0, FTMO_PHASE2_TARGET_PCT - profit_pct)

    return {
        "profit_pct":          round(profit_pct, 2),
        "total_dd_pct":        round(total_dd_pct, 2),
        "max_daily_loss_pct":  round(max_daily_loss_pct, 2),
        "max_total_dd_pct":    round(max_total_dd_pct, 2),
        "daily_rule_ok":       daily_ok,
        "total_rule_ok":       total_ok,
        "phase1_target_hit":   target_ok,
        "phase2_target_hit":   p2_ok,
        "daily_headroom_pct":  round(daily_headroom, 2),
        "total_headroom_pct":  round(total_headroom, 2),
        "profit_to_go_p1":     round(profit_to_go_p1, 2),
        "profit_to_go_p2":     round(profit_to_go_p2, 2),
        "challenge_viable":    daily_ok and total_ok,
    }


def build_ftmo_section(fund_start: float, current_balance: float,
                        peak_balance: float, max_daily_loss_pct: float,
                        max_total_dd_pct: float) -> list:
    """Return Telegram-formatted FTMO compliance lines for the Monday report."""
    m = compute_ftmo_metrics(fund_start, current_balance, peak_balance,
                              max_daily_loss_pct, max_total_dd_pct)
    lines = ["", "🎯 <b>FTMO CHALLENGE METRICS</b>"]

    if not m["challenge_viable"]:
        lines.append("🚨 <b>CHALLENGE FAILED</b> — a rule has been breached")

    p_icon = "✅" if m["phase1_target_hit"] else ("🟢" if m["phase2_target_hit"] else "⏳")
    p_progress = "✅ HIT" if m["phase1_target_hit"] else f"{m['profit_to_go_p1']:.1f}% to go"
    lines.append(
        f"{p_icon} Profit: {m['profit_pct']:+.1f}% "
        f"(Phase 1 target: {FTMO_PROFIT_TARGET_PCT:.0f}% — {p_progress})"
    )

    d_icon = "✅" if m["daily_rule_ok"] else "🚨"
    lines.append(
        f"{d_icon} Max daily loss: {m['max_daily_loss_pct']:.1f}% "
        f"(limit: {FTMO_MAX_DAILY_LOSS_PCT:.0f}% — "
        f"{m['daily_headroom_pct']:.1f}% headroom)"
    )

    t_icon = "✅" if m["total_rule_ok"] else "🚨"
    lines.append(
        f"{t_icon} Max total drawdown: {m['max_total_dd_pct']:.1f}% "
        f"(limit: {FTMO_MAX_TOTAL_LOSS_PCT:.0f}% — "
        f"{m['total_headroom_pct']:.1f}% headroom)"
    )

    return lines


# ── Plain-English output ──────────────────────────────────────────────────────

_MIN_TRADES = 10   # minimum decisive trades before we show the calculation


def _ror_verdict(ror_pct: float) -> tuple:
    """Return (icon, verdict_text) for a given RoR %."""
    if ror_pct >= 50:
        return "🚨", "extremely dangerous — reduce position size immediately"
    if ror_pct >= 20:
        return "🚨", "dangerous — reduce position size"
    if ror_pct >= 5:
        return "⚠️", "elevated risk — consider reducing position size"
    if ror_pct >= 1:
        return "🟡", "acceptable but monitor closely"
    if ror_pct >= 0.1:
        return "🟢", "low — within professional range"
    return "🟢", "extremely safe"


def build_ror_section(current_risk_pct: float) -> list:
    """Return a list of Telegram-formatted lines for the weekly report.

    Parameters
    ----------
    current_risk_pct  current base risk per trade as a fraction (e.g. 0.01)
    """
    lines = ["", "📊 <b>RISK ANALYSIS</b>"]

    stats = compute_trade_stats()
    n     = stats["decisive"]

    if n < _MIN_TRADES:
        lines.append(
            f"⚠️ Risk of ruin: need {_MIN_TRADES} closed trades to calculate "
            f"(have {n} — collecting data)"
        )
        return lines

    win_rate  = stats["win_rate"]
    avg_rr    = stats["avg_win_rr"]
    win_pct   = win_rate * 100
    risk_pct  = current_risk_pct * 100
    ror       = risk_of_ruin(win_rate, avg_rr, current_risk_pct)
    ror_pct   = ror * 100
    kelly_f   = kelly_criterion(win_rate, avg_rr)
    kelly_pct = kelly_f * 100
    qk_pct    = kelly_pct * 0.25

    # ── Risk of Ruin line ─────────────────────────────────────────────────────
    if ror >= 1.0:
        ror_str = "100%"
        icon    = "🚨"
        verdict = "negative expectancy — ruin is certain if you continue trading this system"
    else:
        if ror_pct >= 0.1:
            ror_str = f"{ror_pct:.1f}%"
        elif ror_pct > 0.001:
            ror_str = f"{ror_pct:.2f}%"
        else:
            ror_str = "< 0.01%"
        icon, verdict = _ror_verdict(ror_pct)

    lines.append(
        f"{icon} <b>Risk of Ruin:</b> At your current win rate of {win_pct:.0f}% "
        f"(avg {avg_rr:.1f}:1 R:R on wins) and {risk_pct:.2g}% risk per trade, "
        f"your risk of ruin is {ror_str} — {verdict}"
    )

    # ── Kelly Criterion line ──────────────────────────────────────────────────
    if kelly_f <= 0:
        lines.append(
            "📐 <b>Kelly Criterion:</b> Negative expectancy — "
            "no position size is mathematically sound for this system"
        )
    else:
        if current_risk_pct <= qk_pct / 100:
            comparison = (
                f"your current {risk_pct:.2g}% is conservative and safe (recommended)"
            )
        elif current_risk_pct <= kelly_f / 2:
            comparison = (
                f"your current {risk_pct:.2g}% is within the safe range"
            )
        elif current_risk_pct <= kelly_f:
            comparison = (
                f"your current {risk_pct:.2g}% is above Quarter Kelly — consider reducing"
            )
        else:
            comparison = (
                f"your current {risk_pct:.2g}% exceeds full Kelly — dangerous, reduce immediately"
            )

        lines.append(
            f"📐 <b>Kelly Criterion</b> suggests {kelly_pct:.1f}% risk per trade "
            f"(Quarter Kelly: {qk_pct:.1f}%) — {comparison}"
        )

    # ── Expectancy footnote ───────────────────────────────────────────────────
    exp = stats["avg_all_rr"]
    if exp > 0:
        lines.append(
            f"   System expectancy: {exp:+.2f}R per trade across "
            f"{n} decisive trades ({stats['wins']}W / {stats['losses']}L)"
        )
    else:
        lines.append(
            f"   ⚠️ System expectancy: {exp:+.2f}R — negative edge detected across "
            f"{n} trades ({stats['wins']}W / {stats['losses']}L)"
        )

    return lines
