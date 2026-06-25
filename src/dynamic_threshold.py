"""Dynamic confidence entry threshold.

Three factors adjust the threshold each scan:
  1. Market regime  — read from data/regime_state.json (written by market_regime.py)
  2. Recent win rate — last 20 decisive research trades
  3. Data quality   — overall_pct from data_quality.assess_scan()

Does NOT run regime detection itself; only reads regime_state.json.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import config

_REGIME_STATE_FILE      = config.DATA_DIR / "regime_state.json"
_THRESHOLD_HISTORY_FILE = config.DATA_DIR / "threshold_history.json"
_MAX_HISTORY = 90

_REGIME_BASE = {
    "TRENDING_RISK_ON":  5.5,
    "TRENDING_RISK_OFF": 6.0,
    "RANGING_LOW_VOL":   7.0,
    "RANGING_HIGH_VOL":  7.5,
}
_DEFAULT_BASE = 6.0
_MIN_THRESHOLD = 5.0
_MAX_THRESHOLD = 9.5

_REGIME_DISPLAY = {
    "TRENDING_RISK_ON":  "Trending risk-on",
    "TRENDING_RISK_OFF": "Trending risk-off",
    "RANGING_LOW_VOL":   "Ranging low-vol",
    "RANGING_HIGH_VOL":  "Ranging high-vol",
}


def _read_regime() -> str:
    try:
        if _REGIME_STATE_FILE.exists():
            data = json.loads(_REGIME_STATE_FILE.read_text(encoding="utf-8"))
            return (data.get("regime") or "").upper().strip()
    except Exception:
        pass
    return ""


def _round_half(v: float) -> float:
    return round(v * 2) / 2


def compute(
    quality_pct: float = 100.0,
    scan_mode: str = "full",
    research_trades: list = None,
) -> dict:
    """Compute dynamic confidence threshold. Returns a dict with all components."""

    # Factor 1: Regime base
    regime = _read_regime()
    regime_base = _REGIME_BASE.get(regime, _DEFAULT_BASE)

    # Factor 2: Win rate adjustment (last 50 decisive research trades)
    # 50-trade window (~2-3 months at normal cadence) gives a stable signal;
    # the old 20-trade window swung the threshold ±1.0 from a single bad week.
    n_decisive = 0
    n_wins = 0
    win_rate = None
    win_rate_adj = 0.0
    if research_trades:
        decisive = []
        for r in reversed(research_trades):
            st = (r.get("status") or "").upper()
            if st in ("WIN", "PARTIAL_WIN", "FULL_WIN"):
                decisive.append(True)
            elif st == "LOSS":
                decisive.append(False)
            if len(decisive) >= 50:
                break
        n_decisive = len(decisive)
        if n_decisive >= 20:
            n_wins = sum(decisive)
            win_rate = n_wins / n_decisive
            if win_rate > 0.55:
                win_rate_adj = -0.5
            elif win_rate >= 0.45:
                win_rate_adj = 0.0
            elif win_rate >= 0.35:
                win_rate_adj = 0.5
            else:
                win_rate_adj = 1.0

    # Factor 3: Data quality adjustment
    if quality_pct > 80:
        dq_adj = 0.0
    elif quality_pct >= 60:
        dq_adj = 0.5
    elif quality_pct >= 40:
        dq_adj = 1.0
    else:
        dq_adj = 1.5

    raw = regime_base + win_rate_adj + dq_adj
    final = max(_MIN_THRESHOLD, min(_MAX_THRESHOLD, _round_half(raw)))

    return {
        "regime":                  regime or "UNKNOWN",
        "regime_base":             regime_base,
        "decisive_trades_sample":  n_decisive,
        "win_rate_recent":         round(win_rate, 3) if win_rate is not None else None,
        "win_rate_adjustment":     win_rate_adj,
        "data_quality_pct":        round(quality_pct),
        "data_quality_adjustment": dq_adj,
        "final_threshold":         final,
        "scan_mode":               scan_mode,
    }


def append_history(threshold_data: dict) -> None:
    """Append to threshold_history.json (keep last 90 entries)."""
    try:
        history = []
        if _THRESHOLD_HISTORY_FILE.exists():
            try:
                history = json.loads(_THRESHOLD_HISTORY_FILE.read_text(encoding="utf-8"))
            except Exception:
                history = []
        entry = {**threshold_data, "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()}
        history.append(entry)
        history = history[-_MAX_HISTORY:]
        _THRESHOLD_HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")
    except Exception:
        pass


def build_telegram_lines(threshold_data: dict) -> list:
    """Return Telegram display lines for the threshold section."""
    if not threshold_data:
        return []
    t       = threshold_data.get("final_threshold", 6.0)
    regime  = threshold_data.get("regime", "UNKNOWN")
    base    = threshold_data.get("regime_base", 6.0)
    wr      = threshold_data.get("win_rate_recent")
    wr_adj  = threshold_data.get("win_rate_adjustment", 0.0)
    dq_pct  = threshold_data.get("data_quality_pct", 100)
    dq_adj  = threshold_data.get("data_quality_adjustment", 0.0)

    regime_lbl = _REGIME_DISPLAY.get(regime, "Unknown regime")
    parts = [f"{regime_lbl}: {base:.1f} base"]
    if wr is not None:
        wr_pct = int(round(wr * 100))
        sign = "+" if wr_adj > 0 else ""
        if wr_adj != 0.0:
            parts.append(f"Win rate {wr_pct}% ({sign}{wr_adj:.1f})")
        else:
            parts.append(f"Win rate {wr_pct}% (no adj)")
    if dq_adj != 0.0:
        sign = "+" if dq_adj > 0 else ""
        parts.append(f"Data quality {dq_pct}% ({sign}{dq_adj:.1f})")

    return [
        f"📊 <b>Today's entry threshold: {t:.1f}/10</b>",
        " · ".join(parts),
        f"Only setups scoring {t:.1f}+ recommended today",
    ]


def build_weekly_report_lines() -> list:
    """Return section 8 lines for the Monday learning report."""
    try:
        if not _THRESHOLD_HISTORY_FILE.exists():
            return []
        history = json.loads(_THRESHOLD_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

    day_thresholds: dict = {}
    for entry in history:
        try:
            ts = datetime.fromisoformat(entry.get("timestamp", ""))
            if entry.get("scan_mode") == "full":
                day_key = ts.strftime("%a")
                day_thresholds[day_key] = entry.get("final_threshold", 0)
        except Exception:
            pass

    if not day_thresholds:
        return []

    lines: list = ["", "<b>8. THRESHOLD PERFORMANCE THIS WEEK</b>"]
    day_order = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    day_parts = [f"{d}: {day_thresholds[d]:.1f}" for d in day_order if d in day_thresholds]
    avg = sum(day_thresholds.values()) / len(day_thresholds)
    if day_parts:
        lines.append(" · ".join(day_parts))
        lines.append(f"Average: {avg:.1f}")

    try:
        from src import research_tracker as _rt_th
        bands = {"5.0-6.0": [], "6.0-7.0": [], "7.0-8.0": [], "8.0+": []}
        for tr in _rt_th.load():
            thr_raw = tr.get("threshold_at_entry")
            if not thr_raw:
                continue
            try:
                thr = float(thr_raw)
            except (TypeError, ValueError):
                continue
            st = (tr.get("status") or "").upper()
            is_win  = st in ("WIN", "PARTIAL_WIN", "FULL_WIN")
            is_loss = st == "LOSS"
            if not (is_win or is_loss):
                continue
            if thr < 6.0:
                band = "5.0-6.0"
            elif thr < 7.0:
                band = "6.0-7.0"
            elif thr < 8.0:
                band = "7.0-8.0"
            else:
                band = "8.0+"
            bands[band].append(is_win)
        lines.append("")
        lines.append("By threshold band:")
        for band, results in bands.items():
            if results:
                wr = sum(results) / len(results)
                lines.append(
                    f"{band}: {len(results)} trade{'s' if len(results) != 1 else ''}"
                    f" → {int(round(wr * 100))}% win rate"
                )
            else:
                lines.append(f"{band}: 0 trades")
    except Exception:
        pass

    return lines
