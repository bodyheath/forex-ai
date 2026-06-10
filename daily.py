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
import html as _html_mod
import json
import re as _re_tg
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
    "EUR": ("London",        "7pm–4am"),
    "GBP": ("London",        "7pm–4am"),
    "CHF": ("London",        "7pm–4am"),
    "JPY": ("Tokyo",         "12pm–9pm"),
    "USD": ("New York",      "1am–10am"),
    "CAD": ("New York",      "1am–10am"),
    "AUD": ("Sydney/Tokyo",  "9am–9pm"),
    "NZD": ("Sydney/Tokyo",  "9am–9pm"),
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
    return "London session 7pm – 11pm Auckland time"


def _best_session_for_pair(pair: str) -> str:
    """Return the best Auckland trading session window for a pair."""
    cleaned = pair.upper().replace("/", "").replace("-", "")
    base  = cleaned[:3]
    quote = cleaned[3:6] if len(cleaned) >= 6 else ""
    b = _SESSION_AUCKLAND.get(base)
    q = _SESSION_AUCKLAND.get(quote)
    if not b and not q:
        return "London open 7pm–9pm Auckland"
    if b and q and b[0] == q[0]:
        return f"{b[0]} {b[1]} Auckland"
    if b and q:
        sess_set = {b[0], q[0]}
        if {"Tokyo", "London"} <= sess_set or {"Sydney/Tokyo", "London"} <= sess_set:
            return "London open 7pm–9pm Auckland"
        if {"London", "New York"} <= sess_set:
            return "London/NY overlap 1am–4am Auckland"
        if {"Tokyo", "New York"} <= sess_set or {"Sydney/Tokyo", "New York"} <= sess_set:
            return "Sydney/Tokyo 9am–9pm Auckland"
    s = b or q
    return f"{s[0]} {s[1]} Auckland"


def _session_time_label(pair: str, now_ak: datetime) -> str:
    """Return best session label with today/tonight/tomorrow morning based on current Auckland time.

    Uses specific currency logic: cross-JPY pairs go to London open, USD/JPY to NY open,
    pure JPY to Tokyo, EUR/GBP/CHF to London, USD/CAD to New York, AUD/NZD to Sydney/Tokyo.
    'Today' = session starts within 8 hours; 'tonight' = 8–20 hours away; else 'tomorrow morning'.
    """
    cleaned = pair.upper().replace("/", "").replace("-", "")
    base  = cleaned[:3]
    quote = cleaned[3:6] if len(cleaned) >= 6 else ""
    ccys  = {base, quote}

    if "JPY" in ccys and ccys & {"EUR", "GBP", "CHF"}:
        sess_name, window, start_h = "London open sweet spot", "7pm–9pm", 19
    elif "JPY" in ccys and ccys & {"USD", "CAD"}:
        sess_name, window, start_h = "New York open", "1am–3am", 1
    elif "JPY" in ccys:
        sess_name, window, start_h = "Tokyo session peak", "12pm–3pm", 12
    elif ccys & {"EUR", "GBP", "CHF"}:
        sess_name, window, start_h = "London session peak", "7pm–10pm", 19
    elif ccys & {"USD", "CAD"}:
        sess_name, window, start_h = "New York session peak", "1am–4am", 1
    elif ccys & {"AUD", "NZD"}:
        sess_name, window, start_h = "Sydney/Tokyo peak", "9am–1pm", 9
    else:
        sess_name, window, start_h = "London open", "7pm–9pm", 19

    hours_away = (start_h - now_ak.hour) % 24
    if hours_away == 0:
        time_ref = "now"
    elif hours_away <= 8:
        time_ref = "today"
    elif hours_away <= 20:
        time_ref = "tonight"
    else:
        time_ref = "tomorrow morning"

    return f"{sess_name} {window} Auckland {time_ref}"


def _fmt_time_exact(h: int, m: int = 0) -> str:
    """Format 24-hour time as '5:00pm', '10:30pm'."""
    ampm = "am" if h < 12 else "pm"
    h12  = h % 12 or 12
    return f"{h12}:{m:02d}{ampm}"


def _entry_window_for_pair(pair: str) -> tuple:
    """Return (start_h, start_m, end_h, end_m, window_str, cutoff_str, sess_name).

    Precise 90-minute high-liquidity windows for each currency group.
    Cutoff is the absolute no-entry deadline.
    """
    cleaned = pair.upper().replace("/", "").replace("-", "")
    base  = cleaned[:3]
    quote = cleaned[3:6] if len(cleaned) >= 6 else ""
    ccys  = {base, quote}

    if ccys == {"USD", "JPY"}:
        return (1, 0, 2, 0, "1:00am–2:00am", "2:30am", "New York open")
    if "JPY" in ccys and ccys & {"EUR", "GBP", "CHF"}:
        return (19, 0, 20, 0, "7:00pm–8:00pm", "9:00pm", "London open")
    if "JPY" in ccys:
        return (12, 0, 13, 30, "12:00pm–1:30pm", "3:00pm", "Tokyo open")
    if ccys & {"EUR", "GBP", "CHF"}:
        return (19, 0, 20, 30, "7:00pm–8:30pm", "11:00pm", "London open")
    if ccys & {"USD", "CAD"}:
        return (1, 0, 2, 30, "1:00am–2:30am", "3:00am", "New York open")
    if ccys & {"AUD", "NZD"}:
        return (9, 0, 10, 30, "9:00am–10:30am", "12:00pm", "Sydney open")
    return (19, 0, 20, 30, "7:00pm–8:30pm", "11:00pm", "London open")


def _entry_quality(pair: str, now_ak: datetime) -> tuple:
    """Return (emoji, label) based on how close we are to the optimal entry window now.

    🟢 ENTER NOW   — currently inside optimal window
    🟡 ENTER SOON  — window opens in < 2 hours
    🟠 WAIT        — window opens in 2-6 hours
    🔴 WAIT UNTIL TOMORROW — more than 6 hours away
    """
    start_h, start_m, end_h, end_m, _, _, sess_name = _entry_window_for_pair(pair)
    cur_mins   = now_ak.hour * 60 + now_ak.minute
    win_s_mins = start_h * 60 + start_m
    win_e_mins = end_h   * 60 + end_m

    if win_s_mins <= win_e_mins:
        in_window = win_s_mins <= cur_mins <= win_e_mins
    else:
        in_window = cur_mins >= win_s_mins or cur_mins <= win_e_mins

    if in_window:
        return "🟢", "ENTER NOW — currently in optimal session window"

    mins_away = (win_s_mins - cur_mins) % (24 * 60)
    if mins_away < 120:
        return "🟡", f"ENTER SOON — {sess_name} opens in {mins_away} min"
    if mins_away <= 360:
        hrs = mins_away // 60
        rem = mins_away % 60
        t   = f"{hrs}h {rem}m" if rem else f"{hrs}h"
        return "🟠", f"WAIT — {sess_name} opens in {t}"
    return "🔴", "WAIT UNTIL TOMORROW — optimal window more than 6 hours away"


def _time_ref_for_entry(start_h: int, start_m: int, now_ak: datetime) -> str:
    """Return 'TODAY', 'tonight', or 'tomorrow' for a session start time."""
    cur_mins  = now_ak.hour * 60 + now_ak.minute
    win_mins  = start_h * 60 + start_m
    hours_away = ((win_mins - cur_mins) % (24 * 60)) / 60
    if hours_away <= 8:
        return "TODAY"
    if hours_away <= 20:
        return "tonight"
    return "tomorrow"


