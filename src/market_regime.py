"""Global market regime detector.

Classifies the current macro environment into one of four regimes using VIX,
bond yields, gold (XAU/USD), and the yield curve:

  trending_risk_on   — low VIX, risk appetite; favour AUD/NZD/CAD
  trending_risk_off  — elevated VIX, flight to safety; favour JPY/CHF/USD
  ranging_low_vol    — calm but directionless; favour mean-reversion setups
  ranging_high_vol   — chaotic; reduce position sizes 50%, raise conf to 8

Public API
----------
detect(macro_signals=None) -> dict
    Compute and return the global regime. Caches result for 2 hours.
    macro_signals: optional pre-fetched dict from macro.analyse()["signals"].
    When omitted, fetches FRED data independently (FRED caches its own calls).

regime_currency_bonus(regime, base, quote) -> float
    Return selector score bonus (0–8 pts) for a pair aligned with the regime.

telegram_line(regime_data) -> str
    Return the plain-English Telegram display line for the 6am message.
"""

import requests

import config
from src import cache

_CACHE_KEY = "REGIME:global"
_CACHE_TTL  = 2.0   # hours

# ── Currency classifications ───────────────────────────────────────────────────
_RISK_ON_CCYS  = {"AUD", "NZD", "CAD", "GBP", "EUR"}
_RISK_OFF_CCYS = {"JPY", "CHF", "USD"}

# ── Regime metadata ────────────────────────────────────────────────────────────
REGIMES = {
    "trending_risk_on": {
        "label":         "Risk-ON",
        "emoji":         "🟢",
        "description":   (
            "investors are confident and taking risks. "
            "Currencies that pay higher interest rates like AUD and NZD "
            "tend to do well in this environment."
        ),
        "favour_ccys":   {"AUD", "NZD", "CAD"},
        "avoid_ccys":    {"JPY", "CHF"},
        "conf_override": None,
        "size_mult":     1.0,
    },
    "trending_risk_off": {
        "label":         "Risk-OFF",
        "emoji":         "🔴",
        "description":   (
            "investors are cautious and moving to safe havens. "
            "JPY, CHF and USD tend to strengthen in this environment."
        ),
        "favour_ccys":   {"JPY", "CHF", "USD"},
        "avoid_ccys":    {"AUD", "NZD"},
        "conf_override": None,
        "size_mult":     1.0,
    },
    "ranging_low_vol": {
        "label":         "Ranging — Low Volatility",
        "emoji":         "⚖️",
        "description":   (
            "markets are calm but directionless. "
            "Mean-reversion setups tend to work better than trend-following right now."
        ),
        "favour_ccys":   set(),
        "avoid_ccys":    set(),
        "conf_override": None,
        "size_mult":     1.0,
    },
    "ranging_high_vol": {
        "label":         "Ranging — High Volatility",
        "emoji":         "⚠️",
        "description":   (
            "markets are chaotic with large unpredictable swings. "
            "Position sizes are automatically reduced by 50% and only "
            "the highest conviction trades are taken."
        ),
        "favour_ccys":   set(),
        "avoid_ccys":    set(),
        "conf_override": 8,    # equivalent to raising threshold to 7.5+ with int confidence
        "size_mult":     0.5,
    },
}

_DEFAULT_REGIME = "ranging_low_vol"


# ── Gold via Twelve Data ───────────────────────────────────────────────────────

def _fetch_gold_change_pct():
    """5-day XAU/USD % change from Twelve Data. Returns None on any failure."""
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


# ── Classification algorithm ───────────────────────────────────────────────────

def _classify(vix, vix_trend, yield_curve, gold_5d_pct):
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
            risk_on += 1
            sigs.append(f"VIX {vix:.1f} (below average)")
        elif vix < 25:
            risk_off += 1
            sigs.append(f"VIX {vix:.1f} (elevated)")
        elif vix < 30:
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


# ── Public API ────────────────────────────────────────────────────────────────

def detect(macro_signals: dict = None) -> dict:
    """Detect and return the global market regime.

    macro_signals: dict from macro.analyse()["signals"]. If None, fetches fresh.
    Caches result for 2 hours so all callers in a run share the same regime.

    Returns dict: regime, label, emoji, description, favour_ccys (list),
    avoid_ccys (list), conf_override (int|None), size_mult (float),
    vix, yield_curve, gold_5d_pct, signal_lines (list[str]),
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

    vix        = None
    vix_trend  = None
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

    gold_5d_pct = _fetch_gold_change_pct()

    regime, signal_lines, risk_on, risk_off = _classify(
        vix, vix_trend, yield_curve, gold_5d_pct
    )

    info = REGIMES.get(regime, REGIMES[_DEFAULT_REGIME])

    result = {
        "regime":         regime,
        "label":          info["label"],
        "emoji":          info["emoji"],
        "description":    info["description"],
        "favour_ccys":    sorted(info["favour_ccys"]),   # list for JSON cache
        "avoid_ccys":     sorted(info["avoid_ccys"]),    # list for JSON cache
        "conf_override":  info["conf_override"],
        "size_mult":      info["size_mult"],
        "vix":            vix,
        "yield_curve":    yield_curve,
        "gold_5d_pct":    gold_5d_pct,
        "signal_lines":   signal_lines,
        "risk_on_score":  risk_on,
        "risk_off_score": risk_off,
    }
    cache.set(_CACHE_KEY, result)
    return result


def regime_currency_bonus(regime: str, base: str, quote: str) -> float:
    """Selector score bonus (0–8 pts) for a pair aligned with the current regime.

    At the selector stage the trade direction is unknown, so we score how many
    regime-relevant currencies appear in the pair — pairs with both a favoured
    and an avoided currency score highest (e.g. AUD/JPY in risk-on).
    """
    info   = REGIMES.get(regime, {})
    favour = info.get("favour_ccys", set())
    avoid  = info.get("avoid_ccys",  set())

    if not favour:
        return 0.0   # ranging regimes — no directional currency bias

    base_fav   = base  in favour
    quote_fav  = quote in favour
    base_avoid = base  in avoid
    quote_fav_from_avoid = quote in avoid

    # Strong alignment: one side favoured, other side avoided (e.g. AUD/JPY risk-on)
    if (base_fav and quote_fav_from_avoid) or (base_avoid and quote_fav):
        return 8.0
    # Partial alignment: one side aligned
    elif base_fav or quote_fav or base_avoid or quote_fav_from_avoid:
        return 4.0
    return 0.0


def telegram_line(regime_data: dict) -> str:
    """Plain-English regime line for the 6am Telegram MARKET CONTEXT section."""
    if not regime_data:
        return ""
    emoji = regime_data.get("emoji", "📊")
    label = regime_data.get("label", "Unknown")
    desc  = regime_data.get("description", "")
    return f"{emoji} <b>Market environment today: {label}</b> — {desc}"
