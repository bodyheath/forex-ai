"""The Claude analysis layer — cost-optimised two-stage flow.

Stage 1  — Haiku full analysis for ALL pairs in scope (~200 input tokens each).
           Outputs confidence 1-10, direction, all 5 layer scores, key thesis.
Stage 2  — Sonnet confirmation ONLY for pairs where Haiku confidence >= threshold
           (6 for the 6am full scan, 7 for intraday scans). Typically 0-3 pairs.
           Uses ultra-compressed input (<500 tokens) and 1000 max_tokens output.

Skip-unchanged: pairs that moved <10 pips since last scan are skipped entirely,
further reducing API calls on repeated intraday runs.

Fallback key: if primary ANTHROPIC_API_KEY fails, all remaining calls switch to
ANTHROPIC_API_KEY_2 for the rest of the run.
"""

import json
import re
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
        # Include Python-computed tech signal as an anchor so Claude never drifts below it
        ts       = daily.get("tech_signal") or {}
        sig_part = (f" T_sig={ts['direction']}_{ts['score']}"
                    if ts and ts.get("direction") and ts.get("score") else "")
        # Include Bollinger state so Claude can apply the +1 BB confirmation bonus
        bb_raw   = daily.get("bollinger", "") or ""
        bb_short = bb_raw.split("(")[0].strip()
        bb_part  = (f" BB={bb_short}" if bb_short and bb_short != "inside bands" else "")
        # Include top candlestick pattern detected on D1
        d1_pats  = daily.get("patterns", [])
        d1_pat_str = ""
        if d1_pats:
            top = d1_pats[0]
            d1_pat_str = f" PAT={top['name'].replace(' ','_')}:{top['direction'][:3]}"
        lines.append(
            f"D1 price={_f(daily.get('last_close'))} "
            f"RSI={daily.get('rsi14','?')} "
            f"MACDh={_f(daily.get('macd_hist'))} "
            f"SMA50={_f(daily.get('sma50'))} "
            f"SMA200={_f(daily.get('sma200'))} "
            f"trend={daily.get('trend','?')} "
            f"ATR={_f(daily.get('atr14'))}"
            + sig_part + bb_part + d1_pat_str
        )
        h4 = tech.get("h4") or tech.get("4h") or {}
        if isinstance(h4, dict) and h4.get("rsi14"):
            h4_ts      = h4.get("tech_signal") or {}
            h4_sig_str = f" T={h4_ts['score']}" if h4_ts and h4_ts.get("score") else ""
            h4_pats    = h4.get("patterns", [])
            h4_pat_str = ""
            if h4_pats:
                top = h4_pats[0]
                h4_pat_str = f" PAT={top['name'].replace(' ','_')}:{top['direction'][:3]}"
            lines.append(
                f"4H RSI={h4.get('rsi14')} "
                f"MACDh={_f(h4.get('macd_hist'))} "
                f"trend={h4.get('trend','?')}"
                + h4_sig_str + h4_pat_str
            )
        # Fibonacci summary from daily timeframe
        fib = daily.get("fibonacci", {})
        if isinstance(fib, dict) and fib.get("status") == "ok":
            near      = fib.get("near_levels", [])
            near_str  = ""
            if near:
                n        = near[0]
                near_str = f" near={n['label']}@{n['price']}({n['distance_pips']:.0f}p,{n['type'][:3]})"
            lines.append(
                f"FIB SH={fib['swing_high']} SL={fib['swing_low']} "
                f"range={fib['range_pips']}p"
                + near_str
            )
        # RSI divergence from daily timeframe
        div      = daily.get("divergence", {})
        div_bul  = div.get("bullish")
        div_ber  = div.get("bearish")
        div_parts: list = []
        if div_bul:
            div_parts.append(
                f"bullish:{div_bul['strength']}"
                f"({div_bul['price_diff_pips']:.0f}p/{div_bul['rsi_diff']:.0f}pt)"
            )
        if div_ber:
            div_parts.append(
                f"bearish:{div_ber['strength']}"
                f"({div_ber['price_diff_pips']:.0f}p/{div_ber['rsi_diff']:.0f}pt)"
            )
        lines.append(f"DIV={','.join(div_parts) or 'none'}")
        # Oscillator confluence (RSI + Stochastic + CCI)
        osc = daily.get("oscillator_confluence", {})
        if isinstance(osc, dict) and osc.get("direction") != "NONE" and osc.get("score", 0) >= 2:
            lines.append(
                f"OSC stoch_k={osc.get('stoch_k','?')} cci={osc.get('cci','?')}"
                f" conf={osc.get('conf_label','?')}"
            )
        else:
            stk = daily.get("stochastic_k")
            cci_v = daily.get("cci")
            if stk is not None or cci_v is not None:
                lines.append(
                    f"OSC stoch_k={stk or '?'} cci={cci_v or '?'} conf=NONE"
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
            flag     = (p.get("extreme_flag") or "")[:20]
            momentum = p.get("cot_momentum", "")
            mom_str  = f" MOM={momentum}" if momentum else ""
            pos_parts.append(
                f"{p.get('currency','?')}={p.get('direction','?')}@{p.get('percentile_in_range','?')}pct"
                + mom_str
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
            f"conf={mtf_data.get('agreeing_count', 0)}/3 "
            f"wt={int(mtf_data.get('weighted_score', 0) * 100)}%"
        )

    # MA ribbon (EMA 8/13/21/34/55/89) — only include if daily data available
    rib = daily.get("ribbon", {}) if isinstance(daily, dict) else {}
    if isinstance(rib, dict) and rib.get("status") not in ("UNAVAILABLE", None, ""):
        r_status = rib.get("status", "NEUTRAL")
        r_fan    = (" fan" if rib.get("fanning")
                    else (" conv" if rib.get("converging") else ""))
        lines.append(
            f"RIB={r_status}{r_fan} aligned={rib.get('aligned_count', 0)}/5"
        )

    # Smart Money Divergence (Layer 10)
    smd_data = bundle.get("smart_money", {})
    if isinstance(smd_data, dict) and smd_data.get("status") not in ("insufficient_data", None):
        smd_s   = smd_data.get("smd_score", 0)
        smd_sig = smd_data.get("signal", "NEUTRAL")
        lines.append(f"SMD={smd_s:+d} [{smd_sig}]")

    return "\n".join(lines)


# ── Stage 1: Haiku full analysis ───────────────────────────────────────────────

def _haiku_system_prompt(threshold_override: "float | None" = None) -> str:
    """Build the Haiku system prompt using the active threshold config."""
    from src import threshold_manager
    cfg = threshold_manager.load()
    thr = threshold_override if threshold_override is not None else int(cfg.get("confidence_threshold", 6))
    rr  = cfg.get("min_rr", 1.3)
    return (
        "Forex analyst. Score all 5 data layers 1-10 and output confidence.\n"
        f"TRADE_THIS: YES only if confidence>={thr}, R:R>={rr}, MTF weekly+daily both agree, >=4 fundamental layers agree. UNAVAILABLE=score 5 (neutral — missing data has no directional bias).\n"
        "MTF rule: check MTF line in data — if weekly AND daily don't both agree on direction (conf<2/3), TRADE_THIS NO regardless. Monthly is context only, 4H is optional bonus.\n"
        "TECHNICAL scoring rules (enforce exactly — no exceptions):\n"
        "  RSI tiers (standard/mixed MTF): <30=9-10BUY  30-35=7-8BUY  35-45=5-6BUY  45-62=3-4NEUTRAL  "
        "62-70=4-5SELL  70-76=7-8SELL  >76=9-10SELL\n"
        "  RSI TREND CONTEXT — check MTF line before scoring (critical):\n"
        "  • Confirmed DOWNTREND (W:SELL + D:SELL in MTF data): oversold is normal in a downtrend — "
        "shift BUY zones lower: RSI<20=9-10BUY, 20-30=7-8BUY, 30-42=5-6BUY; RSI 42-62=NEUTRAL(3-4) "
        "— do NOT score 5-6BUY just because RSI is 35-45 in a downtrend.\n"
        "  • Confirmed UPTREND (W:BUY + D:BUY in MTF data): overbought is normal in an uptrend — "
        "shift SELL zones higher: RSI>87=9-10SELL, 77-87=7-8SELL, 65-77=4-5SELL; RSI 45-65=NEUTRAL(3-4) "
        "— do NOT score 4-5SELL just because RSI is 60-70 in an uptrend.\n"
        "  • Mixed/neutral MTF (W and D disagree, or either is NEUTRAL): use standard tiers above.\n"
        "  Bonuses (+1 each, stackable): MACD confirms direction | BB confirms (lower=BUY/upper=SELL) "
        "| price confirms SMA50 direction | D1+4H same direction\n"
        "  T_sig in D1 line is the pre-computed baseline — use it as your TECHNICAL_SCORE unless "
        "a strong contradictory factor justifies adjusting by ±1\n"
        "  HARD RULE: TECHNICAL_SCORE >= 3 whenever RSI+MACD data present. "
        "TECHNICAL_SCORE = 5 when D1:UNAVAILABLE (neutral — a data fetch failure is not bearish).\n"
        "CANDLE: PAT= in D1/4H lines = Python-detected pattern (e.g. pin_bar_at_key_level:bul). "
        "Already factored into T_sig. Use as supporting evidence for CONFIDENCE and KEY_THESIS — "
        "pin bar at key level + RSI oversold = highest-probability setup.\n"
        "FIB: FIB line shows swing SH/SL/range and nearest Fibonacci level (near=). "
        "Price within 10p of a Fib level = stronger confluence → raise CONFIDENCE by 1 if it confirms direction. "
        "Fib level + RSI + pattern = triple-confirmed entry. "
        "nearest_above = resistance targets, nearest_below = support/bounce zones.\n"
        "DIV: DIV= line shows Python-detected RSI divergence. "
        "If DIV confirms your direction (bullish DIV + BUY, or bearish DIV + SELL): "
        "output DIVERGENCE: CONFIRMED and raise CONFIDENCE by 1 (max 10). "
        "If DIV conflicts (bullish DIV + SELL, or bearish DIV + BUY): output DIVERGENCE: CONFLICT — consider reducing CONFIDENCE. "
        "If DIV=none: output DIVERGENCE: NONE.\n"
        "OSC: OSC= line shows oscillator confluence (RSI+Stochastic+CCI agreement). "
        "conf=BUY(3/3) or SELL(3/3) = TRIPLE confluence — raise CONFIDENCE by 2, output OSCILLATOR_CONFLUENCE: TRIPLE_BUY or TRIPLE_SELL. "
        "conf=BUY(2/3) or SELL(2/3) = PARTIAL — raise CONFIDENCE by 1, output OSCILLATOR_CONFLUENCE: PARTIAL_BUY or PARTIAL_SELL. "
        "conf=NONE = OSCILLATOR_CONFLUENCE: NONE. "
        "Triple confluence that confirms direction = highest-reliability reversal signal possible.\n"
        "RIBBON: RIB= line = EMA ribbon (8/13/21/34/55/89). "
        "ALIGNED_BULL=all 6 EMAs stacked bullish (fan=fanning/accelerating). "
        "ALIGNED_BEAR=all 6 EMAs stacked bearish. "
        "CONVERGING=ribbon tightening, trend weakening. LEANING_BULL/BEAR=4 of 5 pairs aligned. "
        "NEUTRAL=mixed. Already factored into T_sig (+2 for fully aligned in direction). "
        "Use to confirm KEY_THESIS — aligned ribbon = trend continuation, converging = reversal risk.\n"
        "COT MOMENTUM: MOM= in POS line shows institutional positioning momentum over 3 weeks. "
        "BUILDING=institutions increasing conviction in current direction: raise POSITIONING_SCORE +1. "
        "UNWINDING=institutions reducing position: lower POSITIONING_SCORE -1 and note as risk. "
        "REVERSING=institutions flipped from long to short (or vice versa): lower POSITIONING_SCORE -2, "
        "add to RISK_FACTORS as 'COT reversal: institutional positioning flipped'. "
        "STABLE=no significant change: no adjustment. "
        "CRITICAL: if trade direction aligns with the OLD positioning but COT is now REVERSING "
        "(e.g. BUY EUR but EUR COT flipped from long to short): reduce CONFIDENCE by 1 and flag in RISK_FACTORS.\n"
        "SMD: SMD= line = Smart Money Divergence −10 to +10 (institutional vs retail sentiment). "
        "Positive = institutions bullish while retail bearish (contrarian BUY edge). "
        "Negative = institutions bearish while retail bullish (contrarian SELL edge). "
        "Context only — do NOT adjust CONFIDENCE for SMD (system handles this). "
        "Note scores ≥ +5 or ≤ −5 in KEY_THESIS.\n"
        "Output PAIR: through TRADE_THIS: only. No preamble.\n"
        "PAIR: [p]\nDIRECTION: [BUY|SELL]\nCONFIDENCE: [n/10]\n"
        "TECHNICAL_SCORE: [n/10]\nFUNDAMENTAL_SCORE: [n/10]\nSENTIMENT_SCORE: [n/10]\n"
        "POSITIONING_SCORE: [n/10]\nMACRO_SCORE: [n/10]\n"
        "DIVERGENCE: [CONFIRMED|NONE|CONFLICT]\n"
        "OSCILLATOR_CONFLUENCE: [TRIPLE_BUY|TRIPLE_SELL|PARTIAL_BUY|PARTIAL_SELL|NONE]\n"
        "KEY_THESIS: [1 sentence]\nRISK_FACTORS: [2 risks]\nTRADE_THIS: [YES|NO]\n"
        "ENTRY_TYPE: [IMMEDIATE|BREAKOUT_BUY|BREAKOUT_SELL|LIMIT_BUY|LIMIT_SELL|PULLBACK] "
        "(IMMEDIATE=enter now at market; BREAKOUT=wait for price to cross a level; "
        "LIMIT=wait for price to reach a better level; PULLBACK=wait for retracement)\n"
        "ENTRY_TRIGGER_PRICE: [float or IMMEDIATE]\n"
        "ENTRY_TRIGGER_REASON: [1 sentence e.g. 'Break above neckline at 1.1510']\n"
        "ENTRY_TRIGGER_EXPIRY_HOURS: [int 0-72, 0 for IMMEDIATE]"
    )


def analyse_haiku_full(pair: str, bundle: dict,
                        threshold_override: "float | None" = None) -> dict:
    """Haiku full analysis for all pairs.

    Returns dict with keys: confidence (int 1-10), direction (str), report (str), reason (str).
    The report is compatible with recparse.parse() — contains all score fields the
    Watch List and dashboard need. No entry/stop/target (Sonnet provides those for conf>=threshold).
    threshold_override: if set, replaces the global confidence threshold in the prompt
    (used for pairs with demonstrated 70%+ win rate).
    """
    user_msg = _compress_bundle(pair, bundle)

    def _call(client):
        return client.messages.create(
            model=config.HAIKU_MODEL,
            max_tokens=400,
            system=_haiku_system_prompt(threshold_override=threshold_override),
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

def _load_system_prompt(threshold_override: "float | None" = None) -> str:
    from src import threshold_manager
    cfg  = threshold_manager.load()
    thr  = threshold_override if threshold_override is not None else int(cfg.get("confidence_threshold", 6))
    rr   = cfg.get("min_rr", 1.3)
    # below_threshold: always 1 below the effective threshold
    below = int(thr) - 1 if isinstance(thr, int) else round(thr - 0.5, 1)
    text = config.PROMPT_FILE.read_text(encoding="utf-8")
    return (
        text
        .replace("{confidence_threshold}", str(thr))
        .replace("{min_rr}", str(rr))
        .replace("{below_threshold}", str(below))
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


def analyse(pair: str, bundle: dict, haiku_report: str = "",
            threshold_override: "float | None" = None, log=print) -> str:
    """Sonnet confirmation for high-confidence pairs.

    Input: ~400-600 tokens (compressed data + Haiku report).
    Output: max 1000 tokens (raised 400 -> 600 -> 1000 — complex ribbon-vs-MTF conflict trades
    kept hitting the 600 limit before reaching the CONFIDENCE line, causing stop_reason=max_tokens
    failures).
    Only called for pairs where Haiku confidence >= sonnet_threshold (6 for full scan, 7 for intraday).
    threshold_override: if set, replaces the global confidence threshold in the Sonnet prompt.
    """
    system_prompt = _load_system_prompt(threshold_override=threshold_override)
    user_message  = _build_sonnet_message(pair, bundle, haiku_report)

    def _call(client):
        return client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1000,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_message}],
        )

    report = ""
    for attempt in range(1, 3):
        resp   = _call_api(_call)
        _cost["sonnet_input"]  += getattr(resp.usage, "input_tokens",  0)
        _cost["sonnet_output"] += getattr(resp.usage, "output_tokens", 0)
        report = "".join(block.text for block in resp.content if block.type == "text")
        stop_reason = getattr(resp, "stop_reason", "?")
        if not report.strip():
            raise RuntimeError(
                f"Sonnet returned empty response for {pair} (stop_reason={stop_reason})"
            )
        if re.search(r"CONFIDENCE:\s*\d", report):
            break
        log(
            f"Sonnet: {pair} attempt {attempt} — response missing parseable CONFIDENCE "
            f"(stop_reason={stop_reason}). Raw: {report[:300]!r}"
        )
        if attempt == 2:
            raise RuntimeError(
                f"Sonnet confirmation for {pair} missing CONFIDENCE after 2 attempts "
                f"(stop_reason={stop_reason})\n"
                f"Last raw response: {report[:400]!r}"
            )

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


