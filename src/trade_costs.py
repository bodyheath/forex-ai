"""Realistic spread, slippage, swap, and commission cost modelling for paper trades.

All cost functions return values in pips.  Pip-based costs are independent of
lot size (because both the dollar value per pip and the notional both scale
linearly with lots, so the ratio cancels).  The ``lots`` parameter is accepted
for signature consistency with the trade tracker but does not affect pip output.

Cost components per round-trip trade:
  spread          — bid/ask spread paid on entry
  entry_slip      — entry slippage (market impact on open)
  exit_slip       — exit slippage (market impact on close / stop trigger)
  swap_total      — overnight interest rate differential × days held
                    positive = carry earns (long high-rate currency)
                    negative = carry costs (short high-rate currency)
  commission      — broker commission, ~$3.50/standard-lot round trip

Net P&L = gross_pips − (spread + entry_slip + exit_slip + commission − swap_total)
"""

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

# Current approximate central bank / short-term policy rates (% per annum).
# Updated June 2026.  Used when live FRED rates are unavailable.
_FALLBACK_RATES: dict = {
    "USD": 5.33,
    "EUR": 3.65,
    "GBP": 4.50,
    "JPY": 0.50,
    "AUD": 4.10,
    "NZD": 3.75,
    "CAD": 3.00,
    "CHF": 0.25,
    "NOK": 4.50,
    "SEK": 2.50,
    "SGD": 3.60,
    "HKD": 5.33,
}

# G10 / liquid-Asian currencies that attract wider spreads and more slippage
_G10_MINOR = {"NOK", "SEK", "SGD", "HKD"}

# Pairs treated as liquid majors (USD is one leg, deep liquidity)
_MAJOR_PAIRS = {
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
    "USDCAD", "NZDUSD", "USDCHF",
}

# Highly liquid JPY and EUR crosses
_PRIMARY_CROSS_PAIRS = {
    "GBPJPY", "EURJPY", "EURGBP", "EURCHF",
    "AUDJPY", "CADJPY", "CHFJPY", "NZDJPY",
}

# Spread in pips by tier
_SPREAD_BY_TIER: dict = {
    "major":         0.8,
    "primary_cross": 1.5,
    "cross":         2.0,
    "g10_minor":     4.0,
}

