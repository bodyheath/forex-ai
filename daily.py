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


def _session_status_for_pair(pair: str, now_ak: datetime) -> tuple:
    """Return (is_active, close_str) for the pair's primary trading session.

    is_active: True if the session is currently open in Auckland time.
    close_str: Human-readable close time, e.g. '4am', '9pm'.
    """
    _, _, _, _, _, _, sess_name = _entry_window_for_pair(pair)
    hour = now_ak.hour
    _SESS_WINDOWS = {
        "London open":   (19, 4,  "4am",  True),
        "Tokyo open":    (12, 21, "9pm",  False),
        "New York open": (1,  10, "10am", False),
        "Sydney open":   (9,  22, "10pm", False),
    }
    window = _SESS_WINDOWS.get(sess_name)
    if not window:
        return False, ""
    open_h, close_h, close_label, wraps = window
    if wraps:
        is_active = hour >= open_h or hour < close_h
    else:
        is_active = open_h <= hour < close_h
    return is_active, close_label


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
    stamp = _auckland_now().strftime("%H:%M:%S")
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
        print("[TELEGRAM] SKIP — TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return
    # Preserve intentional <b>, </b>, <i>, </i> tags while escaping all other
    # < > & characters that come from AI-generated text (e.g. "< 1.08 support").
    # Telegram HTML mode rejects any unescaped special chars and returns 400 silently.
    _TAG = _re_tg.compile(r'(</?[bi]>)')
    _parts = _TAG.split(message)
    message = "".join(
        p if i % 2 == 1 else _html_mod.escape(p, quote=False)
        for i, p in enumerate(_parts)
    )
    _named_recipients = [("Heath", config.TELEGRAM_CHAT_ID)]
    if config.TELEGRAM_CHAT_ID_2:
        _named_recipients.append(("George", config.TELEGRAM_CHAT_ID_2))
    if config.TELEGRAM_CHAT_ID_3:
        _named_recipients.append(("Max", config.TELEGRAM_CHAT_ID_3))
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    _preview = message[:120].replace("\n", " ")
    for name, chat_id in _named_recipients:
        try:
            data = urllib.parse.urlencode({
                "chat_id":    chat_id,
                "text":       message,
                "parse_mode": "HTML",
            }).encode()
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15)
            print(f"[TELEGRAM] SUCCESS — sent to {name} ({len(message)} chars)")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            print(f"[TELEGRAM] FAILED — {name}: HTTP {exc.code} {exc.reason} | {body} | preview: {_preview}", file=sys.stderr)
        except Exception as exc:
            print(f"[TELEGRAM] FAILED — {name}: {exc} | preview: {_preview}", file=sys.stderr)


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


def _eff_conf(result: dict) -> int:
    """Confidence after MA ribbon penalty: −1 when a STRONG ribbon is fully aligned
    against the trade direction (ALIGNED_BULL vs SELL or ALIGNED_BEAR vs BUY).
    LEANING ribbon statuses do not incur a penalty — only ALIGNED ones do.
    """
    raw = _conf(result)
    if raw == 0:
        return 0
    try:
        direction = (result.get("parsed", {}).get("direction") or "").upper()
        rib_status = (
            result.get("bundle", {})
            .get("technical", {})
            .get("daily", {}) or {}
        ).get("ribbon", {}).get("status", "")
        if rib_status == "ALIGNED_BULL" and direction == "SELL":
            return max(0, raw - 1)
        if rib_status == "ALIGNED_BEAR" and direction == "BUY":
            return max(0, raw - 1)
    except Exception:
        pass
    return raw


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

    # Monthly structural bias — informational background context
    monthly_counts: dict = {"BUY": 0, "SELL": 0}
    for r in deep_results:
        mtf = r.get("bundle", {}).get("mtf", {})
        if isinstance(mtf, dict):
            bd = mtf.get("breakdown", "")
            for part in bd.split():
                if ":" in part:
                    tf, sig = part.split(":", 1)
                    if tf == "M" and sig in ("BUY", "SELL"):
                        monthly_counts[sig] += 1
    total_m = monthly_counts["BUY"] + monthly_counts["SELL"]
    if total_m > 0:
        buy_m, sell_m = monthly_counts["BUY"], monthly_counts["SELL"]
        if sell_m > buy_m:
            ctx["monthly_bias"] = f"SELL ({sell_m}/{total_m} pairs bearish)"
        elif buy_m > sell_m:
            ctx["monthly_bias"] = f"BUY ({buy_m}/{total_m} pairs bullish)"
        else:
            ctx["monthly_bias"] = f"MIXED ({buy_m} bullish / {sell_m} bearish)"

    # MTF averages across all deep-analysed pairs
    mtf_scores, mtf_qualifies = [], []
    for r in deep_results:
        mtf = r.get("bundle", {}).get("mtf", {})
        if isinstance(mtf, dict) and mtf.get("breakdown", "UNAVAILABLE") != "UNAVAILABLE":
            ws = mtf.get("weighted_score")
            if ws is not None:
                mtf_scores.append(float(ws))
            mtf_qualifies.append(bool(mtf.get("qualifies", False)))
    ctx["avg_mtf_score"]   = (sum(mtf_scores) / len(mtf_scores)) if mtf_scores else None
    ctx["qualify_pct"]     = (sum(mtf_qualifies) / len(mtf_qualifies)) if mtf_qualifies else None

    # High-impact event count for the week (cached 6h in selector)
    try:
        hi_count, hi_notable = selector.count_weekly_high_impact_events()
    except Exception:
        hi_count, hi_notable = 0, []
    ctx["high_impact_count"]   = hi_count
    ctx["high_impact_notable"] = hi_notable

    return ctx


def _compute_patience_score(ctx: dict) -> dict:
    """Rate today's trading conditions 1-10 from four factors.

    VIX (0-3): calm market scores high, fear index > 25 scores 0.
    News (0-3): more high-impact events this week = lower score.
    MTF agreement (0-2): high avg weighted_score = cleaner trends.
    Trend clarity (0-2): % of pairs that pass weekly+daily gate.

    Returns dict with 'score' (int 1-10), 'description' (str), and
    individual component scores for debugging.
    """
    # VIX component (0-3)
    vix = ctx.get("vix")
    if vix is None:
        vix_pts = 1.5
    elif vix <= 13:
        vix_pts = 3.0
    elif vix <= 17:
        vix_pts = 2.5
    elif vix <= 20:
        vix_pts = 2.0
    elif vix <= 25:
        vix_pts = 1.0
    else:
        vix_pts = 0.0

    # News component (0-3)
    hi = ctx.get("high_impact_count", 0) or 0
    if hi == 0:
        news_pts = 3.0
    elif hi == 1:
        news_pts = 2.0
    elif hi <= 3:
        news_pts = 1.0
    else:
        news_pts = 0.0

    # MTF average weighted score (0-2)
    avg_mtf = ctx.get("avg_mtf_score")
    if avg_mtf is None:
        mtf_pts = 1.0
    elif avg_mtf >= 0.70:
        mtf_pts = 2.0
    elif avg_mtf >= 0.45:
        mtf_pts = 1.0
    else:
        mtf_pts = 0.0

    # Trend clarity: % of pairs with qualifying weekly+daily alignment (0-2)
    qpct = ctx.get("qualify_pct")
    if qpct is None:
        trend_pts = 1.0
    elif qpct >= 0.50:
        trend_pts = 2.0
    elif qpct >= 0.25:
        trend_pts = 1.0
    else:
        trend_pts = 0.0

    raw = vix_pts + news_pts + mtf_pts + trend_pts  # 0–10
    score = max(1, min(10, round(raw)))

    # Build description from the weakest factors
    parts = []
    notable = ctx.get("high_impact_notable") or []

    if avg_mtf is not None and avg_mtf < 0.45:
        parts.append("choppy market")
    elif avg_mtf is not None and avg_mtf < 0.70:
        parts.append("moderate trend clarity")
    else:
        parts.append("strong trend clarity")

    if vix is not None and vix > 25:
        parts.append("VIX elevated")
    elif vix is not None and vix > 20:
        parts.append("VIX above average")

    if hi > 0:
        if notable:
            ev_label = notable[0]
            # shorten well-known events
            for kw, short in [("Non-Farm", "NFP"), ("Nonfarm", "NFP"),
                               ("Federal Reserve", "Fed rate decision"),
                               ("FOMC", "FOMC"), ("Consumer Price", "CPI")]:
                if kw.lower() in ev_label.lower():
                    ev_label = short
                    break
            parts.append(f"{ev_label} this week")
        else:
            parts.append(f"{hi} high-impact event{'s' if hi != 1 else ''} this week")

    if qpct is not None and qpct < 0.25:
        parts.append("few pairs with clear directional bias")

    desc_body = ", ".join(parts)

    if score >= 8:
        suffix = "ideal conditions for A/B setups"
    elif score >= 6:
        suffix = "consider waiting for cleaner setups"
    elif score >= 4:
        suffix = "be selective — only A-grade setups"
    elif score >= 2:
        suffix = "strong recommendation to reduce size"
    else:
        suffix = "strong recommendation to stay in cash today"

    description = f"{desc_body} — {suffix}" if desc_body else suffix

    return {
        "score":     score,
        "raw":       round(raw, 1),
        "vix_pts":   vix_pts,
        "news_pts":  news_pts,
        "mtf_pts":   mtf_pts,
        "trend_pts": trend_pts,
        "description": description,
    }


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


