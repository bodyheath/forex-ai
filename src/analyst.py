"""The Claude analysis layer.

Stage 1  — Haiku screens on slim technical + fundamental only (~250 input tokens).
Stage 1b — Haiku generates KEY_THESIS and RISK_FACTORS (~200 tokens) for pairs
           that pass screening, saving ~120 Sonnet output tokens per pair.
Stage 2  — Sonnet receives the slim bundle + pre-generated thesis (~800 input tokens)
           and outputs scores, entry/exit levels, and confidence only.
Both stages use prompt caching on the system content.

Fallback key:
  If the primary ANTHROPIC_API_KEY fails, _call_api() switches to KEY_2 for all
  remaining calls in the run.  key_status() returns a label for the Telegram message.
"""

import json
import time

from anthropic import Anthropic

import config
from src import memory

# ── API key fallback state ─────────────────────────────────────────────────────
_using_fallback: bool    = False
_fallback_triggered: bool = False

# ── Run-level cost tracker ─────────────────────────────────────────────────────
_cost: dict = {
    "haiku_input":  0,
    "haiku_output": 0,
    "sonnet_input": 0,
    "sonnet_output": 0,
    "cache_hits":   0,   # pairs skipped because price didn't move
}

# ── Skip-unchanged cache ───────────────────────────────────────────────────────
# Stores the last analysis price and report per pair so we can skip re-analysis
# when price moved < 15 pips since the last run (within 6 hours).
_SKIP_TTL_HOURS = 6.0
_SKIP_PIPS      = 15
_analysis_cache: dict = {}  # pair → {ts, price, report, thesis, risk}


def reset_key_state() -> None:
    """Reset fallback and cost state at the start of each daily run."""
    global _using_fallback, _fallback_triggered
    _using_fallback     = False
    _fallback_triggered = False
    for k in _cost:
        _cost[k] = 0
    _analysis_cache.clear()


def key_status() -> str:
    if not config.ANTHROPIC_API_KEY_2:
        return "🔑 API: primary key (no fallback configured)"
    if _fallback_triggered:
        return "⚠️ API: switched to backup key (KEY_2) — primary key failed"
    return "🔑 API: primary key (KEY_1)"


def get_run_stats() -> dict:
    """Return cost tracker snapshot with estimated USD cost."""
    # Pricing (USD per 1M tokens) — Haiku 4.5 / Sonnet 4.6
    H_IN, H_OUT = 0.80, 4.00
    S_IN, S_OUT = 3.00, 15.00
    est = (
        _cost["haiku_input"]  / 1_000_000 * H_IN  +
        _cost["haiku_output"] / 1_000_000 * H_OUT +
        _cost["sonnet_input"] / 1_000_000 * S_IN  +
        _cost["sonnet_output"]/ 1_000_000 * S_OUT
    )
    return {**_cost, "estimated_usd": round(est, 4)}


def _is_auth_or_credit_error(exc: Exception) -> bool:
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
    global _using_fallback, _fallback_triggered
    key    = config.ANTHROPIC_API_KEY_2 if _using_fallback else config.ANTHROPIC_API_KEY
    client = Anthropic(api_key=key)
    try:
        return fn(client)
    except Exception as exc:
        if _using_fallback or not config.ANTHROPIC_API_KEY_2 or not _is_auth_or_credit_error(exc):
            raise
    _using_fallback     = True
    _fallback_triggered = True
    client2 = Anthropic(api_key=config.ANTHROPIC_API_KEY_2)
    return fn(client2)


# ── Bundle trimming helpers ────────────────────────────────────────────────────

def _load_system_prompt() -> str:
    return config.PROMPT_FILE.read_text(encoding="utf-8")


