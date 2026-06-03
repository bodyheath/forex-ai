"""Daily automation runner (intended for a 6am scheduled task).

Sequence:
  1. Refresh learning memory from any outcomes recorded since the last run.
  2. Smart pair selection: score all 21 liquid pairs by 24h movement, 5-day
     momentum, and upcoming economic events; pick the top 10.
  3. Analyse each selected pair (Haiku stage-1 screen → Sonnet deep analysis).
  4. Regenerate the HTML dashboard.
  5. Send a Telegram summary (full alert for YES trades, brief note if none).

Each pair is fault-isolated: one failure (rate limit, bad symbol) is logged and the
run continues. A per-run log is written to data/reports/daily_<date>.log.
"""

import sys
import traceback
import urllib.parse
import urllib.request
from datetime import datetime

# Force UTF-8 stdout/stderr so analyst output with em-dashes/emoji never crashes
# the run when output is redirected (e.g. under Task Scheduler).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import config
from src import dashboard, learning, selector, service

# ---------------------------------------------------------------------------
# Session windows in NZT — mirrors src/dashboard.py so both stay in sync.
# Priority order determines which session wins for mixed pairs (e.g. EUR/JPY → London).
# ---------------------------------------------------------------------------
_SESSION_NZT = {
    "EUR": ("London",   "5pm – 9pm NZT"),
    "GBP": ("London",   "5pm – 9pm NZT"),
    "CHF": ("London",   "5pm – 9pm NZT"),
    "JPY": ("Tokyo",    "9am – 2pm NZT"),
    "USD": ("New York", "10pm – 2am NZT"),
    "CAD": ("New York", "10pm – 2am NZT"),
    "AUD": ("Sydney",   "7am – 12pm NZT"),
    "NZD": ("Sydney",   "7am – 12pm NZT"),
}
_SESSION_PRIORITY = ["EUR", "GBP", "CHF", "AUD", "NZD", "JPY", "USD", "CAD"]


def _session_label(pair: str) -> str:
    """Return 'Session name — HH:HH NZT' for the pair's primary trading window."""
    cleaned = pair.upper().replace("/", "").replace("-", "")
    base = cleaned[:3]
    quote = cleaned[3:6] if len(cleaned) >= 6 else ""
    for ccy in _SESSION_PRIORITY:
        if ccy in (base, quote):
            name, window = _SESSION_NZT[ccy]
            return f"{name} session — {window}"
    return "London session — 5pm – 9pm NZT"


def _log_line(handle, msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line)
    handle.write(line + "\n")
    handle.flush()


def _telegram(message: str) -> None:
    """Send a Telegram message. Silently skips if credentials are not configured."""
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception:  # noqa: BLE001
        pass  # never let a notification failure break the run


def _fmt_price(v) -> str:
    """Format a price value from recparse (float or None) cleanly."""
    if v is None:
        return "—"
    f = float(v)
    # JPY-quoted pairs have values > 10; use 3 dp. Others use 5 dp.
    return f"{f:.3f}" if f > 10 else f"{f:.5f}"


def _send_telegram_summary(date: str, pairs_today: list, filtered_count: int,
                            deep_results: list) -> None:
    yes_trades = [r for r in deep_results if r["parsed"].get("trade_this") == "YES"]
    passed = len(pairs_today) - filtered_count
    lines = []

    # ── Main body ────────────────────────────────────────────────────────────
    if yes_trades:
        lines.append(f"🚨 <b>TRADE ALERT — {date}</b>")
        lines.append("")
        for r in yes_trades:
            p = r["parsed"]
            pair      = r["pair"]
            direction = (p.get("direction") or "?").upper()
            conf      = p.get("confidence") or "?"
            entry     = _fmt_price(p.get("entry"))
            stop      = _fmt_price(p.get("stop_loss"))
            target    = _fmt_price(p.get("target"))
            rr_raw    = p.get("reward_risk")
            rr        = f"{float(rr_raw):.2f}:1" if rr_raw is not None else "—"
            arrow     = "📈" if direction == "BUY" else "📉"
            session   = _session_label(pair)

            lines += [
                "━━━━━━━━━━━━━━━━━━━━━",
                f"{arrow} <b>{pair} — {direction}</b>",
                f"Confidence:   <b>{conf}/10</b>",
                f"Entry:        <code>{entry}</code>",
                f"Stop Loss:    <code>{stop}</code>",
                f"Target:       <code>{target}</code>",
                f"Reward:Risk   <b>{rr}</b>",
                f"⏰ <b>{session}</b>",
            ]
        lines.append("━━━━━━━━━━━━━━━━━━━━━")
    else:
        lines.append(f"<b>forex-ai — {date}</b>")
        lines.append("")
        lines.append("No setups today — market conditions not ideal.")

    # ── Summary footer (always shown) ────────────────────────────────────────
    lines.append("")
    lines.append(
        f"<i>Screened {len(pairs_today)} pairs · "
        f"{filtered_count} filtered at stage 1 · "
        f"{passed} deep analysis</i>"
    )
    for r in deep_results:
        p = r["parsed"]
        trade  = p.get("trade_this", "NO")
        conf   = p.get("confidence") or "?"
        dirn   = (p.get("direction") or "—").upper()
        marker = "🟢" if trade == "YES" else "⚪"
        lines.append(f"  {marker} {r['pair']}  {dirn}  {conf}/10")

    _telegram("\n".join(lines))


