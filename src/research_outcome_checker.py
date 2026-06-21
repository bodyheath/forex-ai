"""Automatic outcome detection for open research trades.

Mirrors outcome_checker.py but operates on research_trades.csv rather than
trades.csv.  Only processes trades with status=OPEN (i.e., those that had
entry/stop/target from Sonnet analysis).  NO_PRICE_LEVELS trades are skipped.
14-day expiry rule is identical to the real trade checker.

MFE/MAE tracking: on every scan price fetch for open trades, mfe_pips and
mae_pips are updated whenever the trade moves to a new extreme.

Post-close tracking: 5 days after a trade closes the pair price is still
monitored to record whether price reached the original target, the max move
since close, and the max reversal.  This calibrates stop/target placement.
"""

import time
from datetime import datetime, timedelta

import requests

import config
from src import research_tracker
from src import cascade as _casc


def _online_learn(updated: dict) -> None:
    """Feed a closed research trade to the online learner (best-effort)."""
    try:
        from src import online_learner as _ol
        _ol.partial_fit_trade(
            "research",
            updated.get("id"),
            updated.get("status", ""),
            updated.get("closed_at", ""),
        )
    except Exception:
        pass

_PRICE_URL         = "https://api.twelvedata.com/price"
_EXPIRY_DAYS       = 7      # fallback; actual expiry is computed from R:R
_FETCH_DELAY       = 10     # seconds between calls; free tier = 8 req/min
_POST_CLOSE_DAYS   = 5      # track for this many days after a trade closes


def _compute_expiry_days(row: dict) -> int:
    """Volatility-based expiry:
      - High volatility  (atr_percentile_6m > 1.2) → 5 days  (trade resolves faster)
      - Low volatility   (atr_percentile_6m < 0.8) → 10 days (trade needs more time)
      - Normal volatility                           → 7 days

    Falls back to R:R-based formula for rows where atr_percentile_6m is missing,
    and ultimately to _EXPIRY_DAYS=7 if levels are missing too.
    """
    try:
        atr_pct = row.get("atr_percentile_6m")
        if atr_pct not in (None, ""):
            atr_val = float(atr_pct)
            if atr_val > 1.2:
                return 5
            if atr_val < 0.8:
                return 10
            return 7
    except (TypeError, ValueError):
        pass

    # Fallback: R:R-based formula
    try:
        entry  = float(row.get("entry")     or 0)
        stop   = float(row.get("stop_loss") or 0)
        target = float(row.get("target")    or 0)
        sd = abs(entry - stop)
        td = abs(entry - target)
        if sd <= 0:
            return _EXPIRY_DAYS
        rr = td / sd
        return max(7, round(rr * 2))
    except (TypeError, ValueError, ZeroDivisionError):
        return _EXPIRY_DAYS


def _is_partial_win(direction: str, price: float, entry, stop, target) -> bool:
    """Return True if price is >=50% of the way to target AND gross pips positive.

    Used to reclassify EXPIRED trades that were moving well but ran out of time.
    """
    e = _to_float(entry)
    t = _to_float(target)
    if None in (e, t, price) or direction not in ("BUY", "SELL"):
        return False
    reward = abs(t - e)
    if reward <= 0:
        return False
    if direction == "BUY":
        gross_pips = price - e
        pct_to_target = (price - e) / reward
    else:
        gross_pips = e - price
        pct_to_target = (e - price) / reward
    return gross_pips > 0 and pct_to_target >= 0.5


def _reclassify_expired(rows: list, log) -> int:
    """Promote EXPIRED rows to PARTIAL_WIN if they meet the criteria.

    Runs once per daily check against all historical EXPIRED trades so that
    the new classification is retroactively applied.  Returns number reclassified.
    """
    count = 0
    for row in rows:
        if row.get("status") != "EXPIRED":
            continue
        cp = _to_float(row.get("close_price"))
        if cp is None:
            continue
        direction = (row.get("direction") or "").upper()
        if _is_partial_win(direction, cp, row.get("entry"), row.get("stop_loss"), row.get("target")):
            try:
                research_tracker.update_outcome(int(row.get("id", 0)), "PARTIAL_WIN", close_price=cp)
                log(f"  Reclassified #{row.get('id')} {row.get('pair')} EXPIRED → PARTIAL_WIN")
                count += 1
            except Exception as exc:
                log(f"  Reclassify error #{row.get('id')}: {exc}")
    return count


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _fetch_live_price(pair: str):
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
                       expiry_days: int = None):
    expiry = expiry_days if expiry_days is not None else _EXPIRY_DAYS
    try:
        opened = datetime.strptime(opened_at[:10], "%Y-%m-%d")
        if (datetime.now() - opened).days >= expiry:
            if _is_partial_win(direction, price, entry, stop, target):
                return "PARTIAL_WIN"
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


