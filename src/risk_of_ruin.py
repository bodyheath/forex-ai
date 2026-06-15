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