def devil_advocate(pair: str, parsed: dict, bundle: dict) -> dict:
    """Devil's advocate second opinion for 7+ confidence trades.

    Asks Sonnet to find the top 3 reasons the proposed trade could fail right
    now. Returns dict: has_objections (bool), n_compelling (int),
    reasons (list[str], max 2 for display), verdict (str).
    """
    direction  = (parsed.get("direction") or "?").upper()
    entry      = parsed.get("entry") or "?"
    stop       = parsed.get("stop_loss") or "?"
    target     = parsed.get("target") or "?"
    thesis     = (parsed.get("key_thesis") or parsed.get("KEY_THESIS") or "").strip() or "not provided"
    risk_known = (parsed.get("risk_factors") or "").strip() or "none"
    data_line  = _compress_bundle(pair, bundle)

    system_prompt = (
        "You are a risk-focused devil's advocate for forex trading. "
        "Your sole task is to find the strongest reasons a proposed trade could fail right now. "
        "Be specific and data-driven. Output only the structured format requested — nothing else."
    )
    user_message = (
        f"Proposed trade: {pair} {direction}\n"
        f"Entry: {entry}  Stop: {stop}  Target: {target}\n"
        f"Thesis: {thesis}\n"
        f"Known risks: {risk_known}\n"
        f"Data: {data_line}\n\n"
        "List the top 3 reasons this trade could fail RIGHT NOW. "
        "Rate each COMPELLING (genuine problem) or MINOR (worth noting but not deal-breaking). "
        "OVERALL_VERDICT must be CONCERNING if 2 or more reasons are COMPELLING, else ACCEPTABLE.\n\n"
        "OBJECTION_1: [one plain-English sentence]\n"
        "SEVERITY_1: COMPELLING or MINOR\n"
        "OBJECTION_2: [one plain-English sentence]\n"
        "SEVERITY_2: COMPELLING or MINOR\n"
        "OBJECTION_3: [one plain-English sentence]\n"
        "SEVERITY_3: COMPELLING or MINOR\n"
        "OVERALL_VERDICT: CONCERNING or ACCEPTABLE"
    )

    def _call(client):
        return client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=250,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

    resp = _call_api(_call)
    _cost["sonnet_input"]  += getattr(resp.usage, "input_tokens",  0)
    _cost["sonnet_output"] += getattr(resp.usage, "output_tokens", 0)
    text = "".join(block.text for block in resp.content if block.type == "text")

    objections: dict = {}
    severities: dict = {}
    verdict = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("OBJECTION_") and ":" in line:
            try:
                idx = int(line[10])
                objections[idx] = line.split(":", 1)[1].strip()
            except (IndexError, ValueError):
                pass
        elif line.startswith("SEVERITY_") and ":" in line:
            try:
                idx = int(line[9])
                severities[idx] = line.split(":", 1)[1].strip().upper()
            except (IndexError, ValueError):
                pass
        elif line.startswith("OVERALL_VERDICT:"):
            verdict = line.split(":", 1)[1].strip().upper()

    n_compelling   = sum(1 for i in range(1, 4) if severities.get(i) == "COMPELLING")
    has_objections = verdict == "CONCERNING" or n_compelling >= 2

    compelling = [objections[i] for i in range(1, 4)
                  if objections.get(i) and severities.get(i) == "COMPELLING"]
    minor      = [objections[i] for i in range(1, 4)
                  if objections.get(i) and severities.get(i) != "COMPELLING"]
    reasons    = (compelling + minor)[:2]

    return {
        "has_objections": has_objections,
        "n_compelling":   n_compelling,
        "reasons":        reasons,
        "verdict":        verdict or "ACCEPTABLE",
    }
