"""Daily automation runner (intended for a 6am scheduled task).

Sequence:
  1. Refresh learning memory from any outcomes recorded since the last run.
  2. Fetch the full Twelve Data forex universe; pre-score all pairs by session
     alignment, economic events, momentum, and volatility; select the top 15.
  2b. Two-stage smart pre-filter: score all candidates on free FRED+COT data
      before fetching any Twelve Data price candles — only the top 20 proceed.
  3. Batch pre-fetch FRED, COT, macro data once for all pairs (shared store).
  4. Analyse each selected pair (Haiku screen → Haiku thesis → Sonnet scoring).
  5. Auto-expand: if fewer than 3 pairs score confidence 5+, pull the next pairs
     from the pre-scored ranked list (already pre-filtered) and analyse them.
  6. Regenerate the HTML dashboard.
  7. Send a reformatted Telegram summary with cost breakdown.
"""

import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import config
from src import dashboard, learning, selector, service

_SESSION_AUCKLAND = {
    "EUR": ("London",   "5pm – 9pm"),
    "GBP": ("London",   "5pm – 9pm"),
    "CHF": ("London",   "5pm – 9pm"),
    "JPY": ("Tokyo",    "9am – 2pm"),
    "USD": ("New York", "10pm – 2am"),
    "CAD": ("New York", "10pm – 2am"),
    "AUD": ("Sydney",   "7am – 12pm"),
    "NZD": ("Sydney",   "7am – 12pm"),
}
_SESSION_PRIORITY = ["EUR", "GBP", "CHF", "AUD", "NZD", "JPY", "USD", "CAD"]


# ── Auckland / New Zealand time helpers ────────────────────────────────────────

def _auckland_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Pacific/Auckland"))
    except Exception:
        utc = datetime.now(__import__("datetime").timezone.utc)
        off = 13 if utc.month in (10, 11, 12, 1, 2, 3) else 12
        from datetime import timezone
        return utc.astimezone(timezone(timedelta(hours=off)))


def _fmt_time_nz(dt: datetime) -> str:
    h    = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    return f"{h}:{dt.minute:02d}{ampm}" if dt.minute else f"{h}{ampm}"


def _fmt_date_nz(dt: datetime) -> str:
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B')} {dt.year}"


def _fmt_date_short_nz(dt: datetime) -> str:
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B')}"


def _session_label(pair: str) -> str:
    cleaned = pair.upper().replace("/", "").replace("-", "")
    base  = cleaned[:3]
    quote = cleaned[3:6] if len(cleaned) >= 6 else ""
    for ccy in _SESSION_PRIORITY:
        if ccy in (base, quote):
            name, window = _SESSION_AUCKLAND[ccy]
            return f"{name} session {window} Auckland time"
    return "London session 5pm – 9pm Auckland time"


