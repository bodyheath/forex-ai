"""Research trade tracker — paper trades for all pairs scoring confidence >= 5.

Tracks at 0.01 lots for reference only (not real money). Outcome checking runs
automatically like real trades: WIN when target hit, LOSS when stop hit, EXPIRED
after 14 days.

Source values:
  'sonnet'     — Sonnet-confirmed setup; entry/stop/target from Claude's analysis.
  'indicative' — Haiku-scored pair (conf 5-6) with ATR-derived price levels
                 computed by _calc_indicative_levels. Fully trackable for outcomes.
  'haiku'      — Legacy: Haiku-only with no price levels at all (NO_PRICE_LEVELS).
                 No longer written by current code but may exist in historical data.

Used by research_analyst.py for the 30-day threshold study: were conf-6 pairs
profitable enough to warrant lowering the live-trade threshold from 7 to 6?
"""

import csv
import os
import tempfile
from datetime import datetime

import config

RESEARCH_TRADES_CSV = config.DATA_DIR / "research_trades.csv"
RESEARCH_LOTS = 0.01

# Fields are append-only for backward compatibility — new fields go at the end.
FIELDS = [
    # ── Core (original fields) ──────────────────────────────────────────────
    "id", "date", "pair", "direction", "confidence",
    "entry", "stop_loss", "target", "lots",
    "status", "close_price", "pips", "net_pips", "r_multiple", "closed_at",
    "source", "scan_mode",
    # ── Score breakdown at entry ────────────────────────────────────────────
    "tech_score", "fund_score", "sent_score", "pos_score", "macro_score",
    "mtf_count", "cot_momentum", "fundamental_alignment", "fund_aligned_count",
    "grade", "ribbon_state", "divergence_type",
    # ── Entry quality & market context ─────────────────────────────────────
    "rsi_at_entry", "bb_position", "price_vs_200ma",
    "vix_at_entry", "dxy_direction", "market_regime",
    "patience_score_at_entry", "day_of_week", "hour_auckland",
    "corr_agreement_count",
    "atr_percentile_6m",       # current ATR vs 6-month average (empty when unavailable)
    "consecutive_weeks_trending",  # weeks current weekly trend has held (empty when unavailable)
    # ── MFE / MAE (updated each scan while OPEN) ────────────────────────────
    "mfe_pips",   # maximum favourable excursion — furthest price moved in trade direction
    "mae_pips",   # maximum adverse excursion — furthest price moved against trade direction
    # ── Exit reason (set at close) ──────────────────────────────────────────
    # Values: TARGET_HIT | STOP_HIT | EXPIRED_PROFITABLE | EXPIRED_LOSS |
    #         EXPIRED_NEUTRAL | PARTIAL_WIN
    "exit_reason",
    # ── Post-close tracking (updated 5 days after close) ───────────────────
    "post_close_target_reached",  # true/false — did price reach original target after close?
    "post_close_max_move_pips",   # max price move in original trade direction after close
    "post_close_reversal_pips",   # max reversal against original direction after close
    "post_close_checked_at",      # timestamp of last post-close check
    # ── Candle quality at entry ─────────────────────────────────────────────
    "entry_candle_type",          # PIN_BAR | ENGULFING_BULL | ENGULFING_BEAR | INSIDE_BAR | NORMAL
    "entry_candle_body_ratio",    # body / total range (0.0–1.0)
    "entry_candle_vs_avg",        # entry candle range vs prior 10-candle average (ratio)
    # ── Price context at entry ──────────────────────────────────────────────
    "dist_weekly_open_pips",      # pips from Monday weekly open (large = possible exhaustion)
    "dist_round_number_pips",     # pips from nearest 50-pip round-number level
    "new_20d_extreme",            # true/false — new 20-day high or low in last 3 candles
    "inside_bars_before",         # consecutive inside bars immediately before entry (compression)
    # ── COT context at entry ────────────────────────────────────────────────
    "cot_weeks_in_direction",     # consecutive weeks COT positioned in trade direction
    "cot_accelerating",           # 1=building, -1=fading, 0=stable
    # ── Intermarket context at entry ───────────────────────────────────────
    "us10y_direction",            # US 10Y yield trend: RISING | FALLING | FLAT
    "gold_direction",             # Gold price trend: RISING | FALLING | FLAT
    "sp500_direction",            # S&P 500 trend: RISING | FALLING | FLAT
    "vix_vs_20d_avg",             # 1=elevated, -1=suppressed, 0=flat/unknown
    # ── Volatility context at entry ─────────────────────────────────────────
    "atr_5d_vs_20d",              # 5-day ATR / 20-day ATR (>1.0 = expanding volatility)
    "atr_expanding",              # 1 if ATR increasing 3+ consecutive days, else 0
    # ── Enhanced post-close tracking ───────────────────────────────────────
    "days_to_target_after_close", # days from trade close to target being hit (blank if not hit)
    "price_at_expiry_momentum",   # price move direction at expiry: UP | DOWN | FLAT
    "entry_level_revisited",      # true/false — did price revisit entry within 5 days after close?
    # ── Currency strength at entry ──────────────────────────────────────────
    "base_currency_strength",     # -100 to +100 strength score for base currency at entry
    "quote_currency_strength",    # -100 to +100 strength score for quote currency at entry
    # ── Monthly trend filter ─────────────────────────────────────────────────
    "monthly_trend",              # BUY | SELL | NEUTRAL — monthly candle direction at entry
    "monthly_trend_aligned",      # 1=aligned with monthly trend, 0=counter-trend, 0.5=neutral
    # ── HHHL trend structure filter ──────────────────────────────────────────
    "hhhl_aligned",               # 1=structure confirms direction, 0=broken structure, 0.5=no data
    # ── Kill zone timing ─────────────────────────────────────────────────────
    "kill_zone_entry",            # LONDON | NEW_YORK | OVERLAP | TOKYO | LONDON_CLOSE | OUTSIDE
    # ── Market structure break ────────────────────────────────────────────────
    "market_structure_break",     # BULLISH_BREAK | BEARISH_BREAK | CONTINUATION
    # ── RSI divergence ────────────────────────────────────────────────────────
    "rsi_divergence_type",        # BULLISH | BEARISH | HIDDEN_BULLISH | HIDDEN_BEARISH | NONE
    # ── Pre-trade checklist ───────────────────────────────────────────────────
    "checklist_score",            # 0-10 integer: number of criteria passed
    # ── Adaptive sizing at entry (fund_state context) ─────────────────────────
    "sizing_mode",                # normal | win_streak | drawdown_caution | drawdown_protection
    "consecutive_wins_at_entry",  # fund win streak when trade was opened
    "drawdown_pct_at_entry",      # fund drawdown % from peak at time of entry
    "position_size_pct_at_entry", # adaptive risk % that would have been applied
    # ── Dynamic threshold at entry ───────────────────────────────────────────
    "threshold_at_entry",             # dynamic confidence threshold active when trade was opened
    "regime_base_at_entry",           # regime component of the threshold
    "win_rate_adjustment_at_entry",   # win rate component (negative = lowered threshold)
    "data_quality_adjustment_at_entry", # data quality component
    # ── Adaptive cascade targets ──────────────────────────────────────────────
    "t1_was_adaptive",            # TRUE when pair-specific MFE data was used
    "t1_target_atr_multiple",     # actual T1 multiplier applied (0.4 standard)
    "t2_target_atr_multiple",     # actual T2 multiplier applied (0.7 standard)
    "t3_target_atr_multiple",     # actual T3 multiplier applied (1.0 standard)
    "volatility_tier_at_entry",  # VERY_QUIET | QUIET | NORMAL | VOLATILE | VERY_VOLATILE
    "atr_percentile_at_entry",   # atr_percentile_6m ratio used for tier (1.0 = normal)
    # ── Cascading targets (append-only) ──────────────────────────────────────
    "t1_price",                   # entry ± 0.4× ATR
    "t2_price",                   # entry ± 0.7× ATR
    "t3_price",                   # existing target (= 1.0× ATR for research trades)
    "t1_hit",                     # TRUE / FALSE
    "t1_hit_price",               # live price when T1 was triggered
    "t1_hit_pips",                # pip profit on 40% closed at T1
    "t2_hit",                     # TRUE / FALSE
    "t2_hit_price",
    "t2_hit_pips",                # pip profit on 30% closed at T2
    "t3_hit",                     # TRUE / FALSE
    "t3_hit_price",
    "t3_hit_pips",                # pip profit on final 30% at T3
    "effective_stop",             # starts as stop_loss; moves to entry after T1 hit
    "cascading_total_pips",       # sum of pips across all hit portions
    "cascading_total_pips_weighted",  # 0.4×t1 + 0.3×t2 + 0.3×t3
    "system_version",             # v1 | v2 — used to filter ML training to v2-only
    # ── Administrative ───────────────────────────────────────────────────────
    "notes",                      # free-text; e.g. duplicate reason, manual override note
]

