"""The Claude analysis layer — cost-optimised two-stage flow.

Stage 1  — Haiku full analysis for ALL pairs in scope (~200 input tokens each).
           Outputs confidence 1-10, direction, all 5 layer scores, key thesis.
Stage 2  — Sonnet confirmation ONLY for pairs where Haiku confidence >= threshold
           (6 for the 6am full scan, 7 for intraday scans). Typically 0-3 pairs.
           Uses ultra-compressed input (<500 tokens) and 400 max_tokens output.

Skip-unchanged: pairs that moved <10 pips since last scan are skipped entirely,
further reducing API calls on repeated intraday runs.

Fallback key: if primary ANTHROPIC_API_KEY fails, all remaining calls switch to
ANTHROPIC_API_KEY_2 for the rest of the run.
"""

import json
import time

from anthropic import Anthropic

import config
from src import memory

# ── API key fallback state ─────────────────────────────────────────────────────
_using_fallback: bool     = False
_fallback_triggered: bool = False

# ── Run-level cost tracker ─────────────────────────────────────────────────────
_cost: dict = {
    "haiku_input":  0,
    "haiku_output": 0,
    "sonnet_input": 0,
    "sonnet_output": 0,
    "cache_hits":   0,
}

# ── Skip-unchanged cache ───────────────────────────────────────────────────────
_SKIP_TTL_HOURS = 6.0
_SKIP_PIPS      = 10        # tightened from 15 — skips Haiku entirely if price flat
_analysis_cache: dict = {}  # pair -> {ts, price, report}


def reset_key_state() -> None:
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


# ── Ultra-compact data builder (~150 tokens) ───────────────────────────────────

def _compress_bundle(pair: str, bundle: dict) -> str:
    """Build a flat, ~150-token data string used for both Haiku and Sonnet prompts."""
    lines = [f"Pair:{pair}"]

    tech  = bundle.get("technical", {})
    daily = tech.get("daily", {})

    def _f(v, d=4):
        try:
            return f"{float(v):.{d}f}"
        except (TypeError, ValueError):
            return "?"

    if isinstance(daily, dict) and daily:
        lines.append(
            f"D1 price={_f(daily.get('last_close'))} "
            f"RSI={daily.get('rsi14','?')} "
            f"MACDh={_f(daily.get('macd_hist'))} "
            f"SMA50={_f(daily.get('sma50'))} "
            f"SMA200={_f(daily.get('sma200'))} "
            f"trend={daily.get('trend','?')} "
            f"ATR={_f(daily.get('atr14'))}"
        )
        h4 = tech.get("h4") or tech.get("4h") or {}
        if isinstance(h4, dict) and h4.get("rsi14"):
            lines.append(
                f"4H RSI={h4.get('rsi14')} "
                f"MACDh={_f(h4.get('macd_hist'))} "
                f"trend={h4.get('trend','?')}"
            )
    else:
        lines.append("D1:UNAVAILABLE")

    fund = bundle.get("fundamental", {})
    diff = fund.get("rate_differential_pct")
    if diff is not None:
        b = fund.get("base") or {}
        q = fund.get("quote") or {}
        lines.append(
            f"FUND diff={diff:+.2f}% "
            f"{b.get('currency','?')}_rate={b.get('rate_trend','?')} "
            f"{q.get('currency','?')}_rate={q.get('rate_trend','?')}"
        )
    else:
        lines.append("FUND:UNAVAILABLE")

    pos = bundle.get("positioning", {})
    pos_parts = []
    for side in ("base", "quote"):
        p = pos.get(side, {})
        if p.get("status") == "ok":
            flag = (p.get("extreme_flag") or "")[:20]
            pos_parts.append(
                f"{p.get('currency','?')}={p.get('direction','?')}@{p.get('percentile_in_range','?')}pct"
                + (f" {flag}" if flag else "")
            )
        else:
            pos_parts.append(f"{side[0].upper()}:N/A")
    lines.append(f"POS {' '.join(pos_parts)}")

    sent = bundle.get("sentiment", {})
    sent_parts = []
    for side in ("base", "quote"):
        s = sent.get(side, {})
        if s.get("status") == "ok":
            sent_parts.append(f"{s.get('currency','?')}={s.get('article_count',0)}art")
        else:
            sent_parts.append(f"{side[0].upper()}:N/A")
    lines.append(f"SENT {' '.join(sent_parts)}")

    mac     = bundle.get("macro", {})
    signals = mac.get("signals") or {}
    mac_parts = []
    for label, key in (
        ("VIX",   "VIX (volatility index)"),
        ("oil",   "WTI crude oil ($/bbl)"),
        ("curve", "US 2s10s curve (10Y-2Y, %)"),
    ):
        d = signals.get(key, {})
        v = d.get("value") if isinstance(d, dict) else None
        if v is not None:
            mac_parts.append(f"{label}={float(v):.1f}")
    lines.append(f"MACRO {' '.join(mac_parts) or 'UNAVAILABLE'}")

    mtf_data = bundle.get("mtf", {})
    mtf_bd   = mtf_data.get("breakdown", "")
    if mtf_bd and mtf_bd != "UNAVAILABLE":
        lines.append(
            f"MTF {mtf_bd} "
            f"conf={mtf_data.get('agreeing_count', 0)}/5 "
            f"wt={int(mtf_data.get('weighted_score', 0) * 100)}%"
        )

    return "\n".join(lines)


