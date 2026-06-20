"""Global market regime detector — single source of truth for trading conditions.

Classifies the current macro environment into one of four regimes using VIX,
bond yields, gold (XAU/USD), the yield curve, S&P 500 vs 50-day MA, and currency
strength confirmation.  Every regime carries its own confidence threshold and
conditions-score cap so the trading conditions score is always consistent with the
regime — no more contradictions.

Regimes
-------
  trending_risk_on   — low VIX, risk appetite; favour AUD/NZD/CAD; threshold 5.5
  trending_risk_off  — elevated VIX, flight to safety; favour JPY/CHF/USD; threshold 6
  ranging_low_vol    — calm but directionless; mean-reversion; threshold 7; cap 6/10
  ranging_high_vol   — chaotic; position sizes −50%; threshold 7.5; cap 4/10

Public API
----------
detect(macro_signals=None, ccy_strength=None) -> dict
    Compute and return the global regime.  Caches result for 2 hours.

regime_currency_bonus(regime, base, quote) -> float
    Pair-selection score bonus (0–8 pts) for alignment with the regime.

regime_display_lines(regime_data) -> list[str]
    Plain-English Telegram lines for the 6am message (regime + system message).

conditions_telegram_line(regime_data, conditions_score) -> str
    Single-line conditions summary for intraday scans.

telegram_line(regime_data) -> str
    Legacy single-line regime label (backward-compat).
"""

import json
import pathlib
import requests

import config
from src import cache

_CACHE_KEY = "REGIME:global"
_CACHE_TTL  = 2.0   # hours

_REGIME_STATE_FILE = pathlib.Path("data/regime_state.json")

# ── Currency classifications ───────────────────────────────────────────────────
_RISK_ON_CCYS  = {"AUD", "NZD", "CAD", "GBP", "EUR"}
_RISK_OFF_CCYS = {"JPY", "CHF", "USD"}

# ── Regime metadata ────────────────────────────────────────────────────────────
REGIMES = {
    "trending_risk_on": {
        "label":           "Trending — Risk On",
        "display_label":   "Trending Risk On",
        "emoji":           "🟢",
        "description":     (
            "investors are confident and taking risks. "
            "Currencies that pay higher interest rates like AUD and NZD "
            "tend to do well in this environment."
        ),
        "favour_ccys":     {"AUD", "NZD", "CAD"},
        "avoid_ccys":      {"JPY", "CHF"},
        # Threshold and conditions rules — regime is the single source of truth
        "conf_threshold":  5.5,   # lower bar — more opportunities in calm markets
        "conditions_cap":  9,     # conditions can reach 9/10 in ideal risk-on
        "threshold_reason": "risk-on environment — threshold lowered to capture opportunities",
        "system_message":   (
            "The system is running with a lower confidence threshold today — "
            "more opportunities may be flagged in risk-on conditions."
        ),
        # Legacy fields
        "conf_override":   None,
        "size_mult":       1.0,
    },
    "trending_risk_off": {
        "label":           "Trending — Risk Off",
        "display_label":   "Trending Risk Off",
        "emoji":           "🔴",
        "description":     (
            "investors are cautious and moving to safe havens. "
            "JPY, CHF and USD tend to strengthen in this environment."
        ),
        "favour_ccys":     {"JPY", "CHF", "USD"},
        "avoid_ccys":      {"AUD", "NZD"},
        "conf_threshold":  6.0,   # standard bar — be selective
        "conditions_cap":  6,     # max 6/10 — markets are stressed
        "threshold_reason": "risk-off environment — standard threshold maintained",
        "system_message":   (
            "The system is selective today — "
            "safe haven and counter-trend setups are favoured — "
            "AUD and NZD signals should be treated with extra caution."
        ),
        "conf_override":   None,
        "size_mult":       1.0,
    },
    "ranging_low_vol": {
        "label":           "Ranging — Low Volatility",
        "display_label":   "Ranging Low Volatility",
        "emoji":           "⚖️",
        "description":     (
            "markets are calm but directionless. "
            "Mean-reversion setups tend to work better than trend-following right now."
        ),
        "favour_ccys":     set(),
        "avoid_ccys":      set(),
        "conf_threshold":  7.0,   # higher bar — directional trades need conviction
        "conditions_cap":  6,     # max 6/10 — no clear direction
        "threshold_reason": "ranging market — higher bar required for directional trades",
        "system_message":   (
            "The system is being more selective than usual — "
            "only the cleanest setups will be recommended today."
        ),
        "conf_override":   None,
        "size_mult":       1.0,
    },
    "ranging_high_vol": {
        "label":           "Ranging — High Volatility",
        "display_label":   "Ranging High Volatility",
        "emoji":           "⚠️",
        "description":     (
            "markets are chaotic with large unpredictable swings. "
            "Position sizes are automatically reduced by 50% and only "
            "the highest conviction trades are taken."
        ),
        "favour_ccys":     set(),
        "avoid_ccys":      set(),
        "conf_threshold":  7.5,   # only highest conviction trades
        "conditions_cap":  4,     # max 4/10 — chaotic conditions
        "threshold_reason": "high volatility — only confidence 7.5+ trades taken, position sizes halved",
        "system_message":   (
            "High volatility mode active — "
            "position sizes reduced by 50% — "
            "only confidence 7.5+ setups will be shown today."
        ),
        "conf_override":   8,     # legacy field kept for backward-compat
        "size_mult":       0.5,
    },
}

