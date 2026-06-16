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
]

OUTCOME_STATUSES = {"WIN", "LOSS", "BREAKEVEN", "EXPIRED", "PARTIAL_WIN"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load() -> list:
    if not RESEARCH_TRADES_CSV.exists():
        return []
    with RESEARCH_TRADES_CSV.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_all(rows: list) -> None:
    with RESEARCH_TRADES_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDS})


def _next_id(rows: list) -> int:
    ids = [int(r["id"]) for r in rows if str(r.get("id", "")).isdigit()]
    return (max(ids) + 1) if ids else 1


def _pip_size(pair: str) -> float:
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


def log_research_trade(pair: str, parsed: dict, source: str, scan_mode: str,
                       extra_fields: dict = None) -> int:
    """Append one research trade; return its assigned id.

    source: 'sonnet'     — Sonnet-confirmed, entry/stop/target from Claude.
            'indicative' — Haiku-scored with ATR-derived levels; status = OPEN.
            'haiku'      — Legacy haiku-only with no levels; status = NO_PRICE_LEVELS.

    extra_fields: optional dict with any of the extended fields (score breakdown,
    entry context, etc.). Keys must match FIELDS entries.
    """
    rows   = load()
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
        "mfe_pips":    "0" if has_levels else "",
        "mae_pips":    "0" if has_levels else "",
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
                   exit_reason: str = "") -> dict:
    status = status.upper()
    if status not in OUTCOME_STATUSES:
        raise ValueError(f"status must be one of {sorted(OUTCOME_STATUSES)}")

    rows = load()
    target = next((r for r in rows if str(r.get("id")) == str(rec_id)), None)
    if target is None:
        raise ValueError(f"no research trade with id {rec_id}")

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
        if status == "WIN":
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
    _write_all(rows)
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
    _write_all(rows)
    return target