OUTCOME_STATUSES = {"WIN", "LOSS", "BREAKEVEN", "EXPIRED", "PARTIAL_WIN", "FULL_WIN", "STALE_EXIT", "SKIPPED"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load() -> list:
    if not RESEARCH_TRADES_CSV.exists():
        return []
    with RESEARCH_TRADES_CSV.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_all(rows: list) -> None:
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=RESEARCH_TRADES_CSV.parent, suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in FIELDS})
        os.replace(tmp_path, RESEARCH_TRADES_CSV)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _next_id(rows: list) -> int:
    ids = [int(r["id"]) for r in rows if str(r.get("id", "")).isdigit()]
    return (max(ids) + 1) if ids else 1


def _pip_size(pair: str) -> float:
    """Return the pip size for any forex pair based on the QUOTE currency.

    Comprehensive rule covering all 130+ eligible pairs:
      quote JPY  → 0.01     e.g. USD/JPY, EUR/JPY, AUD/JPY, NOK/JPY, HKD/JPY
      base  JPY  → 0.000001 e.g. JPY/USD, JPY/AUD  (rare inverted pairs)
      else  → 0.0001        ALL other pairs including:
                            standard:  EUR/USD, GBP/USD, USD/CAD, AUD/USD …
                            HKD quote: USD/HKD (≈7.8), CAD/HKD (≈5.6), AUD/HKD …
                            SGD quote: USD/SGD (≈1.34), EUR/SGD, AUD/SGD …
                            NOK quote: USD/NOK (≈9.5), EUR/NOK (≈11), GBP/NOK …
                            SEK quote: USD/SEK (≈10.5), EUR/SEK (≈12), GBP/SEK …
                            DKK/MXN/ZAR/TRY quote: all use 0.0001
    Note: HKD/SGD/NOK/SEK pairs all use 4-decimal quoting (0.0001 per pip)
    regardless of their price level — only JPY pairs use 2-decimal quoting.
    """
    cleaned = pair.upper().replace("/", "").replace("-", "")
    if len(cleaned) >= 6:
        base  = cleaned[:3]
        quote = cleaned[3:6]
        if quote == "JPY":
            return 0.01
        if base == "JPY":
            return 0.000001
    elif "JPY" in pair.upper():
        return 0.01
    return 0.0001


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def validate_pip_size(pair: str, pips: float, direction: str) -> str | None:
    """Return a warning string if abs(pips) > 500 on non-JPY pairs (unexpected large move).

    NOK/HKD pip_size is 0.0001 which produces large pip counts — this is mathematically
    correct but worth flagging for review when the count is extreme.
    """
    try:
        quote = pair.upper().replace("/", "")
        is_jpy = (len(quote) >= 6 and quote[3:6] == "JPY") or "JPY" in pair.upper()
        if not is_jpy and abs(float(pips)) > 500:
            return (
                f"VALIDATION NOTE: {pair} {direction} closed with {pips:+.0f} pips — "
                f"value >500 pips on non-JPY pair; verify pip_size=0.0001 is correct for this pair."
            )
    except Exception:
        pass
    return None


