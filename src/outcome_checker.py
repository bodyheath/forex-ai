"""Automatic daily outcome detection for open trade recommendations.

Fetches the live price for every OPEN trade from Twelve Data, then:
  1. For each open trade, check if price crossed target (WIN at 2R) or
     stop_loss (LOSS at -1R).  Stop never moves — pure TP-or-SL.
  2. Close any trade that has hit target, stop, or expired (5+ days).

Returns the list of closed trade row dicts so the caller can immediately
run win/loss analysis on them.
"""

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
    except Exception:
        pass

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
                       expiry_days: int = None,
                       breakeven_protected: bool = False) -> str | None:
    """Return 'WIN', 'LOSS', 'BREAKEVEN', 'EXPIRED', or None (still open).

    breakeven_protected=True means the stop has been moved to the breakeven
    level by partial_profit_checker.  When that stop is hit, the outcome is
    BREAKEVEN (not LOSS) — the trade closes at entry price, no loss recorded.
    """
    expiry = expiry_days if expiry_days is not None else _EXPIRY_DAYS
    try:
        opened = datetime.strptime(opened_at[:19], "%Y-%m-%d %H:%M:%S")
        if (datetime.now(timezone.utc).replace(tzinfo=None) - opened).days >= expiry:
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
            return "BREAKEVEN" if breakeven_protected else "LOSS"
    else:
        if price <= target:
            return "WIN"
        if price >= stop:
            return "BREAKEVEN" if breakeven_protected else "LOSS"
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
      2. Run partial_profit_checker.run() — handles stage 1 (breakeven stop
         migration) and stage 2 (50% partial close recording + alert).
      3. Determine outcome using the effective stop (breakeven if stage 1+).
      4. For stage-2 trades, blend the partial close price with the final
         exit price so recorded pips reflect both tranches.
      5. Write the outcome to tracker (trades.csv).

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

    # ── Step 2: partial profit milestones (stage 1 / stage 2) ────────────
    try:
        from src import partial_profit_checker as _ppc
        _ppc.run(open_trades, full_cache, log)
        _pp_state = _ppc.load_state()
    except Exception as exc:
        log(f"  Partial profit checker error — {exc}")
        _ppc = None
        _pp_state = {}

    # ── Step 3: cascade + outcome determination + close ───────────────────
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

            # ── CASCADE: initialise levels if not yet stored ─────────────────
            if not _to_float(row.get("t1_price")):
                try:
                    _ct1, _ct2, _ct3 = _casc.compute_levels(
                        row.get("entry"), row.get("stop_loss"),
                        row.get("target"), direction,
                    )
                    if _ct1 is not None:
                        tracker.update_fields(
                            rec_id,
                            t1_price=_ct1, t2_price=_ct2, t3_price=_ct3,
                            effective_stop=row.get("stop_loss"),
                        )
                        row.update({
                            "t1_price": _ct1, "t2_price": _ct2, "t3_price": _ct3,
                            "effective_stop": row.get("stop_loss"),
                        })
                except Exception:
                    pass

            # ── CASCADE: check milestones (greedy) ───────────────────────────
            _closed_this = False

            if _casc.t1_hit(row, price):
                _t1p = _casc.pips_at(row.get("entry"), row.get("t1_price"), pair, direction)
                tracker.update_fields(
                    rec_id, t1_hit="TRUE", t1_hit_price=price,
                    t1_hit_pips=_t1p, effective_stop=row.get("entry"),
                )
                row.update({
                    "t1_hit": "TRUE", "t1_hit_price": price,
                    "t1_hit_pips": _t1p, "effective_stop": row.get("entry"),
                })
                log(f"  #{rec_id} {pair}: T1 hit at {price} (+{_t1p:.1f}p) — stop at breakeven")
                if _ta_casc:
                    try:
                        _ta_casc.send(
                            f"✅ <b>{pair} — T1 target hit (+{_t1p:.1f} pips)</b>\n\n"
                            f"First target reached — 35% of position banked.\n"
                            f"Stop loss moved to breakeven — no loss possible now.\n\n"
                            f"Direction: {direction}  |  Price: {price}\n"
                            f"Remaining 65% running toward T2 and T3."
                        )
                    except Exception:
                        pass

            if _casc.t2_hit(row, price):
                _t2p = _casc.pips_at(row.get("entry"), row.get("t2_price"), pair, direction)
                tracker.update_fields(rec_id, t2_hit="TRUE", t2_hit_price=price, t2_hit_pips=_t2p)
                row.update({"t2_hit": "TRUE", "t2_hit_price": price, "t2_hit_pips": _t2p})
                log(f"  #{rec_id} {pair}: T2 hit at {price} (+{_t2p:.1f}p) — 70% banked")
                if _ta_casc:
                    try:
                        _ta_casc.send(
                            f"💰 <b>{pair} — T2 target hit (+{_t2p:.1f} pips)</b>\n\n"
                            f"Second target reached — 70% of position banked.\n"
                            f"Final 30% running to full target with stop trailed to T1.\n\n"
                            f"Direction: {direction}  |  Price: {price}\n"
                            f"Worst case: final tranche closes at T1 (+1R locked)."
                        )
                    except Exception:
                        pass

            if _casc.t3_hit(row, price):
                _t3p = _casc.pips_at(
                    row.get("entry"),
                    row.get("t3_price") or row.get("target"),
                    pair, direction,
                )
                tracker.update_fields(
                    rec_id, t3_hit="TRUE", t3_hit_price=price, t3_hit_pips=_t3p,
                )
                row.update({"t3_hit": "TRUE", "t3_hit_price": price, "t3_hit_pips": _t3p})
                _wp = _casc.weighted_pips(row)
                _tp = _casc.total_pips(row)
                tracker.update_fields(
                    rec_id,
                    cascading_total_pips=_tp,
                    cascading_total_pips_weighted=_wp,
                )
                _cp = _to_float(row.get("t3_price") or row.get("target"))
                _notes_fw = f"FULL_WIN: T1(+{_to_float(row.get('t1_hit_pips')) or 0:.0f}p) T2(+{_to_float(row.get('t2_hit_pips')) or 0:.0f}p) T3(+{_t3p:.0f}p) = {_wp:.1f}p weighted"
                updated = tracker.update_outcome(
                    rec_id, "FULL_WIN",
                    exit_price=_cp,
                    notes=_notes_fw,
                    cascading_pips=_wp,
                )
                log(f"  #{rec_id} {pair} {direction}: FULL_WIN — {_wp:.1f}p weighted")
                if _ta_casc:
                    try:
                        _ta_casc.send(
                            f"🎯 <b>{pair} — FULL WIN — all three targets hit!</b>\n\n"
                            f"T1 +{_to_float(row.get('t1_hit_pips')) or 0:.1f}p (35%)  "
                            f"T2 +{_to_float(row.get('t2_hit_pips')) or 0:.1f}p (35%)  "
                            f"T3 +{_t3p:.1f}p (30%)\n"
                            f"Weighted total: +{_wp:.1f} pips\n\n"
                            f"Direction: {direction}  |  Final price: {price}"
                        )
                    except Exception:
                        pass
                closed.append(updated)
                _online_learn(updated)
                _closed_this = True

            elif _casc.effective_stop_hit(row, price):
                _casc_oc = _casc.cascade_outcome(row)
                _wp      = _casc.weighted_pips(row) if _casc_oc != "LOSS" else None
                _tp      = _casc.total_pips(row)    if _casc_oc != "LOSS" else None
                if _wp:
                    tracker.update_fields(
                        rec_id,
                        cascading_total_pips=_tp,
                        cascading_total_pips_weighted=_wp,
                    )
                _cp      = _to_float(row.get("effective_stop") or row.get("stop_loss"))
                _notes_c = f"Auto-closed: {_casc_oc} at {_cp}"
                if _wp:
                    _notes_c += f" | cascade: {_wp:.1f}p weighted"
                updated  = tracker.update_outcome(
                    rec_id, _casc_oc,
                    exit_price=_cp,
                    notes=_notes_c,
                    cascading_pips=_wp if _wp else None,
                )
                r_txt = f", R={updated.get('r_multiple')}, pips={updated.get('pips')}"
                log(f"  #{rec_id} {pair} {direction}: {_casc_oc} at {_cp}{r_txt}")
                closed.append(updated)
                _online_learn(updated)
                _closed_this = True

            if _closed_this:
                continue

            # ── Partial profit checker (breakeven stop migration) ────────────
            _eff_stop = row.get("stop_loss")
            _pp_stage = 0
            if _ppc is not None:
                _pp_stage = _ppc.get_stage(str(rec_id), _pp_state)
                _eff_stop = _ppc.effective_stop(str(rec_id), row.get("stop_loss"), _pp_state)

            _bp_protected = _pp_stage >= 1

            # ── EXPIRY CHECK ─────────────────────────────────────────────────
            _base_exp = _compute_expiry_days(row)
            _ext_exp  = _casc.expiry_extension(row, _base_exp)

            outcome = _determine_outcome(
                direction, price,
                row.get("entry"), _eff_stop, row.get("target"),
                row.get("timestamp", ""),
                expiry_days=_ext_exp,
                breakeven_protected=_bp_protected,
            )
            if outcome is None:
                continue  # still open

            # If expiring but cascade T1/T2 already banked, honour cascade state
            if outcome == "EXPIRED":
                _casc_oc = _casc.cascade_outcome(row)
                if _casc_oc != "LOSS":
                    outcome = _casc_oc

            # Stage 2 + breakeven stop hit → partial profit already locked, WIN
            if outcome == "BREAKEVEN" and _pp_stage >= 2:
                outcome = "WIN"

            # Blended exit price for stage-2 trades
            _final_exit = price
            _partial_note = ""
            if _ppc is not None and _pp_stage >= 2:
                _final_exit = _ppc.blended_exit_price(str(rec_id), price, _pp_state)
                _partial_price = _pp_state.get(str(rec_id), {}).get("partial_close_price", "")
                _partial_pips  = _pp_state.get(str(rec_id), {}).get("partial_close_pips", 0)
                if _partial_price:
                    _partial_note = (
                        f" | Partial close: 50% at {_partial_price} "
                        f"(+{_partial_pips:.1f}p), 50% at {price} "
                        f"(blended {_final_exit:.5f})"
                    )
            else:
                # Use the canonical level (target/stop) not the live price to
                # avoid recording pips that include overshoot past the level.
                if outcome == "WIN":
                    _final_exit = _to_float(row.get("target")) or price
                elif outcome in ("LOSS", "BREAKEVEN"):
                    _final_exit = _to_float(row.get("effective_stop") or row.get("stop_loss")) or price

            _notes = f"Auto-closed: {outcome} at {_final_exit}{_partial_note}"
            updated = tracker.update_outcome(
                rec_id, outcome,
                exit_price=_final_exit,
                notes=_notes,
            )
            r_txt = f", R={updated.get('r_multiple')}, pips={updated.get('pips')}"
            log(f"  #{rec_id} {pair} {direction}: {outcome} at {price}{r_txt}")
            closed.append(updated)
            _online_learn(updated)

        except Exception as exc:
            log(f"  #{rec_id} {pair}: outcome check error — {exc}")

    wins      = sum(1 for r in closed if r.get("status") == "WIN")
    full_wins = sum(1 for r in closed if r.get("status") == "FULL_WIN")
    partial   = sum(1 for r in closed if r.get("status") == "PARTIAL_WIN")
    losses    = sum(1 for r in closed if r.get("status") == "LOSS")
    beven     = sum(1 for r in closed if r.get("status") == "BREAKEVEN")
    expired   = sum(1 for r in closed if r.get("status") == "EXPIRED")
    if closed:
        log(f"Outcome check complete: {wins} WIN, {full_wins} FULL_WIN, "
            f"{partial} PARTIAL_WIN, {losses} LOSS, "
            f"{beven} BREAKEVEN, {expired} EXPIRED.")
    else:
        log("Outcome check: no trades hit target/stop today.")
    try:
        check_virtual_trades(log, price_cache=full_cache)
    except Exception as _virt_exc:
        log(f"  Virtual outcome check error — {_virt_exc}")
    return closed