def _mtf_plain_english(mtf_data: dict, trade_direction: str = None) -> str:
    """Convert MTF data to a 3-TF swing-trading display for phone-readable output.

    Format: "Timeframes: Weekly SELL Daily SELL 4H SELL — all 3 aligned strongest signal"
    Or:     "Timeframes: Weekly SELL Daily SELL 4H BUY — 2 of 3 agree SELL valid setup"
    Monthly is excluded from the display (shown separately in market context).
    """
    if not isinstance(mtf_data, dict):
        return ""
    breakdown = mtf_data.get("breakdown", "")
    if not breakdown or breakdown == "UNAVAILABLE":
        return ""

    direction = (trade_direction or mtf_data.get("direction") or "NEUTRAL").upper()
    if direction == "NEUTRAL":
        return ""

    # Only display the 3 core swing-trading timeframes (skip monthly M)
    _TF_DISPLAY = {"W": "Weekly", "D": "Daily", "4H": "4H"}

    tf_signals: dict = {}
    for part in breakdown.split():
        if ":" in part:
            tf, sig = part.split(":", 1)
            if tf in _TF_DISPLAY:
                tf_signals[tf] = sig

    if not tf_signals:
        return ""

    parts   = []
    agreeing = 0
    for tf in ("W", "D", "4H"):
        if tf in tf_signals:
            sig = tf_signals[tf]
            parts.append(f"{_TF_DISPLAY[tf]} {sig}")
            if sig == direction:
                agreeing += 1

    tf_str = " ".join(parts)
    total  = len(parts)

    if agreeing == total == 3:
        return f"Timeframes: {tf_str} — all 3 aligned strongest signal"
    if agreeing >= 2:
        return f"Timeframes: {tf_str} — {agreeing} of {total} agree {direction} valid setup"
    if agreeing == 1:
        return f"Timeframes: {tf_str} — only 1 of {total} align {direction}"
    return f"Timeframes: {tf_str}"


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


_GRADE_LABELS: dict = {
    "A": "TAKE IMMEDIATELY",
    "B": "TAKE IF NO A AVAILABLE",
    "C": "WATCH ONLY",
    "D": "AVOID",
    "F": "NEVER TRADE",
}
_GRADE_ICONS: dict = {"A": "🏆", "B": "✅", "C": "👀", "D": "⚠️", "F": "❌"}


def _trade_quality_grade(r: dict) -> dict:
    """Compute A–F trade quality grade.

    A — conf≥8, R:R>2.5, all 3 TFs, ATR-calibrated, Fib near, no news, ribbon aligned, divergence
    B — conf≥7, R:R≥2.0, weekly+daily agree, no severe negatives
    C — conf≥6, R:R≥1.5, no blocking conditions
    D — ribbon against direction, R:R<1.5, or TF conflict + low conf
    F — ribbon strongly against, weekly/daily direct conflict, or R:R<1.3
    """
    p         = r.get("parsed", {})
    bundle    = r.get("bundle", {})
    direction = (p.get("direction") or "").upper()

    try:
        conf_raw = int(p.get("confidence") or 0)
    except (TypeError, ValueError):
        conf_raw = 0

    # R:R from parsed price levels (use Claude's field as fallback)
    rr = 0.0
    try:
        e = float(p.get("entry")     or 0)
        s = float(p.get("stop_loss") or 0)
        t = float(p.get("target")    or 0)
        if e and s and t and abs(e - s) > 0:
            rr = abs(t - e) / abs(e - s)
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    if not rr:
        try:
            rr = float(p.get("reward_risk") or 0)
        except (TypeError, ValueError):
            rr = 0.0
    if not rr:
        try:
            _fb_e, _fb_s, _fb_t, _ = _calc_indicative_levels(
                r["pair"], p, r.get("bundle", {})
            )
            if _fb_e and _fb_s and _fb_t and abs(_fb_e - _fb_s) > 0:
                rr = abs(_fb_t - _fb_e) / abs(_fb_e - _fb_s)
        except Exception:
            pass

    # MTF signals
    mtf    = bundle.get("mtf", {})
    sigs   = mtf.get("signals", {})
    w_sig  = sigs.get("weekly",  "NEUTRAL")
    d_sig  = sigs.get("daily",   "NEUTRAL")
    h4_sig = sigs.get("h4",      "NEUTRAL")
    w_ok   = w_sig  == direction
    d_ok   = d_sig  == direction
    h4_ok  = h4_sig == direction
    w_d_agree    = w_ok and d_ok
    all3_agree   = w_ok and d_ok and h4_ok
    w_d_conflict = (w_sig  in ("BUY", "SELL") and d_sig in ("BUY", "SELL")
                    and w_sig != d_sig)

    # Ribbon
    _dt  = (bundle.get("technical", {}).get("daily") or {}) if bundle else {}
    _rib = (_dt.get("ribbon") or {}) if isinstance(_dt, dict) else {}
    rib_st = str(_rib.get("status") or "") if isinstance(_rib, dict) else ""

    rib_aligned = (
        (direction == "BUY"  and rib_st in ("ALIGNED_BULL", "LEANING_BULL")) or
        (direction == "SELL" and rib_st in ("ALIGNED_BEAR", "LEANING_BEAR"))
    )
    rib_strongly_against = (
        (direction == "BUY"  and rib_st == "ALIGNED_BEAR") or
        (direction == "SELL" and rib_st == "ALIGNED_BULL")
    )
    rib_against = (
        (direction == "BUY"  and rib_st in ("ALIGNED_BEAR", "LEANING_BEAR")) or
        (direction == "SELL" and rib_st in ("ALIGNED_BULL", "LEANING_BULL"))
    )

    # Effective confidence (ribbon ALIGNED penalty already in _eff_conf; replicate here)
    conf = conf_raw - (1 if rib_strongly_against else 0)
    conf = max(1, conf)

    # Divergence
    _div = _dt.get("divergence", {}) if isinstance(_dt, dict) else {}
    div_confirmed = (
        (direction == "BUY"  and bool(_div.get("bullish"))) or
        (direction == "SELL" and bool(_div.get("bearish")))
    )

    # Fibonacci near level
    _fib    = _dt.get("fibonacci", {}) if isinstance(_dt, dict) else {}
    fib_near = (isinstance(_fib, dict) and _fib.get("status") == "ok"
                and bool(_fib.get("near_levels")))

    # ATR calibration (stop ≈ 0.7–1.5× ATR, target ≈ 1.5–3.0× ATR)
    atr14   = float(_dt.get("atr14") or 0) if isinstance(_dt, dict) else 0
    atr_cal = False
    if atr14 > 0:
        try:
            e2 = float(p.get("entry")     or 0)
            s2 = float(p.get("stop_loss") or 0)
            t2 = float(p.get("target")    or 0)
            sd = abs(e2 - s2)
            td = abs(t2 - e2)
            if sd > 0 and td > 0:
                atr_cal = (0.7 <= sd / atr14 <= 1.5) and (1.5 <= td / atr14 <= 3.0)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # News warning (no major events)
    news_raw = (p.get("news_warning") or "").lower().strip()
    no_news  = (not news_raw or
                any(kw in news_raw for kw in ("none", "n/a", "no major", "no significant")))

    # ── Grade F — never trade ─────────────────────────────────────────────
    if rib_strongly_against or w_d_conflict or rr < 1.3:
        grade = "F"
    # ── Grade D — avoid ──────────────────────────────────────────────────
    elif rib_against or rr < 1.5 or (not w_d_agree and conf <= 6):
        grade = "D"
    # ── Grade A — take immediately ────────────────────────────────────────
    elif (conf >= 8 and rr > 2.5 and all3_agree and
          atr_cal and fib_near and no_news and rib_aligned and div_confirmed):
        grade = "A"
    # ── Grade B — take if no A ────────────────────────────────────────────
    elif conf >= 7 and rr >= 2.0 and w_d_agree:
        grade = "B"
    # ── Grade C — watch only ──────────────────────────────────────────────
    elif conf >= 6 and rr >= 1.5:
        grade = "C"
    # ── Default D ─────────────────────────────────────────────────────────
    else:
        grade = "D"

    return {
        "grade":        grade,
        "label":        _GRADE_LABELS[grade],
        "icon":         _GRADE_ICONS[grade],
        "conf":         conf,
        "rr":           round(rr, 2),
        "all3_agree":   all3_agree,
        "w_d_agree":    w_d_agree,
        "w_d_conflict": w_d_conflict,
        "atr_cal":      atr_cal,
        "fib_near":     fib_near,
        "no_news":      no_news,
        "rib_aligned":  rib_aligned,
        "rib_against":  rib_against,
        "div_confirmed": div_confirmed,
    }


