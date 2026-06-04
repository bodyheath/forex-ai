"""The Claude analysis layer.

Loads the analyst system prompt, assembles the gathered evidence + system memory
into a user message, and calls Claude. The system prompt is sent as a cached block
(prompt caching) so repeated runs in a session are cheaper and faster.

Cost design:
  Stage 1 — Haiku screens on slim technical + fundamental only (~250 input tokens).
  Stage 2 — Sonnet receives a slim bundle (~1,400 input tokens) for pairs scoring 4+.
  Both stages use prompt caching on the system content.
"""

import json

from anthropic import Anthropic

import config
from src import memory


def _load_system_prompt() -> str:
    return config.PROMPT_FILE.read_text(encoding="utf-8")


def _slim_bundle(bundle: dict) -> dict:
    """Return a token-efficient version of the data bundle for the Sonnet prompt.

    Strips metadata fields (FRED series IDs, as_of dates, historical extremes,
    source strings) that do not improve the analytical output.  Sentiment
    headlines are capped at 8 and descriptions at 80 characters.
    """
    # Technical: _summarise() already returns compact indicator dicts — pass through.
    slim: dict = {"technical": bundle.get("technical", {})}

    # Fundamental: drop fred_series and as_of dates.
    fund = bundle.get("fundamental", {})
    slim_fund: dict = {}
    for k, v in fund.items():
        if k not in ("base", "quote"):
            slim_fund[k] = v
    for side in ("base", "quote"):
        r = fund.get(side, {})
        slim_fund[side] = {k: v for k, v in r.items() if k not in ("fred_series", "as_of")}
    slim["fundamental"] = slim_fund

    # Sentiment: cap headlines at 8, trim descriptions, drop source field.
    sent = bundle.get("sentiment", {})
    slim_sent: dict = {}
    for side in ("base", "quote"):
        s = sent.get(side, {})
        if s.get("status") == "ok":
            slim_sent[side] = {
                "currency":      s.get("currency"),
                "status":        "ok",
                "article_count": s.get("article_count"),
                "headlines": [
                    {
                        "title":     h.get("title"),
                        "published": (h.get("published") or "")[:10],
                        "desc":      (h.get("desc") or "")[:80],
                    }
                    for h in (s.get("headlines") or [])[:8]
                ],
            }
        else:
            slim_sent[side] = {k: s[k] for k in ("currency", "status", "error") if k in s}
    slim["sentiment"] = slim_sent

    # Positioning: drop raw historical extremes and matched_market string.
    pos = bundle.get("positioning", {})
    slim_pos: dict = {}
    _POS_KEEP = ("currency", "status", "report_date", "direction",
                 "net_speculator_position", "percentile_in_range", "extreme_flag")
    for side in ("base", "quote"):
        p = pos.get(side, {})
        slim_pos[side] = {k: p[k] for k in _POS_KEEP if k in p}
    slim["positioning"] = slim_pos

    # Macro: drop as_of from each signal — values and trends are what matter.
    macro = bundle.get("macro", {})
    slim_signals: dict = {}
    for label, data in (macro.get("signals") or {}).items():
        if isinstance(data, dict):
            slim_signals[label] = {k: v for k, v in data.items() if k != "as_of"}
        else:
            slim_signals[label] = data
    slim["macro"] = {"status": macro.get("status"), "signals": slim_signals}

    return slim


