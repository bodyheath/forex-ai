"""Learning memory system.

Reads closed trades from the tracker, computes win/loss statistics overall and by
segment (confidence band, direction, layer agreement), and rewrites the "auto"
records in system memory. Those records feed straight back into the analyst prompt,
so the model's future analysis is conditioned on what has actually worked.

Conservative by design: a segment only becomes a remembered pattern once it has at
least MIN_SAMPLES closed trades, so we never over-fit to one or two results.
"""

from src import memory, tracker

MIN_SAMPLES = 4


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_WIN_STATUSES    = {"WIN", "FULL_WIN", "PARTIAL_WIN"}
_LOSS_STATUSES   = {"LOSS"}
_DECISIVE_STATUSES = {"WIN", "FULL_WIN", "PARTIAL_WIN", "LOSS"}
_CLOSED_STATUSES = _WIN_STATUSES | _LOSS_STATUSES | {"BREAKEVEN"}


def _is_win(r) -> bool:
    """PARTIAL_WIN is a real, decisive, money-affecting close that isn't
    uniformly a win by label -- real data has PARTIAL_WIN trades that closed
    net-negative after costs. Classify it by net_pips sign instead of the
    raw status label, matching the precedent already established in
    risk_manager.py::_is_win_outcome(), dynamic_threshold.py, dashboard.py's
    research-analytics section, and financials.py."""
    status = r.get("status")
    if status in ("WIN", "FULL_WIN"):
        return True
    if status == "PARTIAL_WIN":
        try:
            return float(r.get("net_pips") or 0) > 0
        except (TypeError, ValueError):
            return False  # unparseable/missing net_pips -- conservative, not a win
    return False


def _closed(rows):
    return [r for r in rows if r.get("status") in _CLOSED_STATUSES]


def _winrate(rows):
    decisive = [r for r in rows if r.get("status") in _DECISIVE_STATUSES]
    if not decisive:
        return None, 0
    wins = sum(1 for r in decisive if _is_win(r))
    return wins / len(decisive), len(decisive)


def _expectancy(rows):
    rs = [_to_float(r.get("r_multiple")) for r in rows]
    rs = [r for r in rs if r is not None]
    return (sum(rs) / len(rs)) if rs else None


def _segment_pattern(label, rows):
    """Build a remembered pattern for a segment, or None if too few samples.

    2026-08-30: the verdict used to key off win rate alone (wr < 0.5 ->
    "reduce confidence", else "performed well"). That's backwards for an
    R-multiple strategy: a segment can have a losing win rate but positive
    expectancy (small frequent losses, larger rare wins) or a winning win
    rate but negative expectancy (frequent small wins, rare large losses) --
    confirmed live in this system's own data (BUY setups: WR 33%/expectancy
    +0.48R; SELL setups: WR 62%/expectancy -0.29R), so the old logic was
    telling the model to distrust a profitable segment and trust a losing
    one. Expectancy is what actually answers "is this segment worth taking";
    win rate now only drives the verdict when no r_multiple data exists to
    compute expectancy from.
    """
    wr, n = _winrate(rows)
    if wr is None or n < MIN_SAMPLES:
        return None
    exp = _expectancy(rows)
    exp_txt = f", expectancy {exp:+.2f}R" if exp is not None else ""
    if exp is not None:
        verdict = (
            "This segment has been unprofitable overall (negative expectancy) - reduce confidence here."
            if exp < 0 else
            "This segment has been profitable overall (positive expectancy) - a point in its favour, but still demand full confluence."
        )
    else:
        verdict = (
            "This segment has underperformed by win rate (no expectancy data available) - reduce confidence here."
            if wr < 0.5 else
            "This segment has performed well by win rate (no expectancy data available) - a point in its favour, but still demand full confluence."
        )
    return {
        "pattern": f"{label} (n={n} closed trades)",
        "outcome": f"Win rate {wr*100:.0f}%{exp_txt}. {verdict}",
    }


def compute_stats() -> dict:
    rows = tracker.load()
    closed = _closed(rows)
    wr, n = _winrate(closed)
    return {
        "total_recommendations": len(rows),
        "actionable": sum(1 for r in rows if r.get("trade_this") == "YES"),
        "open": sum(1 for r in rows if r.get("status") == "OPEN"),
        "closed": len(closed),
        "wins": sum(1 for r in closed if r["status"] in _WIN_STATUSES),
        "losses": sum(1 for r in closed if r["status"] in _LOSS_STATUSES),
        "win_rate": wr,
        "decisive": n,
        "expectancy_r": _expectancy(closed),
    }


def update_memory() -> dict:
    """Recompute auto-patterns from closed trades and write them to memory."""
    rows = tracker.load()
    closed = _closed(rows)
    patterns = []

    if not closed:
        patterns.append({
            "pattern": "No closed trades recorded yet",
            "outcome": "No empirical edge data available. Rely on seed priors and "
                       "demand the full 7+/4-source bar before trading.",
        })
        memory.set_auto_patterns(patterns)
        return {"patterns_written": len(patterns), **compute_stats()}

    wr, n = _winrate(closed)
    exp = _expectancy(closed)
    if wr is not None:
        exp_txt = f", expectancy {exp:+.2f}R" if exp is not None else ""
        patterns.append({
            "pattern": f"Overall track record to date (n={n})",
            "outcome": f"Win rate {wr*100:.0f}%{exp_txt}.",
        })

    # Segment: high vs low confidence at recommendation time.
    high_conf = [r for r in closed if (_to_float(r.get("confidence")) or 0) >= 7]
    low_conf = [r for r in closed if (_to_float(r.get("confidence")) or 0) < 7]
    for label, seg in (
        ("Setups taken at confidence >= 7", high_conf),
        ("Setups taken at confidence < 7", low_conf),
    ):
        p = _segment_pattern(label, seg)
        if p:
            patterns.append(p)

    # Segment: by direction.
    for d in ("BUY", "SELL"):
        p = _segment_pattern(f"{d} setups", [r for r in closed if (r.get("direction") or "").upper() == d])
        if p:
            patterns.append(p)

    # Segment: strong technical+fundamental agreement (both >= 6).
    aligned = [
        r for r in closed
        if (_to_float(r.get("technical")) or 0) >= 6 and (_to_float(r.get("fundamental")) or 0) >= 6
    ]
    p = _segment_pattern("Setups where technical AND fundamental both scored >= 6", aligned)
    if p:
        patterns.append(p)

    memory.set_auto_patterns(patterns)
    return {"patterns_written": len(patterns), **compute_stats()}