# Slippage per side (entry OR exit) in pips by tier
_SLIP_BY_TIER: dict = {
    "major":         0.5,
    "primary_cross": 1.0,
    "cross":         1.5,
    "g10_minor":     2.0,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _pip_size(pair: str) -> float:
    """0.01 for JPY-quoted pairs, else 0.0001."""
    clean = pair.upper().replace("/", "")
    return 0.01 if clean.endswith("JPY") else 0.0001


def _currencies(pair: str):
    """Return (base, quote) as 3-letter strings."""
    clean = pair.upper().replace("/", "")
    return clean[:3], clean[3:]


def _tier(pair: str) -> str:
    """Return cost tier string for pair."""
    base, quote = _currencies(pair)
    if base in _G10_MINOR or quote in _G10_MINOR:
        return "g10_minor"
    clean = pair.upper().replace("/", "")
    if clean in _MAJOR_PAIRS:
        return "major"
    if clean in _PRIMARY_CROSS_PAIRS:
        return "primary_cross"
    return "cross"


def _get_rates(pair: str) -> tuple:
    """Return (base_rate, quote_rate) from fallback table."""
    base, quote = _currencies(pair)
    return _FALLBACK_RATES.get(base, 2.0), _FALLBACK_RATES.get(quote, 2.0)


# ---------------------------------------------------------------------------
# Public API — individual cost components
# ---------------------------------------------------------------------------

def get_spread(pair: str) -> float:
    """Typical bid/ask spread in pips for *pair*."""
    return _SPREAD_BY_TIER[_tier(pair)]


def get_slippage_per_side(pair: str) -> float:
    """Expected slippage in pips on entry or exit for *pair*."""
    return _SLIP_BY_TIER[_tier(pair)]


def commission_pips(pair: str) -> float:
    """Round-trip broker commission in pips.

    Based on $3.50 per standard lot.  Because both pip value and notional
    scale linearly with lot size the result is lot-size independent:

      non-JPY, USD-quoted   → $3.50 / ($10/pip)       = 0.35 pips
      JPY-quoted (≈150)     → $3.50 / ($6.67/pip)     ≈ 0.53 pips
      other crosses         → ~0.42 pips (approximate)
    """
    _, quote = _currencies(pair)
    if quote == "JPY":
        return 0.53
    if quote == "USD":
        return 0.35
    return 0.42


def daily_swap_pips(pair: str, direction: str, base_rate: float,
                    quote_rate: float, entry_price: float) -> float:
    """Overnight swap in pips per calendar day.

    Positive = you earn (carry trade in your favour).
    Negative = you pay (carry trade against you).

    Formula:  rate_diff × entry_price / (pip_size × 100 × 365)
    where rate_diff = base_rate − quote_rate for BUY, reversed for SELL.
    """
    pip_sz = _pip_size(pair)
    if entry_price <= 0 or pip_sz <= 0:
        return 0.0
    rate_diff = (base_rate - quote_rate) if direction.upper() == "BUY" else (quote_rate - base_rate)
    return rate_diff * entry_price / (pip_sz * 100.0 * 365.0)


# ---------------------------------------------------------------------------
# Public API — composite functions
# ---------------------------------------------------------------------------

def compute_costs(pair: str, direction: str, entry_price: float,
                  days_held: float, base_rate: float = None,
                  quote_rate: float = None, lots: float = 0.1) -> dict:
    """Return a breakdown dict of all trading costs in pips.

    Keys
    ----
    spread          — bid/ask spread (pips)
    entry_slip      — entry slippage (pips)
    exit_slip       — exit slippage (pips)
    swap_total      — total swap over *days_held* (pips, positive = earns)
    commission      — round-trip commission (pips)
    total_cost_pips — net cost = spread + entry_slip + exit_slip + commission
                      minus swap_total (so carry trade reduces costs)
    """
    if base_rate is None or quote_rate is None:
        br, qr = _get_rates(pair)
        base_rate  = br if base_rate  is None else base_rate
        quote_rate = qr if quote_rate is None else quote_rate

    spread     = get_spread(pair)
    slip       = get_slippage_per_side(pair)
    comm       = commission_pips(pair)
    swap_day   = daily_swap_pips(pair, direction, base_rate, quote_rate, entry_price)
    swap_total = round(swap_day * max(0.0, days_held), 2)

    total = round(spread + slip * 2 + comm - swap_total, 2)

    return {
        "spread":          round(spread, 2),
        "entry_slip":      round(slip,   2),
        "exit_slip":       round(slip,   2),
        "swap_total":      swap_total,
        "commission":      round(comm,   2),
        "total_cost_pips": total,
    }


def check_viability(pair: str, direction: str, entry, stop, target,
                    base_rate: float = None, quote_rate: float = None,
                    lots: float = 0.1, expected_days: float = 3.0,
                    min_net_rr: float = 1.5) -> dict:
    """Check whether a prospective trade is viable after realistic costs.

    Returns
    -------
    dict with keys:
      gross_rr         — R:R before costs
      gross_pips       — target distance in pips (potential profit, gross)
      total_cost_pips  — all costs combined (positive = costs money)
      net_pips         — gross_pips minus total_cost_pips
      net_rr           — R:R after costs
      viable           — True if net_rr >= min_net_rr
      not_viable_reason — human-readable reason string, or None if viable
      costs            — the full compute_costs() dict
    """
    try:
        entry  = float(entry)
        stop   = float(stop)
        target = float(target)
    except (TypeError, ValueError):
        return {
            "gross_rr": 0.0, "gross_pips": 0.0, "total_cost_pips": 0.0,
            "net_pips": 0.0, "net_rr": 0.0, "viable": False,
            "not_viable_reason": "missing price levels",
            "costs": {},
        }

    if base_rate is None or quote_rate is None:
        br, qr = _get_rates(pair)
        base_rate  = br if base_rate  is None else base_rate
        quote_rate = qr if quote_rate is None else quote_rate

    pip_sz          = _pip_size(pair)
    gross_risk_pips = abs(entry - stop)   / pip_sz
    gross_prof_pips = abs(target - entry) / pip_sz

    if gross_risk_pips <= 0:
        return {
            "gross_rr": 0.0, "gross_pips": 0.0, "total_cost_pips": 0.0,
            "net_pips": 0.0, "net_rr": 0.0, "viable": False,
            "not_viable_reason": "zero risk distance",
            "costs": {},
        }

    costs      = compute_costs(pair, direction, entry, expected_days, base_rate, quote_rate, lots)
    total_cost = costs["total_cost_pips"]

    net_pips = gross_prof_pips - total_cost
    gross_rr = round(gross_prof_pips / gross_risk_pips, 2)
    net_rr   = round(net_pips / gross_risk_pips, 2)

    viable = net_rr >= min_net_rr
    reason = None
    if not viable:
        reason = (
            f"Net R:R {net_rr:.1f}:1 < {min_net_rr}:1 after "
            f"{total_cost:.1f}p costs"
        )

    return {
        "gross_rr":         gross_rr,
        "gross_pips":       round(gross_prof_pips, 1),
        "total_cost_pips":  round(total_cost,      2),
        "net_pips":         round(net_pips,         1),
        "net_rr":           net_rr,
        "viable":           viable,
        "not_viable_reason": reason,
        "costs":            costs,
    }


# Default ATR table in price units (approximate daily ATR for suitability check)
_DEFAULT_ATR: dict = {
    "EUR/HKD": 0.020, "NZD/HKD": 0.020, "USD/HKD": 0.001,
    "GBP/HKD": 0.030, "AUD/HKD": 0.018, "CAD/HKD": 0.015,
    "CHF/HKD": 0.025, "SGD/HKD": 0.010, "HKD/JPY": 0.005,
}


def is_pair_suitable(pair: str) -> tuple:
    """Return (suitable: bool, reason: str) based on spread vs ATR.

    Returns (False, reason) if the spread exceeds 33% of the pair's estimated ATR.
    This flags pairs where transaction costs eat too much of the expected move.

    The ATR lookup uses _DEFAULT_ATR for known pairs; unknown pairs are assumed suitable.
    """
    try:
        spread_pips = get_spread(pair)
        atr = _DEFAULT_ATR.get(pair.upper())
        if atr is None:
            # Try reversed pair key
            clean = pair.upper().replace("/", "")
            if len(clean) >= 6:
                rev = clean[3:6] + "/" + clean[:3]
                atr = _DEFAULT_ATR.get(rev)
        if atr is None:
            return (True, "")
        pip_sz  = _pip_size(pair)
        atr_pips = atr / pip_sz
        threshold = atr_pips * 0.33
        if spread_pips > threshold:
            return (
                False,
                f"{pair}: spread {spread_pips:.1f}p exceeds 33% of ATR "
                f"({atr_pips:.0f}p) — transaction costs too high relative to daily move",
            )
        return (True, "")
    except Exception:
        return (True, "")


def net_pips_for_closed_trade(pair: str, direction: str, entry_price: float,
                               gross_pips: float, days_held: float,
                               base_rate: float = None,
                               quote_rate: float = None) -> float:
    """Compute net pips for a trade that has already closed.

    Used by tracker.update_outcome() to store net_pips alongside gross pips.
    """
    try:
        if not isinstance(gross_pips, (int, float)):
            return gross_pips
        costs = compute_costs(pair, direction, entry_price, days_held,
                              base_rate, quote_rate)
        return round(float(gross_pips) - costs["total_cost_pips"], 1)
    except Exception:
        return gross_pips
