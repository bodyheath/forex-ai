"""Automatic daily outcome detection for open trade recommendations.

Fetches the live price for every OPEN trade from Twelve Data, then:
  1. For each open trade, check if price crossed target (WIN at 2R) or
     stop_loss (LOSS at -1R).  Stop never moves — pure TP-or-SL.
  2. Close any trade that has hit target, stop, or expired (5+ days).

Returns the list of closed trade row dicts so the caller can immediately
run win/loss analysis on them.
"""

import sys
import time
from datetime import datetime, timezone

import requests

import config
from src import tracker
from src import cascade as _casc


def _online_learn(updated: dict) -> None:
    """Feed a closed fund trade to the online learner (best-effort)."""
    try:
        from src import online_learner as _ol
        _ol.partial_fit_trade(
            "main",
            updated.get("id"),
            updated.get("status", ""),
            updated.get("closed_at", ""),
        )
    except Exception as exc:
        print(f"[outcome_checker] ML training error (main trade {updated.get('id')}): {exc}", file=sys.stderr)

_PRICE_URL   = "https://api.twelvedata.com/price"
_EXPIRY_DAYS = 5    # fallback; actual expiry is computed from R:R
_FETCH_DELAY = 10   # seconds between price calls; free tier = 8 req/min


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _compute_expiry_days(row: dict) -> int:
    """Dynamic expiry from R:R: max(4, round(rr * 1.5) + 1)."""
    try:
        entry  = float(row.get("entry")     or 0)
        stop   = float(row.get("stop_loss") or 0)
        target = float(row.get("target")    or 0)
        sd = abs(entry - stop)
        td = abs(entry - target)
        if sd <= 0:
            return _EXPIRY_DAYS
        rr = td / sd
        return max(4, round(rr * 1.5) + 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return _EXPIRY_DAYS


def _fetch_live_price(pair: str) -> float | None:
    """Call Twelve Data /price (no cache — must be fresh for outcome checking)."""
    try:
        resp = requests.get(
            _PRICE_URL,
            params={"symbol": pair, "apikey": config.TWELVE_DATA_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "error":
            return None
        return float(data["price"])
    except Exception:
        return None


def _determine_outcome(direction: str, price: float,
                       entry, stop, target, opened_at: str,
                       expiry_days: int = None, as_of: datetime = None) -> str | None:
    """Return 'WIN', 'LOSS', 'EXPIRED', or None (still open).

    as_of: clock to evaluate expiry against; defaults to real now() (live use).
    Passed explicitly by the virtual outcome simulator to evaluate expiry
    against a historical bar timestamp instead of the real clock."""
    expiry = expiry_days if expiry_days is not None else _EXPIRY_DAYS
    _now = as_of if as_of is not None else datetime.now(timezone.utc)
    try:
        opened = datetime.strptime(opened_at[:19], "%Y-%m-%d %H:%M:%S")
        if (_now.replace(tzinfo=None) - opened).days >= expiry:
            return "EXPIRED"
    except (ValueError, TypeError):
        pass

    entry  = _to_float(entry)
    stop   = _to_float(stop)
    target = _to_float(target)

    if direction not in ("BUY", "SELL") or None in (entry, stop, target, price):
        return None

    if direction == "BUY":
        if price >= target:
            return "WIN"
        if price <= stop:
            return "LOSS"
    else:
        if price <= target:
            return "WIN"
        if price >= stop:
            return "LOSS"
    return None


def check_virtual_trades(log=print, price_cache: dict | None = None) -> int:
    """Record virtual outcomes for NO_TRADE and SKIPPED setups to accelerate ML training.

    These are setups the AI analyzed but didn't enter (no-trade decision or system block).
    Tracking whether they would have worked gives the ML free training signal without
    risking real money.  Runs after check_open_trades so pairs already in the price
    cache cost no extra API calls.

    Returns count of new virtual outcomes recorded this run.
    """
    from datetime import timedelta

    rows = tracker.load()
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10)

    candidates = []
    for r in rows:
        st = str(r.get("status", "")).upper()
        if st not in ("NO_TRADE", "SKIPPED"):
            continue
        if "VIRTUAL_OUTCOME:" in str(r.get("notes", "")):
            continue
        if str(r.get("direction", "")).upper() not in ("BUY", "SELL"):
            continue
        e = _to_float(r.get("entry"))
        s = _to_float(r.get("stop_loss"))
        t = _to_float(r.get("target"))
        if not all([e, s, t]) or e == 0 or s == 0 or t == 0:
            continue
        try:
            ts = datetime.strptime(str(r.get("timestamp", ""))[:19], "%Y-%m-%d %H:%M:%S")
            if ts < cutoff:
                continue
        except (ValueError, TypeError):
            continue
        candidates.append(r)

    if not candidates:
        return 0

    log(f"Virtual outcome check: {len(candidates)} unresolved NO_TRADE/SKIPPED setup(s).")
    recorded = 0
    _last_api_t = 0.0

    for row in candidates:
        rec_id    = int(row.get("id", 0))
        pair      = row.get("pair", "")
        direction = str(row.get("direction", "")).upper()

        try:
            price = (price_cache or {}).get(pair)
            if price is None:
                _elapsed = time.time() - _last_api_t
                if _last_api_t > 0 and _elapsed < _FETCH_DELAY:
                    time.sleep(_FETCH_DELAY - _elapsed)
                price = _fetch_live_price(pair)
                _last_api_t = time.time()
            if price is None:
                continue

            e = _to_float(row.get("entry"))
            s = _to_float(row.get("stop_loss"))
            t = _to_float(row.get("target"))

            virtual_outcome = None
            if direction == "BUY":
                if price >= t:
                    virtual_outcome = "WIN"
                elif price <= s:
                    virtual_outcome = "LOSS"
            else:
                if price <= t:
                    virtual_outcome = "WIN"
                elif price >= s:
                    virtual_outcome = "LOSS"

            if virtual_outcome is None:
                continue  # setup still in play

            # Mark as processed so we don't reprocess next run
            existing = str(row.get("notes", ""))
            new_notes = f"VIRTUAL_OUTCOME:{virtual_outcome} | {existing}".rstrip(" |")
            tracker.update_fields(rec_id, notes=new_notes)

            # Feed into online learner — succeeds if feature store has an entry for this trade
            try:
                from src import online_learner as _ol
                _ol.partial_fit_trade(
                    "main", rec_id, virtual_outcome,
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                )
            except Exception:
                pass

            log(f"  Virtual #{rec_id} {pair} {direction}: {virtual_outcome} "
                f"(entry={e:.5f}, stop={s:.5f}, target={t:.5f}, price={price:.5f})")
            recorded += 1

        except Exception as exc:
            log(f"  Virtual #{rec_id} {pair}: virtual check error — {exc}")

    if recorded:
        log(f"Virtual outcome check: {recorded} new virtual outcome(s) fed to ML.")
    return recorded


def check_open_trades(log=print, price_cache: dict | None = None) -> list:
    """Check all OPEN trades; close any that hit target/stop/expiry.

    Workflow:
      1. For each open trade, fetch current price (cache or live API).
      2. Check cascade milestones: target_hit → WIN, stop_hit → LOSS.
      3. Also run _determine_outcome for expiry check.

    Returns a list of updated trade row dicts for each trade closed this run.
    """
    if not config.TWELVE_DATA_KEY:
        log("Outcome check: TWELVE_DATA_KEY not set — skipping.")
        return []

    rows = tracker.load()
    open_trades = [r for r in rows if r.get("status") == "OPEN"
                   and r.get("trade_this") == "YES"]

    if not open_trades:
        log("Outcome check: no open YES-trades to monitor.")
        return []

    log(f"Outcome check: monitoring {len(open_trades)} open trade(s).")
    closed = []
    _last_api_t = 0.0

    # ── Step 1: build price cache for all open trades ─────────────────────
    full_cache = dict(price_cache or {})
    for row in open_trades:
        pair = row.get("pair", "")
        if pair not in full_cache:
            _elapsed = time.time() - _last_api_t
            if _last_api_t > 0 and _elapsed < _FETCH_DELAY:
                time.sleep(_FETCH_DELAY - _elapsed)
            price = _fetch_live_price(pair)
            _last_api_t = time.time()
            if price is not None:
                full_cache[pair] = price

    # ── Step 2: cascade + outcome determination + close ──────────────────
    try:
        from src import telegram_alert as _ta_casc
    except Exception:
        _ta_casc = None

    for row in open_trades:
        rec_id    = int(row.get("id", 0))
        pair      = row.get("pair", "")
        direction = (row.get("direction") or "").upper()

        try:
            price = full_cache.get(pair)
            if price is None:
                log(f"  #{rec_id} {pair}: price unavailable, skipping.")
                continue

            # ── TARGET: initialise t2_price (target level) if not yet stored ──
            if not _to_float(row.get("t2_price")):
                try:
                    _ct1, _ct2, _ct3 = _casc.compute_levels(
                        row.get("entry"), row.get("stop_loss"),
                        row.get("target"), direction,
                    )
                    if _ct2 is not None:
                        tracker.update_fields(
                            rec_id,
                            t2_price=_ct2,
                            effective_stop=row.get("stop_loss"),
                        )
                        row.update({
                            "t2_price": _ct2,
                            "effective_stop": row.get("stop_loss"),
                        })
                except Exception:
                    pass

            # ── TARGET / STOP: check milestones ──────────────────────────────
            _closed_this = False

            if _casc.target_hit(row, price):
                _t2p = _casc.pips_at(row.get("entry"), row.get("t2_price"), pair, direction)
                tracker.update_fields(rec_id, t2_hit="TRUE", t2_hit_price=price, t2_hit_pips=_t2p)
                row.update({"t2_hit": "TRUE", "t2_hit_price": price, "t2_hit_pips": _t2p})
                _cp = _to_float(row.get("t2_price")) or price
                updated = tracker.update_outcome(
                    rec_id, "WIN",
                    exit_price=_cp,
                    notes=f"Auto-closed: WIN at {_cp} (+{_t2p:.1f}p, 2R target)",
                )
                r_txt = f", R={updated.get('r_multiple')}, pips={updated.get('pips')}"
                log(f"  #{rec_id} {pair} {direction}: WIN at {_cp} (+{_t2p:.1f}p){r_txt} | latest_conf={updated.get('latest_conf') or '—'}")
                if _ta_casc:
                    try:
                        _ta_casc.send(
                            f"🎯 <b>{pair} — 2R profit target hit — WIN</b>\n\n"
                            f"Full position closed at +{_t2p:.1f} pips (+2R).\n\n"
                            f"Direction: {direction}  |  Exit: {_cp}"
                        )
                    except Exception:
                        pass
                closed.append(updated)
                _online_learn(updated)
                _closed_this = True

            elif _casc.stop_hit(row, price):
                _cp = _to_float(row.get("stop_loss")) or price
                updated = tracker.update_outcome(
                    rec_id, "LOSS",
                    exit_price=_cp,
                    notes=f"Auto-closed: LOSS at {_cp} (stop_loss hit)",
                )
                r_txt = f", R={updated.get('r_multiple')}, pips={updated.get('pips')}"
                log(f"  #{rec_id} {pair} {direction}: LOSS at {_cp}{r_txt} | latest_conf={updated.get('latest_conf') or '—'}")
                closed.append(updated)
                _online_learn(updated)
                _closed_this = True

            if _closed_this:
                continue

            # ── EXPIRY CHECK ─────────────────────────────────────────────────
            _base_exp = _compute_expiry_days(row)
            _ext_exp  = _casc.expiry_extension(row, _base_exp)

            outcome = _determine_outcome(
                direction, price,
                row.get("entry"), row.get("stop_loss"), row.get("target"),
                row.get("timestamp", ""),
                expiry_days=_ext_exp,
            )
            if outcome is None:
                continue  # still open

            # Use canonical level price (not live) to avoid overshoot distortion
            if outcome == "WIN":
                _final_exit = _to_float(row.get("target")) or price
            elif outcome == "LOSS":
                _final_exit = _to_float(row.get("stop_loss")) or price
            else:
                _final_exit = price

            _notes = f"Auto-closed: {outcome} at {_final_exit}"
            updated = tracker.update_outcome(
                rec_id, outcome,
                exit_price=_final_exit,
                notes=_notes,
            )
            r_txt = f", R={updated.get('r_multiple')}, pips={updated.get('pips')}"
            log(f"  #{rec_id} {pair} {direction}: {outcome} at {price}{r_txt} | latest_conf={updated.get('latest_conf') or '—'}")
            closed.append(updated)
            _online_learn(updated)

        except Exception as exc:
            log(f"  #{rec_id} {pair}: outcome check error — {exc}")

    wins    = sum(1 for r in closed if r.get("status") == "WIN")
    losses  = sum(1 for r in closed if r.get("status") == "LOSS")
    expired = sum(1 for r in closed if r.get("status") == "EXPIRED")
    if closed:
        log(f"Outcome check complete: {wins} WIN, {losses} LOSS, {expired} EXPIRED.")
    else:
        log("Outcome check: no trades hit target/stop today.")
    try:
        check_virtual_trades(log, price_cache=full_cache)
    except Exception as _virt_exc:
        log(f"  Virtual outcome check error — {_virt_exc}")
    return closed
