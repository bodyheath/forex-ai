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
import threading
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

    mins_away  = (win_s_mins - cur_mins) % (24 * 60)
    hours_away = mins_away / 60
    if mins_away < 120:
        return "🟡", f"ENTER SOON — {sess_name} opens in {mins_away} min"
    hrs = mins_away // 60
    rem = mins_away % 60
    t   = f"{hrs}h {rem}m" if rem else f"{hrs}h"
    # Use same 8h / 20h thresholds as _time_ref_for_entry so badge always matches text
    if hours_away <= 8:
        return "🟠", f"WAIT — {sess_name} opens in {t} (today)"
    if hours_away <= 20:
        return "🟠", f"WAIT — {sess_name} opens in {t} (tonight)"
    return "🔴", "WAIT UNTIL TOMORROW — optimal window more than 20 hours away"


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


def _cot_reversal_penalty(result: dict) -> int:
    """Return -1 if a REVERSING COT signal aligns with the OLD positioning that
    now contradicts the trade direction, else 0.

    Rule: penalise when institutions recently FLIPPED and you are trading with
    the crowd that is now exiting.
      BUY  + base  COT REVERSING from net LONG  → institutions abandoned their long
      BUY  + quote COT REVERSING from net SHORT → institutions abandoned their short
      SELL + base  COT REVERSING from net SHORT → institutions abandoned their short
      SELL + quote COT REVERSING from net LONG  → institutions abandoned their long
    """
    try:
        direction = (result.get("parsed", {}).get("direction") or "").upper()
        if direction not in ("BUY", "SELL"):
            return 0
        pos = result.get("bundle", {}).get("positioning", {})
        pair = result.get("pair", "")
        clean = pair.upper().replace("/", "")
        base_ccy  = clean[:3] if len(clean) >= 6 else ""
        quote_ccy = clean[3:6] if len(clean) >= 6 else ""

        for side, ccy in (("base", base_ccy), ("quote", quote_ccy)):
            pp = pos.get(side, {})
            if pp.get("status") != "ok":
                continue
            if pp.get("cot_momentum") != "REVERSING":
                continue
            old_net   = pp.get("net_3w_ago", 0)
            old_long  = old_net > 0
            old_short = old_net < 0
            # BUY: you want base up — penalty if base was long (flipped to short)
            #      or quote was short (flipped to long, making quote stronger)
            if direction == "BUY":
                if side == "base"  and old_long:  return -1
                if side == "quote" and old_short: return -1
            # SELL: you want base down — penalty if base was short (flipped to long)
            #       or quote was long (flipped to short, weakening quote you need weak)
            else:
                if side == "base"  and old_short: return -1
                if side == "quote" and old_long:  return -1
    except Exception:
        pass
    return 0


