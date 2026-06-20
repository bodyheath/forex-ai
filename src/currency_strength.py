"""Currency strength meter — absolute strength for each of the 12 tracked currencies.

Strength is computed by averaging normalised 5-day momentum / ATR across all
cached OHLCV pairs containing each currency.  Only existing cache entries are
used — zero new API calls.

Score: −100 to +100 (integer).
  +40 to +100   strong / strongest
  +10 to +39    slight strength
  −10 to +9     neutral
  −35 to −11    slight weakness
  −100 to −36   weak / weakest
"""

from src import cache as _cache

_CURRENCIES_12 = [
    "EUR", "GBP", "USD", "JPY", "CHF", "AUD", "NZD", "CAD",
    "NOK", "SEK", "SGD", "HKD",
]

# Comprehensive pair set — only pairs with cached data contribute.
# Covers all major and liquid cross combinations.
_STRENGTH_PAIRS = [
    # G8 majors vs USD
    "EUR/USD", "GBP/USD", "AUD/USD", "NZD/USD",
    "USD/JPY", "USD/CAD", "USD/CHF",
    # EUR crosses
    "EUR/GBP", "EUR/JPY", "EUR/AUD", "EUR/CAD", "EUR/CHF", "EUR/NZD",
    # GBP crosses
    "GBP/JPY", "GBP/AUD", "GBP/CAD", "GBP/CHF", "GBP/NZD",
    # JPY crosses
    "AUD/JPY", "CAD/JPY", "CHF/JPY", "NZD/JPY",
    # Other G8 crosses
    "AUD/CAD", "AUD/CHF", "AUD/NZD",
    "NZD/CAD", "NZD/CHF",
    "CAD/CHF",
    # G10 Scandinavian
    "EUR/NOK", "USD/NOK", "GBP/NOK",
    "EUR/SEK", "USD/SEK", "GBP/SEK",
    # Liquid Asian
    "USD/SGD", "EUR/SGD", "SGD/JPY",
    "USD/HKD", "EUR/HKD",
    # Extra Scandinavian / Asian crosses
    "AUD/NOK", "AUD/SEK",
    "NZD/NOK", "NZD/SEK",
    "NOK/JPY", "SEK/JPY",
    "AUD/SGD", "AUD/HKD",
    "HKD/JPY",
]

# Minimum pairs needed before reporting a strength score for a currency.
# Below this, there is too little data to distinguish currency-specific moves.
_MIN_PAIRS = 2


def _pair_signal(pair):
    """Return normalised 5-day directional signal for pair, or None if no data.

    Positive = base currency rising vs quote.  Capped to [−1, +1].
    Uses SEL:snap cache first, falls back to TD:pair:1day:400 cache.
    """
    snap = _cache.get(f"SEL:snap:{pair}")
    if snap is None:
        td = _cache.get(f"TD:{pair}:1day:400")
        if isinstance(td, dict) and "values" in td:
            closes, highs, lows = [], [], []
            for v in (td.get("values") or [])[:20]:
                try:
                    closes.append(float(v["close"]))
                    highs.append(float(v["high"]))
                    lows.append(float(v["low"]))
                except (KeyError, TypeError, ValueError):
                    pass
            if len(closes) >= 3:
                snap = {"closes": closes, "highs": highs, "lows": lows}

    if snap is None:
        return None

    closes = snap.get("closes", [])
    highs  = snap.get("highs",  [])
    lows   = snap.get("lows",   [])

    if len(closes) < 3:
        return None

    n_atr = min(5, len(highs))
    if n_atr > 0 and highs and lows:
        atr = sum(highs[i] - lows[i] for i in range(n_atr)) / n_atr
    else:
        atr = max(closes[:6]) - min(closes[:6]) if len(closes) >= 6 else 0.0

    if atr <= 0:
        return None

    n_back = min(5, len(closes) - 1)
    move   = closes[0] - closes[n_back]
    return max(-1.0, min(1.0, move / atr))


