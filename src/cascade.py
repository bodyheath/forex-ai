"""Simple 2:1 risk:reward target system.

Design:
  TRAIL  = entry ± 1R (1× stop distance) — move stop to breakeven, no partial close
  TARGET = entry ± 2R (2× stop distance) — close 100% → WIN

Trade CSV field mapping:
  t1_price = trail level (1R)    — activates breakeven trail when crossed
  t2_price = target (2R)         — WIN when crossed, close 100%
  t3_price = 0                   — unused in new system
  t1_hit   = trail activated flag
  t2_hit   = WIN recorded flag
  t3_hit   = always False in new system

Outcome statuses:
  WIN         — TARGET (2R) reached, 100% position closed, +2R net
  PARTIAL_WIN — TRAIL activated, then stopped at entry (breakeven, 0R net)
  LOSS        — stopped before TRAIL (original stop_loss, -1R net)

Break-even WR = 33.3%   EV at 55% WR = +0.65R per trade
"""

TARGET_RR   = 2.0   # Risk 1R to make 2R
TRAIL_AT_RR = 1.0   # Move stop to breakeven at 1R from entry

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
    """Return (trail_level, target_level, None).

    trail_level  = entry ± 1× stop_distance  (triggers breakeven trail at 1R)
    target_level = entry ± 2× stop_distance  (full WIN close at 2R)
    Third element is always None — no T3 in the 2:1 system.

    ATR and adaptive multipliers are ignored — targets are always 1R and 2R
    based on the actual stop distance from entry.
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
        t1 = e + stop_dist
        t2 = e + stop_dist * TARGET_RR
    elif d == "SELL":
        t1 = e - stop_dist
        t2 = e - stop_dist * TARGET_RR
    else:
        return None, None, None

    return round(t1, 6), round(t2, 6), None


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
    """True if price crossed the 1R trail level and trail not yet activated."""
    if _is_true(row.get("t1_hit")):
        return False
    t1 = _to_float(row.get("t1_price"))
    if t1 is None:
        return False
    d = (row.get("direction") or "").upper()
    return (price >= t1) if d == "BUY" else (price <= t1)


def t2_hit(row: dict, price: float) -> bool:
    """True if trail is active and price crossed the 2R WIN target."""
    if not _is_true(row.get("t1_hit")):
        return False
    if _is_true(row.get("t2_hit")):
        return False
    t2 = _to_float(row.get("t2_price"))
    if t2 is None or t2 == 0:
        return False
    d = (row.get("direction") or "").upper()
    return (price >= t2) if d == "BUY" else (price <= t2)


def t3_hit(row: dict, price: float) -> bool:
    """Always False — no T3 in the 2:1 system."""
    return False


def effective_stop_hit(row: dict, price: float) -> bool:
    """True if price has crossed the effective stop level."""
    eff = _to_float(row.get("effective_stop")) or _to_float(row.get("stop_loss"))
    if eff is None:
        return False
    d = (row.get("direction") or "").upper()
    return (price <= eff) if d == "BUY" else (price >= eff)


# ── Outcome & pips ────────────────────────────────────────────────────────────

def cascade_outcome(row: dict) -> str:
    """Determine closed outcome from which targets were hit.

    New 2:1 system:
      t2_hit      → WIN (100% closed at 2R target)
      t1_hit only → PARTIAL_WIN (trail active, stopped at entry, 0R net)
      neither     → LOSS (-1R)

    Legacy: if t3_price > 0 and t3_hit, return FULL_WIN for old cascade trades.
    """
    t3p = _to_float(row.get("t3_price")) or 0.0
    if t3p > 0 and _is_true(row.get("t3_hit")):
        return "FULL_WIN"
    if _is_true(row.get("t2_hit")):
        return "WIN"
    if _is_true(row.get("t1_hit")):
        return "PARTIAL_WIN"
    return "LOSS"


def weighted_pips(row: dict) -> float:
    """Net pips for the closed trade.

    New 2:1 system (t3_price=0):
      WIN (t2_hit): full t2_hit_pips at 100% position
      PARTIAL_WIN (t1_hit only): 0.0 (stopped at entry, net breakeven)

    Legacy cascade (t3_price>0): position-weighted T1/T2/T3 partials.
    """
    t3p   = _to_float(row.get("t3_price")) or 0.0
    t1hp  = _to_float(row.get("t1_hit_pips")) or 0.0
    t2hp  = _to_float(row.get("t2_hit_pips")) or 0.0
    t3hp  = _to_float(row.get("t3_hit_pips")) or 0.0

    if t3p > 0:
        # Legacy cascade: weighted partial-close pips
        return round(_LEGACY_T1_SIZE * t1hp + _LEGACY_T2_SIZE * t2hp + _LEGACY_T3_SIZE * t3hp, 1)

    # New 2:1 system
    if _is_true(row.get("t2_hit")):
        return round(t2hp, 1)   # 100% at 2R
    return 0.0                   # PARTIAL_WIN: stopped at breakeven, net = 0


def total_pips(row: dict) -> float:
    """Raw sum of pips from each hit milestone (unweighted)."""
    t1p = _to_float(row.get("t1_hit_pips")) or 0.0
    t2p = _to_float(row.get("t2_hit_pips")) or 0.0
    t3p = _to_float(row.get("t3_hit_pips")) or 0.0
    return round(t1p + t2p + t3p, 1)


def expiry_extension(row: dict, base_expiry: int) -> int:
    """Extend expiry: +3d after trail activated, +5d after WIN; cap at 21d."""
    t2 = _is_true(row.get("t2_hit"))
    t1 = _is_true(row.get("t1_hit"))
    ext = base_expiry + (5 if t2 else (3 if t1 else 0))
    return min(ext, 21)