def check_inverse_open_research(pair: str, direction: str) -> str | None:
    """Return a warning string if an open research trade is the inverse of this pair/direction.

    "AUD/CAD" BUY is equivalent to "CAD/AUD" SELL — same directional bet.
    Returns warning string if such an inverse trade is already open, None otherwise.
    """
    try:
        cleaned = pair.upper().replace("/", "")
        if len(cleaned) < 6:
            return None
        inv_pair = cleaned[3:6] + "/" + cleaned[:3]
        inv_dir  = "SELL" if direction.upper() == "BUY" else "BUY"
        rows = load()
        for row in rows:
            if row.get("status") != "OPEN":
                continue
            rp = (row.get("pair") or "").upper().replace("/", "")
            rd = (row.get("direction") or "").upper()
            rp_slash = rp[:3] + "/" + rp[3:6] if len(rp) >= 6 else rp
            if rp_slash.upper() == inv_pair.upper() and rd == inv_dir:
                return (
                    f"WARNING: Inverse pair conflict — {inv_pair} {inv_dir} already open "
                    f"(same directional bet as {pair} {direction.upper()}). "
                    f"Avoid doubling research exposure."
                )
    except Exception:
        pass
    return None


def log_research_trade(pair: str, parsed: dict, source: str, scan_mode: str,
                       extra_fields: dict = None) -> int:
    """Append one research trade; return its assigned id.

    source: 'sonnet'     — Sonnet-confirmed, entry/stop/target from Claude.
            'indicative' — Haiku-scored with ATR-derived levels; status = OPEN.
            'haiku'      — Legacy haiku-only with no levels; status = NO_PRICE_LEVELS.

    extra_fields: optional dict with any of the extended fields (score breakdown,
    entry context, etc.). Keys must match FIELDS entries.
    """
    rows = load()

    # Duplicate guard: if an OPEN trade already exists for this pair+direction today,
    # skip silently.  Each scan mode (prelondon/full/morning) sees the same market;
    # logging the same directional signal 2-3× per day inflates open counts and WR.
    today = datetime.now().strftime("%Y-%m-%d")
    _direction_norm = (parsed.get("direction") or "").upper()
    for _r in rows:
        if (
            _r.get("status") == "OPEN"
            and _r.get("pair", "").upper() == pair.upper()
            and (_r.get("direction") or "").upper() == _direction_norm
            and (_r.get("date") or "")[:10] == today
        ):
            return 0  # duplicate — caller should not count this as a logged trade

    rec_id = _next_id(rows)

    entry = parsed.get("entry")
    stop  = parsed.get("stop_loss")
    tgt   = parsed.get("target")

    has_levels = all(_to_float(v) is not None for v in (entry, stop, tgt))
    status = "OPEN" if has_levels else "NO_PRICE_LEVELS"

    row: dict = {
        "id":          rec_id,
        "date":        _now()[:10],
        "pair":        pair,
        "direction":   (parsed.get("direction") or "").upper(),
        "confidence":  parsed.get("confidence") or "",
        "entry":       entry or "",
        "stop_loss":   stop or "",
        "target":      tgt or "",
        "lots":        RESEARCH_LOTS,
        "status":      status,
        "close_price": "",
        "pips":        "",
        "net_pips":    "",
        "r_multiple":  "",
        "closed_at":   "",
        "source":      source,
        "scan_mode":   scan_mode,
        # MFE/MAE start at 0 for OPEN trades
        "mfe_pips":       "0" if has_levels else "",
        "mae_pips":       "0" if has_levels else "",
        "system_version": config.SYSTEM_VERSION,
    }

    # Merge in any extra context fields
    if extra_fields:
        for k, v in extra_fields.items():
            if k in FIELDS:
                row[k] = v

    rows.append(row)
    _write_all(rows)
    return rec_id


