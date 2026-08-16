"""Dynamic confidence entry threshold.

Three factors adjust the threshold each scan:
  1. Market regime  — read from data/regime_state.json (written by market_regime.py)
  2. Recent win rate — last 50 decisive research trades, source=sonnet only
  3. Data quality   — overall_pct from data_quality.assess_scan()

Does NOT run regime detection itself; only reads regime_state.json.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import config

_MIN_SONNET_SAMPLE = 20   # below this, the sonnet-only signal isn't trustworthy yet
_STALENESS_DAYS     = 14  # oldest an "as of right now" window's newest trade may be


def _parse_dt(s: str):
    """Parse a closed_at string (naive, matches research_outcome_checker._parse_dt)."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt)], fmt)
        except (ValueError, TypeError):
            continue
    return None

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
    #
    # Population: source=sonnet only — the real, Claude-vetted candidates this
    # gate is meant to judge — not the blended (sonnet + indicative +
    # indicative_borderline) population research_tracker.load() returns by
    # default. The indicative tiers exist for a different, unrelated purpose
    # (research_analyst.py's 30-day threshold study) and ended up feeding this
    # gate only because compute() reused the same general-purpose loader, not
    # from any deliberate decision — confirmed via backtest to have been
    # ~80% synthetic-tier in the live window as of 2026-08-14, dragging the
    # reported win rate far below the real sonnet-tier population's.
    #
    # Two fallbacks, both routing to the pre-existing blended calculation
    # (no new logic path) rather than inventing a new default:
    #   - thin-sample: sonnet-only decisive n < _MIN_SONNET_SAMPLE — not
    #     enough signal yet (only genuinely binding in the system's first
    #     few days; backtested as never triggering since 2026-07-06).
    #   - stale: the newest sonnet-only decisive trade closed more than
    #     _STALENESS_DAYS ago — the window is real but no longer "recent"
    #     (confirmed live-active as of 2026-08-14: only 1 sonnet-tier
    #     decisive trade closed in the preceding 7 days).
    def _decisive_bucket(rows, limit=50):
        bucket = []
        for r in reversed(rows):
            st = (r.get("status") or "").upper()
            if st in ("WIN", "PARTIAL_WIN", "FULL_WIN"):
                bucket.append(True)
            elif st == "LOSS":
                bucket.append(False)
            else:
                continue
            if len(bucket) >= limit:
                break
        return bucket

    n_decisive = 0
    n_wins = 0
    win_rate = None
    win_rate_adj = 0.0
    win_rate_source = "no_data"
    if research_trades:
        sonnet_rows = [r for r in research_trades if (r.get("source") or "").lower() == "sonnet"]
        sonnet_decisive = _decisive_bucket(sonnet_rows)
        n_sonnet = len(sonnet_decisive)

        newest_dt = None
        if n_sonnet:
            for r in reversed(sonnet_rows):
                st = (r.get("status") or "").upper()
                if st in ("WIN", "LOSS", "PARTIAL_WIN", "FULL_WIN"):
                    newest_dt = _parse_dt(r.get("closed_at", ""))
                    break
        is_stale = True  # unknown age (unparseable/missing) is treated as stale — safer default
        if newest_dt is not None:
            now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
            is_stale = (now_naive - newest_dt).total_seconds() / 86400.0 > _STALENESS_DAYS

        if n_sonnet < _MIN_SONNET_SAMPLE:
            blended_decisive = _decisive_bucket(research_trades)
            n_decisive = len(blended_decisive)
            if n_decisive >= 20:
                win_rate = sum(blended_decisive) / n_decisive
            win_rate_source = "blended_thin_sample"
        elif is_stale:
            blended_decisive = _decisive_bucket(research_trades)
            n_decisive = len(blended_decisive)
            if n_decisive >= 20:
                win_rate = sum(blended_decisive) / n_decisive
            win_rate_source = "blended_stale"
        else:
            n_decisive = n_sonnet
            win_rate = sum(sonnet_decisive) / n_sonnet
            win_rate_source = "sonnet_only"

        if win_rate is not None:
            n_wins = round(win_rate * n_decisive)
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
        "win_rate_source":         win_rate_source,
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