def _log_line(handle, msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    line  = f"[{stamp}] {msg}"
    print(line)
    handle.write(line + "\n")
    handle.flush()


def _telegram(message: str) -> None:
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    recipients = [config.TELEGRAM_CHAT_ID]
    if config.TELEGRAM_CHAT_ID_2:
        recipients.append(config.TELEGRAM_CHAT_ID_2)
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    for chat_id in recipients:
        try:
            data = urllib.parse.urlencode({
                "chat_id":    chat_id,
                "text":       message,
                "parse_mode": "HTML",
            }).encode()
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
        except Exception:
            pass


def _fmt_price(v) -> str:
    if v is None:
        return "—"
    f = float(v)
    return f"{f:.3f}" if f > 10 else f"{f:.5f}"


def _conf(result: dict) -> int:
    try:
        return int(result["parsed"].get("confidence") or 0)
    except (TypeError, ValueError):
        return 0


def _conf_bar(conf) -> str:
    """Generate visual confidence bar: 7 → ███████░░░"""
    try:
        n = max(0, min(10, int(conf)))
    except (TypeError, ValueError):
        n = 0
    return "█" * n + "░" * (10 - n)


def _pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair.upper() else 0.0001


def _fmt_pips_between(pair: str, price_a, price_b) -> str:
    try:
        pips = abs(float(price_a) - float(price_b)) / _pip_size(pair)
        return f"{pips:.0f} pips"
    except (TypeError, ValueError, ZeroDivisionError):
        return "—"


def _analyse_pair(pair: str, logf, force_deep: bool = False,
                  shared_fundamental=None, shared_macro=None) -> dict | None:
    try:
        return service.analyse_and_log(
            pair,
            log=lambda m: _log_line(logf, m),
            force_deep=force_deep,
            shared_fundamental=shared_fundamental,
            shared_macro=shared_macro,
        )
    except Exception as exc:
        _log_line(logf, f"FAILED {pair}: {exc}")
        traceback.print_exc(file=logf)
        return None


# ── Telegram context helpers ───────────────────────────────────────────────────

def _derive_market_context(deep_results: list, risk_data: dict) -> dict:
    ctx: dict = {
        "risk_env":      "⚖️ neutral",
        "vix":           None,
        "vix_trend":     None,
        "yield_curve":   None,
        "oil":           None,
        "strongest_ccy": None,
        "weakest_ccy":   None,
        "ccy_scores":    {},
    }
    for r in deep_results:
        try:
            signals  = r["bundle"]["macro"]["signals"]
            vix_data = signals.get("VIX (volatility index)", {})
            if isinstance(vix_data, dict) and vix_data.get("value") is not None:
                ctx["vix"]       = float(vix_data["value"])
                ctx["vix_trend"] = vix_data.get("trend", "unknown")
            oil_data = signals.get("WTI crude oil ($/bbl)", {})
            if isinstance(oil_data, dict) and oil_data.get("value") is not None:
                ctx["oil"] = float(oil_data["value"])
            crv_data = signals.get("US 2s10s curve (10Y-2Y, %)", {})
            if isinstance(crv_data, dict) and crv_data.get("value") is not None:
                ctx["yield_curve"] = float(crv_data["value"])
            if ctx["vix"] is not None:
                break
        except (KeyError, TypeError, ValueError):
            continue

    if ctx["vix"] is not None:
        if ctx["vix"] > 25:
            ctx["risk_env"] = "⚠️ risk-off"
        elif ctx["vix"] < 15:
            ctx["risk_env"] = "🟢 risk-on"
        else:
            ctx["risk_env"] = "⚖️ neutral"
    if ctx["yield_curve"] is not None and ctx["yield_curve"] < 0 and "risk-off" not in ctx["risk_env"]:
        ctx["risk_env"] += " (yield curve inverted)"

    ccy_score: dict[str, float] = {}
    for r in deep_results:
        p    = r["parsed"]
        conf = _conf(r)
        if conf < 4:
            continue
        pair  = r["pair"]
        clean = pair.upper().replace("/", "").replace("-", "")
        if len(clean) < 6:
            continue
        base, quote = clean[:3], clean[3:6]
        direction   = (p.get("direction") or "").upper()
        weight      = float(conf - 3)
        if direction == "BUY":
            ccy_score[base]  = ccy_score.get(base,  0.0) + weight
            ccy_score[quote] = ccy_score.get(quote, 0.0) - weight
        elif direction == "SELL":
            ccy_score[base]  = ccy_score.get(base,  0.0) - weight
            ccy_score[quote] = ccy_score.get(quote, 0.0) + weight

    if ccy_score:
        ctx["ccy_scores"]    = ccy_score
        ctx["strongest_ccy"] = max(ccy_score, key=lambda c: ccy_score[c])
        ctx["weakest_ccy"]   = min(ccy_score, key=lambda c: ccy_score[c])

    return ctx


def _score_breakdown_line(parsed: dict) -> str:
    def _s(key):
        v = parsed.get(key)
        return str(v) if v is not None else "—"
    return (
        f"T:{_s('technical_score')}  "
        f"F:{_s('fundamental_score')}  "
        f"S:{_s('sentiment_score')}  "
        f"P:{_s('positioning_score')}  "
        f"M:{_s('macro_score')}"
    )


def _what_needs_to_change(parsed: dict) -> str:
    scores = {
        "Technical":   parsed.get("technical_score"),
        "Fundamental": parsed.get("fundamental_score"),
        "Sentiment":   parsed.get("sentiment_score"),
        "Positioning": parsed.get("positioning_score"),
        "Macro":       parsed.get("macro_score"),
    }
    missing = [k for k, v in scores.items() if v is None]
    weak    = {k: v for k, v in scores.items() if v is not None and v < 7}

    parts = []
    if missing:
        parts.append(f"Restore {' + '.join(missing)} data")
    if weak:
        for name, score in sorted(weak.items(), key=lambda x: x[1])[:2]:
            if score <= 5:
                parts.append(f"{name} at {score}/10 — needs momentum shift to 7+")
            else:
                parts.append(f"{name} at {score}/10 — one confirmation away from 7+")
    if not parts:
        parts.append("All layers close — wait for price to reach key level")
    return "; ".join(parts)


def _rejection_reason(result: dict) -> str:
    if result.get("screened_out"):
        s      = result.get("screen", {})
        score  = s.get("score", "?")
        reason = (s.get("reason") or "").strip()
        if not reason:
            reason = "Technical and fundamental below minimum threshold"
        return f"Stage 1 score {score}/5 — {reason}"

    parsed    = result["parsed"]
    conf      = parsed.get("confidence")
    direction = (parsed.get("direction") or "?").upper()
    scores = {
        "Technical":   parsed.get("technical_score"),
        "Fundamental": parsed.get("fundamental_score"),
        "Sentiment":   parsed.get("sentiment_score"),
        "Positioning": parsed.get("positioning_score"),
        "Macro":       parsed.get("macro_score"),
    }
    missing = [k for k, v in scores.items() if v is None]
    below   = sorted(
        [(k, v) for k, v in scores.items() if v is not None and v < 7],
        key=lambda x: x[1],
    )

    parts = []
    for k, v in below[:2]:
        if v <= 4:
            parts.append(f"{k} weak ({v}/10)")
        else:
            parts.append(f"{k} at {v}/10 — needs one more confirmation")
    if missing:
        parts.append(f"{' + '.join(missing[:2])} data missing")
    if not parts:
        parts.append("overall confluence just below trade threshold")

    return f"Conf {conf}/10 {direction} — {', '.join(parts)}"


def _get_mtf_alignment(result: dict, direction: str) -> str:
    try:
        daily = result["bundle"]["technical"]["daily"]
        h4    = result["bundle"]["technical"].get("h4") or result["bundle"]["technical"].get("4h") or {}

        def _trend(tech: dict) -> str | None:
            c = tech.get("close") or tech.get("last_close")
            s = tech.get("sma50")
            if c is None or s is None:
                return None
            return "bullish" if float(c) > float(s) else "bearish"

        d_trend  = _trend(daily)
        h_trend  = _trend(h4)
        expected = "bullish" if direction.upper() == "BUY" else "bearish"

        if d_trend and h_trend:
            if d_trend == h_trend == expected:
                return f"✅ D1+4H both {d_trend}"
            elif d_trend == h_trend:
                return f"⚠️ Both {d_trend} — contrary to {direction}"
            return f"⚠️ Mixed (D1: {d_trend}, 4H: {h_trend})"
        if d_trend:
            return f"D1: {d_trend}"
        return "—"
    except (KeyError, TypeError, ValueError):
        return "—"


def _weekly_performance_section(date: str) -> list:
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return []
    if dt.weekday() != 0:
        return []
    try:
        from src import tracker
        rows   = tracker.load()
        cutoff = (dt - timedelta(days=7)).strftime("%Y-%m-%d")
        recent = [
            r for r in rows
            if r.get("status") in ("WIN", "LOSS", "BREAKEVEN")
            and (r.get("closed_at") or r.get("timestamp", ""))[:10] >= cutoff
        ]
    except Exception:
        return []

    lines = ["", "━━━━━━━━━━━━━━━━━━━━━", "📅 <b>WEEKLY PERFORMANCE SUMMARY</b>"]
    if not recent:
        lines.append("No closed trades in the past 7 days.")
        return lines

    wins  = [r for r in recent if r.get("status") == "WIN"]
    total = len(recent)
    wr    = len(wins) / total * 100 if total else 0

    def _r(row):
        try:
            return float(row.get("r_multiple") or 0)
        except (TypeError, ValueError):
            return 0.0

    total_r = sum(_r(r) for r in recent)
    best    = max(recent, key=_r) if recent else None
    worst   = min(recent, key=_r) if recent else None

    lines.append(
        f"7-day: <b>{len(wins)}W / {len(recent)-len(wins)}L</b>  "
        f"Win rate: <b>{wr:.0f}%</b>  Net: <b>{total_r:+.2f}R</b>"
    )
    if best:
        lines.append(f"🏆 Best: #{best.get('id')} {best.get('pair')} — {_r(best):+.2f}R")
    if worst and worst is not best:
        lines.append(f"💔 Worst: #{worst.get('id')} {worst.get('pair')} — {_r(worst):+.2f}R")
    return lines


def _send_in_parts(sections: list) -> None:
    MAX = 4000
    current: list[str] = []
    cur_len = 0
    for sec in sections:
        sec_text = "\n".join(sec)
        sec_len  = len(sec_text)
        if current and cur_len + sec_len > MAX:
            _telegram("\n".join(current))
            current = []
            cur_len = 0
        current.extend(sec)
        cur_len += sec_len
    if current:
        _telegram("\n".join(current))


# ── Trade block helpers ────────────────────────────────────────────────────────

import re as _re


def _parse_entry_window(bet_str: str) -> tuple:
    """Extract (session_label, enter_between, do_not_enter_after) from BEST_ENTRY_TIME."""
    bl = bet_str.lower()
    if "london" in bl:
        sess = "London open"
    elif "new york" in bl or " ny " in bl:
        sess = "New York open"
    elif "tokyo" in bl:
        sess = "Tokyo open"
    elif "sydney" in bl or "asian" in bl:
        sess = "Sydney open"
    else:
        sess = "optimal session"

    m = _re.search(r'(\d+(?::\d+)?(?:am|pm))\s*[–\-—]\s*(\d+(?::\d+)?(?:am|pm))',
                   bet_str, _re.IGNORECASE)
    if m:
        start_s = m.group(1)
        end_s   = m.group(2).lower()
        mid_s   = _add_90min(start_s)
        return sess, f"{start_s} – {mid_s}", end_s
    return sess, bet_str[:40] if bet_str else "session open", "session close"


def _add_90min(time_str: str) -> str:
    """Add 90 min to a time string like '5pm' → '6:30pm'."""
    m = _re.match(r'(\d+)(?::(\d+))?\s*(am|pm)', time_str.lower())
    if not m:
        return time_str
    h    = int(m.group(1))
    mins = int(m.group(2) or "0")
    ampm = m.group(3)
    if ampm == "pm" and h != 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    total = h * 60 + mins + 90
    nh = (total // 60) % 24
    nm = total % 60
    na = "am" if nh < 12 else "pm"
    dh = nh % 12 or 12
    return f"{dh}:{nm:02d}{na}" if nm else f"{dh}{na}"


def _management_lines(direction: str, entry_v: float, stop_v: float,
                      target_v: float, pair: str) -> list:
    """Compute breakeven / trailing stop management instructions."""
    dec = 3 if "JPY" in pair.upper() else 5
    try:
        if direction == "BUY":
            dist        = target_v - entry_v
            half_pt     = entry_v + dist * 0.50
            seventy5_pt = entry_v + dist * 0.75
            trail_stop  = entry_v + dist * 0.30
        else:
            dist        = entry_v - target_v
            half_pt     = entry_v - dist * 0.50
            seventy5_pt = entry_v - dist * 0.75
            trail_stop  = entry_v - dist * 0.30

        return [
            f"- Move stop to breakeven ({entry_v:.{dec}f}) when price reaches {half_pt:.{dec}f} (50% to target)",
            f"- Take half profit at {half_pt:.{dec}f} if you want to reduce risk",
            f"- Trail stop to {trail_stop:.{dec}f} when price reaches {seventy5_pt:.{dec}f} (75% to target)",
            f"- Full target: {target_v:.{dec}f} — let it run if momentum is strong",
        ]
    except (TypeError, ValueError):
        return [f"- Full target: {_fmt_price(target_v)} — let it run if momentum is strong"]


def _why_agrees_lines(r: dict, ctx: dict) -> list:
    """Build 'Why all data agrees' bullet points from the bundle."""
    p     = r["parsed"]
    pair  = r["pair"]
    dirn  = (p.get("direction") or "").upper()
    lines = []

    # Technical
    try:
        daily   = r["bundle"]["technical"]["daily"]
        rsi     = daily.get("rsi14")
        macd    = daily.get("macd", "")
        h4      = (r["bundle"]["technical"].get("h4") or
                   r["bundle"]["technical"].get("4h") or {})
        h4_rsi  = h4.get("rsi14") if isinstance(h4, dict) else None
        rsi_str = f"RSI {rsi:.0f}" if rsi is not None else "RSI —"
        mac_str = "bullish MACD" if "bullish" in str(macd).lower() else "bearish MACD"
        h4_str  = f" + 4H RSI {h4_rsi:.0f}" if h4_rsi is not None else ""
        lines.append(f"- Technical: {rsi_str} · {mac_str}{h4_str}")
    except (KeyError, TypeError, ValueError):
        s = p.get("technical_score")
        if s:
            lines.append(f"- Technical: score {s}/10")

    # Fundamental
    try:
        fund = r["bundle"]["fundamental"]
        diff = fund.get("rate_differential_pct")
        if diff is not None:
            carry = (fund.get("carry_note") or "")[:60]
            lines.append(f"- Fundamental: {diff:+.2f}% carry advantage — {carry}")
        else:
            fs = p.get("fundamental_score")
            if fs:
                lines.append(f"- Fundamental: score {fs}/10")
    except (KeyError, TypeError, ValueError):
        pass

    # COT positioning
    try:
        pos_bundle = r["bundle"]["positioning"]
        for side in ("base", "quote"):
            pp = pos_bundle.get(side, {})
            if pp.get("status") == "ok":
                pct  = pp.get("percentile_in_range")
                pdir = pp.get("direction", "")
                flag = (pp.get("extreme_flag") or "")[:70]
                if pct is not None:
                    lines.append(f"- Positioning: {pp['currency']} {pdir} at {pct:.0f}th pct — {flag}")
                    break
    except (KeyError, TypeError, ValueError):
        ps = p.get("positioning_score")
        if ps:
            lines.append(f"- Positioning: score {ps}/10")

    # Sentiment — draw from key_thesis if available
    kt = (p.get("key_thesis") or "").lower()
    sent_bundle = r["bundle"].get("sentiment", {})
    for side in ("base", "quote"):
        sb = sent_bundle.get(side, {})
        if sb.get("status") == "ok":
            cnt = sb.get("article_count", 0)
            ccy = sb.get("currency", "")
            # Infer tone from thesis
            tone = "mixed"
            if any(w in kt for w in ("bullish", "positive", "strong", "hawkish")):
                tone = "bullish" if dirn == "BUY" else "bearish"
            elif any(w in kt for w in ("bearish", "negative", "weak", "dovish")):
                tone = "bearish" if dirn == "SELL" else "bullish"
            lines.append(f"- Sentiment: {cnt} articles on {ccy} — {tone} news tone")
            break

    # Macro
    risk = ctx.get("risk_env", "⚖️ neutral")
    clean = pair.upper().replace("/", "")
    macro_note = ""
    if "JPY" in clean:
        macro_note = "safe-haven JPY suppressed in risk-on" if "risk-on" in risk else "safe-haven JPY supported"
    elif "USD" in clean:
        macro_note = "USD supported by risk-on flows" if "risk-on" in risk else "USD in defensive mode"
    elif "AUD" in clean or "NZD" in clean:
        macro_note = "commodity currencies benefit from risk-on" if "risk-on" in risk else "commodity currencies under pressure"
    lines.append(f"- Macro: {risk}{' — ' + macro_note if macro_note else ''}")

    return lines


# ── Cost-reduction helpers ─────────────────────────────────────────────────────

def _pre_filter_pairs(ranked_all: list, top_n: int = 20, log=print) -> list:
    """Score pairs using free FRED+COT data before fetching Twelve Data candles.

    Takes the ranked list from selector, pre-fetches fundamental rates and
    COT positioning for all unique currencies (free APIs, disk-cached), scores
    each pair, and returns the top_n symbols for Twelve Data fetching.
    """
    from src import fred as _fred

    all_pairs = [p for p, _ in ranked_all[:max(top_n * 2, 40)]]
    if not all_pairs:
        return []

    # Collect unique currencies
    unique_ccys: set = set()
    for pair in all_pairs:
        clean = pair.upper().replace("/", "").replace("-", "")
        if len(clean) == 6:
            unique_ccys.add(clean[:3])
            unique_ccys.add(clean[3:])

    log(f"Pre-filter: pre-fetching FRED+COT for {len(unique_ccys)} currencies ...")

    # Pre-fetch FRED rates (populate disk cache for all currencies at once)
    _rate_cache: dict = {}
    for ccy in unique_ccys:
        meta   = config.CURRENCIES.get(ccy, {})
        series = meta.get("rate_fred")
        if series:
            val, _ = _fred.latest(series)
            _rate_cache[ccy] = val

    # Pre-fetch COT positioning (populate disk cache)
    from src import positioning as _pos
    _cot_cache: dict = {}
    for ccy in unique_ccys:
        cot_data = _pos._for_currency(ccy)
        if cot_data.get("status") == "ok":
            _cot_cache[ccy] = cot_data.get("percentile_in_range", 50.0)

    # Score each pair
    scored: list = []
    for pair in all_pairs:
        clean = pair.upper().replace("/", "").replace("-", "")
        if len(clean) < 6:
            continue
        base, quote = clean[:3], clean[3:]
        score = 0.0

        # Rate differential: larger magnitude = more carry opportunity
        b_r = _rate_cache.get(base)
        q_r = _rate_cache.get(quote)
        if b_r is not None and q_r is not None:
            score += min(5.0, abs(b_r - q_r) / 0.5)

        # COT extreme: distance from 50th percentile = reversal opportunity
        for ccy in (base, quote):
            pct = _cot_cache.get(ccy)
            if pct is not None:
                score += abs(pct - 50.0) / 25.0  # max 2 pts per currency

        scored.append((pair, score))

    # Stable-sort: primary key = free-data score, secondary = original selector rank
    scored.sort(key=lambda x: x[1], reverse=True)
    top = [p for p, _ in scored[:top_n]]
    log(f"Pre-filter: {len(all_pairs)} candidates → top {len(top)} for Twelve Data")
    return top


def _pre_fetch_shared_data(pairs: list, log=print) -> tuple:
    """Pre-fetch FRED, COT, NewsAPI, and macro for all pairs at once.

    Returns (fund_store, macro_result) where fund_store maps pair → fundamental
    result dict. Subsequent analysis reads from these in-memory objects, avoiding
    duplicate disk-cache reads and API round-trips.
    """
    from src import fundamental as _fund, macro as _mac

    log("Batch pre-fetch: fetching shared fundamental + macro data ...")
    macro_result = _mac.analyse()

    fund_store: dict = {}
    for pair in pairs:
        clean = pair.upper().replace("/", "").replace("-", "")
        if len(clean) == 6:
            base, quote = clean[:3], clean[3:]
            try:
                fund_store[pair] = _fund.analyse(base, quote)
            except Exception:
                pass

    hits = sum(1 for v in fund_store.values() if v.get("status") == "ok")
    log(f"Batch pre-fetch: {len(fund_store)} fundamental results ({hits} ok) + 1 macro")
    return fund_store, macro_result


# ── Main summary builder ───────────────────────────────────────────────────────

def _send_telegram_summary(
    date: str,
    universe_size: int,
    total_scanned: int,
    deep_results: list,
    closed_today: list = None,
    new_patterns: list = None,
    stats: dict = None,
    risk_data: dict = None,
    stage1_filtered: list = None,
    failed_pairs: list = None,
    credit_data: dict = None,
    run_stats: dict = None,
    td_calls: int = 0,
    run_duration_min: float = 0.0,
) -> None:
    """Build and send the reformatted daily Telegram notification."""
    closed_today    = closed_today    or []
    new_patterns    = new_patterns    or []
    stage1_filtered = stage1_filtered or []
    failed_pairs    = failed_pairs    or []
    run_stats       = run_stats       or {}
    credit_data     = credit_data     or {}

    yes_trades  = [r for r in deep_results if r["parsed"].get("trade_this") == "YES"]
    watch_list  = sorted(
        [r for r in deep_results
         if r["parsed"].get("trade_this") != "YES" and 5 <= _conf(r) <= 6],
        key=_conf, reverse=True,
    )[:3]
    near_misses = sorted(
        [r for r in deep_results if r["parsed"].get("trade_this") != "YES"],
        key=_conf, reverse=True,
    )
    upcoming = sorted(
        [r for r in near_misses if 3 <= _conf(r) <= 4],
        key=_conf, reverse=True,
    )[:3]

    _sizes, _exposure, _risk_state = {}, {}, {}
    if risk_data:
        for s in (risk_data.get("sized_trades") or []):
            _sizes[s["pair"]] = s
        _exposure   = risk_data.get("exposure", {})
        _risk_state = risk_data.get("risk_state", {})

    ctx          = _derive_market_context(deep_results, risk_data)
    now_ak       = _auckland_now()
    tomorrow_ak  = now_ak + timedelta(days=1)
    today_short  = _fmt_date_short_nz(now_ak)
    all_sections: list[list[str]] = []

    # ── HEADER ─────────────────────────────────────────────────────────────────
    n_deep = len(deep_results)
    if yes_trades:
        setup_line = f"<b>🟢 {len(yes_trades)} setup{'s' if len(yes_trades) > 1 else ''} found</b>"
    else:
        setup_line = "No setups today"

    all_sections.append([
        f"<b>🤖 Forex AI — {_fmt_date_nz(now_ak)}</b>",
        f"Universe: {universe_size} · Deep analysed: <b>{n_deep}</b> · {setup_line}",
    ])

    # ── MARKET CONTEXT (4 lines max) ───────────────────────────────────────────
    ctx_lines = ["", "━━━━━━━━━━━━━━━━━━━━━", "🌍 <b>MARKET CONTEXT</b>"]

    vix_str = f"VIX {ctx['vix']:.1f}" if ctx["vix"] else ""
    env_str = ctx["risk_env"]
    ctx_lines.append(f"Environment: <b>{env_str}</b>{' (' + vix_str + ')' if vix_str else ''}")

    sc = ctx.get("strongest_ccy")
    wc = ctx.get("weakest_ccy")
    scores = ctx.get("ccy_scores", {})
    if sc:
        sc_reason = "carry + risk-on" if "risk-on" in env_str else "carry + fundamentals"
        ctx_lines.append(f"💪 Strongest: <b>{sc}</b> — {sc_reason} (+{scores.get(sc,0):.0f})")
    if wc:
        wc_reason = "low rates + risk-on selling" if "risk-on" in env_str else "weak fundamentals"
        ctx_lines.append(f"📉 Weakest: <b>{wc}</b> — {wc_reason} ({scores.get(wc,0):.0f})")

    # Event warnings from analyses
    nw_list = []
    for r in deep_results:
        nw = (r["parsed"].get("news_warning") or "").strip()
        if nw and len(nw) > 5 and "none" not in nw.lower() and "n/a" not in nw.lower():
            nw_list.append(f"{r['pair']}: {nw[:60]}")
    if nw_list:
        ctx_lines.append(f"⚡ {nw_list[0]}")

    all_sections.append(ctx_lines)

    # ── TRADE ALERTS ───────────────────────────────────────────────────────────
    if yes_trades:
        for r in yes_trades:
            p         = r["parsed"]
            pair      = r["pair"]
            direction = (p.get("direction") or "?").upper()
            conf      = p.get("confidence") or "?"
            entry_raw = p.get("entry")
            stop_raw  = p.get("stop_loss")
            tgt_raw   = p.get("target")

            sz       = _sizes.get(pair, {})
            adj_stop = sz.get("adj_stop") or stop_raw
            adj_tgt  = sz.get("adj_target") or tgt_raw

            # Compute R:R
            rr_num = None
            try:
                rr_num = abs(float(adj_tgt) - float(entry_raw)) / abs(float(entry_raw) - float(adj_stop))
                rr_str = f"{rr_num:.2f}:1"
            except (TypeError, ValueError, ZeroDivisionError):
                try:
                    rr_num = float(p.get("reward_risk") or 0)
                    rr_str = f"{rr_num:.2f}:1" if rr_num else "—"
                except (TypeError, ValueError):
                    rr_str = "—"

            # Dollar risk + profit
            risk_amt = float(sz.get("risk_amount") or 0)
            cur      = sz.get("currency", "USD")
            pct      = sz.get("risk_pct", 1.0)
            try:
                profit_amt = round(risk_amt * rr_num) if rr_num else None
            except (TypeError, ValueError):
                profit_amt = None

            # Pip distances
            pip_risk   = _fmt_pips_between(pair, entry_raw, adj_stop)
            pip_target = _fmt_pips_between(pair, entry_raw, adj_tgt)

            action_icon = "🟢" if direction == "BUY" else "🔴"

            # Entry window
            bet = (p.get("best_entry_time") or "").strip()
            bet_clean = bet.replace("NZT", "Auckland time").replace("nzt", "Auckland time")
            sess_name, enter_window, end_time = _parse_entry_window(bet_clean or _session_label(pair))

            # Thesis + risk factors
            kt  = (p.get("key_thesis") or "").strip()
            rf  = (p.get("risk_factors") or "").strip()
            rf_parts = [x.strip() for x in rf.replace(";", "\n").split("\n") if x.strip()][:2]

            block = [
                "",
                f"{action_icon} <b>ACTION: {direction} {pair} NOW</b>",
                "━━━━━━━━━━━━━━━━━━━━━",
                f"💰 Entry: {_fmt_price(entry_raw)}",
                f"🛑 Stop Loss: {_fmt_price(adj_stop)}  ({pip_risk} risk)",
                f"🎯 Take Profit: {_fmt_price(adj_tgt)}  ({pip_target} target)",
            ]
            if profit_amt and risk_amt:
                block.append(f"📊 Risk ${risk_amt:,.0f} → Make ${profit_amt:,.0f}  ({rr_str} reward)")
            elif risk_amt:
                block.append(f"📊 Risk ${risk_amt:,.0f} {cur}  ({pct:.2f}% account)")
            if sz.get("lots"):
                block.append(f"📏 Position Size: {sz['lots']} lots")
            block.append("━━━━━━━━━━━━━━━━━━━━━")
            block.append("⏰ <b>EXACT ENTRY INSTRUCTIONS:</b>")
            block.append(f"- Best session: {sess_name} — highest {pair} volume")
            block.append(f"- Enter between: {enter_window} Auckland time TODAY")
            block.append(f"- Do NOT enter after: {end_time} Auckland time")
            block.append(f"- Do NOT enter if price moves more than 30 pips from entry before your session")
            block.append(f"- Wait for price to reach {_fmt_price(entry_raw)} — do not chase if already moved")
            block.append("- If price gaps past entry on open — skip this trade entirely")
            block.append("━━━━━━━━━━━━━━━━━━━━━")
            block.append(f"📈 Confidence: {conf}/10  {_conf_bar(conf)}")
            block.append("🔍 <b>Why all data agrees:</b>")
            for why_line in _why_agrees_lines(r, ctx):
                block.append(why_line)
            block.append("━━━━━━━━━━━━━━━━━━━━━")
            block.append("⚠️ <b>Risk Factors:</b>")
            for rf_line in (rf_parts or [rf[:100]] if rf else ["—"]):
                block.append(f"- {rf_line}")
            block.append("━━━━━━━━━━━━━━━━━━━━━")
            block.append("📋 <b>Trade Management After Entry:</b>")
            try:
                for mgmt in _management_lines(direction, float(entry_raw),
                                              float(adj_stop), float(adj_tgt), pair):
                    block.append(mgmt)
            except (TypeError, ValueError):
                block.append(f"- Full target: {_fmt_price(adj_tgt)}")

            all_sections.append([ln for ln in block if ln])

    # ── WHY NO SETUPS (when no trades, max 3 pairs, 2 lines each) ─────────────
    if not yes_trades:
        no_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "💤 <b>WHY NO SETUPS TODAY</b>"]

        def _s1_sort_key(rr):
            return float(rr.get("screen", {}).get("score") or 0)

        combined = list(near_misses) + sorted(stage1_filtered, key=_s1_sort_key, reverse=True)
        top3     = combined[:3]

        if top3:
            for i, rr in enumerate(top3, 1):
                reason = _rejection_reason(rr)
                no_sec.append(f"<b>{i}. {rr['pair']}</b>  {_conf(rr)}/10")
                no_sec.append(f"   {reason}")
        elif failed_pairs:
            no_sec.append(f"⚠️ All {len(failed_pairs)} pairs failed — check ANTHROPIC_API_KEY in GitHub secrets")
        else:
            no_sec.append("No qualifying setups — staying in cash is a valid position.")

        all_sections.append(no_sec)

    # ── WATCH LIST (max 3 pairs, 3 lines each) ─────────────────────────────────
    watch_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "👀 <b>WATCH LIST</b>"]
    if watch_list:
        for rr in watch_list:
            pp    = rr["parsed"]
            conf  = pp.get("confidence") or "?"
            dirn  = (pp.get("direction") or "—").upper()
            arrow = "📈" if dirn == "BUY" else "📉"
            ntc   = _what_needs_to_change(pp)
            watch_sec.append(f"")
            watch_sec.append(f"{arrow} <b>{rr['pair']}</b> {dirn}  {conf}/10 {_conf_bar(conf)}")
            watch_sec.append(f"<code>{_score_breakdown_line(pp)}</code>")
            watch_sec.append(f"Needs: {ntc}")
    else:
        watch_sec.append("No pairs in the 5–6 confidence range today.")
    all_sections.append(watch_sec)

    # ── UPCOMING OPPORTUNITIES (pairs 3–4 conf, approaching watch list) ────────
    if upcoming:
        up_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "📡 <b>APPROACHING SIGNAL</b>"]
        for rr in upcoming:
            pp   = rr["parsed"]
            conf = pp.get("confidence") or "?"
            dirn = (pp.get("direction") or "—").upper()
            kt   = (pp.get("key_thesis") or "").strip()
            kt50 = (kt[:80] + "…") if len(kt) > 80 else kt

            # What specifically needs to change
            ntc = _what_needs_to_change(pp)
            up_sec.append(f"<b>{rr['pair']}</b> — {conf}/10 {dirn}: {kt50}")
            up_sec.append(f"  Trigger: {ntc}")
        all_sections.append(up_sec)

    # ── LEARNING UPDATE (2 lines) ───────────────────────────────────────────────
    learn_lines = ["", "━━━━━━━━━━━━━━━━━━━━━"]
    if stats:
        wr     = stats.get("win_rate")
        wr_txt = f"{wr*100:.0f}%" if wr is not None else "—"
        wins   = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        dec    = stats.get("decisive", 0)
        cl     = _risk_state.get("consecutive_losses", 0)
        cw     = _risk_state.get("consecutive_wins", 0)

        if cw >= 2:
            streak = f"🔥 {cw} consecutive wins"
        elif cl >= 2:
            streak = f"⚠️ {cl} consecutive losses"
        elif cw == 1:
            streak = "1 win (no streak yet)"
        elif cl == 1:
            streak = "1 loss (watching for streak)"
        else:
            streak = "No streak active"

        learn_lines.append(f"🧠 Win rate: <b>{wr_txt}</b>  ({wins}W/{losses}L · {dec} trades)")
        learn_lines.append(f"🔁 Streak: {streak}")
    else:
        learn_lines.append("🧠 Win rate: building history...")
    all_sections.append(learn_lines)

    # ── RISK DASHBOARD (1 line) ─────────────────────────────────────────────────
    if risk_data and risk_data.get("profile"):
        try:
            prof  = risk_data["profile"]
            rmode = _risk_state.get("risk_mode", "normal")
            rpct  = _risk_state.get("base_risk_pct", 1.0)
            exp   = _exposure.get("total_pct", 0.0)
            bal   = prof.get("account_balance", config.ACCOUNT_BALANCE)
            mode_icons = {
                "capital_protection": "⬇️",
                "streak_protection":  "⬇️",
                "reduced":            "➡️",
                "normal":             "➡️",
                "enhanced":           "⬆️",
            }
            icon = mode_icons.get(rmode, "➡️")
            all_sections.append([
                f"⚙️ ${bal:,.0f} | {rpct:.1f}%/trade | {exp:.1f}% open | {icon} {rmode.replace('_',' ').title()}"
            ])
        except Exception:
            pass

    # ── CREDIT BALANCE (1 line) ─────────────────────────────────────────────────
    try:
        from src import billing as _bill, analyst as _anl
        primary_bal = credit_data.get("primary_balance")
        backup_bal  = credit_data.get("backup_balance")
        total_bal   = (primary_bal or 0.0) + (backup_bal or 0.0 if config.ANTHROPIC_API_KEY_2 else 0.0)
        daily_cost  = config.DAILY_COST_USD
        if total_bal > 0 and daily_cost > 0:
            runway = int(total_bal / daily_cost)
            all_sections.append([f"💳 ${total_bal:.2f} combined ({runway} days)"])
        elif primary_bal is not None:
            all_sections.append([f"💳 ${primary_bal:.2f} primary"])
    except Exception:
        pass

    # ── WEEKLY PERFORMANCE (Monday only) ───────────────────────────────────────
    weekly = _weekly_performance_section(date)
    if weekly:
        all_sections.append(weekly)

    # ── SYSTEM HEALTH ──────────────────────────────────────────────────────────
    health_issues: list = []

    # Technical data weak
    weak_tech = [
        r for r in deep_results
        if r["parsed"].get("technical_score") in (None, 1)
    ]
    if weak_tech and len(weak_tech) >= max(1, len(deep_results) // 2):
        health_issues.append("Technical data weak — T:1 or T:N/A across most pairs, may need investigation")

    # Twelve Data quota
    if td_calls > 600:
        health_issues.append(f"Twelve Data quota at risk — {td_calls} calls used today (limit ~800)")

    # Anthropic credits
    try:
        primary_bal = credit_data.get("primary_balance")
        if primary_bal is not None:
            if primary_bal < 2.0:
                health_issues.append(f"🚨 PRIMARY CREDITS URGENT — ${primary_bal:.2f} remaining, top up NOW")
            elif primary_bal < 5.0:
                health_issues.append(f"Primary credits low — ${primary_bal:.2f} remaining, top up soon")
    except Exception:
        pass

    # Failed pairs
    if failed_pairs:
        health_issues.append(f"{len(failed_pairs)} pair{'s' if len(failed_pairs) > 1 else ''} failed analysis — check GitHub Actions logs")

    # Run duration
    if run_duration_min > 20:
        health_issues.append(f"Run took {run_duration_min:.0f} minutes — longer than normal")

    # Account balance at default
    default_bal = config.ACCOUNT_BALANCE in (10000.0, 100000.0)
    if default_bal:
        health_issues.append(f"Account balance not configured — set ACCOUNT_BALANCE in GitHub secrets")

    # Estimated cost (always shown)
    est_usd = run_stats.get("estimated_usd", 0.0)
    cache_h = run_stats.get("cache_hits", 0)
    cost_line = f"Todays run cost approximately ${est_usd:.2f} USD"
    if cache_h:
        cost_line += f" ({cache_h} pair{'s' if cache_h > 1 else ''} skipped — price unchanged)"

    health_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "⚠️ <b>SYSTEM HEALTH</b>"]
    for issue in health_issues:
        health_sec.append(f"- {issue}")
    health_sec.append(f"- {cost_line}")
    all_sections.append(health_sec)

    # ── FOOTER ─────────────────────────────────────────────────────────────────
    all_sections.append([
        "",
        f"<i>⏰ Next scan tomorrow {_fmt_date_short_nz(tomorrow_ak)} at 6am Auckland time</i>",
    ])

    _send_in_parts(all_sections)