def _grade_display_line(qg: dict) -> str:
    """One-line grade badge: icon, grade, label, key factor dots."""
    grade = qg["grade"]
    rr    = qg["rr"]
    conf  = qg["conf"]

    badges = [f"Conf {conf}", f"R:R {rr:.1f}"]

    if grade in ("A", "B"):
        if qg.get("all3_agree"):
            badges.append("✓ All 3 TFs")
        elif qg.get("w_d_agree"):
            badges.append("✓ W+D agree")
        else:
            badges.append("✗ TF conflict")
        if qg.get("rib_aligned"):
            badges.append("✓ Ribbon")
        elif qg.get("rib_against"):
            badges.append("✗ Ribbon")
        if grade == "A":
            if qg.get("div_confirmed"):
                badges.append("✓ Divergence")
            if qg.get("fib_near"):
                badges.append("✓ Fib")
            if not qg.get("no_news"):
                badges.append("⚠️ News risk")
            if qg.get("atr_cal"):
                badges.append("✓ ATR-calibrated")
    elif grade == "C":
        if qg.get("w_d_agree"):
            badges.append("W+D agree")
        else:
            badges.append("mixed TFs")
    elif grade in ("D", "F"):
        if qg.get("w_d_conflict"):
            badges.append("W/D conflict")
        if qg.get("rib_against"):
            badges.append("ribbon against")
        if rr < 1.5:
            badges.append("low R:R")

    return f"{qg['icon']} <b>Grade {grade} — {qg['label']}</b>  {' · '.join(badges)}"


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

    # Priority 1: MTF gate — most common reason a high-confidence pair is blocked
    mtf = result.get("bundle", {}).get("mtf", {})
    if mtf and not mtf.get("qualifies", True):
        cnt = mtf.get("agreeing_count", 0)
        bkd = mtf.get("breakdown", "")
        bkd_str = f" [{bkd}]" if bkd and bkd != "UNAVAILABLE" else ""
        return f"Conf {conf}/10 {direction} — MTF: weekly+daily must both agree (got {cnt}/3 core TFs){bkd_str}"

    # Priority 2: R:R below minimum threshold
    try:
        from src import threshold_manager as _tm_rr
        rr_min = _tm_rr.get_min_rr()
        rr_actual = float(parsed.get("reward_risk") or 0)
        if rr_actual > 0 and rr_actual < rr_min:
            return f"Conf {conf}/10 {direction} — R:R {rr_actual:.2f}:1 below {rr_min:.1f}:1 minimum"
    except Exception:
        pass

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
        parts.append("all layers strong — analyst judged setup not clean enough to trade")

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
    else:
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

    # ── Risk of Ruin + Kelly Criterion ─────────────────────────────────────────
    try:
        from src import risk_manager as _rm_ror
        from src import risk_of_ruin as _ror
        _ror_profile    = _rm_ror.load_profile()
        _ror_state      = _rm_ror.compute_risk_state(_ror_profile)
        _current_risk_f = _rm_ror.MODE_RISK[_ror_state["risk_mode"]] / 100.0
        lines += _ror.build_ror_section(_current_risk_f)
    except Exception as _ror_exc:
        lines.append(f"⚠️ Risk analysis unavailable ({_ror_exc})")

    return lines


def _build_research_section(research_result=None) -> list:
    """Build the RESEARCH TRADES Telegram section from data/research_trades.csv.

    Three display modes:
      • 30-day analysis complete → recommendation line
      • Closed trades exist      → full stats breakdown
      • No closed trades yet     → collecting-data summary
    Returns an empty list when no research trades exist at all.
    """
    try:
        from src import research_tracker as _rtrk
        rows = _rtrk.load()
    except Exception:
        return []
    if not rows:
        return []

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    open_rows = [r for r in rows if r.get("status") in ("OPEN", "NO_PRICE_LEVELS")]
    closed    = [r for r in rows if r.get("status") in ("WIN", "LOSS", "BREAKEVEN", "EXPIRED")]
    wins      = [r for r in closed if r.get("status") == "WIN"]
    losses    = [r for r in closed if r.get("status") == "LOSS"]

    # Days elapsed from oldest trade date
    days_elapsed = 0
    try:
        dates = [r.get("date", "") for r in rows if r.get("date")]
        if dates:
            days_elapsed = (_auckland_now().replace(tzinfo=None) -
                            datetime.strptime(min(dates), "%Y-%m-%d")).days
    except Exception:
        pass
    days_remaining = max(0, 30 - days_elapsed)

    sec = ["", "━━━━━━━━━━━━━━━━━━━━━"]

    # ── Mode 1: 30-day analysis has fired ─────────────────────────────────────
    if research_result and research_result.get("recommendation") not in (None, "INSUFFICIENT_DATA"):
        rec = research_result.get("recommendation", "")
        c6  = (research_result.get("band_results") or {}).get("6") or {}
        wr6 = f"{c6.get('win_rate', 0) * 100:.0f}%" if c6 else "?"
        sec.append("🔬 <b>RESEARCH TRADES — ANALYSIS COMPLETE</b>")
        if rec == "LOWER_TO_6":
            sec.append(
                f"Recommendation: LOWER_TO_6 — conf 6 setups profitable at {wr6} win rate"
            )
        elif rec == "KEEP_AT_7":
            sec.append(
                f"Recommendation: KEEP_AT_7 — conf 6 setups only {wr6} win rate, insufficient edge"
            )
        else:
            sec.append(f"Recommendation: {rec}")
            rsn = research_result.get("reasoning", "")
            if rsn:
                sec.append(f"<i>{rsn}</i>")
        return sec

    n_open   = len(open_rows)
    n_closed = len(closed)
    n_total  = len(rows)
    sec.append("🔬 <b>RESEARCH TRADES</b>")

    # ── Mode 2: No closed trades yet ──────────────────────────────────────────
    if not closed:
        sec.append(
            f"<b>{n_open}</b> open trades tracking | <b>0</b> closed trades"
        )
        if days_remaining > 0:
            sec.append(f"Collecting data — first analysis in {days_remaining} days")
        else:
            sec.append("Collecting data — analysis pending (need 10+ closed trades)")
        pair_freq: dict = {}
        for r in rows:
            p = r.get("pair", "")
            if p:
                pair_freq[p] = pair_freq.get(p, 0) + 1
        top = sorted(pair_freq, key=lambda x: pair_freq[x], reverse=True)[:4]
        if top:
            sec.append(f"Most active pairs: {', '.join(top)}")
        return sec

    # ── Mode 3: Full breakdown ─────────────────────────────────────────────────
    n_wins   = len(wins)
    n_losses = len(losses)
    decisive = n_wins + n_losses
    wr_pct   = int(n_wins / decisive * 100) if decisive else 0

    sec.append(
        f"Open: <b>{n_open}</b> trades | Closed: <b>{n_closed}</b> trades | Total: <b>{n_total}</b>"
    )
    sec.append("")
    sec.append(
        f"Win rate: <b>{wr_pct}%</b> ({n_wins}W / {n_losses}L) — {decisive} closed trades"
    )

    win_pips  = [_f(r.get("pips")) for r in wins   if _f(r.get("pips")) is not None]
    loss_pips = [_f(r.get("pips")) for r in losses if _f(r.get("pips")) is not None]
    if win_pips and loss_pips:
        sec.append(
            f"Avg win: <b>+{sum(win_pips)/len(win_pips):.0f} pips</b> | "
            f"Avg loss: <b>{sum(loss_pips)/len(loss_pips):.0f} pips</b>"
        )
    elif win_pips:
        sec.append(f"Avg win: <b>+{sum(win_pips)/len(win_pips):.0f} pips</b>")
    elif loss_pips:
        sec.append(f"Avg loss: <b>{sum(loss_pips)/len(loss_pips):.0f} pips</b>")

    all_with_pips = [(r, _f(r.get("pips"))) for r in closed if _f(r.get("pips")) is not None]
    if all_with_pips:
        best  = max(all_with_pips, key=lambda x: x[1])
        worst = min(all_with_pips, key=lambda x: x[1])
        sec.append(
            f"Best trade: {best[0].get('pair')} {best[0].get('direction')} {best[1]:+.0f} pips"
        )
        if worst[0] is not best[0]:
            sec.append(
                f"Worst trade: {worst[0].get('pair')} {worst[0].get('direction')} {worst[1]:+.0f} pips"
            )

    if win_pips and loss_pips:
        total_loss = sum(abs(p) for p in loss_pips if p < 0)
        if total_loss > 0:
            sec.append(f"Profit factor: <b>{sum(win_pips) / total_loss:.2f}</b>")

    # Confidence breakdown (bands 5, 6, 7)
    sec.append("")
    sec.append("📊 <b>Confidence breakdown:</b>")
    for cv in (5, 6, 7):
        band = [r for r in closed if str(r.get("confidence", "")).strip() == str(cv)]
        bw   = sum(1 for r in band if r.get("status") == "WIN")
        bl   = sum(1 for r in band if r.get("status") == "LOSS")
        bd   = bw + bl
        if bd:
            sec.append(f"{cv}/10 setups: {bw}W {bl}L — {int(bw/bd*100)}% win rate")
        elif band:
            sec.append(f"{cv}/10 setups: {len(band)} trades — no decisive outcomes yet")
        else:
            sec.append(f"{cv}/10 setups: 0W 0L — no data yet")

    # Most promising pairs by win rate (min 1 closed trade)
    pair_stats: dict = {}
    for r in closed:
        p  = r.get("pair", "?")
        ps = pair_stats.setdefault(p, {"wins": 0, "total": 0})
        ps["total"] += 1
        if r.get("status") == "WIN":
            ps["wins"] += 1
    sorted_pairs = sorted(
        pair_stats.items(),
        key=lambda x: (x[1]["wins"] / max(x[1]["total"], 1), x[1]["total"]),
        reverse=True,
    )
    sec.append("")
    sec.append("🔬 <b>Most promising pairs so far:</b>")
    for pair, ps in sorted_pairs[:4]:
        pwr = int(ps["wins"] / ps["total"] * 100) if ps["total"] else 0
        sec.append(f"{pair} — {ps['wins']} wins from {ps['total']} trades ({pwr}%)")

    # Days until / since threshold analysis
    sec.append("")
    if days_remaining > 0:
        sec.append(
            f"⏳ <b>Days until threshold analysis: {days_remaining} days remaining</b>"
        )
        sec.append(
            "(System will recommend whether to lower threshold to 6 after 30 days of data)"
        )
    else:
        sec.append("⏳ <b>Threshold analysis: 30+ days of data — awaiting 10+ closed trades</b>")

    return sec


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

