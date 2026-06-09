"""Multi-timeframe confluence analysis.

Derives a directional signal (BUY / SELL / NEUTRAL) from each of five
timeframes already present in the technical bundle and computes a weighted
confluence score.  No extra API calls — all inputs come from the data
fetched by technical.analyse().

Timeframe weights:
  Monthly  30%  — structural trend bias
  Weekly   25%  — intermediate trend
  Daily    20%  — current setup
  4-Hour   15%  — entry alignment
  1-Hour   10%  — timing confirmation

TRADE_THIS YES requires >=4/5 timeframes to agree on direction.
This rule is enforced here (qualifies flag) AND as a hard gate in service.py.
"""

_TF_WEIGHTS: dict = {
    "monthly": 0.30,
    "weekly":  0.25,
    "daily":   0.20,
    "h4":      0.15,
    "h1":      0.10,
}

# Maps our canonical TF names to the keys used in the technical bundle dict
_TF_DATA_KEYS: dict = {
    "monthly": "monthly",
    "weekly":  "weekly",
    "daily":   "daily",
    "h4":      "4h",
    "h1":      "1h",
}

_ABBREV: dict = {
    "monthly": "M",
    "weekly":  "W",
    "daily":   "D",
    "h4":      "4H",
    "h1":      "1H",
}

_MIN_AGREEING = 4   # minimum timeframes that must agree for TRADE_THIS YES
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
      signals        — {tf_name: BUY/SELL/NEUTRAL}
      direction      — dominant direction (BUY / SELL / NEUTRAL)
      agreeing_count — number of timeframes matching dominant direction
      weighted_score — 0.0–1.0 sum of weights for agreeing timeframes
      breakdown      — compact string: "M:BUY W:BUY D:BUY 4H:BUY 1H:SELL"
      qualifies      — True if agreeing_count >= 4 (hard TRADE_THIS gate)
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

    buy_count  = sum(1 for s in signals.values() if s == "BUY")
    sell_count = sum(1 for s in signals.values() if s == "SELL")

    if buy_count == 0 and sell_count == 0:
        dominant       = "NEUTRAL"
        agreeing_count = 0
    elif buy_count >= sell_count:
        dominant       = "BUY"
        agreeing_count = buy_count
    else:
        dominant       = "SELL"
        agreeing_count = sell_count

    weighted_score = sum(
        _TF_WEIGHTS[tf]
        for tf, sig in signals.items()
        if sig == dominant
    )

    breakdown = " ".join(
        f"{_ABBREV[tf]}:{sig}" for tf, sig in signals.items()
    )

    return {
        "signals":        signals,
        "direction":      dominant,
        "agreeing_count": agreeing_count,
        "weighted_score": round(weighted_score, 2),
        "breakdown":      breakdown,
        "qualifies":      agreeing_count >= _MIN_AGREEING,
    }
