"""Broker integration stub.

LIVE_TRADING = False at all times until a real broker API is wired up.
All functions here return simulated/paper values — nothing touches real money.

When ready to go live:
1. Set LIVE_TRADING = True (or load from env)
2. Implement _execute_real_order() against your broker's REST API
3. Implement get_live_balance() to fetch the real account balance
4. Add broker-specific authentication in config.py
"""

import os

# Master kill switch — must be explicitly set to True in the environment to enable live trading.
# False by default in all environments including GitHub Actions.
LIVE_TRADING: bool = os.getenv("LIVE_TRADING", "false").lower() == "true"

# Spread constants (in pips) for realistic cost simulation
_SPREADS: dict = {
    "EUR/USD": 0.8,
    "GBP/USD": 1.0,
    "USD/JPY": 0.8,
    "USD/CHF": 1.0,
    "AUD/USD": 0.9,
    "NZD/USD": 1.5,
    "USD/CAD": 1.2,
    "EUR/GBP": 1.0,
    "EUR/JPY": 1.0,
    "GBP/JPY": 1.5,
    "AUD/JPY": 1.5,
    "EUR/AUD": 1.8,
    "GBP/AUD": 2.5,
    "EUR/CAD": 1.8,
    "USD/NOK": 8.0,
    "USD/SEK": 8.0,
    "USD/HKD": 5.0,
    "USD/SGD": 3.0,
}
_DEFAULT_SPREAD = 2.0


def get_spread_pips(pair: str) -> float:
    """Return typical spread for a pair in pips (for realistic cost simulation)."""
    return _SPREADS.get(pair.upper().replace(" ", ""), _DEFAULT_SPREAD)


def simulate_entry_slippage(pair: str, direction: str, entry_price: float) -> float:
    """Apply spread as entry slippage (buys pay ask, sells receive bid).

    Returns the adjusted entry price after spread cost.
    """
    from src.risk_manager import _pip_size
    pip = _pip_size(pair)
    spread = get_spread_pips(pair) * pip
    if direction.upper() == "BUY":
        return entry_price + spread / 2
    return entry_price - spread / 2


def get_live_balance() -> float | None:
    """Return real account balance from broker API.

    Returns None when LIVE_TRADING is False or broker API is not configured.
    Implement this function with real broker API calls when going live.
    """
    if not LIVE_TRADING:
        return None
    # TODO: implement real broker API call
    # Example: return broker_client.get_account_balance()
    return None


def place_order(pair: str, direction: str, lots: float,
                entry: float, stop: float, target: float) -> dict:
    """Place a trade order (paper only when LIVE_TRADING=False).

    Returns a dict with: order_id, status, message.
    """
    if not LIVE_TRADING:
        return {
            "order_id":  None,
            "status":    "PAPER",
            "message":   f"Paper trade — LIVE_TRADING=False. Would place {direction} {lots} lots {pair}",
        }
    # TODO: implement real broker order placement
    raise NotImplementedError("Live order placement not implemented — set LIVE_TRADING=False")


def close_order(order_id: str, lots: float | None = None) -> dict:
    """Close an open order (paper only when LIVE_TRADING=False)."""
    if not LIVE_TRADING:
        return {
            "order_id": order_id,
            "status":   "PAPER",
            "message":  f"Paper close — LIVE_TRADING=False. Would close order {order_id}",
        }
    raise NotImplementedError("Live order closure not implemented — set LIVE_TRADING=False")
