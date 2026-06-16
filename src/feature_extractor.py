"""Extract ML feature vectors from a trade analysis at the moment of logging.

These 40 features capture the key technical, fundamental, contextual, and
market-regime signals at the time a trade setup is identified.  They form the
training data for the win-probability model in src/ml_predictor.py.

Feature columns are append-only — never reorder or remove (the saved model
depends on column order; new features are appended so old model pkl files
still work on the first 15 columns until a retrain picks up the full set).
"""
import math
from datetime import datetime

FEATURE_COLS = [
    # ── Core (original 15 — fixed order) ────────────────────────────────────
    "confidence",       # overall confidence 1-10
    "tech_score",       # technical layer 1-10
    "fund_score",       # fundamental layer 1-10
    "sent_score",       # sentiment layer 1-10
    "pos_score",        # positioning layer 1-10
    "macro_score",      # macro layer 1-10
    "rsi14",            # RSI value 0-100 (50 = neutral when unavailable)
    "macd_signal",      # +1=bullish  0=flat  -1=bearish
    "atr_pct",          # ATR as percentage of price (normalised volatility)
    "reward_risk",      # R:R ratio from analysis
    "direction_buy",    # 1=BUY  0=SELL
    "mtf_count",        # agreeing core MTF timeframes 0-3 (weekly/daily/4H)
    "ribbon_aligned",   # 1=MA ribbon fully aligned with trade direction
    "month_sin",        # sin(2π·month/12) — seasonal cycle encoding
    "month_cos",        # cos(2π·month/12)
    # ── Extended features (new — appended after original 15) ────────────────
    "grade_num",            # trade quality: A=5, B=4, C=3, D=2, F=1, 0=unknown
    "rsi_extreme",          # +1=extreme aligned with trade, -1=extreme opposed, 0=neutral
    "bb_pos_num",           # Bollinger position: upper=1, inside=0, lower=-1
    "above_200ma",          # 1=above, -1=below, 0=unknown
    "dxy_direction_num",    # DXY trend: rising=1, flat=0, falling=-1
    "market_regime_num",    # trending_risk_on=2, ranging_low_vol=0, ranging_high_vol=1,
                            #                     trending_risk_off=-2
    "patience_score",       # patience score at entry (0-10)
    "day_of_week",          # 1=Monday … 5=Friday
    "hour_sin",             # sin(2π·hour/24) — intraday cycle encoding
    "hour_cos",             # cos(2π·hour/24)
    "corr_agreement_count", # how many correlated-base pairs agreed (0-5)
    "atr_percentile_6m",    # current ATR vs 6-month average (>1 = expanding)
    "consecutive_weeks_trending",  # weeks current weekly trend has held
    "cot_momentum_num",     # COT signal: bullish=1, neutral=0, bearish=-1
    "fund_alignment_num",   # TAILWIND=1, MIXED=0, HEADWIND=-1
    "fund_aligned_count",   # 0-3 fundamental factors aligned
    "rr_over_2",            # 1 if reward_risk > 2.0
    "high_conf",            # 1 if confidence >= 8
    "ribbon_state_num",     # ALIGNED_BULL=2, PARTIAL_BULL=1, NEUTRAL=0,
                            #  PARTIAL_BEAR=-1, ALIGNED_BEAR=-2
    "divergence_present",   # 1=has divergence pattern, 0=none
    "hour_auckland",        # raw Auckland hour 0-23 (useful for tree-based models)
    "vix_level",            # VIX value (0=unknown)
    "is_ranging",           # 1 if market_regime contains 'ranging'
    "fund_tail_high",       # 1 if fund_aligned_count >= 3 AND TAILWIND
    "mtf_full_agree",       # 1 if all 3 core MTF timeframes agree
]


