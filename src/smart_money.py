"""Smart Money Divergence (SMD) — Layer 10 of the forex-ai analysis pipeline.

Compares institutional positioning (CFTC COT data) against retail / public
sentiment (keyword analysis of news headlines) for both currencies in a pair.

Score: −10 to +10
  +10  =  institutions at 1-year max LONG while retail headlines are maximally
           bearish → classic contrarian BUY signal
  −10  =  institutions at 1-year max SHORT while retail headlines are maximally
           bullish → classic contrarian SELL signal
    0  =  no meaningful divergence between the two groups

Signal bands
  STRONG_BUY   ≥ +8   Powerful institutional/retail divergence, supports BUY
  MODERATE_BUY ≥ +5   Moderate divergence, mild BUY lean
  NEUTRAL        −4 to +4   No clear smart-money edge
  MODERATE_SELL ≤ −5   Moderate divergence, mild SELL lean
  STRONG_SELL   ≤ −8   Powerful divergence, supports SELL

Confidence boost rule (applied in daily._eff_conf):
  If SMD ≥ +8 and trade direction = BUY  → +1 confidence
  If SMD ≤ −8 and trade direction = SELL → +1 confidence
"""

# ── Retail sentiment word sets ─────────────────────────────────────────────────

_BULLISH = frozenset([
    "rally", "rallied", "rallies", "surge", "surged", "surges", "surging",
    "rise", "rose", "risen", "rising", "gain", "gains", "gained",
    "bullish", "optimistic", "optimism", "hawkish", "hawkishness",
    "buy", "buying", "upside", "recovery", "recover", "boost", "boosted",
    "increase", "increases", "increased", "higher", "positive",
    "growth", "growing", "grew", "confident", "confidence", "outperform",
    "breakout", "support", "rebound", "rebounded", "strength", "strong",
    "advance", "advances", "advanced", "jump", "jumps", "jumped", "soar",
    "soared", "soaring", "uptrend", "upgrade", "beat", "beats",
])

_BEARISH = frozenset([
    "fall", "falls", "fell", "fallen", "falling", "drop", "drops", "dropped",
    "decline", "declines", "declined", "declining", "plunge", "plunged", "plunges",
    "plunging", "bearish", "pessimistic", "pessimism", "weak", "weakness",
    "dovish", "dovishness", "sell", "selling", "downside", "recession",
    "decrease", "decreases", "decreased", "lower", "negative", "slowdown",
    "concern", "concerns", "risk", "worry", "worries", "fear", "fears",
    "pressure", "selloff", "correction", "corrected", "correcting",
    "short", "downtrend", "down", "tumble", "tumbled", "tumbles",
    "slump", "slumped", "slumps", "deteriorate", "deteriorating", "miss",
    "misses", "missed", "cut", "cuts", "downgrade",
])


def _retail_score(sent_data: dict) -> float:
    """Keyword-based retail sentiment score: −1.0 (max bearish) to +1.0 (max bullish).

    Returns 0.0 when headlines are unavailable, neutral, or insufficient.
    """
    if not isinstance(sent_data, dict) or sent_data.get("status") != "ok":
        return 0.0
    headlines = sent_data.get("headlines") or []
    if not headlines:
        return 0.0

    bull = 0
    bear = 0
    for h in headlines:
        text = f"{h.get('title', '')} {h.get('desc', '')}".lower()
        words = set(text.split())
        bull += len(words & _BULLISH)
        bear += len(words & _BEARISH)

    total = bull + bear
    if total == 0:
        return 0.0
    return round((bull - bear) / total, 3)


def _institutional_score(pos_data: dict) -> float:
    """Normalized institutional bias: −1.0 (max short) to +1.0 (max long).

    Derived from the COT net speculator position relative to its 1-year range,
    then multiplied by a momentum conviction modifier.

    Returns 0.0 when COT data is unavailable.
    """
    if not isinstance(pos_data, dict) or pos_data.get("status") != "ok":
        return 0.0

    net = pos_data.get("net_speculator_position")
    hi  = pos_data.get("one_year_high")
    lo  = pos_data.get("one_year_low")
    if net is None or hi is None or lo is None:
        return 0.0

    span = hi - lo
    if span < 1_000:  # <1k contracts — market too thin to be meaningful
        return 0.0

    # Normalize to −1..+1 within the 1-year range
    normalized = (net - lo) / span * 2 - 1

    # Momentum conviction modifier:
    #   BUILDING  → institutions increasing conviction  (+25%)
    #   STABLE    → no significant change               (no change)
    #   UNWINDING → institutions reducing position      (−35%)
    #   REVERSING → just crossed zero — still meaningful
    #               but reduced certainty about where they stabilise (−20%)
    mult = {
        "BUILDING":  1.25,
        "STABLE":    1.00,
        "UNWINDING": 0.65,
        "REVERSING": 0.80,
    }
    normalized *= mult.get(pos_data.get("cot_momentum", "STABLE"), 1.0)
    return round(max(-1.0, min(1.0, normalized)), 3)