def _compute_expiry_days_from_rr(rr: float) -> int:
    """Calibrate trade expiry from R:R ratio.

    Stop ≈ 1x ATR ≈ average daily range (ADR), so R:R ≈ target_pips / adr_pips.
    Formula: max(4, round(rr * 1.5) + 1)
    Examples: R:R 2.3 → 4 days; R:R 3.1 → 6 days.
    """
    try:
        return max(4, round(float(rr) * 1.5) + 1)
    except (TypeError, ValueError):
        return 5


def _calc_indicative_levels(pair: str, parsed: dict, bundle: dict) -> tuple:
    """Return (entry, stop, target, meta) for indicative display.

    Stop = 1.0x ATR(14), rounded to nearest 5 pips.
    Target = nearest Fibonacci level in [1.5x, 2.5x] ATR range, else 2.0x ATR.
    meta dict: {atr, stop_atr_mult, target_atr_mult, expiry_days, quality_flag}

    Uses Claude's parsed entry/stop/target when present.  When stop or target
    are missing, computes them from ATR.  Returns (None, None, None, {}) if
    price is completely unavailable.
    """
    _empty_meta: dict = {
        "atr": None, "stop_atr_mult": None, "target_atr_mult": None,
        "expiry_days": 5, "quality_flag": "",
    }

    try:
        entry  = float(parsed.get("entry")     or 0) or None
        stop   = float(parsed.get("stop_loss") or 0) or None
        target = float(parsed.get("target")    or 0) or None
    except (TypeError, ValueError):
        entry, stop, target = None, None, None

    # Pull actual ATR14 and current price from bundle
    atr_actual = None
    cur        = None
    try:
        _daily = bundle.get("technical", {}).get("daily", {})
        if isinstance(_daily, dict):
            atr_actual = float(_daily.get("atr14") or 0) or None
            cur        = float(_daily.get("last_close") or _daily.get("close") or 0) or None
    except (TypeError, ValueError):
        pass

    # If all 3 Claude values present, compute meta only
    if entry and stop and target:
        meta = dict(_empty_meta)
        if atr_actual and atr_actual > 0:
            try:
                sd = abs(entry - stop)
                td = abs(entry - target)
                meta["atr"]             = atr_actual
                meta["stop_atr_mult"]   = round(sd / atr_actual, 1)
                meta["target_atr_mult"] = round(td / atr_actual, 1)
                rr = td / sd if sd > 0 else 0
                meta["expiry_days"]  = _compute_expiry_days_from_rr(rr)
                meta["quality_flag"] = "LOW QUALITY SETUP" if rr < 1.5 else ""
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        else:
            try:
                sd = abs(entry - stop)
                td = abs(entry - target)
                rr = td / sd if sd > 0 else 0
                meta["expiry_days"]  = _compute_expiry_days_from_rr(rr)
                meta["quality_flag"] = "LOW QUALITY SETUP" if rr < 1.5 else ""
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        return entry, stop, target, meta

    if cur is None:
        return entry, stop, target, _empty_meta

    # --- ATR-based fallback ---
    quote_ccy = pair.split("/")[-1].upper() if "/" in pair else pair[-3:].upper()
    is_jpy    = quote_ccy == "JPY"
    pip_size  = 0.01 if is_jpy else 0.0001
    dec       = 3 if is_jpy else 5

    # Use actual ATR14 if available, else estimate from pair characteristics
    if atr_actual and atr_actual > 0:
        atr = atr_actual
    elif is_jpy:
        atr = 0.50
    elif cur < 0.10:
        atr = cur * 0.008
    elif any(c in pair.upper() for c in ("EUR", "GBP")):
        atr = 0.0080
    else:
        atr = 0.0050

    dirn = (parsed.get("direction") or "").upper()
    if dirn not in ("BUY", "SELL"):
        return entry, stop, target, _empty_meta

    entry = entry or cur

    # Stop: 1.0x ATR rounded to nearest 5 pips
    if not stop:
        atr_pips   = atr / pip_size
        stop_pips  = round(atr_pips / 5) * 5   # nearest 5 pips
        stop_dist  = stop_pips * pip_size
        stop = round(entry - stop_dist if dirn == "BUY" else entry + stop_dist, dec)

    # Target: nearest Fibonacci level in [1.5x, 2.5x] ATR range, else 2.0x ATR
    if not target:
        min_dist = 1.5 * atr
        max_dist = 2.5 * atr
        fib_target = None
        try:
            _fib_d = bundle.get("technical", {}).get("daily", {})
            if isinstance(_fib_d, dict):
                _fib = _fib_d.get("fibonacci", {})
                if isinstance(_fib, dict) and _fib.get("status") == "ok":
                    _fib_key    = "nearest_above" if dirn == "BUY" else "nearest_below"
                    _fib_levels = _fib.get(_fib_key, [])
                    for _lbl, _px in _fib_levels:
                        _px = float(_px)
                        dist = _px - entry if dirn == "BUY" else entry - _px
                        if min_dist <= dist <= max_dist:
                            fib_target = _px
                            break
        except (TypeError, ValueError, IndexError):
            pass
        if fib_target is not None:
            target = round(fib_target, dec)
        else:
            target = round(entry + atr * 2.0 if dirn == "BUY" else entry - atr * 2.0, dec)

    # Reject impossible levels
    if entry and stop is not None and target is not None:
        if stop <= 0 or target <= 0:
            return None, None, None, _empty_meta
        if entry > 0 and (abs(entry - stop) > entry * 0.5 or abs(entry - target) > entry * 0.5):
            return None, None, None, _empty_meta

    # Compute meta
    meta = dict(_empty_meta)
    try:
        sd = abs(entry - stop)
        td = abs(entry - target)
        rr = td / sd if sd > 0 else 0
        meta["atr"]             = atr
        meta["stop_atr_mult"]   = round(sd / atr, 1) if atr > 0 else None
        meta["target_atr_mult"] = round(td / atr, 1) if atr > 0 else None
        meta["expiry_days"]     = _compute_expiry_days_from_rr(rr)
        meta["quality_flag"]    = "LOW QUALITY SETUP" if rr < 1.5 else ""
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    return entry, stop, target, meta


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

    # Load risk profile once for investment detail calculations
    _rm_profile: dict = {}
    _rm_state: dict = {}
    try:
        from src import risk_manager as _rm_inv
        _rm_profile = _rm_inv.load_profile()
        _rm_state   = _rm_inv.compute_risk_state(_rm_profile)
    except Exception:
        pass

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

        # Age / expiry — calibrated from R:R (stop ≈ 1x ATR ≈ ADR)
        days_open     = 0
        days_open_str = "?"
        expires_str   = "?"
        remaining     = 5
        try:
            _ot_entry  = float(row.get("entry")     or 0) or None
            _ot_stop   = float(row.get("stop_loss") or 0) or None
            _ot_target = float(row.get("target")    or 0) or None
            _ot_expiry = 5
            if _ot_entry and _ot_stop and _ot_target:
                _sd = abs(_ot_entry - _ot_stop)
                _td = abs(_ot_entry - _ot_target)
                _rr = _td / _sd if _sd > 0 else 0
                _ot_expiry = _compute_expiry_days_from_rr(_rr)
            opened_dt = datetime.strptime(row.get("timestamp", "")[:10], "%Y-%m-%d")
            days_open = (now_ak.replace(tzinfo=None) - opened_dt).days
            days_open_str = f"{days_open} day{'s' if days_open != 1 else ''}"
            remaining     = max(0, _ot_expiry - days_open)
            expires_str   = f"{remaining} day{'s' if remaining != 1 else ''}"
        except Exception:
            pass

        # Next key monitoring time
        _ew_t  = _entry_window_for_pair(pair)
        _eq_e, _ = _entry_quality(pair, now_ak)
        _sess_active, _sess_close = _session_status_for_pair(pair, now_ak)
        if _sess_active:
            _check_line = (
                f"⏰ <b>{_ew_t[6]} currently active — closes {_sess_close} Auckland</b>"
            )
        else:
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

            # Investment details — position size, market exposure, risk amount
            if stop and _rm_profile and _rm_state:
                try:
                    from src import risk_manager as _rm_inv
                    _sz = _rm_inv.size_trade(
                        pair=pair, direction=dirn, entry=entry, stop=stop,
                        target=target or entry,
                        confidence=int(float(row.get("confidence") or 8)),
                        profile=_rm_profile, risk_state=_rm_state,
                    )
                    # Scale to paper fund ($10k) rather than real account
                    _paper_scale = _rm_inv.FUND_START / max(config.ACCOUNT_BALANCE, 1)
                    _lots   = max(round(_sz["lots"] * _paper_scale, 2), 0.01)
                    _risk_a = round(_sz["risk_amount"] * _paper_scale, 2)
                    _risk_p = _sz["risk_pct"]
                    _cl     = pair.upper().replace("/", "")
                    _base_c = _cl[:3]
                    if _base_c == "USD":
                        _mkt_exp = _lots * 100_000
                    else:
                        _mkt_exp = _lots * 100_000 * entry
                    sec.append(
                        f"💵 Invested: {_lots:.2f} lots — "
                        f"${_mkt_exp:,.0f} market exposure — "
                        f"${_risk_a:.0f} at risk ({_risk_p:.1f}% of account)"
                    )
                except Exception:
                    pass

            pip_sz    = _pip_size(pair)
            raw       = (cur - entry) if dirn == "BUY" else (entry - cur)
            pips      = raw / pip_sz
            net_pips  = pips
            _cost_str = ""
            try:
                from src import trade_costs as _tc_ot
                _ot_costs = _tc_ot.compute_costs(pair, dirn, entry, float(days_open))
                _tot_cost = _ot_costs["total_cost_pips"]
                net_pips  = pips - _tot_cost
                _sp  = _ot_costs["spread"]
                _sl  = _ot_costs["entry_slip"]
                _cm  = _ot_costs["commission"]
                _sw  = _ot_costs["swap_total"]
                _parts = [
                    f"{_sp:.1f}p spread",
                    f"{_sl:.1f}p slip×2",
                    f"{_cm:.2f}p comm",
                ]
                if abs(_sw) >= 0.05:
                    _parts.append(f"{_sw:+.1f}p swap")
                _cost_str = f"💸 Costs: {' · '.join(_parts)} = {_tot_cost:.1f}p total"
            except Exception:
                pass
            if pips > 2:
                arrow   = "📈"
                pnl_str = (
                    f"+{pips:.0f} pips gross | {net_pips:+.0f} pips net "
                    f"(+${abs(net_pips):.0f}) — moving in your favour"
                )
            elif pips < -2:
                arrow   = "📉"
                pnl_str = (
                    f"{pips:.0f} pips gross | {net_pips:.0f} pips net "
                    f"(-${abs(net_pips):.0f}) — moving against you"
                )
            else:
                arrow   = "➖"
                pnl_str = f"{pips:+.0f} pips gross — at breakeven"
            sec.append(f"{arrow} {pnl_str}")
            if _cost_str:
                sec.append(_cost_str)

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
                    elif pips > 5:
                        sec.append("📈 Slight gain — price moving in the right direction")
                    elif abs(pips) <= 5:
                        sec.append("⚠️ <b>Trade at breakeven — monitor closely</b>")
                        sec.append("Price needs to move away from entry")
                    else:
                        sec.append(f"🔶 Slightly underwater — {abs(pips):.0f} pips from entry, stop is {abs(pips_to_stop):.0f} pips away — still safe")
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
            elif stop:
                sec.append(f"🛑 Stop: {_fmt_price(stop)}")
            if stop and _rm_profile and _rm_state:
                try:
                    from src import risk_manager as _rm_inv
                    _sz = _rm_inv.size_trade(
                        pair=pair, direction=dirn, entry=entry, stop=stop,
                        target=target or entry,
                        confidence=int(float(row.get("confidence") or 8)),
                        profile=_rm_profile, risk_state=_rm_state,
                    )
                    # Scale to paper fund ($10k) rather than real account
                    _paper_scale = _rm_inv.FUND_START / max(config.ACCOUNT_BALANCE, 1)
                    _lots   = max(round(_sz["lots"] * _paper_scale, 2), 0.01)
                    _risk_a = round(_sz["risk_amount"] * _paper_scale, 2)
                    _risk_p = _sz["risk_pct"]
                    _cl     = pair.upper().replace("/", "")
                    _base_c = _cl[:3]
                    if _base_c == "USD":
                        _mkt_exp = _lots * 100_000
                    else:
                        _mkt_exp = _lots * 100_000 * entry
                    sec.append(
                        f"💵 Invested: {_lots:.2f} lots — "
                        f"${_mkt_exp:,.0f} market exposure — "
                        f"${_risk_a:.0f} at risk ({_risk_p:.1f}% of account)"
                    )
                except Exception:
                    pass
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

    # Load open trades first — needed to filter already-open pairs from signals
    _ot_open_trades: list = []
    try:
        from src import tracker as _trk_ot
        _ot_open_trades = [r for r in _trk_ot.load() if r.get("status") == "OPEN"]
    except Exception:
        pass
    _open_pair_set = {r.get("pair", "").upper() for r in _ot_open_trades}

    def _is_inverse(p1: str, p2: str) -> bool:
        """True if p1 and p2 represent the same instrument quoted the other way (e.g. USD/CAD vs CAD/USD)."""
        c1 = p1.upper().replace("/", "").replace("-", "")
        c2 = p2.upper().replace("/", "").replace("-", "")
        return len(c1) == 6 and len(c2) == 6 and c1 == c2[3:] + c2[:3]

    # Pairs the analyst said YES but whose effective confidence (after MA ribbon
    # penalty) falls below the live-trade threshold are demoted to watch list.
    try:
        from src import threshold_manager as _tm_eff
        _trade_conf_thr = _tm_eff.get_confidence_threshold()
    except Exception:
        _trade_conf_thr = 7

    _demoted_pairs = {
        r["pair"] for r in deep_results
        if r["parsed"].get("trade_this") == "YES"
        and _eff_conf(r) < _trade_conf_thr
    }

    # Grade all results — used by display helpers and filtering below
    _quality_grades: dict = {r["pair"]: _trade_quality_grade(r) for r in deep_results}

    # Candidate YES trades (passes MTF gate + effective confidence threshold)
    _yes_raw = [
        r for r in deep_results
        if r["parsed"].get("trade_this") == "YES"
        and r["pair"] not in _demoted_pairs
    ]
    # Only A and B get full trade alerts; C → watch list; D/F → near misses
    yes_trades    = [r for r in _yes_raw if _quality_grades.get(r["pair"], {}).get("grade") in ("A", "B")]
    _c_grade_yes  = [r for r in _yes_raw if _quality_grades.get(r["pair"], {}).get("grade") == "C"]
    _df_grade_yes = [r for r in _yes_raw if _quality_grades.get(r["pair"], {}).get("grade") in ("D", "F")]

    watch_list = sorted(
        [r for r in deep_results
         if (r["parsed"].get("trade_this") != "YES" or r["pair"] in _demoted_pairs)
         and 5 <= _eff_conf(r) <= 6
        ] + _c_grade_yes,
        key=_eff_conf, reverse=True,
    )[:4]   # allow one extra slot for demoted C-grade alerts
    near_misses = sorted(
        [r for r in deep_results
         if r["parsed"].get("trade_this") != "YES" or r["pair"] in _demoted_pairs
        ] + _df_grade_yes,
        key=_eff_conf, reverse=True,
    )
    upcoming = sorted(
        [r for r in near_misses
         if 3 <= _eff_conf(r) <= 4
         and r["pair"].upper() not in _open_pair_set
         and not any(_is_inverse(r["pair"], op) for op in _open_pair_set)
        ],
        key=_eff_conf, reverse=True,
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
        "morning":   "🌏 9AM MORNING CHECK",
        "prelondon": "🌆 3PM PRE-LONDON CHECK",
        "london":    "🌃 5PM LONDON OPEN CHECK",
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

        # ATR display for trade block
        _tb_daily = r.get("bundle", {}).get("technical", {}).get("daily", {})
        _tb_atr14 = float(_tb_daily.get("atr14") or 0) if isinstance(_tb_daily, dict) else 0
        _tb_pip   = _pip_size(pair)
        _tb_atr_line = None
        if _tb_atr14 > 0:
            try:
                _stop_d   = abs(float(entry_raw) - float(adj_stop))
                _tgt_d    = abs(float(adj_tgt)   - float(entry_raw))
                _stop_m   = _stop_d / _tb_atr14
                _tgt_m    = _tgt_d  / _tb_atr14
                _atr_pips = round(_tb_atr14  / _tb_pip)
                _stop_p   = round(_stop_d    / _tb_pip)
                _tgt_p    = round(_tgt_d     / _tb_pip)
                _tb_atr_line = (
                    f"📏 Stop {_stop_m:.1f}x ATR ({_stop_p}p) · "
                    f"Target {_tgt_m:.1f}x ATR ({_tgt_p}p) · "
                    f"ATR={_atr_pips}p"
                )
            except (TypeError, ValueError, ZeroDivisionError):
                pass

        # R:R < 1.5 → LOW QUALITY flag; reduce displayed confidence by 1
        _low_quality = rr_num is not None and rr_num < 1.5
        _conf_display = conf
        if _low_quality:
            try:
                _conf_display = str(max(1, int(conf) - 1))
            except (TypeError, ValueError):
                pass

        _qg_tb = _quality_grades.get(pair, _trade_quality_grade(r))
        block = [
            "",
            f"{action_icon} <b>{_pfx}ACTION: {direction} {pair} NOW</b>",
            "━━━━━━━━━━━━━━━━━━━━━",
            _grade_display_line(_qg_tb),
            "━━━━━━━━━━━━━━━━━━━━━",
            f"💰 Entry: {_fmt_price(entry_raw)}",
            f"🛑 Stop Loss: {_fmt_price(adj_stop)}  ({pip_risk} risk)",
            f"🎯 Take Profit: {_fmt_price(adj_tgt)}  ({pip_target} target)",
        ]
        if _tb_atr_line:
            block.append(_tb_atr_line)
        if _low_quality:
            block.append(f"⚠️ <b>LOW QUALITY SETUP — R:R {rr_str} below 1.5:1 minimum (confidence −1)</b>")
        if profit_amt and risk_amt:
            block.append(f"📊 Risk ${risk_amt:,.0f} → Make ${profit_amt:,.0f}  ({rr_str} reward)")
        elif risk_amt:
            block.append(f"📊 Risk ${risk_amt:,.0f} {cur}  ({pct:.2f}% account)")
        if sz.get("lots"):
            block.append(f"📏 Position Size: {sz['lots']} lots")

        # ── Cost viability ────────────────────────────────────────────────────
        try:
            from src import trade_costs as _tc_tb
            _viab_tb = _tc_tb.check_viability(pair, direction, entry_raw, adj_stop, adj_tgt)
            if not _viab_tb["viable"]:
                block.append(
                    f"🚫 <b>NOT VIABLE AFTER COSTS — net R:R {_viab_tb['net_rr']:.1f}:1</b> "
                    f"(gross {_viab_tb['gross_rr']:.1f}:1 minus "
                    f"{_viab_tb['total_cost_pips']:.1f}p costs)"
                )
            else:
                block.append(
                    f"✅ Net R:R {_viab_tb['net_rr']:.1f}:1 after "
                    f"{_viab_tb['total_cost_pips']:.1f}p costs "
                    f"(gross {_viab_tb['gross_rr']:.1f}:1 · "
                    f"{_viab_tb['gross_pips']:.0f}p target)"
                )
        except Exception:
            pass

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
            f"📈 Confidence: {_conf_display}/10  {_conf_bar(_conf_display)}",
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
            _mtf_txt_tb = _mtf_plain_english(_mtf, trade_direction=direction)
            if _mtf_txt_tb:
                block.append(f"🕐 {_mtf_txt_tb}")
        _rib_line = _ribbon_display(r.get("bundle", {}))
        if _rib_line:
            block.append(_rib_line)
            _rib_status = (r.get("bundle", {}).get("technical", {}).get("daily", {}) or {}).get("ribbon", {}).get("status", "")
            _rib_bull = _rib_status in ("ALIGNED_BULL", "LEANING_BULL")
            _rib_bear = _rib_status in ("ALIGNED_BEAR", "LEANING_BEAR")
            if (_rib_bull and direction == "SELL") or (_rib_bear and direction == "BUY"):
                block.append("⚠️ <b>MA Ribbon conflict — ribbon aligned against trade direction, higher risk</b>")
                try:
                    _conf_adj = int(conf) - 1
                    if _rib_status in ("ALIGNED_BULL", "ALIGNED_BEAR"):
                        block.append(f"Adjusted confidence: {_conf_adj}/10 (−1 for strong ribbon conflict)")
                except (TypeError, ValueError):
                    pass
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
        conf  = _eff_conf(rr)   # ribbon-adjusted confidence — consistent with threshold checks
        dirn  = (pp.get("direction") or "—").upper()
        arrow = "📈" if dirn == "BUY" else "📉"
        ntc   = _what_needs_to_change(pp)
        # Compute entry quality badge first so it appears in header
        _ew_we    = _entry_window_for_pair(rr["pair"])
        _eq_we_e, _eq_we_l = _entry_quality(rr["pair"], now_ak)
        _tref_we  = _time_ref_for_entry(_ew_we[0], _ew_we[1], now_ak)
        _start_we = _fmt_time_exact(_ew_we[0], _ew_we[1])
        _qg_we = _quality_grades.get(rr["pair"], _trade_quality_grade(rr))
        lines = [
            "",
            f"{arrow} <b>{rr['pair']}</b> {dirn}  {conf}/10 {_conf_bar(conf)}  {_eq_we_e} {_eq_we_l}",
            _grade_display_line(_qg_we),
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
            _mtf_txt = _mtf_plain_english(_mtf_wl, trade_direction=dirn)
            if _mtf_txt:
                lines.append(_mtf_txt)
        _rib_wl = _ribbon_display(rr.get("bundle", {}))
        if _rib_wl:
            lines.append(_rib_wl)
            _rib_wl_status = (rr.get("bundle", {}).get("technical", {}).get("daily", {}) or {}).get("ribbon", {}).get("status", "")
            _rib_wl_bull = _rib_wl_status in ("ALIGNED_BULL", "LEANING_BULL")
            _rib_wl_bear = _rib_wl_status in ("ALIGNED_BEAR", "LEANING_BEAR")
            if (_rib_wl_bull and dirn == "SELL") or (_rib_wl_bear and dirn == "BUY"):
                lines.append("⚠️ <b>MA Ribbon conflict — confidence penalised −1, higher risk</b>")
        # D/F grade: monitoring only — suppress all entry instructions
        if _qg_we["grade"] in ("D", "F"):
            if _qg_we["grade"] == "F":
                lines.append("❌ Grade F — conditions not suitable for trading — monitoring only")
            else:
                lines.append("⚠️ Grade D — avoid — conditions not suitable for entry")
            return lines
        # Indicative entry/stop/target — always shown so investor knows the trade shape
        ind_e, ind_s, ind_t, ind_meta = _calc_indicative_levels(rr["pair"], pp, rr.get("bundle", {}))
        if ind_e and ind_s and ind_t:
            is_jpy = "JPY" in rr["pair"].upper()
            dec = 3 if is_jpy else 5
            try:
                rr_ratio   = abs(float(ind_t) - float(ind_e)) / abs(float(ind_e) - float(ind_s))
                rr_ratio   = max(rr_ratio, 1.3)
                risk_pips  = abs(float(ind_e) - float(ind_s)) / _pip_size(rr["pair"])
                profit_pips= abs(float(ind_t) - float(ind_e)) / _pip_size(rr["pair"])
                risk_usd   = max(1, round(risk_pips))
                profit_usd = max(1, round(profit_pips))
                lines.append("🟡 <b>READY TO TRADE IF CONFIRMED:</b>")
                if ind_meta.get("quality_flag"):
                    lines.append(f"⚠️ <b>{ind_meta['quality_flag']}</b> — R:R {rr_ratio:.1f}:1 below 1.5 minimum")
                lines.append(
                    f"Entry ~{ind_e:.{dec}f} | "
                    f"Stop ~{ind_s:.{dec}f} | Target ~{ind_t:.{dec}f}"
                )
                lines.append(f"Risk ${risk_usd} → Make ${profit_usd} ({rr_ratio:.1f}:1)")
                # Net R:R after costs
                try:
                    from src import trade_costs as _tc_we
                    _viab_we = _tc_we.check_viability(rr["pair"], dirn, ind_e, ind_s, ind_t)
                    if not _viab_we["viable"]:
                        lines.append(
                            f"🚫 <b>NOT VIABLE AFTER COSTS — net R:R {_viab_we['net_rr']:.1f}:1</b> "
                            f"(gross {rr_ratio:.1f}:1 minus {_viab_we['total_cost_pips']:.1f}p costs)"
                        )
                    elif _viab_we.get("net_rr", 0) > 0:
                        lines.append(
                            f"✅ Net R:R {_viab_we['net_rr']:.1f}:1 after "
                            f"{_viab_we['total_cost_pips']:.1f}p costs"
                        )
                except Exception:
                    pass
                # ATR multiples display
                _sm = ind_meta.get("stop_atr_mult")
                _tm = ind_meta.get("target_atr_mult")
                _atr_val = ind_meta.get("atr")
                if _sm is not None and _tm is not None and _atr_val:
                    _atr_pip = round(_atr_val / _pip_size(rr["pair"]))
                    lines.append(
                        f"📏 Stop {_sm}x ATR ({round(risk_pips):.0f}p) · "
                        f"Target {_tm}x ATR ({round(profit_pips):.0f}p) · "
                        f"ATR={_atr_pip}p"
                    )
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        _sess_we_active, _sess_we_close = _session_status_for_pair(rr["pair"], now_ak)
        if _sess_we_active:
            lines += [
                f"{_eq_we_e} <b>{_ew_we[6]} currently active — closes {_sess_we_close} Auckland</b>",
                f"If confidence reaches 7+ now — enter immediately at market price",
                f"If confidence has not reached 7+ by {_ew_we[5]} — skip this pair today",
                f"Needs: {ntc}",
            ]
        else:
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
        conf = _eff_conf(rr)   # ribbon-adjusted confidence
        dirn = (pp.get("direction") or "—").upper()
        _qg_ap = _quality_grades.get(rr["pair"], _trade_quality_grade(rr))
        lines = [
            "",
            f"<b>{rr['pair']}</b> {conf}/10 {dirn} — if conditions improve:",
            _grade_display_line(_qg_ap),
        ]
        ind_e, ind_s, ind_t, ind_meta = _calc_indicative_levels(rr["pair"], pp, rr.get("bundle", {}))
        is_jpy = "JPY" in rr["pair"].upper()
        dec = 3 if is_jpy else 5
        if _qg_ap["grade"] in ("D", "F"):
            if _qg_ap["grade"] == "F":
                lines.append("❌ Grade F — conditions not suitable for trading — monitoring only")
            else:
                lines.append("⚠️ Grade D — avoid — not suitable for entry at this time")
            return lines
        lines.append("🟠 <b>POTENTIAL SETUP IF CONDITIONS IMPROVE:</b>")
        if ind_e and ind_s and ind_t:
            try:
                lines.append(
                    f"Entry ~{ind_e:.{dec}f} | "
                    f"Stop ~{ind_s:.{dec}f} | Target ~{ind_t:.{dec}f}"
                )
                _sm_ap = ind_meta.get("stop_atr_mult")
                _tm_ap = ind_meta.get("target_atr_mult")
                _atr_ap = ind_meta.get("atr")
                if _sm_ap is not None and _tm_ap is not None and _atr_ap:
                    _pip_ap = _pip_size(rr["pair"])
                    lines.append(
                        f"📏 Stop {_sm_ap}x ATR · Target {_tm_ap}x ATR · "
                        f"ATR={round(_atr_ap / _pip_ap)}p"
                    )
                # Net R:R viability
                try:
                    from src import trade_costs as _tc_ap
                    _viab_ap = _tc_ap.check_viability(rr["pair"], dirn, ind_e, ind_s, ind_t)
                    if not _viab_ap["viable"]:
                        lines.append(
                            f"🚫 NOT VIABLE AFTER COSTS — net R:R {_viab_ap['net_rr']:.1f}:1 "
                            f"(gross {_viab_ap['gross_rr']:.1f}:1 minus "
                            f"{_viab_ap['total_cost_pips']:.1f}p costs)"
                        )
                    elif _viab_ap.get("net_rr", 0) > 0:
                        lines.append(
                            f"Net R:R {_viab_ap['net_rr']:.1f}:1 after "
                            f"{_viab_ap['total_cost_pips']:.1f}p costs"
                        )
                except Exception:
                    pass
            except (TypeError, ValueError):
                lines.append("Price levels: insufficient data for this pair")
        else:
            lines.append("Price levels: insufficient data for this pair")
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
        _patience = _compute_patience_score(ctx)
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
                _nw_trunc = nw if len(nw) <= 60 else nw[:60].rsplit(" ", 1)[0].rstrip(",;") + "…"
                nw_list.append(f"{r['pair']}: {_nw_trunc}")
        if nw_list:
            ctx_lines.append(f"⚡ {nw_list[0]}")
        if ctx.get("monthly_bias"):
            ctx_lines.append(f"📅 Monthly structural bias: <b>{ctx['monthly_bias']}</b> — background context only")
        _ps = _patience["score"]
        _pd = _patience["description"]
        ctx_lines.append(f"📊 <b>Today's trading conditions: {_ps}/10</b> — {_pd}")
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

        # RESEARCH TRADES
        _rt_sec = _build_research_section(research_result=research_result)
        if _rt_sec:
            all_sections.append(_rt_sec)

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
        if cost_lines:
            health_sec.extend(cost_lines)
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
    elif scan_mode in ("morning", "london", "prelondon"):
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
            and _eff_conf(r) >= 6
            and _morning_conf.get(r["pair"], 10) < 6
        ]
        if _newly_6plus:
            ns = ["", "━━━━━━━━━━━━━━━━━━━━━", "⬆️ <b>NEWLY REACHED 6+ SINCE 6AM</b>"]
            for r in _newly_6plus[:5]:
                pp    = r["parsed"]
                curr  = _eff_conf(r)           # show ribbon-adjusted confidence
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
            [r for r in deep_results if r["pair"] not in _yes_pairs and _eff_conf(r) >= 3],
            key=_eff_conf, reverse=True,
        )
        _watch_items       = [r for r in _all_candidates if _eff_conf(r) >= 5][:3]
        _approaching_items = [
            r for r in _all_candidates
            if _eff_conf(r) <= 4
            and r["pair"].upper() not in _open_pair_set
            and not any(_is_inverse(r["pair"], op) for op in _open_pair_set)
        ][:2]

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

        # RESEARCH TRADES
        _rt_sec_id = _build_research_section(research_result=research_result)
        if _rt_sec_id:
            all_sections.append(_rt_sec_id)

        if cost_lines:
            all_sections.append(
                ["", "━━━━━━━━━━━━━━━━━━━━━", "⚠️ <b>SYSTEM HEALTH</b>"] + cost_lines
            )

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
_COOLDOWN_SECS = 1200  # 20 minutes — blocks accidental double-fires, allows manual re-runs