def _slim_for_screen(bundle: dict) -> tuple:
    """Return (tech_json, fund_json) stripped to just what Haiku needs for a score.

    Haiku only sees daily indicators and the rate differential — no 4H data,
    no headlines, no COT, no macro.  Cuts screener input by ~70 %.
    """
    tech  = bundle.get("technical", {})
    daily = tech.get("daily", {})
    if daily:
        slim_tech = {
            "trend":          daily.get("trend"),
            "rsi14":          daily.get("rsi14"),
            "macd":           daily.get("macd"),
            "bollinger":      daily.get("bollinger"),
            "sma50":          daily.get("sma50"),
            "sma200":         daily.get("sma200"),
            "atr14":          daily.get("atr14"),
            "recent_high_20": daily.get("recent_high_20"),
            "recent_low_20":  daily.get("recent_low_20"),
        }
    else:
        slim_tech = {"status": tech.get("status", "UNAVAILABLE")}

    fund = bundle.get("fundamental", {})
    slim_fund = {
        "rate_differential_pct": fund.get("rate_differential_pct"),
        "carry_note":            fund.get("carry_note"),
        "base_rate_trend":       (fund.get("base") or {}).get("rate_trend"),
        "quote_rate_trend":      (fund.get("quote") or {}).get("rate_trend"),
    }

    return json.dumps(slim_tech, indent=2), json.dumps(slim_fund, indent=2)


def _build_user_message(pair: str, bundle: dict) -> str:
    slim = _slim_bundle(bundle)
    parts = [
        memory.render(),
        "",
        f"Analyse the currency pair: {pair}",
        "",
        "Evidence from independent live data sources follows. "
        "UNAVAILABLE layers do not count as agreeing sources. "
        "All prices are quote currency per 1 unit of base currency.",
        "",
        "=== TECHNICAL ===",
        json.dumps(slim["technical"], indent=2),
        "",
        "=== FUNDAMENTAL ===",
        json.dumps(slim["fundamental"], indent=2),
        "",
        "=== SENTIMENT ===",
        json.dumps(slim["sentiment"], indent=2),
        "",
        "=== POSITIONING (COT) ===",
        json.dumps(slim["positioning"], indent=2),
        "",
        "=== MACRO CONTEXT ===",
        json.dumps(slim["macro"], indent=2),
        "",
        "Respond with ONLY the OUTPUT FORMAT fields. "
        "Start with PAIR: and end with TRADE_THIS:. "
        "No preamble, no commentary. Give BEST_ENTRY_TIME in NZT.",
    ]
    return "\n".join(parts)


_SCREEN_SYSTEM = (
    "You are a fast forex pre-screener. "
    "Score 1–5 based only on the technical indicators and rate differential provided:\n"
    "1 = no signal / flat / choppy\n"
    "2 = weak or contradictory signals\n"
    "3 = mixed / borderline\n"
    "4 = clear directional bias — worth deep analysis\n"
    "5 = strong confluence — high priority\n\n"
    "Respond with EXACTLY two lines:\n"
    "SCORE: <integer 1-5>\n"
    "REASON: <one sentence>"
)


def screen(pair: str, bundle: dict) -> dict:
    """Stage-1 screener: Haiku scores pair 1–5 on slim tech + fundamental only.
    Fails open (score=5) on any API or parse error so deep analysis still runs."""
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    tech_json, fund_json = _slim_for_screen(bundle)
    user_msg = "\n".join([
        f"Currency pair: {pair}",
        "",
        "=== TECHNICAL (daily indicators) ===",
        tech_json,
        "",
        "=== FUNDAMENTAL (rate differential) ===",
        fund_json,
    ])
    try:
        resp = client.messages.create(
            model=config.HAIKU_MODEL,
            max_tokens=80,
            system=_SCREEN_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception:  # noqa: BLE001
        return {"score": 5, "reason": "screener unavailable — failing open"}

    score, reason = 5, text
    for line in text.splitlines():
        if line.startswith("SCORE:"):
            try:
                score = max(1, min(5, int(line.split(":", 1)[1].strip())))
            except ValueError:
                pass
        elif line.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
    return {"score": score, "reason": reason}


def analyse(pair: str, bundle: dict) -> str:
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    system_prompt = _load_system_prompt()
    user_message = _build_user_message(pair, bundle)

    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=800,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )
    return "".join(block.text for block in resp.content if block.type == "text")
