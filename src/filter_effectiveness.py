"""Per-filter win rate tracking for the five scoring dimensions.

For each of technical / fundamental / sentiment / positioning / macro,
tracks win rates split into high-signal (score >= 6) and low-signal bands.

Alerts when any high-signal filter's win rate drops below 55%.

Saved to data/filter_effectiveness.json.
"""
import json
from datetime import datetime

import config

FILTER_FILE = config.DATA_DIR / "filter_effectiveness.json"

WIN_STATUSES  = {"WIN", "FULL_WIN", "PARTIAL_WIN"}
LOSS_STATUSES = {"LOSS"}

FILTERS            = ["technical", "fundamental", "sentiment", "positioning", "macro"]
BAND_THRESHOLD     = 6.0
ALERT_THRESHOLD    = 0.55
MIN_TRADES_ALERT   = 20
MIN_TRADES_DISPLAY = 5


def _load() -> dict:
    try:
        return json.loads(FILTER_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    FILTER_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def update(research_trades: list = None) -> dict:
    """Recompute filter effectiveness from all closed research trades."""
    if research_trades is None:
        try:
            from src import research_tracker as _rt
            research_trades = _rt.load()
        except Exception:
            return {}

    DECISIVE = WIN_STATUSES | LOSS_STATUSES
    stats = {f: {"high": {"wins": 0, "n": 0}, "low": {"wins": 0, "n": 0}}
             for f in FILTERS}

    for row in research_trades:
        status = (row.get("status") or "").upper()
        if status not in DECISIVE:
            continue
        win = status in WIN_STATUSES
        for f in FILTERS:
            score = _to_float(row.get(f))
            if score is None:
                continue
            band = "high" if score >= BAND_THRESHOLD else "low"
            stats[f][band]["n"] += 1
            if win:
                stats[f][band]["wins"] += 1

    for f in FILTERS:
        for band in ("high", "low"):
            n = stats[f][band]["n"]
            w = stats[f][band]["wins"]
            stats[f][band]["win_rate"] = round(w / n, 3) if n > 0 else None

    stats["_updated"] = datetime.now().isoformat()[:19]
    _save(stats)
    return stats


def get_alerts() -> list:
    """Return alert strings for high-signal filters performing below threshold."""
    data   = _load()
    alerts = []
    for f in FILTERS:
        high = data.get(f, {}).get("high", {})
        n    = high.get("n", 0)
        wr   = high.get("win_rate")
        if n >= MIN_TRADES_ALERT and wr is not None and wr < ALERT_THRESHOLD:
            alerts.append(
                f"FILTER ALERT: {f.capitalize()} high-signal win rate "
                f"{wr:.0%} ({n} trades) — below {ALERT_THRESHOLD:.0%} threshold"
            )
    return alerts


def build_monday_report_lines() -> list:
    """Section for the Monday learning report."""
    data  = _load()
    lines = ["", "<b>FILTER EFFECTIVENESS</b>"]
    any_row = False

    for f in FILTERS:
        high = data.get(f, {}).get("high", {})
        low  = data.get(f, {}).get("low",  {})
        nh   = high.get("n", 0)
        nl   = low.get("n",  0)
        wr_h = high.get("win_rate")
        wr_l = low.get("win_rate")

        parts = []
        if nh >= MIN_TRADES_DISPLAY and wr_h is not None:
            flag = " [!]" if wr_h < ALERT_THRESHOLD else ""
            parts.append(f"high>={BAND_THRESHOLD:.0f}: {wr_h:.0%} ({nh}){flag}")
        if nl >= MIN_TRADES_DISPLAY and wr_l is not None:
            parts.append(f"low<{BAND_THRESHOLD:.0f}: {wr_l:.0%} ({nl})")
        if parts:
            any_row = True
            lines.append(f"  {f.capitalize()}: {' | '.join(parts)}")

    if not any_row:
        return []
    return lines
