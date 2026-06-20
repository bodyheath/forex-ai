"""Extract ML feature vectors from a trade analysis at the moment of logging.

These 55 features capture the key technical, fundamental, contextual, and
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
    # ── Entry context features (candle quality, price context, intermarket) ──
    "entry_candle_type_num",    # PIN_BAR=3, ENGULFING=2, INSIDE_BAR=1, NORMAL=0
    "entry_candle_body_ratio",  # body/total_range 0.0-1.0 (high = strong candle)
    "entry_candle_vs_avg",      # entry range / 10-period avg (>1.5 = large candle)
    "dist_weekly_open_pips",    # pips from Monday open (large = exhaustion risk)
    "dist_round_number_pips",   # pips from nearest 00/50 level (low = high probability)
    "new_20d_extreme",          # 1 if new 20d high/low in last 3 candles, else 0
    "inside_bars_before",       # consecutive inside bars before entry (compression)
    "cot_weeks_in_dir",         # weeks COT has been positioned in trade direction
    "cot_accel",                # COT acceleration: 1=building, -1=fading, 0=stable
    "us10y_dir",                # US 10Y yield: 1=rising, -1=falling, 0=flat
    "gold_dir",                 # Gold: 1=rising, -1=falling, 0=flat
    "sp500_dir",                # SP500: 1=up, -1=down, 0=flat
    "vix_vs_20d",               # VIX vs 20d avg: 1=elevated, -1=suppressed, 0=flat
    "atr_5d_vs_20d",            # 5d ATR / 20d ATR (>1.0 = expanding volatility)
    "atr_expanding",            # 1 if ATR increasing 3+ consecutive days, else 0
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


# ── OHLCV helpers (no API calls — reads from warm_cache only) ─────────────────

def _pip_size_fe(pair: str) -> float:
    """Return pip size for pip-distance calculations."""
    cleaned = (pair or "").upper().replace("/", "")
    if len(cleaned) >= 6:
        base, quote = cleaned[:3], cleaned[3:6]
        if quote == "JPY":
            return 0.01
        if base == "JPY":
            return 0.000001
    if "JPY" in (pair or "").upper():
        return 0.01
    return 0.0001


def _get_ohlcv_vals(pair: str, interval: str, size: int) -> list:
    """Return OHLCV values list (newest-first) from cache, or []."""
    try:
        from src import cache as _ca
        raw = _ca.get(f"TD:{pair}:{interval}:{size}")
        if isinstance(raw, dict):
            return raw.get("values") or []
    except Exception:
        pass
    return []


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ── Candle quality analysis ────────────────────────────────────────────────────

def _classify_candle(vals: list, direction: str) -> str:
    """Classify the most recent candle. vals[0]=most recent, vals[1]=previous."""
    if len(vals) < 2:
        return "NORMAL"
    try:
        c0 = vals[0]
        c1 = vals[1]
        o = _safe_float(c0.get("open"))
        h = _safe_float(c0.get("high"))
        l = _safe_float(c0.get("low"))
        c = _safe_float(c0.get("close"))
        po = _safe_float(c1.get("open"))
        ph = _safe_float(c1.get("high"))
        pl = _safe_float(c1.get("low"))
        pc = _safe_float(c1.get("close"))

        rng = h - l
        if rng <= 0:
            return "NORMAL"
        body     = abs(c - o)
        prev_body = abs(pc - po)

        # Inside bar: current fully inside previous
        if h <= ph and l >= pl:
            return "INSIDE_BAR"

        # Engulfing: current body > previous body AND engulfs it
        if body > prev_body:
            if c > o and o <= pc and c >= po:
                return "ENGULFING_BULL"
            if c < o and o >= pc and c <= po:
                return "ENGULFING_BEAR"

        # Pin bar: small body (<30% of range), long wick on one side
        if body / rng < 0.30:
            upper_wick = h - max(o, c)
            lower_wick = min(o, c) - l
            if lower_wick > 2 * body or upper_wick > 2 * body:
                return "PIN_BAR"

    except Exception:
        pass
    return "NORMAL"


def _body_ratio(vals: list) -> float:
    """Entry candle body / total range (0.0–1.0)."""
    if not vals:
        return 0.5
    try:
        c0  = vals[0]
        o   = _safe_float(c0.get("open"))
        h   = _safe_float(c0.get("high"))
        l   = _safe_float(c0.get("low"))
        c   = _safe_float(c0.get("close"))
        rng = h - l
        return round(abs(c - o) / rng, 3) if rng > 0 else 0.5
    except Exception:
        return 0.5


def _candle_vs_avg_size(vals: list, n: int = 10) -> float:
    """Entry candle range / average range of prior n candles."""
    if len(vals) < 2:
        return 1.0
    try:
        c0  = vals[0]
        rng = _safe_float(c0.get("high")) - _safe_float(c0.get("low"))
        prior_ranges = []
        for v in vals[1: n + 1]:
            r = _safe_float(v.get("high")) - _safe_float(v.get("low"))
            if r > 0:
                prior_ranges.append(r)
        if not prior_ranges:
            return 1.0
        avg = sum(prior_ranges) / len(prior_ranges)
        return round(rng / avg, 3) if avg > 0 else 1.0
    except Exception:
        return 1.0


def _inside_bars_count(vals: list) -> int:
    """Count consecutive inside bars IMMEDIATELY preceding the entry candle."""
    count = 0
    for i in range(1, min(6, len(vals) - 1)):
        try:
            ch = _safe_float(vals[i].get("high"))
            cl = _safe_float(vals[i].get("low"))
            ph = _safe_float(vals[i + 1].get("high"))
            pl = _safe_float(vals[i + 1].get("low"))
            if ch <= ph and cl >= pl:
                count += 1
            else:
                break
        except Exception:
            break
    return count


# ── Price context analysis ─────────────────────────────────────────────────────

def _dist_from_weekly_open_pips(pair: str, price: float) -> float:
    """Pips moved from most recent weekly open (Monday open)."""
    try:
        weekly_vals = _get_ohlcv_vals(pair, "1week", 200)
        if not weekly_vals:
            return 0.0
        weekly_open = _safe_float(weekly_vals[0].get("open"))
        ps = _pip_size_fe(pair)
        return round((price - weekly_open) / ps, 1) if ps > 0 else 0.0
    except Exception:
        return 0.0


def _dist_from_round_number_pips(pair: str, price: float) -> float:
    """Pips from the nearest 50-pip round-number level."""
    try:
        ps  = _pip_size_fe(pair)
        gap = 50 * ps   # 50-pip interval in price terms
        if gap <= 0:
            return 0.0
        nearest = round(price / gap) * gap
        return round(abs(price - nearest) / ps, 1)
    except Exception:
        return 0.0


def _new_20d_extreme(pair: str) -> bool:
    """True if pair made a new 20-day high or low in the last 3 daily candles."""
    try:
        vals = _get_ohlcv_vals(pair, "1day", 400)
        if len(vals) < 23:
            return False
        recent   = vals[:3]
        lookback = vals[3:23]
        max_recent_h = max(_safe_float(v.get("high")) for v in recent)
        min_recent_l = min(_safe_float(v.get("low"),  999999) for v in recent)
        max_lb_h     = max(_safe_float(v.get("high")) for v in lookback)
        min_lb_l     = min(_safe_float(v.get("low"),  999999) for v in lookback)
        return max_recent_h > max_lb_h or min_recent_l < min_lb_l
    except Exception:
        return False


# ── COT context analysis ───────────────────────────────────────────────────────

def _cot_context(pair: str, direction: str) -> tuple:
    """Return (weeks_in_direction: int, accelerating: int) for the base currency.

    weeks_in_direction — consecutive weeks COT has been net long (BUY) or net short (SELL).
    accelerating — +1 if most recent delta bigger than prior delta in trade direction,
                   -1 if fading, 0 if stable.
    """
    try:
        import config as _cfg
        from src import cache as _ca

        cleaned  = (pair or "").upper().replace("/", "")
        base     = cleaned[:3]
        meta     = _cfg.CURRENCIES.get(base, {})
        market   = meta.get("cot_market")
        if not market:
            return 0, 0

        series = _ca.get(f"COT:exact:{market}", ttl_hours=12.0)
        if not isinstance(series, list) or not series:
            return 0, 0

        # Compute net speculator positions (long - short)
        nets = []
        for row in series[:12]:
            try:
                nets.append(
                    float(row.get("noncomm_positions_long_all", 0)) -
                    float(row.get("noncomm_positions_short_all", 0))
                )
            except (TypeError, ValueError):
                pass

        if not nets:
            return 0, 0

        is_buy      = direction.upper() == "BUY"
        target_sign = 1 if is_buy else -1
        current_sign = 1 if nets[0] > 0 else -1

        # Consecutive weeks in trade direction
        if current_sign != target_sign:
            weeks = 0
        else:
            weeks = 1
            for i in range(1, len(nets)):
                s = 1 if nets[i] > 0 else -1
                if s == target_sign:
                    weeks += 1
                else:
                    break

        # Acceleration: is latest delta larger than prior delta?
        accel = 0
        if len(nets) >= 3:
            d_recent = nets[0] - nets[1]
            d_prev   = nets[1] - nets[2]
            if abs(d_recent) > abs(d_prev) * 1.2:
                accel = 1 if (d_recent * target_sign > 0) else -1

        return weeks, accel
    except Exception:
        return 0, 0


# ── Intermarket analysis ───────────────────────────────────────────────────────

def _trend_from_closes(vals: list, n: int = 5) -> str:
    """RISING / FALLING / FLAT from the last n*2 close prices."""
    if len(vals) < n + 1:
        return "FLAT"
    try:
        recent_avg = sum(_safe_float(v.get("close")) for v in vals[:n]) / n
        older_avg  = sum(_safe_float(v.get("close")) for v in vals[n: n * 2]) / n
        if older_avg <= 0:
            return "FLAT"
        if recent_avg > older_avg * 1.003:
            return "RISING"
        if recent_avg < older_avg * 0.997:
            return "FALLING"
        return "FLAT"
    except Exception:
        return "FLAT"


def _macro_trend(bundle: dict, signal_key: str) -> str:
    """Extract RISING/FALLING/FLAT from the macro bundle for a FRED signal."""
    try:
        macro   = bundle.get("macro") or {}
        signals = macro.get("signals") or {} if isinstance(macro, dict) else {}
        sig     = signals.get(signal_key) or {}
        trend   = (sig.get("trend") or "").lower()
        if "rising" in trend or "increasing" in trend:
            return "RISING"
        if "falling" in trend or "decreasing" in trend:
            return "FALLING"
        return "FLAT"
    except Exception:
        return "FLAT"


def _gold_direction() -> str:
    """Best-effort gold direction from Twelve Data cache (FLAT if not cached)."""
    try:
        from src import cache as _ca
        for sym in ("XAU/USD", "XAUUSD"):
            raw = _ca.get(f"TD:{sym}:1day:400")
            if isinstance(raw, dict) and raw.get("values"):
                return _trend_from_closes(raw["values"])
    except Exception:
        pass
    return "FLAT"


def _sp500_direction() -> str:
    """Best-effort S&P 500 direction from Twelve Data cache (FLAT if not cached)."""
    try:
        from src import cache as _ca
        for sym in ("SPY", "SPX", "US500", "SP500"):
            raw = _ca.get(f"TD:{sym}:1day:400")
            if isinstance(raw, dict) and raw.get("values"):
                return _trend_from_closes(raw["values"])
    except Exception:
        pass
    return "FLAT"


def _vix_vs_20d(bundle: dict, vix_level: float) -> int:
    """1 if VIX above 20-day average, -1 below, 0 unknown."""
    try:
        # Try the macro bundle trend as a proxy
        trend = _macro_trend(bundle, "VIX (volatility index)")
        if trend == "RISING":
            return 1
        if trend == "FALLING":
            return -1
        # Absolute thresholds as a secondary check
        if vix_level > 20:
            return 1
        if vix_level > 0 and vix_level < 14:
            return -1
    except Exception:
        pass
    return 0


# ── Volatility context analysis ────────────────────────────────────────────────

def _volatility_context(pair: str) -> dict:
    """Compute 5d vs 20d ATR ratio and whether ATR is expanding (3+ days)."""
    try:
        vals = _get_ohlcv_vals(pair, "1day", 400)
        if len(vals) < 21:
            return {"atr_5d_vs_20d": 1.0, "atr_expanding": 0, "atr_percentile_6m": 1.0}

        # True range (high - low as proxy ATR)
        ranges = []
        for v in vals[:130]:
            r = _safe_float(v.get("high")) - _safe_float(v.get("low"))
            if r > 0:
                ranges.append(r)

        if len(ranges) < 20:
            return {"atr_5d_vs_20d": 1.0, "atr_expanding": 0, "atr_percentile_6m": 1.0}

        atr_5d  = sum(ranges[:5])  / 5
        atr_20d = sum(ranges[:20]) / 20
        ratio_5_20 = round(atr_5d / atr_20d, 3) if atr_20d > 0 else 1.0

        # ATR expanding: last 3 daily ranges each larger than the previous
        expanding = 1 if (len(ranges) >= 4 and ranges[0] > ranges[1] > ranges[2]) else 0

        # 6-month percentile: 5d ATR vs 120-day average
        atr_6m = sum(ranges[:120]) / min(120, len(ranges))
        percentile_6m = round(atr_5d / atr_6m, 3) if atr_6m > 0 else 1.0

        return {
            "atr_5d_vs_20d":       ratio_5_20,
            "atr_expanding":       expanding,
            "atr_percentile_6m":   percentile_6m,
        }
    except Exception:
        return {"atr_5d_vs_20d": 1.0, "atr_expanding": 0, "atr_percentile_6m": 1.0}


# ── Public context computation (called from daily.py _log_one_research) ────────

def compute_entry_context(pair: str, bundle: dict, direction: str) -> dict:
    """Compute all new OHLCV-based entry-context fields at trade open time.

    Returns a dict of raw string/number values ready to merge into extra_fields
    for research_trades.csv logging.  Uses only cached data — zero new API calls.
    """
    if not isinstance(bundle, dict):
        bundle = {}

    # Current price from technical bundle
    tech  = bundle.get("technical") or {}
    daily = tech.get("daily") or {}   if isinstance(tech, dict) else {}
    price = _safe_float(daily.get("last_close"), 0.0)

    # 4H OHLCV for candle quality
    vals_4h = _get_ohlcv_vals(pair, "4h", 500)

    # Candle quality
    candle_type  = _classify_candle(vals_4h, direction)
    body_ratio   = _body_ratio(vals_4h)
    candle_ratio = _candle_vs_avg_size(vals_4h)
    inside_bars  = _inside_bars_count(vals_4h)

    # Price context (falls back to 0 when no cache)
    dist_weekly   = _dist_from_weekly_open_pips(pair, price) if price > 0 else 0.0
    dist_round    = _dist_from_round_number_pips(pair, price) if price > 0 else 0.0
    is_extreme    = _new_20d_extreme(pair)

    # COT context
    cot_weeks, cot_accel = _cot_context(pair, direction)

    # Intermarket (from macro bundle FRED signals + cache best-effort)
    vix_level    = _safe_float(daily.get("vix_level") or extra_vix_from_bundle(bundle), 0.0)
    us10y_dir    = _macro_trend(bundle, "US 10Y Treasury yield (%)")
    gold_dir     = _gold_direction()
    sp500_dir    = _sp500_direction()
    vix_relative = _vix_vs_20d(bundle, vix_level)

    # Volatility context
    vol_ctx = _volatility_context(pair)

    return {
        "entry_candle_type":       candle_type,
        "entry_candle_body_ratio": body_ratio,
        "entry_candle_vs_avg":     candle_ratio,
        "dist_weekly_open_pips":   dist_weekly,
        "dist_round_number_pips":  dist_round,
        "new_20d_extreme":         "true" if is_extreme else "false",
        "inside_bars_before":      inside_bars,
        "cot_weeks_in_direction":  cot_weeks,
        "cot_accelerating":        cot_accel,
        "us10y_direction":         us10y_dir,
        "gold_direction":          gold_dir,
        "sp500_direction":         sp500_dir,
        "vix_vs_20d_avg":          vix_relative,
        "atr_5d_vs_20d":           vol_ctx["atr_5d_vs_20d"],
        "atr_expanding":           vol_ctx["atr_expanding"],
        # Also update the existing atr_percentile_6m field with better data
        "atr_percentile_6m":       vol_ctx["atr_percentile_6m"],
    }


def extra_vix_from_bundle(bundle: dict) -> float:
    """Extract VIX level from macro signals."""
    try:
        macro   = bundle.get("macro") or {}
        signals = macro.get("signals") or {} if isinstance(macro, dict) else {}
        vix_sig = signals.get("VIX (volatility index)") or {}
        return _safe_float(vix_sig.get("value"), 0.0)
    except Exception:
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

    if direction == "BUY":
        rsi_extreme = 1.0 if rsi14 < 35 else (-1.0 if rsi14 > 70 else 0.0)
    else:
        rsi_extreme = 1.0 if rsi14 > 65 else (-1.0 if rsi14 < 30 else 0.0)

    bb_pos_num = _encode_bb(extra_data.get("bb_position", ""))

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

    cot_str          = extra_data.get("cot_momentum", "")
    cot_momentum_num = _encode_cot(cot_str)

    fa_str             = extra_data.get("fundamental_alignment", "")
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
    vix_level_val = _safe(extra_data.get("vix_at_entry"), 0.0)
    regime_str    = (extra_data.get("market_regime") or "").lower()
    is_ranging    = 1.0 if "ranging" in regime_str else 0.0
    fund_tail_high = 1.0 if (fund_aligned_count >= 3 and fa_str.upper() == "TAILWIND") else 0.0
    mtf_full_agree = 1.0 if mtf_count >= 3 else 0.0

    # ── New entry-context features (encoded from extra_data) ─────────────────
    candle_type_str = (extra_data.get("entry_candle_type") or "").upper()
    entry_candle_type_num = {
        "PIN_BAR":        3.0,
        "ENGULFING_BULL": 2.0,
        "ENGULFING_BEAR": 2.0,
        "INSIDE_BAR":     1.0,
        "NORMAL":         0.0,
    }.get(candle_type_str, 0.0)

    entry_candle_body_ratio = _safe(extra_data.get("entry_candle_body_ratio"), 0.5)
    entry_candle_vs_avg     = _safe(extra_data.get("entry_candle_vs_avg"), 1.0)
    dist_weekly_open_pips   = _safe(extra_data.get("dist_weekly_open_pips"), 0.0)
    dist_round_number_pips  = _safe(extra_data.get("dist_round_number_pips"), 25.0)
    new_20d_extreme_f       = 1.0 if str(extra_data.get("new_20d_extreme", "")).lower() == "true" else 0.0
    inside_bars_before      = _safe(extra_data.get("inside_bars_before"), 0.0)
    cot_weeks_in_dir        = _safe(extra_data.get("cot_weeks_in_direction"), 0.0)
    cot_accel_f             = _safe(extra_data.get("cot_accelerating"), 0.0)

    us10y_dir_str = (extra_data.get("us10y_direction") or "").upper()
    us10y_dir     = {"RISING": 1.0, "FALLING": -1.0}.get(us10y_dir_str, 0.0)

    gold_dir_str = (extra_data.get("gold_direction") or "").upper()
    gold_dir     = {"RISING": 1.0, "FALLING": -1.0}.get(gold_dir_str, 0.0)

    sp500_dir_str = (extra_data.get("sp500_direction") or "").upper()
    sp500_dir     = {"UP": 1.0, "RISING": 1.0, "DOWN": -1.0, "FALLING": -1.0}.get(sp500_dir_str, 0.0)

    vix_vs_20d_f   = _safe(extra_data.get("vix_vs_20d_avg"), 0.0)
    atr_5d_vs_20d  = _safe(extra_data.get("atr_5d_vs_20d"), 1.0)
    atr_expanding  = _safe(extra_data.get("atr_expanding"), 0.0)

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
        "vix_level":               vix_level_val,
        "is_ranging":              is_ranging,
        "fund_tail_high":          fund_tail_high,
        "mtf_full_agree":          mtf_full_agree,
        # new entry context
        "entry_candle_type_num":   entry_candle_type_num,
        "entry_candle_body_ratio": round(entry_candle_body_ratio, 3),
        "entry_candle_vs_avg":     round(entry_candle_vs_avg,     3),
        "dist_weekly_open_pips":   round(dist_weekly_open_pips,   1),
        "dist_round_number_pips":  round(dist_round_number_pips,  1),
        "new_20d_extreme":         new_20d_extreme_f,
        "inside_bars_before":      inside_bars_before,
        "cot_weeks_in_dir":        cot_weeks_in_dir,
        "cot_accel":               cot_accel_f,
        "us10y_dir":               us10y_dir,
        "gold_dir":                gold_dir,
        "sp500_dir":               sp500_dir,
        "vix_vs_20d":              vix_vs_20d_f,
        "atr_5d_vs_20d":           round(atr_5d_vs_20d, 3),
        "atr_expanding":           atr_expanding,
    }
