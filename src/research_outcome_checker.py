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

import sys
import time
from datetime import datetime, timedelta, timezone

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
    except Exception as exc:
        print(f"[research_outcome_checker] ML training error (research trade {updated.get('id')}): {exc}", file=sys.stderr)

_PRICE_URL         = "https://api.twelvedata.com/price"
_EXPIRY_DAYS       = 7      # fallback; actual expiry is computed from R:R
_STALE_EXIT_DAYS   = 21     # hard maximum — close without T1 hit after this many days
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
        if price <= stop:
            return "LOSS"
    else:
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
            except Exception as e:
                log(f"[mfe_mae] {pair} #{rec_id} update failed: {e}")

            # ── TARGET: initialise t2_price if not yet stored ────────────────
            if not _to_float(row.get("t2_price")):
                try:
                    _ct1, _ct2, _ct3 = _casc.compute_levels(
                        row.get("entry"), row.get("stop_loss"),
                        row.get("target"), direction,
                    )
                    if _ct2 is not None:
                        research_tracker.update_fields(
                            rec_id,
                            t2_price=_ct2,
                            effective_stop=row.get("stop_loss"),
                        )
                        row.update({
                            "t2_price": _ct2,
                            "effective_stop": row.get("stop_loss"),
                        })
                    else:
                        log(f"[t2_price] {pair} #{rec_id} compute_levels returned no target "
                            f"(entry={row.get('entry')!r} stop={row.get('stop_loss')!r} "
                            f"direction={direction!r}) — trade cannot register a WIN via target_hit "
                            f"until t2_price is set")
                except Exception as e:
                    log(f"[t2_price] {pair} #{rec_id} init failed: {e} — trade cannot register "
                        f"a WIN via target_hit until t2_price is set")

            # ── TARGET / STOP: check milestones ──────────────────────────────
            _closed_this = False

            if _casc.target_hit(row, price):
                _t2p = _casc.pips_at(row.get("entry"), row.get("t2_price"), pair, direction)
                _cp  = _to_float(row.get("t2_price")) or price
                research_tracker.update_fields(
                    rec_id, t2_hit="TRUE", t2_hit_price=_cp, t2_hit_pips=_t2p,
                )
                row.update({"t2_hit": "TRUE", "t2_hit_price": _cp, "t2_hit_pips": _t2p})
                updated = research_tracker.update_outcome(
                    rec_id, "WIN",
                    close_price=_cp,
                    exit_reason="TARGET_HIT",
                )
                log(
                    f"  Research #{rec_id} {pair} {direction}: WIN at {_cp} "
                    f"(+{_t2p:.1f}p, 2R target) "
                    f"[MFE={updated.get('mfe_pips', '?')}p]"
                )
                closed.append(updated)
                _online_learn(updated)
                _closed_this = True

            elif _casc.stop_hit(row, price):
                _cp = _to_float(row.get("stop_loss")) or price
                updated = research_tracker.update_outcome(
                    rec_id, "LOSS",
                    close_price=_cp,
                    exit_reason="STOP_HIT",
                )
                log(
                    f"  Research #{rec_id} {pair} {direction}: LOSS at {_cp} "
                    f"[MFE={updated.get('mfe_pips', '?')}p MAE={updated.get('mae_pips', '?')}p]"
                )
                closed.append(updated)
                _online_learn(updated)
                _closed_this = True

            if _closed_this:
                continue

            # ── STALE EXIT: hard 21-day maximum if T1 never hit ──────────────
            try:
                _opened_dt  = datetime.strptime(row.get("date", "")[:10], "%Y-%m-%d")
                _days_open  = (datetime.now(timezone.utc).replace(tzinfo=None) - _opened_dt).days
                if _days_open >= _STALE_EXIT_DAYS:
                    updated = research_tracker.update_outcome(
                        rec_id, "STALE_EXIT", close_price=price,
                    )
                    log(f"  Research {pair} #{rec_id} closed as STALE_EXIT — exceeded 21 day maximum")
                    closed.append(updated)
                    _online_learn(updated)
                    continue
            except Exception:
                pass

            # ── EXPIRY CHECK (extended after T1/T2 milestones) ───────────────
            _base_exp = _compute_expiry_days(row)
            _ext_exp  = _casc.expiry_extension(row, _base_exp)

            outcome = _determine_outcome(
                direction, price,
                row.get("entry"),
                row.get("stop_loss"),
                row.get("target"),
                row.get("date", ""),
                expiry_days=_ext_exp,
            )
            if outcome is None:
                continue

            close_recorded = price
            if outcome == "LOSS":
                close_recorded = _to_float(row.get("stop_loss")) or price
            updated = research_tracker.update_outcome(rec_id, outcome, close_price=close_recorded)
            r_txt   = f", R={updated.get('r_multiple')}, pips={updated.get('pips')}"
            log(
                f"  Research #{rec_id} {pair} {direction}: "
                f"{outcome} at {price}{r_txt} "
                f"[MFE={updated.get('mfe_pips', '?')}p MAE={updated.get('mae_pips', '?')}p"
                f" reason={updated.get('exit_reason', '')}]"
            )
            closed.append(updated)
            _online_learn(updated)

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
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=_POST_CLOSE_DAYS)
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
            return datetime.strptime(s[:len(fmt)], fmt)
        except (ValueError, TypeError):
            continue
    return None
