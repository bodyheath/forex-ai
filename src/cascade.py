"""Pure 2:1 risk:reward target system.

Design:
  TARGET = entry ± 2R (2× stop distance) — close 100% → WIN
  STOP   = original stop_loss — LOSS, stop never moves

Trade CSV field mapping:
  t1_price = (unused for new trades; column preserved for historical display)
  t2_price = target (2R) — WIN when crossed, close 100%
  t3_price = (unused)
  t2_hit   = WIN recorded flag

Outcome statuses:
  WIN  — TARGET (2R) reached, 100% position closed, +2R net
  LOSS — stop_loss hit, -1R net

  PARTIAL_WIN — legacy status preserved for historical trades only;
                not produced by new trades under pure TP-or-SL design.

Break-even WR = 33.3%   EV at 55% WR = +0.65R per trade
"""

TARGET_RR = 2.0

# Legacy constants — kept for backward-compatibility with old research trades.
# NOT used for new fund trades (discriminated by t3_price == 0).
_LEGACY_T1_SIZE = 0.50
_LEGACY_T2_SIZE = 0.30
_LEGACY_T3_SIZE = 0.20


def _is_true(val) -> bool:
    """Return True when a CSV boolean field is set."""
    return str(val).strip().upper() in ("TRUE", "1", "YES")


def _pip_size(pair: str) -> float:
    return 0.01 if "JPY" in (pair or "").upper() else 0.0001


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ── Level computation ─────────────────────────────────────────────────────────

def compute_levels(entry, stop_loss, target, direction, atr=None,
                   t1_mult=None, t2_mult=None, t3_mult=None):
    """Return (None, target_level, None).

    target_level = entry ± 2× stop_distance (full WIN close at 2R).
    First and third elements are always None — no T1 trail or T3 in pure TP-or-SL.
    ATR and adaptive multipliers are accepted but ignored.
    """
    e = _to_float(entry)
    s = _to_float(stop_loss)
    if e is None or s is None:
        return None, None, None

    stop_dist = abs(e - s)
    if stop_dist <= 0:
        return None, None, None

    d = (direction or "").upper()
    if d == "BUY":
        t2 = e + stop_dist * TARGET_RR
    elif d == "SELL":
        t2 = e - stop_dist * TARGET_RR
    else:
        return None, None, None

    return None, round(t2, 6), None


def pips_at(entry, level, pair, direction):
    """Pip profit from entry to level (positive = favourable for the trade)."""
    e  = _to_float(entry)
    lv = _to_float(level)
    if e is None or lv is None:
        return None
    ps = _pip_size(pair or "")
    d  = (direction or "").upper()
    profit = (lv - e) if d == "BUY" else (e - lv)
    return round(profit / ps, 1)


# ── Milestone checkers ────────────────────────────────────────────────────────

def target_hit(row: dict, price: float) -> bool:
    """True if price crossed the 2R WIN target and not already recorded."""
    if _is_true(row.get("t2_hit")):
        return False
    t2 = _to_float(row.get("t2_price"))
    if t2 is None or t2 == 0:
        return False
    d = (row.get("direction") or "").upper()
    return (price >= t2) if d == "BUY" else (price <= t2)


def stop_hit(row: dict, price: float) -> bool:
    """True if price crossed the original stop_loss (stop never moves)."""
    sl = _to_float(row.get("stop_loss"))
    if sl is None:
        return False
    d = (row.get("direction") or "").upper()
    return (price <= sl) if d == "BUY" else (price >= sl)


def t3_hit(row: dict, price: float) -> bool:
    """Always False — no T3 in the pure TP-or-SL system."""
    return False


# ── Outcome & pips ────────────────────────────────────────────────────────────

def cascade_outcome(row: dict) -> str:
    """Determine closed outcome.

    t2_hit (target reached) → WIN
    legacy t3_hit           → FULL_WIN (historical trades with t3_price > 0 only)
    otherwise               → LOSS

    PARTIAL_WIN is a status preserved for historical trades and will never be
    returned here — new trades only ever produce WIN or LOSS.
    """
    t3p = _to_float(row.get("t3_price")) or 0.0
    if t3p > 0 and _is_true(row.get("t3_hit")):
        return "FULL_WIN"
    if _is_true(row.get("t2_hit")):
        return "WIN"
    return "LOSS"


def weighted_pips(row: dict) -> float:
    """Net pips for the closed trade.

    New system (t3_price=0): t2_hit_pips if WIN, else 0.0
    Legacy cascade (t3_price>0): position-weighted T1/T2/T3 partials.
    """
    t3p  = _to_float(row.get("t3_price")) or 0.0
    t1hp = _to_float(row.get("t1_hit_pips")) or 0.0
    t2hp = _to_float(row.get("t2_hit_pips")) or 0.0
    t3hp = _to_float(row.get("t3_hit_pips")) or 0.0

    if t3p > 0:
        # Legacy cascade: weighted partial-close pips
        return round(_LEGACY_T1_SIZE * t1hp + _LEGACY_T2_SIZE * t2hp + _LEGACY_T3_SIZE * t3hp, 1)

    if _is_true(row.get("t2_hit")):
        return round(t2hp, 1)   # 100% at 2R
    return 0.0


def total_pips(row: dict) -> float:
    """Raw sum of pips from each hit milestone (unweighted)."""
    t1p = _to_float(row.get("t1_hit_pips")) or 0.0
    t2p = _to_float(row.get("t2_hit_pips")) or 0.0
    t3p = _to_float(row.get("t3_hit_pips")) or 0.0
    return round(t1p + t2p + t3p, 1)


def expiry_extension(row: dict, base_expiry: int) -> int:
    """Extend expiry +5d after target hit; cap at 21d."""
    t2 = _is_true(row.get("t2_hit"))
    ext = base_expiry + (5 if t2 else 0)
    return min(ext, 21)
