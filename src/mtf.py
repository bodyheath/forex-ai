"""Multi-timeframe confluence analysis.

Derives a directional signal (BUY / SELL / NEUTRAL) from three core
timeframes and one contextual timeframe already present in the technical
bundle.  Optimised for 3-7 day swing trading.

Core timeframe weights:
  Weekly   40%  — trend direction
  Daily    40%  — setup timeframe
  4-Hour   20%  — entry timing

Monthly is informational only (+5% bonus when aligned, never blocks a
good weekly/daily/4H setup).  1-Hour has been removed as noise for
3-7 day swing trades.

TRADE_THIS YES requires weekly AND daily to agree on direction.
4-Hour is optional bonus.  This rule is enforced here (qualifies flag)
AND as a hard gate in service.py.
"""

_CORE_TF_WEIGHTS: dict = {
    "weekly":  0.40,
    "daily":   0.40,
    "h4":      0.20,
}

_MONTHLY_BONUS = 0.05  # informational bonus when monthly aligns with dominant

# Maps our canonical TF names to the keys used in the technical bundle dict
_TF_DATA_KEYS: dict = {
    "monthly": "monthly",
    "weekly":  "weekly",
    "daily":   "daily",
    "h4":      "4h",
}

_ABBREV: dict = {
    "monthly": "M",
    "weekly":  "W",
    "daily":   "D",
    "h4":      "4H",
}

_MIN_AGREEING = 2   # weekly + daily must both agree (4H is optional bonus)
_MIN_SCORE    = 4   # tech_signal score threshold below which we treat as NEUTRAL


def _tf_signal(tf_data: dict) -> str:
    """Extract BUY / SELL / NEUTRAL from a single timeframe summary dict."""
    if not isinstance(tf_data, dict):
        return "NEUTRAL"
    # Explicitly insufficient data
    status = str(tf_data.get("status") or "")
    if "insufficient" in status.lower() or status == "UNAVAILABLE":
        return "NEUTRAL"
    ts        = tf_data.get("tech_signal", {})
    direction = (ts.get("direction") or "NEUTRAL").upper()
    score     = ts.get("score", 1)
    if direction in ("BUY", "SELL") and score >= _MIN_SCORE:
        return direction
    return "NEUTRAL"


def analyse(tech_bundle: dict) -> dict:
    """Compute multi-timeframe confluence from a technical data bundle.

    Returns a dict with keys:
      signals        — {tf_name: BUY/SELL/NEUTRAL} for all 4 TFs
      direction      — dominant direction (BUY / SELL / NEUTRAL)
      agreeing_count — core TFs (W/D/4H) matching dominant direction (0-3)
      weighted_score — 0.0–1.0 sum of core TF weights + monthly bonus
      breakdown      — compact string: "M:BUY W:BUY D:BUY 4H:BUY"
      qualifies      — True if weekly AND daily both agree (mandatory gate)
    """
    if not isinstance(tech_bundle, dict) or tech_bundle.get("status") == "UNAVAILABLE":
        return {
            "signals": {tf: "NEUTRAL" for tf in _TF_DATA_KEYS},
            "direction":      "NEUTRAL",
            "agreeing_count": 0,
            "weighted_score": 0.0,
            "breakdown":      "UNAVAILABLE",
            "qualifies":      False,
        }

    signals: dict = {
        tf: _tf_signal(tech_bundle.get(data_key, {}))
        for tf, data_key in _TF_DATA_KEYS.items()
    }

    # Direction vote uses core TFs only (weekly, daily, h4)
    core_signals = {tf: signals[tf] for tf in _CORE_TF_WEIGHTS}
    buy_count  = sum(1 for s in core_signals.values() if s == "BUY")
    sell_count = sum(1 for s in core_signals.values() if s == "SELL")

    if buy_count == 0 and sell_count == 0:
        dominant       = "NEUTRAL"
        agreeing_count = 0
    elif buy_count >= sell_count:
        dominant       = "BUY"
        agreeing_count = buy_count
    else:
        dominant       = "SELL"
        agreeing_count = sell_count

    # Weekly and daily both NEUTRAL → no directional bias regardless of 4H direction.
    # A single 4H signal cannot establish a swing-trade recommendation without
    # weekly AND daily timeframe support.
    if signals["weekly"] == "NEUTRAL" and signals["daily"] == "NEUTRAL":
        dominant       = "NEUTRAL"
        agreeing_count = 0

    # Core weighted score + monthly bonus
    weighted_score = sum(
        _CORE_TF_WEIGHTS[tf]
        for tf, sig in core_signals.items()
        if sig == dominant
    )
    if signals["monthly"] == dominant and dominant != "NEUTRAL":
        weighted_score += _MONTHLY_BONUS
    weighted_score = min(1.0, weighted_score)

    breakdown = " ".join(
        f"{_ABBREV[tf]}:{sig}" for tf, sig in signals.items()
    )

    # Weekly AND daily must both agree — 4H is optional bonus
    qualifies = (
        dominant != "NEUTRAL"
        and signals["weekly"] == dominant
        and signals["daily"] == dominant
    )

    return {
        "signals":        signals,
        "direction":      dominant,
        "agreeing_count": agreeing_count,
        "weighted_score": round(weighted_score, 2),
        "breakdown":      breakdown,
        "qualifies":      qualifies,
    }
