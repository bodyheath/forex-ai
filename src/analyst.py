"""The Claude analysis layer.

Loads the analyst system prompt, assembles the gathered evidence + system memory
into a user message, and calls Claude. The system prompt is sent as a cached block
(prompt caching) so repeated runs in a session are cheaper and faster.
"""

import json

from anthropic import Anthropic

import config
from src import memory


def _load_system_prompt() -> str:
    return config.PROMPT_FILE.read_text(encoding="utf-8")


def _build_user_message(pair: str, bundle: dict) -> str:
    parts = [
        memory.render(),
        "",
        f"Analyse the currency pair: {pair}",
        "",
        "Below is the evidence gathered from independent live data sources. Any layer "
        "marked UNAVAILABLE or PARTIAL must NOT be counted as an agreeing source - treat "
        "missing data as a reason for caution. All prices are quote currency per 1 unit "
        "of base currency.",
        "",
        "=== TECHNICAL (Twelve Data, indicators computed locally) ===",
        json.dumps(bundle.get("technical", {}), indent=2),
        "",
        "=== FUNDAMENTAL (FRED policy rates) ===",
        json.dumps(bundle.get("fundamental", {}), indent=2),
        "",
        "=== SENTIMENT (NewsAPI headlines - assess tone yourself) ===",
        json.dumps(bundle.get("sentiment", {}), indent=2),
        "",
        "=== POSITIONING (CFTC Commitments of Traders) ===",
        json.dumps(bundle.get("positioning", {}), indent=2),
        "",
        "=== MACRO CONTEXT (FRED: oil, yields, VIX, dollar index) ===",
        json.dumps(bundle.get("macro", {}), indent=2),
        "",
        "Do your reasoning silently. Respond with ONLY the OUTPUT FORMAT fields from "
        "your instructions: begin the very first line with 'PAIR:' and end with the "
        "'TRADE_THIS:' line. Do NOT include any preamble, internal working, section "
        "headings, markdown, or commentary before or after those fields. Be a ruthless "
        "sceptic. The user is in New Zealand - give BEST_ENTRY_TIME in NZT.",
    ]
    return "\n".join(parts)


_SCREEN_SYSTEM = (
    "You are a fast forex pre-screener. "
    "Given only technical indicators and interest-rate fundamentals for a currency pair, "
    "output a score from 1 to 5:\n"
    "1 = No signal, flat/choppy — skip\n"
    "2 = Weak or contradictory signals — likely skip\n"
    "3 = Mixed, borderline\n"
    "4 = Clear directional bias from tech+fundamentals — worth deep analysis\n"
    "5 = Strong confluence — high priority\n\n"
    "Respond with EXACTLY two lines:\n"
    "SCORE: <integer 1-5>\n"
    "REASON: <one sentence>"
)


def screen(pair: str, bundle: dict) -> dict:
    """Stage-1 screener: Haiku scores pair 1-5 using only technical+fundamental.
    Fails open (score=5) on any API/parse error so deep analysis still runs."""
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    user_msg = "\n".join([
        f"Currency pair: {pair}",
        "",
        "=== TECHNICAL ===",
        json.dumps(bundle.get("technical", {}), indent=2),
        "",
        "=== FUNDAMENTAL (interest rates) ===",
        json.dumps(bundle.get("fundamental", {}), indent=2),
    ])
    try:
        resp = client.messages.create(
            model=config.HAIKU_MODEL,
            max_tokens=100,
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
        max_tokens=3000,  # headroom so the structured block is never truncated
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
