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
  6. Send a Telegram summary in the standard three-section format.

Each pair is fault-isolated: one failure is logged and the run continues.
A per-run log is written to data/reports/daily_<date>.log.
"""

import sys
import traceback
import urllib.parse
import urllib.request
from datetime import datetime

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import config
from src import dashboard, learning, selector, service

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
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    recipients = [config.TELEGRAM_CHAT_ID]
    if config.TELEGRAM_CHAT_ID_2:
        recipients.append(config.TELEGRAM_CHAT_ID_2)
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    for chat_id in recipients:
        try:
            data = urllib.parse.urlencode({
                "chat_id": chat_id,
                "text": message,
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
    """Return confidence score as int (0 if unparseable)."""
    try:
        return int(result["parsed"].get("confidence") or 0)
    except (TypeError, ValueError):
        return 0


def _analyse_pair(pair: str, logf, force_deep: bool = False) -> dict | None:
    """Run analyse_and_log for one pair; return result or None on exception."""
    try:
        return service.analyse_and_log(pair, log=lambda m: _log_line(logf, m), force_deep=force_deep)
    except Exception as exc:
        _log_line(logf, f"FAILED {pair}: {exc}")
        traceback.print_exc(file=logf)
        return None


def _send_telegram_summary(
    date: str,
    universe_size: int,
    total_scanned: int,
    deep_results: list,
    closed_today: list = None,
    new_patterns: list = None,
    stats: dict = None,
) -> None:
    """Build and send the Telegram message (3 trade sections + learning update)."""
    yes_trades  = [r for r in deep_results if r["parsed"].get("trade_this") == "YES"]
    watch_list  = sorted(
        [r for r in deep_results
         if r["parsed"].get("trade_this") != "YES" and 5 <= _conf(r) <= 6],
        key=_conf, reverse=True,
    )[:3]

    best = None
    if deep_results:
        best = max(deep_results, key=_conf)

    lines = []

    # ── Header ───────────────────────────────────────────────────────────────
    lines.append(
        f"<b>Forex AI — {date}</b>\n"
        f"<i>Scanned {universe_size} total pairs · Deep analysed {len(deep_results)} pairs</i>"
    )

    # ── Section 1: TRADE ALERTS ──────────────────────────────────────────────
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🚨 <b>SECTION 1 — TRADE ALERTS</b>")
    if yes_trades:
        for r in yes_trades:
            p         = r["parsed"]
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
            bet_raw   = p.get("best_entry_time") or ""
            bet       = bet_raw[:80] if bet_raw else session

            lines += [
                "",
                f"{arrow} <b>{pair} — {direction}</b>",
                f"Confidence:  <b>{conf}/10</b>",
                f"Entry:       <code>{entry}</code>",
                f"Stop Loss:   <code>{stop}</code>",
                f"Target:      <code>{target}</code>",
                f"Reward:Risk  <b>{rr}</b>",
                f"⏰ <b>{bet}</b>",
            ]
    else:
        lines.append("No pairs met the 7+ confidence threshold today.")

    # ── Section 2: WATCH LIST ────────────────────────────────────────────────
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("👀 <b>SECTION 2 — WATCH LIST</b>  <i>(confidence 5-6, approaching signal)</i>")
    if watch_list:
        for r in watch_list:
            p    = r["parsed"]
            pair = r["pair"]
            conf = p.get("confidence") or "?"
            dirn = (p.get("direction") or "—").upper()
            kt   = (p.get("key_thesis") or "").strip()
            note = (kt[:120] + "…") if len(kt) > 120 else kt
            lines += [
                "",
                f"⏳ <b>{pair}</b> {dirn} — conf <b>{conf}/10</b>",
                f"   {note}" if note else "   Analysis available.",
            ]
    else:
        lines.append("No pairs in the 5-6 confidence range today.")

    # ── Section 3: BEST OPPORTUNITY TODAY ────────────────────────────────────
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("⭐ <b>SECTION 3 — BEST OPPORTUNITY TODAY</b>")
    if best:
        p      = best["parsed"]
        pair   = best["pair"]
        conf   = p.get("confidence") or "?"
        dirn   = (p.get("direction") or "—").upper()
        arrow  = "📈" if dirn == "BUY" else "📉"
        kt     = (p.get("key_thesis") or "").strip()
        reason = (kt[:150] + "…") if len(kt) > 150 else kt
        lines += [
            "",
            f"{arrow} <b>{pair}</b> — {dirn}  conf <b>{conf}/10</b>",
            f"   {reason}" if reason else "   See full analysis in dashboard.",
        ]
    else:
        lines.append("No pairs were deeply analysed today.")

    # ── Section 4: LEARNING UPDATE ────────────────────────────────────────────
    closed_today  = closed_today  or []
    new_patterns  = new_patterns  or []
    if closed_today or new_patterns:
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        lines.append("🧠 <b>SECTION 4 — LEARNING UPDATE</b>")
        if closed_today:
            lines.append("<b>Trades closed today:</b>")
            for t in closed_today:
                s = (t.get("status") or "").upper()
                arrow = "✅" if s == "WIN" else "❌" if s == "LOSS" else "⏰"
                rm = t.get("r_multiple")
                try:
                    r_txt = f" ({float(rm):+.2f}R)" if rm not in (None, "") else ""
                except (TypeError, ValueError):
                    r_txt = ""
                lines.append(
                    f"  {arrow} #{t.get('id')} {t.get('pair')} "
                    f"{t.get('direction','')} — {s}{r_txt}"
                )
        if new_patterns:
            lines.append(f"💡 <b>{len(new_patterns)} new pattern(s) learned:</b>")
            for p in new_patterns:
                lines.append(f"  · {p[:100]}{'…' if len(p) > 100 else ''}")
        if stats:
            wr = stats.get("win_rate")
            wr_txt = f"{wr*100:.0f}%" if wr is not None else "n/a"
            dec = stats.get("decisive", 0)
            wins = stats.get("wins", 0)
            losses = stats.get("losses", 0)
            lines.append(
                f"📊 Overall win rate: <b>{wr_txt}</b> "
                f"({wins}W / {losses}L, {dec} decisive trades)"
            )

    # ── Footer ────────────────────────────────────────────────────────────────
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>Next scan 6am NZT tomorrow</i>")

    _telegram("\n".join(lines))


def run() -> int:
    missing = config.missing_keys()
    if missing:
        print("ERROR: missing API keys in .env: " + ", ".join(missing), file=sys.stderr)
        return 2

    date = datetime.now().strftime("%Y-%m-%d")
    log_path = config.REPORTS_DIR / f"daily_{date}.log"
    with log_path.open("a", encoding="utf-8") as logf:

        # 0. Automatic outcome detection + win/loss analysis.
        #    Must run before the learning refresh so newly closed trades are
        #    included in the stats and any extracted patterns land in memory
        #    before today's analysis reads memory.render().
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

        # 1. Learn from prior outcomes (includes trades just closed above).
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
        universe_size = len(selector.UNIVERSE)  # fallback default for log header
        ranked_all = []
        try:
            selection = selector.select_pairs(
                top_n=15,
                log=lambda m: _log_line(logf, m),
            )
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

        # Top 10 by pre-score always reach deep analysis regardless of stage-1 result.
        force_deep_pairs = set(pairs_today[:10])

        _log_line(logf, f"=== Daily run {date} | universe: {universe_size} pairs ===")

        # 3. Analyse initial batch (top 15).
        filtered_count = 0
        deep_results   = []
        analysed_pairs = set()

        def _process_batch(pairs):
            nonlocal filtered_count
            for pair in pairs:
                if pair in analysed_pairs:
                    continue
                analysed_pairs.add(pair)
                result = _analyse_pair(pair, logf, force_deep=pair in force_deep_pairs)
                if result is None:
                    continue
                if result.get("screened_out"):
                    filtered_count += 1
                    s = result["screen"]
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

        # 4. Auto-expand: ensure at least 3 meaningful results or 25 deep-analysed.
        meaningful = [r for r in deep_results if _conf(r) >= 5]
        next_idx = 15  # next position in ranked_all to draw from

        while len(meaningful) < 3 and len(deep_results) < 25 and next_idx < len(ranked_all):
            extra_pairs = [p for p, _ in ranked_all[next_idx:next_idx + 10]]
            next_idx += 10
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
            f"meaningful(conf>=5)={len(meaningful)}",
        )

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

        # 6. Telegram summary.
        _send_telegram_summary(
            date=date,
            universe_size=universe_size,
            total_scanned=len(analysed_pairs),
            deep_results=deep_results,
            closed_today=closed_today,
            new_patterns=new_patterns,
            stats=learning_stats,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
