"""Sentiment Agent -- Phase 01B's third specialist (2026-09-06).

Unlike Positioning (src/positioning.py) and Carry/Macro, this one skipped
Step 1 entirely by design: there is no historical corpus of "hawkishness
scores" to backtest against (the score doesn't exist until an LLM reads the
text), and asking an LLM to score OLD news risks it pattern-matching on
outcomes it already knows from training -- exactly the contamination risk
Phase 02 flagged for the technical layer's LLM judgment. This module's only
honest validation path is a live, forward-only shadow-mode trial -- see
shadow_mode.py's registration of "sentiment_agent_supports" for the
pre-committed promotion bar, registered before a single live evaluation.

============================================================================
SOURCE / TIMESTAMP DISCIPLINE -- READ BEFORE CHANGING ANYTHING HERE
============================================================================
Every function in this module that fetches news is HARD-CODED to only ever
request articles published today (UTC) or later, with no parameter anywhere
that accepts a caller-supplied date. This is deliberate: `_determine_outcome`-
style `as_of` overrides exist elsewhere in this codebase specifically to let
a backtest replay historical dates -- that pattern is exactly what must NOT
exist here, so nobody (including a future well-meaning refactor) can point
this module at a past date, on purpose or by accident.

Mechanism (defense in depth, not just one check):
  1. NewsAPI's own `from` param is set to today's UTC date on every request
     -- the API itself refuses to return anything older, not just a sort
     hint.
  2. Every returned article is re-checked in Python against today's UTC
     midnight and dropped if it somehow predates it anyway.
  3. `evaluate()`'s return value includes `evaluated_at` (wall-clock time of
     the call) and `oldest_headline_published`/`newest_headline_published`
     (the actual publish timestamps of what was fed to the LLM) specifically
     so a later audit can directly confirm, from persisted research_trades.csv
     fields, that nothing scored was stale or backward-looking -- without
     having to trust this module's own claim about it.

The disk cache (src/cache.py) key includes today's date, so a scan run later
the same day reuses same-day news (cheap, correct) but a scan on a
subsequent day can never silently serve yesterday's cached headlines as if
they were fresh.

============================================================================
MODEL CHOICE -- WHY HAIKU, NOT SONNET
============================================================================
devil_advocate() (src/analyst.py) uses Sonnet because it's a nuanced second
opinion on a specific proposed trade's structure. This is a narrower,
more mechanical classification task -- "is this handful of today's
headlines hawkish, dovish, or neutral for this currency, and does that
support or contradict this candidate's direction" -- squarely within a
fast/cheap model's competence, and this needs to run once per research
candidate (~35-40/day at current volume), not once per already-filtered
high-confidence setup like Devil's Advocate. Haiku costs ~4x less on input
and ~3.75x less on output than Sonnet (see src/analyst.py's H_IN/H_OUT vs
S_IN/S_OUT) -- the right tradeoff for a call this narrow and this frequent.

============================================================================
FAILS OPEN, NEVER BLOCKS
============================================================================
Any failure (no headlines, NewsAPI down, LLM call error, unparseable
response) returns verdict="UNAVAILABLE", confidence=0 -- mirrors
devil_advocate()'s own fail-open convention. This is pure observability;
nothing downstream may ever treat a Sentiment Agent failure as a reason to
block or alter a real decision.
"""
import sys
from datetime import datetime, timezone

import requests
from anthropic import Anthropic

import config
from src import cache
from src.analyst import _cost, _call_api  # reuse the real cost tracker + key-fallback

_NEWS_URL = "https://newsapi.org/v2/everything"
_TIMEOUT = 30
_MAX_HEADLINES_PER_CCY = 8
_FIRE_CONFIDENCE_MIN = 6   # matches this system's usual "meaningful confidence" bar

VERDICTS = ("SUPPORTS", "CONTRADICTS", "NEUTRAL", "UNAVAILABLE")

# In-process cache: one LLM call per (pair, direction) per scan run, no matter
# which caller (research-trade logging, or the virtual book's eligibility
# check) asks first. Each scan is a fresh process (GitHub Actions), so this
# naturally resets every run -- no explicit date key needed here (the disk
# news cache below is the one that must be date-keyed, since IT persists
# across scan runs within the same day).
_verdict_cache: dict = {}


