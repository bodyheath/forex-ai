"""Extract ML feature vectors from a trade analysis at the moment of logging.

These 15 features capture the key technical, fundamental, and contextual signals
at the time a trade setup is identified. They form the training data for the
win-probability model in src/ml_predictor.py.

Feature columns are fixed — do not reorder; the saved model depends on column order.
"""
import math
from datetime import datetime

FEATURE_COLS = [
    "confidence",      # overall confidence 1-10
    "tech_score",      # technical layer 1-10
    "fund_score",      # fundamental layer 1-10
    "sent_score",      # sentiment layer 1-10
    "pos_score",       # positioning layer 1-10
    "macro_score",     # macro layer 1-10
    "rsi14",           # RSI value 0-100 (50 = neutral when unavailable)
    "macd_signal",     # +1=bullish  0=flat  -1=bearish
    "atr_pct",         # ATR as percentage of price (normalised volatility)
    "reward_risk",     # R:R ratio from analysis
    "direction_buy",   # 1=BUY  0=SELL
    "mtf_count",       # agreeing core MTF timeframes 0-3 (weekly/daily/4H)
    "ribbon_aligned",  # 1=MA ribbon fully aligned with trade direction
    "month_sin",       # sin(2π·month/12) — seasonal cycle encoding
    "month_cos",       # cos(2π·month/12)
]


def _safe(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        return default if (f != f) else f      # guard NaN
    except (TypeError, ValueError):
        return default


def extract(pair: str, parsed: dict, bundle: dict,
            entry_time: datetime = None) -> dict:
    """Return feature dict for one trade analysis. Missing values → neutral defaults.

    pair        — e.g. "EUR/USD"
    parsed      — dict from recparse.parse() or equivalent
    bundle      — full analysis bundle (technical, mtf, fundamental, …)
    entry_time  — datetime the analysis ran (defaults to now)
    """
    if entry_time is None:
        entry_time = datetime.now()

    tech  = bundle.get("technical", {}) if isinstance(bundle, dict) else {}
    daily = tech.get("daily", {})        if isinstance(tech, dict)   else {}
    mtf   = bundle.get("mtf", {})        if isinstance(bundle, dict) else {}

    direction = (parsed.get("direction") or "BUY").upper()

    # Layer scores
    confidence  = _safe(parsed.get("confidence"),       5.0)
    tech_score  = _safe(parsed.get("technical_score"),  5.0)
    fund_score  = _safe(parsed.get("fundamental_score"),5.0)
    sent_score  = _safe(parsed.get("sentiment_score"),  5.0)
    pos_score   = _safe(parsed.get("positioning_score"),5.0)
    macro_score = _safe(parsed.get("macro_score"),      5.0)

    # RSI (50 = neutral)
    rsi14 = _safe(daily.get("rsi14"), 50.0)

    # MACD direction signal
    macd_h      = _safe(daily.get("macd_hist"), 0.0)
    macd_signal = 1.0 if macd_h > 0 else (-1.0 if macd_h < 0 else 0.0)

    # Normalised ATR
    price   = _safe(daily.get("last_close"), 1.0) or 1.0
    atr     = _safe(daily.get("atr14"), 0.0)
    atr_pct = (atr / price) * 100.0

    # R:R
    reward_risk = _safe(parsed.get("reward_risk"), 1.5)

    # Direction
    direction_buy = 1.0 if direction == "BUY" else 0.0

    # MTF agreement
    mtf_count = _safe(
        mtf.get("agreeing_count") if isinstance(mtf, dict) else None, 0.0
    )

    # MA ribbon alignment
    rib        = daily.get("ribbon", {}) if isinstance(daily, dict) else {}
    rib_status = rib.get("status", "NEUTRAL") if isinstance(rib, dict) else "NEUTRAL"
    ribbon_aligned = 1.0 if (
        (direction == "BUY"  and rib_status == "ALIGNED_BULL") or
        (direction == "SELL" and rib_status == "ALIGNED_BEAR")
    ) else 0.0

    # Seasonal cycle
    month     = entry_time.month
    month_sin = math.sin(2 * math.pi * month / 12)
    month_cos = math.cos(2 * math.pi * month / 12)

    return {
        "confidence":     round(confidence,   2),
        "tech_score":     round(tech_score,   2),
        "fund_score":     round(fund_score,   2),
        "sent_score":     round(sent_score,   2),
        "pos_score":      round(pos_score,    2),
        "macro_score":    round(macro_score,  2),
        "rsi14":          round(rsi14,        2),
        "macd_signal":    macd_signal,
        "atr_pct":        round(atr_pct,      4),
        "reward_risk":    round(reward_risk,  3),
        "direction_buy":  direction_buy,
        "mtf_count":      mtf_count,
        "ribbon_aligned": ribbon_aligned,
        "month_sin":      round(month_sin,    4),
        "month_cos":      round(month_cos,    4),
    }
