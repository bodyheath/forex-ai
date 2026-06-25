"""Manages the active confidence threshold and minimum R:R ratio.

Values are persisted to data/threshold_config.json and read by analyst.py
at call time, so a reversion takes effect on the very next scan without a
redeploy.

Current state: confidence 7, min_rr 2.5 — standard quality mode.
R:R floor raised to 2.5 so the analyst TARGET (T3) always clears T2 (2.0R),
ensuring the cascade is mathematically sound on every trade.

After 50 closed YES-trades, check_and_adjust() evaluates the overall win rate.
If it is below 45% the thresholds are automatically reverted to 7 / 2.5 and
a Telegram-ready message is returned so the user is notified immediately.
"""

import json
from datetime import datetime

import config

_CONFIG_FILE = config.DATA_DIR / "threshold_config.json"

_DEFAULTS = {
    "confidence_threshold":  7,
    "min_rr":                2.5,
    "data_collection_mode":  False,
    "lowered_at":            "2026-06-09",
    "auto_revert_trades":    50,
    "auto_revert_win_rate":  0.45,
    "reverted_at":           "2026-06-25",
    "revert_reason":         "R:R restructure — T1/T2 raised to 1R/2R, min_rr raised to 2.5 to clear T2 floor",
}

_ORIGINAL_THRESHOLD = 7
_ORIGINAL_MIN_RR    = 2.5


def load() -> dict:
    if _CONFIG_FILE.exists():
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            return {**_DEFAULTS, **data}
        except Exception:
            pass
    save(_DEFAULTS.copy())
    return _DEFAULTS.copy()


def save(cfg: dict) -> None:
    _CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def get_confidence_threshold() -> int:
    return int(load().get("confidence_threshold", 6))


def get_min_rr() -> float:
    return float(load().get("min_rr", 1.3))


def is_data_collection_mode() -> bool:
    return bool(load().get("data_collection_mode", True))


def check_and_adjust(log=print):
    """Evaluate win rate after N closed trades; revert thresholds if too low.

    Returns a Telegram-ready alert string when a reversion occurs, else None.
    Called once per daily run after the outcome and learning steps.
    """
    cfg = load()

    if not cfg.get("data_collection_mode"):
        return None  # already reverted, nothing to do

    required = int(cfg.get("auto_revert_trades", 50))
    min_wr   = float(cfg.get("auto_revert_win_rate", 0.45))

    try:
        from src import tracker
        rows       = tracker.load()
        closed_yes = [
            r for r in rows
            if r.get("status") in ("WIN", "LOSS") and r.get("trade_this") == "YES"
        ]
    except Exception as exc:
        log(f"Threshold check: could not load trades — {exc}")
        return None

    total = len(closed_yes)
    if total < required:
        log(f"Threshold check: {total}/{required} closed YES-trades — threshold review pending.")
        return None

    wins     = sum(1 for r in closed_yes if r.get("status") == "WIN")
    win_rate = wins / total

    if win_rate >= min_wr:
        log(
            f"Threshold check: {total} trades, win rate {win_rate*100:.0f}% "
            f">= {min_wr*100:.0f}% — thresholds performing, no change needed."
        )
        return None

    # Win rate too low — revert
    cfg.update({
        "confidence_threshold": _ORIGINAL_THRESHOLD,
        "min_rr":               _ORIGINAL_MIN_RR,
        "data_collection_mode": False,
        "reverted_at":          datetime.now().strftime("%Y-%m-%d"),
        "revert_reason": (
            f"Win rate {win_rate*100:.0f}% after {total} closed trades "
            f"— below {min_wr*100:.0f}% minimum"
        ),
    })
    save(cfg)
    log(
        f"Threshold auto-reverted: confidence→{_ORIGINAL_THRESHOLD}, R:R→{_ORIGINAL_MIN_RR} "
        f"(win rate {win_rate*100:.0f}% after {total} trades)"
    )
    return (
        f"⚠️ <b>Threshold auto-reverted to standard settings</b>\n"
        f"Confidence: → {_ORIGINAL_THRESHOLD}  |  R:R: → {_ORIGINAL_MIN_RR}\n"
        f"Reason: win rate {win_rate*100:.0f}% after {total} closed trades "
        f"was below the {min_wr*100:.0f}% minimum.\n"
        f"Standard thresholds active from next scan."
    )
