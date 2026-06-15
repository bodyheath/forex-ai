"""Fundamental trend alignment checker.

Three-factor fundamental score per currency pair trade direction:
  1. Central bank direction — hiking = bullish, cutting = bearish
  2. Carry differential  — base rate vs quote rate (>0.75% threshold)
  3. Economic surprise   — recent data beating/missing consensus forecasts

TAILWIND (+1 conf): all three factors align with trade direction.
HEADWIND (-1 conf): all three factors oppose trade direction.
MIXED      (0 adj): fewer than three factors align either way.

CB stance and economic surprise are reviewed monthly.  To update:
  - Run monthly after each major CB meeting cycle.
  - Check Bloomberg ESI, Reuters polls, and central bank statements.
  - Update _CB_STANCE bias + note and _ECON_SURPRISE values below.
  - Update the _CB_REVIEWED and _ECON_REVIEWED dates.
"""

# ── Central bank stance ───────────────────────────────────────────────────────
# "bullish" = hiking or hawkish hold (rate is rising / held at elevated level)
# "bearish" = cutting or dovish (rate falling / near zero lower bound)
# "neutral" = holding with data-dependent guidance, no clear next move
# Last reviewed: 2026-06-15
_CB_STANCE = {
    "USD": {"bias": "neutral",  "note": "Fed on hold after 2025 cuts, watching inflation"},
    "EUR": {"bias": "bearish",  "note": "ECB in cutting cycle, dovish forward guidance"},
    "GBP": {"bias": "neutral",  "note": "BoE holding, sticky services inflation"},
    "JPY": {"bias": "bullish",  "note": "BoJ hiking cycle, yield curve control ended"},
    "AUD": {"bias": "bearish",  "note": "RBA cutting, weak domestic demand"},
    "NZD": {"bias": "bearish",  "note": "RBNZ cutting cycle, recession risk"},
    "CAD": {"bias": "bearish",  "note": "BoC cutting aggressively, slowing growth"},
    "CHF": {"bias": "bearish",  "note": "SNB cutting, near zero lower bound"},
    "NOK": {"bias": "neutral",  "note": "Norges Bank on hold, oil-price dependent"},
    "SEK": {"bias": "bearish",  "note": "Riksbank cutting, weak Swedish economy"},
    "SGD": {"bias": "neutral",  "note": "MAS managing NEER, modest policy easing"},
    "HKD": {"bias": "neutral",  "note": "HKMA mirrors Fed rate decisions"},
}
_CB_REVIEWED = "2026-06-15"

# ── Economic surprise assessments ────────────────────────────────────────────
# "bullish" = recent data consistently BEATING consensus forecasts
# "bearish" = recent data consistently MISSING consensus forecasts
# "neutral" = mixed or broadly in-line data
# Sources: Bloomberg Economic Surprise Index, Reuters polls, central bank surveys
# Last reviewed: 2026-06-15
_ECON_SURPRISE = {
    "USD": "neutral",   # jobs resilient, manufacturing soft, mixed signals
    "EUR": "bearish",   # PMI below estimates, German/French data weak
    "GBP": "neutral",   # services OK, manufacturing weak, better than feared
    "JPY": "bullish",   # wages beating, Tankan improving, domestic demand reviving
    "AUD": "bearish",   # employment mixed, CPI below estimates, China drag
    "NZD": "bearish",   # GDP contracting, trade balance weak, consumer soft
    "CAD": "bearish",   # employment disappointing, housing correcting
    "CHF": "neutral",   # inflation in-line, modest growth, defensive
    "NOK": "neutral",   # oil-dependent, volatile, mixed surveys
    "SEK": "bearish",   # GDP contracting, CPIF below target
    "SGD": "neutral",   # trade-driven, modest beat, China-dependent
    "HKD": "neutral",   # mirrors USD/China dynamics
}
_ECON_REVIEWED = "2026-06-15"

# ── Policy rates (kept in sync with trade_costs._FALLBACK_RATES) ──────────────
# Imported to avoid duplication; fallback dict used if trade_costs is unavailable.
try:
    from src.trade_costs import _FALLBACK_RATES as _RATES
except ImportError:
    _RATES = {
        "USD": 5.50, "EUR": 4.50, "GBP": 5.25, "JPY": 0.10,
        "AUD": 4.35, "NZD": 5.50, "CAD": 5.00, "CHF": 1.75,
        "NOK": 4.50, "SEK": 4.00, "SGD": 3.75, "HKD": 5.75,
    }

_CARRY_THRESHOLD = 0.75  # minimum rate differential (%) to register a carry signal


# ── Per-factor scoring ────────────────────────────────────────────────────────

def _bias_score(bias: str) -> int:
    return {"bullish": 1, "neutral": 0, "bearish": -1}.get(bias, 0)