def _compute_result(row: dict, status: str, close_price) -> tuple:
    """Return (r_multiple, pips) for a closed research trade."""
    entry     = _to_float(row.get("entry"))
    stop      = _to_float(row.get("stop_loss"))
    target    = _to_float(row.get("target"))
    direction = (row.get("direction") or "").upper()

    if status == "BREAKEVEN":
        return 0.0, 0.0
    if status in ("EXPIRED", "PARTIAL_WIN") and _to_float(close_price) is None:
        return "", ""

    close_price = _to_float(close_price)
    if close_price is None:
        close_price = target if status == "WIN" else stop
    if entry is None or stop is None or close_price is None or direction not in ("BUY", "SELL"):
        return "", ""

    risk = abs(entry - stop)
    if risk == 0:
        return "", ""
    profit     = (close_price - entry) if direction == "BUY" else (entry - close_price)
    r_multiple = round(profit / risk, 2)
    pips       = round(profit / _pip_size(row.get("pair", "")), 1)
    return r_multiple, pips


def update_outcome(rec_id: int, status: str, close_price=None,
                   exit_reason: str = "", cascading_pips=None) -> dict:
    status = status.upper()
    if status not in OUTCOME_STATUSES:
        raise ValueError(f"status must be one of {sorted(OUTCOME_STATUSES)}")

    rows = load()
    target = next((r for r in rows if str(r.get("id")) == str(rec_id)), None)
    if target is None:
        raise ValueError(f"no research trade with id {rec_id}")

    if cascading_pips is not None and status in ("FULL_WIN", "WIN", "PARTIAL_WIN"):
        pips   = round(float(cascading_pips), 1)
        ps     = _pip_size(target.get("pair", ""))
        entry  = _to_float(target.get("entry"))
        stop   = _to_float(target.get("stop_loss"))
        risk   = abs(entry - stop) if entry is not None and stop is not None else 0
        r_mult = round((pips * ps) / risk, 2) if risk > 0 and ps > 0 else ""
    else:
        r_mult, pips = _compute_result(target, status, close_price)

    net_pips = pips
    if pips not in ("", None):
        try:
            from src import trade_costs as _tc
            lots    = _to_float(target.get("lots")) or RESEARCH_LOTS
            opened  = datetime.strptime(target.get("date", "")[:10], "%Y-%m-%d")
            days_held = max(0.0, (datetime.now() - opened).total_seconds() / 86400)
            net_pips = _tc.net_pips_for_closed_trade(
                target.get("pair", ""),
                target.get("direction", ""),
                _to_float(target.get("entry")) or 1.0,
                float(pips),
                days_held,
            )
        except Exception:
            pass

    # Derive exit_reason from status if not explicitly provided
    if not exit_reason:
        if status in ("WIN", "FULL_WIN"):
            exit_reason = "TARGET_HIT"
        elif status == "LOSS":
            exit_reason = "STOP_HIT"
        elif status == "PARTIAL_WIN":
            exit_reason = "PARTIAL_WIN"
        elif status in ("EXPIRED", "BREAKEVEN"):
            try:
                pips_f = float(pips) if pips not in ("", None) else 0.0
            except (TypeError, ValueError):
                pips_f = 0.0
            if pips_f > 0:
                exit_reason = "EXPIRED_PROFITABLE"
            elif pips_f < 0:
                exit_reason = "EXPIRED_LOSS"
            else:
                exit_reason = "EXPIRED_NEUTRAL"

    target["status"]      = status
    target["close_price"] = close_price if close_price is not None else ""
    target["r_multiple"]  = r_mult
    target["pips"]        = pips
    target["net_pips"]    = net_pips
    target["closed_at"]   = _now()
    target["exit_reason"] = exit_reason

    # Record momentum direction at expiry for EXPIRED / PARTIAL_WIN trades
    if status in ("EXPIRED", "PARTIAL_WIN") and close_price is not None:
        try:
            cp_f   = _to_float(close_price)
            entry_f = _to_float(target.get("entry"))
            dirn   = (target.get("direction") or "").upper()
            if cp_f is not None and entry_f is not None and dirn in ("BUY", "SELL"):
                profit = (cp_f - entry_f) if dirn == "BUY" else (entry_f - cp_f)
                if profit > 0:
                    target["price_at_expiry_momentum"] = "UP"
                elif profit < 0:
                    target["price_at_expiry_momentum"] = "DOWN"
                else:
                    target["price_at_expiry_momentum"] = "FLAT"
        except Exception:
            pass

    _write_all(rows)

    # Validate pip size — log warning for unexpectedly large pip counts
    if pips not in ("", None):
        try:
            _pip_warn = validate_pip_size(
                target.get("pair", ""), float(pips), target.get("direction", "")
            )
            if _pip_warn:
                import sys as _sys
                print(f"[research_tracker] {_pip_warn}", file=_sys.stderr)
        except Exception:
            pass

    return target


