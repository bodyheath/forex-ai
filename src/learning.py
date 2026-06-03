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


def _closed(rows):
    return [r for r in rows if r.get("status") in ("WIN", "LOSS", "BREAKEVEN")]


def _winrate(rows):
    decisive = [r for r in rows if r.get("status") in ("WIN", "LOSS")]
    if not decisive:
        return None, 0
    wins = sum(1 for r in decisive if r["status"] == "WIN")
    return wins / len(decisive), len(decisive)


def _expectancy(rows):
    rs = [_to_float(r.get("r_multiple")) for r in rows]
    rs = [r for r in rs if r is not None]
    return (sum(rs) / len(rs)) if rs else None


def _segment_pattern(label, rows):
    """Build a remembered pattern for a segment, or None if too few samples."""
    wr, n = _winrate(rows)
    if wr is None or n < MIN_SAMPLES:
        return None
    exp = _expectancy(rows)
    exp_txt = f", expectancy {exp:+.2f}R" if exp is not None else ""
    verdict = (
        "This segment has underperformed - reduce confidence here."
        if wr < 0.5 else
        "This segment has performed well - a point in its favour, but still demand full confluence."
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
        "wins": sum(1 for r in closed if r["status"] == "WIN"),
        "losses": sum(1 for r in closed if r["status"] == "LOSS"),
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