_DEFAULT_REGIME = "ranging_low_vol"


# ── Gold via Twelve Data ───────────────────────────────────────────────────────

def _fetch_gold_change_pct():
    """5-day XAU/USD % change from Twelve Data.  Returns None on any failure."""
    if not config.TWELVE_DATA_KEY:
        return None
    cached = cache.get("REGIME:gold_5d", ttl_hours=3.0)
    if cached is not None:
        return cached
    try:
        resp = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol":     "XAU/USD",
                "interval":   "1day",
                "outputsize": 6,
                "apikey":     config.TWELVE_DATA_KEY,
            },
            timeout=12,
        )
        values = resp.json().get("values", [])
        if len(values) < 2:
            return None
        current = float(values[0]["close"])
        prev    = float(values[-1]["close"])
        pct     = round((current - prev) / prev * 100, 2) if prev else None
        cache.set("REGIME:gold_5d", pct)
        return pct
    except Exception:
        return None


def _fetch_spx_above_50d():
    """Return True if SPX is above its 50-day MA, False if below, None on failure."""
    if not config.TWELVE_DATA_KEY:
        return None
    cached = cache.get("REGIME:spx_50d", ttl_hours=3.0)
    if cached is not None:
        return cached
    try:
        resp = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol":     "SPX",
                "interval":   "1day",
                "outputsize": 55,
                "apikey":     config.TWELVE_DATA_KEY,
            },
            timeout=12,
        )
        values = resp.json().get("values", [])
        if len(values) < 51:
            return None
        current = float(values[0]["close"])
        ma50    = sum(float(v["close"]) for v in values[1:51]) / 50
        result  = current > ma50
        cache.set("REGIME:spx_50d", result)
        return result
    except Exception:
        return None


# ── Classification algorithm ───────────────────────────────────────────────────

def _classify(vix, vix_trend, yield_curve, gold_5d_pct,
              spx_above_50d=None, top_ccys=None):
    """Return (regime_key, signal_lines, risk_on_score, risk_off_score)."""
    risk_on  = 0
    risk_off = 0
    sigs     = []

    # VIX level — the primary signal
    if vix is not None:
        if vix < 13:
            risk_on += 3
            sigs.append(f"VIX {vix:.1f} (very low — calm markets)")
        elif vix < 16:
            risk_on += 2
            sigs.append(f"VIX {vix:.1f} (low — risk appetite)")
        elif vix < 20:
            pass   # neutral band: no signal either way
        elif vix < 25:
            risk_off += 1
            sigs.append(f"VIX {vix:.1f} (elevated)")
        elif vix < 28:
            risk_off += 2
            sigs.append(f"VIX {vix:.1f} (high — caution)")
        else:
            risk_off += 3
            sigs.append(f"VIX {vix:.1f} (very high — fear)")

    # VIX trend — direction of travel matters
    if isinstance(vix_trend, str):
        vt = vix_trend.lower()
        if "fall" in vt or "declin" in vt:
            risk_on += 1
            sigs.append("VIX falling (improving sentiment)")
        elif "ris" in vt or "increas" in vt:
            risk_off += 1
            sigs.append("VIX rising (deteriorating sentiment)")

    # Yield curve (10Y − 2Y)
    if yield_curve is not None:
        if yield_curve > 0.3:
            risk_on += 1
            sigs.append(f"yield curve +{yield_curve:.2f}% (healthy/normal)")
        elif yield_curve < -0.2:
            risk_off += 1
            sigs.append(f"yield curve {yield_curve:.2f}% (inverted — recession signal)")

    # Gold — rising gold = safe haven demand = risk-off
    if gold_5d_pct is not None:
        if gold_5d_pct > 1.5:
            risk_off += 2
            sigs.append(f"gold +{gold_5d_pct:.1f}% 5d (strong safe haven demand)")
        elif gold_5d_pct > 0.5:
            risk_off += 1
            sigs.append(f"gold +{gold_5d_pct:.1f}% 5d (mild safe haven demand)")
        elif gold_5d_pct < -1.5:
            risk_on += 2
            sigs.append(f"gold {gold_5d_pct:.1f}% 5d (risk assets strongly preferred)")
        elif gold_5d_pct < -0.5:
            risk_on += 1
            sigs.append(f"gold {gold_5d_pct:.1f}% 5d (mild risk-on preference)")

    # S&P 500 vs 50-day MA — equity trend confirms risk direction
    if spx_above_50d is True:
        risk_on += 1
        sigs.append("SPX above 50-day MA (equity trend bullish)")
    elif spx_above_50d is False:
        risk_off += 1
        sigs.append("SPX below 50-day MA (equity trend bearish)")

    # Currency strength confirmation — top currencies corroborate regime
    if top_ccys:
        top2 = [c.upper() for c in top_ccys[:2]]
        risk_on_count  = sum(1 for c in top2 if c in _RISK_ON_CCYS)
        risk_off_count = sum(1 for c in top2 if c in _RISK_OFF_CCYS)
        if risk_on_count == 2:
            risk_on += 1
            sigs.append(f"currency strength confirms risk-on ({', '.join(top2)} leading)")
        elif risk_off_count == 2:
            risk_off += 1
            sigs.append(f"currency strength confirms risk-off ({', '.join(top2)} leading)")

    # High volatility overrides direction — chaotic markets are their own regime
    high_vol = vix is not None and vix >= 28

    net = risk_on - risk_off

    if high_vol:
        regime = "ranging_high_vol"
    elif net >= 2:
        regime = "trending_risk_on"
    elif net <= -2:
        regime = "trending_risk_off"
    elif net >= 1:
        regime = "trending_risk_on"
    elif net <= -1:
        regime = "trending_risk_off"
    else:
        regime = "ranging_low_vol"

    return regime, sigs, risk_on, risk_off