def _log_line(handle, msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    line  = f"[{stamp}] {msg}"
    print(line)
    handle.write(line + "\n")
    handle.flush()


def _telegram_test() -> None:
    """Call getMe to verify the bot token is valid; log the result."""
    if not config.TELEGRAM_TOKEN:
        print("[TELEGRAM] Bot token INVALID — check TELEGRAM_TOKEN secret")
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/getMe"
    try:
        resp = urllib.request.urlopen(url, timeout=10)
        data = json.loads(resp.read().decode())
        if data.get("ok"):
            bot_name = data["result"].get("username", "Unknown")
            print(f"[TELEGRAM] Bot token valid — bot name: {bot_name}")
        else:
            print("[TELEGRAM] Bot token INVALID — check TELEGRAM_TOKEN secret")
    except Exception as exc:
        print(f"[TELEGRAM] Bot token INVALID — check TELEGRAM_TOKEN secret ({exc})")


def _telegram(message: str) -> None:
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    _named_recipients = [("Heath", config.TELEGRAM_CHAT_ID)]
    if config.TELEGRAM_CHAT_ID_2:
        _named_recipients.append(("George", config.TELEGRAM_CHAT_ID_2))
    if config.TELEGRAM_CHAT_ID_3:
        _named_recipients.append(("Max", config.TELEGRAM_CHAT_ID_3))
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    for name, chat_id in _named_recipients:
        try:
            data = urllib.parse.urlencode({
                "chat_id":    chat_id,
                "text":       message,
                "parse_mode": "HTML",
            }).encode()
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
            print(f"[TELEGRAM] SUCCESS — message sent to {name} (chat_id: {chat_id})")
        except Exception as exc:
            print(f"[TELEGRAM] FAILED — {name}: {exc}")


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
                  shared_fundamental=None, shared_macro=None,
                  sonnet_threshold: int = 6) -> dict | None:
    try:
        return service.analyse_and_log(
            pair,
            log=lambda m: _log_line(logf, m),
            force_deep=force_deep,
            shared_fundamental=shared_fundamental,
            shared_macro=shared_macro,
            sonnet_threshold=sonnet_threshold,
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


def _mtf_plain_english(mtf_data: dict) -> str:
    """Convert MTF data to a plain English summary for phone-readable display."""
    if not isinstance(mtf_data, dict):
        return ""
    direction = mtf_data.get("direction", "NEUTRAL")
    count     = mtf_data.get("agreeing_count", 0)
    breakdown = mtf_data.get("breakdown", "")
    if not breakdown or breakdown == "UNAVAILABLE" or direction == "NEUTRAL" or count == 0:
        return ""

    _TF_NAMES = {"M": "monthly", "W": "weekly", "D": "daily", "4H": "4H", "1H": "1H"}

    def _natural(items):
        if not items:
            return ""
        if len(items) == 1:
            return items[0]
        return ", ".join(items[:-1]) + f" and {items[-1]}"

    agreeing    = []
    disagreeing = []
    for part in breakdown.split():
        if ":" in part:
            tf, sig = part.split(":", 1)
            name = _TF_NAMES.get(tf, tf)
            if sig == direction:
                agreeing.append(name)
            elif sig not in ("NEUTRAL", ""):
                disagreeing.append(name)

    agree_dir    = "bullish" if direction == "BUY" else "bearish"
    disagree_dir = "bearish" if direction == "BUY" else "bullish"
    agree_str    = _natural(agreeing) if agreeing else "—"

    if disagreeing:
        return (
            f"Timeframes: {count} of 5 agree {direction} — "
            f"{agree_str} {agree_dir}, {_natural(disagreeing)} {disagree_dir}"
        )
    return f"Timeframes: {count} of 5 agree {direction} — {agree_str} all {agree_dir}"


def _weakest_layer_hint(parsed: dict) -> str:
    """Return one line identifying the weakest analysis layer and what would fix it."""
    _HINTS = {
        "Technical":   "needs stronger RSI, MACD or trend alignment",
        "Fundamental": "needs clearer rate differential or central bank divergence",
        "Sentiment":   "needs positive news tone shift for this currency",
        "Positioning": "needs more extreme COT positioning data",
        "Macro":       "needs risk-on environment or positive economic data",
    }
    score_keys = [
        ("Technical",   "technical_score"),
        ("Fundamental", "fundamental_score"),
        ("Sentiment",   "sentiment_score"),
        ("Positioning", "positioning_score"),
        ("Macro",       "macro_score"),
    ]
    valid = [(name, parsed.get(key)) for name, key in score_keys if parsed.get(key) is not None]
    if not valid:
        return ""
    name, score = min(valid, key=lambda x: x[1])
    return f"Weakest: {name} {score}/10 — {_HINTS[name]}"


def _ribbon_display(bundle: dict) -> str:
    """Return a one-line MA ribbon status string for Telegram display, or empty string."""
    rib = (bundle or {}).get("technical", {})
    if isinstance(rib, dict):
        rib = rib.get("daily", {})
    if not isinstance(rib, dict):
        return ""
    ribbon = rib.get("ribbon", {})
    if not isinstance(ribbon, dict):
        return ""
    status = ribbon.get("status", "")
    if not status or status in ("UNAVAILABLE", "NEUTRAL", ""):
        return ""
    _LABELS = {
        "ALIGNED_BULL":  "Aligned bull",
        "ALIGNED_BEAR":  "Aligned bear",
        "CONVERGING":    "Converging",
        "LEANING_BULL":  "Leaning bull",
        "LEANING_BEAR":  "Leaning bear",
    }
    label = _LABELS.get(status, status.replace("_", " ").title())
    detail = ""
    if ribbon.get("fanning"):
        detail = " — fanning out (trend accelerating)"
    elif ribbon.get("converging"):
        detail = " — spread tightening (trend weakening)"
    elif status == "CONVERGING":
        detail = " — potential trend change"
    cnt = ribbon.get("aligned_count", 0)
    return f"📊 MA Ribbon: {label} ({cnt}/5 aligned){detail}"


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


# ── Indicative level calculator ───────────────────────────────────────────────

def _calc_indicative_levels(pair: str, parsed: dict, bundle: dict) -> tuple:
    """Return (entry, stop, target) as floats for indicative display.

    Uses Claude's parsed values when present (watch list / approaching pairs
    always have Claude-computed levels).  Falls back to current price ± ATR
    proxy if any value is missing.  Returns (None, None, None) if price
    is completely unavailable.
    """
    try:
        entry  = float(parsed.get("entry")     or 0) or None
        stop   = float(parsed.get("stop_loss") or 0) or None
        target = float(parsed.get("target")    or 0) or None
    except (TypeError, ValueError):
        entry, stop, target = None, None, None

    if entry and stop and target:
        return entry, stop, target

    # Current price from bundle
    cur = None
    try:
        daily = bundle.get("technical", {}).get("daily", {})
        if isinstance(daily, dict):
            cur = float(daily.get("last_close") or daily.get("close") or 0) or None
    except (TypeError, ValueError):
        pass

    if cur is None:
        return entry, stop, target

    is_jpy = "JPY" in pair.upper()
    atr_est = 0.50 if is_jpy else (0.0080 if any(c in pair.upper() for c in ("EUR", "GBP")) else 0.0050)
    dirn = (parsed.get("direction") or "").upper()
    if dirn == "BUY":
        entry  = entry  or cur
        stop   = stop   or round(entry - atr_est * 1.5, 3 if is_jpy else 5)
        target = target or round(entry + atr_est * 2.0, 3 if is_jpy else 5)
    elif dirn == "SELL":
        entry  = entry  or cur
        stop   = stop   or round(entry + atr_est * 1.5, 3 if is_jpy else 5)
        target = target or round(entry - atr_est * 2.0, 3 if is_jpy else 5)
    return entry, stop, target


# ── Open trade live-price helpers ─────────────────────────────────────────────

def _fetch_live_price(pair: str, px_cache: dict) -> tuple:
    """Return (price | None, stale: bool) for an open trade pair.

    Checks px_cache (scan data already in memory) first, then the 24-hour
    Twelve Data disk cache (from the 6am warm-cache run), then makes a
    fresh API call as a last resort.  Never falls through to the old
    'price not in today's scan' message.
    """
    if pair in px_cache:
        return px_cache[pair], False
    # Check every possible outputsize that might be cached from today's run
    try:
        from src import cache as _tc, technical as _tt
        for _sz in (400, 200, 100, 2):
            _cdata = _tc.get(f"TD:{pair}:1day:{_sz}", ttl_hours=24.0)
            if _cdata:
                _df = _tt._frame_from_td(_cdata)
                if len(_df) > 0:
                    p = float(_df["close"].iloc[-1])
                    if p > 0:
                        px_cache[pair] = p
                        return p, False
    except Exception:
        pass
    # Live API call as last resort (uses TD cache internally if available)
    if not config.TWELVE_DATA_KEY:
        return None, True
    try:
        from src import technical as _tt_live
        _data = _tt_live._td_request(pair, "1day", 2)
        _df   = _tt_live._frame_from_td(_data)
        if len(_df) > 0:
            p = float(_df["close"].iloc[-1])
            if p > 0:
                px_cache[pair] = p
                return p, False
    except Exception:
        pass
    return None, True


def _build_open_trades_section(open_trades: list, px_cache: dict, now_ak) -> list:
    """Build the OPEN TRADES section lines for any Telegram scan message.

    Always returns a non-empty list (shows 'No open trades' when empty).
    Placed at the top of every message immediately after the scan header.
    Each trade shows: entry/current/stop/target, unrealised P&L, progress bar,
    one dynamic status message from 8 possible states, and next key check time.
    """
    sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "📊 <b>OPEN TRADES</b>"]

    # Portfolio summary — quick pre-pass to compute aggregate P&L
    _port_dollar = 0.0
    _port_counted = 0
    for _pr in open_trades:
        try:
            _pc, _ = _fetch_live_price(_pr.get("pair", "?"), px_cache)
            _pe    = float(_pr.get("entry") or 0) or None
            _pd    = (_pr.get("direction") or "").upper()
            if _pc and _pe:
                _pz  = _pip_size(_pr.get("pair", "?"))
                _prw = (_pc - _pe) if _pd == "BUY" else (_pe - _pc)
                _port_dollar += (_prw / _pz) * 1.0
                _port_counted += 1
        except Exception:
            pass

    if not open_trades:
        sec.append("No open trades currently.")
        return sec

    # Portfolio summary line
    _n = len(open_trades)
    _tw = "trade" if _n == 1 else "trades"
    if _port_counted > 0:
        if _port_dollar >= 0:
            sec.append(f"<b>{_n} open {_tw} — up ${_port_dollar:.0f} overall</b>")
        else:
            sec.append(f"<b>{_n} open {_tw} — down ${abs(_port_dollar):.0f} overall</b>")
    else:
        sec.append(f"<b>{_n} open {_tw}</b>")

    for row in open_trades:
        pair = row.get("pair", "?")
        dirn = (row.get("direction") or "").upper()
        tid  = row.get("id", "?")

        sec.append("")
        sec.append(f"📊 <b>OPEN TRADE #{tid} — {pair} {dirn}</b>")

        cur, stale = _fetch_live_price(pair, px_cache)

        try:
            entry  = float(row.get("entry")     or 0) or None
            stop   = float(row.get("stop_loss") or 0) or None
            target = float(row.get("target")    or 0) or None
        except (TypeError, ValueError):
            entry, stop, target = None, None, None

        # Age / expiry (5-day default)
        days_open     = 0
        days_open_str = "?"
        expires_str   = "?"
        remaining     = 5
        try:
            opened_dt = datetime.strptime(row.get("timestamp", "")[:10], "%Y-%m-%d")
            days_open = (now_ak.replace(tzinfo=None) - opened_dt).days
            days_open_str = f"{days_open} day{'s' if days_open != 1 else ''}"
            remaining     = max(0, 5 - days_open)
            expires_str   = f"{remaining} day{'s' if remaining != 1 else ''}"
        except Exception:
            pass

        # Next key monitoring time
        _ew_t  = _entry_window_for_pair(pair)
        _eq_e, _ = _entry_quality(pair, now_ak)
        _tref_t = _time_ref_for_entry(_ew_t[0], _ew_t[1], now_ak)
        _check_line = (
            f"⏰ <b>Next key time:</b> {_ew_t[6]} "
            f"{_fmt_time_exact(_ew_t[0], _ew_t[1])} Auckland {_tref_t}"
        )

        if entry and cur:
            stale_note = " ⚠️ last known price" if stale else ""
            sec.append(f"Entry: {_fmt_price(entry)} | Current: {_fmt_price(cur)}{stale_note}")

            if stop and target:
                sec.append(f"🛑 Stop: {_fmt_price(stop)} | 🎯 Target: {_fmt_price(target)}")
            elif stop:
                sec.append(f"🛑 Stop: {_fmt_price(stop)}")
            elif target:
                sec.append(f"🎯 Target: {_fmt_price(target)}")

            pip_sz = _pip_size(pair)
            raw    = (cur - entry) if dirn == "BUY" else (entry - cur)
            pips   = raw / pip_sz
            dollar = abs(pips) * 1.0   # $1/pip estimate at 0.1 lots
            if pips > 2:
                arrow    = "📈"
                pnl_str  = f"+{pips:.0f} pips (+${dollar:.0f}) — moving in your favour"
            elif pips < -2:
                arrow    = "📉"
                pnl_str  = f"{pips:.0f} pips (-${dollar:.0f}) — moving against you"
            else:
                arrow    = "➖"
                pnl_str  = f"{pips:+.0f} pips — at breakeven"
            sec.append(f"{arrow} {pnl_str}")

            if target and stop:
                try:
                    pct_tgt = pct_stp = 0.0
                    pips_to_target = pips_to_stop = 9999.0
                    if dirn == "BUY" and target > entry and entry > stop:
                        trade_range    = target - entry
                        risk_range     = entry  - stop
                        pct_tgt        = min(100.0, max(0.0, (cur - entry) / trade_range * 100))
                        pct_stp        = min(100.0, max(0.0, (entry - cur) / risk_range  * 100))
                        pips_to_target = (target - cur) / pip_sz
                        pips_to_stop   = (cur - stop)   / pip_sz
                    elif dirn == "SELL" and stop > entry and entry > target:
                        trade_range    = entry  - target
                        risk_range     = stop   - entry
                        pct_tgt        = min(100.0, max(0.0, (entry - cur) / trade_range * 100))
                        pct_stp        = min(100.0, max(0.0, (cur - entry) / risk_range  * 100))
                        pips_to_target = (cur - target) / pip_sz
                        pips_to_stop   = (stop - cur)   / pip_sz

                    bar_filled = int(pct_tgt / 100 * 20)
                    prog_bar   = "█" * bar_filled + "░" * (20 - bar_filled)
                    if pct_tgt >= 90:
                        _pctx = "almost at target"
                    elif pct_tgt >= 75:
                        _pctx = "excellent progress, lock in gains"
                    elif pct_tgt >= 50:
                        _pctx = "good momentum, consider partial profit"
                    elif pct_tgt >= 20:
                        _pctx = "building toward target"
                    elif pct_tgt >= 5:
                        _pctx = "price barely moving, watch for momentum" if abs(pips) < 5 else "early progress, stay patient"
                    else:
                        _pctx = "just opened, give it time" if days_open == 0 else "price barely moving, watch for momentum"
                    sec.append(f"Progress: {pct_tgt:.0f}% to target {prog_bar} — {_pctx}")

                    # Dynamic status — most urgent condition takes priority
                    if 0 < pips_to_target <= 20:
                        sec.append(f"🚨 <b>TARGET ALMOST HIT — {pips_to_target:.0f} pips remaining</b>")
                        sec.append("Consider closing entire position now")
                    elif 0 < pips_to_stop <= 20:
                        sec.append(f"🚨 <b>STOP LOSS APPROACHING — {pips_to_stop:.0f} pips remaining</b>")
                        sec.append("Be prepared — let your stop do its job")
                    elif pct_tgt >= 75:
                        try:
                            trail_px = (
                                entry + (target - entry) * 0.75 if dirn == "BUY"
                                else entry - (entry - target) * 0.75
                            )
                            sec.append("🎯 <b>75% to target — trail stop to lock in profit</b>")
                            sec.append(f"Move stop to {_fmt_price(trail_px)} to protect your gains")
                        except Exception:
                            sec.append("🎯 <b>75% to target — trail stop to lock in profit</b>")
                    elif pct_tgt >= 50:
                        sec.append("🎯 <b>50% to target reached — move stop to breakeven now</b>")
                        sec.append(f"Protect your position: move stop to {_fmt_price(entry)}")
                    elif pips > 10:
                        try:
                            halfway_px = (
                                entry + (target - entry) * 0.5 if dirn == "BUY"
                                else entry - (entry - target) * 0.5
                            )
                            sec.append("📈 Good progress — price moving toward target")
                            sec.append(f"Consider partial profit at {_fmt_price(halfway_px)} (50% level)")
                        except Exception:
                            sec.append("📈 Good progress — price moving toward target")
                    elif abs(pips) <= 10:
                        sec.append("⚠️ <b>Trade at breakeven — monitor closely</b>")
                        sec.append("Price needs to move away from entry")
                    else:
                        sec.append(f"📉 Currently underwater — {abs(pips_to_stop):.0f} pips from stop loss")
                        sec.append("Stay disciplined — let the analysis play out")

                    # Additional age warning when trade has been open too long
                    if days_open > 3:
                        sec.append(f"⏳ Trade open {days_open_str} — expires in {expires_str}")
                        sec.append("If no clear momentum consider closing manually")

                    sec.append(_check_line)
                except Exception:
                    sec.append(_check_line)
            else:
                sec.append(_check_line)

        elif entry:
            if stop and target:
                sec.append(f"🛑 Stop: {_fmt_price(stop)} | 🎯 Target: {_fmt_price(target)}")
            sec.append(f"Entry: {_fmt_price(entry)} | Current: ⚠️ price unavailable")
            sec.append(_check_line)
        else:
            sec.append("Trade details unavailable")

        sec.append(f"Opened: {days_open_str} ago | Expires in: {expires_str}")

    return sec


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
    scan_mode: str = "full",
    new_alerts: set = None,
    research_result: dict = None,
    threshold_revert_msg: str = None,
    cost_lines: list = None,
) -> None:
    """Build and send Telegram notifications with format tailored to each scan mode."""
    closed_today    = closed_today    or []
    new_patterns    = new_patterns    or []
    stage1_filtered = stage1_filtered or []
    failed_pairs    = failed_pairs    or []
    run_stats       = run_stats       or {}
    credit_data     = credit_data     or {}
    cost_lines      = cost_lines      or []

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

    ctx         = _derive_market_context(deep_results, risk_data)
    now_ak      = _auckland_now()
    today_short = _fmt_date_short_nz(now_ak)
    n_deep      = len(deep_results)
    all_sections: list[list[str]] = []

    # Load 6am morning confidence scores for change detection in intraday scans
    _morning_conf: dict = {}
    try:
        _morning_conf = json.loads(_MORNING_RANKED_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass

    # Open trades and shared price cache — used in all scan modes
    _ot_open_trades: list = []
    try:
        from src import tracker as _trk_ot
        _ot_open_trades = [r for r in _trk_ot.load() if r.get("status") == "OPEN"]
    except Exception:
        pass

    # Pre-populate price cache from today's scan results (zero extra API calls)
    _ot_px_cache: dict = {}
    for _r in deep_results:
        try:
            _td = _r["bundle"]["technical"]["daily"]
            if isinstance(_td, dict):
                _p = float(_td.get("last_close") or _td.get("close") or 0)
                if _p > 0:
                    _ot_px_cache[_r["pair"]] = _p
        except (KeyError, TypeError, ValueError):
            pass

    # ── Badges ────────────────────────────────────────────────────────────────
    _badge_map = {
        "full":      "🌅 6AM FULL SCAN",
        "asian":     "🌏 9AM CHECK",
        "midday":    "🌇 5PM CHECK",
        "prelondon": "🌆 3PM CHECK",
    }
    _badge = _badge_map.get(scan_mode, "🤖 Forex AI")

    if yes_trades:
        setup_line = f"<b>🟢 {len(yes_trades)} setup{'s' if len(yes_trades) > 1 else ''} found</b>"
    else:
        setup_line = "No setups today"

    # ── Nested helpers ────────────────────────────────────────────────────────

    def _trade_block(r: dict) -> list:
        """Build the full trade alert block for one YES result."""
        p         = r["parsed"]
        pair      = r["pair"]
        direction = (p.get("direction") or "?").upper()
        conf      = p.get("confidence") or "?"
        entry_raw = p.get("entry")
        stop_raw  = p.get("stop_loss")
        tgt_raw   = p.get("target")
        sz        = _sizes.get(pair, {})
        adj_stop  = sz.get("adj_stop") or stop_raw
        adj_tgt   = sz.get("adj_target") or tgt_raw

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

        risk_amt   = float(sz.get("risk_amount") or 0)
        cur        = sz.get("currency", "USD")
        pct        = sz.get("risk_pct", 1.0)
        profit_amt = None
        try:
            profit_amt = round(risk_amt * rr_num) if rr_num else None
        except (TypeError, ValueError):
            pass

        pip_risk   = _fmt_pips_between(pair, entry_raw, adj_stop)
        pip_target = _fmt_pips_between(pair, entry_raw, adj_tgt)
        action_icon = "🟢" if direction == "BUY" else "🔴"
        _fp  = f"{pair}:{direction}"
        _pfx = "🆕 NEW: " if (new_alerts and _fp in new_alerts) else ""

        rf  = (p.get("risk_factors") or "").strip()
        rf_parts = [x.strip() for x in rf.replace(";", "\n").split("\n") if x.strip()][:2]

        # Precise entry window from currency group
        _ew_tb  = _entry_window_for_pair(pair)
        _ew_sh, _ew_sm = _ew_tb[0], _ew_tb[1]
        _ew_win = _ew_tb[4]   # "5:00pm–6:30pm"
        _ew_cut = _ew_tb[5]   # "9:00pm"
        _ew_ses = _ew_tb[6]   # "London open"
        _eq_em, _eq_lb = _entry_quality(pair, now_ak)
        _tref_tb = _time_ref_for_entry(_ew_sh, _ew_sm, now_ak)
        _ideal_verb = "pull back to" if direction == "BUY" else "push up to"

        block = [
            "",
            f"{action_icon} <b>{_pfx}ACTION: {direction} {pair} NOW</b>",
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

        # ── Fibonacci levels ──────────────────────────────────────────────────
        _fib = (r.get("bundle", {}).get("technical", {})
                 .get("daily", {}).get("fibonacci", {}))
        if isinstance(_fib, dict) and _fib.get("status") == "ok":
            _fib_above = _fib.get("nearest_above", [])
            _fib_below = _fib.get("nearest_below", [])
            _fib_near  = _fib.get("near_levels", [])
            _rng_p     = _fib.get("range_pips", "?")
            block.append("━━━━━━━━━━━━━━━━━━━━━")
            block.append(f"📐 <b>Fibonacci Levels</b>  (swing range: {_rng_p}p)")
            if _fib_above:
                block.append("  🔴 Resistance: " + " | ".join(
                    f"{lb} {_fmt_price(px)}" for lb, px in _fib_above))
            if _fib_below:
                block.append("  🟢 Support: " + " | ".join(
                    f"{lb} {_fmt_price(px)}" for lb, px in _fib_below))
            if _fib_near:
                _fn = _fib_near[0]
                block.append(
                    f"  ⭐ Price {_fn['distance_pips']:.0f}p from {_fn['label']}"
                    f" {_fn['type']} — triple confluence zone"
                )

        block += [
            "━━━━━━━━━━━━━━━━━━━━━",
            f"⏰ <b>EXACT ENTRY INSTRUCTIONS:</b>",
            f"{_eq_em} {_eq_lb}",
            f"⏰ ENTER TRADE: Between {_ew_win} Auckland {_tref_tb}",
            f"⛔ DO NOT ENTER after {_ew_cut} Auckland",
            f"⚡ IDEAL ENTRY: Wait for price to {_ideal_verb} {_fmt_price(entry_raw)} then enter",
            f"- Do NOT enter if price moves more than 30 pips from entry before {_ew_ses}",
            "- If price gaps past entry on open — skip this trade entirely",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"📈 Confidence: {conf}/10  {_conf_bar(conf)}",
        ]
        # ML win-probability
        try:
            from src import ml_predictor as _mlp_tb
            _wp_tb = _mlp_tb.get_win_prob(pair, pp, r.get("bundle", {}))
            if _wp_tb:
                block.append(f"🤖 <b>Predicted win probability: {_wp_tb}</b>")
        except Exception:
            pass
        # RSI divergence (Python-computed — reliable, not dependent on Claude)
        _daily_tech = r.get("bundle", {}).get("technical", {}).get("daily", {})
        _div        = _daily_tech.get("divergence", {}) if isinstance(_daily_tech, dict) else {}
        _div_bul    = _div.get("bullish")
        _div_ber    = _div.get("bearish")
        _div_confirms = (direction == "BUY" and _div_bul) or (direction == "SELL" and _div_ber)
        _div_conflicts = (direction == "BUY" and _div_ber) or (direction == "SELL" and _div_bul)
        if _div_confirms:
            _d = _div_bul if direction == "BUY" else _div_ber
            block.append(
                f"⚡ <b>RSI Divergence: {'Bullish' if direction == 'BUY' else 'Bearish'} CONFIRMED</b>"
                f" — price {_d['price_diff_pips']:.0f}p {'lower low' if direction == 'BUY' else 'higher high'}"
                f", RSI {_d['rsi_diff']:.0f}pt {'higher low' if direction == 'BUY' else 'lower high'}"
                f" ({_d['strength'].upper()}) +1 confidence"
            )
        elif _div_conflicts:
            _dc = _div_ber if direction == "BUY" else _div_bul
            block.append(
                f"⚠️ RSI Divergence CONFLICT: "
                f"{'bearish' if direction == 'BUY' else 'bullish'} divergence detected"
                f" ({_dc['price_diff_pips']:.0f}p / {_dc['rsi_diff']:.0f}pt RSI) — see risk factors"
            )
        # Oscillator confluence (RSI + Stochastic + CCI)
        _osc        = _daily_tech.get("oscillator_confluence", {}) if isinstance(_daily_tech, dict) else {}
        _osc_dir    = _osc.get("direction", "NONE")
        _osc_score  = _osc.get("score", 0)
        _osc_triple = _osc.get("triple", False)
        if _osc_dir == direction and _osc_score >= 2:
            _osc_state = "OVERSOLD" if direction == "BUY" else "OVERBOUGHT"
            _osc_boost = "+2 confidence" if _osc_triple else "+1 confidence"
            _osc_label = "TRIPLE" if _osc_triple else f"PARTIAL ({_osc_score}/3)"
            _osc_sigs  = " | ".join(filter(None, [
                f"RSI {_osc.get('stoch_k','?'):.0f}" if isinstance(_osc.get('stoch_k'), float) else None,
                f"Stoch %K {_osc.get('stoch_k','?')}" if _osc.get('stoch_k') is not None else None,
                f"CCI {_osc.get('cci','?')}" if _osc.get('cci') is not None else None,
            ]))
            _rsi_v  = _daily_tech.get("rsi14", "?")
            _stk_v  = _osc.get("stoch_k", "?")
            _cci_v  = _osc.get("cci", "?")
            block.append(
                f"📊 <b>Oscillator Confluence: {_osc_label} {_osc_state}</b> ({_osc_boost})"
            )
            block.append(
                f"   RSI {_rsi_v} | Stoch %K {_stk_v} | CCI {_cci_v}"
            )
        elif _osc_dir not in ("NONE", direction) and _osc_score >= 2:
            block.append(
                f"⚠️ Oscillator conflict: {_osc_dir.lower()} signals"
                f" (Stoch %K {_osc.get('stoch_k','?')} | CCI {_osc.get('cci','?')}) — see risk factors"
            )
        _mtf = r.get("bundle", {}).get("mtf", {})
        if _mtf and _mtf.get("agreeing_count", 0) > 0:
            _mtf_txt_tb = _mtf_plain_english(_mtf)
            if _mtf_txt_tb:
                block.append(f"🕐 {_mtf_txt_tb}")
        _rib_line = _ribbon_display(r.get("bundle", {}))
        if _rib_line:
            block.append(_rib_line)
        block.append("🔍 <b>Why all data agrees:</b>")
        for why_line in _why_agrees_lines(r, ctx):
            block.append(why_line)
        block += [
            "━━━━━━━━━━━━━━━━━━━━━",
            "⚠️ <b>Risk Factors:</b>",
        ]
        for rf_line in (rf_parts or ([rf[:100]] if rf else ["—"])):
            block.append(f"- {rf_line}")
        block.append("━━━━━━━━━━━━━━━━━━━━━")
        block.append("📋 <b>Trade Management After Entry:</b>")
        try:
            for mgmt in _management_lines(
                direction, float(entry_raw), float(adj_stop), float(adj_tgt), pair
            ):
                block.append(mgmt)
        except (TypeError, ValueError):
            block.append(f"- Full target: {_fmt_price(adj_tgt)}")
        return [ln for ln in block if ln]

    def _watch_entry(rr: dict) -> list:
        """Build watch list entry (conf 5–6) with indicative levels and session time."""
        pp    = rr["parsed"]
        conf  = pp.get("confidence") or "?"
        dirn  = (pp.get("direction") or "—").upper()
        arrow = "📈" if dirn == "BUY" else "📉"
        ntc   = _what_needs_to_change(pp)
        # Compute entry quality badge first so it appears in header
        _ew_we    = _entry_window_for_pair(rr["pair"])
        _eq_we_e, _eq_we_l = _entry_quality(rr["pair"], now_ak)
        _tref_we  = _time_ref_for_entry(_ew_we[0], _ew_we[1], now_ak)
        _start_we = _fmt_time_exact(_ew_we[0], _ew_we[1])
        lines = [
            "",
            f"{arrow} <b>{rr['pair']}</b> {dirn}  {conf}/10 {_conf_bar(conf)}  {_eq_we_e} {_eq_we_l}",
            f"{_score_breakdown_line(pp)}",
        ]
        # ML win-probability (only show when model is trained and ready)
        try:
            from src import ml_predictor as _mlp_we
            _wp_we = _mlp_we.get_win_prob(rr["pair"], pp, rr.get("bundle", {}))
            if _wp_we and "learning" not in _wp_we:
                lines.append(f"🤖 Predicted win probability: {_wp_we}")
        except Exception:
            pass
        _wl_hint = _weakest_layer_hint(pp)
        if _wl_hint:
            lines.append(f"↑ {_wl_hint}")
        _mtf_wl = rr.get("bundle", {}).get("mtf", {})
        if _mtf_wl and _mtf_wl.get("agreeing_count", 0) > 0:
            _mtf_txt = _mtf_plain_english(_mtf_wl)
            if _mtf_txt:
                lines.append(_mtf_txt)
        _rib_wl = _ribbon_display(rr.get("bundle", {}))
        if _rib_wl:
            lines.append(_rib_wl)
        # Indicative entry/stop/target — always shown so investor knows the trade shape
        ind_e, ind_s, ind_t = _calc_indicative_levels(rr["pair"], pp, rr.get("bundle", {}))
        if ind_e and ind_s and ind_t:
            is_jpy = "JPY" in rr["pair"].upper()
            dec = 3 if is_jpy else 5
            try:
                rr_ratio   = abs(float(ind_t) - float(ind_e)) / abs(float(ind_e) - float(ind_s))
                risk_pips  = abs(float(ind_e) - float(ind_s)) / _pip_size(rr["pair"])
                profit_pips= abs(float(ind_t) - float(ind_e)) / _pip_size(rr["pair"])
                risk_usd   = max(1, round(risk_pips))
                profit_usd = max(1, round(profit_pips))
                lines.append("🟡 <b>READY TO TRADE IF CONFIRMED:</b>")
                lines.append(
                    f"Entry ~{ind_e:.{dec}f} | "
                    f"Stop ~{ind_s:.{dec}f} | Target ~{ind_t:.{dec}f}"
                )
                lines.append(f"Risk ${risk_usd} → Make ${profit_usd} ({rr_ratio:.1f}:1)")
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        lines += [
            f"{_eq_we_e} <b>BE READY TO ENTER:</b> {_ew_we[6]} {_start_we} Auckland {_tref_we}",
            f"If confidence reaches 7+ before {_start_we} — enter immediately at market price",
            f"If confidence reaches 7+ during {_ew_we[6]} — enter within 30 minutes of the open",
            f"If confidence has not reached 7+ by {_ew_we[5]} {_tref_we} — skip this pair today",
            f"Needs: {ntc}",
        ]
        return lines

    def _approaching_entry(rr: dict) -> list:
        """Build approaching signal entry (conf 3–4) with indicative levels and entry alert time."""
        pp   = rr["parsed"]
        conf = pp.get("confidence") or "?"
        dirn = (pp.get("direction") or "—").upper()
        lines = [
            "",
            f"<b>{rr['pair']}</b> {conf}/10 {dirn} — if conditions improve:",
        ]
        ind_e, ind_s, ind_t = _calc_indicative_levels(rr["pair"], pp, rr.get("bundle", {}))
        if ind_e and ind_s and ind_t:
            is_jpy = "JPY" in rr["pair"].upper()
            dec = 3 if is_jpy else 5
            try:
                lines.append("🟠 <b>POTENTIAL SETUP IF CONDITIONS IMPROVE:</b>")
                lines.append(
                    f"Entry ~{ind_e:.{dec}f} | "
                    f"Stop ~{ind_s:.{dec}f} | Target ~{ind_t:.{dec}f}"
                )
            except (TypeError, ValueError):
                pass
        _ew_ae = _entry_window_for_pair(rr["pair"])
        _eq_ae_e, _ = _entry_quality(rr["pair"], now_ak)
        _tref_ae = _time_ref_for_entry(_ew_ae[0], _ew_ae[1], now_ak)
        _start_ae = _fmt_time_exact(_ew_ae[0], _ew_ae[1])
        lines += [
            f"{_eq_ae_e} <b>SET ALERT FOR:</b> {_start_ae} Auckland {_tref_ae} — check if this pair improved",
            "If confidence reaches 6+ at any scan today — add to watch list",
        ]
        return lines

    # ═══════════════════════════════════════════════════════════════════════════
    # 6AM FULL SCAN — comprehensive morning briefing
    # ═══════════════════════════════════════════════════════════════════════════
    if scan_mode == "full":
        all_sections.append([
            f"<b>🤖 Forex AI — {_badge} — {_fmt_date_nz(now_ak)}</b>",
            f"Universe: {universe_size} · Deep analysed: <b>{n_deep}</b> · {setup_line}",
        ])

        # OPEN TRADES — always at the top so it's the first thing seen
        all_sections.append(_build_open_trades_section(_ot_open_trades, _ot_px_cache, now_ak))

        # MARKET CONTEXT
        ctx_lines = ["", "━━━━━━━━━━━━━━━━━━━━━", "🌍 <b>MARKET CONTEXT</b>"]
        vix_str = f"VIX {ctx['vix']:.1f}" if ctx["vix"] else ""
        env_str = ctx["risk_env"]
        ctx_lines.append(
            f"Environment: <b>{env_str}</b>"
            f"{' (' + vix_str + ')' if vix_str else ''}"
        )
        sc = ctx.get("strongest_ccy")
        wc = ctx.get("weakest_ccy")
        scores = ctx.get("ccy_scores", {})
        if sc:
            sc_reason = "carry + risk-on" if "risk-on" in env_str else "carry + fundamentals"
            ctx_lines.append(f"💪 Strongest: <b>{sc}</b> — {sc_reason} (+{scores.get(sc,0):.0f})")
        if wc:
            wc_reason = "low rates + risk-on selling" if "risk-on" in env_str else "weak fundamentals"
            ctx_lines.append(f"📉 Weakest: <b>{wc}</b> — {wc_reason} ({scores.get(wc,0):.0f})")
        nw_list = []
        for r in deep_results:
            nw = (r["parsed"].get("news_warning") or "").strip()
            if nw and len(nw) > 5 and "none" not in nw.lower() and "n/a" not in nw.lower():
                nw_list.append(f"{r['pair']}: {nw[:60]}")
        if nw_list:
            ctx_lines.append(f"⚡ {nw_list[0]}")
        all_sections.append(ctx_lines)

        # TRADE ALERTS
        if yes_trades:
            for r in yes_trades:
                all_sections.append(_trade_block(r))

        # WHY NO SETUPS
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
                no_sec.append(
                    f"⚠️ All {len(failed_pairs)} pairs failed — "
                    "check ANTHROPIC_API_KEY in GitHub secrets"
                )
            else:
                no_sec.append("No qualifying setups — staying in cash is a valid position.")
            all_sections.append(no_sec)

        # WATCH LIST with session info
        watch_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "👀 <b>WATCH LIST</b>"]
        if watch_list:
            for rr in watch_list:
                watch_sec.extend(_watch_entry(rr))
        else:
            watch_sec.append("No pairs in the 5–6 confidence range today.")
        all_sections.append(watch_sec)

        # APPROACHING SIGNALS with indicative levels and session time
        if upcoming:
            up_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "📡 <b>APPROACHING SIGNAL</b>"]
            for rr in upcoming:
                up_sec.extend(_approaching_entry(rr))
            all_sections.append(up_sec)

        # LEARNING UPDATE
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

        # RISK DASHBOARD
        if risk_data and risk_data.get("profile"):
            try:
                from src import risk_manager as _rm_dash
                prof     = risk_data["profile"]
                rmode    = _risk_state.get("risk_mode", "normal")
                rpct     = _risk_state.get("base_risk_pct", 1.0)
                exp      = _exposure.get("total_pct", 0.0)
                fund     = prof.get("estimated_balance", _rm_dash.FUND_START)
                fund_pk  = prof.get("peak_balance", fund)
                fund_ret = (fund - _rm_dash.FUND_START) / _rm_dash.FUND_START * 100
                real     = config.ACCOUNT_BALANCE
                mode_icons = {
                    "capital_protection": "⬇️",
                    "streak_protection":  "⬇️",
                    "reduced":            "➡️",
                    "normal":             "➡️",
                    "enhanced":           "⬆️",
                }
                icon = mode_icons.get(rmode, "➡️")
                all_sections.append([
                    f"📈 FOREX AI FUND: ${fund:,.0f} ({fund_ret:+.1f}%) | Peak: ${fund_pk:,.0f}",
                    f"💼 Real Account: ${real:,.0f} | {rpct:.1f}%/trade | {exp:.1f}% open | "
                    f"{icon} {rmode.replace('_',' ').title()}",
                ])
            except Exception:
                pass

        # CREDIT BALANCE
        try:
            primary_bal = credit_data.get("primary_balance")
            backup_bal  = credit_data.get("backup_balance")
            total_bal   = (
                (primary_bal or 0.0)
                + (backup_bal or 0.0 if config.ANTHROPIC_API_KEY_2 else 0.0)
            )
            daily_cost = config.DAILY_COST_USD
            if total_bal > 0 and daily_cost > 0:
                runway = int(total_bal / daily_cost)
                all_sections.append([f"💳 ${total_bal:.2f} combined ({runway} days)"])
            elif primary_bal is not None:
                all_sections.append([f"💳 ${primary_bal:.2f} primary"])
        except Exception:
            pass

        # WEEKLY PERFORMANCE (Monday only)
        weekly = _weekly_performance_section(date)
        if weekly:
            all_sections.append(weekly)

        # SYSTEM HEALTH
        health_issues: list = []
        weak_tech = [r for r in deep_results if r["parsed"].get("technical_score") in (None, 1, 2)]
        if weak_tech and len(weak_tech) >= max(1, len(deep_results) // 2):
            _wt_pairs = ", ".join(r["pair"] for r in weak_tech[:5])
            health_issues.append(
                f"Technical scores suppressed — T:1/2/N/A on {len(weak_tech)} pairs "
                f"({_wt_pairs}) — check DIAG lines in Actions log"
            )
        if td_calls > 600:
            health_issues.append(
                f"Twelve Data quota at risk — {td_calls} calls used today (limit ~800)"
            )
        try:
            primary_bal = credit_data.get("primary_balance")
            if primary_bal is not None:
                if primary_bal < 2.0:
                    health_issues.append(
                        f"🚨 PRIMARY CREDITS URGENT — ${primary_bal:.2f} remaining, top up NOW"
                    )
                elif primary_bal < 5.0:
                    health_issues.append(
                        f"Primary credits low — ${primary_bal:.2f} remaining, top up soon"
                    )
        except Exception:
            pass
        if failed_pairs:
            health_issues.append(
                f"{len(failed_pairs)} pair{'s' if len(failed_pairs) > 1 else ''} "
                "failed analysis — check GitHub Actions logs"
            )
        if run_duration_min > 20:
            health_issues.append(f"Run took {run_duration_min:.0f} minutes — longer than normal")
        import os as _os
        if not _os.getenv("ACCOUNT_BALANCE"):
            health_issues.append(
                "Account balance not configured — set ACCOUNT_BALANCE in GitHub secrets"
            )
        health_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "⚠️ <b>SYSTEM HEALTH</b>"]
        if threshold_revert_msg:
            health_sec.append(threshold_revert_msg)
        for issue in health_issues:
            health_sec.append(f"- {issue}")
        # ML model status line
        try:
            from src import ml_predictor as _mlp_hs
            health_sec.append(_mlp_hs.get_model_status_line())
        except Exception:
            pass
        all_sections.append(health_sec)

        # RESEARCH THRESHOLD ANALYSIS
        if research_result:
            br   = research_result.get("band_results", {})
            rec  = research_result.get("recommendation", "")
            rsn  = research_result.get("reasoning", "")
            days = research_result.get("days_of_data", 0)
            rec_icon  = {"LOWER_TO_6": "✅", "KEEP_AT_7": "❌", "MARGINAL_EDGE": "⚠️"}.get(rec, "🔬")
            rec_label = {
                "LOWER_TO_6":        "Consider lowering threshold to 6",
                "KEEP_AT_7":         "Keep threshold at 7",
                "MARGINAL_EDGE":     "Marginal edge — collect more data",
                "INSUFFICIENT_DATA": "Insufficient data",
            }.get(rec, rec)
            def _fmt_band(s):
                if not s:
                    return "no data"
                wr  = f"{s['win_rate']*100:.0f}%"
                exp = f"{s['expectancy']:+.2f}R" if s.get("expectancy") is not None else "—R"
                return f"{s['n']} trades · {wr} win · {exp}"
            r_sec = [
                "", "━━━━━━━━━━━━━━━━━━━━━",
                f"🔬 <b>RESEARCH MODE — {days}-day threshold study</b>",
                f"Conf 5: {_fmt_band(br.get('5'))}",
                f"Conf 6: {_fmt_band(br.get('6'))}",
                f"Conf 7: {_fmt_band(br.get('7'))}",
                f"Conf 8-10: {_fmt_band(br.get('8-10'))}",
                f"{rec_icon} <b>{rec_label}</b>",
                f"<i>{rsn}</i>",
            ]
            all_sections.append(r_sec)

        # SESSION GUIDE
        all_sections.append([
            "",
            "━━━━━━━━━━━━━━━━━━━━━",
            "🕐 <b>TODAY'S SESSIONS (Auckland time)</b>",
            "Sydney: 9am–6pm | Tokyo: 12pm–9pm | London: 7pm–4am | New York: 1am–10am",
            "Best overlap: 1am–4am Auckland (London/NY peak volume)",
        ])

    # ═══════════════════════════════════════════════════════════════════════════
    # INTRADAY SCANS (9AM / 3PM / 5PM) — unified expanded format
    # ═══════════════════════════════════════════════════════════════════════════
    elif scan_mode in ("asian", "midday", "prelondon"):
        all_sections.append([f"<b>🤖 Forex AI — {_badge} — {today_short}</b>"])

        # OPEN TRADES — always at the top so it's the first thing seen
        all_sections.append(_build_open_trades_section(_ot_open_trades, _ot_px_cache, now_ak))

        # ── Market context (one-line brief) ───────────────────────────────────
        vix_str   = f"VIX {ctx['vix']:.1f}" if ctx["vix"] else ""
        env_str   = ctx["risk_env"]
        sc        = ctx.get("strongest_ccy")
        wc        = ctx.get("weakest_ccy")
        ctx_parts = [f"🌍 {env_str}"]
        if vix_str:
            ctx_parts.append(f"({vix_str})")
        if sc and wc:
            ctx_parts.append(f"| 💪 {sc}  📉 {wc}")
        all_sections.append(["", "━━━━━━━━━━━━━━━━━━━━━", " ".join(ctx_parts)])

        # ── YES trade alerts (full entry instructions) ────────────────────────
        for r in yes_trades:
            all_sections.append(_trade_block(r))

        # ── Pairs newly reaching 6+ since 6am ────────────────────────────────
        _yes_pairs   = {r["pair"] for r in yes_trades}
        _newly_6plus = [
            r for r in deep_results
            if r["pair"] not in _yes_pairs
            and _conf(r) >= 6
            and _morning_conf.get(r["pair"], 10) < 6
        ]
        if _newly_6plus:
            ns = ["", "━━━━━━━━━━━━━━━━━━━━━", "⬆️ <b>NEWLY REACHED 6+ SINCE 6AM</b>"]
            for r in _newly_6plus[:5]:
                pp    = r["parsed"]
                curr  = _conf(r)
                prev  = _morning_conf.get(r["pair"], 0)
                dirn  = (pp.get("direction") or "").upper()
                arrow = "📈" if dirn == "BUY" else "📉"
                sign  = f"+{curr - prev}"
                ns.append(
                    f"{arrow} <b>{r['pair']}</b> {dirn} — "
                    f"<b>{curr}/10</b> ({sign} since 6am)  {_conf_bar(curr)}"
                )
                _ew_ns = _entry_window_for_pair(r["pair"])
                _eq_ns_e, _eq_ns_l = _entry_quality(r["pair"], now_ak)
                _tref_ns = _time_ref_for_entry(_ew_ns[0], _ew_ns[1], now_ak)
                _start_ns = _fmt_time_exact(_ew_ns[0], _ew_ns[1])
                ns.append(
                    f"  {_eq_ns_e} <b>BE READY TO ENTER:</b> "
                    f"{_ew_ns[6]} {_start_ns} Auckland {_tref_ns}"
                )
            all_sections.append(ns)
        elif not yes_trades:
            _any_change = any(
                _morning_conf and abs(_conf(r) - _morning_conf.get(r["pair"], _conf(r))) >= 1
                for r in deep_results
            )
            if not _any_change:
                all_sections.append(["", "No new signals since morning scan."])

        # ── Watch list (5–6) and approaching signals (3–4) with levels + session ─
        _all_candidates = sorted(
            [r for r in deep_results if r["pair"] not in _yes_pairs and _conf(r) >= 3],
            key=_conf, reverse=True,
        )
        _watch_items      = [r for r in _all_candidates if _conf(r) >= 5][:3]
        _approaching_items= [r for r in _all_candidates if _conf(r) <= 4][:2]

        if _watch_items:
            wl_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "👀 <b>WATCH LIST</b>"]
            for rr in _watch_items:
                wl_sec.extend(_watch_entry(rr))
            all_sections.append(wl_sec)

        if _approaching_items:
            ap_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "📡 <b>APPROACHING SIGNAL</b>"]
            for rr in _approaching_items:
                ap_sec.extend(_approaching_entry(rr))
            all_sections.append(ap_sec)

    # COST SECTION — shown in all 4 scan modes just before footer
    if cost_lines:
        all_sections.append(["", "━━━━━━━━━━━━━━━━━━━━━"] + cost_lines)

    # ── FOOTER (all modes) ─────────────────────────────────────────────────────
    all_sections.append([
        "",
        f"<i>{_next_scan_footer(scan_mode, now_ak)}</i>",
    ])

    # Strip lines that contain unavailable / n/a noise
    def _is_ok_line(ln: str) -> bool:
        ll = ln.lower().strip()
        if not ll:
            return True
        bad = (" n/a", ": n/a", "=n/a", "check console")
        return not any(b in ll for b in bad)

    all_sections = [[ln for ln in sec if _is_ok_line(ln)] for sec in all_sections]
    _send_in_parts(all_sections)