# ── Stage 1: Haiku full analysis ───────────────────────────────────────────────

def _haiku_system_prompt() -> str:
    """Build the Haiku system prompt using the active threshold config."""
    from src import threshold_manager
    cfg = threshold_manager.load()
    thr = int(cfg.get("confidence_threshold", 6))
    rr  = cfg.get("min_rr", 1.3)
    return (
        "Forex analyst. Score all 5 data layers 1-10 and output confidence.\n"
        f"TRADE_THIS: YES only if confidence>={thr}, R:R>={rr}, >=4 layers agree. UNAVAILABLE=score 1.\n"
        "TECHNICAL: RSI<30=8-9BUY 30-35=6-7BUY 35-40=4-5BUY 40-60=1-3NEUTRAL "
        "60-65=4-5SELL 65-70=6-7SELL >70=8-9SELL +/-1 MACD +/-1 Bollinger +/-1 D1+4H aligned.\n"
        "Output PAIR: through TRADE_THIS: only. No preamble.\n"
        "PAIR: [p]\nDIRECTION: [BUY|SELL]\nCONFIDENCE: [n/10]\n"
        "TECHNICAL_SCORE: [n/10]\nFUNDAMENTAL_SCORE: [n/10]\nSENTIMENT_SCORE: [n/10]\n"
        "POSITIONING_SCORE: [n/10]\nMACRO_SCORE: [n/10]\n"
        "KEY_THESIS: [1 sentence]\nRISK_FACTORS: [2 risks]\nTRADE_THIS: [YES|NO]"
    )


def analyse_haiku_full(pair: str, bundle: dict) -> dict:
    """Haiku full analysis for all pairs.

    Returns dict with keys: confidence (int 1-10), direction (str), report (str), reason (str).
    The report is compatible with recparse.parse() — contains all score fields the
    Watch List and dashboard need. No entry/stop/target (Sonnet provides those for conf>=threshold).
    """
    user_msg = _compress_bundle(pair, bundle)

    def _call(client):
        return client.messages.create(
            model=config.HAIKU_MODEL,
            max_tokens=250,
            system=_haiku_system_prompt(),
            messages=[{"role": "user", "content": user_msg}],
        )

    try:
        resp   = _call_api(_call)
        _cost["haiku_input"]  += getattr(resp.usage, "input_tokens",  0)
        _cost["haiku_output"] += getattr(resp.usage, "output_tokens", 0)
        report = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as exc:
        return {"confidence": 1, "direction": "NONE", "report": "", "reason": str(exc)}

    confidence, direction, reason = 1, "NONE", ""
    for line in report.splitlines():
        line = line.strip()
        if line.startswith("CONFIDENCE:"):
            try:
                confidence = max(1, min(10, int(line.split(":", 1)[1].strip().split("/")[0])))
            except (ValueError, IndexError):
                pass
        elif line.startswith("DIRECTION:"):
            direction = line.split(":", 1)[1].strip()
        elif line.startswith("KEY_THESIS:"):
            reason = line.split(":", 1)[1].strip()[:80]

    _store_skip_cache(pair, bundle, report)
    return {"confidence": confidence, "direction": direction, "report": report, "reason": reason}


