"""Daily automation runner (intended for a 6am scheduled task).

Sequence:
  1. Refresh learning memory from any outcomes recorded since the last run.
  2. Smart pair selection: score all 21 liquid pairs by 24h movement, 5-day
     momentum, and upcoming economic events; pick the top 10.
  3. Analyse each selected pair (Haiku stage-1 screen → Sonnet deep analysis).
  4. Regenerate the HTML dashboard.

Each pair is fault-isolated: one failure (rate limit, bad symbol) is logged and the
run continues. A per-run log is written to data/reports/daily_<date>.log.
"""

import sys
import traceback
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


def _log_line(handle, msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line)
    handle.write(line + "\n")
    handle.flush()


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
        actionable = []
        filtered_count = 0
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
                if p["trade_this"] == "YES":
                    actionable.append(f"{result['pair']} {p['direction']} (conf {p['confidence']})")
            except Exception as exc:  # noqa: BLE001
                _log_line(logf, f"FAILED {pair}: {exc}")
                traceback.print_exc(file=logf)

        passed = len(pairs_today) - filtered_count
        _log_line(logf, f"Stage-1 filter: {filtered_count}/{len(pairs_today)} pairs "
                        f"screened out, {passed} passed to deep analysis.")

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
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
