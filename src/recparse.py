"""Parse the analyst's free-text OUTPUT FORMAT into structured fields.

The analyst returns labelled lines (PAIR:, DIRECTION:, CONFIDENCE: ...). Some
fields span multiple lines (KEY_THESIS, RISK_FACTORS) and some carry explanatory
text after the value (e.g. "ENTRY: 0.8655 (sell into ...)"). This parser walks the
text, groups lines under the most recent recognised label, then extracts numbers
where it makes sense. It is deliberately forgiving: anything it cannot parse comes
back as None rather than raising.
"""

import re

LABELS = [
    "PAIR", "DIRECTION", "CONFIDENCE",
    "TECHNICAL_SCORE", "FUNDAMENTAL_SCORE", "SENTIMENT_SCORE",
    "POSITIONING_SCORE", "MACRO_SCORE",
    "DIVERGENCE", "OSCILLATOR_CONFLUENCE",
    "KEY_THESIS", "RISK_FACTORS",
    "ENTRY", "TARGET", "STOP_LOSS", "REWARD_RISK_RATIO",
    "BEST_ENTRY_TIME", "NEWS_WARNING", "TRADE_THIS",
]
# Tolerate leading markdown (**, #, -, >) and bold-wrapped labels like
# "**CONFIDENCE:** 4/10". Labels are >=2 uppercase letters/underscores.
_LABEL_RE = re.compile(r"^[\s>#*\-]*([A-Z][A-Z_]+)\s*:\**\s*(.*)$")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
# FX price levels are always decimals (1.16354, 159.75, 0.8655). Requiring a
# decimal point avoids grabbing stray integers like "4H", "S1" or "20-day".
_PRICE_RE = re.compile(r"\d+\.\d+")


def _raw_fields(report: str) -> dict:
    lines = report.splitlines()

    # Anchor to the output block: start at the LAST 'PAIR:' label so any
    # preamble or internal working above it is ignored.
    start = 0
    for i, line in enumerate(lines):
        m = _LABEL_RE.match(line)
        if m and m.group(1) == "PAIR":
            start = i
    lines = lines[start:]

    fields = {label: "" for label in LABELS}
    current = None
    for line in lines:
        m = _LABEL_RE.match(line)
        if m and m.group(1) in LABELS:
            current = m.group(1)
            fields[current] = m.group(2).strip().strip("*").strip()
        elif current is not None and line.strip():
            fields[current] += " " + line.strip()
    return fields


def _first_number(text: str):
    m = _NUM_RE.search(text or "")
    return float(m.group()) if m else None


def _first_price(text: str):
    """First decimal number in the text, or None. Used for entry/target/stop so
    that integers embedded in prose ('4H', 'S1', '20-day low') are ignored."""
    m = _PRICE_RE.search(text or "")
    return float(m.group()) if m else None


def _score(text: str):
    """Return an int 1-10, or None if the layer was UNAVAILABLE."""
    if not text or "UNAVAILABLE" in text.upper():
        return None
    n = _first_number(text)
    return int(n) if n is not None else None


def _reward_risk(text: str):
    m = re.search(r"(\d+(?:\.\d+)?)\s*:\s*1", text or "")
    return float(m.group(1)) if m else None


def _direction(text: str):
    t = (text or "").upper()
    # Prefer an explicit leading BUY/SELL; otherwise the first one mentioned.
    m = re.search(r"\b(BUY|SELL)\b", t)
    return m.group(1) if m else "NONE"


def _trade_this(text: str):
    return "YES" if (text or "").strip().upper().startswith("YES") else "NO"


def parse(report: str) -> dict:
    f = _raw_fields(report)
    return {
        "pair": f["PAIR"] or None,
        "direction": _direction(f["DIRECTION"]),
        "direction_raw": f["DIRECTION"] or None,
        "confidence": _score(f["CONFIDENCE"]),
        "technical_score": _score(f["TECHNICAL_SCORE"]),
        "fundamental_score": _score(f["FUNDAMENTAL_SCORE"]),
        "sentiment_score": _score(f["SENTIMENT_SCORE"]),
        "positioning_score": _score(f["POSITIONING_SCORE"]),
        "macro_score": _score(f["MACRO_SCORE"]),
        "entry": _first_price(f["ENTRY"]),
        "target": _first_price(f["TARGET"]),
        "stop_loss": _first_price(f["STOP_LOSS"]),
        "reward_risk": _reward_risk(f["REWARD_RISK_RATIO"]),
        "trade_this": _trade_this(f["TRADE_THIS"]),
        "divergence":            (f.get("DIVERGENCE")            or "").strip().upper() or None,
        "oscillator_confluence": (f.get("OSCILLATOR_CONFLUENCE") or "").strip().upper() or None,
        "key_thesis": f["KEY_THESIS"] or None,
        "risk_factors": f["RISK_FACTORS"] or None,
        "news_warning": f["NEWS_WARNING"] or None,
        "best_entry_time": f["BEST_ENTRY_TIME"] or None,
    }
