"""Daily automation runner (intended for a 6am scheduled task).

Sequence:
  1. Refresh learning memory from any outcomes recorded since the last run.
  2. Fetch the full Twelve Data forex universe; pre-score all pairs by session
     alignment, economic events, momentum, and volatility; select the top 15.
  3. Analyse each selected pair (Haiku stage-1 screen -> Sonnet deep analysis).
  4. Auto-expand: if fewer than 3 pairs score confidence 5+, pull the next 10
     from the pre-scored ranked list and analyse them too.  Keep expanding in
     batches of 10 until either 3 meaningful results exist or 25 pairs have
     been deeply analysed.
  5. Regenerate the HTML dashboard.
  6. Send a fully detailed Telegram summary explaining every result.

Each pair is fault-isolated: one failure is logged and the run continues.
A per-run log is written to data/reports/daily_<date>.log.
"""

import sys
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

# Trading session windows expressed in Auckland time (no DST suffix — the
# _auckland_now() helper handles NZST/NZDT automatically).
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
    """Return the current datetime in Auckland (Pacific/Auckland) time.

    Uses Python's zoneinfo module (stdlib since 3.9) which reads the IANA
    timezone database and handles NZST (UTC+12, April–September) and
    NZDT (UTC+13, September–April) automatically.  Falls back to a manual
    offset approximation when zoneinfo is unavailable.
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Pacific/Auckland"))
    except Exception:
        # Manual fallback: rough approximation only
        utc  = datetime.now(__import__("datetime").timezone.utc)
        off  = 13 if utc.month in (10, 11, 12, 1, 2, 3) else 12
        from datetime import timezone
        return utc.astimezone(timezone(timedelta(hours=off)))


def _fmt_time_nz(dt: datetime) -> str:
    """6am  or  6:08am  (no leading zero, no :00 on the hour)."""
    h    = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    return f"{h}:{dt.minute:02d}{ampm}" if dt.minute else f"{h}{ampm}"


def _fmt_date_nz(dt: datetime) -> str:
    """Thursday 4 June 2026"""
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B')} {dt.year}"


def _fmt_date_short_nz(dt: datetime) -> str:
    """Thursday 4 June  (no year)"""
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B')}"


def _session_label(pair: str) -> str:
    """Return e.g. 'London session 5pm – 9pm Auckland time' for a pair."""
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


def _analyse_pair(pair: str, logf, force_deep: bool = False) -> dict | None:
    try:
        return service.analyse_and_log(pair, log=lambda m: _log_line(logf, m), force_deep=force_deep)
    except Exception as exc:
        _log_line(logf, f"FAILED {pair}: {exc}")
        traceback.print_exc(file=logf)
        return None


# ── Telegram context helpers ───────────────────────────────────────────────────

def _derive_market_context(deep_results: list, risk_data: dict) -> dict:
    """Derive overall market environment from analysis results and macro bundles."""
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
    """Compact one-line score breakdown: T:8  F:7  S:6  P:N/A  M:8"""
    def _s(key):
        v = parsed.get(key)
        return str(v) if v is not None else "N/A"
    return (
        f"T:{_s('technical_score')}  "
        f"F:{_s('fundamental_score')}  "
        f"S:{_s('sentiment_score')}  "
        f"P:{_s('positioning_score')}  "
        f"M:{_s('macro_score')}"
    )


def _what_needs_to_change(parsed: dict) -> str:
    """For a 5-6 conf watch pair, explain the specific thing that would trigger a full alert."""
    scores = {
        "Technical":   parsed.get("technical_score"),
        "Fundamental": parsed.get("fundamental_score"),
        "Sentiment":   parsed.get("sentiment_score"),
        "Positioning": parsed.get("positioning_score"),
        "Macro":       parsed.get("macro_score"),
    }
    missing = [k for k, v in scores.items() if v is None]
    present = {k: v for k, v in scores.items() if v is not None}
    weak    = {k: v for k, v in present.items() if v < 7}

    parts = []
    if missing:
        parts.append(f"Restore {' + '.join(missing)} data (missing layers cap confidence)")
    if weak:
        for name, score in sorted(weak.items(), key=lambda x: x[1])[:2]:
            if score <= 4:
                parts.append(f"{name} needs a catalyst (currently {score}/10 — very weak)")
            elif score == 5:
                parts.append(f"{name} at {score}/10 — needs momentum shift to 7+")
            else:
                parts.append(f"{name} at {score}/10 — one more confirmation triggers alert")
    if not parts:
        parts.append("All layers close — wait for price to reach key level or momentum to build")
    return "; ".join(parts)


def _rejection_reason(result: dict) -> str:
    """Explain exactly why a pair was rejected — plain English, layer by layer."""
    # Stage-1 screener rejection (never went to deep analysis)
    if result.get("screened_out"):
        s      = result.get("screen", {})
        score  = s.get("score", "?")
        reason = (s.get("reason") or "").strip()
        if not reason:
            reason = "Technical and fundamental signals both below minimum threshold"
        return f"Stage 1 screener score {score}/5 — {reason}"

    # Deep-analysed but below 7+ confidence
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
    for k, v in below[:3]:
        if v <= 2:
            parts.append(f"{k} very weak ({v}/10 — no clear signal)")
        elif v <= 4:
            parts.append(f"{k} weak ({v}/10 — below threshold)")
        elif v <= 5:
            parts.append(f"{k} at {v}/10 — needs upward momentum")
        else:
            parts.append(f"{k} at {v}/10 — needs one more confirmation to reach 7+")
    if missing:
        parts.append(f"{' and '.join(missing)} data unavailable (caps overall confidence)")
    if not parts:
        present = [v for v in scores.values() if v is not None]
        if present:
            avg = sum(present) / len(present)
            parts.append(f"average layer score {avg:.1f}/10 — confluence just below trade threshold")

    reason_str = "; ".join(parts) if parts else "overall confluence insufficient for a trade"
    return f"Confidence {conf}/10 {direction} — {reason_str}"


def _get_volatility_info(result: dict) -> str:
    """ATR-based volatility description."""
    try:
        atr   = float(result["bundle"]["technical"]["daily"]["atr14"])
        entry = result["parsed"].get("entry")
        pair  = result.get("pair", "")
        dec   = 3 if "JPY" in pair.upper() else 5
        if entry:
            atr_pct = atr / float(entry) * 100
            level   = "High" if atr_pct > 1.0 else ("Normal" if atr_pct > 0.5 else "Low")
            return f"{level} (ATR14: {atr:.{dec}f}, {atr_pct:.2f}% daily range)"
        return f"ATR14: {atr:.5f}"
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return "data unavailable"


def _get_mtf_alignment(result: dict, direction: str) -> str:
    """Check if D1 and 4H trends align with the trade direction."""
    try:
        daily = result["bundle"]["technical"]["daily"]
        h4    = result["bundle"]["technical"].get("h4") or {}

        def _trend(tech: dict) -> str | None:
            c = tech.get("close")
            s = tech.get("sma50")
            if c is None or s is None:
                return None
            return "bullish" if float(c) > float(s) else "bearish"

        d_trend  = _trend(daily)
        h_trend  = _trend(h4)
        expected = "bullish" if direction.upper() == "BUY" else "bearish"

        if d_trend and h_trend:
            if d_trend == h_trend == expected:
                return f"✅ D1+4H both {d_trend} — aligned with {direction}"
            elif d_trend == h_trend:
                return f"⚠️ D1+4H both {d_trend} — contrary to {direction}"
            return f"⚠️ Mixed (D1: {d_trend}, 4H: {h_trend})"
        if d_trend:
            return f"D1: {d_trend} (4H unavailable)"
        return "data unavailable"
    except (KeyError, TypeError, ValueError):
        return "data unavailable"


def _get_seasonal_tendency(result: dict) -> str:
    """Check key_thesis for seasonal or historical tendency mentions."""
    kt = (result["parsed"].get("key_thesis") or "")
    keywords = [
        "seasonal", "historically", "tends to", "typical", "calendar",
        "month-end", "quarter-end", "year-end", "rebalancing",
        "summer", "winter", "spring", "autumn",
    ]
    kt_lower = kt.lower()
    for kw in keywords:
        if kw in kt_lower:
            for sentence in kt.split("."):
                if kw in sentence.lower() and len(sentence.strip()) > 10:
                    s = sentence.strip()
                    return (s[:100] + "…") if len(s) > 100 else s
    return "Not specifically assessed"


def _weekly_performance_section(date: str) -> list:
    """Return weekly summary lines on Monday, else empty list."""
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return []
    if dt.weekday() != 0:  # 0 = Monday
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

    lines = [
        "",
        "━━━━━━━━━━━━━━━━━━━━━",
        "📅 <b>WEEKLY PERFORMANCE SUMMARY</b>",
    ]
    if not recent:
        lines.append("No closed trades in the past 7 days — system still building history.")
        return lines

    wins  = [r for r in recent if r.get("status") == "WIN"]
    losses = [r for r in recent if r.get("status") == "LOSS"]
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
        f"7-day: <b>{len(wins)}W / {len(losses)}L</b>  "
        f"Win rate: <b>{wr:.0f}%</b>  Net: <b>{total_r:+.2f}R</b>"
    )
    if best:
        lines.append(
            f"🏆 Best:  #{best.get('id')} {best.get('pair')} "
            f"{best.get('direction','')} — {_r(best):+.2f}R"
        )
    if worst and worst is not best:
        lines.append(
            f"💔 Worst: #{worst.get('id')} {worst.get('pair')} "
            f"{worst.get('direction','')} — {_r(worst):+.2f}R"
        )

    try:
        from src import tracker as _tk  # noqa: F811
        prev_cutoff = (dt - timedelta(days=14)).strftime("%Y-%m-%d")
        prev = [
            r for r in rows
            if r.get("status") in ("WIN", "LOSS")
            and prev_cutoff <= (r.get("closed_at") or r.get("timestamp", ""))[:10] < cutoff
        ]
        if prev:
            prev_wr = sum(1 for r in prev if r.get("status") == "WIN") / len(prev) * 100
            delta   = wr - prev_wr
            icon    = "📈" if delta > 5 else ("📉" if delta < -5 else "➡️")
            lines.append(f"Win rate trend: {prev_wr:.0f}% → {wr:.0f}% {icon}")
    except Exception:
        pass

    lines.append("👁 This week: watch for central bank speeches and high-impact data releases.")
    return lines


def _send_in_parts(sections: list) -> None:
    """Combine sections into ≤4000-char Telegram messages; send each separately."""
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
) -> None:
    """Build and send the fully detailed daily Telegram notification."""
    closed_today    = closed_today    or []
    new_patterns    = new_patterns    or []
    stage1_filtered = stage1_filtered or []
    failed_pairs    = failed_pairs    or []

    yes_trades  = [r for r in deep_results if r["parsed"].get("trade_this") == "YES"]
    watch_list  = sorted(
        [r for r in deep_results
         if r["parsed"].get("trade_this") != "YES" and 5 <= _conf(r) <= 6],
        key=_conf, reverse=True,
    )[:4]
    near_misses = sorted(
        [r for r in deep_results if r["parsed"].get("trade_this") != "YES"],
        key=_conf, reverse=True,
    )

    _sizes, _exposure, _risk_state = {}, {}, {}
    if risk_data:
        for s in (risk_data.get("sized_trades") or []):
            _sizes[s["pair"]] = s
        _exposure   = risk_data.get("exposure", {})
        _risk_state = risk_data.get("risk_state", {})

    ctx = _derive_market_context(deep_results, risk_data)
    all_sections: list[list[str]] = []

    # Auckland time references used throughout the message
    now_ak      = _auckland_now()
    tomorrow_ak = now_ak + timedelta(days=1)
    today_short = _fmt_date_short_nz(now_ak)      # "Thursday 4 June"

    # ── HEADER ─────────────────────────────────────────────────────────────────
    header_line2 = (
        f"Universe: {universe_size} pairs · Screened: {total_scanned} · "
        f"Deep analysed: {len(deep_results)}"
    )
    if failed_pairs:
        header_line2 += f" · <b>⚠️ {len(failed_pairs)} analysis error(s)</b>"
    all_sections.append([
        f"<b>🤖 Forex AI — {_fmt_date_nz(now_ak)} — Auckland time</b>",
        f"<i>{header_line2}</i>",
    ])

    # ── MARKET CONTEXT ─────────────────────────────────────────────────────────
    ctx_sec = [
        "",
        "━━━━━━━━━━━━━━━━━━━━━",
        "🌍 <b>MARKET CONTEXT</b>",
    ]
    vix_str = f"VIX {ctx['vix']:.1f}" if ctx["vix"] else "VIX unavailable"
    ctx_sec.append(f"Environment: <b>{ctx['risk_env']}</b>  ({vix_str})")
    if ctx["yield_curve"] is not None:
        curve_note = "inverted — caution" if ctx["yield_curve"] < 0 else "normal"
        ctx_sec.append(f"Yield curve 2s10s: {ctx['yield_curve']:+.2f}%  ({curve_note})")
    if ctx["oil"]:
        ctx_sec.append(f"WTI Oil: ${ctx['oil']:.1f}/bbl")
    if ctx["strongest_ccy"] and ctx["weakest_ccy"]:
        sc = ctx["strongest_ccy"]
        wc = ctx["weakest_ccy"]
        ctx_sec += [
            f"💪 Strongest: <b>{sc}</b>  (composite signal +{ctx['ccy_scores'].get(sc, 0):.0f})",
            f"📉 Weakest:   <b>{wc}</b>  (composite signal {ctx['ccy_scores'].get(wc, 0):.0f})",
        ]
    elif deep_results:
        ctx_sec.append("Currency strength: signals insufficient to rank today.")

    # Collect news/event warnings from all analyses
    nw_list = []
    for r in deep_results:
        nw = (r["parsed"].get("news_warning") or "").strip()
        if nw and len(nw) > 5:
            nw_list.append(f"{r['pair']}: {nw[:70]}")
    if nw_list:
        ctx_sec.append("⚡ <b>Event warnings (next 48h):</b>")
        for nw in nw_list[:3]:
            ctx_sec.append(f"  · {nw}")

    n_a = len(yes_trades)
    n_w = len(watch_list)
    if n_a:
        summary = f"{n_a} high-conviction setup{'s' if n_a > 1 else ''} identified. Execute with strict risk controls."
    elif n_w:
        summary = f"No triggers yet — {n_w} pair{'s' if n_w > 1 else ''} watching for confirmation."
    else:
        summary = "No qualifying setups today — staying in cash is a valid position."
    ctx_sec.append(f"\n<i>📌 {summary}</i>")
    all_sections.append(ctx_sec)

    # ── TRADE ALERTS ───────────────────────────────────────────────────────────
    alert_sec = [
        "",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🚨 <b>TRADE ALERTS</b>  ({n_a} setup{'s' if n_a != 1 else ''})",
    ]
    if yes_trades:
        for r in yes_trades:
            p         = r["parsed"]
            pair      = r["pair"]
            direction = (p.get("direction") or "?").upper()
            conf      = p.get("confidence") or "?"
            entry_raw = p.get("entry")
            stop_raw  = p.get("stop_loss")
            tgt_raw   = p.get("target")
            rr_raw    = p.get("reward_risk")

            sz       = _sizes.get(pair, {})
            adj_stop = sz.get("adj_stop") or stop_raw
            adj_tgt  = sz.get("adj_target") or tgt_raw

            # R:R as a number so we can compute the profit target in dollar terms
            rr_num = None
            try:
                rr_num = abs(float(adj_tgt) - float(entry_raw)) / abs(float(entry_raw) - float(adj_stop))
                rr_str = f"{rr_num:.2f}:1"
            except (TypeError, ValueError, ZeroDivisionError):
                try:
                    rr_num = float(rr_raw)
                    rr_str = f"{rr_num:.2f}:1"
                except (TypeError, ValueError):
                    rr_str = "—"

            # Dollar risk and potential profit for the action header
            risk_amt = float(sz.get("risk_amount") or 0)
            cur      = sz.get("currency", "USD")
            pct      = sz.get("risk_pct", 1.0)
            try:
                profit_amt = risk_amt * rr_num if rr_num else None
                if profit_amt:
                    risk_profit = (
                        f"Risk {risk_amt:,.0f} {cur} to make {profit_amt:,.0f} {cur}"
                    )
                else:
                    risk_profit = f"Risk {risk_amt:,.0f} {cur}  ({pct:.2f}% account)"
            except (TypeError, ValueError):
                risk_profit = f"Risk {risk_amt:,.0f} {cur}  ({pct:.2f}% account)"

            bet     = (p.get("best_entry_time") or "").strip()
            bet_out = (bet[:80] if bet else _session_label(pair))

            kt = (p.get("key_thesis") or "").strip()
            kt_out = (kt[:280] + "…") if len(kt) > 280 else kt

            rf = (p.get("risk_factors") or "").strip()
            rf_parts = [x.strip() for x in rf.replace(";", "\n").split("\n") if x.strip()][:2]
            rf_out   = "; ".join(rf_parts) if rf_parts else (rf[:100] if rf else "None identified")

            action_icon = "🟢" if direction == "BUY" else "🔴"

            block = [
                "",
                # ── Action header — the first thing seen when the phone lights up ──
                f"{action_icon} <b>ACTION: {direction} {pair} NOW AT {_fmt_price(entry_raw)}</b>",
                f"<b>Set stop loss at {_fmt_price(adj_stop)}</b>",
                f"<b>Set take profit at {_fmt_price(adj_tgt)}</b>",
                f"<b>{risk_profit}</b>",
                f"<b>Best entry window: {bet_out}</b>",
                "",
                # ── Supporting detail ──
                f"Confidence: <b>{conf}/10</b>  ·  R:R: <b>{rr_str}</b>",
                f"<code>Scores │ {_score_breakdown_line(p)}</code>",
            ]
            if sz.get("atr_note"):
                block.append(f"📐 ATR adj: {sz['atr_note']}")
            if sz.get("lots"):
                block.append(
                    f"📦 Size: <code>{sz['lots']} lots</code>  ({pct:.2f}% account risk)"
                )
            if sz.get("correlated"):
                block.append("⚠️ <i>Correlated pair — position halved to manage exposure</i>")
            if _exposure.get("limit_reached"):
                block.append("🔴 <i>Risk limit reached — no new trades recommended</i>")
            block += [
                f"📊 Volatility: {_get_volatility_info(r)}",
                f"🔀 MTF align: {_get_mtf_alignment(r, direction)}",
                f"📅 Seasonal: {_get_seasonal_tendency(r)}",
            ]
            if kt_out:
                block.append(f"📝 Thesis: {kt_out}")
            block.append(f"⚠️ Risk factors: {rf_out}")
            alert_sec += [ln for ln in block if ln != ""]
    else:
        alert_sec.append("No pairs met the 7+ confidence threshold today.")
    all_sections.append(alert_sec)

    # ── NO SETUP EXPLANATION (only when no trade alerts) ───────────────────────
    if not yes_trades:
        no_sec = [
            "",
            "━━━━━━━━━━━━━━━━━━━━━",
            "💤 <b>WHY NO SETUPS TODAY</b>",
        ]

        # Build a unified ranked list: deep-analysed pairs first (sorted by conf
        # desc), then stage-1 filtered pairs (sorted by screener score desc).
        # Both lists carry enough info to explain the rejection in plain English.
        def _s1_sort_key(r):
            return float(r.get("screen", {}).get("score") or 0)

        combined = list(near_misses) + sorted(stage1_filtered, key=_s1_sort_key, reverse=True)
        top5     = combined[:5]

        if top5:
            no_sec.append(
                f"<b>Top {len(top5)} pair{'s' if len(top5) > 1 else ''} that came "
                f"closest — and exactly why each was rejected:</b>"
            )
            for i, r in enumerate(top5, 1):
                pair   = r["pair"]
                reason = _rejection_reason(r)
                no_sec.append(f"\n<b>{i}. {pair}</b>")
                no_sec.append(f"   {reason}")
                # For deep-analysed pairs, show a snippet of the thesis
                if not r.get("screened_out"):
                    kt    = (r["parsed"].get("key_thesis") or "").strip()
                    kt_50 = (kt[:90] + "…") if len(kt) > 90 else kt
                    if kt_50:
                        no_sec.append(f"   <i>{kt_50}</i>")
        elif failed_pairs:
            no_sec.append("⚠️ <b>Analysis failed for all pairs — likely a configuration error:</b>")
            no_sec.append(
                f"  All {len(failed_pairs)} pairs threw exceptions before returning results. "
                f"This almost always means <b>CLAUDE_MODEL</b> or <b>ANTHROPIC_API_KEY</b> "
                f"is missing or empty in the GitHub Actions environment."
            )
            no_sec.append(f"\n<b>Pairs attempted:</b> {', '.join(failed_pairs[:10])}")
            no_sec.append(
                "\n<b>To fix:</b> Open GitHub → Settings → Secrets and variables → Actions. "
                "Check that ANTHROPIC_API_KEY is set. CLAUDE_MODEL and HAIKU_MODEL are "
                "optional — if not set, defaults (claude-sonnet-4-6 / claude-haiku-4-5-20251001) "
                "are now used automatically."
            )
        else:
            no_sec.append(
                "No pairs were analysed and none failed with exceptions. "
                "Check that the selector returned pairs and the log file for details."
            )

        # Only show "waiting for" section when we have real analysis data to draw from
        if near_misses or stage1_filtered:
            no_sec.append("\n<b>What the system is waiting for:</b>")
            waiting = []
            score6 = [r for r in near_misses if _conf(r) == 6]
            if score6:
                p6 = ", ".join(r["pair"] for r in score6[:2])
                waiting.append(f"• {p6} at 6/10 — one layer confirmation away from a full alert")
            score5 = [r for r in near_misses if _conf(r) == 5]
            if score5 and not score6:
                p5 = ", ".join(r["pair"] for r in score5[:2])
                waiting.append(f"• {p5} at 5/10 — needs 2+ layer improvements")
            weak_tech = [r for r in near_misses[:5] if (r["parsed"].get("technical_score") or 10) < 5]
            if weak_tech:
                waiting.append("• Clearer trend structure across most pairs (technicals weak)")
            if stage1_filtered and not near_misses:
                waiting.append("• Pairs need stronger technical + fundamental alignment to pass Stage 1 screening")
            if not waiting:
                waiting.append("• Full confluence across Technical + Fundamental + Sentiment + Positioning + Macro")
            for w in waiting:
                no_sec.append(w)
        all_sections.append(no_sec)

    # ── WATCH LIST ─────────────────────────────────────────────────────────────
    watch_sec = [
        "",
        "━━━━━━━━━━━━━━━━━━━━━",
        "👀 <b>WATCH LIST</b>  (confidence 5–6, approaching signal)",
    ]
    if watch_list:
        for r in watch_list:
            p     = r["parsed"]
            pair  = r["pair"]
            conf  = p.get("confidence") or "?"
            dirn  = (p.get("direction") or "—").upper()
            arrow = "📈" if dirn == "BUY" else "📉"
            watch_sec += [
                "",
                f"{arrow} <b>{pair}</b> {dirn}  conf <b>{conf}/10</b>",
                f"<code>Scores │ {_score_breakdown_line(p)}</code>",
                f"🔑 To trigger alert: {_what_needs_to_change(p)}",
            ]
    else:
        watch_sec.append("No pairs in the 5–6 confidence range today.")
    all_sections.append(watch_sec)

    # ── LEARNING UPDATE ─────────────────────────────────────────────────────────
    learn_sec = [
        "",
        "━━━━━━━━━━━━━━━━━━━━━",
        "🧠 <b>LEARNING UPDATE</b>",
    ]
    if closed_today:
        learn_sec.append("<b>Trades auto-closed today:</b>")
        for t in closed_today:
            s     = (t.get("status") or "").upper()
            arrow = "✅" if s == "WIN" else "❌" if s == "LOSS" else "⏰"
            rm    = t.get("r_multiple")
            try:
                r_txt = f" ({float(rm):+.2f}R)" if rm not in (None, "") else ""
            except (TypeError, ValueError):
                r_txt = ""
            try:
                pips    = t.get("pips")
                pip_txt = f" | {float(pips):.1f}p" if pips not in (None, "") else ""
            except (TypeError, ValueError):
                pip_txt = ""
            learn_sec.append(
                f"  {arrow} #{t.get('id')} {t.get('pair')} "
                f"{t.get('direction', '')} — {s}{r_txt}{pip_txt}"
            )
    else:
        learn_sec.append("No trades auto-closed today.")

    if new_patterns:
        learn_sec.append(
            f"\n💡 <b>{len(new_patterns)} new pattern{'s' if len(new_patterns) > 1 else ''} learned:</b>"
        )
        for pat in new_patterns[:3]:
            learn_sec.append(f"  · {(pat[:110] + '…') if len(pat) > 110 else pat}")

    if stats:
        wr     = stats.get("win_rate")
        wr_txt = f"{wr*100:.0f}%" if wr is not None else "n/a"
        dec    = stats.get("decisive", 0)
        wins   = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        cl     = _risk_state.get("consecutive_losses", 0)
        cw     = _risk_state.get("consecutive_wins", 0)

        if cw >= 2:
            streak = f"🔥 {cw} consecutive wins"
        elif cl >= 2:
            streak = f"⚠️ {cl} consecutive losses"
        elif cw == 1:
            streak = "1 win (potential streak forming)"
        elif cl == 1:
            streak = "1 loss (watching for streak)"
        else:
            streak = "No streak active"

        _mode_desc = {
            "capital_protection": "⬇️ 0.25%/trade — drawdown >10% from peak",
            "streak_protection":  "⬇️ 0.25%/trade — 3+ consecutive losses",
            "reduced":            "⬇️ 0.50%/trade — last-5 win rate <40%",
            "normal":             "➡️ 1.00%/trade — standard conditions",
            "enhanced":           "⬆️ 1.50%/trade — last-5 win rate >70%",
        }
        risk_mode = _risk_state.get("risk_mode", "normal")
        learn_sec += [
            f"\n📊 Win rate: <b>{wr_txt}</b>  ({wins}W / {losses}L · {dec} decisive trades)",
            f"🔁 Streak: {streak}",
            f"⚙️ Risk setting: {_mode_desc.get(risk_mode, risk_mode)}",
        ]
    all_sections.append(learn_sec)

    # ── RISK DASHBOARD ──────────────────────────────────────────────────────────
    if risk_data and risk_data.get("profile"):
        risk_sec = ["", "━━━━━━━━━━━━━━━━━━━━━"]
        from src import risk_manager as _rm
        risk_sec += _rm.risk_dashboard_lines(
            risk_data["profile"],
            risk_data.get("risk_state", {}),
            risk_data.get("exposure", {}),
        )
        all_sections.append(risk_sec)

    # ── WEEKLY PERFORMANCE (Monday only) ───────────────────────────────────────
    weekly = _weekly_performance_section(date)
    if weekly:
        all_sections.append(weekly)

    # ── FOOTER ─────────────────────────────────────────────────────────────────
    all_sections.append([
        "",
        "━━━━━━━━━━━━━━━━━━━━━",
        "<i>⏰ Next scan 6am NZT tomorrow</i>",
    ])

    _send_in_parts(all_sections)


# ── Daily run ──────────────────────────────────────────────────────────────────

def run() -> int:
    missing = config.missing_keys()
    if missing:
        print("ERROR: missing API keys in .env: " + ", ".join(missing), file=sys.stderr)
        return 2

    now_ak   = _auckland_now()                    # all date logic uses Auckland time
    date     = now_ak.strftime("%Y-%m-%d")
    log_path = config.REPORTS_DIR / f"daily_{date}.log"
    with log_path.open("a", encoding="utf-8") as logf:

        # 0. Automatic outcome detection + win/loss analysis.
        closed_today = []
        new_patterns = []
        try:
            from src import outcome_checker, outcome_analyst
            closed_today = outcome_checker.check_open_trades(
                log=lambda m: _log_line(logf, m)
            )
            if closed_today:
                new_patterns = outcome_analyst.run_outcome_analysis(
                    closed_today, log=lambda m: _log_line(logf, m)
                )
        except Exception as exc:
            _log_line(logf, f"Outcome step failed: {exc}")

        # 1. Learn from prior outcomes.
        learning_stats = None
        try:
            learning_stats = learning.update_memory()
            _log_line(
                logf,
                f"Learning refreshed: {learning_stats['closed']} closed trades, "
                f"win rate {('%.0f%%' % (learning_stats['win_rate'] * 100)) if learning_stats['win_rate'] is not None else 'n/a'}, "
                f"{learning_stats['patterns_written']} auto-patterns written.",
            )
        except Exception as exc:
            _log_line(logf, f"Learning step failed: {exc}")

        # 2. Smart pair selection from the full universe.
        universe_size = len(selector.UNIVERSE)
        ranked_all    = []
        try:
            selection     = selector.select_pairs(top_n=10, log=lambda m: _log_line(logf, m))
            pairs_today   = selection["selected"]
            ranked_all    = selection["ranked"]
            universe_size = selection["universe_size"]
            _log_line(
                logf,
                f"Selected {len(pairs_today)} pairs from universe of {universe_size} "
                f"(pre-screened {selection['prescreened']} with price data): "
                f"{', '.join(pairs_today)}",
            )
        except Exception as exc:
            _log_line(logf, f"Smart selection failed ({exc}) — falling back to watchlist.")
            pairs_today = list(config.WATCHLIST)

        # Pre-fetch technical data for all selected pairs in one controlled batch
        # BEFORE analysis starts.  This keeps the 8-calls/min Twelve Data rate
        # limit from being hit during analysis and warms the 24-hour cache so
        # every technical.analyse() call is a pure cache hit.
        try:
            from src import technical as _tech
            _tech.warm_cache(pairs_today, log=lambda m: _log_line(logf, m))
        except Exception as exc:
            _log_line(logf, f"Technical pre-fetch failed (analysis will still run): {exc}")

        _log_line(logf, f"=== Daily run {date} | universe: {universe_size} pairs ===")

        # ── Startup diagnostics — log exactly what config values are in use ──
        _log_line(logf, f"[DIAG] CLAUDE_MODEL={repr(config.CLAUDE_MODEL)}")
        _log_line(logf, f"[DIAG] HAIKU_MODEL={repr(config.HAIKU_MODEL)}")
        _log_line(logf, (
            f"[DIAG] ANTHROPIC_API_KEY="
            f"{'set (len=' + str(len(config.ANTHROPIC_API_KEY)) + ')' if config.ANTHROPIC_API_KEY else 'MISSING OR EMPTY'}"
        ))
        _log_line(logf, (
            f"[DIAG] TWELVE_DATA_KEY="
            f"{'set (len=' + str(len(config.TWELVE_DATA_KEY)) + ')' if config.TWELVE_DATA_KEY else 'not set — will use UNIVERSE fallback'}"
        ))
        _log_line(logf, f"[DIAG] pairs_today ({len(pairs_today)}): {pairs_today}")

        # 3. Analyse initial batch (top 10).
        # All pairs go through Haiku screening first.  Only those scoring 4+
        # proceed to Sonnet deep analysis.  This is enforced naturally by
        # force_deep=False — no bypass of the stage-1 threshold.
        filtered_count  = 0
        stage1_filtered: list = []   # pairs rejected by Haiku screening
        failed_pairs:    list = []   # pairs where _analyse_pair returned None (exception)
        deep_results    = []
        analysed_pairs: set = set()

        def _process_batch(pairs):
            nonlocal filtered_count
            for pair in pairs:
                if pair in analysed_pairs:
                    continue
                analysed_pairs.add(pair)
                result = _analyse_pair(pair, logf, force_deep=False)
                if result is None:
                    failed_pairs.append(pair)
                    continue
                if result.get("screened_out"):
                    filtered_count += 1
                    s = result["screen"]
                    stage1_filtered.append(result)   # store for Telegram WHY section
                    _log_line(
                        logf,
                        f"  {result['pair']}: FILTERED stage-1 "
                        f"(score {s['score']}/5 — {s['reason']})",
                    )
                    continue
                p = result["parsed"]
                verdict = f"{p['trade_this']} | conf {p['confidence']} | {p['direction']}"
                _log_line(logf, f"#{result['id']} {result['pair']}: {verdict}")
                deep_results.append(result)

        _process_batch(pairs_today)

        # 4. Auto-expand: if fewer than 3 meaningful results after the first 10,
        # pull the next 5 from the ranked list and screen them through Haiku too.
        # Cap at 15 deep-analysed pairs total to contain costs.
        meaningful = [r for r in deep_results if _conf(r) >= 5]
        next_idx   = 10

        while len(meaningful) < 3 and len(deep_results) < 15 and next_idx < len(ranked_all):
            extra_pairs = [p for p, _ in ranked_all[next_idx:next_idx + 5]]
            next_idx   += 5
            _log_line(
                logf,
                f"Expanding: {len(deep_results)} deep results so far, "
                f"{len(meaningful)} with conf>=5. Adding {len(extra_pairs)} more pairs.",
            )
            _process_batch(extra_pairs)
            meaningful = [r for r in deep_results if _conf(r) >= 5]

        passed = len(deep_results)
        _log_line(
            logf,
            f"Analysis complete: universe={universe_size} · "
            f"stage-1 filtered={filtered_count} · "
            f"deep-analysed={passed} · "
            f"meaningful(conf>=5)={len(meaningful)} · "
            f"failed(exception)={len(failed_pairs)}",
        )
        if failed_pairs:
            _log_line(logf, f"[DIAG] Failed pairs (API/exception): {failed_pairs}")

        actionable = [
            f"{r['pair']} {r['parsed']['direction']} (conf {r['parsed']['confidence']})"
            for r in deep_results
            if r["parsed"].get("trade_this") == "YES"
        ]

        # 5. Rebuild dashboard.
        try:
            path = dashboard.generate()
            _log_line(logf, f"Dashboard updated: {path}")
        except Exception as exc:
            _log_line(logf, f"Dashboard step failed: {exc}")

        if actionable:
            _log_line(logf, "ACTIONABLE TODAY: " + "; ".join(actionable))
        else:
            _log_line(logf, "No actionable setups today (all TRADE_THIS: NO).")
        _log_line(logf, "=== Daily run complete ===")

        # 6. Risk management — size YES trades and update risk profile.
        risk_data = {}
        try:
            from src import risk_manager
            risk_profile = risk_manager.load_profile()
            risk_state   = risk_manager.compute_risk_state(risk_profile)
            exposure     = risk_manager.compute_open_exposure(risk_profile)

            sized = []
            for r in deep_results:
                if r["parsed"].get("trade_this") == "YES":
                    sized.append(risk_manager.size_trade_from_result(
                        r, risk_profile, risk_state
                    ))
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
            _log_line(
                logf,
                f"Risk: mode={risk_state['risk_mode']} "
                f"risk={risk_state['base_risk_pct']:.2f}% "
                f"open_exposure={exposure['total_pct']:.1f}% "
                f"streak_loss={risk_state['consecutive_losses']}"
            )
        except Exception as exc:
            _log_line(logf, f"Risk management step failed: {exc}")

        # 7. Telegram summary.
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
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
