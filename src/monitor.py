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

_PRICE_URL    = "https://api.twelvedata.com/price"
_OHLCV_URL    = "https://api.twelvedata.com/time_series"
_MONITOR_LOG  = config.DATA_DIR / "monitor_log.json"
_MILESTONE_LOG = config.DATA_DIR / "milestone_log.json"
_LOCK_FILE    = config.DATA_DIR / "monitor.lock"
_API_USAGE    = config.DATA_DIR / "api_usage.json"
_OHLCV_CANDLES = 6          # hourly candles per pair (Yahoo Finance 1H)
_API_BUDGET_LIMIT = 700     # daily TD call threshold (for batch price fetch logging)
_MONITOR_HISTORY = config.DATA_DIR / "monitor_history.json"
_BATCH_SIZE   = 20          # pairs per batch price request
_FETCH_TIMEOUT = 15         # seconds per HTTP request
_HOT_THRESHOLD  = 0.70      # price ≥ 70% of way to target → HOT
_WARM_THRESHOLD = 0.40      # price ≥ 40% of way to target → WARM
_DEDUP_HOURS  = 24          # suppress duplicate alerts within this window
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
        history["runs"].append({
            "ts":                result.get("timestamp", ""),
            "milestones":        len(result.get("milestones_hit", [])),
            "hot":               result.get("hot_zone_count", 0),
            "warm":              result.get("warm_zone_count", 0),
            "td_calls":          result.get("api_calls_used", 0),
            "yf_ohlcv_pairs":    result.get("yf_ohlcv_pairs", 0),
            "approaching_alerts": result.get("approaching_alerts", 0),
            "skipped":           result.get("skipped_reason", ""),
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
    n_runs   = len(active)
    n_ms     = sum(r.get("milestones", 0) for r in active)
    n_hot    = sum(r.get("approaching_alerts", 0) for r in active)
    yf_total = sum(r.get("yf_ohlcv_pairs", 0) for r in active)
    td_total = sum(r.get("td_calls", 0) for r in active)
    # Each Yahoo 1H fetch replaces one TD time_series call
    td_saved = yf_total

    return (
        f"Monitor weekly report (last 7 days)\n"
        f"  Runs completed:    {n_runs}\n"
        f"  Milestones hit:    {n_ms}\n"
        f"  HOT alerts sent:   {n_hot}\n"
        f"  Yahoo OHLCV total: {yf_total} pair-fetches\n"
        f"  TD calls used:     {td_total} (price only)\n"
        f"  TD calls saved:    {td_saved} (replaced by Yahoo Finance)"
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
            break   # trade closed

    return closed_rows


def _apply_research_milestones(row: dict, milestones: list, row_state: dict,
                               log=print) -> tuple:
    """Apply detected milestones to research_trades.csv.

    Returns (closed_row_or_None, summary_fragment_str).
    summary_fragment_str is appended to the batch Telegram message.
    """
    from src import research_tracker as _rt
    rec_id    = int(row.get("id", 0))
    pair      = row.get("pair", "")
    direction = (row.get("direction") or "").upper()

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

    # ── API budget check (informational — OHLCV uses Yahoo Finance, not TD) ────
    calls_today = _get_td_calls_today()
    if calls_today >= _API_BUDGET_LIMIT:
        log(
            f"Monitor: TD API at {calls_today}/800 calls today — "
            f"OHLCV uses Yahoo Finance so batch price fetch will still proceed"
        )

    # ── Step 1: Batch price fetch ─────────────────────────────────────────────
    all_open    = _fund_open + _res_open
    _fund_pairs = sorted({r.get("pair", "") for r in _fund_open if r.get("pair")})
    _res_pairs  = sorted({r.get("pair", "") for r in _res_open  if r.get("pair")})
    all_pairs   = sorted({r.get("pair", "") for r in all_open   if r.get("pair")})
    log(
        f"Monitor: building price fetch list — "
        f"{len(_fund_pairs)} fund pair(s) + {len(_res_pairs)} research pair(s) = "
        f"{len(_fund_pairs) + len(_res_pairs)} total (after dedup: {len(all_pairs)} unique pairs to fetch)"
    )
    prices, batch_calls = _batch_price_fetch(all_pairs, log=log)
    result["api_calls_used"] += batch_calls
    log(f"Monitor: fetched prices for {len(prices)}/{len(all_pairs)} pairs in {batch_calls} API call(s).")

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

    # ── Detect newly-HOT trades and send early-warning alert (2d) ────────────
    current_hot_keys: set = set()
    for row in fund_zones["HOT"] + res_zones["HOT"]:
        current_hot_keys.add(f"{row.get('pair', '')}#{row.get('id', '')}")

    newly_hot = current_hot_keys - previously_hot
    result["approaching_alerts"] = len(newly_hot)
    for key in sorted(newly_hot):
        _pair_str = key.split("#")[0]
        log(f"  Monitor: {_pair_str} newly entered HOT zone — sending early warning")
        if _ta:
            try:
                _ta.send(
                    f"\U0001f525 <b>{_pair_str} approaching target</b>\n\n"
                    f"Trade has entered the HOT zone (≥70% progress to next cascade level).\n"
                    f"No action needed — monitoring continues automatically.\n\n"
                    f"Watch for milestone alert in the next few runs."
                )
            except Exception:
                pass

    # ── Step 3: Yahoo Finance 1H OHLCV for ALL open trades ────────────────────
    from src.yahoo_finance import fetch_1h_candles as _yf_1h
    candle_map: dict = {}   # pair → list of candle dicts (newest-first)
    yf_ok = 0
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
        f"Monitor: Yahoo 1H OHLCV fetched for {yf_ok}/{len(all_pairs)} pairs — "
        f"0 TD OHLCV calls used"
    )

    # ── Step 3b: pip movement check vs last run (2c) ──────────────────────────
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

    for pair in all_pairs:
        curr = prices.get(pair)
        prev = last_prices.get(pair)
        if curr is None or prev is None:
            continue
        cands = candle_map.get(pair, [])
        if not cands:
            continue
        atr = _quick_atr(cands)
        if atr is None or atr <= 0:
            continue
        pf       = _pip_factor(pair)
        pip_move = abs(curr - prev) / pf
        atr_pips = atr / pf
        ratio    = pip_move / atr_pips if atr_pips > 0 else 0
        if ratio >= 0.3:
            log(
                f"  Monitor: {pair} moved {pip_move:.1f} pips since last run "
                f"({ratio:.2f}x ATR={atr_pips:.1f}p)"
            )

    # ── Step 4+5: Detect milestones and apply cascade updates ─────────────────

    fund_closed: list = []
    research_fragments: list = []

    def _process_trade(row, candles, is_fund: bool):
        pair      = row.get("pair", "")
        direction = (row.get("direction") or "").upper()
        price     = prices.get(pair)

        if price is None:
            return

        # Fast-path: all cascade targets already recorded in CSV — nothing to do
        if _is_true(row.get("t3_hit")):
            log(f"  Monitor: {pair} #{row.get('id')} all targets already recorded — skipping")
            return

        # Check milestone log for any levels already sent, log them for each hit level
        for _chk_lvl in ("T1", "T2", "T3"):
            _field_map = {"T1": "t1_hit", "T2": "t2_hit", "T3": "t3_hit"}
            if _is_true(row.get(_field_map[_chk_lvl])):
                _prev = _check_milestone_sent(pair, _chk_lvl)
                if not _prev:
                    pass   # already in CSV but not in dedup log — log it now
                    _record_milestone_sent(
                        pair, _chk_lvl, int(row.get("id", 0)),
                        trade_type="fund" if is_fund else "research",
                    )

        # Detect milestones using candle data (HOT/WARM) or spot price (COLD)
        if candles:
            milestones, row_state = _detect_candle_milestones(row, candles, pair, log=log)
        else:
            milestones, row_state = _detect_spot_milestones(row, price, pair)

        if not milestones:
            return

        # Pre-filter milestones that were already sent via the dedup log.
        # The _apply_*_milestones functions do the same check, but doing it here
        # provides an early log line before any CSV writes happen.
        _dedup_filtered = []
        for _m in milestones:
            _lvl = _m["level"]
            if _lvl in ("T1", "T2", "T3"):
                _prev_ts = _check_milestone_sent(pair, _lvl)
                if _prev_ts:
                    log(
                        f"  Monitor: {pair} #{row.get('id')} {_lvl} already recorded — "
                        f"skipping duplicate — no Telegram sent (last sent {_prev_ts})"
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
                row, milestones, row_state, log=log,
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
        pair    = row.get("pair", "")
        candles = candle_map.get(pair, [])
        _process_trade(row, candles, is_fund=True)

    for row in _res_open:
        pair    = row.get("pair", "")
        candles = candle_map.get(pair, [])
        _process_trade(row, candles, is_fund=False)

    # ── Step 6: MFE/MAE updates for research trades ───────────────────────────
    mfe_updated = 0
    for row in _res_open:
        pair      = row.get("pair", "")
        direction = (row.get("direction") or "").upper()
        rec_id    = int(row.get("id", 0) or 0)
        try:
            candles = candle_map.get(pair, [])
            if candles:
                if _update_mfe_mae_from_ohlcv(rec_id, direction, candles):
                    mfe_updated += 1
            else:
                # COLD zone: update from current spot price only
                p = prices.get(pair)
                if p is not None:
                    _rt.update_mfe_mae(rec_id, p)
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
    _release_lock()
    return result