def update_mfe_mae(rec_id: int, current_price: float) -> dict:
    """Update mfe_pips and mae_pips for an open trade based on current price.

    MFE = max favourable excursion (max move in trade direction, pips).
    MAE = max adverse excursion (max move against trade direction, pips).

    Both are non-negative; 0 means price has never moved in that direction.
    """
    rows   = load()
    target = next((r for r in rows if str(r.get("id")) == str(rec_id)), None)
    if target is None:
        return {}

    entry     = _to_float(target.get("entry"))
    direction = (target.get("direction") or "").upper()
    if entry is None or direction not in ("BUY", "SELL"):
        return target

    ps   = _pip_size(target.get("pair", ""))
    move = (current_price - entry) if direction == "BUY" else (entry - current_price)

    cur_mfe = _to_float(target.get("mfe_pips")) or 0.0
    cur_mae = _to_float(target.get("mae_pips")) or 0.0

    new_mfe = max(cur_mfe, round(max(0.0, move) / ps, 1))
    new_mae = max(cur_mae, round(max(0.0, -move) / ps, 1))

    if new_mfe != cur_mfe or new_mae != cur_mae:
        target["mfe_pips"] = new_mfe
        target["mae_pips"] = new_mae
        _write_all(rows)

    return target


def update_fields(rec_id: int, **kwargs) -> None:
    """Update arbitrary fields on an open research trade without changing its status.

    Only updates keys that exist in FIELDS.  Used by cascade milestone tracking
    to record T1/T2/T3 hits and move the effective_stop without closing the trade.
    """
    rows = load()
    target = next((r for r in rows if str(r.get("id")) == str(rec_id)), None)
    if target is None:
        return
    for k, v in kwargs.items():
        if k in FIELDS:
            target[k] = v
    _write_all(rows)