# ── Daily run ──────────────────────────────────────────────────────────────────

_LAST_RUN_FILE = config.REPORTS_DIR.parent / "last_run.txt"
_COOLDOWN_SECS = 3600  # 60 minutes

_ALERTS_FILE         = config.DATA_DIR / "last_alerts.json"
_MORNING_RANKED_FILE = config.DATA_DIR / "morning_ranked.json"

# (label, session-currency filter set — empty = no filter)
_SCAN_MODES: dict = {
    "asian":     ("9am Check",     set()),
    "midday":    ("5pm Check",     set()),
    "prelondon": ("3pm Check",     set()),
    "full":      ("6am Full Scan", set()),
}

# Pair counts per scan mode (top_n for selector, max pairs after session filter)
_SCAN_TOP_N   = {"full": 15, "asian": 25, "midday": 15, "prelondon": 25}
_SCAN_MAX_PRS = {"full": 15, "asian":  7, "midday":  5, "prelondon":  7}

# Sonnet escalation threshold: 6 for 6am (higher quality), 7 for intraday (cheaper)
_SONNET_THRESH = {"full": 6, "asian": 7, "midday": 7, "prelondon": 7}

# Max pre-filtered pairs to warm Twelve Data cache for
_TD_CACHE_MAX = {"full": 20, "asian": 10, "midday": 10, "prelondon": 10}