def check_open_research_trades(log=print, price_cache: dict | None = None) -> list:
    """Check all OPEN research trades; close any that hit target/stop/expiry.

    Also updates mfe_pips and mae_pips for each OPEN trade based on the
    current price — this records the maximum excursion profile for ML training.

    ``price_cache`` is an optional {pair: float} dict pre-fetched by
    price_fetcher.fetch_prices_for_open_trades().  When a pair's price is
    present in the cache the direct API call and rate-limit sleep are both
    skipped, allowing ALL open research trades to be checked regardless of
    whether they appeared in today's scan.  Falls back to the live /price
    endpoint for any cache misses.

    Returns list of closed trade row dicts.  Fully fault-tolerant.
    """
    if not config.TWELVE_DATA_KEY:
        log("Research outcome check: TWELVE_DATA_KEY not set — skipping.")
        return []

    rows = research_tracker.load()

    # ── Retroactively reclassify EXPIRED → PARTIAL_WIN ───────────────────────
    reclassified = _reclassify_expired(rows, log)
    if reclassified:
        rows = research_tracker.load()  # reload after mutations

    open_trades = [r for r in rows if r.get("status") == "OPEN"]
    no_levels   = [r for r in rows if r.get("status") == "NO_PRICE_LEVELS"]
    closed_hist = [r for r in rows if r.get("status") in research_tracker.OUTCOME_STATUSES]

    # ── Audit log — visible in every GitHub Actions run ──────────────────────
    wins_hist      = sum(1 for r in closed_hist if r.get("status") == "WIN")
    full_win_hist  = sum(1 for r in closed_hist if r.get("status") == "FULL_WIN")
    losses_hist    = sum(1 for r in closed_hist if r.get("status") == "LOSS")
    expired_hist   = sum(1 for r in closed_hist if r.get("status") == "EXPIRED")
    partial_hist   = sum(1 for r in closed_hist if r.get("status") == "PARTIAL_WIN")
    log(
        f"Research outcome audit: {len(rows)} total trades — "
        f"{len(open_trades)} OPEN · {len(no_levels)} NO_PRICE_LEVELS · "
        f"{wins_hist} WIN · {full_win_hist} FULL_WIN · {losses_hist} LOSS · "
        f"{expired_hist} EXPIRED · {partial_hist} PARTIAL_WIN"
    )

    if not open_trades:
        log("Research outcome check: no open research trades to monitor.")
        return []

    n_cached  = sum(1 for r in open_trades if price_cache and r.get("pair") in price_cache)
    n_api     = len(open_trades) - n_cached
    log(
        f"Research outcome check: monitoring {len(open_trades)} open research trade(s) — "
        f"{n_cached} from price cache · {n_api} via direct API."
    )
    closed = []
    _last_api_t = 0.0  # timestamp of the last direct API call (for rate limiting)

    for row in open_trades:
        rec_id    = int(row.get("id", 0))
        pair      = row.get("pair", "")
        direction = (row.get("direction") or "").upper()

        try:
            if price_cache and pair in price_cache:
                price = price_cache[pair]
            else:
                # Rate-limit guard: ensure ≥ _FETCH_DELAY between live API calls
                _elapsed = time.time() - _last_api_t
                if _last_api_t > 0 and _elapsed < _FETCH_DELAY:
                    time.sleep(_FETCH_DELAY - _elapsed)
                price = _fetch_live_price(pair)
                _last_api_t = time.time()
            if price is None:
                log(f"  Research #{rec_id} {pair}: price unavailable, skipping.")
                continue

            # ── MFE/MAE update (always, before determining outcome) ──────────
            try:
                research_tracker.update_mfe_mae(rec_id, price)
            except Exception:
                pass

            # ── CASCADE: initialise levels if not yet stored ─────────────────
            if not _to_float(row.get("t1_price")):
                try:
                    _ct1, _ct2, _ct3 = _casc.compute_levels(
                        row.get("entry"), row.get("stop_loss"),
                        row.get("target"), direction,
                    )
                    if _ct1 is not None:
                        research_tracker.update_fields(
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

            # ── CASCADE: check milestones (greedy — all in same scan) ────────
            _closed_this = False

            if _casc.t1_hit(row, price):
                _t1p = _casc.pips_at(row.get("entry"), row.get("t1_price"), pair, direction)
                research_tracker.update_fields(
                    rec_id, t1_hit="TRUE", t1_hit_price=price,
                    t1_hit_pips=_t1p, effective_stop=row.get("entry"),
                )
                row.update({
                    "t1_hit": "TRUE", "t1_hit_price": price,
                    "t1_hit_pips": _t1p, "effective_stop": row.get("entry"),
                })
                log(f"  Research #{rec_id} {pair}: T1 hit at {price} (+{_t1p:.1f}p) — stop at breakeven")

            if _casc.t2_hit(row, price):
                _t2p = _casc.pips_at(row.get("entry"), row.get("t2_price"), pair, direction)
                research_tracker.update_fields(
                    rec_id, t2_hit="TRUE", t2_hit_price=price, t2_hit_pips=_t2p,
                )
                row.update({"t2_hit": "TRUE", "t2_hit_price": price, "t2_hit_pips": _t2p})
                log(f"  Research #{rec_id} {pair}: T2 hit at {price} (+{_t2p:.1f}p) — 70% banked")

            if _casc.t3_hit(row, price):
                _t3p = _casc.pips_at(
                    row.get("entry"),
                    row.get("t3_price") or row.get("target"),
                    pair, direction,
                )
                research_tracker.update_fields(
                    rec_id, t3_hit="TRUE", t3_hit_price=price, t3_hit_pips=_t3p,
                )
                row.update({"t3_hit": "TRUE", "t3_hit_price": price, "t3_hit_pips": _t3p})
                _wp = _casc.weighted_pips(row)
                _tp = _casc.total_pips(row)
                research_tracker.update_fields(
                    rec_id,
                    cascading_total_pips=_tp,
                    cascading_total_pips_weighted=_wp,
                )
                _cp = _to_float(row.get("t3_price") or row.get("target"))
                updated = research_tracker.update_outcome(
                    rec_id, "FULL_WIN",
                    close_price=_cp,
                    exit_reason="TARGET_HIT",
                    cascading_pips=_wp,
                )
                log(
                    f"  Research #{rec_id} {pair} {direction}: FULL_WIN at {price} "
                    f"(weighted {_wp:.1f}p) "
                    f"[MFE={updated.get('mfe_pips', '?')}p]"
                )
                closed.append(updated)
                _online_learn(updated)
                _closed_this = True

            elif _casc.effective_stop_hit(row, price):
                _casc_oc = _casc.cascade_outcome(row)
                _wp      = _casc.weighted_pips(row) if _casc_oc != "LOSS" else None
                _tp      = _casc.total_pips(row)    if _casc_oc != "LOSS" else None
                if _wp:
                    research_tracker.update_fields(
                        rec_id,
                        cascading_total_pips=_tp,
                        cascading_total_pips_weighted=_wp,
                    )
                _cp = _to_float(row.get("effective_stop") or row.get("stop_loss"))
                updated = research_tracker.update_outcome(
                    rec_id, _casc_oc,
                    close_price=_cp,
                    exit_reason="STOP_HIT",
                    cascading_pips=_wp if _wp else None,
                )
                log(
                    f"  Research #{rec_id} {pair} {direction}: {_casc_oc} at {_cp} "
                    f"[MFE={updated.get('mfe_pips', '?')}p MAE={updated.get('mae_pips', '?')}p]"
                )
                closed.append(updated)
                _online_learn(updated)
                _closed_this = True

            if _closed_this:
                continue

            # ── EXPIRY CHECK (extended after T1/T2 milestones) ───────────────
            _base_exp = _compute_expiry_days(row)
            _ext_exp  = _casc.expiry_extension(row, _base_exp)

            outcome = _determine_outcome(
                direction, price,
                row.get("entry"),
                row.get("effective_stop") or row.get("stop_loss"),
                row.get("target"),
                row.get("date", ""),
                expiry_days=_ext_exp,
            )
            if outcome is None:
                continue

            # On expiry: if cascade milestones were reached, use cascade outcome
            if outcome in ("EXPIRED", "PARTIAL_WIN"):
                _casc_oc = _casc.cascade_outcome(row)
                if _casc_oc != "LOSS":
                    _wp = _casc.weighted_pips(row)
                    _tp = _casc.total_pips(row)
                    research_tracker.update_fields(
                        rec_id,
                        cascading_total_pips=_tp,
                        cascading_total_pips_weighted=_wp,
                    )
                    updated = research_tracker.update_outcome(
                        rec_id, _casc_oc,
                        close_price=price,
                        exit_reason="EXPIRED_PROFITABLE",
                        cascading_pips=_wp,
                    )
                    log(
                        f"  Research #{rec_id} {pair} {direction}: "
                        f"expired as {_casc_oc} ({_wp:.1f}p cascade)"
                    )
                    closed.append(updated)
                    continue

            close_recorded = price
            if outcome == "WIN":
                close_recorded = _to_float(row.get("target")) or price
            elif outcome == "LOSS":
                close_recorded = _to_float(row.get("effective_stop") or row.get("stop_loss")) or price
            updated = research_tracker.update_outcome(rec_id, outcome, close_price=close_recorded)
            r_txt   = f", R={updated.get('r_multiple')}, pips={updated.get('pips')}"
            log(
                f"  Research #{rec_id} {pair} {direction}: "
                f"{outcome} at {price}{r_txt} "
                f"[MFE={updated.get('mfe_pips', '?')}p MAE={updated.get('mae_pips', '?')}p"
                f" reason={updated.get('exit_reason', '')}]"
            )
            closed.append(updated)

        except Exception as exc:
            log(f"  Research #{rec_id} {pair}: outcome check error — {exc}")

    wins      = sum(1 for r in closed if r.get("status") == "WIN")
    full_wins = sum(1 for r in closed if r.get("status") == "FULL_WIN")
    losses    = sum(1 for r in closed if r.get("status") == "LOSS")
    expired   = sum(1 for r in closed if r.get("status") == "EXPIRED")
    partial   = sum(1 for r in closed if r.get("status") == "PARTIAL_WIN")
    still_open = len(open_trades) - len(closed)
    if closed:
        log(
            f"Research outcome check complete: closed {len(closed)} trade(s) this run — "
            f"{wins} WIN · {full_wins} FULL_WIN · {losses} LOSS · "
            f"{expired} EXPIRED · {partial} PARTIAL_WIN · {still_open} still open."
        )
    else:
        log(
            f"Research outcome check: no trades hit target/stop/expiry — "
            f"{len(open_trades)} still open."
        )
    return closed


def check_post_close_trades(log=print, price_cache: dict | None = None) -> int:
    """Track pair prices for 5 days after a trade closes.

    Records whether price reached the original target after expiry, the max
    move since close, and the max reversal.  Returns number of trades updated.

    This teaches the ML model whether stops and targets are correctly calibrated
    — a trade that would have hit target 2 days after expiry suggests the expiry
    window should be longer.
    """
    if not config.TWELVE_DATA_KEY:
        return 0

    rows = research_tracker.load()

    # Find closed trades within the last _POST_CLOSE_DAYS days
    cutoff = datetime.now() - timedelta(days=_POST_CLOSE_DAYS)
    post_close = [
        r for r in rows
        if r.get("status") in research_tracker.OUTCOME_STATUSES
        and r.get("closed_at")
        and r.get("entry")  # must have had levels to track
        and r.get("target")
        and _parse_dt(r.get("closed_at", "")) is not None
        and _parse_dt(r.get("closed_at", "")) >= cutoff
    ]

    if not post_close:
        return 0

    log(f"Post-close tracking: {len(post_close)} trade(s) closed within last {_POST_CLOSE_DAYS} days.")
    updated_count = 0
    _last_api_t   = 0.0

    for row in post_close:
        rec_id = int(row.get("id", 0))
        pair   = row.get("pair", "")

        try:
            if price_cache and pair in price_cache:
                price = price_cache[pair]
            else:
                _elapsed = time.time() - _last_api_t
                if _last_api_t > 0 and _elapsed < _FETCH_DELAY:
                    time.sleep(_FETCH_DELAY - _elapsed)
                price = _fetch_live_price(pair)
                _last_api_t = time.time()

            if price is None:
                continue

            research_tracker.update_post_close(rec_id, price)
            updated_count += 1

        except Exception as exc:
            log(f"  Post-close #{rec_id} {pair}: error — {exc}")

    if updated_count:
        log(f"Post-close tracking: updated {updated_count} trade(s).")
    return updated_count


def _parse_dt(s: str):
    """Parse datetime string; return None on failure."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt) + 3], fmt)
        except (ValueError, TypeError):
            continue
    return None