def run() -> int:
    missing = config.missing_keys()
    if missing:
        print("ERROR: missing API keys in .env: " + ", ".join(missing), file=sys.stderr)
        return 2

    date = datetime.now().strftime("%Y-%m-%d")
    log_path = config.REPORTS_DIR / f"daily_{date}.log"
    with log_path.open("a", encoding="utf-8") as logf:
        _log_line(logf, f"=== Daily run {date} | universe: {len(selector.UNIVERSE)} pairs ===")

        # 1. Learn from prior outcomes first.
        try:
            stats = learning.update_memory()
            _log_line(logf, f"Learning refreshed: {stats['closed']} closed trades, "
                            f"win rate {('%.0f%%' % (stats['win_rate']*100)) if stats['win_rate'] is not None else 'n/a'}, "
                            f"{stats['patterns_written']} auto-patterns written.")
        except Exception as exc:  # noqa: BLE001
            _log_line(logf, f"Learning step failed: {exc}")

        # 2. Smart pair selection from the extended universe.
        try:
            pairs_today = selector.select_pairs(
                top_n=10,
                log=lambda m: _log_line(logf, m),
            )
            _log_line(logf, f"Selected {len(pairs_today)} pairs for analysis: "
                            f"{', '.join(pairs_today)}")
        except Exception as exc:  # noqa: BLE001
            _log_line(logf, f"Smart selection failed ({exc}) — falling back to watchlist.")
            pairs_today = list(config.WATCHLIST)

        # 3. Analyse each selected pair (two-stage: Haiku screen → Sonnet deep).
        filtered_count = 0
        deep_results = []
        for pair in pairs_today:
            try:
                result = service.analyse_and_log(pair, log=lambda m: _log_line(logf, m))
                if result.get("screened_out"):
                    filtered_count += 1
                    s = result["screen"]
                    _log_line(logf, f"  {result['pair']}: FILTERED stage-1 "
                                    f"(score {s['score']}/5 — {s['reason']})")
                    continue
                p = result["parsed"]
                verdict = f"{p['trade_this']} | conf {p['confidence']} | {p['direction']}"
                _log_line(logf, f"#{result['id']} {result['pair']}: {verdict}")
                deep_results.append(result)
            except Exception as exc:  # noqa: BLE001
                _log_line(logf, f"FAILED {pair}: {exc}")
                traceback.print_exc(file=logf)

        passed = len(pairs_today) - filtered_count
        _log_line(logf, f"Stage-1 filter: {filtered_count}/{len(pairs_today)} pairs "
                        f"screened out, {passed} passed to deep analysis.")

        actionable = [
            f"{r['pair']} {r['parsed']['direction']} (conf {r['parsed']['confidence']})"
            for r in deep_results if r["parsed"].get("trade_this") == "YES"
        ]

        # 4. Rebuild dashboard.
        try:
            path = dashboard.generate()
            _log_line(logf, f"Dashboard updated: {path}")
        except Exception as exc:  # noqa: BLE001
            _log_line(logf, f"Dashboard step failed: {exc}")

        if actionable:
            _log_line(logf, "ACTIONABLE TODAY: " + "; ".join(actionable))
        else:
            _log_line(logf, "No actionable setups today (all TRADE_THIS: NO).")
        _log_line(logf, "=== Daily run complete ===")

        # 5. Telegram summary.
        _send_telegram_summary(date, pairs_today, filtered_count, deep_results)

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