def update_post_close(rec_id: int, current_price: float) -> dict:
    """Update post-close tracking fields for a recently closed trade.

    Checks whether price reached the original target after the trade closed,
    records the max move and max reversal since close. Call this for 5 days
    after a trade closes to calibrate stop/target placement.
    """
    rows   = load()
    target = next((r for r in rows if str(r.get("id")) == str(rec_id)), None)
    if target is None:
        return {}

    entry     = _to_float(target.get("entry"))
    tgt_price = _to_float(target.get("target"))
    direction = (target.get("direction") or "").upper()
    if entry is None or tgt_price is None or direction not in ("BUY", "SELL"):
        return target

    ps = _pip_size(target.get("pair", ""))

    # Move since original entry in trade direction
    move_from_entry = (
        (current_price - entry) if direction == "BUY" else (entry - current_price)
    )
    move_pips = round(move_from_entry / ps, 1)

    # Did price reach original target?
    target_reached = (
        (current_price >= tgt_price) if direction == "BUY"
        else (current_price <= tgt_price)
    )

    # Max move in trade direction vs close_price
    close_p = _to_float(target.get("close_price"))
    if close_p is not None:
        move_since_close = (
            (current_price - close_p) if direction == "BUY"
            else (close_p - current_price)
        )
        max_move  = round(max(0.0,  move_since_close) / ps, 1)
        max_rev   = round(max(0.0, -move_since_close) / ps, 1)
    else:
        max_move = max_rev = 0.0

    # Update if improvement
    prev_max  = _to_float(target.get("post_close_max_move_pips")) or 0.0
    prev_rev  = _to_float(target.get("post_close_reversal_pips")) or 0.0

    target["post_close_target_reached"] = "true" if target_reached else "false"
    target["post_close_max_move_pips"]  = max(prev_max, max_move)
    target["post_close_reversal_pips"]  = max(prev_rev, max_rev)
    target["post_close_checked_at"]     = _now()

    # days_to_target_after_close: set (once) when target is first reached
    if target_reached and not target.get("days_to_target_after_close"):
        closed_at_str = target.get("closed_at") or ""
        try:
            closed_dt   = datetime.strptime(closed_at_str[:19], "%Y-%m-%d %H:%M:%S")
            days_to_tgt = round((datetime.now() - closed_dt).total_seconds() / 86400, 1)
            target["days_to_target_after_close"] = days_to_tgt
        except Exception:
            pass

    # entry_level_revisited: true if price comes back within 5 pips of entry after close
    if entry is not None:
        entry_revisit_threshold = 5 * ps
        if abs(current_price - entry) <= entry_revisit_threshold:
            target["entry_level_revisited"] = "true"
        elif not target.get("entry_level_revisited"):
            target["entry_level_revisited"] = "false"

    _write_all(rows)
    return target
