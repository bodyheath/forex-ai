"""The Claude analysis layer.

Loads the analyst system prompt, assembles the gathered evidence + system memory
into a user message, and calls Claude. The system prompt is sent as a cached block
(prompt caching) so repeated runs in a session are cheaper and faster.

Cost design:
  Stage 1 — Haiku screens on slim technical + fundamental only (~250 input tokens).
  Stage 2 — Sonnet receives a slim bundle (~1,400 input tokens) for pairs scoring 4+.
  Both stages use prompt caching on the system content.

Fallback key:
  If the primary ANTHROPIC_API_KEY fails with an authentication or credit error,
  _call_api() automatically switches to ANTHROPIC_API_KEY_2 for all remaining calls
  in the run.  key_status() returns a human-readable label for the Telegram message
  so you can see which account is being billed.
"""

import json

from anthropic import Anthropic

import config
from src import memory

# ── API key fallback state ─────────────────────────────────────────────────────
# Module-level flag: once the primary key fails, every subsequent call this run
# uses the fallback key.  reset_key_state() is called at the start of each run.

_using_fallback: bool = False      # True once primary key has failed
_fallback_triggered: bool = False  # True if we ever switched (for status reporting)


def reset_key_state() -> None:
    """Reset fallback state at the start of each daily run."""
    global _using_fallback, _fallback_triggered
    _using_fallback      = False
    _fallback_triggered  = False


def key_status() -> str:
    """Return a Telegram-ready label showing which API key(s) were used."""
    if not config.ANTHROPIC_API_KEY_2:
        return "🔑 API: primary key (no fallback configured)"
    if _fallback_triggered:
        return "⚠️ API: switched to backup key (KEY_2) — primary key failed"
    return "🔑 API: primary key (KEY_1)"


def _is_auth_or_credit_error(exc: Exception) -> bool:
    """Return True for errors that indicate the key is invalid or has no credit."""
    try:
        import anthropic as _anth
        if isinstance(exc, (_anth.AuthenticationError, _anth.PermissionDeniedError)):
            return True
    except Exception:
        pass
    status = getattr(exc, "status_code", None)
    if status in (401, 402, 403):
        return True
    msg = str(exc).lower()
    return any(kw in msg for kw in (
        "authentication_error", "invalid x-api-key", "invalid_api_key",
        "credit", "balance", "billing", "payment required",
    ))


def _call_api(fn):
    """Call fn(client) → result, switching to fallback key on auth/credit error.

    fn receives an Anthropic client and must return the API response.
    On first auth/credit failure the module switches to ANTHROPIC_API_KEY_2
    for this and all remaining calls in the run.
    """
    global _using_fallback, _fallback_triggered

    key = config.ANTHROPIC_API_KEY_2 if _using_fallback else config.ANTHROPIC_API_KEY
    client = Anthropic(api_key=key)

    try:
        return fn(client)
    except Exception as exc:
        # Only attempt fallback if: not already on fallback, fallback exists, and
        # the error is the kind that a different key would fix.
        if _using_fallback or not config.ANTHROPIC_API_KEY_2 or not _is_auth_or_credit_error(exc):
            raise

    # Switch permanently to the fallback key for the rest of this run.
    _using_fallback     = True
    _fallback_triggered = True
    client2 = Anthropic(api_key=config.ANTHROPIC_API_KEY_2)
    return fn(client2)


# ── Bundle trimming helpers ────────────────────────────────────────────────────

def _load_system_prompt() -> str:
    return config.PROMPT_FILE.read_text(encoding="utf-8")


def _slim_bundle(bundle: dict) -> dict:
    """Return a token-efficient version of the data bundle for the Sonnet prompt.

    Strips metadata fields (FRED series IDs, as_of dates, historical extremes,
    source strings) that do not improve the analytical output.  Sentiment
    headlines are capped at 8 and descriptions at 80 characters.
    """
    slim: dict = {"technical": bundle.get("technical", {})}

    fund = bundle.get("fundamental", {})
    slim_fund: dict = {}
    for k, v in fund.items():
        if k not in ("base", "quote"):
            slim_fund[k] = v
    for side in ("base", "quote"):
        r = fund.get(side, {})
        slim_fund[side] = {k: v for k, v in r.items() if k not in ("fred_series", "as_of")}
    slim["fundamental"] = slim_fund

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

    pos = bundle.get("positioning", {})
    slim_pos: dict = {}
    _POS_KEEP = ("currency", "status", "report_date", "direction",
                 "net_speculator_position", "percentile_in_range", "extreme_flag")
    for side in ("base", "quote"):
        p = pos.get(side, {})
        slim_pos[side] = {k: p[k] for k in _POS_KEEP if k in p}
    slim["positioning"] = slim_pos

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
    """Return (tech_json, fund_json) stripped to just what Haiku needs for a score."""
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
        "No preamble, no commentary. "
        "Give BEST_ENTRY_TIME as a session name and time range in Auckland time "
        "(New Zealand — UTC+13 NZDT in summer Sep–Apr, UTC+12 NZST in winter Apr–Sep).",
    ]
    return "\n".join(parts)


# ── Stage-1 screener (Haiku) ───────────────────────────────────────────────────

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

    def _call(client):
        return client.messages.create(
            model=config.HAIKU_MODEL,
            max_tokens=80,
            system=_SCREEN_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )

    try:
        resp = _call_api(_call)
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


# ── Stage-2 deep analysis (Sonnet) ────────────────────────────────────────────

def analyse(pair: str, bundle: dict) -> str:
    system_prompt = _load_system_prompt()
    user_message  = _build_user_message(pair, bundle)

    def _call(client):
        return client.messages.create(
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

    resp = _call_api(_call)
    return "".join(block.text for block in resp.content if block.type == "text")