# ── Daily run ──────────────────────────────────────────────────────────────────

_LAST_RUN_FILE = config.REPORTS_DIR.parent / "last_run.txt"
_COOLDOWN_SECS = 3600  # 60 minutes


def run() -> int:
    _run_start = time.time()

    # ── Duplicate-run guard ────────────────────────────────────────────────────
    _now_utc = datetime.utcnow()
    try:
        _LAST_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _LAST_RUN_FILE.exists():
            _last_ts = datetime.fromisoformat(_LAST_RUN_FILE.read_text().strip())
            _elapsed = (_now_utc - _last_ts).total_seconds()
            if _elapsed < _COOLDOWN_SECS:
                print(
                    f"[guard] Duplicate run blocked — last run started "
                    f"{int(_elapsed / 60)}m {int(_elapsed % 60)}s ago. Exiting.",
                    file=sys.stderr,
                )
                return 0
        _LAST_RUN_FILE.write_text(_now_utc.isoformat())
    except Exception as _guard_err:
        print(f"[guard] last_run.txt check failed ({_guard_err}) — proceeding.", file=sys.stderr)

    missing = config.missing_keys()
    if missing:
        print("ERROR: missing API keys in .env: " + ", ".join(missing), file=sys.stderr)
        return 2

    now_ak   = _auckland_now()
    date     = now_ak.strftime("%Y-%m-%d")
    log_path = config.REPORTS_DIR / f"daily_{date}.log"

    # Reset per-run state
    try:
        from src import analyst as _anl, technical as _tech
        _anl.reset_key_state()
        _tech.reset_call_count()
    except Exception:
        pass

    with log_path.open("a", encoding="utf-8") as logf:

        # 0. Automatic outcome detection
        closed_today = []
        new_patterns = []
        try:
            from src import outcome_checker, outcome_analyst
            closed_today = outcome_checker.check_open_trades(log=lambda m: _log_line(logf, m))
            if closed_today:
                new_patterns = outcome_analyst.run_outcome_analysis(
                    closed_today, log=lambda m: _log_line(logf, m)
                )
        except Exception as exc:
            _log_line(logf, f"Outcome step failed: {exc}")

        # 1. Learn from prior outcomes
        learning_stats = None
        try:
            learning_stats = learning.update_memory()
            _log_line(
                logf,
                f"Learning refreshed: {learning_stats['closed']} closed trades, "
                f"win rate {('%.0f%%' % (learning_stats['win_rate']*100)) if learning_stats['win_rate'] is not None else 'n/a'}, "
                f"{learning_stats['patterns_written']} auto-patterns written.",
            )
        except Exception as exc:
            _log_line(logf, f"Learning step failed: {exc}")

        # 2. Smart pair selection
        universe_size = len(selector.UNIVERSE)
        ranked_all    = []
        pairs_today   = []
        try:
            selection     = selector.select_pairs(top_n=15, log=lambda m: _log_line(logf, m))
            pairs_today   = selection["selected"]
            ranked_all    = selection["ranked"]
            universe_size = selection["universe_size"]
            _log_line(
                logf,
                f"Selected {len(pairs_today)} pairs from universe of {universe_size}: "
                f"{', '.join(pairs_today)}",
            )
        except Exception as exc:
            _log_line(logf, f"Smart selection failed ({exc}) — falling back to watchlist.")
            pairs_today = list(config.WATCHLIST)

        # 2b. COST OPTIMISATION — Pre-filter using free data before Twelve Data fetch
        # Take top candidates from ranked_all and score on FRED+COT to determine which
        # 20 pairs are worth fetching price candles for.
        try:
            if ranked_all:
                pre_filtered = _pre_filter_pairs(
                    ranked_all, top_n=20, log=lambda m: _log_line(logf, m)
                )
                # Ensure pairs_today (selector's top picks) are always in the pool
                for p in pairs_today:
                    if p not in pre_filtered:
                        pre_filtered.insert(0, p)
                pre_filtered = pre_filtered[:20]
            else:
                pre_filtered = pairs_today
            _log_line(logf, f"Pre-filtered pool: {len(pre_filtered)} pairs for Twelve Data fetch")
        except Exception as exc:
            _log_line(logf, f"Pre-filter failed ({exc}) — using selector output.")
            pre_filtered = pairs_today

        # 3. COST OPTIMISATION — Batch pre-fetch shared data (FRED, COT, macro) once
        _shared_fund: dict = {}
        _shared_macro      = None
        try:
            _shared_fund, _shared_macro = _pre_fetch_shared_data(
                pre_filtered, log=lambda m: _log_line(logf, m)
            )
        except Exception as exc:
            _log_line(logf, f"Shared data pre-fetch failed ({exc}) — each pair will fetch independently.")

        # 4. Pre-fetch Twelve Data candles for pre-filtered pairs only (max 20)
        try:
            from src import technical as _tech
            _tech.warm_cache(pre_filtered, log=lambda m: _log_line(logf, m))
        except Exception as exc:
            _log_line(logf, f"Technical pre-fetch failed (analysis will still run): {exc}")

        # Diagnostic: log indicator snapshot
        _log_line(logf, "[DIAG] Technical indicator snapshot (cache, no API calls):")
        for _diag_pair in ["EUR/USD", "USD/JPY", "AUD/CAD"]:
            try:
                from src import technical as _tech
                _ind = _tech.read_cached_indicators(_diag_pair)
                if _ind:
                    _ts = _ind.get("tech_signal", {})
                    _log_line(logf, (
                        f"  {_diag_pair}: RSI={_ind['rsi14']}  "
                        f"MACD_hist={_ind['macd_hist']}  "
                        f"→ signal={_ts.get('direction','?')} {_ts.get('score','?')}/10"
                    ))
            except Exception:
                pass

        _log_line(logf, f"=== Daily run {date} | universe: {universe_size} pairs ===")
        _log_line(logf, f"[DIAG] CLAUDE_MODEL={repr(config.CLAUDE_MODEL)}")
        _log_line(logf, f"[DIAG] HAIKU_MODEL={repr(config.HAIKU_MODEL)}")
        _log_line(logf, f"[DIAG] pairs_today ({len(pairs_today)}): {pairs_today}")

        # 5. Analyse pairs
        filtered_count  = 0
        stage1_filtered: list = []
        failed_pairs:    list = []
        deep_results          = []
        analysed_pairs: set   = set()

        def _process_batch(pairs, force_deep=False):
            nonlocal filtered_count
            for pair in pairs:
                if pair in analysed_pairs:
                    _log_line(logf, f"  {pair}: CACHE HIT (already analysed this run)")
                    continue
                analysed_pairs.add(pair)
                result = _analyse_pair(
                    pair, logf,
                    force_deep=force_deep,
                    shared_fundamental=_shared_fund.get(pair),
                    shared_macro=_shared_macro,
                )
                if result is None:
                    failed_pairs.append(pair)
                    continue
                if result.get("screened_out"):
                    filtered_count += 1
                    s = result["screen"]
                    stage1_filtered.append(result)
                    _log_line(
                        logf,
                        f"  {result['pair']}: FILTERED stage-1 "
                        f"(score {s['score']}/5 — {s['reason']})",
                    )
                    continue
                pp = result["parsed"]
                skipped = "SKIP-UNCHANGED " if result.get("skipped_unchanged") else ""
                verdict = f"{pp['trade_this']} | conf {pp['confidence']} | {pp['direction']}"
                _log_line(logf, f"#{result['id']} {result['pair']}: {skipped}{verdict}")
                deep_results.append(result)

        # Merit-selected pairs go directly to Sonnet (Haiku bypassed for initial 15)
        _log_line(logf, f"Sending {len(pairs_today)} merit-selected pairs to Sonnet: {', '.join(pairs_today)}")
        _process_batch(pairs_today, force_deep=True)

        # Auto-expand if fewer than 3 meaningful results
        meaningful = [r for r in deep_results if _conf(r) >= 5]
        next_idx   = len(pairs_today)

        while len(meaningful) < 3 and len(deep_results) < 25 and next_idx < len(ranked_all):
            extra_pairs = [p for p, _ in ranked_all[next_idx:next_idx + 5]
                           if p in pre_filtered]  # only pre-filtered pairs (already cached)
            if not extra_pairs:
                # Fall back to un-filtered expansion if pre-filtered pool exhausted
                extra_pairs = [p for p, _ in ranked_all[next_idx:next_idx + 5]]
            next_idx += 5
            if extra_pairs:
                _log_line(logf, f"Expanding: {len(deep_results)} results, {len(meaningful)} conf>=5. Adding {len(extra_pairs)} more.")
                _process_batch(extra_pairs)
                meaningful = [r for r in deep_results if _conf(r) >= 5]

        # Minimum guarantee
        if len(deep_results) == 0:
            _log_line(logf, "WARNING: deep_results=0. Activating minimum guarantee.")
            fallback_pairs = [p for p, _ in ranked_all[:5]] if ranked_all else list(config.WATCHLIST[:5])
            _process_batch(fallback_pairs, force_deep=True)

        passed = len(deep_results)
        _log_line(
            logf,
            f"Analysis complete: universe={universe_size} · "
            f"stage-1 filtered={filtered_count} · deep-analysed={passed} · "
            f"meaningful(conf>=5)={len(meaningful)} · failed={len(failed_pairs)}",
        )

        # 6. Rebuild dashboard
        try:
            path = dashboard.generate()
            _log_line(logf, f"Dashboard updated: {path}")
        except Exception as exc:
            _log_line(logf, f"Dashboard step failed: {exc}")

        actionable = [
            f"{r['pair']} {r['parsed']['direction']} (conf {r['parsed']['confidence']})"
            for r in deep_results if r["parsed"].get("trade_this") == "YES"
        ]
        if actionable:
            _log_line(logf, "ACTIONABLE TODAY: " + "; ".join(actionable))
        else:
            _log_line(logf, "No actionable setups today.")
        _log_line(logf, "=== Daily run complete ===")

        # 7. Risk management
        risk_data = {}
        try:
            from src import risk_manager
            risk_profile = risk_manager.load_profile()
            risk_state   = risk_manager.compute_risk_state(risk_profile)
            exposure     = risk_manager.compute_open_exposure(risk_profile)
            sized = [
                risk_manager.size_trade_from_result(r, risk_profile, risk_state)
                for r in deep_results if r["parsed"].get("trade_this") == "YES"
            ]
            sized = risk_manager.apply_correlation_checks(sized)
            risk_profile.update({
                "risk_mode":          risk_state["risk_mode"],
                "consecutive_losses": risk_state["consecutive_losses"],
                "consecutive_wins":   risk_state["consecutive_wins"],
                "last_5_win_rate":    risk_state["last_5_win_rate"],
                "total_open_pct":     exposure["total_pct"],
            })
            risk_manager.save_profile(risk_profile)
            risk_data = {
                "profile":      risk_profile,
                "risk_state":   risk_state,
                "exposure":     exposure,
                "sized_trades": sized,
            }
        except Exception as exc:
            _log_line(logf, f"Risk management step failed: {exc}")

        # 8. Fetch API credit balances
        credit_data = {}
        try:
            from src import billing as _bill
            primary_bal = _bill.fetch_balance(config.ANTHROPIC_API_KEY)
            backup_bal  = _bill.fetch_balance(config.ANTHROPIC_API_KEY_2)
            credit_data = {"primary_balance": primary_bal, "backup_balance": backup_bal}
        except Exception as exc:
            _log_line(logf, f"Credit balance check failed: {exc}")

        # 9. Cost summary
        run_duration_min = (time.time() - _run_start) / 60.0
        try:
            from src import analyst as _anl, technical as _tech_mod
            run_stats = _anl.get_run_stats()
            td_calls  = _tech_mod.get_call_count()
        except Exception:
            run_stats, td_calls = {}, 0

        _log_line(logf, (
            f"[COST] Haiku in={run_stats.get('haiku_input',0)} out={run_stats.get('haiku_output',0)} · "
            f"Sonnet in={run_stats.get('sonnet_input',0)} out={run_stats.get('sonnet_output',0)} · "
            f"TD calls={td_calls} · est=${run_stats.get('estimated_usd',0):.4f} · "
            f"cache_hits={run_stats.get('cache_hits',0)} · "
            f"duration={run_duration_min:.1f}m"
        ))

        # 10. Send Telegram summary
        _send_telegram_summary(
            date=date,
            universe_size=universe_size,
            total_scanned=len(analysed_pairs),
            deep_results=deep_results,
            closed_today=closed_today,
            new_patterns=new_patterns,
            stats=learning_stats,
            risk_data=risk_data,
            stage1_filtered=stage1_filtered,
            failed_pairs=failed_pairs,
            credit_data=credit_data,
            run_stats=run_stats,
            td_calls=td_calls,
            run_duration_min=run_duration_min,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
