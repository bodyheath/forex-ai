"""Between-scan trade monitor: batch price + OHLCV candle cascade detection.

Runs every 2 hours via GitHub Actions schedule.  Catches milestones (T1/T2/T3
and stop hits) that occur between regular scans, including price spikes that
fully reverse before the next scheduled scan.

Strategy:
  1. Batch current price fetch for all open trades (2 API calls max, 20/batch).
  2. Classify trades into HOT/WARM/COLD zones by proximity to next cascade target.
  3. Fetch 4 hourly OHLCV candles for HOT+WARM zone trades (1 API call per pair).
  4. Detect milestones using candle HIGH (BUY targets/SELL stops) and
     candle LOW (SELL targets/BUY stops) — catches spikes that reversed.
  5. Apply cascade updates to trades.csv / research_trades.csv.
  6. Send individual Telegram alerts for fund trade milestones.
  7. Send one batch Telegram summary if any research milestones were hit.
  8. Update MFE/MAE for research trades using OHLCV extremes.
  9. Write data/monitor_log.json every run.
"""

import json
import os
import time
from datetime import date as _date_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import config
from src import cascade as _casc

try:
    from src import discord_notifier as _dn
except Exception:
    _dn = None


def _online_learn_closure(source_table: str, updated: dict, log=print) -> None:
    """Feed a monitor-closed trade to the online learner (best-effort)."""
    try:
        from src import online_learner as _ol
        _ol.partial_fit_trade(
            source_table,
            updated.get("id"),
            updated.get("status", ""),
            updated.get("closed_at", ""),
        )
    except Exception:
        pass
    # Fund trades: train the fund-specific ML model (features stored at trade open)
    if source_table == "main":
        try:
            from src.online_learner import OnlineLearner as _OL_mon
            _fund_ol = _OL_mon()
            _pips    = float(updated.get("pips") or 0)
            outcome  = 1 if _pips > 0 else 0
            _fund_ol.train_fund_outcome(
                trade_id=str(updated.get("id", "")),
                outcome=outcome,
                pips=_pips,
            )
        except Exception as _e:
            log(f"[ML] train error: {_e}")


def _analyse_loss(trade: dict, prices: dict, log_fn=None) -> dict:
    """Use Claude Haiku to analyse why a trade lost and what to learn from it."""
    _log = log_fn or print

    pair      = str(trade.get("pair", ""))
    direction = str(trade.get("direction", "")).upper()
    entry     = float(trade.get("entry", 0) or 0)
    exit_p    = float(trade.get("exit_price", 0) or 0)
    pips      = float(trade.get("pips", 0) or 0)
    conf      = float(trade.get("confidence", 0) or 0)
    regime    = str(trade.get("regime_at_entry", "") or trade.get("market_regime", "") or "")
    rsi       = float(trade.get("rsi_at_entry", 0) or 0)
    weekly    = str(trade.get("weekly_trend_at_entry", "") or trade.get("weekly_trend", "") or "")
    monthly   = str(trade.get("monthly_trend_at_entry", "") or trade.get("monthly_trend", "") or "")
    stop_pips = float(trade.get("stop_pips_at_entry", 0) or 0)
    rr        = float(trade.get("rr_at_entry", 0) or trade.get("reward_risk", 0) or 0)
    session   = str(trade.get("session_at_entry", "") or "")
    cons_l    = int(trade.get("consecutive_losses_at_entry", 0) or 0)

    prompt = f"""You are a forex trading analyst.
A trade just closed as a LOSS. Analyse why it failed and what should be learned.

TRADE DETAILS:
Pair: {pair}
Direction: {direction}
Entry: {entry:.5f}
Exit: {exit_p:.5f}
Loss: {pips:.1f} pips
Confidence at entry: {conf}/10

CONDITIONS AT ENTRY:
Market regime: {regime}
Session: {session}
RSI: {rsi:.1f}
Weekly trend: {weekly}
Monthly trend: {monthly}
Stop distance: {stop_pips:.0f} pips
R:R ratio: {rr:.2f}
Consecutive losses before this: {cons_l}

Analyse this loss and respond in this EXACT format:

ROOT_CAUSE: [one sentence — the main reason this trade failed]

WARNING_SIGNS: [comma-separated list of signals present at entry that should have warned against this trade]

WHAT_TO_AVOID: [one sentence — specific condition to avoid in future]

RULE_TO_ADD: [one specific rule in IF/THEN format that would have prevented this loss. Example: IF regime=RANGING and direction opposes weekly trend THEN block trade]

CONFIDENCE: [0.0-1.0 how confident you are in this analysis]"""

    try:
        import requests as _req
        import os as _os
        response = _req.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         _os.getenv("ANTHROPIC_API_KEY", ""),
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 400,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )

        if response.status_code == 200:
            text = response.json()["content"][0]["text"]
            import re as _re
            analysis = {
                "trade_id":  trade.get("id"),
                "pair":      pair,
                "direction": direction,
                "pips":      pips,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "raw_analysis": text,
            }
            for field, key in [
                ("ROOT_CAUSE",   "root_cause"),
                ("WHAT_TO_AVOID","what_to_avoid"),
                ("RULE_TO_ADD",  "rule_to_add"),
            ]:
                m = _re.search(f"{field}:\\s*(.+?)(?=\\n[A-Z_]+:|$)", text, _re.DOTALL)
                analysis[key] = m.group(1).strip() if m else ""

            m_warn = _re.search(r"WARNING_SIGNS:\s*(.+?)(?=\n[A-Z_]+:|$)", text, _re.DOTALL)
            analysis["warning_signs"] = (
                [w.strip() for w in m_warn.group(1).split(",")]
                if m_warn else []
            )
            m_conf = _re.search(r"CONFIDENCE:\s*([0-9.]+)", text)
            analysis["confidence_in_analysis"] = float(m_conf.group(1)) if m_conf else 0.5

            _log(f"[loss-analysis] {pair} {direction}: {analysis.get('root_cause', '')[:80]}")
            return analysis

        _log(f"[loss-analysis] API error: {response.status_code}")
        return {}
    except Exception as exc:
        _log(f"[loss-analysis] Error: {exc}")
        return {}


def _save_loss_analysis(analysis: dict, log_fn=None) -> None:
    """Append a loss analysis to data/loss_journal.json."""
    _log = log_fn or print
    import shutil as _sh
    try:
        journal_path = config.DATA_DIR / "loss_journal.json"
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except Exception:
            journal = {"analyses": [], "extracted_rules": [], "pattern_counts": {}}

        journal["analyses"].append(analysis)

        # Track pattern frequency in root causes
        root = analysis.get("root_cause", "")
        if root:
            patterns = journal.get("pattern_counts", {})
            for word in ["ranging", "trend", "regime", "correlation", "oversold",
                         "overbought", "session", "stop", "rr", "timing", "news", "spread"]:
                if word.lower() in root.lower():
                    patterns[word] = patterns.get(word, 0) + 1
            journal["pattern_counts"] = patterns

        # Deduplicate and append extracted rules
        rule = analysis.get("rule_to_add", "")
        existing_rules = [r.get("rule", "") for r in journal.get("extracted_rules", [])]
        if rule and rule not in existing_rules:
            journal["extracted_rules"].append({
                "rule":               rule,
                "pair":               analysis.get("pair"),
                "added":              analysis.get("timestamp"),
                "times_triggered":    0,
                "times_prevented_loss": 0,
            })

        tmp = str(journal_path) + ".tmp"
        Path(tmp).write_text(json.dumps(journal, indent=2), encoding="utf-8")
        _sh.move(tmp, str(journal_path))
        _log("[loss-analysis] Saved to loss_journal.json OK")
    except Exception as exc:
        _log(f"[loss-analysis] Save error: {exc}")

_PRICE_URL    = "https://api.twelvedata.com/price"
_OHLCV_URL    = "https://api.twelvedata.com/time_series"
_MONITOR_LOG  = config.DATA_DIR / "monitor_log.json"
_MILESTONE_LOG = config.DATA_DIR / "milestone_log.json"
_LOCK_FILE    = config.DATA_DIR / "monitor.lock"
_API_USAGE    = config.DATA_DIR / "api_usage.json"
_HEARTBEAT_FILE     = config.DATA_DIR / "heartbeat.json"
_MARKET_REOPEN_FILE = config.DATA_DIR / "market_reopen.json"
_OHLCV_CANDLES = 6          # hourly candles per pair (Yahoo Finance 1H)
_API_BUDGET_LIMIT = 700     # daily TD call threshold (for batch price fetch logging)
_MONITOR_HISTORY  = config.DATA_DIR / "monitor_history.json"
_HOT_ZONE_ALERTS  = config.DATA_DIR / "hot_zone_alerts.json"
_BATCH_SIZE   = 20          # pairs per batch price request
_FETCH_TIMEOUT = 15         # seconds per HTTP request
_HOT_THRESHOLD  = 0.70      # price ≥ 70% of way to target → HOT
_WARM_THRESHOLD = 0.40      # price ≥ 40% of way to target → WARM
_HOT_ALERT_COOLDOWN_HOURS = 4   # suppress duplicate HOT zone alerts within this window
_DEDUP_HOURS  = 24          # suppress duplicate milestone alerts within this window
_LOCK_TIMEOUT = 120         # seconds before a stale lock is removed


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _update_trailing_stop(trade_row: dict, current_price: float,
                          log_fn=None) -> dict:
    """Update effective_stop based on cascade levels hit.

    T1 hit → stop to breakeven (entry)
    T2 hit → stop to T1 price  (locks 40% profit)
    T3 hit → stop to T2 price  (locks 40%+30% profit)
    Price > 50% of T2→T3 range → trail stop to T2

    Returns dict with new_effective_stop, stop_moved, reason.
    """
    _log = log_fn or print
    pair      = str(trade_row.get("pair", ""))
    direction = str(trade_row.get("direction", "")).upper()
    entry     = _to_float(trade_row.get("entry")) or 0
    t1_price  = _to_float(trade_row.get("t1_price")) or 0
    t2_price  = _to_float(trade_row.get("t2_price")) or 0
    t3_price  = _to_float(trade_row.get("t3_price") or trade_row.get("target")) or 0
    t1_hit    = _is_true(trade_row.get("t1_hit"))
    t2_hit    = _is_true(trade_row.get("t2_hit"))
    t3_hit    = _is_true(trade_row.get("t3_hit"))
    current_eff = _to_float(trade_row.get("effective_stop")) or 0
    new_stop    = current_eff or (_to_float(trade_row.get("stop_loss")) or 0)
    reason      = "no change"
    moved       = False

    if not t1_hit:
        return {"new_effective_stop": new_stop, "stop_moved": False, "reason": "T1 not hit"}

    def _better(candidate):
        if direction == "BUY":
            return candidate > new_stop
        return candidate < new_stop

    if t3_hit and t2_price > 0:
        if _better(t2_price):
            new_stop, moved, reason = t2_price, True, "T3 hit — trailing to T2"
    elif t2_hit and t1_price > 0:
        if _better(t1_price):
            new_stop, moved, reason = t1_price, True, "T2 hit — trailing to T1"
    elif t1_hit and entry > 0:
        if _better(entry):
            new_stop, moved, reason = entry, True, "T1 hit — trailing to BE"

    # Mid-zone: price > 50% from T2 toward T3 → trail stop to T2
    if t2_hit and not t3_hit and t2_price and t3_price:
        if direction == "BUY":
            halfway = t2_price + (t3_price - t2_price) * 0.5
            if current_price >= halfway and _better(t2_price):
                new_stop, moved, reason = t2_price, True, "> 50% to T3 — trailing to T2"
        else:
            halfway = t2_price - (t2_price - t3_price) * 0.5
            if current_price <= halfway and _better(t2_price):
                new_stop, moved, reason = t2_price, True, "> 50% to T3 — trailing to T2"

    if moved:
        _log(f"[trailing] {pair} {direction}: stop moved "
             f"{current_eff:.5f} → {new_stop:.5f} ({reason})")

    return {"new_effective_stop": new_stop, "stop_moved": moved, "reason": reason}


def _get_td_calls_today() -> int:
    try:
        usage = json.loads(_API_USAGE.read_text(encoding="utf-8")) if _API_USAGE.exists() else {}
        return int(usage.get("calls", 0)) if usage.get("date") == str(_date_mod.today()) else 0
    except Exception:
        return 0


def _increment_td_usage(n: int = 1) -> None:
    try:
        usage: dict = {}
        if _API_USAGE.exists():
            try:
                usage = json.loads(_API_USAGE.read_text(encoding="utf-8"))
            except Exception:
                usage = {}
        today = str(_date_mod.today())
        if usage.get("date") != today:
            usage = {"date": today, "calls": 0}
        usage["calls"] = int(usage.get("calls", 0)) + n
        _API_USAGE.write_text(json.dumps(usage), encoding="utf-8")
    except Exception:
        pass


def _auckland_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Pacific/Auckland"))
    except Exception:
        from datetime import timezone
        utc = datetime.now(timezone.utc)
        off = 13 if utc.month in (10, 11, 12, 1, 2, 3) else 12
        return utc.astimezone(timezone(timedelta(hours=off)))


def _is_weekend() -> bool:
    return _auckland_now().weekday() >= 5


def _is_true(val) -> bool:
    """Return True when a CSV boolean field is set.

    Handles str "TRUE"/"1"/"YES", bool True, and int 1 so callers never need
    to know whether the value came from a CSV row (string) or Python code.
    """
    return str(val).strip().upper() in ("TRUE", "1", "YES")


def _verify_fund_state(fund_open: list, log=print) -> None:
    """Log a fund state integrity snapshot and flag anomalies."""
    try:
        from src import fund_state as _fs_int
        fs = _fs_int.load()
        bal = fs.get("daily_opening_balance", 0)
        pnl = fs.get("daily_pnl_dollars", 0)
        open_count = len(fund_open)
        log(
            f"[integrity] Fund state: opening_balance=${bal:,.2f} "
            f"daily_pnl=${pnl:+.2f} open={open_count} trades"
        )
        if open_count > 4:
            log(f"[integrity] WARNING {open_count} open fund trades exceeds maximum of 4")
        # Flag trades that look closed (have exit_price) but are still OPEN
        for row in fund_open:
            ep = row.get("exit_price", "")
            if ep not in ("", "nan", "None", None, 0, "0"):
                try:
                    if float(str(ep)) > 0:
                        log(
                            f"[integrity] WARNING #{row.get('id')} {row.get('pair')} "
                            f"shows OPEN but has exit_price={ep} — may need manual close"
                        )
                except (ValueError, TypeError):
                    pass
    except Exception as exc:
        log(f"[integrity] Check failed: {exc}")


def _write_monitor_log(data: dict) -> None:
    try:
        _MONITOR_LOG.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── HOT zone alert deduplication ─────────────────────────────────────────────
# Uses a dedicated hot_zone_alerts.json written IMMEDIATELY on each alert send,
# rather than relying on monitor_log.json (written at end-of-run).
# This prevents duplicate alerts when concurrent monitor runs both read a stale
# monitor_log.json from git before the previous run has committed.

def _check_hot_alert_sent(pair: str, is_currently_hot: bool = True) -> str | None:
    """Return human-readable elapsed string if this pair is still in cooldown, else None.

    Cooldown rules:
    - If continuously_hot (price never left HOT zone since last alert): 24-hour cooldown
    - Otherwise: standard _HOT_ALERT_COOLDOWN_HOURS cooldown
    """
    try:
        if not _HOT_ZONE_ALERTS.exists():
            return None
        data       = json.loads(_HOT_ZONE_ALERTS.read_text(encoding="utf-8"))
        entry      = data.get("pairs", {}).get(pair) or {}
        # Support legacy format: {"alerts_sent": {pair: ts}}
        if not entry:
            ts_str = data.get("alerts_sent", {}).get(pair)
            if not ts_str:
                return None
            entry = {"last_alert_sent": ts_str, "continuously_hot": False}

        ts_str   = entry.get("last_alert_sent", "")
        if not ts_str:
            return None
        ts       = datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S")
        elapsed  = datetime.now(timezone.utc).replace(tzinfo=None) - ts
        cont_hot = entry.get("continuously_hot", False)
        cooldown = timedelta(hours=24) if cont_hot else timedelta(hours=_HOT_ALERT_COOLDOWN_HOURS)

        if elapsed < cooldown:
            mins = int(elapsed.total_seconds() / 60)
            elapsed_str = f"{mins} minutes ago" if mins < 60 else f"{int(mins / 60)}h{mins % 60:02d}m ago"
            if cont_hot:
                return f"{elapsed_str} (continuously HOT — 24h cooldown)"
            return elapsed_str
    except Exception:
        pass
    return None