def _today_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _today_utc_midnight():
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _recent_headlines_for(ccy: str) -> list:
    """Headlines for one currency, published today (UTC) or later ONLY.
    No parameter accepts a caller-supplied date -- see module docstring."""
    meta = config.CURRENCIES.get(ccy, {})
    terms = meta.get("news_terms") or ccy
    today = _today_utc_str()
    key = f"SENTIMENT_NEWS:{ccy}:{today}"   # date-keyed: a later day never reuses today's cache
    cached = cache.get(key, ttl_hours=6.0)
    if cached is None:
        try:
            resp = requests.get(
                _NEWS_URL,
                params={
                    "q": terms,
                    "language": "en",
                    "sortBy": "publishedAt",
                    "pageSize": _MAX_HEADLINES_PER_CCY,
                    "from": today,   # API-level enforcement, not just a sort hint
                    "apiKey": config.NEWS_API_KEY,
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
            cached = [
                {
                    "title": a.get("title"),
                    "source": (a.get("source") or {}).get("name"),
                    "published": a.get("publishedAt"),
                }
                for a in payload.get("articles", [])
            ]
            cache.set(key, cached)
        except Exception as exc:  # noqa: BLE001
            print(f"[sentiment_agent] news fetch failed for {ccy}: {exc}", file=sys.stderr)
            return []

    # Defense in depth: re-check every article against today's UTC midnight,
    # even though the API's own `from` param should have already enforced
    # this -- never trust a single layer for the one guarantee this module
    # exists to make.
    midnight = _today_utc_midnight()
    out = []
    for a in cached:
        pub = a.get("published")
        try:
            pub_dt = datetime.fromisoformat((pub or "").replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue  # unparseable timestamp -- can't verify it's not stale, so drop it
        if pub_dt >= midnight:
            out.append(a)
    return out


def _build_prompt(pair: str, direction: str, base: str, quote: str,
                   base_headlines: list, quote_headlines: list) -> tuple:
    def _fmt(ccy, headlines):
        if not headlines:
            return f"{ccy}: no headlines published today."
        lines = [f"{ccy} headlines (published today):"]
        for h in headlines:
            lines.append(f"  - [{h.get('source', '?')}] {h.get('title', '')}")
        return "\n".join(lines)

    system_prompt = (
        "You are a macro/central-bank-language sentiment reader for forex trading. "
        "Given today's news headlines only, classify whether each currency's "
        "central-bank/policy tone is drifting HAWKISH, DOVISH, or NEUTRAL, based "
        "strictly on what the headlines say -- not on what you recall about this "
        "currency's history or expected future path. If the headlines don't "
        "clearly support a reading, say NEUTRAL rather than guessing. "
        "Output only the structured format requested -- nothing else."
    )
    user_message = (
        f"Candidate trade: {pair} {direction}\n\n"
        f"{_fmt(base, base_headlines)}\n\n"
        f"{_fmt(quote, quote_headlines)}\n\n"
        f"For a {direction} on {pair} (buying {base}, selling {quote} if BUY; "
        f"the reverse if SELL), does today's news flow support, contradict, or "
        f"say nothing about this trade's direction? A hawkish drift in the "
        f"currency being bought, or a dovish drift in the currency being sold, "
        f"SUPPORTS a BUY (and the mirror image for a SELL).\n\n"
        f"BASE_DRIFT: HAWKISH, DOVISH, or NEUTRAL\n"
        f"QUOTE_DRIFT: HAWKISH, DOVISH, or NEUTRAL\n"
        f"VERDICT: SUPPORTS, CONTRADICTS, or NEUTRAL\n"
        f"CONFIDENCE: 0-10 (0 if NEUTRAL or no real headlines to go on)\n"
        f"REASON: [one plain-English sentence]"
    )
    return system_prompt, user_message


def _parse_response(text: str) -> dict:
    out = {"base_drift": "NEUTRAL", "quote_drift": "NEUTRAL",
           "verdict": "NEUTRAL", "confidence": 0, "reason": ""}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("BASE_DRIFT:"):
            v = line.split(":", 1)[1].strip().upper()
            if v in ("HAWKISH", "DOVISH", "NEUTRAL"):
                out["base_drift"] = v
        elif line.startswith("QUOTE_DRIFT:"):
            v = line.split(":", 1)[1].strip().upper()
            if v in ("HAWKISH", "DOVISH", "NEUTRAL"):
                out["quote_drift"] = v
        elif line.startswith("VERDICT:"):
            v = line.split(":", 1)[1].strip().upper()
            if v in ("SUPPORTS", "CONTRADICTS", "NEUTRAL"):
                out["verdict"] = v
        elif line.startswith("CONFIDENCE:"):
            try:
                out["confidence"] = max(0, min(10, int(float(line.split(":", 1)[1].strip()))))
            except (ValueError, IndexError):
                pass
        elif line.startswith("REASON:"):
            out["reason"] = line.split(":", 1)[1].strip()
    return out


def evaluate(pair: str, direction: str) -> dict:
    """Raw evaluation -- always makes an LLM call (if headlines exist).
    Prefer get_or_evaluate() from call sites so a candidate is never scored
    twice in the same scan."""
    evaluated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        base, quote = pair.split("/")
    except ValueError:
        return {"verdict": "UNAVAILABLE", "confidence": 0,
                "reason": f"malformed pair {pair!r}", "evaluated_at": evaluated_at}

    base_headlines = _recent_headlines_for(base)
    quote_headlines = _recent_headlines_for(quote)
    all_published = [
        h.get("published") for h in (base_headlines + quote_headlines) if h.get("published")
    ]

    if not base_headlines and not quote_headlines:
        return {
            "verdict": "UNAVAILABLE", "confidence": 0,
            "reason": f"no today-dated news for {base} or {quote}",
            "evaluated_at": evaluated_at,
            "oldest_headline_published": "", "newest_headline_published": "",
            "n_headlines": 0,
        }

    system_prompt, user_message = _build_prompt(
        pair, direction, base, quote, base_headlines, quote_headlines
    )

    try:
        def _call(client):
            return client.messages.create(
                model=config.HAIKU_MODEL,
                max_tokens=200,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
        resp = _call_api(_call)
        _cost["haiku_input"]  += getattr(resp.usage, "input_tokens", 0)
        _cost["haiku_output"] += getattr(resp.usage, "output_tokens", 0)
        text = "".join(block.text for block in resp.content if block.type == "text")
        parsed = _parse_response(text)
    except Exception as exc:  # noqa: BLE001 -- fail open, never block the pipeline
        print(f"[sentiment_agent] LLM call failed for {pair} {direction}: {exc}", file=sys.stderr)
        return {
            "verdict": "UNAVAILABLE", "confidence": 0, "reason": f"LLM call failed: {exc}",
            "evaluated_at": evaluated_at,
            "oldest_headline_published": min(all_published) if all_published else "",
            "newest_headline_published": max(all_published) if all_published else "",
            "n_headlines": len(base_headlines) + len(quote_headlines),
        }

    parsed.update({
        "evaluated_at": evaluated_at,
        "oldest_headline_published": min(all_published) if all_published else "",
        "newest_headline_published": max(all_published) if all_published else "",
        "n_headlines": len(base_headlines) + len(quote_headlines),
    })
    return parsed


def get_or_evaluate(pair: str, direction: str) -> dict:
    """Memoizing wrapper -- the one call sites should actually use. Ensures
    at most one LLM call per (pair, direction) per scan process, regardless
    of whether research-trade logging or the virtual book's eligibility
    check happens to run first."""
    key = (pair, (direction or "").upper())
    if key not in _verdict_cache:
        _verdict_cache[key] = evaluate(pair, direction)
    return _verdict_cache[key]


def would_fire(result: dict) -> bool:
    """Shadow-mode / virtual-book boolean: did this evaluation come back as
    genuine, confident support for the candidate's own stated direction?
    UNAVAILABLE and NEUTRAL are both would_fire=False -- an absent or
    non-committal read is not evidence for the trade."""
    return result.get("verdict") == "SUPPORTS" and result.get("confidence", 0) >= _FIRE_CONFIDENCE_MIN
