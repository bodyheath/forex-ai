"""Cascading target system: T1/T2/T3 milestone computation and checking.

Shared by research_outcome_checker.py (research trades) and
outcome_checker.py (fund trades).

Design:
  T1 = entry ± 0.4 × ATR  — close 40%, move stop to breakeven
  T2 = entry ± 0.7 × ATR  — close 30% more (70% total banked)
  T3 = existing target     — close final 30% → FULL_WIN

ATR is estimated as abs(entry - stop_loss) since the stop is placed at 1× ATR.
An explicit atr= override can be passed when ATR14 is available from the bundle.

Outcome statuses:
  FULL_WIN    — all three targets hit
  WIN         — T1 + T2 hit, then stopped or expired (70% banked)
  PARTIAL_WIN — T1 hit, then stopped or expired (40% banked)
  LOSS        — stopped before T1 (original stop_loss)

Position-weighted pips (cascading_total_pips_weighted):
  0.40 × t1_pips  +  0.30 × t2_pips  +  0.30 × t3_pips
"""

T1_MULT = 0.4
T2_MULT = 0.7

T1_SIZE = 0.40
T2_SIZE = 0.30
T3_SIZE = 0.30


def _is_true(val) -> bool:
    """Return True when a CSV boolean field is set.

    Handles str ("TRUE", "1", "YES"), bool True, and int 1 consistently so the
    caller never needs to know whether the value came from a CSV string, a
    Python boolean, or a JSON number.
    """
    return str(val).strip().upper() in ("TRUE", "1", "YES")


def _pip_size(pair: str) -> float:
    cleaned = (pair or "").upper().replace("/", "").replace("-", "")
    if len(cleaned) >= 6:
        if cleaned[3:6] == "JPY":
            return 0.01
        if cleaned[:3] == "JPY":
            return 0.000001
    if "JPY" in (pair or "").upper():
        return 0.01
    return 0.0001


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ── Level computation ─────────────────────────────────────────────────────────

def compute_levels(entry, stop_loss, target, direction, atr=None):
    """Return (t1_price, t2_price, t3_price).

    T1 and T2 are from ATR (estimated as stop distance when atr is not given).
    T3 is the existing target — unchanged.
    Returns (None, None, target_float) when ATR cannot be determined.
    """
    e = _to_float(entry)
    s = _to_float(stop_loss)
    t = _to_float(target)
    if e is None:
        return None, None, t

    atr_val = _to_float(atr)
    if atr_val and atr_val > 0:
        use_atr = atr_val
    elif s is not None and abs(e - s) > 0:
        use_atr = abs(e - s)
    else:
        return None, None, t

    d = (direction or "").upper()
    if d == "BUY":
        t1 = e + T1_MULT * use_atr
        t2 = e + T2_MULT * use_atr
    elif d == "SELL":
        t1 = e - T1_MULT * use_atr
        t2 = e - T2_MULT * use_atr
    else:
        return None, None, t

    return round(t1, 6), round(t2, 6), round(t, 6) if t is not None else None


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

def t1_hit(row: dict, price: float) -> bool:
    """True if price has crossed T1 and T1 not yet recorded."""
    if str(row.get("t1_hit", "")).upper() == "TRUE":
        return False
    t1 = _to_float(row.get("t1_price"))
    if t1 is None:
        return False
    d = (row.get("direction") or "").upper()
    return (price >= t1) if d == "BUY" else (price <= t1)


def t2_hit(row: dict, price: float) -> bool:
    """True if T1 was hit and price has crossed T2 (T2 not yet recorded)."""
    if str(row.get("t1_hit", "")).upper() != "TRUE":
        return False
    if str(row.get("t2_hit", "")).upper() == "TRUE":
        return False
    t2 = _to_float(row.get("t2_price"))
    if t2 is None:
        return False
    d = (row.get("direction") or "").upper()
    return (price >= t2) if d == "BUY" else (price <= t2)


def t3_hit(row: dict, price: float) -> bool:
    """True if T2 was hit and price has crossed T3 (T3 not yet recorded)."""
    if str(row.get("t2_hit", "")).upper() != "TRUE":
        return False
    if str(row.get("t3_hit", "")).upper() == "TRUE":
        return False
    t3 = _to_float(row.get("t3_price")) or _to_float(row.get("target"))
    if t3 is None:
        return False
    d = (row.get("direction") or "").upper()
    return (price >= t3) if d == "BUY" else (price <= t3)


def effective_stop_hit(row: dict, price: float) -> bool:
    """True if price has crossed the effective stop level."""
    eff = _to_float(row.get("effective_stop")) or _to_float(row.get("stop_loss"))
    if eff is None:
        return False
    d = (row.get("direction") or "").upper()
    return (price <= eff) if d == "BUY" else (price >= eff)


# ── Outcome & pips ────────────────────────────────────────────────────────────

def cascade_outcome(row: dict) -> str:
    """Determine closed outcome from which targets were hit."""
    if str(row.get("t3_hit", "")).upper() == "TRUE":
        return "FULL_WIN"
    if str(row.get("t2_hit", "")).upper() == "TRUE":
        return "WIN"
    if str(row.get("t1_hit", "")).upper() == "TRUE":
        return "PARTIAL_WIN"
    return "LOSS"


def weighted_pips(row: dict) -> float:
    """Position-size-weighted pips across banked portions (0 for unbanked)."""
    t1p = _to_float(row.get("t1_hit_pips")) or 0.0
    t2p = _to_float(row.get("t2_hit_pips")) or 0.0
    t3p = _to_float(row.get("t3_hit_pips")) or 0.0
    return round(T1_SIZE * t1p + T2_SIZE * t2p + T3_SIZE * t3p, 1)


def total_pips(row: dict) -> float:
    """Raw sum of pips from each hit T milestone (unweighted)."""
    t1p = _to_float(row.get("t1_hit_pips")) or 0.0
    t2p = _to_float(row.get("t2_hit_pips")) or 0.0
    t3p = _to_float(row.get("t3_hit_pips")) or 0.0
    return round(t1p + t2p + t3p, 1)


def expiry_extension(row: dict, base_expiry: int) -> int:
    """Extend expiry: +3d after T1, +5d after T2; cap at 21 days total."""
    t2 = str(row.get("t2_hit", "")).upper() == "TRUE"
    t1 = str(row.get("t1_hit", "")).upper() == "TRUE"
    ext = base_expiry + (5 if t2 else (3 if t1 else 0))
    return min(ext, 21)