_ALERTS_FILE         = config.DATA_DIR / "last_alerts.json"
_MORNING_RANKED_FILE = config.DATA_DIR / "morning_ranked.json"

# (label, session-currency filter set — empty = no filter)
_SCAN_MODES: dict = {
    "morning":   ("9am Morning Check",    set()),
    "london":    ("5pm London Check",     set()),
    "prelondon": ("3pm Pre-London Check", set()),
    "full":      ("6am Full Scan",        set()),
}

# All 4 scans select from the full universe by 8-factor merit score — no mode filtering
_SCAN_TOP_N   = 15   # pairs selected and analysed from the full universe
_TD_CACHE_MAX = 20   # pairs to pre-warm in Twelve Data cache

# Sonnet escalation threshold: 6 for 6am (higher quality), 7 for intraday (cheaper)
_SONNET_THRESH = {"full": 6, "morning": 7, "london": 7, "prelondon": 7}


def _get_scan_mode() -> str:
    """Return scan mode from SCAN_MODE env var or current Auckland hour."""
    import os as _os_
    mode = _os_.getenv("SCAN_MODE", "").lower().strip()
    if mode in _SCAN_MODES:
        return mode
    hour = _auckland_now().hour
    if hour in (5, 6):    # 5am–7am  → 6am full scan
        return "full"
    if hour in (8, 9):    # 8am–10am → 9am morning check
        return "morning"
    if hour in (14, 15):  # 2pm–4pm  → 3pm pre-London check
        return "prelondon"
    if hour in (16, 17):  # 4pm–6pm  → 5pm London open check
        return "london"
    # Off-hours fallback — log a warning so it is visible in GitHub Actions logs
    print(
        f"[scan] WARNING — Auckland hour={hour} is outside all defined scan windows "
        f"(5-6=full, 8-9=morning, 14-15=prelondon, 16-17=london) and SCAN_MODE env var is unset. "
        f"Defaulting to 'full'. Check GitHub Actions schedule and workflow trigger times.",
        file=sys.stderr,
    )
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
    if scan_mode == "morning" and is_weekday:
        return "⏰ Next scan today at 3pm Auckland time"
    if scan_mode == "prelondon" and is_weekday:
        return "⏰ Next scan today at 5pm Auckland time"
    if scan_mode == "london":
        return "⏰ Next scan tomorrow 6am Auckland — have a good evening."
    return f"⏰ Next full scan {nxt_short} at 6am Auckland time"