def _check_regime_change(current_regime: str) -> None:
    """Persist regime to disk; send Telegram alert if it changed since last scan."""
    try:
        prev_regime = None
        if _REGIME_STATE_FILE.exists():
            try:
                data = json.loads(_REGIME_STATE_FILE.read_text(encoding="utf-8"))
                prev_regime = data.get("regime")
            except Exception:
                pass

        if prev_regime and prev_regime != current_regime:
            prev_info = REGIMES.get(prev_regime, {})
            curr_info = REGIMES.get(current_regime, {})
            prev_lbl  = prev_info.get("display_label", prev_regime)
            curr_lbl  = curr_info.get("display_label", current_regime)
            curr_emoji = curr_info.get("emoji", "📊")
            curr_sysmsg = curr_info.get("system_message", "")
            alert = (
                f"{curr_emoji} <b>Market regime change detected</b>\n\n"
                f"Previous: <b>{prev_lbl}</b>\n"
                f"Current:  <b>{curr_lbl}</b>\n\n"
                f"{curr_sysmsg}"
            )
            try:
                from src import telegram_alert as _ta
                _ta.send(alert)
            except Exception:
                pass

        _REGIME_STATE_FILE.write_text(
            json.dumps({"regime": current_regime}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


# ── Public API ────────────────────────────────────────────────────────────────

def detect(macro_signals: dict = None, ccy_strength: dict = None) -> dict:
    """Detect and return the global market regime.

    macro_signals: dict from macro.analyse()["signals"]. If None, fetches fresh.
    Caches result for 2 hours so all callers in a run share the same regime.

    Returns dict with: regime, label, emoji, description, favour_ccys (list),
    avoid_ccys (list), conf_override (int|None), conf_threshold (float),
    conditions_cap (int), threshold_reason (str), system_message (str),
    size_mult (float), vix, yield_curve, gold_5d_pct, signal_lines (list[str]),
    risk_on_score, risk_off_score.
    """
    cached = cache.get(_CACHE_KEY, ttl_hours=_CACHE_TTL)
    if cached is not None:
        return cached

    if macro_signals is None:
        try:
            from src import macro as _macro
            macro_signals = _macro.analyse().get("signals", {})
        except Exception:
            macro_signals = {}

    vix         = None
    vix_trend   = None
    yield_curve = None

    try:
        vix_data = macro_signals.get("VIX (volatility index)", {})
        if isinstance(vix_data, dict) and vix_data.get("value") is not None:
            vix       = float(vix_data["value"])
            vix_trend = vix_data.get("trend", "unknown")
    except Exception:
        pass

    try:
        crv_data = macro_signals.get("US 2s10s curve (10Y-2Y, %)", {})
        if isinstance(crv_data, dict) and crv_data.get("value") is not None:
            yield_curve = float(crv_data["value"])
    except Exception:
        pass

    gold_5d_pct  = _fetch_gold_change_pct()
    spx_above_50d = _fetch_spx_above_50d()

    # Derive top currencies by score (highest to lowest)
    top_ccys = None
    if ccy_strength:
        try:
            sorted_ccys = sorted(
                ccy_strength.keys(),
                key=lambda c: (ccy_strength[c] or {}).get("score", 0),
                reverse=True,
            )
            top_ccys = sorted_ccys[:3]
        except Exception:
            pass

    regime, signal_lines, risk_on, risk_off = _classify(
        vix, vix_trend, yield_curve, gold_5d_pct,
        spx_above_50d=spx_above_50d, top_ccys=top_ccys,
    )

    info = REGIMES.get(regime, REGIMES[_DEFAULT_REGIME])

    result = {
        "regime":           regime,
        "label":            info["label"],
        "display_label":    info.get("display_label", info["label"]),
        "emoji":            info["emoji"],
        "description":      info["description"],
        "favour_ccys":      sorted(info["favour_ccys"]),
        "avoid_ccys":       sorted(info["avoid_ccys"]),
        # Threshold and conditions (single source of truth)
        "conf_threshold":   info["conf_threshold"],
        "conditions_cap":   info["conditions_cap"],
        "threshold_reason": info["threshold_reason"],
        "system_message":   info["system_message"],
        # Legacy
        "conf_override":    info["conf_override"],
        "size_mult":        info["size_mult"],
        # Signal details
        "vix":              vix,
        "yield_curve":      yield_curve,
        "gold_5d_pct":      gold_5d_pct,
        "spx_above_50d":    spx_above_50d,
        "signal_lines":     signal_lines,
        "risk_on_score":    risk_on,
        "risk_off_score":   risk_off,
    }
    cache.set(_CACHE_KEY, result)
    _check_regime_change(regime)
    return result


def regime_currency_bonus(regime: str, base: str, quote: str) -> float:
    """Pair-selection score bonus (0–12 pts) for alignment with the current regime.

    Pairs with both a favoured and an avoided currency score highest (e.g.
    AUD/JPY in risk-on).  Partial alignment scores 6 pts.
    """
    info   = REGIMES.get(regime, {})
    favour = info.get("favour_ccys", set())
    avoid  = info.get("avoid_ccys",  set())

    if not favour:
        return 0.0   # ranging regimes — no directional currency bias

    base_fav   = base  in favour
    quote_fav  = quote in favour
    base_avoid = base  in avoid
    quote_avoid = quote in avoid

    # Strong alignment: one side favoured, other side avoided (e.g. AUD/JPY risk-on)
    if (base_fav and quote_avoid) or (base_avoid and quote_fav):
        return 12.0
    # Partial alignment: one side aligned
    elif base_fav or quote_fav or base_avoid or quote_avoid:
        return 6.0
    return 0.0


def regime_display_lines(regime_data: dict) -> list:
    """Return plain-English Telegram lines for the 6am MARKET CONTEXT section.

    Returns a list of 1–2 strings:
      [0] Primary regime line:  "📊 Market environment today: Trending — Risk On — ..."
      [1] System message line:  "The system is running with a lower confidence threshold..."
    """
    if not regime_data:
        return []
    emoji   = regime_data.get("emoji", "📊")
    label   = regime_data.get("label", "Unknown")
    desc    = (regime_data.get("description") or "").strip()
    sysmsg  = (regime_data.get("system_message") or "").strip()

    # Capitalise first letter of description
    if desc:
        desc = desc[0].upper() + desc[1:]

    lines = [f"📊 <b>Market environment today: {label}</b> — {desc}"]
    if sysmsg:
        lines.append(sysmsg)
    return lines


def conditions_telegram_line(regime_data: dict, conditions_score: int) -> str:
    """Single-line conditions summary: regime label + score + brief description."""
    if not regime_data:
        return f"📊 Trading conditions: {conditions_score}/10"
    label = regime_data.get("label", "")
    cap   = regime_data.get("conditions_cap", 10)
    if conditions_score >= cap:
        quality = "at capacity for this regime"
    elif conditions_score >= cap - 2:
        quality = "approaching capacity"
    else:
        quality = "below average"
    return f"📊 Trading conditions: {conditions_score}/10 — {label} — {quality}"


def telegram_line(regime_data: dict) -> str:
    """Legacy single-line regime label for backward-compatibility."""
    if not regime_data:
        return ""
    emoji = regime_data.get("emoji", "📊")
    label = regime_data.get("label", "Unknown")
    desc  = (regime_data.get("description") or "").strip()
    if desc:
        desc = desc[0].upper() + desc[1:]
    return f"{emoji} <b>Market environment: {label}</b> — {desc}"