def _safe(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        return default if (f != f) else f      # guard NaN
    except (TypeError, ValueError):
        return default


def _encode_grade(grade: str) -> float:
    return {"A": 5.0, "B": 4.0, "C": 3.0, "D": 2.0, "F": 1.0}.get(
        (grade or "").upper(), 0.0
    )


def _encode_ribbon(ribbon_state: str) -> float:
    return {
        "ALIGNED_BULL": 2.0, "PARTIAL_BULL": 1.0, "NEUTRAL": 0.0,
        "PARTIAL_BEAR": -1.0, "ALIGNED_BEAR": -2.0,
    }.get((ribbon_state or "").upper(), 0.0)


def _encode_market_regime(regime: str) -> float:
    return {
        "trending_risk_on":  2.0,
        "ranging_high_vol":  1.0,
        "ranging_low_vol":   0.0,
        "trending_risk_off": -2.0,
    }.get((regime or "").lower(), 0.0)


def _encode_dxy(dxy: str) -> float:
    s = (dxy or "").lower()
    if s == "rising":
        return 1.0
    if s == "falling":
        return -1.0
    return 0.0


def _encode_bb(bb: str) -> float:
    s = (bb or "").lower()
    if s == "upper":
        return 1.0
    if s == "lower":
        return -1.0
    return 0.0


def _encode_cot(cot: str) -> float:
    s = (cot or "").lower()
    if "bull" in s or "long" in s:
        return 1.0
    if "bear" in s or "short" in s:
        return -1.0
    return 0.0


def _encode_fund_align(alignment: str) -> float:
    s = (alignment or "").upper()
    if s == "TAILWIND":
        return 1.0
    if s == "HEADWIND":
        return -1.0
    return 0.0


def extract(pair: str, parsed: dict, bundle: dict,
            entry_time: datetime = None,
            extra_data: dict = None) -> dict:
    """Return feature dict for one trade analysis. Missing values → neutral defaults.

    pair        — e.g. "EUR/USD"
    parsed      — dict from recparse.parse() or equivalent
    bundle      — full analysis bundle (technical, mtf, fundamental, …)
    entry_time  — datetime the analysis ran (defaults to now)
    extra_data  — optional dict with extended context fields captured at log time
                  (grade, ribbon_state, cot_momentum, fundamental_alignment, etc.)
    """
    if entry_time is None:
        entry_time = datetime.now()
    if extra_data is None:
        extra_data = {}

    tech  = bundle.get("technical", {}) if isinstance(bundle, dict) else {}
    daily = tech.get("daily", {})        if isinstance(tech, dict)   else {}
    mtf   = bundle.get("mtf", {})        if isinstance(bundle, dict) else {}

    direction = (parsed.get("direction") or "BUY").upper()

    # ── Original 15 features ─────────────────────────────────────────────────
    confidence  = _safe(parsed.get("confidence"),       5.0)
    tech_score  = _safe(parsed.get("technical_score"),  5.0)
    fund_score  = _safe(parsed.get("fundamental_score"),5.0)
    sent_score  = _safe(parsed.get("sentiment_score"),  5.0)
    pos_score   = _safe(parsed.get("positioning_score"),5.0)
    macro_score = _safe(parsed.get("macro_score"),      5.0)

    rsi14  = _safe(daily.get("rsi14"), 50.0)
    macd_h = _safe(daily.get("macd_hist"), 0.0)
    macd_signal = 1.0 if macd_h > 0 else (-1.0 if macd_h < 0 else 0.0)

    price   = _safe(daily.get("last_close"), 1.0) or 1.0
    atr     = _safe(daily.get("atr14"), 0.0)
    atr_pct = (atr / price) * 100.0

    reward_risk   = _safe(parsed.get("reward_risk"), 1.5)
    direction_buy = 1.0 if direction == "BUY" else 0.0

    mtf_count = _safe(
        mtf.get("agreeing_count") if isinstance(mtf, dict) else None, 0.0
    )

    rib        = daily.get("ribbon", {}) if isinstance(daily, dict) else {}
    rib_status = rib.get("status", "NEUTRAL") if isinstance(rib, dict) else "NEUTRAL"
    ribbon_aligned = 1.0 if (
        (direction == "BUY"  and rib_status == "ALIGNED_BULL") or
        (direction == "SELL" and rib_status == "ALIGNED_BEAR")
    ) else 0.0

    month     = entry_time.month
    month_sin = math.sin(2 * math.pi * month / 12)
    month_cos = math.cos(2 * math.pi * month / 12)

    # ── Extended features ────────────────────────────────────────────────────
    grade_str   = extra_data.get("grade", "")
    grade_num   = _encode_grade(grade_str)

    # RSI extreme: +1 if RSI is in an extreme that SUPPORTS the trade direction
    # (RSI < 35 supports BUY as oversold; RSI > 65 supports SELL as overbought)
    if direction == "BUY":
        rsi_extreme = 1.0 if rsi14 < 35 else (-1.0 if rsi14 > 70 else 0.0)
    else:
        rsi_extreme = 1.0 if rsi14 > 65 else (-1.0 if rsi14 < 30 else 0.0)

    bb_pos_num = _encode_bb(extra_data.get("bb_position", ""))

    # price_vs_200ma: "above" → 1, "below" → -1, else 0
    pv200 = (extra_data.get("price_vs_200ma") or "").lower()
    above_200ma = 1.0 if pv200 == "above" else (-1.0 if pv200 == "below" else 0.0)

    dxy_direction_num  = _encode_dxy(extra_data.get("dxy_direction", ""))
    market_regime_num  = _encode_market_regime(extra_data.get("market_regime", ""))
    patience_score     = _safe(extra_data.get("patience_score_at_entry"), 0.0)

    day_of_week = _safe(extra_data.get("day_of_week"), float(entry_time.isoweekday()))
    hour_ak     = _safe(extra_data.get("hour_auckland"), float(entry_time.hour))
    hour_sin    = math.sin(2 * math.pi * hour_ak / 24)
    hour_cos    = math.cos(2 * math.pi * hour_ak / 24)

    corr_agreement_count = _safe(extra_data.get("corr_agreement_count"), 0.0)
    atr_percentile_6m    = _safe(extra_data.get("atr_percentile_6m"), 1.0)
    consecutive_weeks    = _safe(extra_data.get("consecutive_weeks_trending"), 0.0)

    cot_str        = extra_data.get("cot_momentum", "")
    cot_momentum_num = _encode_cot(cot_str)

    fa_str          = extra_data.get("fundamental_alignment", "")
    fund_alignment_num = _encode_fund_align(fa_str)
    fund_aligned_count = _safe(extra_data.get("fund_aligned_count"), 0.0)

    rr_over_2 = 1.0 if reward_risk > 2.0 else 0.0
    high_conf  = 1.0 if confidence >= 8.0 else 0.0

    ribbon_state_num = _encode_ribbon(
        extra_data.get("ribbon_state") or rib_status
    )

    div_type = (extra_data.get("divergence_type") or "").strip()
    divergence_present = 1.0 if div_type and div_type.lower() != "none" else 0.0

    hour_auckland = hour_ak
    vix_level     = _safe(extra_data.get("vix_at_entry"), 0.0)
    regime_str    = (extra_data.get("market_regime") or "").lower()
    is_ranging    = 1.0 if "ranging" in regime_str else 0.0
    fund_tail_high = 1.0 if (fund_aligned_count >= 3 and fa_str.upper() == "TAILWIND") else 0.0
    mtf_full_agree = 1.0 if mtf_count >= 3 else 0.0

    return {
        "confidence":              round(confidence,   2),
        "tech_score":              round(tech_score,   2),
        "fund_score":              round(fund_score,   2),
        "sent_score":              round(sent_score,   2),
        "pos_score":               round(pos_score,    2),
        "macro_score":             round(macro_score,  2),
        "rsi14":                   round(rsi14,        2),
        "macd_signal":             macd_signal,
        "atr_pct":                 round(atr_pct,      4),
        "reward_risk":             round(reward_risk,  3),
        "direction_buy":           direction_buy,
        "mtf_count":               mtf_count,
        "ribbon_aligned":          ribbon_aligned,
        "month_sin":               round(month_sin,    4),
        "month_cos":               round(month_cos,    4),
        # extended
        "grade_num":               grade_num,
        "rsi_extreme":             rsi_extreme,
        "bb_pos_num":              bb_pos_num,
        "above_200ma":             above_200ma,
        "dxy_direction_num":       dxy_direction_num,
        "market_regime_num":       market_regime_num,
        "patience_score":          round(patience_score,   2),
        "day_of_week":             day_of_week,
        "hour_sin":                round(hour_sin,     4),
        "hour_cos":                round(hour_cos,     4),
        "corr_agreement_count":    corr_agreement_count,
        "atr_percentile_6m":       round(atr_percentile_6m, 3),
        "consecutive_weeks_trending": consecutive_weeks,
        "cot_momentum_num":        cot_momentum_num,
        "fund_alignment_num":      fund_alignment_num,
        "fund_aligned_count":      fund_aligned_count,
        "rr_over_2":               rr_over_2,
        "high_conf":               high_conf,
        "ribbon_state_num":        ribbon_state_num,
        "divergence_present":      divergence_present,
        "hour_auckland":           hour_auckland,
        "vix_level":               vix_level,
        "is_ranging":              is_ranging,
        "fund_tail_high":          fund_tail_high,
        "mtf_full_agree":          mtf_full_agree,
    }
