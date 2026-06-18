"""Multi-timeframe confluence analysis.

Derives a directional signal (BUY / SELL / NEUTRAL) from three core
timeframes and one contextual timeframe already present in the technical
bundle.  Optimised for 3-7 day swing trading.

Core timeframe weights:
  Weekly   40%  — trend direction (anchor — must always agree)
  Daily    40%  — setup timeframe
  4-Hour   20%  — entry timing

Monthly is informational only (+5% bonus when aligned, never blocks a
good weekly/daily/4H setup).  1-Hour has been removed as noise for
3-7 day swing trades.

Graduated MTF gate (qualifies flag + mtf_gate string):
  strong_all3     W+D+4H all agree       → qualifies, no penalty
  strong_w_d      W+D agree (4H any)     → qualifies, no penalty
  strong_w_4h     W+4H agree, D neutral  → qualifies, no penalty
                  (daily consolidation within weekly trend = classic pullback)
  weak_weekly_only  W only, D+4H neutral → qualifies, confidence −1
  weak_mixed        W+some opposition    → qualifies, confidence −1
  blocked           W opposes D+4H, or D+4H both oppose W → blocked
  no_signal         W neutral or no dominant direction   → blocked

Confidence penalty (conf_penalty) is applied in service.py.
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

    # ── Graduated MTF gate ────────────────────────────────────────────────────
    w   = signals["weekly"]
    d   = signals["daily"]
    h4  = signals["h4"]

    if dominant == "NEUTRAL" or w == "NEUTRAL":
        # No weekly directional signal — no basis for a swing trade
        mtf_gate     = "no_signal"
        qualifies    = False
        conf_penalty = 0
    elif w != dominant:
        # Weekly actively opposes the direction that Daily+4H majority suggests
        # ("Weekly opposes Daily and 4H simultaneously" per spec)
        mtf_gate     = "blocked"
        qualifies    = False
        conf_penalty = 0
    else:
        # Weekly agrees with dominant — now classify strength from D and 4H
        _d_agrees   = (d == dominant)
        _d_neutral  = (d == "NEUTRAL")
        _d_opposes  = (d != "NEUTRAL" and not _d_agrees)
        _h4_agrees  = (h4 == dominant)
        _h4_neutral = (h4 == "NEUTRAL")
        _h4_opposes = (h4 != "NEUTRAL" and not _h4_agrees)

        if _d_agrees and _h4_agrees:
            # STRONGEST: Weekly + Daily + 4H all agree
            mtf_gate     = "strong_all3"
            qualifies    = True
            conf_penalty = 0
        elif _d_agrees:
            # STRONG: Weekly + Daily agree (4H neutral or opposing)
            mtf_gate     = "strong_w_d"
            qualifies    = True
            conf_penalty = 0
        elif _h4_agrees and _d_neutral:
            # STRONG: Weekly + 4H agree, Daily consolidating (neutral)
            # Classic pullback — daily consolidation within weekly trend
            mtf_gate     = "strong_w_4h"
            qualifies    = True
            conf_penalty = 0
        elif _d_neutral and _h4_neutral:
            # WEAK: only Weekly is directional, D and 4H both neutral
            mtf_gate     = "weak_weekly_only"
            qualifies    = True
            conf_penalty = 1
        elif _d_opposes and _h4_opposes:
            # BLOCKED: Daily and 4H both actively oppose the weekly direction
            mtf_gate     = "blocked"
            qualifies    = False
            conf_penalty = 0
        else:
            # WEAK: mixed signals — some opposition but not fully blocked
            mtf_gate     = "weak_mixed"
            qualifies    = True
            conf_penalty = 1

    return {
        "signals":        signals,
        "direction":      dominant,
        "agreeing_count": agreeing_count,
        "weighted_score": round(weighted_score, 2),
        "breakdown":      breakdown,
        "qualifies":      qualifies,
        "mtf_gate":       mtf_gate,
        "conf_penalty":   conf_penalty,
    }