def _get_scan_mode() -> str:
    """Return scan mode from SCAN_MODE env var or current Auckland hour."""
    import os as _os_
    mode = _os_.getenv("SCAN_MODE", "").lower().strip()
    if mode in _SCAN_MODES:
        return mode
    hour = _auckland_now().hour
    if 8 <= hour <= 10:
        return "asian"
    if 14 <= hour <= 16:
        return "prelondon"
    if 17 <= hour <= 19:
        return "midday"
    return "full"


def _filter_pairs_for_mode(pairs: list, mode: str) -> list:
    """Return only the pairs relevant to the given scan mode."""
    ccys = _SCAN_MODES.get(mode, ("", set()))[1]
    if not ccys:
        return pairs
    return [p for p in pairs if any(c in p.upper() for c in ccys)]


def _alert_fingerprints(results: list) -> set:
    """Build a set of 'PAIR:DIRECTION' strings for all YES trade alerts."""
    return {
        f"{r['pair']}:{(r['parsed'].get('direction') or '').upper()}"
        for r in results
        if r["parsed"].get("trade_this") == "YES"
    }


def _load_last_alerts() -> set:
    try:
        data = json.loads(_ALERTS_FILE.read_text(encoding="utf-8"))
        return set(data.get("alerts", []))
    except Exception:
        return set()