# ── Skip-unchanged check ───────────────────────────────────────────────────────

def _pip_value(pair: str) -> float:
    return 0.01 if "JPY" in pair.upper() else 0.0001


def check_skip(pair: str, bundle: dict) -> tuple:
    """Return (should_skip, cached_report) if pair hasn't moved >=10 pips in 6h."""
    entry = _analysis_cache.get(pair)
    if not entry:
        return False, None
    if (time.time() - entry["ts"]) / 3600.0 > _SKIP_TTL_HOURS:
        return False, None
    try:
        cur_price = float(bundle["technical"]["daily"]["last_close"])
        if abs(cur_price - entry["price"]) / _pip_value(pair) < _SKIP_PIPS:
            _cost["cache_hits"] += 1
            return True, entry["report"]
    except (KeyError, TypeError, ValueError):
        pass
    return False, None


def _store_skip_cache(pair: str, bundle: dict, report: str) -> None:
    try:
        price = float(bundle["technical"]["daily"]["last_close"])
        _analysis_cache[pair] = {"ts": time.time(), "price": price, "report": report}
    except (KeyError, TypeError, ValueError):
        pass


# ── Stage 2: Sonnet confirmation (high-confidence pairs only) ──────────────────

def _load_system_prompt() -> str:
    from src import threshold_manager
    cfg  = threshold_manager.load()
    thr  = int(cfg.get("confidence_threshold", 6))
    rr   = cfg.get("min_rr", 1.3)
    text = config.PROMPT_FILE.read_text(encoding="utf-8")
    return (
        text
        .replace("{confidence_threshold}", str(thr))
        .replace("{min_rr}", str(rr))
        .replace("{below_threshold}", str(thr - 1))
    )


def _build_sonnet_message(pair: str, bundle: dict, haiku_report: str) -> str:
    """Compact Sonnet user message: compressed data + Haiku preliminary (~300-400 tokens)."""
    parts = []

    mem = memory.render()
    if mem and len(mem) < 500:
        parts.append(mem)
        parts.append("")

    parts.append(_compress_bundle(pair, bundle))

    if haiku_report:
        parts.append("\n--- Haiku preliminary ---")
        parts.append(haiku_report.strip())

    parts.append(
        "\nOutput PAIR: through TRADE_THIS: only. "
        "Include ENTRY TARGET STOP_LOSS REWARD_RISK_RATIO BEST_ENTRY_TIME(Auckland time) NEWS_WARNING."
    )
    return "\n".join(parts)


def analyse(pair: str, bundle: dict, haiku_report: str = "") -> str:
    """Sonnet confirmation for high-confidence pairs.

    Input: ~400-600 tokens (compressed data + Haiku report).
    Output: max 400 tokens (full structured format with entry/stop/target).
    Only called for pairs where Haiku confidence >= sonnet_threshold (6 for full scan, 7 for intraday).
    """
    system_prompt = _load_system_prompt()
    user_message  = _build_sonnet_message(pair, bundle, haiku_report)

    def _call(client):
        return client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=400,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_message}],
        )

    resp   = _call_api(_call)
    _cost["sonnet_input"]  += getattr(resp.usage, "input_tokens",  0)
    _cost["sonnet_output"] += getattr(resp.usage, "output_tokens", 0)
    report = "".join(block.text for block in resp.content if block.type == "text")

    # Inject Haiku thesis/risk if Sonnet omitted them (saves output tokens)
    if haiku_report:
        for field in ("KEY_THESIS", "RISK_FACTORS"):
            if field + ":" not in report:
                for line in haiku_report.splitlines():
                    if line.strip().startswith(field + ":"):
                        report = report.rstrip() + "\n" + line.strip()
                        break

    _store_skip_cache(pair, bundle, report)
    return report
