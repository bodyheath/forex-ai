"""Regime-specific and time-of-day win rate tracking.

Reads closed research trades and groups outcomes by:
  - market regime (from regime_base_at_entry column)
  - hour of day (UTC) when the trade was opened

Results saved to:
  data/regime_learning.json    — regime -> {wins, losses, n, win_rate}
  data/time_effectiveness.json — hour   -> {wins, losses, n, win_rate}
"""
import json
from datetime import datetime

import config

REGIME_FILE = config.DATA_DIR / "regime_learning.json"
TIME_FILE   = config.DATA_DIR / "time_effectiveness.json"

WIN_STATUSES  = {"WIN", "FULL_WIN", "PARTIAL_WIN"}
LOSS_STATUSES = {"LOSS"}

_SESSIONS = {
    "Asia (21-06 UTC)":     [21, 22, 23, 0, 1, 2, 3, 4, 5, 6],
    "London (07-16 UTC)":   [7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
    "New York (13-22 UTC)": [13, 14, 15, 16, 17, 18, 19, 20, 21, 22],
}


def _load(path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def update(research_trades: list = None) -> None:
    """Recompute regime and time-of-day win rates from all closed research trades."""
    if research_trades is None:
        try:
            from src import research_tracker as _rt
            research_trades = _rt.load()
        except Exception:
            return

    DECISIVE = WIN_STATUSES | LOSS_STATUSES
    regime_stats: dict = {}
    time_stats: dict   = {}

    for row in research_trades:
        status = (row.get("status") or "").upper()
        if status not in DECISIVE:
            continue
        win = status in WIN_STATUSES

        regime = (row.get("regime_base_at_entry") or "unknown").strip().lower()
        if regime not in regime_stats:
            regime_stats[regime] = {"wins": 0, "losses": 0}
        if win:
            regime_stats[regime]["wins"] += 1
        else:
            regime_stats[regime]["losses"] += 1

        ts = row.get("date") or row.get("timestamp", "")
        try:
            hour = datetime.fromisoformat(str(ts)[:16].replace(" ", "T")).hour
        except Exception:
            hour = -1
        if hour >= 0:
            k = str(hour)
            if k not in time_stats:
                time_stats[k] = {"wins": 0, "losses": 0}
            if win:
                time_stats[k]["wins"] += 1
            else:
                time_stats[k]["losses"] += 1

    for s in regime_stats.values():
        n = s["wins"] + s["losses"]
        s["n"] = n
        s["win_rate"] = round(s["wins"] / n, 3) if n > 0 else None

    for s in time_stats.values():
        n = s["wins"] + s["losses"]
        s["n"] = n
        s["win_rate"] = round(s["wins"] / n, 3) if n > 0 else None

    _save(REGIME_FILE, regime_stats)
    _save(TIME_FILE,   time_stats)


def get_regime_win_rate(regime: str) -> dict | None:
    return _load(REGIME_FILE).get((regime or "").lower())


def get_time_win_rate(hour: int) -> dict | None:
    return _load(TIME_FILE).get(str(hour))


def build_monday_report_lines() -> list:
    regime_data = _load(REGIME_FILE)
    time_data   = _load(TIME_FILE)

    lines = []

    if regime_data:
        lines.append("")
        lines.append("<b>REGIME WIN RATES</b>")
        for regime in sorted(regime_data):
            s  = regime_data[regime]
            n  = s.get("n", 0)
            wr = s.get("win_rate")
            if n >= 5 and wr is not None:
                lines.append(f"  {regime}: {wr:.0%} ({n} decisive trades)")

    if time_data:
        lines.append("")
        lines.append("<b>TRADING SESSION WIN RATES</b>")
        for session, hours in _SESSIONS.items():
            wins   = sum(time_data.get(str(h), {}).get("wins",   0) for h in hours)
            losses = sum(time_data.get(str(h), {}).get("losses", 0) for h in hours)
            n      = wins + losses
            if n >= 5:
                wr = wins / n
                lines.append(f"  {session}: {wr:.0%} ({n} trades)")

    return lines
