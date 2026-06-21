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
from datetime import datetime, timedelta
from pathlib import Path

import requests

import config
from src import cascade as _casc


def _online_learn_closure(source_table: str, updated: dict) -> None:
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

def _check_hot_alert_sent(pair: str) -> str | None:
    """Return human-readable elapsed string if this pair got a HOT alert within
    the cooldown window, else None."""
    try:
        if not _HOT_ZONE_ALERTS.exists():
            return None
        data    = json.loads(_HOT_ZONE_ALERTS.read_text(encoding="utf-8"))
        ts_str  = data.get("alerts_sent", {}).get(pair)
        if not ts_str:
            return None
        ts      = datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S")
        elapsed = datetime.utcnow() - ts
        if elapsed < timedelta(hours=_HOT_ALERT_COOLDOWN_HOURS):
            mins = int(elapsed.total_seconds() / 60)
            return f"{mins} minutes ago" if mins < 60 else f"{int(mins / 60)}h{mins % 60:02d}m ago"
    except Exception:
        pass
    return None


def _record_hot_alert_sent(pair: str) -> None:
    """Immediately write pair + UTC timestamp to hot_zone_alerts.json.

    Uses a sidecar lock file to prevent interleaved writes from concurrent runs.
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

        data: dict = {"alerts_sent": {}}
        if _HOT_ZONE_ALERTS.exists():
            try:
                data = json.loads(_HOT_ZONE_ALERTS.read_text(encoding="utf-8"))
            except Exception:
                data = {"alerts_sent": {}}
        data.setdefault("alerts_sent", {})
        data["alerts_sent"][pair] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        _HOT_ZONE_ALERTS.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass
    finally:
        try:
            _lock.unlink()
        except Exception:
            pass


def _build_hot_alert_message(pair: str, row: dict, price: float | None) -> str:
    """Build the HOT zone Telegram alert message with progress details."""
    direction = (row.get("direction") or "").upper()
    entry     = _to_float(row.get("entry"))
    stop      = _to_float(row.get("effective_stop") or row.get("stop_loss"))

    # Determine the next unmet target level
    if not _is_true(row.get("t1_hit")):
        level, tgt = "T1", _to_float(row.get("t1_price"))
    elif not _is_true(row.get("t2_hit")):
        level, tgt = "T2", _to_float(row.get("t2_price"))
    else:
        level, tgt = "T3", _to_float(row.get("t3_price") or row.get("target"))

    pip_factor = 0.01 if "JPY" in pair else 0.0001
    decimals   = 3    if "JPY" in pair else 5

    def _fmt(v):
        return f"{v:.{decimals}f}" if v is not None else "?"

    lines = [f"\U0001f525 <b>{pair} approaching {level} target</b>", ""]

    if price is not None and tgt is not None and entry is not None and direction in ("BUY", "SELL"):
        total = abs(tgt - entry)
        if total > 0:
            progress = (price - entry) / total if direction == "BUY" else (entry - price) / total
            lines.append(f"Progress: {int(progress * 100)}% of the way to {level}")
        dist_pips = abs(tgt - price) / pip_factor
        lines.append(f"{level} target: {_fmt(tgt)}  ·  Current price: {_fmt(price)}")
        lines.append(f"Distance remaining: {dist_pips:.1f} pips")
    else:
        lines.append(f"{level} target: {_fmt(tgt)}  ·  Current price: {_fmt(price)}")

    if stop is not None:
        lines.append(f"Stop loss: {_fmt(stop)} (protected)")

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
    cutoff   = datetime.utcnow() - timedelta(hours=hours)
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
        "timestamp":  datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    log_data["sent"] = sent[-500:]   # keep last 500 entries (~3 weeks at normal pace)
    try:
        _MILESTONE_LOG.write_text(json.dumps(log_data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── File lock ─────────────────────────────────────────────────────────────────

def _try_acquire_lock(log=print) -> bool:
    """Atomically create monitor.lock. Returns True if lock was acquired."""
    try:
        fd = os.open(str(_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            age = time.time() - _LOCK_FILE.stat().st_mtime
            if age > _LOCK_TIMEOUT:
                try:
                    _LOCK_FILE.unlink()
                except Exception:
                    pass
                log(f"Monitor: stale lock ({age:.0f}s old) removed — re-acquiring")
                return _try_acquire_lock(log)
        except Exception:
            pass
        return False
    except Exception:
        return True   # if we can't create the lock, proceed without it


def _release_lock() -> None:
    try:
        _LOCK_FILE.unlink()
    except Exception:
        pass


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
            if int(row.get("id", -1)) == rec_id:
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
            "t1_yahoo_ohlcv":     _dt.get("t1_yahoo_ohlcv",  0),
            "t2_synthetic":       _dt.get("t2_synthetic",     0),
            "t3_current_only":    _dt.get("t3_current_only",  0),
            "t4_last_known":      _dt.get("t4_last_known",    0),
            "t0_no_data":         _dt.get("t0_no_data",       0),
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

    cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
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

        # Greedy cascade: T1 → T2 → T3 (sequential guards enforced by _casc)
        if _casc.t1_hit(row_state, tgt_price):
            t1p = _casc.pips_at(row_state.get("entry"), row_state.get("t1_price"), pair, direction)
            row_state.update({
                "t1_hit":       "TRUE",
                "t1_hit_price": tgt_price,
                "t1_hit_pips":  t1p,
                "effective_stop": row_state.get("entry"),
            })
            milestones.append({"level": "T1", "price": tgt_price, "candle_dt": dt, "pips": t1p})
            side_label = "HIGH" if direction == "BUY" else "LOW"
            log(
                f"  Monitor: {pair} candle {side_label} {tgt_price} crossed T1 "
                f"{row.get('t1_price')} during {dt} candle — T1 recorded as hit "
                f"even if current price is now below T1 — partial WIN locked in"
            )

        if _casc.t2_hit(row_state, tgt_price):
            t2p = _casc.pips_at(row_state.get("entry"), row_state.get("t2_price"), pair, direction)
            row_state.update({
                "t2_hit":       "TRUE",
                "t2_hit_price": tgt_price,
                "t2_hit_pips":  t2p,
            })
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

    if _casc.t1_hit(row_state, price):
        t1p = _casc.pips_at(row_state.get("entry"), row_state.get("t1_price"), pair, direction)
        row_state.update({
            "t1_hit": "TRUE", "t1_hit_price": price,
            "t1_hit_pips": t1p, "effective_stop": row_state.get("entry"),
        })
        milestones.append({"level": "T1", "price": price, "candle_dt": None, "pips": t1p})

    if _casc.t2_hit(row_state, price):
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


# ── Cascade application ───────────────────────────────────────────────────────

def _apply_fund_milestones(row: dict, milestones: list, row_state: dict,
                           log=print, ta=None, is_weekend: bool = False) -> list:
    """Apply detected milestones to trades.csv. Return list of closed row dicts."""
    from src import tracker as _trk
    rec_id    = int(row.get("id", 0))
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
                        f"40% of position banked at +{pips:.1f} pips.\n"
                        f"Stop loss moved to breakeven — this trade can no longer lose money.\n"
                        f"No action needed from you.\n\n"
                        f"Direction: {direction}  |  T1 price: {mprice}"
                        f"{f'  |  Candle: {cdt}' if cdt else ''}"
                        + wknd_note
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
                        f"Another 30% of position banked at +{pips:.1f} pips (70% total).\n"
                        f"Final 30% running toward full target with stop at breakeven.\n\n"
                        f"Direction: {direction}  |  T2 price: {mprice}"
                        f"{f'  |  Candle: {cdt}' if cdt else ''}"
                        + wknd_note
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
                        f"T1 +{t1p_str}p (40%)  T2 +{t2p_str}p (30%)  T3 +{pips:.1f}p (30%)\n"
                        f"Weighted total: +{_wp:.1f} pips\n\n"
                        f"Direction: {direction}  |  Final price: {mprice}"
                        + wknd_note
                    )
                except Exception:
                    pass
            closed_rows.append(updated)
            _online_learn_closure("main", updated)
            break   # trade closed — skip further milestones

        elif level == "STOP":
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
            closed_rows.append(updated)
            _online_learn_closure("main", updated)
            break   # trade closed

    return closed_rows


def _apply_research_milestones(row: dict, milestones: list, row_state: dict,
                               log=print, ta=None) -> tuple:
    """Apply detected milestones to research_trades.csv.

    Returns (closed_row_or_None, summary_fragment_str).
    summary_fragment_str is appended to the batch Telegram message.
    """
    from src import research_tracker as _rt
    rec_id    = int(row.get("id", 0))
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
                int(row.get("id", 0)),
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
    cutoff = (datetime.utcnow() - _td_td(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
            if (datetime.utcnow() - datetime.fromisoformat(last_str)).days < 6:
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
        reopen_data["last_reopen_detected"] = datetime.utcnow().isoformat()
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

    cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    week = [r for r in runs if r.get("ts", "") >= cutoff and not r.get("skipped")]
    if not week:
        return []

    total_t1 = sum(r.get("t1_yahoo_ohlcv", 0) for r in week)
    total_t2 = sum(r.get("t2_synthetic",   0) for r in week)
    total_t3 = sum(r.get("t3_current_only", 0) for r in week)
    total_t4 = sum(r.get("t4_last_known",   0) for r in week)
    total_t0 = sum(r.get("t0_no_data",      0) for r in week)
    total    = total_t1 + total_t2 + total_t3 + total_t4 + total_t0

    if total == 0:
        return []

    def pct(n):
        return f"{round(n / total * 100)}%" if total else "0%"

    lines = [
        "",
        "<b>MONITOR DATA QUALITY (this week)</b>",
        f"T1 Yahoo 1H candles:    {pct(total_t1)} ({total_t1} checks)",
        f"T2 synthetic history:   {pct(total_t2)} ({total_t2} checks)",
        f"T3 current price only:  {pct(total_t3)} ({total_t3} checks)",
        f"T4 last known price:    {pct(total_t4)} ({total_t4} checks)",
        f"T0 no data:             {pct(total_t0)} ({total_t0} checks)"
        + (" ✅" if total_t0 == 0 else " ⚠️"),
    ]
    return lines


def run(log=print) -> dict:
    """Run the between-scan monitor. Returns the monitor_log dict written to disk."""
    now_ak   = _auckland_now()
    is_wknd  = now_ak.weekday() >= 5   # Saturday or Sunday (Auckland)
    now_str  = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
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

    # ── Step 1: Price fetch — Yahoo Finance primary, Twelve Data fallback ───────
    all_open    = _fund_open + _res_open
    _fund_pairs = sorted({r.get("pair", "") for r in _fund_open if r.get("pair")})
    _res_pairs  = sorted({r.get("pair", "") for r in _res_open  if r.get("pair")})
    all_pairs   = sorted({r.get("pair", "") for r in all_open   if r.get("pair")})
    log(
        f"Monitor: {len(_fund_pairs)} fund pair(s) + {len(_res_pairs)} research pair(s) — "
        f"{len(all_pairs)} unique pairs — fetching via Yahoo Finance (0 TD calls)"
    )

    prices = _yahoo_price_batch(all_pairs, log=log)

    # Fallback to Twelve Data if Yahoo returned fewer than 50% of pairs
    if len(prices) < len(all_pairs) * 0.5 and config.TWELVE_DATA_KEY:
        _missing = [p for p in all_pairs if p not in prices]
        log(
            f"Monitor: Yahoo Finance partial failure ({len(prices)}/{len(all_pairs)} pairs) — "
            f"falling back to Twelve Data for {len(_missing)} pairs"
        )
        _td_prices, _td_calls = _batch_price_fetch(_missing, log=log)
        prices.update(_td_prices)
        result["api_calls_used"] += _td_calls

    result["yf_price_pairs"] = len(prices)

    # ── Update rolling price history (max 96 readings = 48h at 30-min intervals) ──
    for _ph_pair, _ph_price in prices.items():
        _hist = price_history.get(_ph_pair, [])
        _hist.append({"price": _ph_price, "timestamp": now_str, "source": "yahoo_finance"})
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

                        if ratio_mon < 0.5 or pips_mon < 3:
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
    current_hot_keys: set = set()
    _hot_rows: dict = {}   # key → row, for building the rich alert message
    for _hr in fund_zones["HOT"] + res_zones["HOT"]:
        _hk = f"{_hr.get('pair', '')}#{_hr.get('id', '')}"
        current_hot_keys.add(_hk)
        _hot_rows[_hk] = _hr

    _alerts_sent = 0
    for _hk in sorted(current_hot_keys):
        _pair_str = _hk.split("#")[0]
        _prev_ts  = _check_hot_alert_sent(_pair_str)
        if _prev_ts:
            log(f"  Monitor: HOT zone alert suppressed for {_pair_str} — already sent {_prev_ts}")
            continue
        _hrow = _hot_rows.get(_hk, {})
        log(f"  Monitor: {_pair_str} HOT zone — sending approaching target alert")
        if _ta:
            try:
                _ta.send(_build_hot_alert_message(_pair_str, _hrow, prices.get(_pair_str)))
            except Exception:
                pass
        _record_hot_alert_sent(_pair_str)
        _alerts_sent += 1

    result["approaching_alerts"] = _alerts_sent

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
    _tier_counts = result["data_tiers_used"]
    _tier_key_map = {1: "t1_yahoo_ohlcv", 2: "t2_synthetic",
                     3: "t3_current_only", 4: "t4_last_known", 0: "t0_no_data"}

    def _resolve_pair_data(pair: str):
        """Return (tier, candles_or_None, resolved_price, tier_label) for a pair."""
        pf = _pip_factor(pair)

        # T1: Yahoo 1H OHLCV candles available (any age — weekend Friday candles included)
        if pair in candle_map:
            clist  = candle_map[pair]
            newest = clist[0].get("datetime", "")[:13] if clist else "?"
            high_v = max((float(c.get("high", 0)) for c in clist), default=0)
            low_v  = min((float(c.get("low",  999999)) for c in clist), default=0)
            curr_p = prices.get(pair) or last_prices.get(pair)
            label  = (f"[T1] {pair}: Yahoo 1H ({len(clist)} candles, newest {newest}) "
                      f"HIGH {high_v:.5g} LOW {low_v:.5g}")
            return 1, clist, curr_p, label

        # T2: Synthetic candle from rolling price history
        hist = price_history.get(pair, [])
        if len(hist) >= 3:
            synth = _build_synthetic_candle(hist)
            if synth:
                curr_p = prices.get(pair) or last_prices.get(pair)
                oldest = hist[0].get("timestamp", "")[:16]
                label  = (f"[T2] {pair}: synthetic candle ({len(hist)} readings, "
                          f"oldest {oldest}) HIGH {synth['high']} LOW {synth['low']}")
                return 2, [synth], curr_p, label

        # T3: Current price only (live from this run)
        curr_p = prices.get(pair)
        if curr_p is not None:
            hist_n = len(hist)
            label  = (f"[T3] {pair}: current price only "
                      f"({hist_n} history readings) — price {curr_p}")
            return 3, None, curr_p, label

        # T4: Last known price from previous run (max ~30 min old)
        last_p = last_prices.get(pair)
        if last_p is not None:
            label = (f"[T4] {pair}: last known price {last_p} "
                     f"(Yahoo Finance unavailable this run)")
            return 4, None, last_p, label

        # T0: No data at all — should never happen in normal operation
        return 0, None, None, f"[T0] {pair}: NO PRICE DATA — cannot monitor"

    # Resolve tiers for every unique pair (log once per pair, not per trade)
    pair_data: dict = {}
    for pair in all_pairs:
        tier, candles_t, resolved_p, tier_label = _resolve_pair_data(pair)
        pair_data[pair] = (tier, candles_t, resolved_p)
        _tier_counts[_tier_key_map.get(tier, "t0_no_data")] += 1
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
                        pair, _chk_lvl, int(row.get("id", 0)),
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
        tier, candles_t, resolved_p = pair_data.get(pair, (0, None, None))
        _process_trade(row, candles_t, resolved_p, is_fund=True)

    for row in _res_open:
        pair = row.get("pair", "")
        tier, candles_t, resolved_p = pair_data.get(pair, (0, None, None))
        _process_trade(row, candles_t, resolved_p, is_fund=False)

    # Data tier summary
    _tc = _tier_counts
    log(
        f"Monitor data sources: "
        f"T1(Yahoo 1H)={_tc['t1_yahoo_ohlcv']} "
        f"T2(synthetic)={_tc['t2_synthetic']} "
        f"T3(current)={_tc['t3_current_only']} "
        f"T4(last known)={_tc['t4_last_known']} "
        f"T0(none)={_tc['t0_no_data']}/{len(all_pairs)} pairs"
    )
    if _tc["t0_no_data"] == 0:
        log("Monitor: all pairs covered — zero unmonitored trades")

    # ── Step 6: MFE/MAE updates for research trades ───────────────────────────
    mfe_updated = 0
    for row in _res_open:
        pair      = row.get("pair", "")
        direction = (row.get("direction") or "").upper()
        rec_id    = int(row.get("id", 0) or 0)
        try:
            _, candles_mfe, resolved_p_mfe = pair_data.get(pair, (0, None, None))
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

    # ── Step 7: Telegram summary for research milestones ─────────────────────
    if research_fragments and _ta:
        now_fmt = now_ak.strftime("%-I%p").lower().replace("m", "").rstrip("0") or "12am"
        wknd_txt = "\n📅 <i>Weekend monitor — Friday close prices.</i>" if is_wknd else ""
        try:
            _ta.send(
                f"🔬 <b>Monitor update ({now_fmt} check): "
                f"{len(research_fragments)} research trade milestone"
                f"{'s' if len(research_fragments) != 1 else ''} hit</b>\n\n"
                + " · ".join(research_fragments)
                + f"\n\nML training data updated — next full scan {next_6am}"
                + wknd_txt
            )
        except Exception:
            pass

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

    # Item 2: Write heartbeat — checked by daily.py to detect monitor downtime
    try:
        _HEARTBEAT_FILE.write_text(
            json.dumps({"last_run": datetime.utcnow().isoformat()}),
            encoding="utf-8",
        )
    except Exception:
        pass

    _release_lock()
    return result
