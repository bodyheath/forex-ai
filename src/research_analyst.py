"""30-day threshold analysis for the research trading mode.

Compares the performance of paper trades by confidence band (5, 6, 7, 8-10)
to determine whether lowering the live-trade threshold from 7 to 6 would have
been profitable.  Only closed research trades WITH price levels (status WIN /
LOSS / BREAKEVEN / EXPIRED with an R-multiple) are included in statistics.
NO_PRICE_LEVELS trades (Haiku-only, no entry/stop/target) are counted but not
used in win-rate or R calculations.

Returns None when fewer than 30 days of data or fewer than 10 closed trackable
research trades exist — the caller should silently skip the Telegram section.
"""

from datetime import datetime, timedelta

from src import research_tracker

_MIN_DAYS  = 30
_MIN_TRADES = 10  # minimum closed trades with price levels for analysis


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _band(confidence) -> str | None:
    try:
        c = int(confidence)
    except (TypeError, ValueError):
        return None
    if c == 5:
        return "5"
    if c == 6:
        return "6"
    if c == 7:
        return "7"
    if c >= 8:
        return "8-10"
    return None


def _band_stats(rows: list) -> dict | None:
    """Win rate and expectancy for a group of closed research trades."""
    decisive = [r for r in rows if r.get("status") in ("WIN", "LOSS")]
    if not decisive:
        return None
    wins = sum(1 for r in decisive if r.get("status") == "WIN")
    win_rate = wins / len(decisive)
    rs = [_to_float(r.get("r_multiple")) for r in decisive]
    rs = [r for r in rs if r is not None]
    expectancy = round(sum(rs) / len(rs), 2) if rs else None
    return {
        "n":          len(decisive),
        "wins":       wins,
        "losses":     len(decisive) - wins,
        "win_rate":   win_rate,
        "expectancy": expectancy,
    }


def analyse(log=print) -> dict | None:
    """Run the 30-day threshold analysis.

    Returns a result dict or None if insufficient data.
    """
    all_rows = research_tracker.load()
    if not all_rows:
        log("Research analyst: no research trades recorded yet.")
        return None

    # Determine data age
    dates = [r.get("date", "") for r in all_rows if r.get("date", "")]
    if not dates:
        return None
    oldest = min(dates)
    try:
        days_of_data = (datetime.now() - datetime.strptime(oldest, "%Y-%m-%d")).days
    except ValueError:
        days_of_data = 0

    if days_of_data < _MIN_DAYS:
        log(f"Research analyst: {days_of_data} days of data (need {_MIN_DAYS}) — skipping.")
        return None

    # Use all closed trades (not just last 30 days — we want the full picture)
    closed = [
        r for r in all_rows
        if r.get("status") in ("WIN", "LOSS", "BREAKEVEN", "EXPIRED")
        and r.get("r_multiple") not in (None, "")
    ]

    if len(closed) < _MIN_TRADES:
        log(f"Research analyst: {len(closed)} closed trackable trades (need {_MIN_TRADES}) — skipping.")
        return None

    log(f"Research analyst: analysing {len(closed)} closed research trades over {days_of_data} days.")

    # Group by confidence band
    bands: dict = {"5": [], "6": [], "7": [], "8-10": []}
    for r in closed:
        b = _band(r.get("confidence"))
        if b:
            bands[b].append(r)

    band_results = {b: _band_stats(bands[b]) for b in ("5", "6", "7", "8-10")}

    # Real trade comparison (from main tracker)
    real_stats = None
    try:
        from src import learning
        real_stats = learning.compute_stats()
    except Exception:
        pass

    # Recommendation logic
    conf6 = band_results.get("6")
    conf7 = band_results.get("7")
    recommendation = "INSUFFICIENT_DATA"
    reasoning      = f"Confidence-6 band has {conf6['n'] if conf6 else 0} closed trades — need more data."

    if conf6 and conf6["n"] >= 5:
        exp6 = conf6.get("expectancy") or 0.0
        wr6  = conf6.get("win_rate") or 0.0
        if exp6 >= 0.3 and wr6 >= 0.45:
            recommendation = "LOWER_TO_6"
            reasoning = (
                f"Conf-6 trades: {wr6*100:.0f}% win rate, {exp6:+.2f}R expectancy "
                f"over {conf6['n']} trades → lowering threshold to 6 appears profitable."
            )
        elif exp6 < 0 or wr6 < 0.35:
            recommendation = "KEEP_AT_7"
            reasoning = (
                f"Conf-6 trades: {wr6*100:.0f}% win rate, {exp6:+.2f}R expectancy "
                f"over {conf6['n']} trades → insufficient edge, keep threshold at 7."
            )
        else:
            recommendation = "MARGINAL_EDGE"
            reasoning = (
                f"Conf-6 trades: {wr6*100:.0f}% win rate, {exp6:+.2f}R expectancy "
                f"over {conf6['n']} trades → marginal edge, continue collecting data."
            )

    return {
        "days_of_data":  days_of_data,
        "total_logged":  len(all_rows),
        "total_closed":  len(closed),
        "band_results":  band_results,
        "real_stats":    real_stats,
        "recommendation": recommendation,
        "reasoning":     reasoning,
    }