def _net_score_for_direction(base_val: int, quote_val: int, direction: str) -> int:
    """Return +1/0/-1 for a factor given base & quote numeric bias and direction.

    For BUY: want base bullish (net > 0).   net > 0 → +1, net < 0 → -1.
    For SELL: want base bearish (net < 0).  net < 0 → +1, net > 0 → -1.
    """
    net = base_val - quote_val
    if direction == "BUY":
        if net > 0:
            return 1
        if net < 0:
            return -1
        return 0
    else:
        if net < 0:
            return 1
        if net > 0:
            return -1
        return 0


def _cb_factor(base: str, quote: str, direction: str) -> int:
    b = _bias_score(_CB_STANCE.get(base,  {}).get("bias", "neutral"))
    q = _bias_score(_CB_STANCE.get(quote, {}).get("bias", "neutral"))
    return _net_score_for_direction(b, q, direction)


def _carry_factor(base: str, quote: str, direction: str) -> int:
    """Positive carry (base rate > quote rate) aligns with BUY base, opposes SELL base."""
    diff = _RATES.get(base, 0.0) - _RATES.get(quote, 0.0)
    if direction == "BUY":
        if diff > _CARRY_THRESHOLD:
            return 1
        if diff < -_CARRY_THRESHOLD:
            return -1
        return 0
    else:
        if diff < -_CARRY_THRESHOLD:
            return 1   # giving up less carry by selling the low-yielder
        if diff > _CARRY_THRESHOLD:
            return -1  # giving up positive carry by selling the high-yielder
        return 0


def _econ_factor(base: str, quote: str, direction: str) -> int:
    b = _bias_score(_ECON_SURPRISE.get(base,  "neutral"))
    q = _bias_score(_ECON_SURPRISE.get(quote, "neutral"))
    return _net_score_for_direction(b, q, direction)


# ── Currency-level convenience functions for pre-filter scoring ───────────────

def currency_cb_score(ccy: str) -> int:
    """Return +1/0/-1 CB bias score for a single currency."""
    return _bias_score(_CB_STANCE.get(ccy.upper(), {}).get("bias", "neutral"))


def currency_econ_score(ccy: str) -> int:
    """Return +1/0/-1 economic surprise score for a single currency."""
    return _bias_score(_ECON_SURPRISE.get(ccy.upper(), "neutral"))


# ── Main alignment function ───────────────────────────────────────────────────

def get_fundamental_alignment(base_ccy: str, quote_ccy: str, direction: str) -> dict:
    """Return fundamental alignment for a currency pair trade direction.

    Returns
    -------
    dict with keys:
      alignment    "TAILWIND" | "HEADWIND" | "MIXED"
      conf_adj     +1 (tailwind) | -1 (headwind) | 0 (mixed)
      cb_score     +1 / 0 / -1
      carry_score  +1 / 0 / -1
      econ_score   +1 / 0 / -1
      aligned      count of factors with score +1
      opposed      count of factors with score -1
      cb_base      CB bias string for base ("bullish"/"bearish"/"neutral")
      cb_quote     CB bias string for quote
      carry_diff   base_rate - quote_rate in %
      econ_base    econ surprise string for base
      econ_quote   econ surprise string for quote
      cb_note_base  CB meeting note for base
      cb_note_quote CB meeting note for quote
    """
    base = base_ccy.upper()
    quot = quote_ccy.upper()
    dirn = direction.upper()

    cb_score    = _cb_factor(base, quot, dirn)
    carry_score = _carry_factor(base, quot, dirn)
    econ_score  = _econ_factor(base, quot, dirn)

    scores  = [cb_score, carry_score, econ_score]
    aligned = sum(1 for s in scores if s > 0)
    opposed = sum(1 for s in scores if s < 0)

    if aligned == 3:
        alignment = "TAILWIND"
        conf_adj  = +1
    elif opposed == 3:
        alignment = "HEADWIND"
        conf_adj  = -1
    else:
        alignment = "MIXED"
        conf_adj  = 0

    return {
        "alignment":     alignment,
        "conf_adj":      conf_adj,
        "cb_score":      cb_score,
        "carry_score":   carry_score,
        "econ_score":    econ_score,
        "aligned":       aligned,
        "opposed":       opposed,
        "cb_base":       _CB_STANCE.get(base, {}).get("bias", "neutral"),
        "cb_quote":      _CB_STANCE.get(quot, {}).get("bias", "neutral"),
        "carry_diff":    round(_RATES.get(base, 0.0) - _RATES.get(quot, 0.0), 2),
        "econ_base":     _ECON_SURPRISE.get(base, "neutral"),
        "econ_quote":    _ECON_SURPRISE.get(quot, "neutral"),
        "cb_note_base":  _CB_STANCE.get(base, {}).get("note", ""),
        "cb_note_quote": _CB_STANCE.get(quot, {}).get("note", ""),
    }