def _slim_bundle(bundle: dict) -> dict:
    """Token-efficient version of the data bundle for the Sonnet prompt."""
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
    """Return (tech_json, fund_json) for Haiku screener — minimal tokens."""
    tech  = bundle.get("technical", {})
    daily = tech.get("daily", {})
    if daily:
        slim_tech = {
            "trend":          daily.get("trend"),
            "rsi14":          daily.get("rsi14"),
            "macd_hist":      daily.get("macd_hist"),
            "bollinger":      daily.get("bollinger"),
            "sma50":          daily.get("sma50"),
            "sma200":         daily.get("sma200"),
            "atr14":          daily.get("atr14"),
            "recent_high_20": daily.get("recent_high_20"),
            "recent_low_20":  daily.get("recent_low_20"),
            "tech_signal":    daily.get("tech_signal"),
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


def _build_user_message(pair: str, bundle: dict, thesis: str = "", risk: str = "") -> str:
    """Compact user message. Injects pre-generated thesis/risk from Haiku."""
    slim = _slim_bundle(bundle)
    parts = [
        memory.render(),
        "",
        f"Pair: {pair}",
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
        "=== MACRO ===",
        json.dumps(slim["macro"], indent=2),
    ]
    if thesis or risk:
        parts += [
            "",
            "=== PRE-GENERATED THESIS (use verbatim in output) ===",
            f"KEY_THESIS: {thesis}" if thesis else "",
            f"RISK_FACTORS: {risk}" if risk else "",
        ]
    parts += [
        "",
        "Output PAIR: through TRADE_THIS: only. No preamble.",
        "BEST_ENTRY_TIME in Auckland time (NZDT=UTC+13 Sep–Apr, NZST=UTC+12 Apr–Sep).",
    ]
    return "\n".join(p for p in parts if p != "")


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
    Fails open (score=5) on any error so deep analysis still runs."""
    tech_json, fund_json = _slim_for_screen(bundle)
    user_msg = "\n".join([
        f"Pair: {pair}",
        "",
        "=== TECHNICAL ===",
        tech_json,
        "",
        "=== FUNDAMENTAL ===",
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
        _cost["haiku_input"]  += getattr(resp.usage, "input_tokens",  0)
        _cost["haiku_output"] += getattr(resp.usage, "output_tokens", 0)
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception:
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


# ── Stage-1b: Haiku thesis generation ─────────────────────────────────────────

_THESIS_SYSTEM = (
    "Generate KEY_THESIS and RISK_FACTORS for a forex pair from the data provided.\n"
    "KEY_THESIS: 1-2 sentences on what the combined data shows as the main edge.\n"
    "RISK_FACTORS: 2 specific events or price levels that would invalidate a trade.\n"
    "Respond with exactly:\n"
    "KEY_THESIS: <text>\n"
    "RISK_FACTORS: <text>"
)


def _generate_thesis(pair: str, bundle: dict) -> tuple:
    """Use Haiku to cheaply generate thesis and risk factors."""
    slim = _slim_bundle(bundle)
    user_msg = f"Pair: {pair}\n\n{json.dumps(slim, separators=(',', ':'))}"

    def _call(client):
        return client.messages.create(
            model=config.HAIKU_MODEL,
            max_tokens=200,
            system=_THESIS_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )

    try:
        resp = _call_api(_call)
        _cost["haiku_input"]  += getattr(resp.usage, "input_tokens",  0)
        _cost["haiku_output"] += getattr(resp.usage, "output_tokens", 0)
        text    = "".join(b.text for b in resp.content if b.type == "text").strip()
        thesis, risk = "", ""
        for line in text.splitlines():
            if line.startswith("KEY_THESIS:"):
                thesis = line.split(":", 1)[1].strip()
            elif line.startswith("RISK_FACTORS:"):
                risk = line.split(":", 1)[1].strip()
        return thesis, risk
    except Exception:
        return "", ""


# ── Skip-unchanged check ───────────────────────────────────────────────────────

def _pip_value(pair: str) -> float:
    """Return pip size: 0.01 for JPY pairs, 0.0001 for all others."""
    return 0.01 if "JPY" in pair.upper() else 0.0001


def check_skip(pair: str, bundle: dict) -> tuple:
    """Return (should_skip, cached_report) if pair hasn't moved ≥15 pips in 6h."""
    entry = _analysis_cache.get(pair)
    if not entry:
        return False, None
    age_h = (time.time() - entry["ts"]) / 3600.0
    if age_h > _SKIP_TTL_HOURS:
        return False, None
    try:
        cur_price = float(bundle["technical"]["daily"]["last_close"])
        pip_move  = abs(cur_price - entry["price"]) / _pip_value(pair)
        if pip_move < _SKIP_PIPS:
            _cost["cache_hits"] += 1
            return True, entry["report"]
    except (KeyError, TypeError, ValueError):
        pass
    return False, None


def _store_skip_cache(pair: str, bundle: dict, report: str) -> None:
    """Cache the analysis result for potential skip on next run."""
    try:
        price = float(bundle["technical"]["daily"]["last_close"])
        _analysis_cache[pair] = {"ts": time.time(), "price": price, "report": report}
    except (KeyError, TypeError, ValueError):
        pass


# ── Stage-2 deep analysis (Sonnet) ────────────────────────────────────────────

def analyse(pair: str, bundle: dict) -> str:
    """Full analysis: Haiku generates thesis, Sonnet scores and sizes the trade."""
    # Step 1: Haiku generates thesis + risk (cheap, saves Sonnet output tokens)
    thesis, risk = _generate_thesis(pair, bundle)

    # Step 2: Sonnet scores the trade with pre-generated thesis in context
    system_prompt = _load_system_prompt()
    user_message  = _build_user_message(pair, bundle, thesis=thesis, risk=risk)

    def _call(client):
        return client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=600,
            system=[
                {
                    "type":          "text",
                    "text":          system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )

    resp   = _call_api(_call)
    _cost["sonnet_input"]  += getattr(resp.usage, "input_tokens",  0)
    _cost["sonnet_output"] += getattr(resp.usage, "output_tokens", 0)
    report = "".join(block.text for block in resp.content if block.type == "text")

    # Step 3: Inject Haiku-generated thesis/risk into the report if Sonnet omitted them
    injected = []
    if thesis and "KEY_THESIS:" not in report:
        injected.append(f"KEY_THESIS: {thesis}")
    if risk and "RISK_FACTORS:" not in report:
        injected.append(f"RISK_FACTORS: {risk}")
    if injected:
        report = report.rstrip() + "\n" + "\n".join(injected)

    _store_skip_cache(pair, bundle, report)
    return report