def _save_alerts(alerts: set) -> None:
    try:
        _ALERTS_FILE.write_text(
            json.dumps({"alerts": sorted(alerts)}), encoding="utf-8"
        )
    except Exception:
        pass


def _next_scan_footer(scan_mode: str, now_ak: datetime) -> str:
    """Return a localised 'next scan' line for the Telegram footer."""
    nxt = now_ak + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    nxt_short  = _fmt_date_short_nz(nxt)
    is_weekday = now_ak.weekday() < 5

    if scan_mode == "full" and is_weekday:
        return "⏰ Next scan today at 9am Auckland time"
    if scan_mode == "asian" and is_weekday:
        return "⏰ Next scan today at 3pm Auckland time"
    if scan_mode == "prelondon" and is_weekday:
        return "⏰ Next scan today at 5pm Auckland time"
    if scan_mode == "midday":
        return "⏰ Next scan tomorrow 6am Auckland — have a good evening."
    return f"⏰ Next full scan {nxt_short} at 6am Auckland time"


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

    _telegram_test()

    missing = config.missing_keys()
    if missing:
        print("ERROR: missing API keys in .env: " + ", ".join(missing), file=sys.stderr)
        return 2

    scan_mode = _get_scan_mode()
    print(f"[scan] mode={scan_mode} ({_SCAN_MODES[scan_mode][0]})", file=sys.stderr)

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

        try:
            from src import research_outcome_checker
            research_outcome_checker.check_open_research_trades(log=lambda m: _log_line(logf, m))
        except Exception as exc:
            _log_line(logf, f"Research outcome check failed: {exc}")

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

        # Retrain ML win-probability model if > 7 days old (weekly cadence)
        try:
            from src import ml_predictor as _mlp
            _ml_meta = _mlp.retrain_if_stale(quiet=True)
            if _ml_meta:
                _log_line(logf, f"ML model retrained: {_ml_meta.get('n_trades',0)} trades, "
                                 f"ROC-AUC {_ml_meta.get('roc_auc',0):.3f}")
            else:
                _log_line(logf, f"ML model: {_mlp.get_model_status_line()}")
        except Exception as _ml_exc:
            _log_line(logf, f"ML model step: {_ml_exc}")

        # Threshold auto-adjust: revert conf 6→7 / R:R 1.3→1.5 if win rate < 45% after 50 trades
        threshold_revert_msg = None
        try:
            from src import threshold_manager as _thresh_check
            threshold_revert_msg = _thresh_check.check_and_adjust(log=lambda m: _log_line(logf, m))
        except Exception as exc:
            _log_line(logf, f"Threshold check failed: {exc}")

        # 2. Smart pair selection
        from src import threshold_manager as _thresh_mgr
        _trade_conf   = _thresh_mgr.get_confidence_threshold()
        _top_n        = _SCAN_TOP_N.get(scan_mode, 15)
        _max_pairs    = _SCAN_MAX_PRS.get(scan_mode, 15)
        _td_cap       = _TD_CACHE_MAX.get(scan_mode, 20)
        # Sonnet threshold equals trade threshold so every potential TRADE_THIS YES
        # pair gets entry/stop/target — essential when threshold is 6 (data collection).
        sonnet_thresh = _trade_conf
        _coll_note = (
            " [DATA COLLECTION MODE — threshold lowered from 7/1.5 for trade accumulation]"
            if _thresh_mgr.is_data_collection_mode() else ""
        )
        _log_line(logf, f"Active thresholds: conf>={_trade_conf}, R:R>={_thresh_mgr.get_min_rr()}{_coll_note}")

        universe_size = len(selector.UNIVERSE)
        ranked_all    = []
        pairs_today   = []
        try:
            selection     = selector.select_pairs(top_n=_top_n, log=lambda m: _log_line(logf, m))
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

        # Midday scan: use morning ranked state to pick the 5 pairs closest to triggering
        if scan_mode == "midday":
            try:
                _mranked = json.loads(_MORNING_RANKED_FILE.read_text(encoding="utf-8"))
                _morning_pairs = [p for p, _ in sorted(_mranked.items(), key=lambda x: -x[1])]
                if _morning_pairs:
                    # Keep only pairs that are still in today's universe
                    pairs_today = [p for p in _morning_pairs if p in {q for q, _ in ranked_all}][:_max_pairs]
                    if not pairs_today:
                        pairs_today = _morning_pairs[:_max_pairs]
                    _log_line(logf, f"[midday] Using morning-ranked top {len(pairs_today)}: {', '.join(pairs_today)}")
            except Exception:
                pairs_today = pairs_today[:_max_pairs]

        # 2b. COST OPTIMISATION — Pre-filter using free data before Twelve Data fetch
        try:
            if ranked_all:
                pre_filtered = _pre_filter_pairs(
                    ranked_all, top_n=_td_cap, log=lambda m: _log_line(logf, m)
                )
                for p in pairs_today:
                    if p not in pre_filtered:
                        pre_filtered.insert(0, p)
                pre_filtered = pre_filtered[:_td_cap]
            else:
                pre_filtered = pairs_today
            _log_line(logf, f"Pre-filtered pool: {len(pre_filtered)} pairs for Twelve Data (cap={_td_cap})")
        except Exception as exc:
            _log_line(logf, f"Pre-filter failed ({exc}) — using selector output.")
            pre_filtered = pairs_today

        # 3. Batch pre-fetch shared data (FRED, COT, macro) once — all cached 12h
        _shared_fund: dict = {}
        _shared_macro      = None
        try:
            _shared_fund, _shared_macro = _pre_fetch_shared_data(
                pre_filtered, log=lambda m: _log_line(logf, m)
            )
        except Exception as exc:
            _log_line(logf, f"Shared data pre-fetch failed ({exc}) — each pair will fetch independently.")

        # 4. Pre-fetch Twelve Data candles — capped to _td_cap pairs
        try:
            from src import technical as _tech
            _tech.warm_cache(pre_filtered, log=lambda m: _log_line(logf, m))
        except Exception as exc:
            _log_line(logf, f"Technical pre-fetch failed (analysis will still run): {exc}")

        # Diagnostic: log indicator snapshot — all AUD crosses + one per currency group
        _log_line(logf, "[DIAG] Technical indicator snapshot (from cache):")
        _DIAG_PAIRS = [
            # AUD crosses — full set for debugging AUD-specific issues
            "AUD/CHF", "AUD/JPY", "AUD/USD", "AUD/NZD", "AUD/CAD",
            "EUR/AUD", "GBP/AUD",
            # One representative per other currency group
            "EUR/USD", "EUR/GBP", "EUR/JPY", "EUR/CHF",
            "GBP/USD", "GBP/JPY",
            "USD/JPY", "USD/CHF", "USD/CAD",
            "NZD/USD", "NZD/JPY",
        ]
        for _diag_pair in _DIAG_PAIRS:
            try:
                from src import technical as _tech
                _ind = _tech.read_cached_indicators(_diag_pair)
                if _ind:
                    _ts = _ind.get("tech_signal", {})
                    _log_line(logf, (
                        f"  {_diag_pair}: RSI={_ind['rsi14']}  "
                        f"MACDh={_ind['macd_hist']}  "
                        f"SMA50={_ind.get('sma50','?')}  "
                        f"BB={(_ind.get('bb_state') or 'inside bands').split('(')[0].strip()[:25]}  "
                        f"→ T_sig={_ts.get('direction','?')} {_ts.get('score','?')}/10"
                    ))
                else:
                    _log_line(logf, f"  {_diag_pair}: NOT IN CACHE (candles not fetched this run)")
            except Exception as _de:
                _log_line(logf, f"  {_diag_pair}: DIAG ERROR — {_de}")

        _log_line(logf, f"=== {scan_mode.upper()} run {date} | universe: {universe_size} pairs | Sonnet threshold: {sonnet_thresh}/10 ===")
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
                    sonnet_threshold=sonnet_thresh,
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
                # Capture ML feature snapshot for future win-probability training
                try:
                    from src import feature_extractor as _fe, feature_store as _fs
                    _feat = _fe.extract(result["pair"], pp, result.get("bundle", {}))
                    _fs.save("main", result["id"], _feat)
                except Exception:
                    pass

        # Haiku analyses all pairs; Sonnet only called for conf >= sonnet_thresh
        _log_line(logf, f"Analysing {len(pairs_today)} pairs (Haiku all, Sonnet if conf>={sonnet_thresh}): {', '.join(pairs_today)}")
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

        # Save morning-ranked state so intraday scans can pick the closest-to-trigger pairs
        if scan_mode == "full":
            try:
                morning_confs = {
                    r["pair"]: (_conf(r) or 0)
                    for r in deep_results
                }
                _MORNING_RANKED_FILE.write_text(
                    json.dumps(morning_confs), encoding="utf-8"
                )
                _log_line(logf, f"Morning ranked state saved ({len(morning_confs)} pairs).")
            except Exception as _mr_exc:
                _log_line(logf, f"Morning ranked save failed: {_mr_exc}")

        # Research trading mode: paper-trade every pair with conf >= 5 (0.01 lots)
        try:
            from src import research_tracker as _rt
            _rt_today = {
                (r["pair"], (r.get("direction") or "").upper())
                for r in _rt.load()
                if r.get("date") == date
            }
            _rt_logged = 0
            for _r in deep_results:
                _rconf = _conf(_r)
                if _rconf >= 5:
                    _rp   = _r["parsed"]
                    _rdir = (_rp.get("direction") or "").upper()
                    if (_r["pair"], _rdir) not in _rt_today:
                        _rsrc = (
                            "sonnet"
                            if all(_rp.get(k) for k in ("entry", "stop_loss", "target"))
                            else "haiku"
                        )
                        # Haiku-only results have no price levels — compute indicative
                        # entry/stop/target from the technical bundle so every conf-5+
                        # research trade can be properly tracked for outcome analysis.
                        if _rsrc == "haiku":
                            _ind_e, _ind_s, _ind_t = _calc_indicative_levels(
                                _r["pair"], _rp, _r.get("bundle", {})
                            )
                            if _ind_e and _ind_s and _ind_t:
                                _rp = dict(_rp)          # shallow copy — don't mutate original
                                _rp["entry"]     = _rp.get("entry")     or _ind_e
                                _rp["stop_loss"] = _rp.get("stop_loss") or _ind_s
                                _rp["target"]    = _rp.get("target")    or _ind_t
                                _rsrc = "indicative"
                        _rt_id = _rt.log_research_trade(_r["pair"], _rp, _rsrc, scan_mode)
                        # Capture ML feature snapshot keyed to this research trade
                        try:
                            from src import feature_extractor as _fe, feature_store as _fs
                            _feat = _fe.extract(_r["pair"], _r["parsed"], _r.get("bundle", {}))
                            _fs.save("research", _rt_id, _feat)
                        except Exception:
                            pass
                        _rt_today.add((_r["pair"], _rdir))
                        _rt_logged += 1
            if _rt_logged:
                _log_line(logf, f"Research mode: logged {_rt_logged} paper trade(s) (conf>=5).")
        except Exception as exc:
            _log_line(logf, f"Research trade logging failed: {exc}")

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

        # Record scan cost and prepare formatted lines for Telegram
        _cost_lines = []
        try:
            from src import cost_tracker as _ct
            _cost_lines = _ct.record_and_get_lines(
                run_stats.get("estimated_usd", 0.0),
                _auckland_now(),
            )
        except Exception as _ct_exc:
            _log_line(logf, f"Cost tracking failed: {_ct_exc}")

        # Research threshold analysis (only meaningful after 30 days of paper trades)
        research_result = None
        try:
            from src import research_analyst as _ra
            research_result = _ra.analyse(log=lambda m: _log_line(logf, m))
        except Exception as exc:
            _log_line(logf, f"Research analysis failed: {exc}")

        # Alert deduplication: intraday scans only notify when new trade alerts appear
        _last_alerts    = _load_last_alerts()
        _current_alerts = _alert_fingerprints(deep_results)
        _new_alerts     = _current_alerts - _last_alerts
        _save_alerts(_current_alerts)
        _should_notify = True
        _log_line(logf, f"[{scan_mode}] Sending Telegram. New alerts: {sorted(_new_alerts) if _new_alerts else 'none'}")

        # 10. Send Telegram summary
        if _should_notify:
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
                scan_mode=scan_mode,
                new_alerts=_new_alerts,
                research_result=research_result,
                threshold_revert_msg=threshold_revert_msg,
                cost_lines=_cost_lines,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