def compute(pairs=None):
    """Compute currency strength scores for all 12 tracked currencies.

    Returns:
        dict  {currency: {"score": int, "label": str, "n_pairs": int}}

    Scores range from −100 (extremely weak) to +100 (extremely strong).
    Currencies with fewer than _MIN_PAIRS cached pairs return score=0,
    label="no data".
    """
    if pairs is None:
        pairs = _STRENGTH_PAIRS

    ccy_signals = {c: [] for c in _CURRENCIES_12}

    for pair in pairs:
        parts = pair.split("/")
        if len(parts) != 2:
            continue
        base, quote = parts[0].upper(), parts[1].upper()
        if base not in ccy_signals and quote not in ccy_signals:
            continue

        sig = _pair_signal(pair)
        if sig is None:
            continue

        if base in ccy_signals:
            ccy_signals[base].append(sig)      # +sig when base is rising
        if quote in ccy_signals:
            ccy_signals[quote].append(-sig)    # −sig when quote is falling

    result = {}
    for ccy in _CURRENCIES_12:
        sigs = ccy_signals[ccy]
        if len(sigs) < _MIN_PAIRS:
            result[ccy] = {"score": 0, "label": "no data", "n_pairs": len(sigs)}
        else:
            avg   = sum(sigs) / len(sigs)
            score = round(avg * 100)
            result[ccy] = {"score": score, "n_pairs": len(sigs), "label": ""}

    # Assign relative labels (strongest / weakest) and absolute labels for the rest
    valid = [(c, d["score"]) for c, d in result.items() if d["n_pairs"] >= _MIN_PAIRS]
    if valid:
        max_score = max(s for _, s in valid)
        min_score = min(s for _, s in valid)
        for ccy, data in result.items():
            if data["n_pairs"] < _MIN_PAIRS:
                continue
            s = data["score"]
            if s == max_score and s >= 25:
                data["label"] = "strongest — broad-based buying across all pairs"
            elif s == min_score and s <= -25:
                data["label"] = "weakest — broad-based selling"
            elif s >= 40:
                data["label"] = "strong"
            elif s >= 10:
                data["label"] = "slight strength"
            elif s >= -10:
                data["label"] = "neutral"
            elif s >= -35:
                data["label"] = "slight weakness"
            else:
                data["label"] = "weak"

    return result


def strength_display_lines(scores, scan_mode="full"):
    """Build Telegram lines for the 💪 Currency strength today block.

    Only shown on 6am (full) scans.  Currencies with no data are omitted.
    Sorted by score descending.

    Returns a list of strings, or [] if nothing to display.
    """
    if scan_mode != "full":
        return []

    entries = [
        (ccy, data)
        for ccy, data in scores.items()
        if data.get("n_pairs", 0) >= _MIN_PAIRS
    ]
    if not entries:
        return []

    entries.sort(key=lambda x: x[1]["score"], reverse=True)

    lines = ["💪 <b>Currency strength today:</b>"]
    for ccy, data in entries:
        s     = data["score"]
        prefix = "+" if s >= 0 else ""
        label  = data.get("label", "")
        lines.append(f"  {ccy}: {prefix}{s}  ({label})")
    return lines


def alignment_note(base_ccy, quote_ccy, direction, scores):
    """Return (conf_boost: int, note_line: str | None) for a trade.

    conf_boost = +1 when both currencies strongly confirm the trade direction.
    note_line  = human-readable note for the Telegram message (or None).

    Double-aligned: base strong + quote weak in trade direction.
    Threshold: |score| >= 30 counts as "strong enough".
    """
    base_data  = scores.get(base_ccy, {})
    quote_data = scores.get(quote_ccy, {})
    bs = base_data.get("score")
    qs = quote_data.get("score")
    bn = base_data.get("n_pairs", 0)
    qn = quote_data.get("n_pairs", 0)

    if bs is None or qs is None or bn < _MIN_PAIRS or qn < _MIN_PAIRS:
        return 0, None

    _STRONG = 30    # threshold for "genuinely strong"
    _MODERATE = 8   # threshold for "at least slightly positive"

    direction = direction.upper()
    if direction == "BUY":
        double_aligned = bs >= _STRONG and qs <= -_STRONG
        base_dir_score  = bs
        quote_dir_score = qs
        strong_ccy  = base_ccy
        neutral_ccy = quote_ccy
    elif direction == "SELL":
        double_aligned = bs <= -_STRONG and qs >= _STRONG
        base_dir_score  = -bs   # for SELL, we want base to be negative
        quote_dir_score = qs    # for SELL, we want quote to be positive
        strong_ccy  = quote_ccy
        neutral_ccy = base_ccy
    else:
        return 0, None

    if double_aligned:
        bs_fmt = f"{'+' if bs >= 0 else ''}{bs}"
        qs_fmt = f"{'+' if qs >= 0 else ''}{qs}"
        note = (
            f"💪 Double-aligned: {base_ccy} {bs_fmt} · {quote_ccy} {qs_fmt} — "
            f"broad {strong_ccy} strength confirmed across all pairs — confidence boosted +1"
        )
        return 1, note

    # Check for moderate-only strength in the expected direction
    if direction == "BUY" and _MODERATE <= bs < _STRONG:
        note = (
            f"ℹ️ Moderate {base_ccy} strength (+{bs}) — "
            f"this may be a {quote_ccy} move rather than genuine {base_ccy} strength — "
            f"confidence unchanged"
        )
        return 0, note
    if direction == "SELL" and _MODERATE <= qs < _STRONG:
        note = (
            f"ℹ️ Moderate {quote_ccy} strength (+{qs}) — "
            f"this may be a {base_ccy} move rather than genuine {quote_ccy} strength — "
            f"confidence unchanged"
        )
        return 0, note

    return 0, None