def _smd_score(result: dict) -> int:
    """Extract pre-computed Smart Money Divergence score from the bundle (−10 to +10)."""
    try:
        return int(result.get("bundle", {}).get("smart_money", {}).get("smd_score", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _eff_conf(result: dict) -> int:
    """Confidence after MA ribbon, COT momentum, and Smart Money Divergence adjustments.

    Ribbon:       −1 when ALIGNED ribbon is fully against trade direction.
    COT reversal: −1 when institutions just flipped away from the direction
                  you are trading with.
    Both penalties can stack (max −2).
    """
    raw = _conf(result)
    if raw == 0:
        return 0
    adj = 0
    try:
        direction = (result.get("parsed", {}).get("direction") or "").upper()
        rib_status = (
            result.get("bundle", {})
            .get("technical", {})
            .get("daily", {}) or {}
        ).get("ribbon", {}).get("status", "")
        if rib_status == "ALIGNED_BULL" and direction == "SELL":
            adj -= 1
        if rib_status == "ALIGNED_BEAR" and direction == "BUY":
            adj -= 1
    except Exception:
        pass
    adj += _cot_reversal_penalty(result)
    # Fundamental alignment adjustment
    _fa = result.get("_fundamental_alignment")
    if _fa is None:
        _fa_pair = result.get("pair", "")
        _fa_dirn = (result.get("parsed", {}).get("direction") or "").upper()
        if _fa_pair and "/" in _fa_pair and _fa_dirn:
            try:
                from src import fundamentals as _fund
                _fa_base, _fa_quot = _fa_pair.split("/")
                _fa = _fund.get_fundamental_alignment(_fa_base, _fa_quot, _fa_dirn)
                result["_fundamental_alignment"] = _fa
            except Exception:
                _fa = {}
    adj += (_fa or {}).get("conf_adj", 0)
    # Smart Money Divergence: boost when aligned, penalty when opposing
    try:
        _smd = _smd_score(result)
        _smd_dir = (result.get("parsed", {}).get("direction") or "").upper()
        if (_smd >= 8 and _smd_dir == "BUY") or (_smd <= -8 and _smd_dir == "SELL"):
            adj += 1
        elif (_smd <= -5 and _smd_dir == "BUY") or (_smd >= 5 and _smd_dir == "SELL"):
            adj -= 1
    except Exception:
        pass
    return max(1, raw + adj)


def _conf_bar(conf) -> str:
    """Generate visual confidence bar: 7 → ███████░░░"""
    try:
        n = max(0, min(10, int(conf)))
    except (TypeError, ValueError):
        n = 0
    return "█" * n + "░" * (10 - n)


def _pip_size(pair: str) -> float:
    """Return the pip size for any forex pair based on the QUOTE currency.

    Comprehensive rule covering all 130+ eligible pairs:
      quote JPY  → 0.01     e.g. USD/JPY, EUR/JPY, AUD/JPY, NOK/JPY, HKD/JPY
      base  JPY  → 0.000001 e.g. JPY/USD, JPY/AUD  (rare inverted pairs)
      else  → 0.0001        ALL other pairs including:
                            standard:  EUR/USD, GBP/USD, USD/CAD, AUD/USD …
                            HKD quote: USD/HKD (≈7.8), CAD/HKD (≈5.6), AUD/HKD …
                            SGD quote: USD/SGD (≈1.34), EUR/SGD, AUD/SGD …
                            NOK quote: USD/NOK (≈9.5), EUR/NOK (≈11), GBP/NOK …
                            SEK quote: USD/SEK (≈10.5), EUR/SEK (≈12), GBP/SEK …
                            DKK/MXN/ZAR/TRY quote: all use 0.0001
    Note: HKD/SGD/NOK/SEK pairs all use 4-decimal quoting (0.0001 per pip)
    regardless of their price level — only JPY pairs use 2-decimal quoting.
    """
    cleaned = pair.upper().replace("/", "").replace("-", "")
    if len(cleaned) >= 6:
        base  = cleaned[:3]
        quote = cleaned[3:6]
        if quote == "JPY":
            return 0.01
        if base == "JPY":
            return 0.000001
    elif "JPY" in pair.upper():
        return 0.01
    return 0.0001


def _fmt_pips_between(pair: str, price_a, price_b) -> str:
    try:
        pips = abs(float(price_a) - float(price_b)) / _pip_size(pair)
        return f"{pips:.0f} pips"
    except (TypeError, ValueError, ZeroDivisionError):
        return "—"


def _analyse_pair(pair: str, logf, force_deep: bool = False,
                  shared_fundamental=None, shared_macro=None,
                  sonnet_threshold: int = 6,
                  pair_threshold_override=None) -> dict | None:
    try:
        return service.analyse_and_log(
            pair,
            log=lambda m: _log_line(logf, m),
            force_deep=force_deep,
            shared_fundamental=shared_fundamental,
            shared_macro=shared_macro,
            sonnet_threshold=sonnet_threshold,
            pair_threshold_override=pair_threshold_override,
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
        mtf_pts = 0.0
    elif avg_mtf >= 0.70:
        mtf_pts = 2.0
    elif avg_mtf >= 0.45:
        mtf_pts = 1.0
    else:
        mtf_pts = 0.0

    # Trend clarity: % of pairs with qualifying weekly+daily alignment (0-2)
    qpct = ctx.get("qualify_pct")
    if qpct is None:
        trend_pts = 0.0
    elif qpct >= 0.50:
        trend_pts = 2.0
    elif qpct >= 0.25:
        trend_pts = 1.0
    else:
        trend_pts = 0.0

    raw = vix_pts + news_pts + mtf_pts + trend_pts  # 0–10
    score = max(1, min(10, round(raw)))

    # Risk-OFF override: cap score regardless of other components
    _ro_env = "risk-off" in (ctx.get("risk_env") or "").lower()
    if _ro_env:
        _vix_ro = ctx.get("vix")
        if _vix_ro is not None and _vix_ro > 25:
            score = min(score, 4)
        else:
            score = min(score, 6)

    # Build description from the weakest factors
    parts = []
    notable = ctx.get("high_impact_notable") or []

    if avg_mtf is not None and avg_mtf < 0.45:
        parts.append("choppy market")
    elif avg_mtf is not None and avg_mtf < 0.70:
        parts.append("moderate trend clarity")
    elif avg_mtf is not None:
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

    # Consistency check: if market environment is ranging/low-volatility, cap score
    # and override MTF description to avoid "strong trend clarity" contradiction
    _env_lo = (ctx.get("risk_env") or "").lower()
    if "ranging" in _env_lo or "low volatility" in _env_lo:
        score = min(score, 5)
        parts = [
            "ranging market — limited directional bias"
            if p in ("strong trend clarity", "moderate trend clarity")
            else p
            for p in parts
        ]
        if not any("ranging" in p for p in parts):
            parts.insert(0, "ranging market")

    desc_body = ", ".join(parts)

    if _ro_env:
        _vix_ro_desc = ctx.get("vix")
        if _vix_ro_desc is not None and _vix_ro_desc > 25:
            suffix = "elevated risk aversion — high caution recommended"
        else:
            suffix = "risk-off environment — safe haven pairs favoured — conditions selective"
    elif score >= 8:
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


def _score_breakdown_line(parsed: dict, smd: int = 0) -> str:
    def _s(key):
        v = parsed.get(key)
        return str(v) if v is not None else "—"
    smd_str = f"  SMD:{smd:+d}" if smd != 0 else "  SMD:0"
    return (
        f"T:{_s('technical_score')}  "
        f"F:{_s('fundamental_score')}  "
        f"S:{_s('sentiment_score')}  "
        f"P:{_s('positioning_score')}  "
        f"M:{_s('macro_score')}"
        f"{smd_str}"
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

    if not rr:
        _atr_dbg = float(
            (r.get("bundle", {}).get("technical", {}).get("daily") or {}).get("atr14") or 0
        )
        print(
            f"[DEBUG R:R=0] {r.get('pair','?')} "
            f"entry={p.get('entry')} stop={p.get('stop_loss')} target={p.get('target')} "
            f"rr_field={p.get('reward_risk')} atr14={_atr_dbg:.5f}"
        )

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

    # Issue 3: compute fundamental alignment for grade floor
    _fa_grade = r.get("_fundamental_alignment")
    if _fa_grade is None:
        try:
            from src import fundamentals as _fund_grade
            _pair_grade = r.get("pair", "")
            if _pair_grade and "/" in _pair_grade and direction:
                _fb_g, _fq_g = _pair_grade.split("/")
                _fa_grade = _fund_grade.get_fundamental_alignment(_fb_g, _fq_g, direction)
                r["_fundamental_alignment"] = _fa_grade
        except Exception:
            pass
    _fa_grade = _fa_grade if isinstance(_fa_grade, dict) else {}
    _fa_aligned_g   = _fa_grade.get("aligned", 0)
    _fa_alignment_g = _fa_grade.get("alignment", "MIXED")
    _has_fund_tailwind = (_fa_alignment_g == "TAILWIND" and _fa_aligned_g >= 3)

    # ── Grade F — never trade ─────────────────────────────────────────────
    if rib_strongly_against or w_d_conflict or rr < 1.3:
        grade = "F"
        # Issue 3 floor: 3/3 fundamental tailwind + conf>=6 cannot be Grade F
        if _has_fund_tailwind and conf >= 6:
            grade = "C"
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
    _DIMENSION_PLAIN = {
        "Technical":   "price charts need a clearer trend signal",
        "Fundamental": "interest rate advantage needs to strengthen",
        "Sentiment":   "news tone needs to become more positive",
        "Positioning": "institutional investors need to increase their position",
        "Macro":       "global economic conditions need to improve",
    }
    if weak:
        for name, _sc in sorted(weak.items(), key=lambda x: x[1])[:2]:
            parts.append(_DIMENSION_PLAIN.get(name, f"{name} needs to strengthen"))
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
            f"<b>{wr:.0f}%</b> wins  Net: <b>{total_r:+.2f}R</b>"
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


def _build_compact_research_section() -> list:
    """2–4 line research summary for intraday scans (9am / 5pm / 11pm)."""
    try:
        from src import research_tracker as _rtrk_cpt
        _cpt_rows = _rtrk_cpt.load()
    except Exception:
        return []
    if not _cpt_rows:
        return []

    def _cf(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    _cpt_open  = [r for r in _cpt_rows if r.get("status") in ("OPEN", "NO_PRICE_LEVELS")]
    _cpt_cls   = [r for r in _cpt_rows if r.get("status") in ("WIN", "LOSS", "BREAKEVEN", "EXPIRED", "PARTIAL_WIN")]
    _cpt_wins  = [r for r in _cpt_cls if r.get("status") == "WIN"]
    _cpt_loss  = [r for r in _cpt_cls if r.get("status") == "LOSS"]
    _cpt_pw    = [r for r in _cpt_cls if r.get("status") == "PARTIAL_WIN"]
    _cpt_dec   = len(_cpt_wins) + len(_cpt_loss)
    _cpt_wr    = int(len(_cpt_wins) / _cpt_dec * 100) if _cpt_dec else 0

    _l2 = [f"Open: {len(_cpt_open)}", f"Closed: {len(_cpt_cls)}"]
    if _cpt_dec:
        _l2.append(f"Win rate: {_cpt_wr}% ({len(_cpt_wins)}W/{len(_cpt_loss)}L)")
    if _cpt_pw:
        _l2.append(f"{len(_cpt_pw)} partial wins")

    sec = [
        "", "━━━━━━━━━━━━━━━━━━━━━",
        "🔬 <b>RESEARCH TRADES</b>",
        " · ".join(_l2),
    ]

    # Best pairs by win rate (≥2 trades for statistical relevance)
    _cpt_ps: dict = {}
    for _cr in _cpt_cls:
        _cp = _cr.get("pair", "?")
        _cpt_ps.setdefault(_cp, {"wins": 0, "total": 0})
        _cpt_ps[_cp]["total"] += 1
        if _cr.get("status") in ("WIN", "PARTIAL_WIN"):
            _cpt_ps[_cp]["wins"] += 1
    _cpt_good = [(p, s) for p, s in _cpt_ps.items() if s["total"] >= 2]
    if _cpt_good:
        _cpt_sorted = sorted(
            _cpt_good,
            key=lambda x: (x[1]["wins"] / x[1]["total"], x[1]["total"]),
            reverse=True,
        )[:3]
        sec.append("Best pairs: " + " · ".join(
            f"{p} {int(s['wins']/s['total']*100)}%" for p, s in _cpt_sorted
        ))

    # Profit factor
    _cpt_wp = [_cf(r.get("pips")) for r in _cpt_wins if _cf(r.get("pips")) is not None]
    _cpt_lp = [_cf(r.get("pips")) for r in _cpt_loss if _cf(r.get("pips")) is not None]
    if _cpt_wp and _cpt_lp:
        _cpt_tl = sum(abs(p) for p in _cpt_lp if p < 0)
        if _cpt_tl > 0:
            _cpt_pf = sum(_cpt_wp) / _cpt_tl
            _cpt_icon = "✅" if _cpt_pf > 1.0 else "⚠️"
            _cpt_txt  = "wins larger than losses" if _cpt_pf > 1.0 else "losses larger than wins"
            sec.append(f"Profit factor: {_cpt_pf:.2f} — {_cpt_txt} {_cpt_icon}")

    return sec


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

    open_rows    = [r for r in rows if r.get("status") in ("OPEN", "NO_PRICE_LEVELS")]
    closed       = [r for r in rows if r.get("status") in ("WIN", "LOSS", "BREAKEVEN", "EXPIRED", "PARTIAL_WIN")]
    wins         = [r for r in closed if r.get("status") == "WIN"]
    losses       = [r for r in closed if r.get("status") == "LOSS"]
    partial_wins = [r for r in closed if r.get("status") == "PARTIAL_WIN"]
    expired_rows = [r for r in closed if r.get("status") == "EXPIRED"]

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
        f"<b>{wr_pct}%</b> wins ({n_wins}W / {n_losses}L) — {decisive} closed trades"
    )
    if decisive >= 10 and wr_pct < 30:
        sec.append(
            f"⚠️ <b>Win rate {wr_pct}% from {decisive} trades — well below target.</b> "
            f"System is in learning phase — do not adjust strategy until 50+ decisive trades recorded."
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
            _pf = sum(win_pips) / total_loss
            sec.append(f"Profit factor: <b>{_pf:.2f}</b>")
            if _pf > 1.0:
                sec.append(
                    f"📈 Profit factor {_pf:.2f} — your wins are larger than your losses which is "
                    f"a good sign even at an early low win rate. This is how professional traders operate."
                )

    # Expiry analysis breakdown
    n_expiry_total = len(expired_rows) + len(partial_wins)
    if n_expiry_total > 0:
        sec.append("")
        sec.append("⏰ <b>Expiry analysis:</b>")
        partial_ep = [_f(r.get("pips")) for r in partial_wins if _f(r.get("pips")) is not None]
        exp_pos    = [_f(r.get("pips")) for r in expired_rows if (_f(r.get("pips")) or 0) > 0]
        exp_neg    = [_f(r.get("pips")) for r in expired_rows if (_f(r.get("pips")) or 0) < 0]
        exp_neu    = [r for r in expired_rows if _f(r.get("pips")) is None or _f(r.get("pips")) == 0]
        if partial_wins:
            avg_p = sum(partial_ep) / len(partial_ep) if partial_ep else 0
            sec.append(
                f"✅ {len(partial_wins)} PARTIAL WIN (>50% to target, avg +{avg_p:.0f}p) · "
                f"{len(expired_rows)} expired"
            )
        else:
            sec.append(f"{n_expiry_total} expired — expiry window extended to 7-10 days")
        detail = []
        if exp_pos:
            avg_pos = sum(exp_pos) / len(exp_pos)
            detail.append(f"{len(exp_pos)} profitable (avg +{avg_pos:.0f}p)")
        if exp_neg:
            avg_neg = sum(exp_neg) / len(exp_neg)
            detail.append(f"{len(exp_neg)} against direction (avg {avg_neg:.0f}p)")
        if exp_neu:
            detail.append(f"{len(exp_neu)} neutral")
        if detail and expired_rows:
            sec.append(f"   Expired: {' · '.join(detail)}")

    # Confidence breakdown (bands 5, 6, 7)
    sec.append("")
    sec.append("📊 <b>Confidence breakdown:</b>")
    for cv in (5, 6, 7):
        band = [r for r in closed if str(r.get("confidence", "")).strip() == str(cv)]
        bw   = sum(1 for r in band if r.get("status") == "WIN")
        bpw  = sum(1 for r in band if r.get("status") == "PARTIAL_WIN")
        bl   = sum(1 for r in band if r.get("status") == "LOSS")
        bd   = bw + bl
        pw_str = f" · {bpw} partial" if bpw else ""
        if bd:
            sec.append(f"{cv}/10 setups: {bw}W {bl}L{pw_str} — {int(bw/bd*100)}% win rate")
        elif bpw:
            sec.append(f"{cv}/10 setups: {bpw} partial wins — no full wins/losses yet")
        elif band:
            sec.append(f"{cv}/10 setups: {len(band)} trades — no decisive outcomes yet")
        else:
            sec.append(f"{cv}/10 setups: 0W 0L — no data yet")

    # Most promising pairs by win rate (PARTIAL_WIN counted as directional success)
    pair_stats: dict = {}
    for r in closed:
        p  = r.get("pair", "?")
        ps = pair_stats.setdefault(p, {"wins": 0, "total": 0})
        ps["total"] += 1
        if r.get("status") in ("WIN", "PARTIAL_WIN"):
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

    # ── Pair performance weighting display ───────────────────────────────────
    # Build per-pair stats using decisive trades (WIN + LOSS only, not PARTIAL/EXPIRED)
    _pw_stats: dict = {}
    for r in rows:
        p = r.get("pair", "")
        s = r.get("status", "")
        if not p or s not in ("WIN", "LOSS"):
            continue
        if p not in _pw_stats:
            _pw_stats[p] = {"wins": 0, "n": 0}
        _pw_stats[p]["n"] += 1
        if s == "WIN":
            _pw_stats[p]["wins"] += 1

    _strengths = sorted(
        [(p, d["wins"] / d["n"], d["n"]) for p, d in _pw_stats.items()
         if d["n"] >= 3 and d["wins"] / d["n"] >= 0.60],
        key=lambda x: x[1], reverse=True,
    )
    _weaknesses = sorted(
        [(p, d["n"]) for p, d in _pw_stats.items()
         if d["n"] >= 4 and d["wins"] == 0],
        key=lambda x: x[1], reverse=True,
    )

    if _strengths or _weaknesses:
        sec.append("")
    if _strengths:
        _str_parts = " · ".join(f"{p} {wr:.0%}" for p, wr, _ in _strengths[:6])
        sec.append(f"🏆 System strengths (60%+ win rate with 3+ trades): {_str_parts}")
    if _weaknesses:
        _wk_parts = " · ".join(p for p, _ in _weaknesses[:5])
        sec.append(
            f"⚠️ System weaknesses (0% win rate with 4+ trades): {_wk_parts}"
            f" — deprioritised until conditions change"
        )

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
    try:
        _ev = float(entry_v)
        if _ev < 0.01:
            dec = 6
        elif _ev < 0.10:
            dec = 5
        elif "JPY" in pair.upper():
            dec = 3
        else:
            dec = 5
    except (TypeError, ValueError):
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

    # COT positioning + momentum
    try:
        pos_bundle = r["bundle"]["positioning"]
        for side in ("base", "quote"):
            pp = pos_bundle.get(side, {})
            if pp.get("status") == "ok":
                pct      = pp.get("percentile_in_range")
                pdir     = pp.get("direction", "")
                flag     = (pp.get("extreme_flag") or "")[:55]
                momentum = pp.get("cot_momentum", "")
                if pct is not None:
                    _mom_icons = {
                        "BUILDING":  "📈",
                        "UNWINDING": "📉",
                        "REVERSING": "🔄",
                        "STABLE":    "➡️",
                    }
                    _mom_icon = _mom_icons.get(momentum, "")
                    _mom_str  = f" · {_mom_icon} COT {momentum}" if momentum else ""
                    lines.append(
                        f"- COT: {pp['currency']} {pdir} at {pct:.0f}th pct"
                        f"{_mom_str} — {flag}"
                    )
                    break
    except (KeyError, TypeError, ValueError):
        ps = p.get("positioning_score")
        if ps:
            lines.append(f"- COT: positioning score {ps}/10")

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
    _cot_cache: dict = {}          # ccy -> percentile_in_range
    _cot_mom_cache: dict = {}      # ccy -> cot_momentum string
    for ccy in unique_ccys:
        cot_data = _pos._for_currency(ccy)
        if cot_data.get("status") == "ok":
            _cot_cache[ccy]     = cot_data.get("percentile_in_range", 50.0)
            _cot_mom_cache[ccy] = cot_data.get("cot_momentum", "STABLE")

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

        # COT positioning: distance from 50th percentile + momentum bonus
        for ccy in (base, quote):
            pct = _cot_cache.get(ccy)
            if pct is not None:
                score += abs(pct - 50.0) / 25.0   # max 2 pts per currency
            mom = _cot_mom_cache.get(ccy, "")
            if mom == "BUILDING":
                score += 0.5   # institutions adding conviction — higher opportunity
            elif mom == "REVERSING":
                score -= 0.5   # institutional flip is a warning, deprioritise

        # Fundamental divergence: pairs where one ccy is fundamentally bullish and
        # the other bearish have a clearer directional trade — score them higher
        try:
            from src import fundamentals as _fund_pf
            _cb_b  = _fund_pf.currency_cb_score(base)
            _cb_q  = _fund_pf.currency_cb_score(quote)
            _ec_b  = _fund_pf.currency_econ_score(base)
            _ec_q  = _fund_pf.currency_econ_score(quote)
            score += abs((_cb_b + _ec_b) - (_cb_q + _ec_q)) * 0.5  # max 2.0
        except Exception:
            pass

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


def _calc_indicative_levels(pair: str, parsed: dict, bundle: dict,
                             research_mode: bool = False) -> tuple:
    """Return (entry, stop, target, meta) for indicative display.

    Stop = 1.0x ATR(14), rounded to nearest 5 pips.
    Target (display mode):  nearest Fibonacci in [1.5x, 2.5x] ATR, else 2.0x ATR.
    Target (research_mode): nearest Fibonacci in [0.8x, 1.2x] ATR, else 1.0x ATR.
      research_mode=True uses a tighter target so research trades resolve as WIN/LOSS
      faster (more decisive outcomes for ML training).  Fund trade display is always
      called with the default research_mode=False — targets are never changed.
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
    base_ccy  = pair.split("/")[0].upper()  if "/" in pair else pair[:3].upper()
    is_jpy      = quote_ccy == "JPY"
    # Issue 2: JPY-as-base pairs (JPY/USD, JPY/EUR etc.) have very small prices (~0.006)
    is_jpy_base = (base_ccy == "JPY" and not is_jpy)

    if is_jpy:
        pip_size, dec = 0.01, 3
    elif is_jpy_base:
        pip_size, dec = 0.000001, 6   # micro pip for JPY/USD at price ~0.006
    else:
        pip_size, dec = 0.0001, 5

    # Use actual ATR14 if available, else estimate from pair characteristics
    if atr_actual and atr_actual > 0:
        atr = atr_actual
    elif is_jpy:
        atr = 0.50
    elif is_jpy_base:
        # ~0.3% of price is equivalent to a ~50 pip move in USD/JPY
        atr = (cur * 0.003) if cur else 0.00002
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

    # Stop: 1.0x ATR rounded to nearest 5 pips (minimum 5 pips to avoid zero stop)
    if not stop:
        atr_pips   = atr / pip_size
        stop_pips  = round(atr_pips / 5) * 5   # nearest 5 pips
        stop_pips  = max(stop_pips, 5)          # never allow zero stop distance
        stop_dist  = stop_pips * pip_size
        stop = round(entry - stop_dist if dirn == "BUY" else entry + stop_dist, dec)

    # Target: nearest Fibonacci level in the calibrated ATR range, else ATR fallback.
    # research_mode uses 1.0x ATR target (tighter) so trades resolve WIN/LOSS quickly.
    # Display mode keeps 2.0x ATR for fund-level trade blocks — NEVER changed.
    if not target:
        if research_mode:
            min_dist     = 0.8 * atr
            max_dist     = 1.2 * atr
            fallback_mult = 1.0
        else:
            min_dist     = 1.5 * atr
            max_dist     = 2.5 * atr
            fallback_mult = 2.0
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
            target = round(
                entry + atr * fallback_mult if dirn == "BUY"
                else entry - atr * fallback_mult, dec
            )

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
        # research_mode intentionally uses 1:1 R:R — don't flag as low quality
        _rr_threshold = 0.5 if research_mode else 1.5
        meta["quality_flag"]    = "LOW QUALITY SETUP" if rr < _rr_threshold else ""
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


def _build_open_trades_section(open_trades: list, px_cache: dict, now_ak, compact: bool = False, cur_conf_map: dict = None) -> list:
    """Build the OPEN TRADES section lines for any Telegram scan message.

    compact=True: one-line-per-trade format for intraday scans.
    Always returns a non-empty list (shows 'No open trades' when empty).
    Placed at the top of every message immediately after the scan header.
    Each trade shows: entry/current/stop/target, unrealised P&L, progress bar,
    one dynamic status message from 8 possible states, and next key check time.
    """
    sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "📊 <b>YOUR OPEN TRADES</b>"]

    # Load risk profile once for investment detail calculations
    _rm_profile: dict = {}
    _rm_state: dict = {}
    try:
        from src import risk_manager as _rm_inv
        _rm_profile = _rm_inv.load_profile()
        _rm_state   = _rm_inv.compute_risk_state(_rm_profile)
    except Exception:
        pass

    if not open_trades:
        _fund_bal = _rm_profile.get("estimated_balance", 0) if _rm_profile else 0
        if compact:
            sec.append("No trades open · Fund: $" + (f"{_fund_bal:,.0f}" if _fund_bal else "—") + " fully available")
        else:
            sec.append("No trades currently open — the system is waiting for the right opportunity.")
            if _fund_bal > 0:
                sec.append(f"Your fund balance: <b>${_fund_bal:,.0f}</b> — fully available for the next trade.")
        return sec

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

    _CCY_FULL = {
        "USD": "US Dollar",   "EUR": "Euro",         "GBP": "British Pound",
        "JPY": "Japanese Yen","AUD": "Australian Dollar","NZD": "New Zealand Dollar",
        "CAD": "Canadian Dollar","CHF": "Swiss Franc","HKD": "Hong Kong Dollar",
        "SGD": "Singapore Dollar","NOK": "Norwegian Krone","SEK": "Swedish Krona",
    }

    # Compact mode: one-line summary per trade for intraday messages
    if compact:
        for row in open_trades:
            _cp_row, _ = _fetch_live_price(row.get("pair", "?"), px_cache)
            _ce_row    = float(row.get("entry") or 0) or None
            _cs_row    = float(row.get("stop_loss") or 0) or None
            _ct_row    = float(row.get("target") or 0) or None
            _cd_row    = (row.get("direction") or "").upper()
            _cid_row   = row.get("id", "?")
            _cpair     = row.get("pair", "?")
            _buy_row   = _cd_row == "BUY"
            # Progress %
            _pct_row   = 0.0
            _gross_row = None
            try:
                if _cp_row and _ce_row and _cs_row and _ct_row:
                    _pr_pip = _pip_size(_cpair)
                    _pips_row = ((_cp_row - _ce_row) if _buy_row else (_ce_row - _cp_row)) / _pr_pip
                    if _buy_row and _ct_row > _ce_row:
                        _pct_row = min(100.0, max(0.0, (_cp_row - _ce_row) / (_ct_row - _ce_row) * 100))
                    elif not _buy_row and _ct_row < _ce_row:
                        _pct_row = min(100.0, max(0.0, (_ce_row - _cp_row) / (_ce_row - _ct_row) * 100))
                    # Dollar P&L approx (pip value * pips)
                    _stop_pips_row = abs(_ce_row - _cs_row) / _pr_pip
                    # simple approx: assume $1/pip for mini lot
                    _gross_row = _pips_row * 1.0
            except Exception:
                pass
            _bar_row   = int(_pct_row / 100 * 20)
            _prog_row  = "█" * _bar_row + "░" * (20 - _bar_row)
            if _gross_row is None:
                _gross_row = 0.0
            _pnl_icon  = "✅" if _gross_row > 0 else ("📉" if _gross_row < 0 else "➡️")
            _pnl_str   = (f"+${_gross_row:.0f}" if _gross_row > 0
                          else (f"-${abs(_gross_row):.0f}" if _gross_row < 0
                                else "$0.00"))
            sec.append("")
            sec.append(
                f"TRADE #{_cid_row} — {_cpair} {'Buying' if _buy_row else 'Selling'} · "
                f"{_pnl_str} {_pnl_icon} · {_pct_row:.0f}% to target {_prog_row}"
            )
            # Expiry warning
            try:
                _ot_exp = 5
                if _ce_row and _cs_row and _ct_row:
                    _sd_c = abs(_ce_row - _cs_row)
                    _td_c = abs(_ct_row - _ce_row)
                    _ot_exp = _compute_expiry_days_from_rr(_td_c / _sd_c if _sd_c > 0 else 0)
                _odt_c = datetime.strptime((row.get("timestamp") or "")[:10], "%Y-%m-%d")
                _days_c = (now_ak.replace(tzinfo=None) - _odt_c).days
                _rem_c = max(0, _ot_exp - _days_c)
                if _rem_c <= 2:
                    sec.append(f"⚠️ Expires in {_rem_c} day{'s' if _rem_c != 1 else ''}")
            except Exception:
                pass
            # 50% milestone reminder
            if _pct_row >= 50:
                sec.append(f"⚠️ Halfway — move stop loss to entry price to guarantee no loss")
            # Confidence monitoring (compact)
            if cur_conf_map is not None:
                _entry_conf_c = None
                try:
                    _entry_conf_c = int(float(row.get("confidence") or 0)) or None
                except (TypeError, ValueError):
                    pass
                _cur_conf_c = cur_conf_map.get(_cpair)
                if _entry_conf_c and _cur_conf_c is not None:
                    _cdelta_c = _cur_conf_c - _entry_conf_c
                    if _cdelta_c >= 1:
                        sec.append(f"✅ Thesis strengthening — confidence {_entry_conf_c}→{_cur_conf_c}/10")
                    elif _cdelta_c == 0:
                        sec.append(f"Confidence stable at {_entry_conf_c}/10 since entry")
                    elif _cdelta_c <= -2:
                        _urg_c = "🚨" if _cur_conf_c < 3 else "⚠️"
                        sec.append(f"{_urg_c} Confidence dropped {_entry_conf_c}→{_cur_conf_c}/10 — conditions have weakened — monitor closely")
            # News reminder (compact)
            try:
                from src import economic_calendar as _ec_ot_c
                _ot_evs_c = _ec_ot_c.events_for_pair(_cpair, hours=48)
                for _ot_ev_c in _ot_evs_c[:1]:
                    _ot_ccy_c = _ot_ev_c["currency"]
                    _ot_en_c  = _ot_ev_c["plain_name"]
                    _ot_ak_c  = _ot_ev_c["ak_display"]
                    sec.append(f"⚠️ {_ot_ccy_c} {_ot_en_c} within 48h ({_ot_ak_c}) — affects your {_cpair} trade")
            except Exception:
                pass
        return sec

    for row in open_trades:
        pair = row.get("pair", "?")
        dirn = (row.get("direction") or "").upper()
        tid  = row.get("id", "?")
        _base_c = pair.split("/")[0] if "/" in pair else pair[:3]
        _quot_c = pair.split("/")[1] if "/" in pair else pair[3:]
        _base_name = _CCY_FULL.get(_base_c, _base_c)
        _quot_name = _CCY_FULL.get(_quot_c, _quot_c)
        if dirn == "BUY":
            _dirn_plain = f"Buying {pair} (betting the {_base_name} will strengthen against the {_quot_name})"
        else:
            _dirn_plain = f"Selling {pair} (betting the {_base_name} will weaken against the {_quot_name})"

        sec.append("")
        sec.append(f"📊 <b>TRADE #{tid} — {_dirn_plain}</b>")

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

        # Compute entry date string
        _entry_date_str = ""
        try:
            _ots = row.get("timestamp", "")[:10]
            _odt = datetime.strptime(_ots, "%Y-%m-%d")
            _entry_date_str = f" on {_odt.day} {_odt.strftime('%B')}"
        except Exception:
            pass

        if entry:
            stale_note = " ⚠️ last known price" if (cur and stale) else ""
            sec.append(f"Entered at: {_fmt_price(entry)}{_entry_date_str}")
            if cur:
                sec.append(f"Current price: {_fmt_price(cur)}{stale_note}")

        # Confidence monitoring — improvement, stability, or deterioration since entry
        if cur_conf_map is not None:
            _entry_conf_f = None
            try:
                _entry_conf_f = int(float(row.get("confidence") or 0)) or None
            except (TypeError, ValueError):
                pass
            _cur_conf_f = cur_conf_map.get(pair)
            if _entry_conf_f and _cur_conf_f is not None:
                _conf_delta_f = _cur_conf_f - _entry_conf_f   # positive = improved
                if _conf_delta_f >= 1:
                    sec.append(
                        f"✅ Trade thesis strengthening — confidence improved from "
                        f"{_entry_conf_f}/10 at entry to {_cur_conf_f}/10 today"
                    )
                elif _conf_delta_f == 0:
                    sec.append(f"Confidence stable at {_entry_conf_f}/10 since entry")
                elif _conf_delta_f == -1:
                    sec.append(
                        f"Confidence eased from {_entry_conf_f}/10 at entry to "
                        f"{_cur_conf_f}/10 today — within normal variation"
                    )
                else:
                    _urg_f = "🚨" if _cur_conf_f < 3 else "⚠️"
                    sec.append(
                        f"{_urg_f} {pair} confidence dropped from {_entry_conf_f}/10 at entry to "
                        f"{_cur_conf_f}/10 today — the conditions that supported this trade have "
                        f"weakened — monitor closely"
                    )
        # Session monitoring reminder for this pair
        try:
            _stl_f     = _session_time_label(pair, now_ak)
            _dom_ccy_f = next(
                (c for c in _SESSION_PRIORITY
                 if c in {pair.replace("/", "")[:3].upper(),
                          pair.replace("/", "")[3:6].upper()}),
                None,
            )
            _sess_nm_f = _SESSION_AUCKLAND.get(_dom_ccy_f, ("London", ""))[0] if _dom_ccy_f else "London"
            if _dom_ccy_f:
                sec.append(
                    f"⏰ Check at {_stl_f} — "
                    f"{_dom_ccy_f} pairs most active during {_sess_nm_f} session"
                )
            else:
                sec.append(f"⏰ Check at {_stl_f}")
        except Exception:
            pass

        # News reminder for open trade
        try:
            from src import economic_calendar as _ec_ot_f
            _ot_evs_f = _ec_ot_f.events_for_pair(pair, hours=48)
            for _ot_ev_f in _ot_evs_f[:2]:
                _ot_ccy_f = _ot_ev_f["currency"]
                _ot_en_f  = _ot_ev_f["plain_name"]
                _ot_ak_f  = _ot_ev_f["ak_display"]
                _ot_hrs_f = _ot_ev_f.get("hours_away", 48)
                _timing_f = "very soon" if _ot_hrs_f <= 6 else "within 24 hours" if _ot_hrs_f <= 24 else "within 48 hours"
                sec.append(
                    f"⚠️ Reminder: {_ot_ccy_f} {_ot_en_f} releases {_timing_f} ({_ot_ak_f}) "
                    f"— directly affects your open {pair} trade"
                )
        except Exception:
            pass

        if stop:
            _stop_verb = "falls" if dirn == "BUY" else "rises"
            sec.append(
                f"Stop loss at: {_fmt_price(stop)} — if price {_stop_verb} here the trade closes automatically with a small loss"
            )
        if target:
            _tgt_verb = "rises" if dirn == "BUY" else "falls"
            sec.append(
                f"Target at: {_fmt_price(target)} — if price {_tgt_verb} here the trade closes automatically with a profit"
            )

        if entry and cur:
            # Compute dollar P&L using risk amount as reference
            pip_sz    = _pip_size(pair)
            raw       = (cur - entry) if dirn == "BUY" else (entry - cur)
            pips      = raw / pip_sz
            net_pips  = pips

            # Dollar P&L — use risk_amount if available, else 1:1 pip/dollar approx
            _risk_a   = 0.0
            _lots     = 0.0
            try:
                from src import risk_manager as _rm_inv2
                _sz2 = _rm_inv2.size_trade(
                    pair=pair, direction=dirn, entry=entry, stop=(stop or entry),
                    target=(target or entry),
                    confidence=int(float(row.get("confidence") or 8)),
                    profile=_rm_profile, risk_state=_rm_state,
                )
                _paper_scale2 = _rm_inv2.FUND_START / max(config.ACCOUNT_BALANCE, 1)
                _lots    = max(round(_sz2["lots"] * _paper_scale2, 2), 0.01)
                _risk_a  = round(_sz2["risk_amount"] * _paper_scale2, 2)
            except Exception:
                pass

            # Compute pip value per pip (dollar per pip)
            _pip_val = 0.0
            if _risk_a > 0 and stop and abs(entry - stop) > 0:
                _stop_pips = abs(entry - stop) / pip_sz
                _pip_val   = _risk_a / _stop_pips if _stop_pips > 0 else 0.0

            _gross_dollar = pips * _pip_val if _pip_val > 0 else None

            # Costs
            _cost_dollar  = 0.0
            _cost_pips    = 0.0
            try:
                from src import trade_costs as _tc_ot
                _ot_costs  = _tc_ot.compute_costs(pair, dirn, entry, float(days_open))
                _cost_pips = _ot_costs["total_cost_pips"]
                net_pips   = pips - _cost_pips
                _cost_dollar = _cost_pips * _pip_val if _pip_val > 0 else 0.0
            except Exception:
                pass

            _net_dollar = _gross_dollar - _cost_dollar if _gross_dollar is not None else None

            sec.append("")
            if _gross_dollar is not None:
                if pips > 2:
                    sec.append(f"Current profit: <b>+${_gross_dollar:.0f}</b> — the trade is moving in the right direction ✅")
                elif pips < -2:
                    sec.append(f"Current loss: <b>-${abs(_gross_dollar):.0f}</b> — the trade is moving against you")
                else:
                    sec.append("At breakeven — the trade is flat, watching for movement")
                if _cost_dollar > 0:
                    sec.append(f"Costs so far: -${_cost_dollar:.0f} spread and swap costs")
                if _net_dollar is not None and _cost_dollar > 0:
                    _net_sign = "+" if _net_dollar >= 0 else ""
                    sec.append(f"Net profit including costs: {_net_sign}${_net_dollar:.0f}")
            elif pips > 2:
                sec.append("Moving in the right direction ✅")
            elif pips < -2:
                sec.append("Moving against you — stay disciplined and let your stop do its job")

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
                    sec.append(f"Progress: {pct_tgt:.0f}% of the way to target {prog_bar}")

                    # "What to do" advice — most urgent condition first
                    _check_str = _check_line.replace("⏰ <b>", "").replace("</b>", "").replace("⏰ ", "")
                    if 0 < pips_to_target <= 20:
                        sec.append(f"⚠️ <b>What to do: Target almost reached — consider closing your position now to lock in profit</b>")
                    elif 0 < pips_to_stop <= 20:
                        sec.append(f"⚠️ <b>What to do: Stop loss is very close — be prepared. Let your stop do its job automatically</b>")
                    elif pct_tgt >= 75:
                        try:
                            trail_px = (
                                entry + (target - entry) * 0.75 if dirn == "BUY"
                                else entry - (entry - target) * 0.75
                            )
                            sec.append(f"What to do: Move your stop loss to {_fmt_price(trail_px)} to lock in gains — you are 75% of the way there")
                        except Exception:
                            sec.append("What to do: Move your stop to lock in profits — 75% to target")
                    elif pct_tgt >= 50:
                        sec.append(f"What to do: Move your stop loss to {_fmt_price(entry)} (your entry price) to protect your profit")
                        try:
                            halfway_px = (
                                entry + (target - entry) * 0.5 if dirn == "BUY"
                                else entry - (entry - target) * 0.5
                            )
                            sec.append(f"If price hits {_fmt_price(halfway_px)} (halfway to target) — your risk is now zero")
                        except Exception:
                            pass
                    elif days_open > 3:
                        sec.append(f"What to do: Trade has been open for {days_open_str} — if no clear momentum building, consider closing manually")
                    else:
                        sec.append(f"What to do: Nothing — let the trade run. Check again at {_check_str}")
                        try:
                            halfway_px2 = (
                                entry + (target - entry) * 0.5 if dirn == "BUY"
                                else entry - (entry - target) * 0.5
                            )
                            sec.append(
                                f"If price hits {_fmt_price(halfway_px2)} (halfway to target) — "
                                f"move your stop loss up to {_fmt_price(entry)} to protect your profit"
                            )
                        except Exception:
                            pass
                except Exception:
                    sec.append(_check_line)
            else:
                sec.append(_check_line)

        elif entry:
            if stop:
                _sv = "rises" if dirn == "BUY" else "falls"
                sec.append(f"Stop loss at: {_fmt_price(stop)} — if price {_sv} here the trade closes with a small loss")
            if target:
                _tv = "falls" if dirn == "BUY" else "rises"
                sec.append(f"Target at: {_fmt_price(target)} — if price {_tv} here the trade closes with a profit")
            sec.append("Current price: ⚠️ price unavailable — check your broker app")
            sec.append(f"What to do: Check again at {_check_line.replace('⏰ <b>','').replace('</b>','').replace('⏰ ','')}")
        else:
            sec.append("Trade details unavailable")

        if days_open > 0:
            if remaining <= 2:
                try:
                    _exp_date = (now_ak + timedelta(days=remaining)).strftime("%-d %B")
                except ValueError:
                    _exp_date = (now_ak + timedelta(days=remaining)).strftime("%d %B").lstrip("0")
                sec.append(
                    f"⚠️ This trade expires in {remaining} day{'s' if remaining != 1 else ''} — "
                    f"if target not reached by {_exp_date} it closes automatically at current price"
                )
            else:
                sec.append(f"Opened: {days_open_str} ago | Expires in: {expires_str}")

    return sec


# ── Drawdown-aware trade filter ───────────────────────────────────────────────

def _dd_allows_trade(r: dict, dd_mode: str, quality_grades: dict,
                     conf_threshold: int) -> bool:
    """Return True if the active drawdown protection tier permits this trade.

    Tier rules:
      halt         — no new trades under any circumstances
      preservation — A-grade only + all 3 core timeframes aligned
      defensive    — A-grade only
      caution      — A or B grade only
      normal       — A, B, or (C with effective confidence >= threshold)
    """
    grade = (quality_grades.get(r["pair"]) or {}).get("grade", "F")
    if dd_mode == "halt":
        return False
    if dd_mode == "preservation":
        if grade != "A":
            return False
        mtf = (r.get("bundle") or {}).get("mtf") or {}
        return mtf.get("agreeing_count", 0) >= 3
    if dd_mode == "defensive":
        return grade == "A"
    if dd_mode == "caution":
        return grade in ("A", "B")
    # normal
    return (grade in ("A", "B") or
            (grade == "C" and _eff_conf(r) >= conf_threshold))


# ── Main summary builder ───────────────────────────────────────────────────────

def _build_system_learning_report(date: str) -> list:
    """Build the SYSTEM LEARNING REPORT — Monday 6am scans only.

    Six sections: win rate trend, ML accuracy trend, MFE trend,
    best/worst pairs, confidence calibration, and overall verdict.
    Returns an empty list on any non-Monday day or when data is absent.
    """
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return []
    if dt.weekday() != 0:   # 0 = Monday
        return []

    sec = [
        "", "━━━━━━━━━━━━━━━━━━━━━",
        "🎓 <b>SYSTEM LEARNING REPORT</b>",
        "Is the system genuinely improving over time?",
    ]

    try:
        from src import research_tracker as _rtrk_lr
        rows = _rtrk_lr.load()
    except Exception:
        return []

    if not rows:
        sec.append("No research trades yet — report will appear once data is collected.")
        return sec

    def _f(v):
        try:
            return float(v) if v not in ("", None) else None
        except (TypeError, ValueError):
            return None

    decisive = [r for r in rows if r.get("status") in ("WIN", "LOSS")]
    n_dec    = len(decisive)

    def _trade_sort(r):
        return (r.get("date", ""), str(r.get("id", "")).zfill(6))

    dec_sorted = sorted(decisive, key=_trade_sort)
    any_added  = False

    # ── 1. WIN RATE TREND ──────────────────────────────────────────────────────
    if n_dec >= 20:
        groups = []
        for i in range(0, n_dec, 10):
            grp  = dec_sorted[i:i + 10]
            wins = sum(1 for r in grp if r.get("status") == "WIN")
            wr   = round(wins / len(grp) * 100) if grp else 0
            groups.append((i + 1, i + len(grp), wr))

        trend_str = " · ".join(f"Trades {s}-{e}: {w}%" for s, e, w in groups)
        improving = len(groups) >= 2 and groups[-1][2] > groups[0][2] + 4

        sec += ["", "<b>1. WIN RATE TREND</b>", trend_str]
        if improving:
            sec.append("✅ Win rate improving — system is learning")
        else:
            sec.append("⚠️ Win rate not improving — review needed")
        any_added = True

    # ── 2. ML ACCURACY TREND ──────────────────────────────────────────────────
    try:
        from src import ml_predictor as _mlp_lr
        _ml_meta  = _mlp_lr._load_meta()
        _acc_hist = _ml_meta.get("accuracy_history", [])
        if len(_acc_hist) >= 2:
            _recent   = _acc_hist[-4:]
            _acc_strs = [
                f"Week of {e.get('trained_at', '')[:10]}: {e.get('roc_auc', 0) * 100:.0f}%"
                for e in _recent
            ]
            _first_roc = _recent[0].get("roc_auc", 0.0)
            _last_roc  = _recent[-1].get("roc_auc", 0.0)
            sec += ["", "<b>2. ML ACCURACY TREND</b>", " · ".join(_acc_strs)]
            if _last_roc > _first_roc + 0.03:
                sec.append(
                    f"✅ ML model getting smarter — accuracy improved from "
                    f"{_first_roc * 100:.0f}% to {_last_roc * 100:.0f}% this week"
                )
            elif _last_roc < _first_roc - 0.05:
                sec.append(
                    f"⚠️ ML model may be overfitting — "
                    f"accuracy dropped from {_first_roc * 100:.0f}% to {_last_roc * 100:.0f}% — more data needed"
                )
            else:
                sec.append(
                    f"ML model stable — accuracy at {_last_roc * 100:.0f}% "
                    f"({_ml_meta.get('n_trades', '?')} trades)"
                )
            any_added = True
        elif _ml_meta.get("model_ready"):
            _roc = _ml_meta.get("roc_auc", 0)
            _n   = _ml_meta.get("n_trades", 0)
            sec += [
                "", "<b>2. ML ACCURACY TREND</b>",
                f"ML model active — prediction accuracy {_roc * 100:.0f}% on {_n} trades",
                "Trend will show after 2+ weekly retrains",
            ]
            any_added = True
    except Exception:
        pass

    # ── ML HEALTH CHECK ───────────────────────────────────────────────────────
    # Shows whether the model learned real patterns (generalise to new data) or
    # just memorised the training set (overfit / curve-fitted).
    try:
        from src import ml_predictor as _mlp_hc
        _hc = _mlp_hc._load_meta()
        if _hc.get("model_ready") and _hc.get("overfit_gap") is not None:
            _hc_roc   = _hc.get("roc_auc", 0.0)
            _hc_hold  = _hc.get("temporal_holdout_auc", 0.0)
            _hc_gap   = _hc.get("overfit_gap", 0.0)
            _hc_hlthy = _hc.get("is_healthy", True)
            _hc_p_wr  = _hc.get("period_win_rates") or []
            _hc_stbl  = _hc.get("period_stable")

            sec.append("")
            sec.append("<b>ML HEALTH CHECK</b>")
            if _hc_hlthy and _hc_gap < 0.05:
                sec.append(
                    f"✅ The AI model is healthy — its accuracy on new trades "
                    f"({_hc_hold * 100:.0f}%) matches its accuracy on training data "
                    f"({_hc_roc * 100:.0f}%)."
                )
            elif _hc_hlthy:
                sec.append(
                    f"🟡 The AI model is acceptable — slight gap between training accuracy "
                    f"({_hc_roc * 100:.0f}%) and new-trade accuracy ({_hc_hold * 100:.0f}%) "
                    f"— normal for this dataset size."
                )
            else:
                sec.append(
                    f"⚠️ Warning — the AI model appears to have memorised historical "
                    f"patterns that are not repeating — training accuracy "
                    f"{_hc_roc * 100:.0f}% vs new data accuracy {_hc_hold * 100:.0f}% "
                    f"— gathering more data before trusting predictions."
                )
            if len(_hc_p_wr) == 3:
                _wr_strs = [f"{r * 100:.0f}%" for r in _hc_p_wr]
                if _hc_stbl:
                    sec.append(
                        f"✅ Data distribution stable across 3 time periods "
                        f"({' / '.join(_wr_strs)} win rates)"
                    )
                else:
                    sec.append(
                        f"⚠️ Win rate shifted between periods "
                        f"({' / '.join(_wr_strs)}) — historical patterns may not "
                        f"reflect current market conditions"
                    )
            any_added = True
        elif _hc.get("model_ready"):
            sec += [
                "", "<b>ML HEALTH CHECK</b>",
                "Health check will appear once 30+ closed trades are recorded.",
            ]
            any_added = True
    except Exception:
        pass

    # ── 3. MFE TREND ──────────────────────────────────────────────────────────
    closed_all  = [
        r for r in rows
        if r.get("status") in ("WIN", "LOSS", "BREAKEVEN", "EXPIRED", "PARTIAL_WIN")
    ]
    mfe_pairs   = [(r, _f(r.get("mfe_pips"))) for r in closed_all
                   if _f(r.get("mfe_pips")) is not None]
    mfe_sorted  = sorted(mfe_pairs, key=lambda x: _trade_sort(x[0]))

    if len(mfe_sorted) >= 40:
        mfe_groups = []
        for i in range(0, len(mfe_sorted), 20):
            grp = mfe_sorted[i:i + 20]
            avg = round(sum(v for _, v in grp) / len(grp)) if grp else 0
            mfe_groups.append((i + 1, i + len(grp), avg))

        mfe_str = " · ".join(
            f"Trades {s}-{e}: {avg}p avg MFE" for s, e, avg in mfe_groups
        )
        sec += ["", "<b>3. MFE TREND</b>", mfe_str]
        if len(mfe_groups) >= 2 and mfe_groups[-1][2] > mfe_groups[0][2] + 5:
            sec.append(
                f"✅ Entry quality improving — average MFE increased from "
                f"{mfe_groups[0][2]} pips to {mfe_groups[-1][2]} pips over last 20 trades"
            )
        else:
            sec.append("Entry quality consistent across recent trades")
        any_added = True

    # ── TARGET CALIBRATION ANALYSIS ───────────────────────────────────────────
    # Runs always (not gated on trade count) so it shows from the first Monday.
    try:
        def _pip_sz(pair_str: str) -> float:
            cl = pair_str.upper().replace("/", "").replace("-", "")
            if len(cl) >= 6:
                if cl[3:6] == "JPY": return 0.01
                if cl[:3] == "JPY": return 0.000001
            return 0.01 if "JPY" in pair_str.upper() else 0.0001

        _tc_closed = [
            r for r in rows
            if r.get("status") in ("WIN", "LOSS", "EXPIRED", "PARTIAL_WIN")
            and r.get("entry") and r.get("stop_loss") and r.get("target") and r.get("pips")
        ]
        _tc_results = []
        for _tcr in _tc_closed:
            try:
                _tc_entry  = float(_tcr["entry"])
                _tc_stop   = float(_tcr["stop_loss"])
                _tc_target = float(_tcr["target"])
                _tc_pips   = float(_tcr["pips"])
                _tc_ps     = _pip_sz(_tcr["pair"])
                _tc_stop_d = abs(_tc_entry - _tc_stop) / _tc_ps
                _tc_tgt_d  = abs(_tc_target - _tc_entry) / _tc_ps
                if _tc_stop_d <= 0 or _tc_tgt_d <= 0 or _tc_stop_d > 200 or _tc_tgt_d > 500:
                    continue
                _tc_results.append({
                    "status":       _tcr["status"],
                    "stop_pips":    round(_tc_stop_d, 1),
                    "target_pips":  round(_tc_tgt_d, 1),
                    "achieved":     round(_tc_pips, 1),
                    "mfe":          _f(_tcr.get("mfe_pips")),
                })
            except (TypeError, ValueError, ZeroDivisionError):
                pass

        if _tc_results:
            _tc_exp     = [r for r in _tc_results if r["status"] == "EXPIRED"]
            _tc_exp_pos = [r for r in _tc_exp if r["achieved"] > 0]
            _tc_all_tgt = [r["target_pips"] for r in _tc_results]
            _tc_all_stp = [r["stop_pips"] for r in _tc_results]
            _tc_mfe_raw = [r["mfe"] for r in _tc_results if r["mfe"] is not None and r["mfe"] > 0]

            _tc_avg_tgt = sum(_tc_all_tgt) / len(_tc_all_tgt) if _tc_all_tgt else 0
            _tc_avg_stp = sum(_tc_all_stp) / len(_tc_all_stp) if _tc_all_stp else 0
            _tc_avg_mfe = sum(_tc_mfe_raw) / len(_tc_mfe_raw) if _tc_mfe_raw else 0
            _tc_avg_ach = (
                sum(r["achieved"] for r in _tc_exp_pos) / len(_tc_exp_pos)
                if _tc_exp_pos else 0
            )

            # Old multiplier (from data) vs new
            _tc_old_mult = round(_tc_avg_tgt / _tc_avg_stp, 1) if _tc_avg_stp > 0 else 2.0
            _tc_new_mult = 1.0

            # Projected WIN conversion: EXPIRED positives that would WIN at new target
            _tc_new_tgt_p  = _tc_avg_stp * _tc_new_mult   # pips at 1.0x stop
            _tc_conv_would = sum(1 for r in _tc_exp_pos if r["achieved"] >= _tc_new_tgt_p)
            _tc_conv_pct   = (_tc_conv_would / len(_tc_exp_pos) * 100) if _tc_exp_pos else 0

            # PARTIAL_WIN trades that would become full WIN at new target
            _tc_pw_conv = sum(
                1 for r in _tc_results
                if r["status"] == "PARTIAL_WIN" and r["achieved"] >= _tc_new_tgt_p
            )

            # Current decisive rate (WIN+LOSS out of all closed)
            _tc_total  = len(_tc_results)
            _tc_dec    = sum(1 for r in _tc_results if r["status"] in ("WIN", "LOSS"))
            _tc_dec_pct = _tc_dec / _tc_total * 100 if _tc_total else 0
            _tc_proj_dec = _tc_dec + _tc_conv_would + _tc_pw_conv
            _tc_proj_pct = _tc_proj_dec / _tc_total * 100 if _tc_total else 0

            sec += ["", "<b>TARGET CALIBRATION ANALYSIS</b>"]
            if _tc_avg_mfe > 0:
                sec.append(
                    f"Average MFE: {_tc_avg_mfe:.0f} pips — "
                    f"average stop: {_tc_avg_stp:.0f} pips — "
                    f"average target: {_tc_avg_tgt:.0f} pips ({_tc_old_mult}x stop)"
                )
            else:
                sec.append(
                    f"Average stop: {_tc_avg_stp:.0f} pips — "
                    f"average target: {_tc_avg_tgt:.0f} pips ({_tc_old_mult}x stop)"
                )
            if _tc_exp_pos:
                sec.append(
                    f"EXPIRED trades moved avg {_tc_avg_ach:.0f} pips in right direction "
                    f"before expiry — target of {_tc_avg_tgt:.0f} pips was too far"
                )
            sec.append(
                f"Research target reduced: {_tc_old_mult}x ATR → {_tc_new_mult}x ATR "
                f"(fund trade targets unchanged)"
            )
            sec.append(
                f"Projected improvement: {_tc_dec} decisive outcomes → "
                f"~{_tc_proj_dec} decisive outcomes "
                f"({_tc_dec_pct:.0f}% → {_tc_proj_pct:.0f}% decisive rate)"
            )
            if _tc_conv_would > 0 or _tc_pw_conv > 0:
                sec.append(
                    f"✅ ~{_tc_conv_would} expired trades + {_tc_pw_conv} partial wins "
                    f"would convert to full WIN with tighter research target"
                )
            any_added = True
    except Exception:
        pass

    # ── ML TRAINING DATA BALANCE ───────────────────────────────────────────────
    try:
        from src import ml_predictor as _mlp_bal
        _bal_meta   = _mlp_bal._load_meta()
        _bal_wins   = _bal_meta.get("n_wins_training")
        _bal_loss   = _bal_meta.get("n_losses_training")
        _bal_n      = _bal_meta.get("n_trades")
        _bal_biased = _bal_meta.get("is_biased", False)
        _bal_lpct   = _bal_meta.get("prediction_loss_pct")

        sec += ["", "<b>ML TRAINING DATA BALANCE</b>"]
        if _bal_wins is not None and _bal_loss is not None and _bal_n is not None:
            _bal_ratio_str = f"{_bal_wins} WIN + {_bal_loss} LOSS"
            if _bal_biased:
                sec.append(
                    f"⚠️ ML model biased — predicting LOSS on {_bal_lpct}% of training "
                    f"examples ({_bal_ratio_str}) — class weights applied automatically"
                )
            else:
                _bal_r = round(_bal_loss / _bal_wins, 1) if _bal_wins > 0 else None
                sec.append(
                    f"ML training data: {_bal_n} trades used ({_bal_ratio_str}) "
                    f"— PARTIAL_WIN counted as WIN — class weights applied"
                )
                if _bal_r is not None:
                    if _bal_r <= 3.0:
                        sec.append(f"✅ Class ratio {_bal_r}:1 — well balanced training data — model learning genuine patterns")
                    else:
                        sec.append(f"Class ratio {_bal_r}:1 — class weights correcting imbalance — model learning genuine patterns")
        else:
            _raw_pw = sum(1 for r in rows if r.get("status") in ("WIN", "PARTIAL_WIN"))
            _raw_ls = sum(1 for r in rows if r.get("status") == "LOSS")
            sec.append(
                f"Model not yet trained — {_raw_pw} WIN/PARTIAL_WIN + {_raw_ls} LOSS recorded "
                f"(PARTIAL_WIN will count as WIN when training runs)"
            )
        any_added = True
    except Exception:
        pass

    # ── LEARNING VELOCITY ─────────────────────────────────────────────────────
    try:
        from datetime import timedelta as _lv_td
        _lv_cutoff = (datetime.now() - _lv_td(days=7)).strftime("%Y-%m-%d")
        _lv_new = [
            r for r in rows
            if r.get("status") in ("WIN", "LOSS", "PARTIAL_WIN")
            and (r.get("closed_at") or r.get("date") or "")[:10] >= _lv_cutoff
        ]
        _lv_count = len(_lv_new)
        sec += ["", "<b>LEARNING VELOCITY</b>"]
        if _lv_count >= 5:
            sec.append(
                f"✅ This week {_lv_count} new decisive outcomes added — "
                f"learning velocity strong"
            )
        elif _lv_count >= 2:
            sec.append(
                f"This week {_lv_count} new decisive outcomes added — "
                f"learning velocity improving"
            )
        else:
            sec.append(
                f"⚠️ This week {_lv_count} new decisive outcome"
                f"{'s' if _lv_count != 1 else ''} added — learning too slow — "
                f"target calibration should improve this automatically"
            )
        any_added = True
    except Exception:
        pass

    # ── 4. BEST AND WORST PAIRS ────────────────────────────────────────────────
    pair_stats: dict = {}
    for r in decisive:
        p  = r.get("pair", "?")
        ps = pair_stats.setdefault(p, {"wins": 0, "total": 0})
        ps["total"] += 1
        if r.get("status") == "WIN":
            ps["wins"] += 1

    qualified = {p: v for p, v in pair_stats.items() if v["total"] >= 3}
    if qualified:
        def _pwr(v):
            return v["wins"] / v["total"] if v["total"] else 0

        by_wr   = sorted(qualified.items(), key=lambda x: (_pwr(x[1]), x[1]["total"]), reverse=True)
        best3   = by_wr[:3]
        worst3  = [x for x in by_wr[-3:] if x not in best3]

        sec.append("")
        sec.append("<b>4. BEST AND WORST PAIRS</b>")
        if best3:
            sec.append("✅ Best pairs: " + " · ".join(
                f"{p} {int(_pwr(v) * 100)}%" for p, v in best3
            ))
        if worst3:
            sec.append("❌ Weakest pairs: " + " · ".join(
                f"{p} {int(_pwr(v) * 100)}%" for p, v in worst3
            ))
        any_added = True

    # ── 5. CONFIDENCE CALIBRATION ──────────────────────────────────────────────
    conf_stats: dict = {}
    for r in decisive:
        try:
            cv = int(float(r.get("confidence", "") or 0))
        except (TypeError, ValueError):
            continue
        if cv < 5:
            continue
        cs = conf_stats.setdefault(cv, {"wins": 0, "total": 0})
        cs["total"] += 1
        if r.get("status") == "WIN":
            cs["wins"] += 1

    conf_q = {k: v for k, v in conf_stats.items() if v["total"] >= 3}
    _cal_checked = False
    _is_calibrated = False
    if len(conf_q) >= 2:
        def _cwr(v):
            return v["wins"] / v["total"] if v["total"] else 0

        conf_levels = sorted(conf_q.keys())
        conf_wrs    = [_cwr(conf_q[c]) for c in conf_levels]
        # Allow up to 5% tolerance for natural variance
        _is_calibrated = all(
            conf_wrs[i] <= conf_wrs[i + 1] + 0.05
            for i in range(len(conf_wrs) - 1)
        )
        _cal_checked = True
        conf_strs = [f"Conf {c}: {int(_cwr(conf_q[c]) * 100)}%" for c in conf_levels]
        sec += ["", "<b>5. CONFIDENCE CALIBRATION</b>", " · ".join(conf_strs)]
        if _is_calibrated:
            sec.append("✅ Confidence scoring is working")
        else:
            sec.append("⚠️ Confidence scoring needs review")
        any_added = True

    # ── DATA QUALITY REPORT ───────────────────────────────────────────────────
    try:
        from src import data_quality as _dq_lr
        _dq_lr_lines = _dq_lr.weekly_report(_dq_lr.load_state())
        if _dq_lr_lines:
            sec.extend(_dq_lr_lines)
            any_added = True
    except Exception:
        pass

    # ── 6. OVERALL LEARNING VERDICT ────────────────────────────────────────────
    if any_added or n_dec >= 10:
        parts = []
        if n_dec < 10:
            parts.append(
                f"The system has {n_dec} decisive trade outcome{'s' if n_dec != 1 else ''} so far. "
                "Too early to draw conclusions — a minimum of 20 decisive trades is needed before "
                "patterns become meaningful. Keep collecting data."
            )
        else:
            _tot_wins  = sum(1 for r in decisive if r.get("status") == "WIN")
            _overall_wr = _tot_wins / n_dec * 100 if n_dec else 0
            parts.append(
                f"The system has {n_dec} decisive trade outcomes with an overall win rate of "
                f"{_overall_wr:.0f}%."
            )
            if n_dec >= 20:
                _fw = sum(1 for r in dec_sorted[:10] if r.get("status") == "WIN") / 10 * 100
                _lw = sum(1 for r in dec_sorted[-10:] if r.get("status") == "WIN") / 10 * 100
                if _lw > _fw + 4:
                    parts.append(
                        f"Win rate improved from {_fw:.0f}% in the first 10 trades to "
                        f"{_lw:.0f}% in the most recent 10 — the system is learning."
                    )
                else:
                    parts.append(
                        f"Win rate has been {_fw:.0f}% early vs {_lw:.0f}% recently — "
                        "no clear improvement trend yet."
                    )
            if _cal_checked:
                if _is_calibrated:
                    parts.append("Confidence scoring is working as intended.")
                else:
                    parts.append(
                        "Confidence scoring is not yet well calibrated — higher confidence "
                        "setups are not reliably winning more often."
                    )
            if _overall_wr >= 50:
                parts.append(
                    "Overall verdict: system is performing above breakeven — continue operating "
                    "with current settings."
                )
            elif n_dec >= 30:
                parts.append(
                    "Overall verdict: win rate below 50% over 30+ trades — consider reviewing "
                    "the entry criteria for the weakest pairs."
                )
            else:
                parts.append(
                    "Overall verdict: system is still in early learning phase — "
                    "no strategy changes recommended until at least 30 decisive trades are recorded."
                )
        sec += ["", "<b>6. OVERALL LEARNING VERDICT</b>", " ".join(parts)]

    return sec


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

    # Extract drawdown mode early (before _risk_state is fully initialised below)
    # so it can be applied in the yes_trades filter immediately.
    _dd_mode: str = (risk_data or {}).get("risk_state", {}).get("drawdown_mode", "normal")

    # Hard block: compute not-viable pairs (net R:R after costs < 1.3:1)
    _not_viable_pairs: set = set()
    try:
        from src import trade_costs as _tc_nv
        for _r_nv in deep_results:
            try:
                _p_nv = _r_nv["parsed"]
                _dir_nv   = (_p_nv.get("direction") or "").upper()
                _entry_nv = float(_p_nv.get("entry") or _p_nv.get("entry_price") or 0)
                _stop_nv  = float(_p_nv.get("stop_loss") or _p_nv.get("stop") or 0)
                _tgt_nv   = float(_p_nv.get("take_profit") or _p_nv.get("target") or 0)
                if _entry_nv and _stop_nv and _tgt_nv and _dir_nv:
                    _viab_nv = _tc_nv.check_viability(
                        _r_nv["pair"], _dir_nv, _entry_nv, _stop_nv, _tgt_nv
                    )
                    if _viab_nv.get("net_rr", 999) < 1.3:
                        _not_viable_pairs.add(_r_nv["pair"])
            except Exception:
                pass
    except Exception:
        pass

    # ── GLOBAL MARKET REGIME — prime cache and apply overrides ───────────────────
    # Run before _yes_raw assembly so conf_override and size_mult are in effect.
    try:
        from src import market_regime as _mr_scan
        _regime_macro_sigs: dict = {}
        for _r_regime in deep_results:
            try:
                _regime_macro_sigs = _r_regime["bundle"]["macro"]["signals"]
                if _regime_macro_sigs:
                    break
            except (KeyError, TypeError):
                pass
        _grd = _mr_scan.detect(_regime_macro_sigs or None)
        if _grd.get("conf_override"):
            _trade_conf_thr = max(_trade_conf_thr, _grd["conf_override"])
    except Exception:
        pass

    # ── SECOND OPINION: devil's advocate for all 7+ raw-confidence pairs ─────────
    # Runs BEFORE _yes_raw assembly so a confidence reduction (7→6) correctly
    # removes the pair from trade alerts.
    for _da_r in deep_results:
        if _conf(_da_r) >= 7:
            try:
                from src import analyst as _da_analyst
                _da = _da_analyst.devil_advocate(
                    _da_r["pair"],
                    _da_r["parsed"],
                    _da_r.get("bundle", {}),
                )
                _da_r["second_opinion"] = _da
                if _da.get("has_objections"):
                    try:
                        _da_r["parsed"]["confidence"] = max(1, int(_da_r["parsed"]["confidence"]) - 1)
                    except (TypeError, ValueError):
                        pass
                    if _da.get("reasons"):
                        _existing_rf = (_da_r["parsed"].get("risk_factors") or "").strip()
                        _new_rf = "; ".join(_da["reasons"])
                        _da_r["parsed"]["risk_factors"] = (
                            _new_rf + ("; " + _existing_rf if _existing_rf else "")
                        )
                else:
                    _da_r["_da_boost"] = 0.5
            except Exception:
                pass

    # Issue 1: overall confidence is the deciding factor, not individual layer scores.
    # Any pair with 7+ effective confidence qualifies for a trade alert regardless of
    # what the analyst's trade_this field says — confidence overrides individual layers.
    _yes_raw = [
        r for r in deep_results
        if _eff_conf(r) >= _trade_conf_thr
        and r["pair"] not in _demoted_pairs
        and r["pair"] not in _not_viable_pairs
    ]
    # Drawdown tier + grade filtering: stricter tiers require higher grade / more confirmation.
    yes_trades = [
        r for r in _yes_raw
        if _dd_allows_trade(r, _dd_mode, _quality_grades, _trade_conf_thr)
    ]
    # Deduplicate inverse pairs (e.g. USD/CHF SELL + CHF/USD BUY) — keep higher-ranked
    _yt_seen: set = set()
    _yt_deduped = []
    for _r in yes_trades:
        _p = _r["pair"].upper().replace("/", "")
        _inv = _p[3:] + _p[:3]
        if _p not in _yt_seen and _inv not in _yt_seen:
            _yt_deduped.append(_r)
            _yt_seen.add(_p)
    yes_trades = _yt_deduped
    # C-grade demoted to watchlist only in normal mode; restricted tiers skip C entirely
    _c_grade_yes = (
        [r for r in _yes_raw if _quality_grades.get(r["pair"], {}).get("grade") == "C"
         and _eff_conf(r) < _trade_conf_thr]
        if _dd_mode == "normal" else []
    )
    _df_grade_yes = [r for r in _yes_raw if _quality_grades.get(r["pair"], {}).get("grade") in ("D", "F")]

    _yes_pair_set = {r["pair"] for r in _yes_raw}

    watch_list = sorted(
        [r for r in deep_results
         if r["pair"] not in _yes_pair_set
         and 5 <= _eff_conf(r) <= 6
         and _quality_grades.get(r["pair"], _trade_quality_grade(r)).get("grade") not in ("D", "F")
        ] + _c_grade_yes,
        key=_eff_conf, reverse=True,
    )[:4]   # allow one extra slot for demoted C-grade alerts
    # Deduplicate inverse pairs from watch list vs yes_trades and within itself
    _wl_seen: set = {r["pair"].upper().replace("/", "") for r in yes_trades}
    _wl_deduped = []
    for _r in watch_list:
        _p = _r["pair"].upper().replace("/", "")
        _inv = _p[3:] + _p[:3]
        if _p not in _wl_seen and _inv not in _wl_seen:
            _wl_deduped.append(_r)
            _wl_seen.add(_p)
    watch_list = _wl_deduped

    near_misses = sorted(
        [r for r in deep_results
         if r["pair"] not in _yes_pair_set
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

    # Pre-compute drawdown banner (non-empty when in a protection tier)
    _dd_banner = ""
    try:
        if risk_data:
            from src import risk_manager as _rm_banner
            _dd_banner = _rm_banner.drawdown_header_line(
                _risk_state, risk_data.get("profile", {})
            )
    except Exception:
        pass

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
        "prelondon": "🌆 5PM PRE-LONDON CHECK",
        "preny":     "🌃 11PM PRE-NEW YORK CHECK",
    }
    _badge = _badge_map.get(scan_mode, "🤖 Forex AI")

    if yes_trades:
        setup_line = f"<b>🟢 {len(yes_trades)} setup{'s' if len(yes_trades) > 1 else ''} found</b>"
    elif _dd_mode == "halt":
        setup_line = "🚨 <b>HALT MODE — no new trades</b>"
    elif _dd_mode in ("preservation", "defensive", "caution"):
        _tier_limit = {"preservation": "A + all TFs", "defensive": "A-grade", "caution": "A/B-grade"}.get(_dd_mode, "")
        setup_line = f"No qualifying setups ({_tier_limit} required)"
    else:
        setup_line = "No setups today"

    # ── Nested helpers ────────────────────────────────────────────────────────

    def _fundamental_lines(result: dict, compact: bool = False) -> list:
        """Return Telegram lines for fundamental alignment.  Computes and caches alignment."""
        fa = result.get("_fundamental_alignment")
        if fa is None:
            _fa_p = result.get("pair", "")
            _fa_d = (result.get("parsed", {}).get("direction") or "").upper()
            if _fa_p and "/" in _fa_p and _fa_d:
                try:
                    from src import fundamentals as _fund_fl
                    _fb, _fq = _fa_p.split("/")
                    fa = _fund_fl.get_fundamental_alignment(_fb, _fq, _fa_d)
                    result["_fundamental_alignment"] = fa
                except Exception:
                    fa = {}
            else:
                fa = {}
        if not fa:
            return []

        alignment   = fa.get("alignment", "MIXED")
        conf_adj    = fa.get("conf_adj", 0)
        aligned     = fa.get("aligned", 0)
        opposed     = fa.get("opposed", 0)
        cb_s        = fa.get("cb_score", 0)
        carry_s     = fa.get("carry_score", 0)
        econ_s      = fa.get("econ_score", 0)

        def _tk(s):
            return "✅" if s > 0 else ("❌" if s < 0 else "➖")

        if alignment == "TAILWIND":
            hdr = f"🌊 <b>FUNDAMENTAL TAILWIND</b> — {aligned}/3 factors aligned (conf +1)"
        elif alignment == "HEADWIND":
            hdr = f"⛔ <b>FIGHTING THE FUNDAMENTALS</b> — {opposed}/3 factors opposed (conf −1)"
        else:
            hdr = f"📊 <b>Mixed fundamentals</b> — {aligned}/3 aligned"

        if compact:
            tick_str = f"CB {_tk(cb_s)} · Carry {_tk(carry_s)} · Econ {_tk(econ_s)}"
            cb_b = fa.get("cb_base",  "neutral")
            cb_q = fa.get("cb_quote", "neutral")
            pair = result.get("pair", "?")
            base_c = pair.split("/")[0] if "/" in pair else "?"
            quot_c = pair.split("/")[1] if "/" in pair else "?"
            adj_str = f" · conf {'−1' if conf_adj < 0 else ('+1' if conf_adj > 0 else '±0')}"
            return [
                f"{hdr}",
                f"   {tick_str} · {base_c} {cb_b} CB · {quot_c} {cb_q} CB{adj_str}",
            ]

        # Full detail for trade blocks — plain English
        pair   = result.get("pair", "?")
        dirn   = (result.get("parsed", {}).get("direction") or "?").upper()
        base_c = pair.split("/")[0] if "/" in pair else "?"
        quot_c = pair.split("/")[1] if "/" in pair else "?"

        cb_base_b  = fa.get("cb_base",  "neutral")
        cb_quot_b  = fa.get("cb_quote", "neutral")
        cb_note_b  = fa.get("cb_note_base",  "")
        cb_note_q  = fa.get("cb_note_quote", "")
        carry_diff = fa.get("carry_diff", 0)
        econ_base  = fa.get("econ_base",  "neutral")
        econ_quote = fa.get("econ_quote", "neutral")

        _CB_NAMES = {
            "USD": "US Federal Reserve", "EUR": "European Central Bank",
            "GBP": "Bank of England",    "JPY": "Bank of Japan",
            "AUD": "Reserve Bank of Australia", "NZD": "Reserve Bank of New Zealand",
            "CAD": "Bank of Canada",     "CHF": "Swiss National Bank",
        }
        _CCY_NAMES = {
            "USD": "US Dollar",       "EUR": "Euro",          "GBP": "British Pound",
            "JPY": "Japanese Yen",    "AUD": "Australian Dollar", "NZD": "New Zealand Dollar",
            "CAD": "Canadian Dollar", "CHF": "Swiss Franc",   "HKD": "Hong Kong Dollar",
            "SGD": "Singapore Dollar","NOK": "Norwegian Krone","SEK": "Swedish Krona",
        }

        def _cb_plain(bias, note, ccy):
            cb_name = _CB_NAMES.get(ccy, f"{ccy} central bank")
            if bias == "hawkish":
                act = note if note else "raising interest rates"
                return f"{cb_name} is {act} — favours {ccy}"
            if bias == "dovish":
                act = note if note else "cutting interest rates"
                return f"{cb_name} is {act} — works against {ccy}"
            return f"{cb_name} is holding rates steady — neutral for {ccy}"

        def _econ_plain(surprise, ccy):
            ccy_name = _CCY_NAMES.get(ccy, ccy)
            if surprise in ("strong", "positive"):
                return f"{ccy_name} economic data has been stronger than expected recently"
            if surprise in ("weak", "negative"):
                return f"{ccy_name} economic data has been weaker than expected recently"
            return f"{ccy_name} economic data has been mixed recently"

        carry_helps   = (carry_diff > 0 and dirn == "BUY") or (carry_diff < 0 and dirn == "SELL")
        carry_hurts   = (carry_diff < 0 and dirn == "BUY") or (carry_diff > 0 and dirn == "SELL")
        trade_ccy     = base_c if dirn == "BUY" else quot_c

        lines = [hdr, "📊 <b>Economic backdrop:</b>"]

        # Central bank
        _cb_desc = _cb_plain(cb_base_b, cb_note_b, base_c)
        lines.append(f"   {_tk(cb_s)} <b>Central bank:</b> {_cb_desc}")

        # Carry / interest rate advantage
        if carry_helps:
            lines.append(
                f"   {_tk(carry_s)} <b>Interest rate advantage:</b> You earn interest daily holding {trade_ccy} — adds to this trade"
            )
        elif carry_hurts:
            lines.append(
                f"   {_tk(carry_s)} <b>Interest rate advantage:</b> Holding {trade_ccy} costs money daily — works against this trade"
            )
        else:
            lines.append(
                f"   {_tk(carry_s)} <b>Interest rate advantage:</b> Carry is neutral — no daily cost advantage"
            )

        # Economic data
        _econ_desc = _econ_plain(econ_base, base_c)
        lines.append(f"   {_tk(econ_s)} <b>Economic data:</b> {_econ_desc}")
        return lines

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

        # Economic calendar: if either currency has a HIGH impact event within
        # 48 hours, reduce displayed confidence by 1 and store warning lines.
        _cal_warn_lines = []
        try:
            from src import economic_calendar as _ec_tb
            _cal_ev_48h = _ec_tb.events_for_pair(pair, hours=48)
            if _cal_ev_48h:
                try:
                    _conf_display = str(max(1, int(_conf_display) - 1))
                except (TypeError, ValueError):
                    pass
                _cal_warn_lines = _ec_tb.warning_lines_for_pair(pair, _cal_ev_48h)
        except Exception:
            pass

        # Second opinion boost: if DA found no compelling objections, show +0.5 on display
        _so_tb = r.get("second_opinion")
        if _so_tb and not _so_tb.get("has_objections"):
            try:
                _conf_display = f"{float(_conf_display) + 0.5:.1f}"
            except (TypeError, ValueError):
                pass

        _qg_tb = _quality_grades.get(pair, _trade_quality_grade(r))
        _tb_grade = (_qg_tb or {}).get("grade", "B")

        # Plain English description of what the trade is betting on
        _CCY_FULL_TB = {
            "USD": "US Dollar", "EUR": "Euro", "GBP": "British Pound",
            "JPY": "Japanese Yen", "AUD": "Australian Dollar", "NZD": "New Zealand Dollar",
            "CAD": "Canadian Dollar", "CHF": "Swiss Franc", "HKD": "Hong Kong Dollar",
            "SGD": "Singapore Dollar", "NOK": "Norwegian Krone", "SEK": "Swedish Krona",
        }
        _base_tb = pair.split("/")[0] if "/" in pair else pair[:3]
        _quot_tb = pair.split("/")[1] if "/" in pair else pair[3:]
        _base_nm_tb = _CCY_FULL_TB.get(_base_tb, _base_tb)
        _quot_nm_tb = _CCY_FULL_TB.get(_quot_tb, _quot_tb)
        if direction == "BUY":
            _plain_desc_tb = f"Betting the {_base_nm_tb} will strengthen against the {_quot_nm_tb}"
        else:
            _plain_desc_tb = f"Betting the {_base_nm_tb} will weaken against the {_quot_nm_tb}"

        if _tb_grade == "A":
            _grade_alert_hdr = "🚨 <b>TRADE ALERT — GRADE A</b>"
            _action_line = f"{action_icon} <b>{_pfx}{direction} {pair} NOW — Grade A · Confidence {_conf_display}/10</b>"
        elif _tb_grade == "B":
            _grade_alert_hdr = "⚡ <b>TRADE ALERT — GRADE B</b>"
            _action_line = f"🟡 <b>{_pfx}{direction} {pair} — Grade B · Confidence {_conf_display}/10</b>"
        else:
            _grade_alert_hdr = None
            _action_line = f"👁 <b>{_pfx}WATCH ONLY: {direction} {pair} — Grade {_tb_grade}</b>"

        block = [""]
        if _grade_alert_hdr:
            block.append(_grade_alert_hdr)
            block.append("")
        block += [
            _action_line,
            f"({_plain_desc_tb})",
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
            _lots_display = sz["lots"]
            try:
                from src import market_regime as _mr_sz
                if _mr_sz.detect().get("regime") == "ranging_high_vol":
                    _lots_display = max(0.01, round(_lots_display * 0.5, 2))
            except Exception:
                pass
            block.append(f"📏 Position Size: {_lots_display} lots")

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

        # ── Calendar news warning ────────────────────────────────────────────────
        if _cal_warn_lines:
            block.append("")
            for _cw in _cal_warn_lines:
                block.append(_cw)

        # ── Plain English outcome summary ─────────────────────────────────────
        if risk_amt and profit_amt:
            block += [
                "",
                "💡 <b>What this trade means:</b>",
                f"If price reaches target: <b>+${profit_amt:,.0f} profit</b> on this trade",
                f"If stop loss is hit: <b>-${risk_amt:,.0f} loss</b> on this trade",
                f"Net risk to reward: you risk ${risk_amt:,.0f} to potentially make ${profit_amt:,.0f}",
            ]

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

        # For 7+ confidence: find the nearest Fibonacci support (BUY) or resistance
        # (SELL) closest to Claude's entry price and use it as the ideal entry level.
        _is_high_conf = False
        try:
            _is_high_conf = int(conf) >= 7
        except (TypeError, ValueError):
            pass

        _fib_entry_price = entry_raw
        _fib_entry_label = None
        if _is_high_conf and isinstance(_fib, dict) and _fib.get("status") == "ok":
            _fib_lvls = (
                _fib.get("nearest_below", []) if direction == "BUY"
                else _fib.get("nearest_above", [])
            )
            if _fib_lvls:
                try:
                    _best_fib = min(
                        _fib_lvls,
                        key=lambda lp: abs(float(lp[1]) - float(entry_raw)),
                    )
                    _fib_entry_price = _best_fib[1]
                    _fib_entry_label = str(_best_fib[0])
                except (TypeError, ValueError, IndexError):
                    pass

        _cur_price = None
        try:
            _cp_raw = (
                _tb_daily.get("last_close") or _tb_daily.get("close")
                if isinstance(_tb_daily, dict) else None
            )
            if _cp_raw:
                _cur_price = float(_cp_raw)
        except (TypeError, ValueError):
            pass

        block.append("━━━━━━━━━━━━━━━━━━━━━")
        if _is_high_conf and _fib_entry_price is not None and _cur_price is not None:
            _pb_move_word   = "dip"    if direction == "BUY" else "rally"
            _pb_participant = "buyers" if direction == "BUY" else "sellers"
            _pb_chase_word  = "higher" if direction == "BUY" else "lower"

            # Pips between current price and pullback entry level
            _pb_dist_pips = None
            try:
                _pb_dist_pips = round(abs(_cur_price - float(_fib_entry_price)) / _tb_pip)
            except (TypeError, ValueError, ZeroDivisionError):
                pass

            # True when price already ran through the entry without pulling back
            _pb_past_entry = False
            try:
                _pb_past_entry = (
                    float(_cur_price) <= float(_fib_entry_price) if direction == "BUY"
                    else float(_cur_price) >= float(_fib_entry_price)
                )
            except (TypeError, ValueError):
                pass

            # R:R entering at current market price
            _pb_rr_market_str = None
            try:
                if direction == "BUY":
                    _pb_rr_mkt = abs(float(adj_tgt) - _cur_price) / abs(_cur_price - float(adj_stop))
                else:
                    _pb_rr_mkt = abs(_cur_price - float(adj_tgt)) / abs(float(adj_stop) - _cur_price)
                _pb_rr_market_str = f"{_pb_rr_mkt:.1f}:1"
            except (TypeError, ValueError, ZeroDivisionError):
                pass

            # R:R entering at the pullback level (only valid if pullback is inside the stop)
            _pb_rr_pullback_str = None
            try:
                _fep = float(_fib_entry_price)
                _ast = float(adj_stop)
                _at  = float(adj_tgt)
                if direction == "BUY" and _fep > _ast:
                    _pb_rr_pullback_str = f"{abs(_at - _fep) / abs(_fep - _ast):.1f}:1"
                elif direction == "SELL" and _fep < _ast:
                    _pb_rr_pullback_str = f"{abs(_fep - _at) / abs(_ast - _fep):.1f}:1"
            except (TypeError, ValueError, ZeroDivisionError):
                pass

            # Confluence: is the 20-period swing high/low within 15 pips of the fib level?
            _pb_why = None
            try:
                _pb_swing_key  = "recent_low_20"  if direction == "BUY" else "recent_high_20"
                _pb_swing_type = "previous swing low" if direction == "BUY" else "previous swing high"
                _pb_swing_val  = float(_tb_daily.get(_pb_swing_key) or 0)
                _pb_thresh     = _tb_pip * 15
                _fep_f = float(_fib_entry_price)
                if _pb_swing_val > 0 and abs(_pb_swing_val - _fep_f) <= _pb_thresh:
                    if _fib_entry_label:
                        _pb_why = (
                            f"Fibonacci {_fib_entry_label} retracement AND {_pb_swing_type} "
                            f"both sit at {_fmt_price(_fib_entry_price)} — this is where "
                            f"{_pb_participant} are likely to step in strongly"
                        )
                    else:
                        _pb_why = (
                            f"Key Fibonacci retracement AND {_pb_swing_type} "
                            f"both sit at {_fmt_price(_fib_entry_price)} — this is where "
                            f"{_pb_participant} are likely to step in strongly"
                        )
                else:
                    if _fib_entry_label:
                        _pb_why = (
                            f"Fibonacci {_fib_entry_label} retracement sits at "
                            f"{_fmt_price(_fib_entry_price)} — this is where "
                            f"{_pb_participant} are likely to step in"
                        )
                    else:
                        _pb_why = (
                            f"Key price level at {_fmt_price(_fib_entry_price)} — "
                            f"this is where {_pb_participant} are likely to step in"
                        )
            except (TypeError, ValueError):
                _pb_why = (
                    f"Key price level at {_fmt_price(_fib_entry_price)} — "
                    f"this is where {_pb_participant} are likely to step in"
                )

            if _pb_past_entry:
                block += [
                    "⏳ <b>PULLBACK ENTRY</b>",
                    "Price ran away without a pullback — skip this trade — "
                    "wait for the next pullback opportunity on this pair",
                ]
            else:
                _pb_dip_str = (
                    f"price needs to {_pb_move_word} {_pb_dist_pips} pips to the ideal level"
                    if _pb_dist_pips else
                    f"price needs to {_pb_move_word} to this level"
                )
                block += [
                    f"⏳ <b>PULLBACK ENTRY</b> — do not enter at current price",
                    f"Current price: {_fmt_price(_cur_price)}",
                    f"Ideal entry level: {_fmt_price(_fib_entry_price)} — {_pb_dip_str}",
                    f"Why this level: {_pb_why}",
                    "",
                    f"If price reaches {_fmt_price(_fib_entry_price)} — enter immediately",
                    f"If price does not reach {_fmt_price(_fib_entry_price)} by {_ew_cut} Auckland — skip this trade entirely",
                ]
                if _pb_rr_pullback_str and _pb_rr_market_str:
                    block.append(
                        f"Do not chase price {_pb_chase_word} — a patient entry at "
                        f"{_fmt_price(_fib_entry_price)} gives you R:R of {_pb_rr_pullback_str} "
                        f"vs only {_pb_rr_market_str} if you enter now"
                    )
                else:
                    block.append(
                        f"Do not chase price {_pb_chase_word} — "
                        "wait for the pullback to get the best risk to reward"
                    )
        else:
            block += [
                f"⏰ <b>EXACT ENTRY INSTRUCTIONS:</b>",
                f"{_eq_em} {_eq_lb}",
                f"⏰ ENTER TRADE: Between {_ew_win} Auckland {_tref_tb}",
                f"⛔ DO NOT ENTER after {_ew_cut} Auckland",
                f"⚡ IDEAL ENTRY: Wait for price to {_ideal_verb} {_fmt_price(entry_raw)} then enter",
                f"- Do NOT enter if price moves more than 30 pips from entry before {_ew_ses}",
                "- If price gaps past entry on open — skip this trade entirely",
            ]
        block += [
            "━━━━━━━━━━━━━━━━━━━━━",
            f"📈 Confidence: {_conf_display}/10  {_conf_bar(_conf_display)}",
        ]
        # MTF timeframe display line
        try:
            _mtf_tb      = r.get("bundle", {}).get("mtf", {})
            _mtf_gate_tb = _mtf_tb.get("mtf_gate", "") if isinstance(_mtf_tb, dict) else ""
            _mtf_sigs_tb = _mtf_tb.get("signals", {}) if isinstance(_mtf_tb, dict) else {}
            if _mtf_gate_tb and _mtf_sigs_tb:
                def _sig_word(sig, tf_for_neutral):
                    if sig in ("BUY", "SELL"):
                        return sig
                    return "consolidating (neutral)" if tf_for_neutral == "daily" else "neutral"
                _w_word   = _sig_word(_mtf_sigs_tb.get("weekly", "NEUTRAL"), "weekly")
                _d_word   = _sig_word(_mtf_sigs_tb.get("daily",  "NEUTRAL"), "daily")
                _h4_word  = _sig_word(_mtf_sigs_tb.get("h4",     "NEUTRAL"), "h4")
                _tf_bar   = f"Timeframes: Weekly {_w_word} · Daily {_d_word} · 4H {_h4_word}"
                _dir_word = "uptrend" if direction == "BUY" else "downtrend"
                if _mtf_gate_tb == "strong_all3":
                    _tf_tag = "all timeframes aligned — highest quality setup"
                elif _mtf_gate_tb == "strong_w_d":
                    _tf_tag = "weekly and daily aligned — valid setup"
                elif _mtf_gate_tb == "strong_w_4h":
                    _tf_tag = f"classic pullback entry within weekly {_dir_word} — high quality setup"
                elif _mtf_gate_tb in ("weak_weekly_only", "weak_mixed"):
                    _tf_tag = "weekly trend only — reduced confidence — watch list"
                else:
                    _tf_tag = None
                if _tf_tag:
                    block.append(f"📊 {_tf_bar} — {_tf_tag}")
        except Exception:
            pass
        # Second opinion summary line
        _so_tb = r.get("second_opinion")
        if _so_tb:
            if _so_tb.get("has_objections"):
                _so_nc = _so_tb.get("n_compelling", 0)
                block.append(f"⚠️ Second opinion flagged {_so_nc} concern(s) — see risk factors below.")
            else:
                block.append("🔍 Second opinion: No major red flags found — confidence boosted.")
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
        # COT reversal warning
        if _cot_reversal_penalty(r) < 0:
            block.append(
                "🔄 <b>COT REVERSAL WARNING — institutional positioning just flipped against "
                "this trade direction — confidence penalised −1</b>"
            )
        # Smart Money Divergence (Layer 10) — plain English
        _smd_tb   = _smd_score(r)
        _smd_data = r.get("bundle", {}).get("smart_money", {})
        if isinstance(_smd_data, dict) and _smd_data.get("status") not in ("insufficient_data", None):
            _smd_bi  = float(_smd_data.get("base_inst",   0) or 0)
            _smd_qi  = float(_smd_data.get("quote_inst",  0) or 0)
            _smd_br  = float(_smd_data.get("base_retail", 0) or 0)
            _smd_qr  = float(_smd_data.get("quote_retail",0) or 0)
            _smd_base_ccy  = pair.split("/")[0] if "/" in pair else "BASE"
            _smd_quote_ccy = pair.split("/")[1] if "/" in pair else "QUOTE"

            def _smd_action(score):
                if score > 0.3:  return "buying"
                if score < -0.3: return "selling"
                return "neutral on"

            def _smd_retail_stance(score):
                if score > 0.3:  return "very bullish"
                if score > 0.1:  return "mildly bullish"
                if score < -0.3: return "very bearish"
                if score < -0.1: return "mildly bearish"
                return "neutral"

            # Describe the most prominent institutional signal
            if abs(_smd_bi) >= abs(_smd_qi):
                _smd_desc_ccy  = _smd_base_ccy
                _smd_desc_act  = _smd_action(_smd_bi)
                _smd_desc_ret  = _smd_retail_stance(_smd_br)
            else:
                _smd_desc_ccy  = _smd_quote_ccy
                _smd_desc_act  = _smd_action(_smd_qi)
                _smd_desc_ret  = _smd_retail_stance(_smd_qr)

            _smd_conflicts = (
                (_smd_tb >= 5 and direction == "SELL") or (_smd_tb <= -5 and direction == "BUY")
            )
            _smd_supports  = (
                (_smd_tb >= 5 and direction == "BUY")  or (_smd_tb <= -5 and direction == "SELL")
            )
            _smd_icon = "🐋" if abs(_smd_tb) >= 8 else ("📊" if abs(_smd_tb) >= 5 else "💡")

            block.append(
                f"{_smd_icon} <b>Smart Money Signal:</b> Big investors are {_smd_desc_act} "
                f"{_smd_desc_ccy} while regular traders are {_smd_desc_ret}"
            )
            if _smd_conflicts:
                block.append(f"This conflicts with our {direction} signal — treat with extra caution")
                block.append("When big money and our signal disagree the risk is higher")
            elif _smd_supports and abs(_smd_tb) >= 8:
                block.append(f"This strongly supports our {direction} signal — institutional confirmation")
                block.append("When big money and our signal agree the probability of success is higher")
            elif _smd_supports and abs(_smd_tb) >= 5:
                block.append(f"Mild institutional support for our {direction} signal")
        # Fundamental alignment
        block += _fundamental_lines(r, compact=False)
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
        _qg_we = _quality_grades.get(rr["pair"]) or _trade_quality_grade(rr)
        if not _qg_we or _qg_we.get("grade") == "F":
            return []
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
        lines = [
            "",
            f"{arrow} <b>{rr['pair']}</b> {dirn}  {conf}/10 {_conf_bar(conf)}  {_eq_we_e} {_eq_we_l}",
            _grade_display_line(_qg_we),
            f"{_score_breakdown_line(pp, smd=_smd_score(rr))}",
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
        # COT reversal warning
        if _cot_reversal_penalty(rr) < 0:
            lines.append(
                "🔄 <b>COT REVERSAL WARNING — institutional positioning just flipped — "
                "confidence penalised −1</b>"
            )
        # Smart Money Divergence (compact)
        _smd_we   = _smd_score(rr)
        _smd_d_we = rr.get("bundle", {}).get("smart_money", {})
        if isinstance(_smd_d_we, dict) and _smd_d_we.get("status") not in ("insufficient_data", None):
            _smd_sig_we = _smd_d_we.get("signal", "NEUTRAL")
            _smd_icon_we = "🐋" if abs(_smd_we) >= 8 else "📊"
            _smd_boost_we = " +1 conf" if (
                (_smd_we >= 8 and dirn == "BUY") or (_smd_we <= -8 and dirn == "SELL")
            ) else ""
            _smd_warn_we = " ⚠️ contradicts trade" if (
                (_smd_we >= 8 and dirn == "SELL") or (_smd_we <= -8 and dirn == "BUY")
            ) else ""
            lines.append(
                f"{_smd_icon_we} SMD: {_smd_we:+d}/10 [{_smd_sig_we}]{_smd_boost_we}{_smd_warn_we}"
            )
        # Fundamental alignment (compact)
        lines += _fundamental_lines(rr, compact=True)
        # D/F grade: monitoring only — suppress all entry instructions
        if (_qg_we or {}).get("grade") == "F":
            return []
        if (_qg_we or {}).get("grade") == "D":
            lines.append("⚠️ Grade D — worth monitoring — conditions improving but not ready to enter yet")
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
        if (_quality_grades.get(rr["pair"]) or _trade_quality_grade(rr)).get("grade") == "F":
            return []
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
        if _qg_ap["grade"] == "D":
            _ntc_ap   = _what_needs_to_change(pp)
            _bar_n    = max(1, min(conf, 6))
            _bar      = "█" * _bar_n + "░" * (7 - _bar_n)
            _rr_d_str = ""
            if ind_e and ind_s and ind_t:
                try:
                    _risk_d = abs(float(ind_e) - float(ind_s))
                    _prof_d = abs(float(ind_t) - float(ind_e))
                    if _risk_d:
                        _rr_d_str = f" · R:R {round(_prof_d / _risk_d, 1)}:1"
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
            _ew_d    = _entry_window_for_pair(rr["pair"])
            _ses_d   = _ew_d[6] if len(_ew_d) > 6 else ""
            _time_d  = _fmt_time_exact(_ew_d[0], _ew_d[1])
            return [
                "",
                f"<b>{rr['pair']}</b> · Grade D · {conf}/10 {_bar} — ({_ntc_ap}) — Getting closer — not ready yet{_rr_d_str} — ⏰ Check at {_ses_d} {_time_d} Auckland",
            ]
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
        # ── DATA QUALITY ASSESSMENT — runs before message assembly so alerts fire first
        _dq_quality: dict = {}
        _dq_state:   dict = {}
        try:
            from src import data_quality as _dq
            _dq_quality = _dq.assess_scan(deep_results)
            _dq_state   = _dq.update_state(_dq_quality, scan_mode)
            # Immediate alert: >8 candle failures
            _dq_hv = _dq.high_volume_alert(_dq_quality)
            if _dq_hv:
                try:
                    _telegram(_dq_hv)
                except Exception:
                    pass
            # Immediate alerts: consecutive data source failures
            for _dq_alert in _dq.consecutive_alerts(_dq_state):
                try:
                    _telegram(_dq_alert)
                except Exception:
                    pass
        except Exception:
            pass

        _hdr_full = [
            f"<b>🤖 FOREX AI — 🌅 {_fmt_date_nz(now_ak)}</b>",
            f"Universe: {universe_size} · Deep analysed: <b>{n_deep}</b> · {setup_line}",
        ]
        all_sections.append(_hdr_full)
        if _dd_banner:
            all_sections.append(["", "━━━━━━━━━━━━━━━━━━━━━", _dd_banner])

        # OPEN TRADES — always at the top so it's the first thing seen
        _ot_conf_map = {r["pair"]: _eff_conf(r) for r in deep_results if r.get("pair")}
        # Confidence crisis: send immediate separate alert before main message
        for _cca_t in _ot_open_trades:
            _cca_p  = _cca_t.get("pair", "")
            _cca_ec = None
            try:
                _cca_ec = int(float(_cca_t.get("confidence") or 0)) or None
            except (TypeError, ValueError):
                pass
            _cca_cc = _ot_conf_map.get(_cca_p)
            if _cca_ec and _cca_cc is not None and _cca_cc < 3 and _cca_ec >= 3:
                try:
                    _telegram(
                        f"⚠️ Open trade warning — {_cca_p} confidence has fallen to "
                        f"{_cca_cc}/10 — the original trade rationale may no longer be valid "
                        f"— consider whether to exit early to avoid a larger loss"
                    )
                except Exception:
                    pass
        all_sections.append(_build_open_trades_section(_ot_open_trades, _ot_px_cache, now_ak, cur_conf_map=_ot_conf_map))

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
        _roff_ctx = "risk-off" in env_str.lower()
        _safe_h = {"JPY", "CHF", "USD"}
        _comm_c = {"AUD", "NZD"}
        if sc:
            if _roff_ctx and sc in _comm_c:
                sc_reason = "data shows strength but risk-off conditions favour safe havens"
                ctx_lines.append(f"💪 Strongest: <b>{sc}</b> — {sc_reason} (+{scores.get(sc,0):.0f})")
                ctx_lines.append("⚠️ Risk-OFF environment — JPY, CHF, USD typically outperform — verify before trading AUD/NZD")
            elif _roff_ctx and sc in _safe_h:
                sc_reason = "safe haven demand — expected in risk-off environment"
                ctx_lines.append(f"💪 Strongest: <b>{sc}</b> — {sc_reason} (+{scores.get(sc,0):.0f})")
            else:
                sc_reason = "carry + risk-on" if "risk-on" in env_str else "carry + fundamentals"
                ctx_lines.append(f"💪 Strongest: <b>{sc}</b> — {sc_reason} (+{scores.get(sc,0):.0f})")
        if wc:
            if _roff_ctx:
                wc_reason = "risk-off selling pressure" if wc in _comm_c else "weak fundamentals"
            else:
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
        # Market regime — plain-English macro environment summary
        try:
            from src import market_regime as _mr_ctx
            _rd_ctx = _mr_ctx.detect()
            _regime_line = _mr_ctx.telegram_line(_rd_ctx)
            if _regime_line:
                ctx_lines.append(_regime_line)
                if _rd_ctx.get("conf_override"):
                    ctx_lines.append(
                        f"ℹ️ <b>High volatility mode active</b> — "
                        f"only confidence {_rd_ctx['conf_override']}+ trades taken, "
                        f"position sizes halved."
                    )
        except Exception:
            pass
        _ps = _patience["score"]
        _pd = _patience["description"]
        ctx_lines.append(f"📊 <b>Today's trading conditions: {_ps}/10</b> — {_pd}")
        # Threshold display — always show so investor knows the current bar to clear
        try:
            from src import threshold_manager as _tm_ctx
            _ctx_thr = _tm_ctx.get_confidence_threshold()
            if _ctx_thr != 7:
                _thr_reason = (
                    f"raised to {_ctx_thr} due to high-volatility market regime"
                    if _ctx_thr > 7 else f"currently {_ctx_thr}"
                )
                ctx_lines.append(f"📊 <b>Today's trade threshold: {_ctx_thr}/10</b> — {_thr_reason}")
        except Exception:
            pass
        all_sections.append(ctx_lines)

        # ECONOMIC CALENDAR — 7-day high impact event timeline (every 6am scan)
        try:
            from src import economic_calendar as _ec_cal
            _cal_section = _ec_cal.build_calendar_section()
            if _cal_section:
                all_sections.append(_cal_section)
            else:
                _raw_ev = _ec_cal.get_events_7d()
                print(
                    f"[ECO-CAL] build_calendar_section() returned empty — "
                    f"get_events_7d() returned {len(_raw_ev)} events after HIGH-impact filter",
                    file=sys.stderr,
                )
                all_sections.append([
                    "", "━━━━━━━━━━━━━━━━━━━━━",
                    "📅 <b>UPCOMING HIGH IMPACT EVENTS</b>",
                    "No high-impact events scheduled for the next 7 days — clear sailing ahead",
                ])
        except Exception as _ec_err:
            import traceback as _tb_cal
            print(f"[ECO-CAL] Exception in economic calendar: {_ec_err}", file=sys.stderr)
            print(_tb_cal.format_exc(), file=sys.stderr)

        # SYSTEM LEARNING REPORT (Monday 6am only)
        _slr = _build_system_learning_report(date)
        if _slr:
            all_sections.append(_slr)

        # TRADE ALERTS
        if yes_trades:
            for r in yes_trades:
                all_sections.append(_trade_block(r))

        # WHY NO MORE SETUPS
        if not yes_trades:
            no_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "💤 <b>WHY NO MORE SETUPS TODAY</b>"]
            if _dd_mode == "halt":
                no_sec += [
                    "🚨 <b>HALT MODE ACTIVE</b> — all new trades suspended.",
                    "Account has reached 10%+ drawdown from peak. No new positions.",
                    "Review open trades and wait for drawdown recovery before resuming.",
                ]
            elif _dd_mode in ("preservation", "defensive", "caution"):
                _tier_req = {"preservation": "A-grade + all 3 TFs aligned",
                             "defensive": "A-grade setups", "caution": "A/B-grade setups"}.get(_dd_mode)
                _tier_icon = {"preservation": "🔴", "defensive": "🟠", "caution": "⚠️"}.get(_dd_mode, "⚠️")
                no_sec.append(f"{_tier_icon} Drawdown protection active — only {_tier_req} accepted. No qualifying setups found today.")
            else:
                def _s1_sort_key(rr):
                    return float(rr.get("screen", {}).get("score") or 0)
                combined = list(near_misses) + sorted(stage1_filtered, key=_s1_sort_key, reverse=True)
                top3     = combined[:3]

                def _plain_rejection(rr) -> str:
                    """Convert rejection reason to plain English for non-traders."""
                    conf_v  = _eff_conf(rr)
                    dirn_v  = (rr.get("parsed", {}).get("direction") or "").upper()
                    parsed  = rr.get("parsed", {})
                    if rr.get("screened_out"):
                        return "Technical indicators not strong enough — the system needs more confirmation before considering this pair"
                    mtf = rr.get("bundle", {}).get("mtf", {})
                    if mtf and not mtf.get("qualifies", True):
                        cnt = mtf.get("agreeing_count", 0)
                        return (f"Nearly there at {conf_v}/10 confidence but the short-term and long-term charts "
                                f"need to agree ({cnt}/3 timeframes currently aligned) — waiting for alignment")
                    try:
                        from src import threshold_manager as _tm_pe
                        rr_min = _tm_pe.get_min_rr()
                        rr_act = float(parsed.get("reward_risk") or 0)
                        if rr_act > 0 and rr_act < rr_min:
                            return (f"Good setup but the potential profit does not justify the risk at current levels "
                                    f"(reward needs to be at least {rr_min:.1f}x the risk)")
                    except Exception:
                        pass
                    scores_pe = {
                        "technical indicators":  parsed.get("technical_score"),
                        "fundamental data":      parsed.get("fundamental_score"),
                        "market sentiment":      parsed.get("sentiment_score"),
                        "institutional positioning": parsed.get("positioning_score"),
                    }
                    below_pe = sorted(
                        [(k, v) for k, v in scores_pe.items() if v is not None and v < 7],
                        key=lambda x: x[1],
                    )
                    if below_pe:
                        k_pe, v_pe = below_pe[0]
                        if v_pe <= 4:
                            return f"Nearly there at {conf_v}/10 but {k_pe} are weak — need to improve before we enter"
                        return f"Nearly there at {conf_v}/10 confidence but {k_pe} need to strengthen before we enter"
                    return f"Reached {conf_v}/10 confidence but the analyst judged the setup is not clean enough to enter right now"

                if top3:
                    no_sec.append(f"The system considered {universe_size} pairs and analysed the top {n_deep} in depth. Here is why the best candidates did not fully qualify:")
                    for rr in top3:
                        reason_plain = _plain_rejection(rr)
                        no_sec.append(f"<b>{rr['pair']}:</b> {reason_plain}")
                    no_sec.append("The system is being selective — waiting for higher quality opportunities")
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
                # Missed opportunity: flag pairs with confidence ≥5 but data on fallback
                try:
                    from src import data_quality as _dq_wl
                    _mo_note = _dq_wl.missed_opportunity_note(rr)
                    if _mo_note:
                        watch_sec.append(_mo_note)
                except Exception:
                    pass
        else:
            watch_sec.append("Nothing on watch list today — the system is waiting for cleaner setups")
        all_sections.append(watch_sec)

        # APPROACHING SIGNALS with indicative levels and session time
        if upcoming:
            up_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "📡 <b>APPROACHING SIGNAL</b>"]
            for rr in upcoming:
                up_sec.extend(_approaching_entry(rr))
            all_sections.append(up_sec)

        # RESEARCH TRADES
        _rt_sec = _build_research_section(research_result=research_result)
        if _rt_sec:
            all_sections.append(_rt_sec)

        # FOREX AI FUND — expanded section with full performance stats
        if risk_data and risk_data.get("profile"):
            try:
                from src import risk_manager as _rm_dash
                from src import tracker as _trk_fund
                prof     = risk_data["profile"]
                rmode    = _risk_state.get("risk_mode", "normal")
                dd_mode_dash = _risk_state.get("drawdown_mode", "normal")
                rpct     = _rm_dash.DD_RISK_PCT.get(dd_mode_dash,
                           _rm_dash.MODE_RISK.get(rmode, 1.0))
                exp      = _exposure.get("total_pct", 0.0)
                fund     = prof.get("estimated_balance", _rm_dash.FUND_START)
                fund_pk  = prof.get("peak_balance", fund)
                fund_ret = (fund - _rm_dash.FUND_START) / _rm_dash.FUND_START * 100
                dd_pct   = max(0.0, (fund_pk - fund) / fund_pk * 100) if fund_pk > 0 else 0.0
                real     = config.ACCOUNT_BALANCE
                mode_icons = {
                    "halt":              "🚨",
                    "preservation":      "🔴",
                    "defensive":         "🟠",
                    "caution":           "⚠️",
                    "capital_protection": "⬇️",
                    "streak_protection":  "⬇️",
                    "reduced":            "➡️",
                    "normal":             "🟢",
                    "enhanced":           "⬆️",
                }
                icon = mode_icons.get(dd_mode_dash if dd_mode_dash != "normal" else rmode, "🟢")

                # Load all main fund trades for stats (YES rows only — not analysis sequence IDs)
                _raw_fund_all = list(_trk_fund.load())
                _tt_vals = sorted({str(r.get("trade_this", "")) for r in _raw_fund_all[:200]})
                print(f"[DEBUG FUND] raw rows={len(_raw_fund_all)} · trade_this unique values: {_tt_vals}", file=sys.stderr)
                _all_fund_t = [r for r in _raw_fund_all if str(r.get("trade_this", "")).strip().upper() == "YES"]
                print(f"[DEBUG FUND] after YES filter: {len(_all_fund_t)} fund trades", file=sys.stderr)
                _open_ft    = [r for r in _all_fund_t if r.get("status") == "OPEN"]
                _closed_ft  = [r for r in _all_fund_t
                               if r.get("status") in ("WIN","LOSS","BREAKEVEN","EXPIRED")]
                _wins_ft    = [r for r in _closed_ft
                               if r.get("status") == "WIN" or
                               (r.get("status") in ("BREAKEVEN","EXPIRED") and
                                float(r.get("pips") or 0) > 0)]
                _losses_ft  = [r for r in _closed_ft
                               if r.get("status") == "LOSS" or
                               (r.get("status") in ("BREAKEVEN","EXPIRED") and
                                float(r.get("pips") or 0) < 0)]
                for _dbg_r in _closed_ft[:15]:
                    print(f"[DEBUG CLOSED] pair={_dbg_r.get('pair')} status={_dbg_r.get('status')!r} pips={_dbg_r.get('pips')!r}", file=sys.stderr)
                _expired_ft = [r for r in _closed_ft if r.get("status") == "EXPIRED"]
                _decisive_ft = _wins_ft + _losses_ft
                _n_total    = len(_all_fund_t)
                _n_open     = len(_open_ft)
                _n_closed   = len(_closed_ft)
                _n_wins     = len(_wins_ft)
                _n_losses   = len(_losses_ft)
                _n_expired  = len(_expired_ft)
                _wr_pct     = _n_wins / len(_decisive_ft) * 100 if _decisive_ft else 0.0

                # Best trade by pips
                _best_str = "None yet"
                try:
                    _wp = [(r, float(r.get("pips") or 0)) for r in _closed_ft if r.get("pips")]
                    if _wp:
                        _br, _bp = max(_wp, key=lambda x: x[1])
                        _rm_v = float(_br.get("r_multiple") or 0)
                        _usd  = round(rpct / 100 * fund * _rm_v) if _rm_v else round(abs(_bp))
                        _best_str = (f"{_br.get('pair')} {_br.get('direction')} "
                                     f"+{_bp:.1f} pips (+${_usd})")
                except Exception:
                    pass

                # Avg holding time
                _hold_str = "—"
                try:
                    _days = []
                    for _tr in _closed_ft:
                        _ts = (_tr.get("timestamp") or "")[:10]
                        _ca = (_tr.get("closed_at") or "")[:10]
                        if _ts and _ca:
                            _d = (datetime.strptime(_ca, "%Y-%m-%d") -
                                  datetime.strptime(_ts, "%Y-%m-%d")).days
                            _days.append(max(0, _d))
                    if _days:
                        _avg = sum(_days) / len(_days)
                        _hold_str = f"{_avg:.0f} day{'s' if _avg != 1 else ''}"
                except Exception:
                    pass

                # ML milestone (activates at 10 closed trades)
                _ml_need = max(0, 10 - _n_closed)
                _ml_str  = (f"Need {_ml_need} more closed trade{'s' if _ml_need != 1 else ''} "
                            f"for ML activation") if _ml_need > 0 else "ML model active"

                # Prop firm status — weeks since first YES trade (not calendar week)
                _week_n = 1
                try:
                    _ts_list = sorted(
                        r.get("timestamp", "")[:10]
                        for r in _all_fund_t if r.get("timestamp", "")[:10]
                    )
                    if _ts_list:
                        from datetime import date as _date
                        _first_dt = datetime.strptime(_ts_list[0], "%Y-%m-%d").date()
                        _days_since = (now_ak.date() - _first_dt).days
                        _week_n = max(1, (_days_since // 7) + 1)
                        print(f"[DEBUG WEEK] first_trade={_ts_list[0]} today={now_ak.date()} days={_days_since} week={_week_n}", file=sys.stderr)
                    else:
                        print(f"[DEBUG WEEK] _all_fund_t has {len(_all_fund_t)} rows but no timestamps — week defaulting to 1", file=sys.stderr)
                except Exception as _wk_err:
                    print(f"[DEBUG WEEK] exception: {_wk_err} — week defaulting to 1", file=sys.stderr)
                if fund_ret >= 5.0:
                    _prop_str = f"EXCELLENT — {fund_ret:.1f}% return in week {_week_n}"
                elif fund_ret >= 2.0:
                    _prop_str = f"STRONG — {fund_ret:.1f}% return in week {_week_n}"
                elif fund_ret >= 0:
                    _prop_str = f"On track — {fund_ret:.1f}% return in week {_week_n}"
                else:
                    _prop_str = f"Behind — {fund_ret:.1f}% return in week {_week_n}"

                # ML model plain English status (Rule 27)
                _ml_status_plain = (
                    f"learning — need {_ml_need} more decisive trade{'s' if _ml_need != 1 else ''} for activation"
                    if _ml_need > 0 else "active — correctly predicting from training data"
                )
                try:
                    from src import ml_predictor as _mlp_fd
                    _ml_line_raw = _mlp_fd.get_model_status_line()
                    if "accuracy" in _ml_line_raw.lower():
                        import re as _re_ml
                        _acc_m = _re_ml.search(r'(\d+)%', _ml_line_raw)
                        if _acc_m:
                            _ml_status_plain = f"active — correctly predicting {_acc_m.group(1)}% of trades (based on {_n_closed} closed trades)"
                except Exception:
                    pass

                _fund_sec = [
                    "", "━━━━━━━━━━━━━━━━━━━━━",
                    f"📈 <b>FOREX AI FUND</b>",
                    f"Balance: <b>${fund:,.0f} ({fund_ret:+.1f}%)</b> · Peak: ${fund_pk:,.0f}",
                    f"Fund trades: {_n_total} taken · {_n_closed} closed ({_n_wins} WIN · {_n_losses} LOSS) · {_n_open} open",
                    f"Drawdown: {dd_pct:.1f}% · {icon} {dd_mode_dash.replace('_',' ').title()} · {rpct:.2f}% risk per trade",
                    f"🤖 ML model: {_ml_status_plain}",
                ]
                if _decisive_ft:
                    _early = f" ({len(_decisive_ft)} trade{'s' if len(_decisive_ft)!=1 else ''} — too early to judge)" if len(_decisive_ft) < 10 else ""
                    _fund_sec.append(f"Win rate: {_wr_pct:.0f}%{_early}")
                else:
                    _fund_sec.append("Win rate: — (no decisive outcomes yet)")
                _fund_sec += [
                    f"Best trade: {_best_str}",
                    f"Avg holding time: {_hold_str}",
                    f"PROP FIRM STATUS: {_prop_str}",
                ]
                all_sections.append(_fund_sec)

                # FUND TRADES — every main fund trade with full detail
                if _all_fund_t:
                    _ft_sec = [
                        "", "━━━━━━━━━━━━━━━━━━━━━",
                        "💼 <b>FUND TRADES</b>",
                    ]
                    _status_icons = {"WIN":"✅","LOSS":"❌","OPEN":"⏳","EXPIRED":"⏰",
                                     "BREAKEVEN":"➖","NO_TRADE":"•"}
                    for _ftr in _all_fund_t:
                        _fs    = _ftr.get("status","?")
                        _fi    = _status_icons.get(_fs, "•")
                        _fpair = _ftr.get("pair","?")
                        _fdir  = (_ftr.get("direction") or "?").upper()
                        _fid   = _ftr.get("id","?")
                        _fent  = _ftr.get("entry","?")
                        _fex   = _ftr.get("exit_price","")
                        _fpips = _ftr.get("pips","")
                        _frm   = _ftr.get("r_multiple","")
                        _fts   = (_ftr.get("timestamp") or "")[:10]
                        _fca   = (_ftr.get("closed_at") or "")[:10]

                        _ft_sec.append(f"{_fi} <b>#{_fid} {_fpair} {_fdir}</b> — {_fs}")
                        _price_line = f"   Entry: {_fent}"
                        if _fex:
                            _price_line += f" → Exit: {_fex}"
                        _price_line += f"  ({_fts}"
                        if _fca and _fca != _fts:
                            _price_line += f" → {_fca}"
                        _price_line += ")"
                        _ft_sec.append(_price_line)
                        if _fpips:
                            try:
                                _pp = float(_fpips)
                                _pips_line = f"   {_pp:+.1f} pips"
                                if _frm:
                                    _rm2 = float(_frm)
                                    _usd2 = round(rpct / 100 * fund * _rm2)
                                    _pips_line += f" ({_rm2:+.2f}R · ${_usd2:+})"
                                _ft_sec.append(_pips_line)
                            except (TypeError, ValueError):
                                pass
                    all_sections.append(_ft_sec)
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
        health_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "⚠️ <b>SYSTEM HEALTH</b>"]
        if threshold_revert_msg:
            health_sec.append(threshold_revert_msg)
        # Data quality scorecard
        try:
            from src import data_quality as _dq_hs
            if _dq_quality:
                health_sec.extend(_dq_hs.build_scorecard(_dq_quality))
        except Exception:
            pass
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
        # Run statistics
        _rt_open = sum(1 for r in deep_results if r.get("parsed", {}).get("trade_this") != "NO")
        health_sec.append(
            f"Run took {run_duration_min:.0f} minutes · {universe_size} pairs scanned · "
            f"{n_deep} deep analysed · {_rt_open} research trades opened"
        )
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
            "Sydney: 9am–6pm · Tokyo: 12pm–9pm",
            "London: 7pm–4am · New York: 1am–10am",
            "Best window: 1am–4am Auckland (London/New York overlap — highest volume of the week)",
        ])

    # ═══════════════════════════════════════════════════════════════════════════
    # INTRADAY SCANS (9AM / 5PM / 11PM) — unified expanded format
    # ═══════════════════════════════════════════════════════════════════════════
    elif scan_mode in ("morning", "prelondon", "preny"):
        # ── DATA QUALITY ASSESSMENT (intraday)
        _dq_quality_id: dict = {}
        _dq_state_id:   dict = {}
        try:
            from src import data_quality as _dq_id_pre
            _dq_quality_id = _dq_id_pre.assess_scan(deep_results)
            _dq_state_id   = _dq_id_pre.update_state(_dq_quality_id, scan_mode)
            _dq_hv_id = _dq_id_pre.high_volume_alert(_dq_quality_id)
            if _dq_hv_id:
                try:
                    _telegram(_dq_hv_id)
                except Exception:
                    pass
            for _dq_al_id in _dq_id_pre.consecutive_alerts(_dq_state_id):
                try:
                    _telegram(_dq_al_id)
                except Exception:
                    pass
        except Exception:
            pass

        # Header — include session context line for 5pm and 11pm
        if scan_mode == "morning":
            _hdr_id = [f"<b>🤖 FOREX AI — 🌏 9AM MORNING CHECK — {today_short}</b>"]
        elif scan_mode == "prelondon":
            _hdr_id = [
                f"<b>🤖 FOREX AI — 🌆 5PM PRE-LONDON CHECK — {today_short}</b>",
                "London market opens in 2 hours at 7pm Auckland — best time for EUR GBP CHF pairs",
            ]
        else:  # preny
            _hdr_id = [
                f"<b>🤖 FOREX AI — 🌃 11PM PRE-NEW YORK CHECK — {today_short}</b>",
                "New York market opens in 2 hours at 1am Auckland",
                "London/New York overlap 1am–4am Auckland — highest volume of the entire week",
            ]
        all_sections.append(_hdr_id)
        if _dd_banner:
            all_sections.append(["", "━━━━━━━━━━━━━━━━━━━━━", _dd_banner])

        # OPEN TRADES — compact format for intraday (always first)
        _ot_conf_map_id = {r["pair"]: _eff_conf(r) for r in deep_results if r.get("pair")}
        # Confidence crisis: send immediate separate alert before main message
        for _cca_t in _ot_open_trades:
            _cca_p  = _cca_t.get("pair", "")
            _cca_ec = None
            try:
                _cca_ec = int(float(_cca_t.get("confidence") or 0)) or None
            except (TypeError, ValueError):
                pass
            _cca_cc = _ot_conf_map_id.get(_cca_p)
            if _cca_ec and _cca_cc is not None and _cca_cc < 3 and _cca_ec >= 3:
                try:
                    _telegram(
                        f"⚠️ Open trade warning — {_cca_p} confidence has fallen to "
                        f"{_cca_cc}/10 — the original trade rationale may no longer be valid "
                        f"— consider whether to exit early to avoid a larger loss"
                    )
                except Exception:
                    pass
        _ot_compact = _build_open_trades_section(_ot_open_trades, _ot_px_cache, now_ak, compact=True, cur_conf_map=_ot_conf_map_id)
        all_sections.append(_ot_compact)

        # ── SESSION FOCUS (5pm = London, 11pm = New York) ─────────────────────
        if scan_mode == "prelondon":
            _sf_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "🌆 <b>LONDON SESSION FOCUS</b>"]
            _sf_sec.append("London opens in 2 hours — EUR GBP CHF pairs are most active during this session")
            _london_pairs_raw = [r for r in (watch_list + upcoming)
                                 if any(c in r["pair"].upper() for c in ("EUR", "GBP", "CHF"))
                                 and _quality_grades.get(r["pair"], _trade_quality_grade(r)).get("grade") not in ("D", "F")]
            # Dedup inverse pairs within session focus list
            _sf_lseen: set = set()
            _london_pairs = []
            for _r in _london_pairs_raw:
                _p = _r["pair"].upper().replace("/", "")
                _inv = _p[3:] + _p[:3]
                if _p not in _sf_lseen and _inv not in _sf_lseen:
                    _london_pairs.append(_r)
                    _sf_lseen.add(_p)
            if _london_pairs:
                for _lp in _london_pairs[:3]:
                    _sf_sec.append(f"{_lp['pair']} entry window opens at 7pm Auckland — be ready")
            else:
                _sf_sec.append("No EUR or GBP pairs on watch list today — London session may be quiet for us")
            all_sections.append(_sf_sec)
        elif scan_mode == "preny":
            _sf_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "🌃 <b>NEW YORK SESSION FOCUS</b>"]
            _sf_sec.append("New York opens in 2 hours — USD CAD pairs are most active during this session")
            _sf_sec.append("London and New York overlap from 1am to 4am Auckland — highest volume period of the entire week — best time for EUR USD GBP pairs")
            _ny_pairs_raw = [r for r in (watch_list + upcoming)
                             if any(c in r["pair"].upper() for c in ("USD", "CAD"))
                             and _quality_grades.get(r["pair"], _trade_quality_grade(r)).get("grade") not in ("D", "F")]
            # Dedup inverse pairs within session focus list
            _sf_nyseen: set = set()
            _ny_pairs = []
            for _r in _ny_pairs_raw:
                _p = _r["pair"].upper().replace("/", "")
                _inv = _p[3:] + _p[:3]
                if _p not in _sf_nyseen and _inv not in _sf_nyseen:
                    _ny_pairs.append(_r)
                    _sf_nyseen.add(_p)
            if _ny_pairs:
                for _np in _ny_pairs[:3]:
                    _sf_sec.append(f"{_np['pair']} entry window opens at 1am Auckland — be ready when New York opens")
            else:
                _sf_sec.append("No USD or CAD pairs on watch list tonight — New York session may be quiet for us")
            all_sections.append(_sf_sec)

        # ── Market context (full plain-English format) ────────────────────────
        _ctx_id = ["", "━━━━━━━━━━━━━━━━━━━━━", "🌍 <b>MARKET CONTEXT</b>"]
        _vix_id   = ctx.get("vix")
        _env_id   = ctx["risk_env"]
        _vix_str_id = f"VIX {_vix_id:.1f}" if _vix_id else ""
        _ctx_id.append(
            f"Environment: <b>{_env_id}</b>"
            f"{' (' + _vix_str_id + ')' if _vix_str_id else ''}"
        )
        _sc_id     = ctx.get("strongest_ccy")
        _wc_id     = ctx.get("weakest_ccy")
        _scores_id = ctx.get("ccy_scores", {})
        _roff_id = "risk-off" in _env_id.lower()
        _safe_h_id = {"JPY", "CHF", "USD"}
        _comm_c_id = {"AUD", "NZD"}
        if _sc_id:
            if _roff_id and _sc_id in _comm_c_id:
                _sc_rsn_id = "data shows strength but risk-off conditions favour safe havens"
                _ctx_id.append(f"💪 Strongest: <b>{_sc_id}</b> — {_sc_rsn_id} (+{_scores_id.get(_sc_id,0):.0f})")
                _ctx_id.append("⚠️ Risk-OFF environment — JPY, CHF, USD typically outperform — verify before trading AUD/NZD")
            elif _roff_id and _sc_id in _safe_h_id:
                _sc_rsn_id = "safe haven demand — expected in risk-off environment"
                _ctx_id.append(f"💪 Strongest: <b>{_sc_id}</b> — {_sc_rsn_id} (+{_scores_id.get(_sc_id,0):.0f})")
            else:
                _sc_rsn_id = "carry + risk-on" if "risk-on" in _env_id else "carry + fundamentals"
                _ctx_id.append(f"💪 Strongest: <b>{_sc_id}</b> — {_sc_rsn_id} (+{_scores_id.get(_sc_id,0):.0f})")
        if _wc_id:
            if _roff_id:
                _wc_rsn_id = "risk-off selling pressure" if _wc_id in _comm_c_id else "weak fundamentals"
            else:
                _wc_rsn_id = "low rates + risk-on selling" if "risk-on" in _env_id else "weak fundamentals"
            _ctx_id.append(f"📉 Weakest: <b>{_wc_id}</b> — {_wc_rsn_id} ({_scores_id.get(_wc_id,0):.0f})")
        try:
            from src import market_regime as _mr_id
            _rd_id = _mr_id.detect()
            _rl_id = _mr_id.telegram_line(_rd_id)
            if _rl_id:
                _ctx_id.append(_rl_id)
        except Exception:
            pass
        _ps_id = _compute_patience_score(ctx)
        _ctx_id.append(f"📊 <b>Trading conditions: {_ps_id['score']}/10</b> — {_ps_id['description']}")
        all_sections.append(_ctx_id)

        # ECONOMIC CALENDAR — include on 5pm and 11pm scans (shows tonight's events)
        if scan_mode in ("prelondon", "preny"):
            try:
                from src import economic_calendar as _ec_id
                _cal_id = _ec_id.build_calendar_section()
                if _cal_id:
                    all_sections.append(_cal_id)
            except Exception:
                pass

        # ── YES trade alerts (full entry instructions) ────────────────────────
        for r in yes_trades:
            all_sections.append(_trade_block(r))

        # ── CHANGES SINCE LAST SCAN ───────────────────────────────────────────
        _yes_pairs = {r["pair"] for r in yes_trades}
        _prev_scan_label = {"morning": "6am", "prelondon": "9am", "preny": "5pm"}.get(scan_mode, "last")
        _changes_lines = []

        # New alerts (pairs that just reached YES/7+ that weren't before)
        _newly_yes = [r for r in yes_trades if new_alerts and f"{r['pair']}:{(r['parsed'].get('direction') or '').upper()}" in new_alerts]
        for _ny_r in _newly_yes[:3]:
            _ny_d = (_ny_r["parsed"].get("direction") or "").upper()
            _changes_lines.append(f"🆕 {_ny_r['pair']} {_ny_d} reached {_eff_conf(_ny_r)}/10 confidence — new trade alert — full details below")

        # Pairs that improved to 6+ since last scan
        _newly_6plus = [
            r for r in deep_results
            if r["pair"] not in _yes_pairs
            and _eff_conf(r) >= 6
            and _morning_conf.get(r["pair"], 10) < 6
        ]
        for r in _newly_6plus[:3]:
            curr = _eff_conf(r)
            prev = _morning_conf.get(r["pair"], 0)
            dirn = (r["parsed"].get("direction") or "").upper()
            arrow = "📈" if dirn == "BUY" else "📉"
            _changes_lines.append(f"⬆️ {r['pair']} improved from {prev} to {curr}/10 — getting closer to entry")
            _ew_ns = _entry_window_for_pair(r["pair"])
            _eq_ns_e, _ = _entry_quality(r["pair"], now_ak)
            _tref_ns = _time_ref_for_entry(_ew_ns[0], _ew_ns[1], now_ak)
            _start_ns = _fmt_time_exact(_ew_ns[0], _ew_ns[1])
            _changes_lines.append(
                f"  {_eq_ns_e} <b>BE READY TO ENTER:</b> "
                f"{_ew_ns[6]} {_start_ns} Auckland {_tref_ns}"
            )

        # Pairs that dropped since last scan
        _dropped = [
            r for r in deep_results
            if r["pair"] not in _yes_pairs
            and _morning_conf.get(r["pair"]) is not None
            and _morning_conf[r["pair"]] >= 6
            and _eff_conf(r) < 6
        ]
        for r in _dropped[:2]:
            curr = _eff_conf(r)
            prev = _morning_conf[r["pair"]]
            _changes_lines.append(f"⬇️ {r['pair']} dropped from {prev} to {curr}/10 — removed from watch list")

        # Conditions change
        _prev_patience = _morning_conf.get("__patience__")
        _cur_patience  = _compute_patience_score(ctx)["score"]
        if _prev_patience is not None and abs(_cur_patience - _prev_patience) >= 2:
            _changes_lines.append(f"📊 Conditions update: Trading conditions changed to {_cur_patience}/10")

        # Research trades closed since last scan
        _prev_scan_hours = {"morning": 3, "prelondon": 8, "preny": 6}.get(scan_mode, 8)
        try:
            from src import research_tracker as _rtrk_chg
            _rt_chg_rows = _rtrk_chg.load()
            _rt_cutoff = (now_ak - timedelta(hours=_prev_scan_hours)).replace(tzinfo=None)
            for _rt_r in _rt_chg_rows:
                if _rt_r.get("status") not in ("WIN", "LOSS"):
                    continue
                _rt_cat = _rt_r.get("closed_at", "")
                if not _rt_cat:
                    continue
                try:
                    _rt_ts = datetime.strptime(_rt_cat[:16], "%Y-%m-%dT%H:%M")
                    if _rt_ts >= _rt_cutoff:
                        _rt_pips = _rt_r.get("pips", "")
                        try:
                            _rt_pips_f = float(_rt_pips)
                            _rt_pips_str = f"{'+' if _rt_pips_f >= 0 else ''}{_rt_pips_f:.0f}"
                        except (TypeError, ValueError):
                            _rt_pips_str = "0"
                        _changes_lines.append(
                            f"🔬 Research update: {_rt_r['pair']} closed as {_rt_r['status']} {_rt_pips_str} pips"
                        )
                except (ValueError, TypeError):
                    pass
        except Exception:
            pass

        # News reminders for open trades — add to changes section
        try:
            from src import economic_calendar as _ec_chg
            for _ot_row_chg in _ot_open_trades:
                _ot_pair_chg = _ot_row_chg.get("pair", "")
                if not _ot_pair_chg:
                    continue
                _chg_evs = _ec_chg.events_for_pair(_ot_pair_chg, hours=48)
                for _chg_ev in _chg_evs[:2]:
                    _chg_ccy  = _chg_ev["currency"]
                    _chg_en   = _chg_ev["plain_name"]
                    _chg_ak   = _chg_ev["ak_display"]
                    _chg_hrs  = _chg_ev.get("hours_away", 48)
                    _chg_time = "very soon" if _chg_hrs <= 6 else "within 24 hours" if _chg_hrs <= 24 else "within 48 hours"
                    _changes_lines.append(
                        f"⚠️ Reminder: {_chg_ccy} {_chg_en} releases {_chg_time} ({_chg_ak}) "
                        f"— directly affects your open {_ot_pair_chg} trade"
                    )
        except Exception:
            pass

        chg_sec = ["", "━━━━━━━━━━━━━━━━━━━━━",
                   f"🔄 <b>CHANGES SINCE {_prev_scan_label.upper()} SCAN</b>"]
        if _changes_lines:
            chg_sec.extend(_changes_lines)
        else:
            chg_sec.append(f"No significant changes since {_prev_scan_label} — existing opportunities remain valid")
        all_sections.append(chg_sec)

        # ── Watch list (5–6) and approaching signals (3–4) with levels + session ─
        _all_candidates = sorted(
            [r for r in deep_results if r["pair"] not in _yes_pairs and _eff_conf(r) >= 3],
            key=_eff_conf, reverse=True,
        )
        _watch_items_raw    = [r for r in _all_candidates
                               if _eff_conf(r) >= 5
                               and _quality_grades.get(r["pair"], _trade_quality_grade(r)).get("grade") not in ("D", "F")]
        # Deduplicate inverse pairs vs yes_trades and within watch list
        _wi_seen: set = {r["pair"].upper().replace("/", "") for r in yes_trades}
        _watch_items = []
        for _r in _watch_items_raw:
            _p = _r["pair"].upper().replace("/", "")
            _inv = _p[3:] + _p[:3]
            if _p not in _wi_seen and _inv not in _wi_seen:
                _watch_items.append(_r)
                _wi_seen.add(_p)
        _watch_items = _watch_items[:3]
        _approaching_items = [
            r for r in _all_candidates
            if _eff_conf(r) <= 4
            and r["pair"].upper() not in _open_pair_set
            and not any(_is_inverse(r["pair"], op) for op in _open_pair_set)
            and _quality_grades.get(r["pair"], _trade_quality_grade(r)).get("grade") not in ("D", "F")
        ][:2]

        if _watch_items:
            wl_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "👀 <b>WATCH LIST</b>"]
            for rr in _watch_items:
                _rr_pair = rr["pair"]
                _rr_conf = _eff_conf(rr)
                _rr_dirn = (rr["parsed"].get("direction") or "").upper()
                _rr_arrow = "📈" if _rr_dirn == "BUY" else "📉"
                _rr_ew = _entry_window_for_pair(_rr_pair)
                _rr_eq_e, _ = _entry_quality(_rr_pair, now_ak)
                _rr_tref = _time_ref_for_entry(_rr_ew[0], _rr_ew[1], now_ak)
                _rr_start = _fmt_time_exact(_rr_ew[0], _rr_ew[1])
                _rr_qg = _quality_grades.get(_rr_pair, _trade_quality_grade(rr))
                _rr_grade = (_rr_qg or {}).get("grade", "C")
                if _rr_grade in ("D", "F"):
                    continue
                # Session label for London/NY relevance
                _rr_ses_label = ""
                if scan_mode == "prelondon" and any(c in _rr_pair.upper() for c in ("EUR", "GBP", "CHF")):
                    _rr_ses_label = "🌆 London session pair — entry window 7pm–8:30pm Auckland tonight"
                elif scan_mode == "preny":
                    if any(c in _rr_pair.upper() for c in ("USD", "CAD")):
                        _rr_ses_label = "🌃 New York session pair — entry window 1am–2:30am Auckland tonight"
                    elif any(c in _rr_pair.upper() for c in ("EUR", "GBP")):
                        _rr_ses_label = "🌃 London/NY overlap pair — peak entry window 1am–4am Auckland tonight"
                wl_sec.append("")
                if _rr_ses_label:
                    wl_sec.append(_rr_ses_label)
                wl_sec.append(
                    f"{_rr_arrow} <b>{_rr_pair}</b> · Grade {_rr_grade} · {_rr_conf}/10"
                )
                # Indicative levels
                _rr_ind_e, _rr_ind_s, _rr_ind_t, _ = _calc_indicative_levels(
                    _rr_pair, rr["parsed"], rr.get("bundle", {})
                )
                if _rr_ind_e and _rr_ind_s and _rr_ind_t:
                    try:
                        _rr_risk = round(abs(float(_rr_ind_e) - float(_rr_ind_s)) / _pip_size(_rr_pair))
                        _rr_prof = round(abs(float(_rr_ind_t) - float(_rr_ind_e)) / _pip_size(_rr_pair))
                        _is_jpy_rr = "JPY" in _rr_pair.upper()
                        _dec_rr = 3 if _is_jpy_rr else 5
                        wl_sec.append(
                            f"Ideal entry: {float(_rr_ind_e):.{_dec_rr}f} · "
                            f"Stop: {float(_rr_ind_s):.{_dec_rr}f} · "
                            f"Target: {float(_rr_ind_t):.{_dec_rr}f}"
                        )
                        _rr_rr = round(_rr_prof / _rr_risk, 1) if _rr_risk else 0
                        wl_sec.append(f"R:R {_rr_rr}:1 — Stop {_rr_risk}p · Target {_rr_prof}p")
                    except (TypeError, ValueError, ZeroDivisionError):
                        pass
                # Entry window
                _rr_sess_active, _rr_sess_close = _session_status_for_pair(_rr_pair, now_ak)
                if _rr_sess_active:
                    wl_sec.append(f"⏰ {_rr_ew[6]} currently active — closes {_rr_sess_close} Auckland")
                elif _rr_tref not in ("TODAY",):
                    wl_sec.append(f"⏰ Entry window passed for today — next opportunity: {_rr_ew[6]} {_rr_start} Auckland tomorrow")
                else:
                    wl_sec.append(f"⏰ Best entry window: {_rr_ew[6]} {_rr_start} Auckland {_rr_tref}")
            all_sections.append(wl_sec)
        else:
            all_sections.append(["", "━━━━━━━━━━━━━━━━━━━━━", "👀 <b>WATCH LIST</b>",
                                  "Nothing on watch list — system waiting for cleaner setups"])

        if _approaching_items:
            ap_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "📡 <b>APPROACHING SIGNAL</b>"]
            for rr in _approaching_items:
                ap_sec.extend(_approaching_entry(rr))
            all_sections.append(ap_sec)

        # RESEARCH TRADES — compact summary for all intraday scans
        _cpt_rt_sec = _build_compact_research_section()
        if _cpt_rt_sec:
            all_sections.append(_cpt_rt_sec)

        # FOREX AI FUND — compact version for intraday scans
        if risk_data and risk_data.get("profile"):
            try:
                from src import risk_manager as _rm_id
                from src import tracker as _trk_id
                _prof_id = risk_data["profile"]
                _rmode_id    = _risk_state.get("risk_mode", "normal")
                _dd_mode_id  = _risk_state.get("drawdown_mode", "normal")
                _rpct_id     = _rm_id.DD_RISK_PCT.get(_dd_mode_id,
                               _rm_id.MODE_RISK.get(_rmode_id, 1.0))
                _fund_id  = _prof_id.get("estimated_balance", _rm_id.FUND_START)
                _pk_id    = _prof_id.get("peak_balance", _fund_id)
                _ret_id   = (_fund_id - _rm_id.FUND_START) / _rm_id.FUND_START * 100
                _dd_id    = max(0.0, (_pk_id - _fund_id) / _pk_id * 100) if _pk_id > 0 else 0.0
                _icon_id  = {"halt":"🚨","preservation":"🔴","defensive":"🟠","caution":"⚠️",
                             "capital_protection":"⬇️","streak_protection":"⬇️",
                             "reduced":"➡️","normal":"🟢","enhanced":"⬆️"}.get(
                             _dd_mode_id if _dd_mode_id != "normal" else _rmode_id, "🟢")
                _all_ft_id = [r for r in _trk_id.load() if r.get("trade_this") == "YES"]
                _cls_ft_id = [r for r in _all_ft_id if r.get("status") in ("WIN","LOSS","BREAKEVEN","EXPIRED")]
                _opn_ft_id = [r for r in _all_ft_id if r.get("status") == "OPEN"]
                _w_ft_id   = [r for r in _cls_ft_id
                              if r.get("status") == "WIN" or
                              (r.get("status") in ("BREAKEVEN","EXPIRED") and float(r.get("pips") or 0) > 0)]
                _l_ft_id   = [r for r in _cls_ft_id
                              if r.get("status") == "LOSS" or
                              (r.get("status") in ("BREAKEVEN","EXPIRED") and float(r.get("pips") or 0) < 0)]
                _dec_id    = _w_ft_id + _l_ft_id
                _ml_need_id  = max(0, 10 - len(_cls_ft_id))
                _ml_stat_id  = (
                    f"learning — need {_ml_need_id} more decisive trade{'s' if _ml_need_id != 1 else ''} for activation"
                    if _ml_need_id > 0 else "active — correctly predicting from training data"
                )
                try:
                    from src import ml_predictor as _mlp_id
                    _ml_raw_id = _mlp_id.get_model_status_line()
                    if "accuracy" in _ml_raw_id.lower():
                        import re as _re_id
                        _acc_id = _re_id.search(r'(\d+)%', _ml_raw_id)
                        if _acc_id:
                            _ml_stat_id = f"active — correctly predicting {_acc_id.group(1)}% of trades"
                except Exception:
                    pass
                _fund_id_sec = [
                    "", "━━━━━━━━━━━━━━━━━━━━━",
                    "📈 <b>FOREX AI FUND</b>",
                    f"Balance: ${_fund_id:,.0f} ({_ret_id:+.1f}%) · Peak: ${_pk_id:,.0f}",
                    f"Fund trades: {len(_all_ft_id)} taken · {len(_cls_ft_id)} closed ({len(_w_ft_id)} WIN · {len(_l_ft_id)} LOSS) · {len(_opn_ft_id)} open",
                    f"Drawdown: {_dd_id:.1f}% · {_icon_id} {_dd_mode_id.replace('_',' ').title()} · {_rpct_id:.2f}% risk per trade",
                    f"🤖 ML model: {_ml_stat_id}",
                ]
                # Show any open trades inline
                for _ot_id in _opn_ft_id:
                    _pp_id = (_ot_id.get("pips") or "")
                    _pp_str = f" ({float(_pp_id):+.1f}p)" if _pp_id else ""
                    _fund_id_sec.append(
                        f"   ⏳ #{_ot_id.get('id')} {_ot_id.get('pair')} {(_ot_id.get('direction') or '').upper()}{_pp_str}"
                    )
                all_sections.append(_fund_id_sec)
            except Exception:
                pass

        # TONIGHT'S KEY TIMES (5pm and 11pm scans)
        if scan_mode == "prelondon":
            _kt_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "⏰ <b>TONIGHT'S KEY TIMES</b>"]
            _kt_sec.append("7:00pm Auckland — London market opens — EUR GBP CHF pairs become most active")
            # Add any scheduled news events for tonight
            _kt_events_listed = False
            try:
                from src import economic_calendar as _ec_kt
                _kt_cal = _ec_kt.build_calendar_section()
                if _kt_cal and len(_kt_cal) > 2:
                    _kt_sec.append("Check calendar section above for tonight's news events")
                    _kt_events_listed = True
            except Exception:
                pass
            if not _kt_events_listed:
                _kt_sec.append("No major economic events tonight — clean London session ahead")
            all_sections.append(_kt_sec)
        elif scan_mode == "preny":
            _kt_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "⏰ <b>OVERNIGHT KEY TIMES</b>"]
            _kt_sec.append("1:00am Auckland — New York market opens — USD and CAD pairs become most active")
            _kt_sec.append("1:00am–4:00am Auckland — London and New York both open simultaneously — highest volume window of the entire week — best time for EUR USD GBP pairs")
            _kt_events2_listed = False
            try:
                from src import economic_calendar as _ec_kt2
                _kt_cal2 = _ec_kt2.build_calendar_section()
                if _kt_cal2 and len(_kt_cal2) > 2:
                    _kt_sec.append("Check calendar section above for overnight news events")
                    _kt_events2_listed = True
            except Exception:
                pass
            if not _kt_events2_listed:
                _kt_sec.append("No major economic events overnight — clean New York session ahead")
            all_sections.append(_kt_sec)

        # SYSTEM HEALTH — always show on all intraday scans
        _intraday_health = ["", "━━━━━━━━━━━━━━━━━━━━━", "⚠️ <b>SYSTEM HEALTH</b>"]
        try:
            from src import data_quality as _dq_ih
            if _dq_quality_id:
                _intraday_health.extend(_dq_ih.build_scorecard(_dq_quality_id))
        except Exception:
            pass
        if cost_lines:
            _intraday_health.extend(cost_lines)
        else:
            _intraday_health.append("Cost tracking unavailable")
        all_sections.append(_intraday_health)

    # ═══════════════════════════════════════════════════════════════════════════
    # SUNDAY GAP SCAN — brief weekend gap detection message
    # ═══════════════════════════════════════════════════════════════════════════
    elif scan_mode == "gap":
        _gap_hdr = [
            f"<b>🤖 FOREX AI — 🌏 SUNDAY GAP SCAN — {_fmt_date_nz(now_ak)}</b>",
            "Checking for significant price gaps since Friday's close",
        ]
        all_sections.append(_gap_hdr)

        # Detect gaps: compare current price to Friday's close from daily series
        _gap_found = []
        _gap_checked = []
        _gap_pairs_list = [
            r["pair"] for r in deep_results[:12]
            if r.get("pair")
        ] or ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD",
               "USDCHF", "EURJPY", "GBPJPY", "EURGBP"]
        for _gp in _gap_pairs_list:
            try:
                import requests as _rq_gap
                _gp_resp = _rq_gap.get(
                    "https://api.twelvedata.com/time_series",
                    params={
                        "symbol":     _gp,
                        "interval":   "1day",
                        "outputsize": 3,
                        "format":     "JSON",
                        "apikey":     config.TWELVE_DATA_KEY,
                    },
                    timeout=12,
                )
                _gp_data = _gp_resp.json().get("values", [])
                if len(_gp_data) < 2:
                    continue
                _gp_cur   = float(_gp_data[0]["close"])
                _gp_fri   = float(_gp_data[1]["close"])
                _gp_pips  = (_gp_cur - _gp_fri) / _pip_size(_gp)
                _gp_thresh = 10 if "JPY" in _gp.upper() else 20
                _gap_checked.append(_gp)
                if abs(_gp_pips) >= _gp_thresh:
                    _gp_sign = "+" if _gp_pips > 0 else ""
                    _gp_dir  = "⬆️" if _gp_pips > 0 else "⬇️"
                    _is_jpy_g = "JPY" in _gp.upper()
                    _dec_g    = 3 if _is_jpy_g else 5
                    _gap_found.append(
                        f"{_gp_dir} <b>{_gp}</b> — Friday: {_gp_fri:.{_dec_g}f} → Now: {_gp_cur:.{_dec_g}f} — Gap: {_gp_sign}{int(round(_gp_pips))} pips"
                    )
            except Exception:
                pass

        if _gap_found:
            _gap_sec = ["", "━━━━━━━━━━━━━━━━━━━━━", "⚡ <b>GAPS DETECTED</b>",
                        f"Checked {len(_gap_checked)} pairs — {len(_gap_found)} significant gap{'s' if len(_gap_found) != 1 else ''} found:"]
            _gap_sec.extend(_gap_found)
            _gap_sec.append("")
            _gap_sec.append("Consider waiting for gaps to fill before entering new trades — gaps often retrace in the first hours of Sunday trading")
            all_sections.append(_gap_sec)
        else:
            all_sections.append([
                "", "━━━━━━━━━━━━━━━━━━━━━",
                "✅ <b>NO SIGNIFICANT GAPS</b>",
                f"Checked {len(_gap_checked)} pairs — markets opened normally, no significant price gaps from Friday's close",
                "Normal trading conditions expected — Monday 6am full scan will identify opportunities",
            ])

        if cost_lines:
            all_sections.append(["", "━━━━━━━━━━━━━━━━━━━━━", "⚠️ <b>SYSTEM HEALTH</b>"] + cost_lines)

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

    _clean_sections = []
    for _sc_sec in all_sections:
        _sec_has_f = any("Grade F" in str(_sc_ln) for _sc_ln in _sc_sec)
        if _sec_has_f:
            print(f"[CRITICAL] Grade F text found in section starting with: {_sc_sec[:2]} — dropping section", file=sys.stderr)
        else:
            _clean_sections.append(_sc_sec)
    all_sections = _clean_sections

    all_sections = [[ln for ln in sec if _is_ok_line(ln)] for sec in all_sections]
    _send_in_parts(all_sections)


# ── Daily run ──────────────────────────────────────────────────────────────────

_RUN_GUARD_FILE = config.DATA_DIR / "run_guard.json"
_COOLDOWN_SECS  = 5400  # 90 minutes — catches cron-job.org retries on long-running scans

_ALERTS_FILE         = config.DATA_DIR / "last_alerts.json"
_MORNING_RANKED_FILE = config.DATA_DIR / "morning_ranked.json"

# (label, session-currency filter set — empty = no filter)
_SCAN_MODES: dict = {
    "morning":   ("9am Morning Check",      set()),
    "prelondon": ("5pm Pre-London Check",   set()),
    "preny":     ("11pm Pre-New York Check", set()),
    "full":      ("6am Full Scan",          set()),
    "gap":       ("Sunday Gap Scan",        set()),
}

# All 4 scans select from the full universe by 8-factor merit score — no mode filtering
_SCAN_TOP_N   = 20   # pairs selected and analysed from the full universe
_TD_CACHE_MAX = 35   # pairs to pre-warm in Twelve Data cache (research sweep uses the extras)

# Sonnet escalation threshold: 6 for 6am (higher quality), 7 for intraday (cheaper)
_SONNET_THRESH = {"full": 6, "morning": 7, "prelondon": 7, "preny": 7}


def _get_scan_mode() -> str:
    """Return scan mode from SCAN_MODE env var or current Auckland hour."""
    import os as _os_
    mode = _os_.getenv("SCAN_MODE", "").lower().strip()
    if mode in _SCAN_MODES:
        return mode
    now = _auckland_now()
    # Sunday morning gap scan (weekday 6 = Sunday, hours 5–8am)
    if now.weekday() == 6 and now.hour in (5, 6, 7, 8):
        return "gap"
    hour = now.hour
    if hour in (5, 6):    # 5am–7am   → 6am full scan
        return "full"
    if hour in (8, 9):    # 8am–10am  → 9am morning check
        return "morning"
    if hour in (16, 17):  # 4pm–6pm   → 5pm pre-London check
        return "prelondon"
    if hour in (22, 23):  # 10pm–12am → 11pm pre-New York check
        return "preny"
    # Off-hours fallback — log a warning so it is visible in GitHub Actions logs
    print(
        f"[scan] WARNING — Auckland hour={hour} is outside all defined scan windows "
        f"(5-6=full, 8-9=morning, 16-17=prelondon, 22-23=preny) and SCAN_MODE env var is unset. "
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

    if scan_mode == "gap":
        return "⏰ Next scan Monday 6am Auckland — full market analysis"
    if scan_mode == "full" and is_weekday:
        return "⏰ Next scan today at 9am Auckland"
    if scan_mode == "morning" and is_weekday:
        return "⏰ Next scan today at 5pm Auckland — Pre-London check"
    if scan_mode == "prelondon" and is_weekday:
        return "⏰ Next scan tonight at 11pm Auckland — Pre-New York check"
    if scan_mode == "preny":
        return "⏰ Next scan tomorrow 6am Auckland — have a good night."
    return f"⏰ Next full scan {nxt_short} at 6am Auckland"


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

    # ── Scan mode (needed by guard log and stored in guard state) ────────────
    scan_mode = _get_scan_mode()
    print(
        f"[scan] mode={scan_mode} ({_SCAN_MODES[scan_mode][0]}) "
        f"— detected from Auckland hour={_startup_ak.hour}",
        file=sys.stderr,
    )

    # ── Duplicate-run guard ────────────────────────────────────────────────────
    # State is stored in data/run_guard.json (committed to git after every run
    # so it persists across GitHub Actions stateless runners).  The guard blocks
    # any run that starts within _COOLDOWN_SECS of the previous one — prevents
    # cron-job.org retries and backup-schedule double-fires from both executing.
    try:
        _RUN_GUARD_FILE.parent.mkdir(parents=True, exist_ok=True)
        _guard_state: dict = {}
        if _RUN_GUARD_FILE.exists():
            try:
                _guard_state = json.loads(_RUN_GUARD_FILE.read_text(encoding="utf-8"))
            except Exception:
                _guard_state = {}

        _last_ts_str = _guard_state.get("timestamp", "")
        print(f"[guard] File: {_RUN_GUARD_FILE} · exists={_RUN_GUARD_FILE.exists()} · stored_timestamp={_last_ts_str!r} · now_utc={_now_utc.isoformat()}", file=sys.stderr)
        if _last_ts_str:
            _last_ts  = datetime.fromisoformat(_last_ts_str)
            _elapsed  = (_now_utc - _last_ts).total_seconds()
            _el_min   = int(_elapsed / 60)
            _el_sec   = int(_elapsed % 60)
            _cdmin    = _COOLDOWN_SECS // 60
            if _elapsed < _COOLDOWN_SECS:
                _rem_min = int((_COOLDOWN_SECS - _elapsed) / 60)
                print(
                    f"[guard] Last run: {_el_min}m {_el_sec}s ago "
                    f"(mode={_guard_state.get('mode','?')}) — "
                    f"cooldown {_cdmin} minutes — BLOCKED "
                    f"({_rem_min}m remaining). Exiting.",
                    file=sys.stderr,
                )
                return 0
            print(
                f"[guard] Last run: {_el_min}m ago "
                f"(mode={_guard_state.get('mode','?')}) — "
                f"cooldown {_cdmin} minutes — PROCEEDING",
                file=sys.stderr,
            )
        else:
            print(
                f"[guard] No previous run on record — "
                f"cooldown {_COOLDOWN_SECS // 60} minutes — PROCEEDING",
                file=sys.stderr,
            )

        # Write guard state immediately so any queued concurrent run sees it
        _RUN_GUARD_FILE.write_text(
            json.dumps({"timestamp": _now_utc.isoformat(), "mode": scan_mode}, indent=2),
            encoding="utf-8",
        )
    except Exception as _guard_err:
        print(f"[guard] run_guard.json check failed ({_guard_err}) — proceeding.", file=sys.stderr)

    # ── 45-minute run-time guard ───────────────────────────────────────────────
    # cron-job.org treats jobs that run > ~60min as failed and queues a retry.
    # If the scan is still running at 45 min we send a plain-English Telegram
    # warning so recipients know a report is coming, preventing a retry-triggered
    # duplicate scan.
    _MAX_RUN_SECS = 45 * 60
    _timeout_fired = [False]

    def _timeout_handler() -> None:
        _timeout_fired[0] = True
        _telegram(
            "⚠️ Scan is taking longer than expected — still running — full report coming shortly"
        )

    _timeout_timer = threading.Timer(_MAX_RUN_SECS, _timeout_handler)
    _timeout_timer.daemon = True
    _timeout_timer.start()

    _telegram_test()

    missing = config.missing_keys()
    if missing:
        print("ERROR: missing API keys in .env: " + ", ".join(missing), file=sys.stderr)
        return 2

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
            _telegram(
                "🤖 FOREX AI — 6am scan starting — full market analysis running — "
                "full report in approximately 20 minutes"
            )
        else:
            print(
                f"[startup-ping] WARNING — scan_mode='full' but Auckland hour={now_ak.hour} "
                f"is outside expected window {_exp_lo}h–{_exp_hi}h. "
                f"Skipping startup ping to avoid a confusing '6am full scan' message at {now_ak.hour}h. "
                f"Check cron-job.org trigger time and SCAN_MODE env var.",
                file=sys.stderr,
            )
    elif scan_mode == "gap":
        _telegram(
            "🤖 FOREX AI — Sunday gap scan starting — checking for weekend price gaps — "
            "brief report in a few minutes"
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
                log=lambda m: _log_line(logf, m),
                scan_mode=scan_mode,
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
            research_outcome_checker.check_post_close_trades(
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
        # Issue 4: reduce Twelve Data pre-warm cap for afternoon scans to cut cost
        # 6am: 20 deep + 10 research sweep = 30; afternoon: 20 deep + 5 sweep = 25
        _td_cap    = 30 if scan_mode == "full" else 25
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

        # 4. Pre-fetch Twelve Data candles — 20 pairs for 6am, 15 for afternoon scans
        # Reducing from 25×4TF=100 to 15×4TF=60 calls on afternoon scans saves ~6-7 min.
        _warm_cap = 20 if scan_mode == "full" else 15
        try:
            from src import technical as _tech
            _tech.warm_cache(pre_filtered[:_warm_cap], log=lambda m: _log_line(logf, m))
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

        # Load pair performance map for per-pair threshold overrides
        _pair_perf_map: dict = {}
        try:
            from src import selector as _sel_pm
            _pair_perf_map = _sel_pm.load_pair_performance()
        except Exception:
            pass

        def _process_batch(pairs, force_deep=False):
            nonlocal filtered_count
            for pair in pairs:
                if pair in analysed_pairs:
                    _log_line(logf, f"  {pair}: CACHE HIT (already analysed this run)")
                    continue
                analysed_pairs.add(pair)
                # Per-pair threshold: 70%+ win rate from 10+ trades → lower threshold to 5.5
                _pth_override = None
                _pp = _pair_perf_map.get(pair, {})
                if isinstance(_pp, dict) and _pp.get("wr", 0) >= 0.70 and _pp.get("n", 0) >= 10:
                    _pth_override = 5.5
                    _log_line(logf, f"  {pair}: proven edge (wr={_pp['wr']:.0%}, n={_pp['n']}) — threshold lowered to 5.5")
                result = _analyse_pair(
                    pair, logf,
                    force_deep=force_deep,
                    shared_fundamental=_shared_fund.get(pair),
                    shared_macro=_shared_macro,
                    sonnet_threshold=sonnet_thresh,
                    pair_threshold_override=_pth_override,
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

        # Research trading mode: paper-trade conf>=4 pairs (0.01 lots)
        # conf-4 "borderline" setups are tracked separately — they build ML training
        # data and reveal where the real profitable threshold actually sits.
        try:
            from src import research_tracker as _rt
            _rt_today = {
                (r["pair"], (r.get("direction") or "").upper())
                for r in _rt.load()
                if r.get("date") == date
            }
            _rt_logged = 0

            def _log_one_research(r_result, src_override=None, mode_override=None):
                """Log one analysis result as a research trade. Returns True if logged."""
                nonlocal _rt_logged
                _rconf = _conf(r_result)
                if _rconf < 4:
                    return False
                _rp   = r_result["parsed"]
                _rdir = (_rp.get("direction") or "").upper()
                if (r_result["pair"], _rdir) in _rt_today:
                    return False
                _rsrc = src_override or (
                    "sonnet"
                    if all(_rp.get(k) for k in ("entry", "stop_loss", "target"))
                    else "haiku"
                )
                if _rsrc in ("haiku", "haiku_sweep"):
                    _ind_e, _ind_s, _ind_t, _ = _calc_indicative_levels(
                        r_result["pair"], _rp, r_result.get("bundle", {}),
                        research_mode=True,   # tighter 1.0x ATR target for faster ML signal
                    )
                    if _ind_e and _ind_s and _ind_t:
                        _rp = dict(_rp)
                        _rp["entry"]     = _rp.get("entry")     or _ind_e
                        _rp["stop_loss"] = _rp.get("stop_loss") or _ind_s
                        _rp["target"]    = _rp.get("target")    or _ind_t
                        _rsrc = ("indicative" if _rconf >= 5 else "indicative_borderline") \
                                if _rsrc == "haiku" else "haiku_sweep"
                    elif _rconf < 5:
                        _rsrc = "haiku_borderline" if _rsrc == "haiku" else "haiku_sweep_borderline"
                _smode = mode_override or scan_mode

                # ── Collect extended entry-context fields ─────────────────────
                _bundle_rt = r_result.get("bundle", {})
                _tech_rt   = (_bundle_rt.get("technical") or {}) if isinstance(_bundle_rt, dict) else {}
                _daily_rt  = (_tech_rt.get("daily") or {})       if isinstance(_tech_rt, dict) else {}
                _rib_rt    = (_daily_rt.get("ribbon") or {})      if isinstance(_daily_rt, dict) else {}
                _mtf_rt    = (_bundle_rt.get("mtf") or {})        if isinstance(_bundle_rt, dict) else {}
                _fa_rt     = r_result.get("_fundamental_alignment") or {}
                _fa_rt     = _fa_rt if isinstance(_fa_rt, dict) else {}

                # Grade from quality_grades (computed during scoring)
                _grade_rt  = _quality_grades.get(r_result["pair"], {}).get("grade", "")

                # Correlation agreement: count other pairs with same base CCY and direction
                _pair_base_rt = r_result["pair"].split("/")[0].upper() if "/" in r_result["pair"] else ""
                _corr_count   = sum(
                    1 for _r2 in deep_results
                    if _r2.get("pair", "") != r_result["pair"]
                    and "/" in _r2.get("pair", "")
                    and _r2["pair"].split("/")[0].upper() == _pair_base_rt
                    and (_r2.get("parsed", {}).get("direction") or "").upper() == _rdir
                )

                # Market regime — use global macro detector (cached; accurate for ML features)
                try:
                    from src import market_regime as _mr_rt
                    _regime_rt = _mr_rt.detect().get("regime", "ranging_low_vol")
                except Exception:
                    # Fallback: MTF-based heuristic
                    _mtf_cnt_rt = int(_mtf_rt.get("agreeing_count", 0) or 0) \
                                  if isinstance(_mtf_rt, dict) else 0
                    _risk_ccys  = {"AUD", "NZD", "CAD", "EUR", "GBP"}
                    _safe_ccys  = {"JPY", "CHF"}
                    _regime_rt  = "ranging_low_vol"
                    if _mtf_cnt_rt >= 2:
                        if (_rdir == "BUY" and _pair_base_rt in _risk_ccys) or \
                           (_rdir == "SELL" and _pair_base_rt in _safe_ccys):
                            _regime_rt = "trending_risk_on"
                        else:
                            _regime_rt = "trending_risk_off"

                # Auckland time context
                try:
                    _aknow_rt = _auckland_now()
                    _dow_rt   = _aknow_rt.isoweekday()   # 1=Mon … 5=Fri
                    _hour_rt  = _aknow_rt.hour
                except Exception:
                    _dow_rt   = datetime.now().isoweekday()
                    _hour_rt  = datetime.now().hour

                _extra_rt = {
                    # Score breakdown
                    "tech_score":             _rp.get("technical_score", ""),
                    "fund_score":             _rp.get("fundamental_score", ""),
                    "sent_score":             _rp.get("sentiment_score", ""),
                    "pos_score":              _rp.get("positioning_score", ""),
                    "macro_score":            _rp.get("macro_score", ""),
                    "mtf_count":              _mtf_cnt_rt,
                    "cot_momentum":           r_result.get("_cot_signal", ""),
                    "fundamental_alignment":  _fa_rt.get("alignment", ""),
                    "fund_aligned_count":     _fa_rt.get("aligned", ""),
                    "grade":                  _grade_rt,
                    "ribbon_state":           _rib_rt.get("status", ""),
                    "divergence_type":        _daily_rt.get("divergence", ""),
                    # Entry quality from technical bundle
                    "rsi_at_entry":           _daily_rt.get("rsi14", ""),
                    "bb_position":            _daily_rt.get("bb_position", ""),
                    "price_vs_200ma":         _daily_rt.get("price_vs_200ma", ""),
                    # Market context
                    "market_regime":          _regime_rt,
                    "patience_score_at_entry": r_result.get("_patience_score", ""),
                    "day_of_week":            _dow_rt,
                    "hour_auckland":          _hour_rt,
                    "corr_agreement_count":   _corr_count,
                }
                # ─────────────────────────────────────────────────────────────

                _rt_id = _rt.log_research_trade(
                    r_result["pair"], _rp, _rsrc, _smode,
                    extra_fields=_extra_rt,
                )
                try:
                    from src import feature_extractor as _fe, feature_store as _fs
                    _feat = _fe.extract(
                        r_result["pair"],
                        r_result["parsed"],
                        _bundle_rt,
                        extra_data=_extra_rt,
                    )
                    _fs.save("research", _rt_id, _feat)
                except Exception:
                    pass
                _rt_today.add((r_result["pair"], _rdir))
                _rt_logged += 1
                return True

            # Pass 1: deep analysis results (conf>=4, includes borderline conf-4)
            for _r in deep_results:
                _log_one_research(_r)
            if _rt_logged:
                _log_line(logf, f"Research mode: {_rt_logged} trade(s) from deep analysis (conf>=4).")

            # Pass 2: research sweep — Haiku-only on all pre-filtered pairs not yet analysed.
            # Uses Twelve Data data already cached by warm_cache (no extra API calls).
            # _TD_CACHE_MAX controls how many pairs are pre-warmed; increase it for wider coverage.
            _already_in_deep = {r["pair"] for r in deep_results}
            # Issue 4: limit research sweep — 10 extra pairs for 6am, 5 for afternoon scans
            _sweep_limit = 10 if scan_mode == "full" else 5
            _sweep_candidates = [p for p in pre_filtered if p not in _already_in_deep][:_sweep_limit]
            if _sweep_candidates:
                _log_line(logf,
                    f"Research sweep: Haiku-only scan of {len(_sweep_candidates)} additional "
                    f"pre-warmed pairs (limit={_sweep_limit} for {scan_mode})...")
                _sweep_new = 0
                for _sp in _sweep_candidates:
                    try:
                        _sr = _analyse_pair(
                            _sp, logf,
                            force_deep=False,
                            shared_fundamental=_shared_fund.get(_sp),
                            shared_macro=_shared_macro,
                            sonnet_threshold=99,
                        )
                        if _sr and not _sr.get("screened_out") and _conf(_sr) >= 4:
                            if _log_one_research(_sr, src_override="haiku_sweep",
                                                 mode_override=f"{scan_mode}_sweep"):
                                _sweep_new += 1
                    except Exception:
                        pass
                if _sweep_new:
                    _log_line(logf, f"Research sweep: {_sweep_new} additional trade(s) logged.")

            # Running total — visible in every GitHub Actions run
            try:
                _rt_all  = _rt.load()
                _rt_open = sum(1 for r in _rt_all if r.get("status") == "OPEN")
                _log_line(logf,
                    f"Research trades: {_rt_logged} new this scan "
                    f"(total: {len(_rt_all)} | open: {_rt_open})")
            except Exception:
                if _rt_logged:
                    _log_line(logf, f"Research trades opened this scan: {_rt_logged}")

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
        _dd_tier_alert_lines = []
        try:
            from src import risk_manager
            risk_profile    = risk_manager.load_profile()
            _old_dd_mode    = risk_profile.get("drawdown_mode", "normal")
            risk_state      = risk_manager.compute_risk_state(risk_profile)
            exposure        = risk_manager.compute_open_exposure(risk_profile)
            sized = [
                risk_manager.size_trade_from_result(r, risk_profile, risk_state)
                for r in deep_results if r["parsed"].get("trade_this") == "YES"
            ]
            sized = risk_manager.apply_correlation_checks(sized)
            risk_profile.update({
                "risk_mode":               risk_state["risk_mode"],
                "drawdown_mode":           risk_state["drawdown_mode"],
                "dd_tier_entered_balance": risk_state["dd_tier_entered_balance"],
                "dd_tier_entered_peak":    risk_state["dd_tier_entered_peak"],
                "consecutive_losses":      risk_state["consecutive_losses"],
                "consecutive_wins":        risk_state["consecutive_wins"],
                "last_5_win_rate":         risk_state["last_5_win_rate"],
                "total_open_pct":          exposure["total_pct"],
            })
            risk_manager.save_profile(risk_profile)
            risk_data = {
                "profile":      risk_profile,
                "risk_state":   risk_state,
                "exposure":     exposure,
                "sized_trades": sized,
            }
            # Build tier transition alert if drawdown mode changed
            _new_dd_mode = risk_state.get("drawdown_mode", "normal")
            if risk_state.get("drawdown_mode_changed") and _new_dd_mode != _old_dd_mode:
                _fund_bal  = risk_profile.get("estimated_balance", risk_manager.FUND_START)
                _fund_peak = risk_profile.get("peak_balance", _fund_bal)
                _dd_tier_alert_lines = risk_manager.drawdown_tier_alert_lines(
                    _old_dd_mode, _new_dd_mode, risk_state, _fund_bal, _fund_peak
                )
                _log_line(logf, f"[DRAWDOWN] Tier transition: {_old_dd_mode} → {_new_dd_mode} "
                          f"(dd={risk_state.get('drawdown_pct',0)*100:.1f}%)")
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

        # 10. Send drawdown tier transition alert (separate message, sent before main summary)
        if _dd_tier_alert_lines:
            try:
                _telegram("\n".join(_dd_tier_alert_lines))
            except Exception as _dda_exc:
                _log_line(logf, f"Drawdown tier alert send failed: {_dda_exc}")

        # 11. Send Telegram summary
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

    _timeout_timer.cancel()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