def analyse(base_sentiment: dict, quote_sentiment: dict,
            base_positioning: dict, quote_positioning: dict) -> dict:
    """Compute Smart Money Divergence score for a currency pair.

    All arguments are the per-currency sub-dicts from the pipeline bundle:
      base_sentiment   = bundle["sentiment"]["base"]
      quote_sentiment  = bundle["sentiment"]["quote"]
      base_positioning = bundle["positioning"]["base"]
      quote_positioning= bundle["positioning"]["quote"]

    Returns
    -------
    smd_score        : int  −10 to +10
    signal           : str  STRONG_BUY|MODERATE_BUY|NEUTRAL|MODERATE_SELL|STRONG_SELL
    base_retail      : float  retail sentiment for base currency (−1 to +1)
    quote_retail     : float  retail sentiment for quote currency (−1 to +1)
    base_inst        : float  institutional bias for base currency (−1 to +1)
    quote_inst       : float  institutional bias for quote currency (−1 to +1)
    divergence_notes : list[str]  key observations for display
    status           : str  "ok" | "cot_only" | "sentiment_only" | "insufficient_data"
    """
    base_ret   = _retail_score(base_sentiment)
    quote_ret  = _retail_score(quote_sentiment)
    base_inst  = _institutional_score(base_positioning)
    quote_inst = _institutional_score(quote_positioning)

    cot_ok  = sum(1 for p in (base_positioning, quote_positioning)
                  if isinstance(p, dict) and p.get("status") == "ok")
    sent_ok = sum(1 for s in (base_sentiment, quote_sentiment)
                  if isinstance(s, dict) and s.get("status") == "ok"
                  and s.get("headlines"))

    if cot_ok == 0 and sent_ok == 0:
        return {
            "smd_score": 0, "signal": "NEUTRAL", "status": "insufficient_data",
            "base_retail": 0.0, "quote_retail": 0.0,
            "base_inst": 0.0, "quote_inst": 0.0,
            "divergence_notes": ["No COT or sentiment data available"],
        }

    # ── Pair-level divergence calculation ─────────────────────────────────────
    #
    # For a BUY on BASE/QUOTE we want:
    #   → institutions bullish on BASE (base_inst > 0)  while retail bearish (base_ret < 0)
    #   → institutions bearish on QUOTE (quote_inst < 0) while retail bullish (quote_ret > 0)
    #
    # smd_base  = base_inst − base_ret     (+2 = max BUY edge on base)
    # smd_quote = quote_inst − quote_ret   (−2 = max BUY edge on quote when subtracted)
    # pair_smd  = smd_base − smd_quote     range −4 to +4

    smd_base  = base_inst  - base_ret
    smd_quote = quote_inst - quote_ret
    pair_smd  = smd_base   - smd_quote      # −4 to +4

    # Additional BUILDING conviction bonus: if institutions are actively growing
    # their position on the more extreme side, add a small push to conviction
    conviction = 0.0
    for pos_data, sign in ((base_positioning, +1.0), (quote_positioning, -1.0)):
        if isinstance(pos_data, dict) and pos_data.get("cot_momentum") == "BUILDING":
            if (sign > 0 and base_inst > 0) or (sign < 0 and quote_inst > 0):
                conviction += 0.25 * sign
            elif (sign > 0 and base_inst < 0) or (sign < 0 and quote_inst < 0):
                conviction -= 0.25 * sign

    smd_raw   = pair_smd + conviction          # −4.5 to +4.5 approx
    smd_score = int(round(max(-10.0, min(10.0, smd_raw * (10.0 / 4.5)))))

    # ── Signal classification ──────────────────────────────────────────────────
    if smd_score >= 8:
        signal = "STRONG_BUY"
    elif smd_score >= 5:
        signal = "MODERATE_BUY"
    elif smd_score <= -8:
        signal = "STRONG_SELL"
    elif smd_score <= -5:
        signal = "MODERATE_SELL"
    else:
        signal = "NEUTRAL"

    # ── Narrative notes ────────────────────────────────────────────────────────
    base_ccy  = (base_positioning  if isinstance(base_positioning,  dict) else {}).get("currency", "BASE")
    quote_ccy = (quote_positioning if isinstance(quote_positioning, dict) else {}).get("currency", "QUOTE")

    def _label(score: float) -> str:
        if score >  0.40: return "VERY BULLISH"
        if score >  0.15: return "mildly bullish"
        if score < -0.40: return "VERY BEARISH"
        if score < -0.15: return "mildly bearish"
        return "neutral"

    notes = []
    if cot_ok > 0:
        notes.append(
            f"{base_ccy}: institutions {_label(base_inst)}"
            + (f" ({_label(base_ret)} retail)" if sent_ok > 0 else "")
        )
        notes.append(
            f"{quote_ccy}: institutions {_label(quote_inst)}"
            + (f" ({_label(quote_ret)} retail)" if sent_ok > 0 else "")
        )

    if abs(smd_score) >= 8:
        if smd_score > 0:
            notes.append("Institutions building longs vs retail skepticism — contrarian BUY edge")
        else:
            notes.append("Institutions building shorts vs retail optimism — contrarian SELL edge")
    elif abs(smd_score) >= 5:
        if smd_score > 0:
            notes.append("Moderate institutional/retail divergence — mild BUY lean")
        else:
            notes.append("Moderate institutional/retail divergence — mild SELL lean")

    # Flag REVERSING momentum — this is especially important context
    for pd, ccy in ((base_positioning, base_ccy), (quote_positioning, quote_ccy)):
        if isinstance(pd, dict) and pd.get("cot_momentum") == "REVERSING":
            notes.append(f"⚠️ {ccy} COT REVERSING — institutions just flipped positioning")

    if cot_ok == 0:
        status = "sentiment_only"
    elif sent_ok == 0:
        status = "cot_only"
    else:
        status = "ok"

    return {
        "smd_score":        smd_score,
        "signal":           signal,
        "status":           status,
        "base_retail":      base_ret,
        "quote_retail":     quote_ret,
        "base_inst":        base_inst,
        "quote_inst":       quote_inst,
        "divergence_notes": notes,
    }