def _record_hot_alert_sent(pair: str, continuously_hot: bool = False) -> None:
    """Immediately write pair + UTC timestamp to hot_zone_alerts.json.

    Uses a sidecar lock file to prevent interleaved writes from concurrent runs.
    Tracks first_entered_hot and continuously_hot state.
    """
    _lock = Path(str(_HOT_ZONE_ALERTS) + ".lock")
    try:
        # Acquire tiny lock (up to 3 seconds)
        for _ in range(30):
            try:
                fd = os.open(str(_lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                time.sleep(0.1)

        data: dict = {"alerts_sent": {}, "pairs": {}}
        if _HOT_ZONE_ALERTS.exists():
            try:
                data = json.loads(_HOT_ZONE_ALERTS.read_text(encoding="utf-8"))
            except Exception:
                data = {"alerts_sent": {}, "pairs": {}}

        now_str = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
        data.setdefault("alerts_sent", {})[pair] = now_str   # keep legacy format
        data.setdefault("pairs", {})
        existing = data["pairs"].get(pair, {})
        data["pairs"][pair] = {
            "first_entered_hot": existing.get("first_entered_hot") or now_str,
            "last_alert_sent":   now_str,
            "continuously_hot":  continuously_hot,
        }
        _HOT_ZONE_ALERTS.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass
    finally:
        try:
            _lock.unlink()
        except Exception:
            pass


def _mark_hot_zone_exited(pair: str) -> None:
    """Called when a pair leaves the HOT zone — resets continuously_hot flag."""
    _lock = Path(str(_HOT_ZONE_ALERTS) + ".lock")
    try:
        for _ in range(30):
            try:
                fd = os.open(str(_lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                time.sleep(0.1)

        if not _HOT_ZONE_ALERTS.exists():
            return
        data = json.loads(_HOT_ZONE_ALERTS.read_text(encoding="utf-8"))
        if pair in data.get("pairs", {}):
            data["pairs"][pair]["continuously_hot"] = False
        _HOT_ZONE_ALERTS.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass
    finally:
        try:
            _lock.unlink()
        except Exception:
            pass


def _build_hot_trade_lines(row: dict, price: float | None, pair: str) -> list:
    """Return detail lines for one HOT trade — handles both target-approaching and
    stop-approaching cases with correct direction-aware progress percentages."""
    direction = (row.get("direction") or "").upper()
    entry     = _to_float(row.get("entry"))
    stop      = _to_float(row.get("effective_stop") or row.get("stop_loss"))

    pip_factor = 0.01 if "JPY" in pair else 0.0001
    decimals   = 3    if "JPY" in pair else 5

    def _fmt(v):
        return f"{v:.{decimals}f}" if v is not None else "?"

    # Determine next unmet target
    if not _is_true(row.get("t1_hit")):
        level, tgt = "T1", _to_float(row.get("t1_price"))
    elif not _is_true(row.get("t2_hit")):
        level, tgt = "T2", _to_float(row.get("t2_price"))
    else:
        level, tgt = "T3", _to_float(row.get("t3_price") or row.get("target"))

    lines = []
    if price is None or entry is None or direction not in ("BUY", "SELL"):
        if tgt is not None:
            lines.append(f"{level} target: {_fmt(tgt)}  ·  Current price: {_fmt(price)}")
        return lines

    # Direction-aware progress toward target
    if tgt is not None:
        total = abs(tgt - entry)
        progress = (
            ((price - entry) / total) if direction == "BUY"
            else ((entry - price) / total)
        ) if total > 0 else 0.0

        if progress >= 1.0:
            # Past the target — milestone should have been recorded this run
            lines.append(f"🎯 {level} ALREADY CROSSED — {int(progress * 100)}% — recording milestone now")
        elif progress >= 0:
            lines.append(f"Progress: {int(progress * 100)}% of the way to {level}")
        else:
            # Negative progress = price moved against trade → HOT is due to stop proximity
            if stop is not None:
                stop_range = abs(entry - stop)
                stop_prox = (
                    max(0.0, (entry - price) / stop_range) if direction == "BUY"
                    else max(0.0, (price - entry) / stop_range)
                ) if stop_range > 0 else 0.0
                lines.append(
                    f"⚠️ Approaching stop loss — {int(stop_prox * 100)}% of the way to stop"
                )
            else:
                lines.append(f"⚠️ Price moving against trade direction")

        dist_pips = abs(tgt - price) / pip_factor
        lines.append(f"{level} target: {_fmt(tgt)}  ·  Current price: {_fmt(price)}")
        lines.append(f"Distance remaining: {dist_pips:.1f} pips")
    else:
        lines.append(f"Current price: {_fmt(price)}")

    if stop is not None:
        dist_stop = abs(price - stop) / pip_factor
        lines.append(f"Stop: {_fmt(stop)}  ·  {dist_stop:.1f} pips away")

    return lines


def _build_hot_alert_message(pair: str, hot_rows: list, price: float | None) -> str:
    """Build the HOT zone Telegram alert for a pair.

    Accepts all HOT-zone rows for the pair so multiple trades are shown in one
    message rather than sending separate alerts per trade ID.
    """
    pip_factor = 0.01 if "JPY" in pair else 0.0001  # noqa: F841

    # Determine header: "approaching target" vs "approaching stop"
    # Use first row to decide the primary reason for HOT
    _first = hot_rows[0] if hot_rows else {}
    _dir   = (_first.get("direction") or "").upper()
    _entry = _to_float(_first.get("entry"))
    _price = price
    _stop  = _to_float(_first.get("effective_stop") or _first.get("stop_loss"))

    _approaching_stop = False
    if _price is not None and _entry is not None and _dir in ("BUY", "SELL"):
        if not _is_true(_first.get("t1_hit")):
            _tgt = _to_float(_first.get("t1_price"))
        elif not _is_true(_first.get("t2_hit")):
            _tgt = _to_float(_first.get("t2_price"))
        else:
            _tgt = _to_float(_first.get("t3_price") or _first.get("target"))
        if _tgt is not None:
            _tot = abs(_tgt - _entry)
            _prog = (
                (_price - _entry) / _tot if _dir == "BUY" else (_entry - _price) / _tot
            ) if _tot > 0 else 0.0
            _approaching_stop = _prog < 0

    if _approaching_stop:
        header = f"⚠️ <b>{pair} approaching stop loss</b>"
    else:
        header = f"\U0001f525 <b>{pair} approaching target</b>"

    lines = [header, ""]
    if len(hot_rows) > 1:
        lines.append(f"{len(hot_rows)} open trades on this pair:")

    for _row in hot_rows:
        trade_id = _row.get("id", "?")
        _d       = (_row.get("direction") or "").upper()
        if len(hot_rows) > 1:
            lines.append(f"\nTrade #{trade_id} ({_d})")
        else:
            lines.append(f"{_d} · Trade #{trade_id}")
        lines.extend(_build_hot_trade_lines(_row, price, pair))

    lines += ["", "Monitor checking every 30 minutes ✅"]
    return "\n".join(lines)


# ── Milestone deduplication log ───────────────────────────────────────────────

def _load_milestone_log() -> dict:
    try:
        if _MILESTONE_LOG.exists():
            return json.loads(_MILESTONE_LOG.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"sent": []}


def _check_milestone_sent(pair: str, level: str, hours: int = _DEDUP_HOURS) -> str | None:
    """Return ISO timestamp if this pair/level was already sent within `hours`, else None."""
    log_data = _load_milestone_log()
    cutoff   = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    for entry in log_data.get("sent", []):
        if entry.get("pair") == pair and entry.get("level") == level:
            ts_str = entry.get("timestamp", "")
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ")
                if ts > cutoff:
                    return ts_str
            except Exception:
                pass
    return None


def _record_milestone_sent(pair: str, level: str, trade_id: int,
                           trade_type: str = "fund") -> None:
    """Record that a milestone alert was sent — persists to milestone_log.json."""
    log_data = _load_milestone_log()
    sent     = log_data.get("sent", [])
    sent.append({
        "pair":       pair,
        "level":      level,
        "trade_id":   trade_id,
        "trade_type": trade_type,
        "timestamp":  datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    log_data["sent"] = sent[-500:]   # keep last 500 entries (~3 weeks at normal pace)
    try:
        _MILESTONE_LOG.write_text(json.dumps(log_data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── File lock ─────────────────────────────────────────────────────────────────

def _try_acquire_lock(log=print) -> bool:
    """Atomically create monitor.lock. Returns True if lock was acquired."""
    def _create_lock() -> bool | None:
        """Try once to create the lock file. Returns True on success,
        None if file already exists, True if non-FileExistsError (proceed anyway)."""
        try:
            fd = os.open(str(_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            return None
        except Exception:
            return True  # if we can't create the lock, proceed without it

    result = _create_lock()
    if result is True:
        return True

    # Lock file exists — check age
    try:
        age = time.time() - _LOCK_FILE.stat().st_mtime
        if age > _LOCK_TIMEOUT:
            try:
                _LOCK_FILE.unlink()
            except OSError as _unlink_err:
                log(f"Monitor: stale lock ({age:.0f}s) removal failed: {_unlink_err} — skipping run")
                return False
            log(f"Monitor: stale lock ({age:.0f}s old) removed — re-acquiring")
            # Single direct retry; no recursion
            return _create_lock() is True
    except Exception:
        pass
    return False


def _release_lock() -> None:
    try:
        _LOCK_FILE.unlink()
    except Exception:
        pass


# ── Dashboard sender ──────────────────────────────────────────────────────────

_DASHBOARD_MSG_FILE = Path("data/discord_dashboard.json")

def _send_dashboard(state: dict, log_fn=None) -> None:
    """Build and post (or edit) the Discord fund dashboard from a pre-computed state dict."""
    _log = log_fn or print
    try:
        from src import discord_notifier as _dn_dash
        embed = _dn_dash.build_fund_dashboard_embed(state)

        # Load saved message ID if it exists
        _msg_id = None
        try:
            if _DASHBOARD_MSG_FILE.exists():
                import json as _json_dash
                _saved = _json_dash.loads(_DASHBOARD_MSG_FILE.read_text(encoding="utf-8"))
                _msg_id = _saved.get("message_id")
        except Exception:
            pass

        import requests as _req_dash
        _webhook = getattr(_dn_dash, "WEBHOOK_FUND", None)
        if not _webhook:
            try:
                import os as _os_dash
                _webhook = _os_dash.environ.get("DISCORD_WEBHOOK_FUND") or _os_dash.environ.get("DISCORD_WEBHOOK")
            except Exception:
                pass

        if not _webhook:
            _log("  [dashboard] No webhook URL — skipping Discord post")
            return

        payload = {"embeds": [embed]}

        if _msg_id:
            # Edit existing message (webhook message edit endpoint)
            _edit_url = f"{_webhook.rstrip('/')}/messages/{_msg_id}?wait=true"
            resp = _req_dash.patch(_edit_url, json=payload, timeout=15)
            if resp.status_code in (200, 204):
                _log(f"  [dashboard] Edited Discord dashboard (msg {_msg_id})")
                return
            else:
                _log(f"  [dashboard] Edit failed ({resp.status_code}) — reposting")

        # Post new message
        _post_url = _webhook.rstrip("/") + "?wait=true"
        resp = _req_dash.post(_post_url, json=payload, timeout=15)
        if resp.status_code in (200, 204):
            try:
                _new_id = resp.json().get("id")
                if _new_id:
                    import json as _json2
                    _DASHBOARD_MSG_FILE.parent.mkdir(parents=True, exist_ok=True)
                    _DASHBOARD_MSG_FILE.write_text(
                        _json2.dumps({"message_id": _new_id}, indent=2), encoding="utf-8"
                    )
                    _log(f"  [dashboard] Posted Discord dashboard (msg {_new_id})")
            except Exception as _e:
                _log(f"  [dashboard] Posted but couldn't save msg ID: {_e}")
        else:
            _log(f"  [dashboard] Post failed ({resp.status_code}): {resp.text[:200]}")

    except Exception as _exc:
        import traceback as _tb
        _log(f"  [dashboard] ERROR: {_exc}\n{_tb.format_exc()}")


# ── Safe ID helper ───────────────────────────────────────────────────────────

def _safe_int_id(val, default: int = 0) -> int:
    """Convert a trade ID value to int, tolerating empty strings and NaN."""
    try:
        v = str(val).strip()
        if not v or v in ("nan", "None", ""):
            return default
        return int(float(v))
    except (ValueError, TypeError):
        return default


# ── Write-back verification ───────────────────────────────────────────────────

def _verify_milestone_write(rec_id: int, level: str, tracker_mod,
                             pair: str, log=print) -> bool:
    """Read the trade row back from CSV to confirm the milestone was persisted.

    Returns True on success.  Logs a CRITICAL warning if the field is missing
    so the operator knows there is a risk of duplicate alert on the next run.
    """
    field_map = {"T1": "t1_hit", "T2": "t2_hit", "T3": "t3_hit"}
    field = field_map.get(level)
    if not field:
        return True   # STOP has no boolean field to verify
    try:
        rows = tracker_mod.load()
        for row in rows:
            if _safe_int_id(row.get("id", -1), default=-1) == rec_id:
                if _is_true(row.get(field)):
                    return True
                log(
                    f"⚠️ CRITICAL: milestone write failed for {pair} #{rec_id} — "
                    f"{field} not persisted — risk of duplicate alert on next run"
                )
                return False
        log(f"⚠️ CRITICAL: row #{rec_id} {pair} not found during write-back check")
        return False
    except Exception as exc:
        log(f"  Monitor: write-back check error for #{rec_id} {pair}: {exc}")
        return False


# ── Zone classification ───────────────────────────────────────────────────────

def _next_cascade_target(row: dict):
    """Return the next unmet cascade target price, or None."""
    if not _is_true(row.get("t1_hit")):
        return _to_float(row.get("t1_price"))
    if not _is_true(row.get("t2_hit")):
        return _to_float(row.get("t2_price"))
    if not _is_true(row.get("t3_hit")):
        return _to_float(row.get("t3_price") or row.get("target"))
    return None


def _classify_zone(row: dict, price: float) -> str:
    """Return 'HOT', 'WARM', or 'COLD' based on price proximity to target/stop."""
    if price is None:
        return "COLD"
    entry     = _to_float(row.get("entry"))
    stop      = _to_float(row.get("effective_stop") or row.get("stop_loss"))
    direction = (row.get("direction") or "").upper()

    if not entry or not stop or direction not in ("BUY", "SELL"):
        return "COLD"

    # Progress toward next unmet target
    next_tgt = _next_cascade_target(row)
    target_zone = "COLD"
    if next_tgt is not None:
        total = abs(next_tgt - entry)
        if total > 0:
            progress = (
                (price - entry) / total if direction == "BUY"
                else (entry - price) / total
            )
            if progress >= _HOT_THRESHOLD:
                target_zone = "HOT"
            elif progress >= _WARM_THRESHOLD:
                target_zone = "WARM"

    # Proximity to stop
    stop_range = abs(entry - stop)
    stop_zone = "COLD"
    if stop_range > 0:
        stop_prox = (
            max(0.0, (entry - price) / stop_range) if direction == "BUY"
            else max(0.0, (price - entry) / stop_range)
        )
        if stop_prox >= _HOT_THRESHOLD:
            stop_zone = "HOT"
        elif stop_prox >= _WARM_THRESHOLD:
            stop_zone = "WARM"

    # Return worst (most urgent) zone
    order = {"HOT": 0, "WARM": 1, "COLD": 2}
    return min(target_zone, stop_zone, key=lambda z: order[z])


# ── API calls ─────────────────────────────────────────────────────────────────

def _batch_price_fetch(pairs: list, log=print) -> tuple:
    """Return ({pair: float}, calls_made). Batches 20 pairs per request."""
    if not pairs:
        return {}, 0
    prices = {}
    calls  = 0
    _MAX_RETRIES = 2
    for i in range(0, len(pairs), _BATCH_SIZE):
        batch      = pairs[i : i + _BATCH_SIZE]
        symbol_str = ",".join(batch)
        batch_num  = i // _BATCH_SIZE + 1
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = requests.get(
                    _PRICE_URL,
                    params={"symbol": symbol_str, "apikey": config.TWELVE_DATA_KEY},
                    timeout=_FETCH_TIMEOUT,
                )
                if resp.status_code == 429:
                    if attempt < _MAX_RETRIES:
                        log(
                            f"  Monitor: batch price fetch hit rate limit — "
                            f"waiting 60 seconds before retry {attempt + 1} of {_MAX_RETRIES}"
                        )
                        time.sleep(60)
                        continue
                    log(
                        f"  Monitor: batch price fetch hit rate limit after {_MAX_RETRIES} retries — "
                        f"batch {batch_num} skipped"
                    )
                    break
                resp.raise_for_status()
                data = resp.json()
                if len(batch) == 1:
                    if isinstance(data, dict) and "price" in data and data.get("status") != "error":
                        prices[batch[0]] = float(data["price"])
                else:
                    for pair in batch:
                        pd = data.get(pair, {})
                        if isinstance(pd, dict) and "price" in pd and pd.get("status") != "error":
                            prices[pair] = float(pd["price"])
                calls += 1
                break
            except Exception as exc:
                log(f"  Monitor: batch price fetch error (batch {batch_num}): {exc}")
                break
    _increment_td_usage(calls)
    return prices, calls


def _yahoo_price_batch(pairs: list, log=print) -> dict:
    """Fetch current prices for all pairs from Yahoo Finance.

    Uses yfinance.download() for a single batch call across all pairs.
    Returns {pair: float} with the most recent close (~15 min delayed).
    Never raises — returns partial dict on any per-pair failure.
    15-minute delay is fully acceptable for swing trades targeting 40-200 pips.
    """
    try:
        import yfinance as yf
    except ImportError:
        log("[YF-PRICE] yfinance not installed — Yahoo price fetch unavailable")
        return {}

    from src.yahoo_finance import _pair_to_yahoo_symbol

    if not pairs:
        return {}

    sym_to_pair = {_pair_to_yahoo_symbol(p): p for p in pairs}
    symbols     = list(sym_to_pair.keys())

    prices: dict = {}
    try:
        data = yf.download(
            tickers   =" ".join(symbols),
            period    ="1d",
            interval  ="1m",
            progress  =False,
            auto_adjust=True,
            group_by  ="ticker",
        )

        if data is None or (hasattr(data, "empty") and data.empty):
            log("[YF-PRICE] yfinance download returned empty data")
            return {}

        for sym, pair in sym_to_pair.items():
            try:
                if len(symbols) == 1:
                    # Single-ticker: flat column structure
                    col = next((c for c in data.columns
                                if str(c).lower() == "close"), None)
                    if col is not None:
                        vals = data[col].dropna()
                        if not vals.empty:
                            prices[pair] = float(vals.iloc[-1])
                else:
                    # Multi-ticker: MultiIndex — try (ticker, field) then (field, ticker)
                    if hasattr(data.columns, "levels"):
                        lvl0 = [str(c) for c in data.columns.get_level_values(0)]
                        if sym in lvl0:
                            vals = data[sym]["Close"].dropna()
                        else:
                            vals = data["Close"][sym].dropna()
                    else:
                        vals = data[sym]["Close"].dropna()
                    if not vals.empty:
                        prices[pair] = float(vals.iloc[-1])
            except Exception:
                pass

    except Exception as exc:
        log(f"[YF-PRICE] Batch download failed: {exc}")
        return {}

    log(
        f"Monitor: Yahoo Finance prices — {len(prices)}/{len(pairs)} pairs fetched — "
        f"0 Twelve Data calls — 15min delay acceptable for swing trading"
    )
    return prices


def _append_to_monitor_history(result: dict) -> None:
    """Append a compact record of this run to monitor_history.json."""
    try:
        history: dict = {"runs": []}
        if _MONITOR_HISTORY.exists():
            try:
                history = json.loads(_MONITOR_HISTORY.read_text(encoding="utf-8"))
            except Exception:
                history = {"runs": []}
        history.setdefault("runs", [])
        _dt = result.get("data_tiers_used", {})
        history["runs"].append({
            "ts":                 result.get("timestamp", ""),
            "milestones":         len(result.get("milestones_hit", [])),
            "hot":                result.get("hot_zone_count", 0),
            "warm":               result.get("warm_zone_count", 0),
            "td_calls":           result.get("api_calls_used", 0),
            "yf_price_pairs":     result.get("yf_price_pairs", 0),
            "yf_ohlcv_pairs":     result.get("yf_ohlcv_pairs", 0),
            "approaching_alerts": result.get("approaching_alerts", 0),
            "skipped":            result.get("skipped_reason", ""),
            "t1_yf_ohlcv":        _dt.get("t1_yf_ohlcv",      0),
            "t1_td_backup":       _dt.get("t1_td_backup",      0),
            "t1_sq_backup":       _dt.get("t1_sq_backup",      0),
            "t2_synthetic":       _dt.get("t2_synthetic",       0),
            "t3_current_only":    _dt.get("t3_current_only",    0),
            "t4_last_known":      _dt.get("t4_last_known",      0),
            "t0_no_data":         _dt.get("t0_no_data",         0),
        })
        # Keep last 1000 runs (~3 weeks at 30-min intervals)
        history["runs"] = history["runs"][-1000:]
        _MONITOR_HISTORY.write_text(json.dumps(history, indent=2), encoding="utf-8")
    except Exception:
        pass


def build_monitor_weekly_report() -> str:
    """Build a weekly performance summary from monitor_history.json.

    Covers the last 7 days of recorded runs. Returns a formatted string
    suitable for a Telegram message or log output.
    """
    try:
        if not _MONITOR_HISTORY.exists():
            return "Monitor history not yet available."
        history = json.loads(_MONITOR_HISTORY.read_text(encoding="utf-8"))
        runs = history.get("runs", [])
    except Exception as exc:
        return f"Monitor history read error: {exc}"

    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    week   = [r for r in runs if r.get("ts", "") >= cutoff]

    if not week:
        return "No monitor runs recorded in the last 7 days."

    active   = [r for r in week if not r.get("skipped")]
    n_runs       = len(active)
    n_ms         = sum(r.get("milestones", 0) for r in active)
    n_hot        = sum(r.get("approaching_alerts", 0) for r in active)
    yf_price     = sum(r.get("yf_price_pairs", 0) for r in active)
    yf_ohlcv     = sum(r.get("yf_ohlcv_pairs", 0) for r in active)
    td_total     = sum(r.get("td_calls", 0) for r in active)
    pairs_per_run = round(yf_price / n_runs) if n_runs else 0

    return (
        f"Monitor weekly report (last 7 days)\n"
        f"  Runs completed:        {n_runs}\n"
        f"  Data source:           Yahoo Finance (0 Twelve Data calls)\n"
        f"  Rate limit errors:     0\n"
        f"  Prices fetched:        {pairs_per_run} pairs/run avg ({yf_price} total)\n"
        f"  OHLCV candles checked: {pairs_per_run} pairs x {_OHLCV_CANDLES} candles/run ({yf_ohlcv} total fetches)\n"
        f"  Milestones detected:   {n_ms}\n"
        f"  Approaching alerts:    {n_hot}\n"
        f"  TD calls used:         {td_total} (fallback only, normally 0)"
    )


# ── Candle-based milestone detection ─────────────────────────────────────────

def _detect_candle_milestones(row: dict, candles: list, pair: str, log=print) -> tuple:
    """Return (milestones_list, updated_row_state).

    milestones_list: each item is a dict with keys:
        level     — "T1", "T2", "T3", "STOP"
        price     — candle extreme that triggered the level
        candle_dt — datetime string of the candle
        pips      — pip distance from entry (None for STOP)

    Processes candles from oldest to newest, applying greedy cascade so that a
    single candle crossing T1+T2+T3 fires all three in sequence.
    """
    direction = (row.get("direction") or "").upper()
    if direction not in ("BUY", "SELL"):
        return [], row

    row_state  = dict(row)   # mutable local copy for greedy tracking
    milestones = []

    _t1_done  = _is_true(row_state.get("t1_hit"))
    _t2_done  = _is_true(row_state.get("t2_hit"))
    _trade_id = row_state.get("id", "")

    if _t1_done and _t2_done:
        log(f"  Monitor: {pair} #{_trade_id} — T1+T2 already hit — checking T3 only")
    elif _t1_done:
        log(f"  Monitor: {pair} #{_trade_id} — T1 already hit — checking T2+T3")

    for candle in reversed(candles):   # API returns newest first → oldest first
        high = _to_float(candle.get("high"))
        low  = _to_float(candle.get("low"))
        dt   = candle.get("datetime", "")
        if high is None or low is None:
            continue

        # BUY: profit goes up → use HIGH for targets, LOW for stop
        # SELL: profit goes down → use LOW for targets, HIGH for stop
        tgt_price  = high if direction == "BUY" else low
        stop_price = low  if direction == "BUY" else high

        # Greedy cascade: T1 → T2 → T3 — skip already-hit levels
        if not _t1_done and _casc.t1_hit(row_state, tgt_price):
            t1p = _casc.pips_at(row_state.get("entry"), row_state.get("t1_price"), pair, direction)
            row_state.update({
                "t1_hit":       "TRUE",
                "t1_hit_price": tgt_price,
                "t1_hit_pips":  t1p,
                "effective_stop": row_state.get("entry"),
            })
            _t1_done = True
            milestones.append({"level": "T1", "price": tgt_price, "candle_dt": dt, "pips": t1p})
            side_label = "HIGH" if direction == "BUY" else "LOW"
            log(
                f"  Monitor: {pair} candle {side_label} {tgt_price} crossed T1 "
                f"{row.get('t1_price')} during {dt} candle — T1 recorded as hit "
                f"even if current price is now below T1 — partial WIN locked in"
            )

        if not _t2_done and _casc.t2_hit(row_state, tgt_price):
            t2p = _casc.pips_at(row_state.get("entry"), row_state.get("t2_price"), pair, direction)
            row_state.update({
                "t2_hit":       "TRUE",
                "t2_hit_price": tgt_price,
                "t2_hit_pips":  t2p,
            })
            _t2_done = True
            milestones.append({"level": "T2", "price": tgt_price, "candle_dt": dt, "pips": t2p})
            log(f"  Monitor: {pair} T2 hit at {tgt_price} (+{t2p:.1f}p) — 70% banked")

        if _casc.t3_hit(row_state, tgt_price):
            t3p = _casc.pips_at(
                row_state.get("entry"),
                row_state.get("t3_price") or row_state.get("target"),
                pair, direction,
            )
            row_state.update({
                "t3_hit":       "TRUE",
                "t3_hit_price": tgt_price,
                "t3_hit_pips":  t3p,
            })
            milestones.append({"level": "T3", "price": tgt_price, "candle_dt": dt, "pips": t3p})
            log(f"  Monitor: {pair} T3 (FULL_WIN) hit at {tgt_price} (+{t3p:.1f}p)")
            break   # trade closed — no further milestone checks

        # Stop check (only if T3 not hit in this candle)
        elif _casc.effective_stop_hit(row_state, stop_price):
            # Use the stop level (not the candle extreme) as the close price
            eff = _to_float(row_state.get("effective_stop") or row_state.get("stop_loss"))
            milestones.append({"level": "STOP", "price": eff, "candle_dt": dt, "pips": None})
            log(f"  Monitor: {pair} effective stop {eff} hit (candle extreme {stop_price}) during {dt}")
            break

    return milestones, row_state


def _detect_spot_milestones(row: dict, price: float, pair: str) -> tuple:
    """Detect milestones from a single spot price (COLD zone, no OHLCV)."""
    direction = (row.get("direction") or "").upper()
    if direction not in ("BUY", "SELL") or price is None:
        return [], row

    row_state  = dict(row)
    milestones = []

    _t1_done  = _is_true(row_state.get("t1_hit"))
    _t2_done  = _is_true(row_state.get("t2_hit"))
    _trade_id = row_state.get("id", "")

    if _t1_done and _t2_done:
        print(f"  Monitor: {pair} #{_trade_id} — T1+T2 already hit — checking T3 only")
    elif _t1_done:
        print(f"  Monitor: {pair} #{_trade_id} — T1 already hit — checking T2+T3")

    if not _t1_done and _casc.t1_hit(row_state, price):
        t1p = _casc.pips_at(row_state.get("entry"), row_state.get("t1_price"), pair, direction)
        row_state.update({
            "t1_hit": "TRUE", "t1_hit_price": price,
            "t1_hit_pips": t1p, "effective_stop": row_state.get("entry"),
        })
        milestones.append({"level": "T1", "price": price, "candle_dt": None, "pips": t1p})

    if not _t2_done and _casc.t2_hit(row_state, price):
        t2p = _casc.pips_at(row_state.get("entry"), row_state.get("t2_price"), pair, direction)
        row_state.update({"t2_hit": "TRUE", "t2_hit_price": price, "t2_hit_pips": t2p})
        milestones.append({"level": "T2", "price": price, "candle_dt": None, "pips": t2p})

    if _casc.t3_hit(row_state, price):
        t3p = _casc.pips_at(
            row_state.get("entry"),
            row_state.get("t3_price") or row_state.get("target"),
            pair, direction,
        )
        row_state.update({"t3_hit": "TRUE", "t3_hit_price": price, "t3_hit_pips": t3p})
        milestones.append({"level": "T3", "price": price, "candle_dt": None, "pips": t3p})
    elif _casc.effective_stop_hit(row_state, price):
        eff = _to_float(row_state.get("effective_stop") or row_state.get("stop_loss"))
        milestones.append({"level": "STOP", "price": eff, "candle_dt": None, "pips": None})

    return milestones, row_state


# ── Stop alert dedup helpers ─────────────────────────────────────────────────

def _already_sent_stop_alert(trade_id: int, log_fn=None) -> bool:
    """Return True if this trade is already CLOSED in trades.csv.

    Guards against git-conflict reverts: if the auto-commit system reverted our
    closure the trade shows OPEN again, but the milestone log still records the
    sent alert so the cooldown blocks a second one.
    """
    _log = log_fn or print
    try:
        import pandas as _pd_sa
        _df_sa = _pd_sa.read_csv(str(config.TRADES_CSV), encoding="utf-8-sig")
        _matches = _df_sa[_df_sa["id"].astype(str) == str(trade_id)]
        if _matches.empty:
            return False
        _status = str(_matches.iloc[0].get("status", ""))
        if _status not in ("OPEN", "PENDING"):
            _log(f"  Monitor: #{trade_id} already {_status} — skipping duplicate stop alert")
            return True
        return False
    except Exception:
        return False


def _emergency_close_trade(trade_id: int, exit_price: float, log_fn=None) -> bool:
    """Last-resort closure via pandas atomic write when tracker.update_outcome() fails."""
    _log = log_fn or print
    try:
        import pandas as _pd_ec
        from src.trading.financials import atomic_write_csv, utc_now_str, TRADES_CSV as _ec_csv
        _df_ec = _pd_ec.read_csv(str(_ec_csv), encoding="utf-8-sig")
        _matches_ec = _df_ec[_df_ec["id"].astype(str) == str(trade_id)]
        if _matches_ec.empty:
            _log(f"  Monitor: [emergency] #{trade_id} not found in trades.csv")
            return False
        _i_ec = _matches_ec.index[0]
        _df_ec.at[_i_ec, "status"]     = "LOSS"
        _df_ec.at[_i_ec, "exit_price"] = float(exit_price)
        _df_ec.at[_i_ec, "closed_at"]  = utc_now_str()
        if atomic_write_csv(_ec_csv, _df_ec):
            _log(f"  Monitor: [emergency] #{trade_id} force-closed at {exit_price} via fallback")
            return True
        return False
    except Exception as _ec_err:
        _log(f"  Monitor: [emergency] #{trade_id} fallback failed: {_ec_err}")
        return False


# ── Cascade application ───────────────────────────────────────────────────────

def _apply_fund_milestones(row: dict, milestones: list, row_state: dict,
                           log=print, ta=None, is_weekend: bool = False,
                           prices: dict = None) -> list:
    """Apply detected milestones to trades.csv. Return list of closed row dicts."""
    from src import tracker as _trk
    _raw_id   = row.get("id", "")
    rec_id    = _safe_int_id(_raw_id)
    if rec_id == 0:
        log(f"[monitor] SKIP _apply_fund_milestones — invalid trade ID: "
            f"{repr(_raw_id)} pair={row.get('pair')}")
        return []
    pair      = row.get("pair", "")
    direction = (row.get("direction") or "").upper()

    wknd_note = (
        "\n\n📅 <i>Weekend monitor — prices are Friday market close. "
        "Next live prices Monday 6am Auckland. "
        "Milestone recorded and will be confirmed at Monday open.</i>"
        if is_weekend else ""
    )

    closed_rows = []

    for m in milestones:
        level  = m["level"]
        mprice = m["price"]
        pips   = m["pips"]
        cdt    = m["candle_dt"] or ""

        # Deduplication: skip if we already sent this alert within the window
        if level in ("T1", "T2", "T3"):
            _prev_ts = _check_milestone_sent(pair, level)
            if _prev_ts:
                log(
                    f"  Monitor: duplicate alert suppressed — {pair} {level} already sent "
                    f"{_prev_ts} — skipping Telegram"
                )
                # Still apply the CSV update in case the previous write didn't persist
                # (but don't re-send Telegram)
                _send_telegram = False
            else:
                _send_telegram = True
        else:
            _send_telegram = True

        if level == "T1":
            _trk.update_fields(
                rec_id,
                t1_hit="TRUE", t1_hit_price=mprice,
                t1_hit_pips=pips, effective_stop=row.get("entry"),
            )
            _verify_milestone_write(rec_id, "T1", _trk, pair, log=log)
            log(f"  Monitor fund #{rec_id} {pair}: T1 recorded at {mprice} (+{pips:.1f}p)")
            if _send_telegram:
                _record_milestone_sent(pair, "T1", rec_id, trade_type="fund")
            if ta and _send_telegram:
                try:
                    ta.send(
                        f"✅ <b>{pair} has reached its first profit target</b>\n\n"
                        f"35% of position banked at +{pips:.1f} pips.\n"
                        f"Stop loss moved to breakeven — this trade can no longer lose money.\n"
                        f"No action needed from you.\n\n"
                        f"Direction: {direction}  |  T1 price: {mprice}"
                        f"{f'  |  Candle: {cdt}' if cdt else ''}"
                        + wknd_note
                    )
                except Exception:
                    pass
            try:
                if _dn and _send_telegram:
                    _dn.send_fund_milestone(
                        pair, direction, "T1", pips or 0.0,
                        _to_float(row_state.get("entry")) or 0.0, mprice,
                        _to_float(row_state.get("effective_stop") or row_state.get("stop_loss")) or 0.0,
                    )
            except Exception:
                pass

        elif level == "T2":
            _trk.update_fields(
                rec_id, t2_hit="TRUE", t2_hit_price=mprice, t2_hit_pips=pips,
            )
            _verify_milestone_write(rec_id, "T2", _trk, pair, log=log)
            log(f"  Monitor fund #{rec_id} {pair}: T2 recorded at {mprice} (+{pips:.1f}p)")
            if _send_telegram:
                _record_milestone_sent(pair, "T2", rec_id, trade_type="fund")
            if ta and _send_telegram:
                try:
                    ta.send(
                        f"💰 <b>{pair} has reached its second profit target</b>\n\n"
                        f"Another 35% of position banked at +{pips:.1f} pips (70% total).\n"
                        f"Final 30% running toward full target — stop trailed to T1 (+1R locked).\n\n"
                        f"Direction: {direction}  |  T2 price: {mprice}"
                        f"{f'  |  Candle: {cdt}' if cdt else ''}"
                        + wknd_note
                    )
                except Exception:
                    pass
            try:
                if _dn and _send_telegram:
                    _dn.send_fund_milestone(
                        pair, direction, "T2", pips or 0.0,
                        _to_float(row_state.get("entry")) or 0.0, mprice,
                        _to_float(row_state.get("effective_stop") or row_state.get("stop_loss")) or 0.0,
                    )
            except Exception:
                pass

        elif level == "T3":
            _trk.update_fields(
                rec_id, t3_hit="TRUE", t3_hit_price=mprice, t3_hit_pips=pips,
            )
            _verify_milestone_write(rec_id, "T3", _trk, pair, log=log)
            _wp = _casc.weighted_pips(row_state)
            _tp = _casc.total_pips(row_state)
            _trk.update_fields(
                rec_id,
                cascading_total_pips=_tp,
                cascading_total_pips_weighted=_wp,
            )
            t1p_str = f"{_to_float(row_state.get('t1_hit_pips')) or 0:.1f}"
            t2p_str = f"{_to_float(row_state.get('t2_hit_pips')) or 0:.1f}"
            updated = _trk.update_outcome(
                rec_id, "FULL_WIN",
                exit_price=_to_float(row_state.get("t3_price") or row_state.get("target")),
                notes=f"FULL_WIN via monitor: T1+{t1p_str}p T2+{t2p_str}p T3+{pips:.1f}p = {_wp:.1f}p weighted",
                cascading_pips=_wp,
            )
            log(f"  Monitor fund #{rec_id} {pair}: FULL_WIN — {_wp:.1f}p weighted")
            if _send_telegram:
                _record_milestone_sent(pair, "T3", rec_id, trade_type="fund")
            if ta and _send_telegram:
                try:
                    ta.send(
                        f"🎯 <b>{pair} has hit its full profit target</b>\n\n"
                        f"Final 30% of position closed — trade complete.\n"
                        f"T1 +{t1p_str}p (35%)  T2 +{t2p_str}p (35%)  T3 +{pips:.1f}p (30%)\n"
                        f"Weighted total: +{_wp:.1f} pips\n\n"
                        f"Direction: {direction}  |  Final price: {mprice}"
                        + wknd_note
                    )
                except Exception:
                    pass
            try:
                if _dn and _send_telegram:
                    _dn.send_fund_milestone(
                        pair, direction, "T3", pips or 0.0,
                        _to_float(row_state.get("entry")) or 0.0, mprice,
                        _to_float(row_state.get("effective_stop") or row_state.get("stop_loss")) or 0.0,
                    )
            except Exception:
                pass
            closed_rows.append(updated)
            _online_learn_closure("main", updated)
            break   # trade closed — skip further milestones

        elif level == "STOP":
            # Guard: if already CLOSED in CSV, skip entirely (git conflict revert protection)
            if _already_sent_stop_alert(rec_id, log_fn=log):
                break
            casc_oc = _casc.cascade_outcome(row_state)
            _wp     = _casc.weighted_pips(row_state) if casc_oc != "LOSS" else None
            _tp     = _casc.total_pips(row_state)    if casc_oc != "LOSS" else None
            if _wp:
                _trk.update_fields(
                    rec_id,
                    cascading_total_pips=_tp,
                    cascading_total_pips_weighted=_wp,
                )
            updated = _trk.update_outcome(
                rec_id, casc_oc,
                exit_price=mprice,
                notes=f"Monitor: {casc_oc} at {mprice}{f' | cascade: {_wp:.1f}p' if _wp else ''}",
                cascading_pips=_wp,
            )
            log(f"  Monitor fund #{rec_id} {pair}: {casc_oc} at {mprice}")
            # Emergency fallback: if tracker write failed and trade is still OPEN, force-close
            if updated.get("status") in ("OPEN", "PENDING", None, ""):
                log(f"  Monitor: [warning] #{rec_id} still OPEN after update_outcome — running emergency close")
                _emergency_close_trade(rec_id, mprice, log_fn=log)
            # Cooldown: suppress duplicate alerts even if git reverted the closure
            _prev_stop_alert = _check_milestone_sent(pair, "STOP")
            if _prev_stop_alert:
                log(f"  Monitor: STOP alert cooldown active for {pair} (last sent {_prev_stop_alert}) — alerts suppressed")
            else:
                if ta:
                    try:
                        _r_txt = f"R={updated.get('r_multiple')}"
                        ta.send(
                            f"📊 <b>{pair} stop loss triggered — trade closed</b>\n\n"
                            f"Outcome: {casc_oc}  |  Exit: {mprice}  |  {_r_txt}\n"
                            f"Outcome recorded — ML training data updated."
                            + wknd_note
                        )
                    except Exception:
                        pass
            try:
                if _dn and not _prev_stop_alert:
                    _was_protected  = casc_oc != "LOSS"
                    _pip_sz_sh      = 0.01 if "JPY" in pair else 0.0001
                    _ent_sh         = _to_float(row_state.get("entry")) or 0
                    _orig_stop_sh   = _to_float(row_state.get("stop_loss")) or 0
                    _stop_pips_sh   = (abs(_ent_sh - _orig_stop_sh) / _pip_sz_sh
                                       if _ent_sh and _orig_stop_sh else 0)
                    _t1_pips_sh  = 0.0
                    _t2_pips_sh  = 0.0
                    _t2h_sh      = False
                    _t1_dol_sh   = 0.0
                    _t2_dol_sh   = 0.0
                    _net_pips_sh = 0.0
                    _net_dol_sh  = 0.0
                    if _was_protected:
                        _t1p_sh = _to_float(row_state.get("t1_price")) or 0
                        _t2p_sh = _to_float(row_state.get("t2_price")) or 0
                        _t2h_sh = _is_true(row_state.get("t2_hit"))
                        if direction == "BUY":
                            _t1_pips_sh = max(0, (_t1p_sh - _ent_sh) / _pip_sz_sh) if _t1p_sh and _ent_sh else 0.0
                            _t2_pips_sh = max(0, (_t2p_sh - _ent_sh) / _pip_sz_sh) if _t2p_sh and _ent_sh and _t2h_sh else 0.0
                        else:
                            _t1_pips_sh = max(0, (_ent_sh - _t1p_sh) / _pip_sz_sh) if _t1p_sh and _ent_sh else 0.0
                            _t2_pips_sh = max(0, (_ent_sh - _t2p_sh) / _pip_sz_sh) if _t2p_sh and _ent_sh and _t2h_sh else 0.0
                        _sz_pct_sh_raw = _to_float(row_state.get("position_size_pct_at_entry"))
                        _sz_pct_sh = _sz_pct_sh_raw if (_sz_pct_sh_raw and _sz_pct_sh_raw == _sz_pct_sh_raw) else 1.0
                        try:
                            from src import fund_state as _fs_sh
                            _fs_sh_data = _fs_sh.load()
                            _bal_sh = float(_fs_sh_data.get("balance") or _fs_sh_data.get("daily_opening_balance") or 10000)
                        except Exception:
                            _bal_sh = 10000.0
                        _risk_sh     = _sz_pct_sh / 100.0 * max(_bal_sh, 1)
                        _dpp_sh      = _risk_sh / _stop_pips_sh if _stop_pips_sh > 0 else 1.0
                        _t1_dol_sh   = round(_t1_pips_sh * 0.40 * _dpp_sh, 2)
                        _t2_dol_sh   = round(_t2_pips_sh * 0.30 * _dpp_sh, 2)
                        _net_pips_sh = round(_t1_pips_sh * 0.40 + _t2_pips_sh * 0.30, 1)
                        _net_dol_sh  = round(_t1_dol_sh + _t2_dol_sh, 2)
                    else:
                        # Pure loss — full risk amount lost
                        _sz_pct_sl_raw = _to_float(row_state.get("position_size_pct_at_entry"))
                        _sz_pct_sl = _sz_pct_sl_raw if (_sz_pct_sl_raw and _sz_pct_sl_raw == _sz_pct_sl_raw and _sz_pct_sl_raw > 0) else 1.0
                        try:
                            from src import fund_state as _fs_sl
                            _fs_sl_data = _fs_sl.load()
                            _bal_sl = float(_fs_sl_data.get("balance") or _fs_sl_data.get("daily_opening_balance") or 10000)
                        except Exception:
                            _bal_sl = 10000.0
                        _risk_sl  = _sz_pct_sl / 100.0 * max(_bal_sl, 1)
                        _net_pips_sh = -(_stop_pips_sh) if _stop_pips_sh > 0 else -100.0
                        _net_dol_sh  = -(_risk_sl)
                    _casc_lbl_sh = "T1+T2" if _t2h_sh else ("T1" if _was_protected else "")
                    _dn.send_fund_stop_hit(
                        pair, direction,
                        t1_hit=_was_protected,
                        t2_hit=_t2h_sh,
                        t1_pips=round(_t1_pips_sh, 1),
                        t2_pips=round(_t2_pips_sh, 1),
                        t1_dollars=_t1_dol_sh,
                        t2_dollars=_t2_dol_sh,
                        net_pips=_net_pips_sh,
                        net_dollars=_net_dol_sh,
                        cascade_label=_casc_lbl_sh,
                    )
                    # Record that we sent the STOP alert — prevents duplicate on next monitor run
                    _record_milestone_sent(pair, "STOP", rec_id, "fund")
            except Exception:
                pass
            # Sync fund_state.json from trades.csv — single source of truth
            try:
                import pandas as _pd_fin
                from src.trading import financials as _fin
                _df_fin = _pd_fin.read_csv(str(config.TRADES_CSV), encoding="utf-8-sig")
                _prices_fin = prices or {}  # passed from run() — never NameError
                _state_fin = _fin.calculate_fund_state(_df_fin, _prices_fin)
                _ok_fin = _fin.sync_fund_state_json(_state_fin)
                log(f"  Monitor fund #{rec_id} {pair}: fund_state synced — "
                    f"bal=${_state_fin['balance']:,.2f} "
                    f"daily={_state_fin['daily_pnl_dollars']:+.2f} "
                    f"({'ok' if _ok_fin else 'write failed'})")
            except Exception as _fs_exc:
                log(f"  Monitor: fund_state sync failed for {pair}: {_fs_exc}")
            # Safety-net: verify trades.csv reflects the closure (guards against git conflict overwrites)
            try:
                import csv as _csv_sn
                _sn_rows = []
                _sn_written = False
                with config.TRADES_CSV.open("r", encoding="utf-8-sig", newline="") as _sn_fh:
                    _sn_reader = _csv_sn.DictReader(_sn_fh)
                    _sn_fields = _sn_reader.fieldnames or []
                    for _sn_r in _sn_reader:
                        _sn_rows.append(_sn_r)
                _sn_target = next(
                    (r for r in _sn_rows if str(r.get("id", "")) == str(rec_id)), None
                )
                if _sn_target and _sn_target.get("status") == "OPEN":
                    _sn_target["status"]     = updated.get("status", casc_oc)
                    _sn_target["exit_price"] = updated.get("exit_price", mprice)
                    _sn_target["pips"]       = updated.get("pips", "")
                    _sn_target["closed_at"]  = updated.get("closed_at", "")
                    # Atomic write — never truncate the live file directly
                    import shutil as _sn_sh
                    _sn_tmp = str(config.TRADES_CSV) + ".tmp"
                    with open(_sn_tmp, "w", encoding="utf-8", newline="") as _sn_wh:
                        _sn_writer = _csv_sn.DictWriter(_sn_wh, fieldnames=_sn_fields)
                        _sn_writer.writeheader()
                        for _sn_r in _sn_rows:
                            _sn_writer.writerow({k: _sn_r.get(k, "") for k in _sn_fields})
                    _sn_sh.move(_sn_tmp, str(config.TRADES_CSV))
                    _sn_written = True
                    log(f"  Monitor: safety-net rewrite confirmed #{rec_id} closed in trades.csv")
                elif _sn_target:
                    log(f"  Monitor: #{rec_id} trades.csv already shows {_sn_target.get('status')} ✅")
            except Exception as _sn_exc:
                log(f"  Monitor: safety-net check failed for #{rec_id}: {_sn_exc}")
            closed_rows.append(updated)
            _online_learn_closure("main", updated)
            # Loss autopsy — analyse why this trade failed
            if casc_oc == "LOSS":
                log("[loss-analysis] Pure loss — running autopsy...")
                try:
                    _loss_trade_data = dict(row)
                    _loss_trade_data["exit_price"] = mprice
                    _analysis = _analyse_loss(
                        trade=_loss_trade_data,
                        prices=prices or {},
                        log_fn=log,
                    )
                    if _analysis:
                        _save_loss_analysis(analysis=_analysis, log_fn=log)
                        # Send Discord alert for this loss
                        try:
                            if _dn:
                                _loss_dols = 0.0
                                try:
                                    _sz_pct_la = float(row.get("position_size_pct_at_entry") or 1.0)
                                    _bal_la    = 10000.0
                                    try:
                                        from src import fund_state as _fs_la
                                        _fs_la_data = _fs_la.load()
                                        _bal_la = float(
                                            _fs_la_data.get("balance") or
                                            _fs_la_data.get("daily_opening_balance") or 10000
                                        )
                                    except Exception:
                                        pass
                                    _loss_dols = round(-(_sz_pct_la / 100.0 * _bal_la), 2)
                                except Exception:
                                    pass
                                _dn.send_loss_analysis_alert(
                                    trade_id=rec_id,
                                    pair=pair,
                                    direction=direction,
                                    pips=float(updated.get("pips") or pips or 0),
                                    dollars=_loss_dols,
                                    analysis=_analysis,
                                )
                        except Exception as _la_dsc_err:
                            log(f"[loss-analysis] Discord alert failed: {_la_dsc_err}")
                except Exception as _la_err:
                    log(f"[loss-analysis] Failed: {_la_err}")
            break   # trade closed

    return closed_rows


def _apply_research_milestones(row: dict, milestones: list, row_state: dict,
                               log=print, ta=None) -> tuple:
    """Apply detected milestones to research_trades.csv.

    Returns (closed_row_or_None, summary_fragment_str).
    summary_fragment_str is appended to the batch Telegram message.
    """
    from src import research_tracker as _rt
    _raw_id   = row.get("id", "")
    rec_id    = _safe_int_id(_raw_id)
    if rec_id == 0:
        log(f"[monitor] SKIP _apply_research_milestones — invalid trade ID: "
            f"{repr(_raw_id)} pair={row.get('pair')}")
        return None, ""
    pair      = row.get("pair", "")
    direction = (row.get("direction") or "").upper()
    _cs       = _to_float(row.get("checklist_score") or 0)

    closed_row = None
    fragments  = []

    for m in milestones:
        level  = m["level"]
        mprice = m["price"]
        pips   = m["pips"]

        # Deduplication: skip if this milestone was already sent within the window
        if level in ("T1", "T2", "T3"):
            _prev_ts = _check_milestone_sent(pair, level)
            if _prev_ts:
                log(
                    f"  Monitor: duplicate suppressed — {pair} research {level} "
                    f"already sent {_prev_ts} — CSV update applied, Telegram skipped"
                )
                _send_frag = False
            else:
                _send_frag = True
        else:
            _send_frag = True

        if level == "T1":
            _rt.update_fields(
                rec_id,
                t1_hit="TRUE", t1_hit_price=mprice,
                t1_hit_pips=pips, effective_stop=row.get("entry"),
            )
            _verify_milestone_write(rec_id, "T1", _rt, pair, log=log)
            if _send_frag:
                _record_milestone_sent(pair, "T1", rec_id, trade_type="research")
                fragments.append(f"{pair} T1 hit (+{pips:.1f} pips partial WIN)")
            log(f"  Monitor research #{rec_id} {pair}: T1 at {mprice} (+{pips:.1f}p)")

        elif level == "T2":
            _rt.update_fields(
                rec_id, t2_hit="TRUE", t2_hit_price=mprice, t2_hit_pips=pips,
            )
            _verify_milestone_write(rec_id, "T2", _rt, pair, log=log)
            if _send_frag:
                _record_milestone_sent(pair, "T2", rec_id, trade_type="research")
                fragments.append(f"{pair} T2 hit (+{pips:.1f} pips)")
                # Item 10: individual alert for high-quality research T2
                if ta and _cs >= 8.0:
                    try:
                        ta.send(
                            f"🔬 <b>HIGH-QUALITY RESEARCH MILESTONE</b>\n\n"
                            f"{pair} {direction} — T2 hit at {mprice} (+{pips:.1f} pips)\n"
                            f"Checklist score: {_cs:.0f}/10 — strong setup confirmed\n"
                            f"ML training data updated"
                        )
                    except Exception:
                        pass
            log(f"  Monitor research #{rec_id} {pair}: T2 at {mprice} (+{pips:.1f}p)")

        elif level == "T3":
            _rt.update_fields(
                rec_id, t3_hit="TRUE", t3_hit_price=mprice, t3_hit_pips=pips,
            )
            _verify_milestone_write(rec_id, "T3", _rt, pair, log=log)
            _wp = _casc.weighted_pips(row_state)
            _tp = _casc.total_pips(row_state)
            _rt.update_fields(
                rec_id,
                cascading_total_pips=_tp,
                cascading_total_pips_weighted=_wp,
            )
            closed_row = _rt.update_outcome(
                rec_id, "FULL_WIN",
                close_price=_to_float(row_state.get("t3_price") or row_state.get("target")),
                exit_reason="TARGET_HIT",
                cascading_pips=_wp,
            )
            if _send_frag:
                _record_milestone_sent(pair, "T3", rec_id, trade_type="research")
                fragments.append(f"{pair} FULL_WIN (+{_wp:.1f}p weighted)")
                # Item 10: individual alert for high-quality research FULL_WIN
                if ta and _cs >= 8.0:
                    try:
                        ta.send(
                            f"🔬 <b>HIGH-QUALITY RESEARCH FULL WIN</b>\n\n"
                            f"{pair} {direction} — FULL WIN at {mprice} (+{_wp:.1f} pips weighted)\n"
                            f"Checklist score: {_cs:.0f}/10 — all 3 targets hit\n"
                            f"ML training data updated"
                        )
                    except Exception:
                        pass
            _online_learn_closure("research", closed_row)
            log(f"  Monitor research #{rec_id} {pair}: FULL_WIN {_wp:.1f}p")
            break

        elif level == "STOP":
            casc_oc = _casc.cascade_outcome(row_state)
            _wp     = _casc.weighted_pips(row_state) if casc_oc != "LOSS" else None
            _tp     = _casc.total_pips(row_state)    if casc_oc != "LOSS" else None
            if _wp:
                _rt.update_fields(
                    rec_id,
                    cascading_total_pips=_tp,
                    cascading_total_pips_weighted=_wp,
                )
            closed_row = _rt.update_outcome(
                rec_id, casc_oc,
                close_price=mprice,
                exit_reason="STOP_HIT",
                cascading_pips=_wp,
            )
            label = f"+{_wp:.1f}p cascade" if _wp else "LOSS"
            fragments.append(f"{pair} stop hit ({label})")
            _online_learn_closure("research", closed_row)
            log(f"  Monitor research #{rec_id} {pair}: {casc_oc} at {mprice}")
            break

    summary = " · ".join(fragments)
    return closed_row, summary


# ── MFE / MAE update ─────────────────────────────────────────────────────────

def _update_mfe_mae_from_ohlcv(rec_id: int, direction: str, candles: list) -> bool:
    """Update MFE and MAE using candle extremes. Returns True if updated."""
    from src import research_tracker as _rt
    if not candles:
        return False
    highs = [_to_float(c.get("high")) for c in candles if _to_float(c.get("high")) is not None]
    lows  = [_to_float(c.get("low"))  for c in candles if _to_float(c.get("low"))  is not None]
    if not highs or not lows:
        return False
    d = direction.upper()
    if d == "BUY":
        _rt.update_mfe_mae(rec_id, max(highs))   # best MFE candidate
        _rt.update_mfe_mae(rec_id, min(lows))    # worst MAE candidate
    elif d == "SELL":
        _rt.update_mfe_mae(rec_id, min(lows))    # best MFE candidate for SELL
        _rt.update_mfe_mae(rec_id, max(highs))   # worst MAE candidate for SELL
    else:
        return False
    return True


# ── Cascade initialisation ────────────────────────────────────────────────────

def _ensure_cascade_levels(row: dict, tracker_mod, log=print) -> dict:
    """If t1_price not set, compute and store cascade levels. Return updated row."""
    if _to_float(row.get("t1_price")):
        return row
    try:
        ct1, ct2, ct3 = _casc.compute_levels(
            row.get("entry"), row.get("stop_loss"),
            row.get("target"), row.get("direction"),
        )
        if ct1 is not None:
            tracker_mod.update_fields(
                _safe_int_id(row.get("id", 0)),
                t1_price=ct1, t2_price=ct2, t3_price=ct3,
                effective_stop=row.get("stop_loss"),
            )
            row = dict(row)
            row.update({
                "t1_price": ct1, "t2_price": ct2, "t3_price": ct3,
                "effective_stop": row.get("stop_loss"),
            })
    except Exception as exc:
        log(f"  Monitor: cascade init failed for #{row.get('id')} {row.get('pair')}: {exc}")
    return row


# ── Main entry point ──────────────────────────────────────────────────────────

def _fetch_stooq_prices(pairs: list, log=print) -> dict:
    """Fetch daily close prices from Stooq (no API key, free).

    Uses Stooq CSV API: https://stooq.com/q/l/?s=AUDJPY&f=sd2t2ohlcv&h&e=csv
    Returns {pair: float} for pairs that respond successfully.
    """
    import csv as _csv
    import io as _io
    prices: dict = {}
    for pair in pairs:
        symbol = pair.replace("/", "").replace("-", "").upper()
        try:
            url = (f"https://stooq.com/q/l/?s={symbol}"
                   f"&f=sd2t2ohlcv&h&e=csv")
            resp = requests.get(url, timeout=_FETCH_TIMEOUT)
            if resp.status_code != 200:
                continue
            reader = _csv.DictReader(_io.StringIO(resp.text))
            for row in reader:
                close_str = (row.get("Close") or row.get("close") or "").strip()
                if close_str and close_str not in ("N/D", ""):
                    prices[pair] = float(close_str)
                break  # only first data row needed
        except Exception as exc:
            log(f"  [SQ] {pair}: Stooq fetch error — {exc}")
    if prices:
        log(f"  [SQ] Stooq returned {len(prices)}/{len(pairs)} pairs")
    return prices


def _get_backup_mode(log=print) -> str:
    """Return 'td', 'stooq', or 'none' based on current TD quota.

    Quota thresholds (preserving capacity for main daily scans):
      > 700 calls today: disable ALL external backup — synthetic/internal only
      > 600 calls today: skip TD, fall back to Stooq
      <= 600 calls today: use TD as backup
    """
    calls_today = _get_td_calls_today()
    if calls_today > 700:
        log(f"  API quota critical — {calls_today}/800 calls used — "
            f"external backup disabled — synthetic candle only")
        return "none"
    if calls_today > 600:
        log(f"  TD quota protection — {calls_today}/800 calls used — "
            f"skipping TD backup — trying Stooq instead")
        return "stooq"
    return "td"


def _send_fallback_alerts(ta, td_pairs: list, sq_pairs: list,
                          unavailable_pairs: list, log=print) -> None:
    """Send Telegram alerts when backup data sources were activated."""
    if ta is None:
        return
    try:
        if len(td_pairs) >= 5:
            ta.send(
                f"⚠️ <b>Monitor: Yahoo Finance degraded this run</b>\n\n"
                f"Twelve Data backup activated for {len(td_pairs)} pairs\n"
                f"All trades still monitored ✅\n"
                f"Yahoo Finance issues are usually temporary"
            )
    except Exception:
        pass
    try:
        if sq_pairs:
            ta.send(
                f"⚠️ <b>Monitor: Yahoo AND Twelve Data failed</b>\n\n"
                f"Stooq backup activated — reduced precision\n"
                f"Pairs: {', '.join(sq_pairs[:5])}"
                + (f" + {len(sq_pairs) - 5} more" if len(sq_pairs) > 5 else "")
                + f"\nAll trades still monitored ✅\n"
                  f"Investigating data source issues"
            )
    except Exception:
        pass
    for pair in unavailable_pairs:
        try:
            ta.send(
                f"🚨 <b>Monitor: All external sources failed for {pair}</b>\n\n"
                f"Using internal price history only\n"
                f"Price may be up to 30 minutes old\n"
                f"Manual check recommended if near a cascade target"
            )
        except Exception:
            pass


def build_monitor_source_report(days: int = 1) -> list:
    """Return source reliability lines for 11pm daily digest or Monday report."""
    try:
        if not _MONITOR_HISTORY.exists():
            return []
        history = json.loads(_MONITOR_HISTORY.read_text(encoding="utf-8"))
        runs    = history.get("runs", [])
    except Exception:
        return []

    from datetime import timedelta as _td_td
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - _td_td(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    week   = [r for r in runs if r.get("ts", "") >= cutoff and not r.get("skipped")]
    if not week:
        return []

    keys = [
        ("t1_yf_ohlcv",    "Yahoo 1H OHLCV"),
        ("t1_td_backup",   "Twelve Data backup"),
        ("t1_sq_backup",   "Stooq backup"),
        ("t2_synthetic",   "Synthetic candle"),
        ("t3_current_only","Current price only"),
        ("t4_last_known",  "Internal history"),
        ("t0_no_data",     "No data"),
    ]
    totals = {k: sum(r.get(k, 0) for r in week) for k, _ in keys}
    grand  = sum(totals.values())
    if grand == 0:
        return []

    def pct(n): return f"{round(n / grand * 100)}%" if grand else "0%"

    label = f"(last {days}d)" if days > 1 else "(today)"
    lines = [f"<b>Monitor price sources {label}:</b>"]
    for k, name in keys:
        n = totals[k]
        if n == 0 and k != "t0_no_data":
            continue
        flag = " ✅" if (k == "t0_no_data" and n == 0) else (" ⚠️" if k == "t0_no_data" and n > 0 else "")
        lines.append(f"  {name}: {pct(n)} ({n} checks){flag}")
    return lines


def _build_synthetic_candle(readings: list) -> dict | None:
    """Build a synthetic OHLCV candle from a list of price readings.

    Each reading is {price: float, timestamp: str, source: str}.
    Requires at least 3 readings; returns None otherwise.
    """
    if len(readings) < 3:
        return None
    vals = [r["price"] for r in readings if isinstance(r.get("price"), (int, float))]
    if not vals:
        return None
    return {
        "open":            str(readings[0]["price"]),
        "high":            str(max(vals)),
        "low":             str(min(vals)),
        "close":           str(readings[-1]["price"]),
        "datetime":        readings[-1].get("timestamp", "")[:16].replace("T", " "),
        "source":          "synthetic_from_history",
        "n_readings":      len(readings),
        "oldest_reading":  readings[0].get("timestamp", ""),
        "newest_reading":  readings[-1].get("timestamp", ""),
    }


def _check_market_reopen(now_ak, ta=None, log=print) -> bool:
    """Send Sunday market-reopen alert once per week (5pm–6pm Auckland Sunday)."""
    if now_ak.weekday() != 6:      # 6 = Sunday
        return False
    if not (17 <= now_ak.hour < 18):
        return False
    try:
        reopen_data: dict = {}
        if _MARKET_REOPEN_FILE.exists():
            reopen_data = json.loads(_MARKET_REOPEN_FILE.read_text(encoding="utf-8"))
        last_str = reopen_data.get("last_reopen_detected", "")
        if last_str:
            if (datetime.now(timezone.utc).replace(tzinfo=None) - datetime.fromisoformat(last_str)).days < 6:
                return False  # already sent this week
        if ta:
            try:
                ta.send(
                    "📊 <b>Forex markets reopening</b>\n\n"
                    "Checking for Sunday opening moves and weekend gaps across all open trades.\n"
                    "Live prices now available — next full scan 6am Auckland."
                )
            except Exception:
                pass
        reopen_data["last_reopen_detected"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        reopen_data["reopen_alert_sent"]    = True
        _MARKET_REOPEN_FILE.write_text(json.dumps(reopen_data, indent=2), encoding="utf-8")
        log("Monitor: Sunday market reopen detected — alert sent")
        return True
    except Exception:
        return False


def build_monitor_data_quality_report() -> list:
    """Return data-tier quality section for Monday learning report."""
    try:
        if not _MONITOR_HISTORY.exists():
            return []
        history = json.loads(_MONITOR_HISTORY.read_text(encoding="utf-8"))
        runs = history.get("runs", [])
    except Exception:
        return []

    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    week = [r for r in runs if r.get("ts", "") >= cutoff and not r.get("skipped")]
    if not week:
        return []

    _keys = [
        ("t1_yf_ohlcv",    "T1-YF  Yahoo 1H candles"),
        ("t1_td_backup",   "T1-TD  Twelve Data backup"),
        ("t1_sq_backup",   "T1-SQ  Stooq backup"),
        ("t2_synthetic",   "T2-SYN synthetic history"),
        ("t3_current_only","T3-CUR current price only"),
        ("t4_last_known",  "T4-PH  last known price"),
        ("t0_no_data",     "T0     no data"),
    ]
    totals = {k: sum(r.get(k, 0) for r in week) for k, _ in _keys}
    grand  = sum(totals.values())
    if grand == 0:
        return []

    def pct(n):
        return f"{round(n / grand * 100)}%" if grand else "0%"

    lines = ["", "<b>MONITOR DATA QUALITY (this week)</b>"]
    for k, label in _keys:
        n    = totals[k]
        flag = ""
        if k == "t0_no_data":
            flag = " ✅" if n == 0 else " ⚠️"
        elif k in ("t1_td_backup", "t1_sq_backup") and n == 0:
            continue  # suppress zero-usage backup sources
        lines.append(f"{label}: {pct(n)} ({n} checks){flag}")
    return lines


def _check_pending_trades(prices: dict, log_fn=None) -> list:
    """Check all PENDING fund trades to see if their entry triggers have been hit.

    Returns list of trade dicts that were activated this run.
    """
    import pandas as _pd_pend
    from src.trading.financials import (
        get_price as _gp_pend,
        atomic_write_csv as _awc_pend,
        utc_now_str as _uns_pend,
    )
    _log = log_fn or print

    try:
        df = _pd_pend.read_csv("data/trades.csv", encoding="utf-8-sig")
    except Exception as exc:
        _log(f"[pending] CSV load failed: {exc}")
        return []

    try:
        fund = df[df["trade_this"].astype(str).str.strip().str.upper() == "YES"]
        pending = fund[fund["status"] == "PENDING"].copy()
    except Exception as exc:
        _log(f"[pending] Filter failed: {exc}")
        return []

    if len(pending) == 0:
        return []

    _log(f"[pending] Checking {len(pending)} pending trades")

    activated: list = []
    cancelled: list = []
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    for idx, t in pending.iterrows():
        trade_id    = t.get("id")
        pair        = str(t.get("pair", ""))
        direction   = str(t.get("direction", "")).upper()
        entry_type  = str(t.get("entry_type", "")).upper()
        try:
            trigger_price = float(t.get("entry_trigger_price") or 0)
        except (TypeError, ValueError):
            trigger_price = 0.0
        expiry    = str(t.get("entry_trigger_expiry", ""))
        try:
            stop = float(t.get("stop_loss") or 0)
        except (TypeError, ValueError):
            stop = 0.0

        # Check expiry
        if expiry not in ("", "nan", "None"):
            try:
                exp_dt = datetime.strptime(expiry[:19], "%Y-%m-%d %H:%M:%S")
                if exp_dt < now_utc:
                    _log(f"[pending] #{trade_id} {pair} EXPIRED — trigger never hit")
                    df.loc[idx, "status"]    = "EXPIRED"
                    df.loc[idx, "outcome"]   = "EXPIRED" if "outcome" in df.columns else ""
                    df.loc[idx, "closed_at"] = _uns_pend()
                    cancelled.append(trade_id)
                    continue
            except (ValueError, TypeError):
                pass

        # Get current price
        current = _gp_pend(prices, pair)
        if not current:
            _log(f"[pending] #{trade_id} {pair} — no price found")
            continue

        # Check if stop has been breached — cancel the setup
        if stop > 0:
            if direction == "BUY" and current <= stop:
                _log(f"[pending] #{trade_id} {pair} CANCELLED — price {current} below stop {stop}")
                df.loc[idx, "status"]    = "CANCELLED"
                df.loc[idx, "closed_at"] = _uns_pend()
                cancelled.append(trade_id)
                continue
            if direction == "SELL" and current >= stop:
                _log(f"[pending] #{trade_id} {pair} CANCELLED — price {current} above stop {stop}")
                df.loc[idx, "status"]    = "CANCELLED"
                df.loc[idx, "closed_at"] = _uns_pend()
                cancelled.append(trade_id)
                continue

        # Check trigger
        triggered      = False
        trigger_reason = ""

        if entry_type == "IMMEDIATE":
            triggered      = True
            trigger_reason = "Immediate entry corrected"
        elif entry_type == "BREAKOUT_BUY":
            if trigger_price > 0 and current >= trigger_price:
                triggered      = True
                trigger_reason = f"Broke above {trigger_price:.5f}"
        elif entry_type == "BREAKOUT_SELL":
            if trigger_price > 0 and current <= trigger_price:
                triggered      = True
                trigger_reason = f"Broke below {trigger_price:.5f}"
        elif entry_type == "LIMIT_BUY":
            if trigger_price > 0 and current <= trigger_price:
                triggered      = True
                trigger_reason = f"Price reached {trigger_price:.5f}"
        elif entry_type == "LIMIT_SELL":
            if trigger_price > 0 and current >= trigger_price:
                triggered      = True
                trigger_reason = f"Price reached {trigger_price:.5f}"
        elif entry_type == "PULLBACK":
            if direction == "BUY" and trigger_price > 0 and current <= trigger_price:
                triggered      = True
                trigger_reason = f"Pullback to {trigger_price:.5f}"
            elif direction == "SELL" and trigger_price > 0 and current >= trigger_price:
                triggered      = True
                trigger_reason = f"Rally to {trigger_price:.5f}"

        if not triggered:
            continue

        # Check fund capacity — respect 5th-slot tiered override if trade was approved as one
        currently_open = len(df[
            (df["trade_this"].astype(str).str.strip().str.upper() == "YES") &
            (df["status"] == "OPEN")
        ])
        _pend_conf     = float(t.get("confidence") or 0)
        _pend_override = str(t.get("capacity_override", "")).upper() in ("TRUE", "YES", "1")
        _pend_cap      = 5 if (_pend_override and _pend_conf >= 7.5) else 4
        if currently_open >= _pend_cap:
            _log(f"[pending] #{trade_id} {pair} trigger hit but fund at capacity "
                 f"{currently_open}/{_pend_cap} — extending expiry 24h")
            try:
                new_expiry = datetime.now(timezone.utc) + timedelta(hours=24)
                df.loc[idx, "entry_trigger_expiry"] = new_expiry.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
            continue

        # Re-check circuit breaker — CB may have fired since trade was originally approved
        try:
            import json as _json_pend
            with open("data/fund_state.json", encoding="utf-8") as _fs_pend_fh:
                _fs_pend = _json_pend.load(_fs_pend_fh)
            _pend_cl = int(_fs_pend.get("consecutive_losses", 0))
        except Exception:
            _pend_cl = 0
        if _pend_cl >= 3:
            _log(f"[pending] #{trade_id} {pair} trigger hit but circuit breaker active "
                 f"({_pend_cl} losses) — extending expiry 24h")
            try:
                new_expiry = datetime.now(timezone.utc) + timedelta(hours=24)
                df.loc[idx, "entry_trigger_expiry"] = new_expiry.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
            continue

        # Activate the trade — recalculate stop and targets from actual entry
        now_str      = _uns_pend()
        actual_entry = current
        ps_size      = 0.01 if "JPY" in pair else 0.0001

        try:
            orig_entry = float(t.get("entry") or 0)
            orig_stop  = float(t.get("stop_loss") or 0)
            if orig_entry > 0 and orig_stop > 0:
                orig_stop_pips = abs(orig_entry - orig_stop) / ps_size
                new_stop = (
                    actual_entry - orig_stop_pips * ps_size
                    if direction == "BUY"
                    else actual_entry + orig_stop_pips * ps_size
                )
                df.loc[idx, "stop_loss"]    = round(new_stop, 5)
                df.loc[idx, "effective_stop"] = round(new_stop, 5)
                for t_col in ("target", "t1_price", "t2_price", "t3_price"):
                    try:
                        orig_t = float(t.get(t_col) or 0)
                        if orig_t > 0:
                            t_pips = abs(orig_t - orig_entry) / ps_size
                            new_t  = (
                                actual_entry + t_pips * ps_size
                                if direction == "BUY"
                                else actual_entry - t_pips * ps_size
                            )
                            df.loc[idx, t_col] = round(new_t, 5)
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass

        df.loc[idx, "status"]             = "OPEN"
        df.loc[idx, "entry"]              = actual_entry
        df.loc[idx, "entry_confirmed_at"] = now_str
        df.loc[idx, "timestamp"]          = now_str

        _log(f"[pending] #{trade_id} {pair} ACTIVATED — {trigger_reason} — entry at {actual_entry}")
        activated.append({
            "trade_id":          trade_id,
            "pair":              pair,
            "direction":         direction,
            "entry":             actual_entry,
            "trigger_reason":    trigger_reason,
            "orig_signal_price": float(t.get("entry") or 0),
            "entry_type":        entry_type,
        })

    if activated or cancelled:
        if _awc_pend(df, "data/trades.csv"):
            _log(f"[pending] Saved: {len(activated)} activated {len(cancelled)} cancelled/expired")
        for act in activated:
            _log(
                f"[pending] #{act['trade_id']} {act['pair']} {act['direction']} "
                f"ACTIVATED at {act['entry']} ({act['trigger_reason']})"
            )
            try:
                from src import discord_notifier as _dn_pend
                _dn_pend._send_entry_confirmed_alert(act, log_fn=_log)
            except Exception as _ale:
                _log(f"[pending] Alert error: {_ale}")

    return activated


def run(log=print) -> dict:
    """Run the between-scan monitor. Returns the monitor_log dict written to disk."""
    now_ak   = _auckland_now()
    is_wknd  = now_ak.weekday() >= 5   # Saturday or Sunday (Auckland)
    now_str  = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
    next_6am = "6am Auckland"

    result = {
        "timestamp":               now_str,
        "trades_checked_fund":     0,
        "trades_checked_research": 0,
        "hot_zone_count":          0,
        "warm_zone_count":         0,
        "cold_zone_count":         0,
        "api_calls_used":          0,
        "yf_price_pairs":          0,
        "yf_ohlcv_pairs":          0,
        "approaching_alerts":      0,
        "milestones_hit":          [],
        "mfe_mae_updated":         0,
        "last_prices":             {},
        "previously_hot":          [],
        "skipped_reason":          "",
        "watchlist_prices":        {},
        "watchlist_alerts_sent":   [],
        "data_tiers_used": {
            "t1_yf_ohlcv":     0,  # Yahoo 1H OHLCV candles
            "t1_td_backup":    0,  # Twelve Data live price backup
            "t1_sq_backup":    0,  # Stooq live price backup
            "t2_synthetic":    0,  # Synthetic from rolling price history
            "t3_current_only": 0,  # Current price only (< 3 history readings)
            "t4_last_known":   0,  # Last known price from previous run
            "t0_no_data":      0,
        },
        "source_stats": {
            "yahoo_finance":   0,
            "twelve_data":     0,
            "stooq":           0,
            "synthetic":       0,
            "internal_hist":   0,
            "no_data":         0,
        },
        "price_history": {},
    }

    # ── File lock — prevents duplicate processing if runs overlap ────────────
    _lock_held = _try_acquire_lock(log)
    if not _lock_held:
        result["skipped_reason"] = "lock_held"
        log("Monitor: another instance is already running (lock held) — skipping this run.")
        _write_monitor_log(result)
        return result

    # ── Load previous run state (last_prices, previously_hot) ────────────────
    _prev_log: dict = {}
    try:
        if _MONITOR_LOG.exists():
            _prev_log = json.loads(_MONITOR_LOG.read_text(encoding="utf-8"))
    except Exception:
        pass
    last_prices    = {k: float(v) for k, v in _prev_log.get("last_prices", {}).items()
                      if v is not None}
    previously_hot = set(_prev_log.get("previously_hot", []))
    price_history  = _prev_log.get("price_history", {})

    # ── Telegram alert module (loaded once, used throughout) ─────────────────
    try:
        from src import telegram_alert as _ta
    except Exception:
        _ta = None

    # ── Smart skip: exit immediately if no open trades ────────────────────────
    try:
        from src import tracker as _trk
        from src import research_tracker as _rt
        _fund_open = [r for r in _trk.load() if r.get("status") == "OPEN"
                      and r.get("trade_this") == "YES"]
        _res_open  = [r for r in _rt.load()  if r.get("status") == "OPEN"]
    except Exception as exc:
        log(f"Monitor: failed to load trade data — {exc}")
        result["skipped_reason"] = "load_error"
        _write_monitor_log(result)
        _release_lock()
        return result

    _verify_fund_state(_fund_open, log=log)

    # Definitive fund trade sets — used to hard-filter all monitor channel sends.
    fund_pairs = {str(r.get("pair", "")) for r in _fund_open}
    fund_ids   = {str(r.get("id",   "")) for r in _fund_open}
    log(f"[monitor] Fund pairs: {fund_pairs}")

    if not _fund_open and not _res_open:
        result["skipped_reason"] = "no_open_trades"
        log("Monitor: no open trades — skipping — zero API calls used.")
        _write_monitor_log(result)
        _release_lock()
        return result

    result["trades_checked_fund"]     = len(_fund_open)
    result["trades_checked_research"] = len(_res_open)
    log(
        f"Monitor: {len(_fund_open)} open fund trade(s) · "
        f"{len(_res_open)} open research trade(s) · "
        f"{'weekend mode — Friday close prices' if is_wknd else 'live prices'}"
    )

    # ── Step 1: Multi-source price fetch — Yahoo → Twelve Data → Stooq ─────────
    all_open    = _fund_open + _res_open
    _fund_pairs = sorted({r.get("pair", "") for r in _fund_open if r.get("pair")})
    _res_pairs  = sorted({r.get("pair", "") for r in _res_open  if r.get("pair")})
    all_pairs   = sorted({r.get("pair", "") for r in all_open   if r.get("pair")})
    log(
        f"Monitor: {len(_fund_pairs)} fund pair(s) + {len(_res_pairs)} research pair(s) — "
        f"{len(all_pairs)} unique pairs — fetching via Yahoo Finance"
    )

    # Primary: Yahoo Finance
    prices = _yahoo_price_batch(all_pairs, log=log)
    price_sources: dict = {p: "yahoo" for p in prices}  # {pair: "yahoo"|"twelve_data"|"stooq"}

    _td_backup_pairs: list = []
    _sq_backup_pairs: list = []
    _yf_coverage = len(prices) / len(all_pairs) if all_pairs else 1.0

    # Activate backup sources when Yahoo covers < 70% of pairs
    if _yf_coverage < 0.70:
        _missing = [p for p in all_pairs if p not in prices]
        _backup_mode = _get_backup_mode(log=log)

        if _backup_mode == "td" and config.TWELVE_DATA_KEY:
            log(
                f"  Monitor: Yahoo degraded ({len(prices)}/{len(all_pairs)} pairs, "
                f"{_yf_coverage:.0%}) — Twelve Data backup for {len(_missing)} pairs"
            )
            _td_prices, _td_calls = _batch_price_fetch(_missing, log=log)
            result["api_calls_used"] += _td_calls
            for _p, _v in _td_prices.items():
                prices[_p] = _v
                price_sources[_p] = "twelve_data"
                _td_backup_pairs.append(_p)

            # Still missing after TD? Try Stooq
            _still_missing = [p for p in _missing if p not in prices]
            if _still_missing:
                _sq_prices = _fetch_stooq_prices(_still_missing, log=log)
                for _p, _v in _sq_prices.items():
                    prices[_p] = _v
                    price_sources[_p] = "stooq"
                    _sq_backup_pairs.append(_p)

        elif _backup_mode == "stooq":
            log(
                f"  Monitor: Yahoo degraded + TD quota exceeded — "
                f"Stooq backup for {len(_missing)} pairs"
            )
            _sq_prices = _fetch_stooq_prices(_missing, log=log)
            for _p, _v in _sq_prices.items():
                prices[_p] = _v
                price_sources[_p] = "stooq"
                _sq_backup_pairs.append(_p)

        else:
            log(f"  API quota critical — external backup disabled for {len(_missing)} pairs")

    _unavailable = [p for p in all_pairs if p not in prices]
    # Last-resort: fill still-missing prices from price_cache (populated by daily scan)
    if _unavailable:
        try:
            from src.price_fetcher import _load_price_cache as _lpc_mon
            _cached_p_mon, _cache_age_mon = _lpc_mon()
            _filled_mon = 0
            for _pm in _unavailable:
                if _pm in _cached_p_mon:
                    prices[_pm] = _cached_p_mon[_pm]
                    price_sources[_pm] = "price_cache"
                    _filled_mon += 1
            if _filled_mon:
                _age_str = f"{_cache_age_mon:.1f}h old" if _cache_age_mon is not None else "age unknown"
                log(f"Monitor: {_filled_mon} pair(s) filled from price cache ({_age_str})")
                _unavailable = [p for p in _unavailable if p not in prices]
        except Exception:
            pass
    result["yf_price_pairs"] = len(prices)
    _send_fallback_alerts(_ta, _td_backup_pairs, _sq_backup_pairs, _unavailable, log=log)

    # ── Update rolling price history (max 96 readings = 48h at 30-min intervals) ──
    for _ph_pair, _ph_price in prices.items():
        _src_label = price_sources.get(_ph_pair, "yahoo_finance")
        _hist = price_history.get(_ph_pair, [])
        _hist.append({"price": _ph_price, "timestamp": now_str, "source": _src_label})
        price_history[_ph_pair] = _hist[-96:]
    result["price_history"] = price_history

    # ── Sunday market reopen detection ────────────────────────────────────────
    _check_market_reopen(now_ak, ta=_ta, log=log)

    # ── Watchlist movement check — flag near-miss pairs approaching trade threshold ──
    _wl_priority_pairs: list = []
    try:
        _wl_cache_path_mon = config.DATA_DIR / "watchlist_cache.json"
        if _wl_cache_path_mon.exists():
            _wl_data_mon = json.loads(_wl_cache_path_mon.read_text(encoding="utf-8"))
            _wl_age_mon  = (time.time() - float(_wl_data_mon.get("timestamp", 0))) / 3600.0
            if _wl_age_mon <= 28:   # skip if cache older than 28h (stale)
                _nm_mon      = _wl_data_mon.get("near_miss", {})      # {pair: conf}
                _nm_dirs_mon = _wl_data_mon.get("near_miss_dirs", {}) # {pair: "BUY"/"SELL"}
                _top5_mon    = sorted(_nm_mon.items(), key=lambda x: -float(x[1]))[:5]
                _wl_pairs_to_check = [p for p, _ in _top5_mon if p]

                if _wl_pairs_to_check:
                    from src.yahoo_finance import batch_fetch_prices as _yf_batch_mon
                    _wl_prices_mon = _yf_batch_mon(_wl_pairs_to_check, log=log)
                    _prev_wl_prices = {k: float(v) for k, v in
                                       _prev_log.get("watchlist_prices", {}).items() if v}
                    _wl_alerts_sent = set(_prev_log.get("watchlist_alerts_sent", []))
                    _today_ak_mon   = _auckland_now().strftime("%Y-%m-%d")

                    from src.selector import _pip_size as _pip_sz_mon
                    _priority_next_scan = list(_wl_data_mon.get("priority_for_next_scan", []))
                    _monday_mon = (_auckland_now() - timedelta(days=_auckland_now().weekday())).strftime("%Y-%m-%d")
                    _wl_sts_mon = _wl_data_mon.get("watchlist_weekly_stats", {})
                    if _wl_sts_mon.get("week_start", "") != _monday_mon:
                        _wl_sts_mon = {"week_start": _monday_mon, "alerts_sent": 0, "promoted_to_deep": 0, "became_trade_alerts": 0}

                    for _wlp, _wlconf in _top5_mon:
                        cur_mon = _wl_prices_mon.get(_wlp)
                        if cur_mon is None or cur_mon <= 0:
                            continue
                        result.setdefault("watchlist_prices", {})[_wlp] = cur_mon

                        alert_key = f"{_wlp}:{_today_ak_mon}"
                        if alert_key in _wl_alerts_sent:
                            continue

                        prev_mon = _prev_wl_prices.get(_wlp)
                        if prev_mon is None:
                            continue

                        pip_mon   = _pip_sz_mon(_wlp)
                        move_mon  = cur_mon - prev_mon
                        pips_mon  = round(abs(move_mon) / pip_mon)
                        atr_mon   = cur_mon * 0.0080 if _wlp.upper().endswith("JPY") else cur_mon * 0.0008
                        ratio_mon = abs(move_mon) / atr_mon if atr_mon > 0 else 0.0

                        _pip_floor = 50 if "JPY" in _wlp.upper() else 15
                        if ratio_mon < 1.0 or pips_mon < _pip_floor:
                            continue

                        exp_dir  = (_nm_dirs_mon.get(_wlp) or "").upper()
                        act_dir  = "BUY" if move_mon > 0 else "SELL"
                        if exp_dir and act_dir != exp_dir:
                            continue

                        log(
                            f"Monitor: watchlist movement — {_wlp} {pips_mon} pips "
                            f"({ratio_mon:.1f}x ATR) in {act_dir} direction (conf {_wlconf}/10)"
                        )
                        if _ta:
                            try:
                                _ta.send(
                                    f"🔔 <b>Watch list movement detected</b>\n\n"
                                    f"{_wlp} (watch list confidence {_wlconf}/10)\n"
                                    f"Moved {pips_mon} pips in {act_dir} direction since last scan\n\n"
                                    f"This may become a trade signal at next 6am Auckland scan "
                                    f"— added to priority queue"
                                )
                            except Exception:
                                pass
                        try:
                            if _dn:
                                _dn.send_watch_list_movement(
                                    _wlp, float(_wlconf), act_dir, float(pips_mon)
                                )
                        except Exception:
                            pass
                        _wl_alerts_sent.add(alert_key)
                        _wl_priority_pairs.append(_wlp)
                        if _wlp not in _priority_next_scan:
                            _priority_next_scan.append(_wlp)

                    if _wl_priority_pairs:
                        _wl_sts_mon["alerts_sent"]       = _wl_sts_mon.get("alerts_sent", 0) + len(_wl_priority_pairs)
                        _wl_sts_mon["promoted_to_deep"]  = _wl_sts_mon.get("promoted_to_deep", 0) + len(_wl_priority_pairs)
                        _wl_data_mon["priority_for_next_scan"] = _priority_next_scan
                        _wl_data_mon["watchlist_weekly_stats"] = _wl_sts_mon
                        try:
                            _wl_cache_path_mon.write_text(
                                json.dumps(_wl_data_mon, indent=2), encoding="utf-8"
                            )
                        except Exception:
                            pass

                    result["watchlist_alerts_sent"] = sorted(_wl_alerts_sent)
    except Exception as _wl_mon_exc:
        log(f"Monitor: watchlist movement check failed — {_wl_mon_exc}")

    # ── Check pending entry triggers ───────────────────────────────────────────
    try:
        _newly_activated = _check_pending_trades(prices=prices, log_fn=log)
        if _newly_activated:
            log(f"[pending] {len(_newly_activated)} trades activated this run")
            # Sync fund state after activations
            try:
                import pandas as _pd_pend_sync
                from src.trading import financials as _fin_pend_sync
                _df_pend_sync   = _pd_pend_sync.read_csv(str(config.TRADES_CSV), encoding="utf-8-sig")
                _prices_pend    = _fin_pend_sync.load_prices()
                _state_pend     = _fin_pend_sync.calculate_fund_state(_df_pend_sync, _prices_pend)
                _fin_pend_sync.sync_fund_state_json(_state_pend)
            except Exception:
                pass
            # Reload open trades to include newly activated ones
            try:
                _fund_open = [r for r in _trk.load()
                              if r.get("status") == "OPEN"
                              and r.get("trade_this") == "YES"]
            except Exception:
                pass
    except Exception as _pend_exc:
        log(f"[pending] Pending check failed: {_pend_exc}")

    # ── Step 2: Ensure cascade levels + zone classification ──────────────────
    for i, row in enumerate(_fund_open):
        _fund_open[i] = _ensure_cascade_levels(row, _trk, log=log)
    for i, row in enumerate(_res_open):
        _res_open[i] = _ensure_cascade_levels(row, _rt, log=log)

    def _zone(rows_list):
        zones = {"HOT": [], "WARM": [], "COLD": []}
        for row in rows_list:
            z = _classify_zone(row, prices.get(row.get("pair", "")))
            zones[z].append(row)
        return zones

    fund_zones = _zone(_fund_open)
    res_zones  = _zone(_res_open)

    total_hot  = len(fund_zones["HOT"])  + len(res_zones["HOT"])
    total_warm = len(fund_zones["WARM"]) + len(res_zones["WARM"])
    total_cold = len(fund_zones["COLD"]) + len(res_zones["COLD"])
    result["hot_zone_count"]  = total_hot
    result["warm_zone_count"] = total_warm
    result["cold_zone_count"] = total_cold
    log(
        f"Monitor: zones — HOT={total_hot} WARM={total_warm} COLD={total_cold} — "
        f"fetching Yahoo 1H OHLCV for all {len(all_pairs)} pairs"
    )

    # ── Detect HOT trades and send early-warning alert (time-based dedup) ──────
    # Uses hot_zone_alerts.json written IMMEDIATELY on send — not monitor_log.json
    # (which is only written at end-of-run and may not be committed yet when the
    # next concurrent run starts, causing duplicate alerts).
    #
    # SEPARATION: fund HOT → WEBHOOK_MONITOR (individual alerts)
    #             research HOT → WEBHOOK_RESEARCH (one batch per 2 hours)
    current_hot_keys: set = set()
    _all_hot_by_pair: dict  = {}   # all pairs — zone-exit tracking only
    _fund_hot_by_pair: dict = {}   # fund pairs only — Discord MONITOR sends
    _res_hot_rows: list     = []   # research HOT rows — batch report

    for _hr in fund_zones["HOT"]:
        _hpair = _hr.get("pair", "")
        _hk    = f"{_hpair}#{_hr.get('id', '')}"
        current_hot_keys.add(_hk)
        _all_hot_by_pair.setdefault(_hpair, []).append(_hr)
        _fund_hot_by_pair.setdefault(_hpair, []).append(_hr)

    for _hr in res_zones["HOT"]:
        _hpair = _hr.get("pair", "")
        _hk    = f"{_hpair}#{_hr.get('id', '')}"
        current_hot_keys.add(_hk)
        _all_hot_by_pair.setdefault(_hpair, []).append(_hr)
        _res_hot_rows.append(_hr)

    # Determine which pairs left HOT zone since last run (reset continuously_hot)
    _prev_hot_pairs: set = set()
    try:
        if _HOT_ZONE_ALERTS.exists():
            _hza_data = json.loads(_HOT_ZONE_ALERTS.read_text(encoding="utf-8"))
            _prev_hot_pairs = set(_hza_data.get("pairs", {}).keys())
    except Exception:
        pass
    for _exited_pair in _prev_hot_pairs - set(_all_hot_by_pair.keys()):
        _mark_hot_zone_exited(_exited_pair)

    _alerts_sent = 0
    for _pair_str in sorted(_fund_hot_by_pair):
        # Hard filter: only fund pairs go to WEBHOOK_MONITOR
        if _pair_str not in fund_pairs:
            log(f"[monitor] BLOCKED: {_pair_str} reached fund HOT loop but is not a fund pair — skipping")
            continue
        # Check if this pair was continuously HOT (never left HOT zone since last alert)
        _cont_hot = False
        try:
            if _HOT_ZONE_ALERTS.exists():
                _hza_read = json.loads(_HOT_ZONE_ALERTS.read_text(encoding="utf-8"))
                _cont_hot = _hza_read.get("pairs", {}).get(_pair_str, {}).get("continuously_hot", False)
        except Exception:
            pass

        _prev_ts = _check_hot_alert_sent(_pair_str)
        if _prev_ts:
            _n = len(_fund_hot_by_pair[_pair_str])
            log(f"  Monitor: HOT zone alert suppressed for {_pair_str} ({_n} trade(s)) — already sent {_prev_ts}")
            continue
        _rows_for_pair = _fund_hot_by_pair[_pair_str]
        log(f"  Monitor: {_pair_str} HOT zone ({len(_rows_for_pair)} trade(s)) — sending approaching target alert")
        if _ta:
            try:
                _ta.send(_build_hot_alert_message(
                    _pair_str, _rows_for_pair, prices.get(_pair_str)
                ))
            except Exception:
                pass
        try:
            if _dn and _rows_for_pair:
                _cur_p = prices.get(_pair_str)
                _r0    = _rows_for_pair[0]
                _dir0  = (_r0.get("direction") or "").upper()
                _entry = _to_float(_r0.get("entry"))
                _stop0 = _to_float(_r0.get("effective_stop") or _r0.get("stop_loss"))
                # For stop-approaching alerts, always use the actual stop_loss column —
                # effective_stop may be set to entry (breakeven) after T1 hit, which would
                # show the wrong level and compute incorrect distance to stop.
                _actual_stop = _to_float(_r0.get("stop_loss") or _r0.get("effective_stop"))
                _t1h   = str(_r0.get("t1_hit", "")).upper() in ("TRUE", "1", "YES")
                _t2h   = str(_r0.get("t2_hit", "")).upper() in ("TRUE", "1", "YES")
                if not _t1h:
                    _tgt0, _ms0 = _to_float(_r0.get("t1_price")), "T1"
                elif not _t2h:
                    _tgt0, _ms0 = _to_float(_r0.get("t2_price")), "T2"
                else:
                    _tgt0, _ms0 = _to_float(_r0.get("t3_price") or _r0.get("target")), "T3"
                _pip_sz = 0.01 if "JPY" in _pair_str else 0.0001
                if _cur_p and _entry and _tgt0:
                    _rng  = abs(_tgt0 - _entry)
                    # Signed progress: +ve = toward target, -ve = against trade, >100 = past target
                    _prog = (
                        (_cur_p - _entry) / _rng if _dir0 == "BUY" else (_entry - _cur_p) / _rng
                    ) * 100 if _rng > 0 else 0.0
                    _dist = abs(_tgt0 - _cur_p) / _pip_sz

                    if _prog > 100:
                        # Price already past target — skip approaching alert, trigger milestone
                        log(
                            f"  Monitor: {_pair_str} HOT zone {_prog:.0f}% — target already "
                            f"crossed — triggering milestone detection instead of HOT zone alert"
                        )
                        try:
                            _spot_ms, _spot_state = _detect_spot_milestones(
                                _r0, _cur_p, _pair_str
                            )
                            if _spot_ms:
                                _fund_ids_hot = {int(r.get("id", -1)) for r in _fund_open}
                                if int(_r0.get("id", -2)) in _fund_ids_hot:
                                    _apply_fund_milestones(
                                        _r0, _spot_ms, _spot_state, log=log, ta=_ta,
                                        prices=prices,
                                    )
                                else:
                                    _apply_research_milestones(
                                        _r0, _spot_ms, _spot_state, log=log, ta=_ta
                                    )
                        except Exception as _spot_exc:
                            log(
                                f"  Monitor: immediate spot detection failed for "
                                f"{_pair_str}: {_spot_exc}"
                            )
                        # No send_fund_approaching — milestone detection sends the Discord alert

                    elif _prog <= 0:
                        # Price moved against trade — approaching stop loss.
                        # Use actual stop_loss from CSV (not effective_stop which may be
                        # the entry/breakeven level after T1 hit).
                        _stop_dist = abs(_cur_p - (_actual_stop or _entry)) / _pip_sz
                        # Warning zone size: the buffer between the HOT threshold and the
                        # actual stop.  HOT fires at 70% of stop_range from entry, so the
                        # remaining 30% is the "warning buffer" in pips.
                        _stop_range_d = (
                            abs((_entry or 0) - (_actual_stop or _entry or 0)) / _pip_sz
                        )
                        _warning_pips = round((1.0 - _HOT_THRESHOLD) * _stop_range_d)
                        # 2-hour cooldown for stop-approaching alerts (independent of HOT zone dedup)
                        # Always fire if within 5 pips of stop; suppress otherwise
                        _stop_alrt_suppress = False
                        try:
                            _sa_file = config.DATA_DIR / "stop_approach_alerts.json"
                            if _sa_file.exists():
                                _sa_data = json.loads(_sa_file.read_text(encoding="utf-8"))
                                _sa_ts   = _sa_data.get(_pair_str, {}).get("last_sent", "")
                                if _sa_ts:
                                    _sa_elapsed = (
                                        datetime.now(timezone.utc).replace(tzinfo=None) -
                                        datetime.strptime(_sa_ts[:19], "%Y-%m-%dT%H:%M:%S")
                                    ).total_seconds() / 3600
                                    if _stop_dist > 5 and _sa_elapsed < 2.0:
                                        log(
                                            f"  Monitor: stop alert suppressed for {_pair_str} "
                                            f"— sent {_sa_elapsed:.1f}h ago "
                                            f"({_stop_dist:.1f}p from stop)"
                                        )
                                        _stop_alrt_suppress = True
                        except Exception:
                            pass
                        if not _stop_alrt_suppress:
                            log(
                                f"  Monitor: {_pair_str} HOT zone {_prog:.0f}% "
                                f"— price moving against trade — sending stop-approaching alert "
                                f"(stop={_actual_stop} dist={_stop_dist:.1f}p warn={_warning_pips}p)"
                            )
                            _dn.send_fund_approaching(
                                _pair_str, _dir0, _prog,
                                _actual_stop or 0.0, _cur_p,
                                _stop_dist, _actual_stop or 0.0, "STOP",
                                entry_price=_entry or 0.0,
                                warning_pips=_warning_pips,
                            )
                            try:
                                _sa_file = config.DATA_DIR / "stop_approach_alerts.json"
                                _sa_w: dict = {}
                                if _sa_file.exists():
                                    try:
                                        _sa_w = json.loads(_sa_file.read_text(encoding="utf-8"))
                                    except Exception:
                                        pass
                                _sa_w[_pair_str] = {
                                    "last_sent": datetime.now(timezone.utc).replace(
                                        tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    "distance_pips": round(_stop_dist, 1),
                                }
                                _sa_file.write_text(json.dumps(_sa_w, indent=2), encoding="utf-8")
                            except Exception:
                                pass

                    else:
                        # 0 < _prog < 100 — price normally approaching cascade target
                        _dn.send_fund_approaching(
                            _pair_str, _dir0, _prog, _tgt0, _cur_p, _dist,
                            _stop0 or 0.0, _ms0
                        )
        except Exception:
            pass
        # Mark as continuously HOT if it was already HOT last run (pair stayed in HOT zone)
        _new_cont_hot = _pair_str in _prev_hot_pairs or _cont_hot
        _record_hot_alert_sent(_pair_str, continuously_hot=_new_cont_hot)
        _alerts_sent += 1

    result["approaching_alerts"] = _alerts_sent

    # ── Research HOT batch alert → WEBHOOK_RESEARCH (2-hour cooldown) ────────
    # Split _res_hot_rows into target-approaching vs stop-approaching buckets,
    # then send one combined message to #research (never to #monitor).
    _res_hot_target_rows: list = []
    _res_near_stop_rows:  list = []
    for _rr in _res_hot_rows:
        _rr_pair  = _rr.get("pair", "")
        _rr_dir   = (_rr.get("direction") or "").upper()
        _rr_entry = _to_float(_rr.get("entry")) or 0
        _rr_stop  = _to_float(_rr.get("stop_loss") or _rr.get("effective_stop")) or 0
        _rr_cur   = prices.get(_rr_pair)
        _rr_pip   = 0.01 if "JPY" in _rr_pair else 0.0001
        if not (_rr_cur and _rr_entry):
            continue
        _rr_ms = "T1" if not _is_true(_rr.get("t1_hit")) else (
                 "T2" if not _is_true(_rr.get("t2_hit")) else "T3")
        _rr_tgt_key = {"T1": "t1_price", "T2": "t2_price", "T3": "t3_price"}.get(_rr_ms, "t1_price")
        _rr_tgt  = _to_float(_rr.get(_rr_tgt_key)) or 0
        _rr_rng  = abs(_rr_tgt - _rr_entry) if _rr_tgt else 0
        _rr_prog = (((_rr_cur - _rr_entry) / _rr_rng if _rr_dir == "BUY"
                     else (_rr_entry - _rr_cur) / _rr_rng) * 100
                    if _rr_rng > 0 else 0.0)
        if _rr_prog <= 0 and _rr_stop:
            _res_near_stop_rows.append({
                "pair":               _rr_pair,
                "stop_distance_pips": round(abs(_rr_cur - _rr_stop) / _rr_pip, 1),
                "is_near_stop":       True,
            })
        else:
            _res_hot_target_rows.append({
                "pair":         _rr_pair,
                "progress_pct": round(_rr_prog),
                "target_label": _rr_ms,
                "distance_pips": round(abs(_rr_tgt - _rr_cur) / _rr_pip, 1) if _rr_tgt else 0,
                "is_near_stop": False,
            })

    # 2-hour cooldown; override if any near-stop row is < 20 pips away
    _res_batch_suppress = False
    try:
        _rb_file = config.DATA_DIR / "research_batch_alert.json"
        if _rb_file.exists():
            _rb_data = json.loads(_rb_file.read_text(encoding="utf-8"))
            _rb_ts   = _rb_data.get("last_sent", "")
            if _rb_ts:
                _rb_elapsed = (
                    datetime.now(timezone.utc).replace(tzinfo=None) -
                    datetime.strptime(_rb_ts[:19], "%Y-%m-%dT%H:%M:%S")
                ).total_seconds() / 3600
                _urgent = any(r.get("stop_distance_pips", 999) < 20 for r in _res_near_stop_rows)
                if _rb_elapsed < 2.0 and not _urgent:
                    log(f"  Monitor: research batch alert suppressed — sent {_rb_elapsed:.1f}h ago")
                    _res_batch_suppress = True
    except Exception:
        pass
    if not _res_batch_suppress and (_res_hot_target_rows or _res_near_stop_rows):
        log(
            f"  Monitor: research batch alert "
            f"({len(_res_hot_target_rows)} HOT target, {len(_res_near_stop_rows)} near stop)"
        )
        if _dn:
            _dn.send_research_monitor_batch(
                hot=_res_hot_target_rows,
                near_stop=_res_near_stop_rows,
            )
        try:
            _rb_file = config.DATA_DIR / "research_batch_alert.json"
            _rb_file.write_text(
                json.dumps({
                    "last_sent": datetime.now(timezone.utc).replace(tzinfo=None)
                                  .strftime("%Y-%m-%dT%H:%M:%SZ")
                }),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ── Step 3: Yahoo Finance 1H OHLCV for ALL open trades (no age cutoff) ───
    # All candles are used regardless of age — old data is better than no data.
    # Weekend: Friday closing candles are valid and checked for milestone crosses.
    from src.yahoo_finance import fetch_1h_candles as _yf_1h
    candle_map: dict = {}   # pair → list of candle dicts (newest-first)
    yf_ok       = 0
    for pair in all_pairs:
        try:
            candles_result = _yf_1h(pair, _OHLCV_CANDLES, log=log)
            if candles_result and candles_result.get("values"):
                candle_map[pair] = candles_result["values"]
                yf_ok += 1
        except Exception as exc:
            log(f"  Monitor: Yahoo 1H fetch error for {pair}: {exc}")
    result["yf_ohlcv_pairs"] = yf_ok
    log(
        f"Monitor: Yahoo Finance 1H OHLCV — {yf_ok}/{len(all_pairs)} pairs "
        f"({_OHLCV_CANDLES} candles each, no age cutoff)"
    )

    # Helper functions used in Step 4+5
    def _pip_factor(p: str) -> float:
        return 0.01 if "JPY" in p else 0.0001

    def _quick_atr(cands: list) -> float | None:
        if len(cands) < 2:
            return None
        trs = []
        for i in range(len(cands) - 1):
            try:
                h = float(cands[i]["high"])
                lo = float(cands[i]["low"])
                cp = float(cands[i + 1]["close"])
                trs.append(max(h - lo, abs(h - cp), abs(lo - cp)))
            except (ValueError, TypeError, KeyError):
                continue
        return sum(trs) / len(trs) if trs else None

    # ── Step 4+5: Resolve data tier per pair, detect milestones, apply cascade ──

    fund_closed: list = []
    research_fragments: list = []
    _tier_counts  = result["data_tiers_used"]
    _source_stats = result["source_stats"]

    def _resolve_pair_data(pair: str):
        """Return (tier_tag, candles_or_None, resolved_price, tier_label) for a pair.

        tier_tag matches a key in result['data_tiers_used']:
          t1_yf_ohlcv    — Yahoo 1H OHLCV candles (best quality)
          t1_td_backup   — Twelve Data live price  (+ synthetic candle if history >= 3)
          t1_sq_backup   — Stooq live price        (+ synthetic candle if history >= 3)
          t2_synthetic   — Internal synthetic candle (no live external price this run)
          t3_current_only— Current price, < 3 history readings
          t4_last_known  — Last known price from previous run
          t0_no_data     — Nothing available
        """
        pf = _pip_factor(pair)

        src = price_sources.get(pair, "none")  # "yahoo"|"twelve_data"|"stooq"|"none"

        # T1-YF: Yahoo 1H OHLCV candles available (any age — weekend candles included)
        if pair in candle_map:
            clist  = candle_map[pair]
            newest = clist[0].get("datetime", "")[:13] if clist else "?"
            high_v = max((float(c.get("high", 0)) for c in clist), default=0)
            low_v  = min((float(c.get("low",  999999)) for c in clist), default=0)
            curr_p = prices.get(pair) or last_prices.get(pair)
            label  = (f"[T1-YF] {pair}: Yahoo 1H ({len(clist)} candles, newest {newest}) "
                      f"HIGH {high_v:.5g} LOW {low_v:.5g}")
            return "t1_yf_ohlcv", clist, curr_p, label

        # T1-TD: Twelve Data live price (+ synthetic candle if 3+ history readings)
        if src == "twelve_data":
            curr_p = prices[pair]
            hist   = price_history.get(pair, [])
            synth  = _build_synthetic_candle(hist) if len(hist) >= 3 else None
            candles = [synth] if synth else None
            note    = f"+ synthetic ({len(hist)} readings)" if synth else "spot price only"
            label   = f"[T1-TD] {pair}: Twelve Data {curr_p} ({note})"
            return "t1_td_backup", candles, curr_p, label

        # T1-SQ: Stooq daily close (+ synthetic candle if 3+ history readings)
        if src == "stooq":
            curr_p = prices[pair]
            hist   = price_history.get(pair, [])
            synth  = _build_synthetic_candle(hist) if len(hist) >= 3 else None
            candles = [synth] if synth else None
            note    = f"+ synthetic ({len(hist)} readings)" if synth else "daily close only"
            label   = f"[T1-SQ] {pair}: Stooq {curr_p} ({note})"
            return "t1_sq_backup", candles, curr_p, label

        # T2-SYN: Synthetic candle from rolling price history (no live external price)
        hist = price_history.get(pair, [])
        if len(hist) >= 3:
            synth = _build_synthetic_candle(hist)
            if synth:
                curr_p = last_prices.get(pair)
                oldest = hist[0].get("timestamp", "")[:16]
                label  = (f"[T2-SYN] {pair}: synthetic candle ({len(hist)} readings, "
                          f"oldest {oldest}) HIGH {synth['high']} LOW {synth['low']}")
                return "t2_synthetic", [synth], curr_p, label

        # T3-CUR: Yahoo price but < 3 history readings (no synthetic possible)
        if src == "yahoo":
            curr_p = prices[pair]
            label  = (f"[T3-CUR] {pair}: Yahoo price {curr_p} "
                      f"(no candles, {len(hist)} history readings)")
            return "t3_current_only", None, curr_p, label

        # T4-PH: Last known price from previous run (all external sources unavailable)
        last_p = last_prices.get(pair)
        if last_p is not None:
            label = (f"[T4-PH] {pair}: last known price {last_p} "
                     f"(all external sources unavailable this run)")
            return "t4_last_known", None, last_p, label

        # T0: Nothing at all — should never happen in normal operation
        return "t0_no_data", None, None, f"[T0] {pair}: NO PRICE DATA — cannot monitor"

    # Resolve tiers for every unique pair (log once per pair, not per trade)
    pair_data: dict = {}
    _src_stat_map = {
        "t1_yf_ohlcv":    "yahoo_finance",
        "t1_td_backup":   "twelve_data",
        "t1_sq_backup":   "stooq",
        "t2_synthetic":   "synthetic",
        "t3_current_only":"yahoo_finance",
        "t4_last_known":  "internal_hist",
        "t0_no_data":     "no_data",
    }
    for pair in all_pairs:
        tag, candles_t, resolved_p, tier_label = _resolve_pair_data(pair)
        pair_data[pair] = (tag, candles_t, resolved_p)
        _tier_counts[tag]                       = _tier_counts.get(tag, 0) + 1
        _source_stats[_src_stat_map.get(tag, "no_data")] = (
            _source_stats.get(_src_stat_map.get(tag, "no_data"), 0) + 1
        )
        log(tier_label)

    # Part 7: log significant price movements (>= 0.5x ATR) as upgrade candidates
    for pair in all_pairs:
        curr = prices.get(pair)
        prev = last_prices.get(pair)
        if curr is None or prev is None:
            continue
        cands = candle_map.get(pair, [])
        atr   = _quick_atr(cands) if cands else None
        pf    = _pip_factor(pair)
        if atr and atr > 0:
            pip_move = abs(curr - prev) / pf
            atr_pips = atr / pf
            ratio    = pip_move / atr_pips if atr_pips > 0 else 0
            if ratio >= 0.5:
                log(
                    f"  Monitor: {pair} SIGNIFICANT move {pip_move:.1f}p "
                    f"({ratio:.2f}x ATR) since last run — best data tier used"
                )
            elif ratio >= 0.3:
                log(
                    f"  Monitor: {pair} moved {pip_move:.1f}p ({ratio:.2f}x ATR) since last run"
                )

    def _process_trade(row, candles, resolved_price, is_fund: bool):
        pair      = row.get("pair", "")
        direction = (row.get("direction") or "").upper()
        price     = resolved_price

        if price is None:
            log(f"  Monitor: {pair} #{row.get('id')} skipped — no price data from any source")
            return

        # Fast-path: all cascade targets already recorded in CSV — nothing to do
        if _is_true(row.get("t3_hit")):
            log(f"  Monitor: {pair} #{row.get('id')} all targets already recorded — skipping")
            return

        # Check milestone log for any levels already sent
        for _chk_lvl in ("T1", "T2", "T3"):
            _field_map = {"T1": "t1_hit", "T2": "t2_hit", "T3": "t3_hit"}
            if _is_true(row.get(_field_map[_chk_lvl])):
                _prev = _check_milestone_sent(pair, _chk_lvl)
                if not _prev:
                    _record_milestone_sent(
                        pair, _chk_lvl, _safe_int_id(row.get("id", 0)),
                        trade_type="fund" if is_fund else "research",
                    )

        # Detect milestones — candle data (T1/T2) or spot price (T3/T4)
        if candles:
            milestones, row_state = _detect_candle_milestones(row, candles, pair, log=log)
        else:
            milestones, row_state = _detect_spot_milestones(row, price, pair)

        if not milestones:
            return

        # Pre-filter already-sent milestones
        _dedup_filtered = []
        for _m in milestones:
            _lvl = _m["level"]
            if _lvl in ("T1", "T2", "T3"):
                _prev_ts = _check_milestone_sent(pair, _lvl)
                if _prev_ts:
                    log(
                        f"  Monitor: {pair} #{row.get('id')} {_lvl} already recorded — "
                        f"skipping duplicate (last sent {_prev_ts})"
                    )
                    continue
            _dedup_filtered.append(_m)

        if not _dedup_filtered:
            return
        milestones = _dedup_filtered

        # Apply to appropriate tracker
        if is_fund:
            closed = _apply_fund_milestones(
                row, milestones, row_state, log=log,
                ta=_ta, is_weekend=is_wknd,
                prices=prices,
            )
            fund_closed.extend(closed)
        else:
            closed_row, frag = _apply_research_milestones(
                row, milestones, row_state, log=log, ta=_ta,
            )
            if frag:
                research_fragments.append(frag)

        # Record in result milestones list
        for m in milestones:
            result["milestones_hit"].append({
                "type":      "fund" if is_fund else "research",
                "pair":      pair,
                "level":     m["level"],
                "price":     m["price"],
                "pips":      m["pips"],
                "candle_dt": m.get("candle_dt"),
            })

    for row in _fund_open:
        pair = row.get("pair", "")
        _, candles_t, resolved_p = pair_data.get(pair, ("t0_no_data", None, None))
        _process_trade(row, candles_t, resolved_p, is_fund=True)

    for row in _res_open:
        pair = row.get("pair", "")
        _, candles_t, resolved_p = pair_data.get(pair, ("t0_no_data", None, None))
        _process_trade(row, candles_t, resolved_p, is_fund=False)

    # ── Step 4b: Trailing stop updates for all open fund trades ──────────────
    # Runs after milestone detection so cascade state is fresh.
    _trailing_updates = 0
    try:
        from src import tracker as _trk_trail
        import pandas as _pd_trail
        _df_trail = _pd_trail.read_csv(str(config.TRADES_CSV), encoding="utf-8-sig")
        for _tr_row in _fund_open:
            _tr_pair = _tr_row.get("pair", "")
            _tr_id   = int(_tr_row.get("id", 0) or 0)
            _tr_price = prices.get(_tr_pair)
            if _tr_price is None:
                continue
            # Re-read the latest row state (cascade may have updated it this run)
            _tr_idx_list = _df_trail.index[
                _df_trail["id"].astype(str) == str(_tr_id)
            ].tolist()
            if not _tr_idx_list:
                continue
            _tr_idx = _tr_idx_list[0]
            _tr_latest = _df_trail.iloc[_tr_idx].to_dict()
            if str(_tr_latest.get("status", "")).upper() != "OPEN":
                continue
            _trail = _update_trailing_stop(
                trade_row=_tr_latest,
                current_price=_tr_price,
                log_fn=log,
            )
            if _trail["stop_moved"]:
                _old_stop = float(_tr_latest.get("effective_stop") or
                                  _tr_latest.get("stop_loss") or 0)
                _df_trail.at[_tr_idx, "effective_stop"] = _trail["new_effective_stop"]
                from src.trading.financials import atomic_write_csv as _awc_trail
                _awc_trail(config.TRADES_CSV, _df_trail)
                _trailing_updates += 1
                log(f"[trailing] #{_tr_id} {_tr_pair}: stop trailed to "
                    f"{_trail['new_effective_stop']:.5f} — {_trail['reason']}")
                try:
                    if _dn:
                        _dn.send_trailing_stop_update(
                            trade_id=_tr_id,
                            pair=_tr_pair,
                            direction=str(_tr_latest.get("direction", "")).upper(),
                            old_stop=_old_stop,
                            new_stop=_trail["new_effective_stop"],
                            reason=_trail["reason"],
                        )
                except Exception as _trail_dn_exc:
                    log(f"[trailing] Discord alert error: {_trail_dn_exc}")
        if not _trailing_updates:
            log("[trailing] No stops trailed this run")
        else:
            log(f"[trailing] {_trailing_updates} stop(s) trailed")
    except Exception as _trail_exc:
        log(f"[trailing] Trailing stop update failed: {_trail_exc}")

    # Data tier + source summary
    _tc = _tier_counts
    _yf  = _tc.get("t1_yf_ohlcv", 0)
    _td  = _tc.get("t1_td_backup", 0)
    _sq  = _tc.get("t1_sq_backup", 0)
    _syn = _tc.get("t2_synthetic", 0)
    _cur = _tc.get("t3_current_only", 0)
    _ph  = _tc.get("t4_last_known", 0)
    _nod = _tc.get("t0_no_data", 0)
    _parts = [f"[T1-YF]={_yf}"]
    if _td:  _parts.append(f"[T1-TD]={_td}")
    if _sq:  _parts.append(f"[T1-SQ]={_sq}")
    if _syn: _parts.append(f"[T2-SYN]={_syn}")
    if _cur: _parts.append(f"[T3-CUR]={_cur}")
    if _ph:  _parts.append(f"[T4-PH]={_ph}")
    if _nod: _parts.append(f"[T0]={_nod}")
    log(f"Monitor data sources ({len(all_pairs)} pairs): {' '.join(_parts)}")
    if _nod == 0:
        log("Monitor: all pairs covered — zero unmonitored trades")

    # ── Step 6: MFE/MAE updates for research trades ───────────────────────────
    mfe_updated = 0
    for row in _res_open:
        pair      = row.get("pair", "")
        direction = (row.get("direction") or "").upper()
        rec_id    = _safe_int_id(row.get("id", 0))
        try:
            _, candles_mfe, resolved_p_mfe = pair_data.get(pair, ("t0_no_data", None, None))
            if candles_mfe:
                if _update_mfe_mae_from_ohlcv(rec_id, direction, candles_mfe):
                    mfe_updated += 1
            elif resolved_p_mfe is not None:
                _rt.update_mfe_mae(rec_id, resolved_p_mfe)
                mfe_updated += 1
        except Exception:
            pass

    result["mfe_mae_updated"] = mfe_updated
    if mfe_updated:
        log(f"Monitor: MFE/MAE updated for {mfe_updated} research trade(s).")

    # ── Step 7: Research milestones — Discord #research only, never Telegram ──
    if research_fragments:
        log(
            f"[monitor] {len(research_fragments)} research milestone(s) — "
            "Discord #research only, not sending to Telegram"
        )

    # ── Summary log ──────────────────────────────────────────────────────────
    n_ms = len(result["milestones_hit"])
    if n_ms:
        log(
            f"Monitor complete: {n_ms} milestone(s) detected — "
            f"{len(fund_closed)} fund trade(s) closed — "
            f"{len(research_fragments)} research milestone(s) recorded."
        )
    else:
        log(
            f"Monitor complete: no milestones — all {len(all_open)} trades within normal range — "
            f"{result['api_calls_used']} API calls used."
        )

    result["last_prices"]    = {p: prices[p] for p in all_pairs if p in prices}
    result["previously_hot"] = sorted(current_hot_keys)
    _append_to_monitor_history(result)
    _write_monitor_log(result)

    # ── Discord fund dashboard update ─────────────────────────────────────────
    try:
        if _dn:
            import pandas as _pd_dash
            from src.trading import financials as _fin_dash

            # Fresh CSV read + live prices → every number reflects reality NOW
            _df_dash    = _pd_dash.read_csv(str(config.TRADES_CSV), encoding="utf-8-sig")
            _state_dash = _fin_dash.calculate_fund_state(_df_dash, prices)

            # _state_dash["open_trades"] is now fully populated by _open_trade_summary()
            _dash_trades = _state_dash["open_trades"]
            _dash_bal    = float(_state_dash.get("balance") or 0)
            _dash_pnl_d  = float(_state_dash.get("daily_pnl_pct") or 0)
            _dash_pnl_usd = float(_state_dash.get("daily_pnl_dollars") or 0)
            _dash_open_bal = float(_state_dash.get("daily_opening_balance") or 10000)
            _dash_ret    = (_dash_bal - 10000.0) / 10000.0 * 100 if _dash_bal else 0.0
            _total_unreal_usd  = float(_state_dash.get("unrealised_dollars") or 0)
            _total_unreal_pips = float(_state_dash.get("unrealised_pips") or 0)
            _total_equity      = float(_state_dash.get("total_equity") or _dash_bal)
            _ftmo_pct = (_total_equity - 10000.0) / 10000.0 * 100

            # Log per-trade progress for debugging
            for _dt in _dash_trades:
                log(
                    f"[progress] {_dt.get('pair','')} {_dt.get('direction','')}: "
                    f"entry={_dt.get('entry',0):.5f} cur={_dt.get('current',0):.5f} "
                    f"target={_dt.get('t1',0):.5f} ({_dt.get('next_target','T1')}) "
                    f"progress={_dt.get('progress_pct',0):.1f}% "
                    f"P&L={_dt.get('pips_unrealised',0):+.1f}p/${_dt.get('dollars_unrealised',0):+.2f} "
                    f"t1h={_dt.get('t1_hit',False)} t2h={_dt.get('t2_hit',False)} "
                    f"days={_dt.get('days_open',0)}"
                )

            # ── Fund trade statistics ──────────────────────────────────────────
            _st_wins = _st_losses = _st_partial = _st_breakeven = _st_decisive = 0
            _st_wr = _st_avg_win = _st_avg_loss = _st_pf = _st_best_pips = _st_total_pips = 0.0
            _st_avg_win_d = _st_avg_loss_d = _st_dollar_pf = 0.0
            _st_best_pair = ""
            _st_total = 0
            try:
                import csv as _csv_dash
                import math as _math_st
                with open(config.DATA_DIR / "trades.csv", encoding="utf-8-sig", newline="") as _tf:
                    _fund_rows = [r for r in _csv_dash.DictReader(_tf)
                                  if r.get("trade_this") == "YES"]
                _st_total = len(_fund_rows)
                _sample   = _fund_rows[0] if _fund_rows else {}
                _has_outcome = (
                    "outcome" in _sample and
                    any(str(r.get("outcome") or "").strip() for r in _fund_rows)
                )
                _result_col = "outcome" if _has_outcome else "status"
                log(f"  [fund-stats] result_col={_result_col} total={_st_total}")

                _win_pips_list, _loss_pips_list = [], []
                _win_dollars_list, _loss_dollars_list = [], []
                _best_pips_seen = 0.0
                _fs_sizing_pct = 1.0

                for _fr in _fund_rows:
                    _st_out = str(_fr.get(_result_col) or "").upper()
                    _raw_pip = (
                        _fr.get("cascading_total_pips_weighted") or
                        _fr.get("cascading_total_pips") or
                        _fr.get("pips") or ""
                    )
                    try:
                        _fp = float(_raw_pip) if _raw_pip != "" else 0.0
                        if _math_st.isnan(_fp):
                            _fp = 0.0
                    except (ValueError, TypeError):
                        _fp = 0.0

                    _pair_d_st = str(_fr.get("pair") or "")
                    _pip_sz_st = 0.01 if "JPY" in _pair_d_st.upper() else 0.0001
                    try:
                        _sz_pct_raw = _fr.get("position_size_pct_at_entry")
                        _sz_pct_st = float(_sz_pct_raw) if (
                            _sz_pct_raw and str(_sz_pct_raw).strip() not in ("", "nan")
                        ) else _fs_sizing_pct
                        _risk_usd_st = _dash_open_bal * _sz_pct_st / 100.0
                        _entry_v = float(_fr.get("entry") or 0)
                        _stop_v  = float(_fr.get("stop_loss") or 0)
                        _sp_st   = abs(_entry_v - _stop_v) / _pip_sz_st if _entry_v and _stop_v else 0.0
                        _dollar_pnl = _fp * (_risk_usd_st / _sp_st) if _sp_st > 0 else 0.0
                    except (ValueError, TypeError):
                        _dollar_pnl = 0.0

                    if _st_out in ("WIN", "FULL_WIN"):
                        _st_wins += 1
                        _win_pips_list.append(_fp)
                        if _dollar_pnl > 0:
                            _win_dollars_list.append(_dollar_pnl)
                    elif _st_out == "PARTIAL_WIN":
                        _st_partial += 1
                        _win_pips_list.append(_fp)
                        if _dollar_pnl > 0:
                            _win_dollars_list.append(_dollar_pnl)
                    elif _st_out == "BREAKEVEN":
                        _st_breakeven += 1
                    elif _st_out in ("LOSS", "EXPIRED", "EXPIRED_LOSS", "STALE_EXIT"):
                        if _fp > 0:
                            _st_partial += 1
                            _win_pips_list.append(_fp)
                            if _dollar_pnl > 0:
                                _win_dollars_list.append(_dollar_pnl)
                        else:
                            _st_losses += 1
                            _loss_pips_list.append(abs(_fp))
                            if _dollar_pnl < 0:
                                _loss_dollars_list.append(abs(_dollar_pnl))
                    if _fp > _best_pips_seen:
                        _best_pips_seen = _fp
                        _st_best_pair = _pair_d_st

                _st_best_pips = _best_pips_seen
                _st_decisive  = _st_wins + _st_losses + _st_partial
                if _st_decisive > 0:
                    _st_wr = (_st_wins + _st_partial) / _st_decisive * 100
                _tot_win  = sum(_win_pips_list)
                _tot_loss = sum(_loss_pips_list)
                _st_avg_win  = _tot_win  / len(_win_pips_list)  if _win_pips_list  else 0.0
                _st_avg_loss = _tot_loss / len(_loss_pips_list) if _loss_pips_list else 0.0
                _st_pf       = _tot_win / _tot_loss             if _tot_loss > 0   else 0.0
                _st_total_pips = _tot_win - _tot_loss
                _tot_win_d   = sum(_win_dollars_list)
                _tot_loss_d  = sum(_loss_dollars_list)
                _st_avg_win_d  = _tot_win_d  / len(_win_dollars_list)  if _win_dollars_list  else 0.0
                _st_avg_loss_d = _tot_loss_d / len(_loss_dollars_list) if _loss_dollars_list else 0.0
                _st_dollar_pf  = _tot_win_d / _tot_loss_d if _tot_loss_d > 0 else 0.0
                log(
                    f"  [fund-stats] wins={_st_wins} protected={_st_partial} "
                    f"losses={_st_losses} decisive={_st_decisive} wr={_st_wr:.1f}%"
                )
            except Exception as _st_exc:
                log(f"  Monitor: fund stats calculation failed: {_st_exc}")

            from src import fund_state as _fs_dash
            _fs_d = _fs_dash.load()

            # Build full state dict — single source of truth for the dashboard
            _full_state = {
                **_state_dash,
                # Statistics (computed above)
                "win_count":           _st_wins,
                "protected_count":     _st_partial,
                "loss_count":          _st_losses,
                "breakeven_count":     _st_breakeven,
                "decisive_count":      _st_decisive,
                "win_rate":            _st_wr,
                "profit_factor":       _st_pf,
                "avg_win_pips":        _st_avg_win,
                "avg_win_dollars":     _st_avg_win_d,
                "avg_loss_pips":       _st_avg_loss,
                "avg_loss_dollars":    _st_avg_loss_d,
                "fund_total_trades":   _st_total,
                "best_pair":           _st_best_pair,
                "best_pips":           _st_best_pips,
                "fund_dollar_pf":      _st_dollar_pf,
                # Metadata from fund_state.json (sizing, win streak — not in CSV)
                "sizing_mode":         str(_fs_d.get("sizing_mode") or "normal"),
                "current_sizing_pct":  float(_fs_d.get("current_sizing_pct") or 1.0),
                "consecutive_wins":    int(_fs_d.get("consecutive_wins") or 0),
            }
            _send_dashboard(state=_full_state, log_fn=log)
    except Exception as _dash_exc:
        import traceback as _tb_dash
        log(f"  Monitor: Discord dashboard update failed: {_dash_exc}")
        log(_tb_dash.format_exc())

    # ── Closed trades log ─────────────────────────────────────────────────────
    try:
        if _dn:
            import csv as _csv_ct
            import pandas as _pd_ct

            _ct_rows = []
            try:
                with open(config.DATA_DIR / "trades.csv", encoding="utf-8-sig", newline="") as _ctf:
                    _all_ct = [r for r in _csv_ct.DictReader(_ctf)
                               if r.get("trade_this") == "YES"]
                _closed_ct = [r for r in _all_ct
                               if str(r.get("status", "")).upper() not in ("OPEN", "")]
                # Sort most recent first by closed_at
                def _ct_sort_key(r):
                    return str(r.get("closed_at") or r.get("timestamp") or "")
                _closed_ct.sort(key=_ct_sort_key, reverse=True)
            except Exception:
                _closed_ct = []

            def _ct_float(val, default=0.0):
                try:
                    return float(str(val))
                except Exception:
                    return default

            def _ct_bool(val):
                return str(val).strip().upper() in ("TRUE", "YES", "1", "T")

            try:
                _ct_fund_bal = float(_fs_d.get("daily_opening_balance") or 10000)
            except Exception:
                _ct_fund_bal = 10000.0

            for _cr in _closed_ct:
                _ct_pair    = str(_cr.get("pair", ""))
                _ct_pip     = 0.01 if "JPY" in _ct_pair else 0.0001
                _ct_entry   = _ct_float(_cr.get("entry"))
                _ct_stop    = _ct_float(_cr.get("stop_loss"))
                _ct_exitp   = _ct_float(_cr.get("exit_price"))
                _ct_pips    = _ct_float(_cr.get("pips"))
                _ct_wpips   = _ct_float(
                    _cr.get("cascading_total_pips_weighted") or _cr.get("pips"))

                _ct_risk_pct  = _ct_float(_cr.get("position_size_pct_at_entry"), 1.0)
                _ct_risk_usd  = _ct_fund_bal * _ct_risk_pct / 100
                _ct_stop_pips = (abs(_ct_entry - _ct_stop) / _ct_pip
                                  if _ct_stop and _ct_entry else 0)
                _ct_dpp       = (_ct_risk_usd / _ct_stop_pips
                                  if _ct_stop_pips > 0 else 0)

                _ct_t1h = _ct_bool(_cr.get("t1_hit"))
                _ct_t2h = _ct_bool(_cr.get("t2_hit"))

                if _ct_t2h:
                    _ct_cascade = "T1+T2 banked · 30% ran"
                elif _ct_t1h:
                    _ct_cascade = "T1 banked · 60% ran"
                else:
                    _ct_cascade = None

                _ct_dollars = round(_ct_wpips * _ct_dpp, 2) if _ct_dpp > 0 else 0.0

                # Hold time
                _ct_opened   = str(_cr.get("timestamp", ""))
                _ct_closedat = str(_cr.get("closed_at", ""))
                _ct_hold     = "?"
                _ct_fmt_opts = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"]
                try:
                    _dto, _dtc = None, None
                    for _fmt in _ct_fmt_opts:
                        try:
                            _dto = datetime.strptime(_ct_opened[:19], _fmt); break
                        except Exception:
                            pass
                    for _fmt in _ct_fmt_opts:
                        try:
                            _dtc = datetime.strptime(_ct_closedat[:19], _fmt); break
                        except Exception:
                            pass
                    if _dto and _dtc:
                        _ct_delta = _dtc - _dto
                        _ct_total_h = _ct_delta.total_seconds() / 3600
                        if _ct_total_h < 24:
                            _ct_hold = f"{int(_ct_total_h)}h {int((_ct_total_h % 1) * 60)}m"
                        else:
                            _ct_hold = f"{int(_ct_total_h // 24)}d {int(_ct_total_h % 24)}h"
                except Exception:
                    pass

                _ct_status  = str(_cr.get("status", ""))
                _ct_outcome = _ct_status
                if _ct_status.upper() == "EXPIRED":
                    _ct_outcome = "WIN (expired)" if _ct_pips > 0 else "LOSS (expired)"

                _ct_rows.append({
                    "id":         _cr.get("id", ""),
                    "pair":       _ct_pair,
                    "direction":  str(_cr.get("direction", "")),
                    "conf":       _ct_float(_cr.get("confidence")),
                    "entry":      _ct_entry,
                    "exit_price": _ct_exitp,
                    "stop_loss":  _ct_stop,
                    "pips":       _ct_wpips,
                    "dollars":    _ct_dollars,
                    "status":     _ct_status,
                    "outcome":    _ct_outcome,
                    "cascade":    _ct_cascade,
                    "closed_at":  _ct_closedat[:10] if _ct_closedat else "",
                    "hold_time":  _ct_hold,
                    "t1_hit":     _ct_t1h,
                    "t2_hit":     _ct_t2h,
                })

            _ct_total_pips    = sum(t["pips"]    for t in _ct_rows)
            _ct_total_dollars = sum(t["dollars"] for t in _ct_rows)

            _ct_wins_row = 0
            _ct_loss_row = 0
            for _ct_t in _ct_rows:
                _oc = _ct_t["outcome"]
                if "WIN" in _oc:
                    _ct_wins_row += 1; _ct_loss_row = 0
                elif "LOSS" in _oc:
                    _ct_loss_row += 1; _ct_wins_row = 0
                else:
                    break

            _dn.update_closed_trades_log(
                closed_trades=_ct_rows,
                fund_balance=_ct_fund_bal,
                total_realised_pips=_ct_total_pips,
                total_realised_dollars=_ct_total_dollars,
                win_streak=_ct_wins_row,
                loss_streak=_ct_loss_row,
            )
            log("Closed trades log updated ✅")
    except Exception as _ct_exc:
        log(f"Closed trades log failed: {_ct_exc}")

    # Item 2: Write heartbeat — checked by daily.py to detect monitor downtime
    try:
        _hb_ts = datetime.now(timezone.utc).isoformat()
        _HEARTBEAT_FILE.write_text(
            json.dumps({
                "last_run":          _hb_ts,
                "last_monitor_run":  _hb_ts,
                "monitor_interval_mins": 30,
            }),
            encoding="utf-8",
        )
    except Exception:
        pass

    _release_lock()
    return result