def run() -> int:
    # ── Auckland startup log — very first line, before all guards and checks ──
    _startup_ak = _auckland_now()
    print(
        f"[startup] Auckland: {_startup_ak.strftime('%A')} "
        f"{_startup_ak.day} {_startup_ak.strftime('%B %Y')} "
        f"{_startup_ak.strftime('%H:%M')} NZT "
        f"(weekday={_startup_ak.weekday()}, hour={_startup_ak.hour})",
        file=sys.stderr, flush=True,
    )
    _run_start = time.time()

    # ── Startup timezone diagnostics ──────────────────────────────────────────
    _now_utc = datetime.utcnow()
    print(
        f"[startup] UTC time:      {_now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        file=sys.stderr,
    )

    # ── Duplicate-run guard ────────────────────────────────────────────────────
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
    print(
        f"[scan] mode={scan_mode} ({_SCAN_MODES[scan_mode][0]}) "
        f"— detected from Auckland hour={_startup_ak.hour}",
        file=sys.stderr,
    )

    now_ak   = _auckland_now()
    date     = now_ak.strftime("%Y-%m-%d")
    log_path = config.REPORTS_DIR / f"daily_{date}.log"

    # Startup ping — only for the 6am full scan so Telegram delivery is confirmed before analysis.
    # Guard: if scan_mode resolved to 'full' but the clock is clearly outside the morning window
    # (e.g. 11pm), something triggered this run at the wrong time — skip the ping and warn instead.
    _FULL_SCAN_EXPECTED_HOURS = (5, 8)  # 5am–8am Auckland
    if scan_mode == "full":
        _exp_lo, _exp_hi = _FULL_SCAN_EXPECTED_HOURS
        if _exp_lo <= now_ak.hour <= _exp_hi:
            _time_str = now_ak.strftime("%I:%M%p").lstrip("0").lower()
            _telegram(
                f"⏱️ <b>6am full scan starting</b> — {_time_str} Auckland\n"
                f"Analysing up to 15 pairs. Summary to follow in ~20 min."
            )
        else:
            print(
                f"[startup-ping] WARNING — scan_mode='full' but Auckland hour={now_ak.hour} "
                f"is outside expected window {_exp_lo}h–{_exp_hi}h. "
                f"Skipping startup ping to avoid a confusing '6am full scan' message at {now_ak.hour}h. "
                f"Check cron-job.org trigger time and SCAN_MODE env var.",
                file=sys.stderr,
            )

    # Reset per-run state
    try:
        from src import analyst as _anl, technical as _tech
        _anl.reset_key_state()
        _tech.reset_call_count()
    except Exception:
        pass

    with log_path.open("a", encoding="utf-8") as logf:

        # 0a. Pre-fetch prices for all open trades before outcome checking.
        #     Uses /time_series?interval=1day&outputsize=2 — more reliable on
        #     the free tier than /price.  The resulting cache is passed to both
        #     outcome checkers so they skip per-trade API calls entirely, which
        #     means ALL open research trades can be priced regardless of whether
        #     they appeared in today's pair selection.
        _open_trade_prices: dict = {}
        try:
            from src import price_fetcher as _pf
            _open_trade_prices = _pf.fetch_prices_for_open_trades(
                log=lambda m: _log_line(logf, m)
            )
        except Exception as _pf_exc:
            _log_line(
                logf,
                f"Price pre-fetch failed ({_pf_exc}) — outcome checkers will use direct API.",
            )

        # 0. Automatic outcome detection
        closed_today = []
        new_patterns = []
        try:
            from src import outcome_checker, outcome_analyst
            closed_today = outcome_checker.check_open_trades(
                log=lambda m: _log_line(logf, m),
                price_cache=_open_trade_prices,
            )
            if closed_today:
                new_patterns = outcome_analyst.run_outcome_analysis(
                    closed_today, log=lambda m: _log_line(logf, m)
                )
        except Exception as exc:
            _log_line(logf, f"Outcome step failed: {exc}")

        try:
            from src import research_outcome_checker
            research_outcome_checker.check_open_research_trades(
                log=lambda m: _log_line(logf, m),
                price_cache=_open_trade_prices,
            )
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
        _top_n     = _SCAN_TOP_N
        _td_cap    = _TD_CACHE_MAX
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
                f"Scanning full universe of {universe_size} pairs — selecting top pairs by merit score. "
                f"Top {len(pairs_today)}: {', '.join(pairs_today)}",
            )
        except Exception as exc:
            _log_line(logf, f"Smart selection failed ({exc}) — falling back to watchlist.")
            pairs_today = list(config.WATCHLIST)

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
                            _ind_e, _ind_s, _ind_t, _ind_meta_r = _calc_indicative_levels(
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
            try:
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
            except Exception as _tg_exc:
                print(f"[telegram] _send_telegram_summary CRASHED: {_tg_exc}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                _telegram(
                    f"⚠️ <b>{scan_mode.upper()} scan complete but summary build failed</b>\n"
                    f"{type(_tg_exc).__name__}: {str(_tg_exc)[:200]}"
                )

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
