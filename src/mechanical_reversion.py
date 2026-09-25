"""Proposal-stage logic for the mechanical-only reversion signal validated in
the 2026-09-25 edge-mining research loop (see PROPOSAL_mechanical_reversion_
engine.md at the repo root for the full writeup, the real discovery/holdout
numbers, and the honest caveats).

NOT WIRED INTO ANYTHING LIVE. This module is not imported by daily.py,
monitor.py, virtual_books.py, or any other real code path -- it exists so
the proposal is concrete enough to review, nothing more. Pure functions
only: no I/O, no API calls, no state.

The validated signal: rib_against AND osc_agrees -- the daily EMA ribbon is
against the trade direction, AND the RSI/Stochastic/CCI oscillator
confluence agrees with the trade's own direction (a reversal signal in the
SAME direction as the trade, i.e. the trade is a counter-trend bet that the
oscillators say is due to bounce). Entry construction reuses this
codebase's own already-validated mechanical 2:1 R:R shape
(cascade.py's TARGET_RR), not a new sizing scheme.
"""
from src.cascade import TARGET_RR

_RIBBON_AGAINST_BUY = {"ALIGNED_BEAR", "LEANING_BEAR"}
_RIBBON_AGAINST_SELL = {"ALIGNED_BULL", "LEANING_BULL"}


def ribbon_against(direction: str, ribbon_status: str) -> bool:
    """True when the daily EMA ribbon opposes the trade direction (either
    LEANING or ALIGNED against -- the broader definition, not the narrower
    "strongly against" ALIGNED-only one; the mechanical backtest found the
    two are statistically indistinguishable from each other, so this uses
    the broader, simpler one)."""
    d = (direction or "").upper()
    rib = (ribbon_status or "").upper().strip()
    if d == "BUY":
        return rib in _RIBBON_AGAINST_BUY
    if d == "SELL":
        return rib in _RIBBON_AGAINST_SELL
    return False


def oscillator_agrees(direction: str, osc_direction: str) -> bool:
    """True when technical.py's _oscillator_confluence() direction (BUY/
    SELL/NONE, from RSI/Stochastic/CCI oversold-overbought agreement)
    matches the trade's own direction -- the oscillators are signaling a
    reversal in the same direction as the trade itself."""
    d = (direction or "").upper()
    osc = (osc_direction or "").upper().strip()
    return d in ("BUY", "SELL") and osc == d


def mechanical_reversion_fires(direction: str, ribbon_status: str, osc_direction: str) -> bool:
    """The validated signal itself: ribbon against the trade direction AND
    oscillators confirming a reversal in the trade's own direction.

    KNOWN LIMITATION, stated plainly per the proposal doc: round 2 of the
    discovery-slice research found oscillator confirmation does NOT add
    statistically significant value on top of ribbon_against alone (the two
    signals overlap substantially, r=0.32) -- this combined condition is
    what was holdout-tested and survived, but it is not proven to be a
    genuine synergistic interaction rather than a redundant restatement of
    mostly the same underlying fact. Reported as such, not oversold.
    """
    return ribbon_against(direction, ribbon_status) and oscillator_agrees(direction, osc_direction)


def compute_mechanical_levels(entry: float, atr14: float, direction: str, pip_size: float) -> dict:
    """Entry/stop/target using the exact mechanical construction validated
    in scripts/mechanical_edge_mining_dataset.py and already used
    elsewhere in this codebase (cascade.py's TARGET_RR=2.0): stop is
    ATR14-derived, rounded to the nearest 5 pips (floor 5 pips), target is
    TARGET_RR x stop distance. Returns {} if inputs are invalid (zero/
    negative ATR or pip size) -- callers should treat that as "cannot
    construct a mechanical entry for this candidate", not a zero-risk trade.
    """
    if not entry or not atr14 or atr14 <= 0 or not pip_size or pip_size <= 0:
        return {}
    d = (direction or "").upper()
    if d not in ("BUY", "SELL"):
        return {}

    atr_pips = atr14 / pip_size
    stop_pips = max(round(atr_pips / 5) * 5, 5)
    stop_dist = stop_pips * pip_size

    if d == "BUY":
        stop = entry - stop_dist
        target = entry + TARGET_RR * stop_dist
    else:
        stop = entry + stop_dist
        target = entry - TARGET_RR * stop_dist

    return {
        "entry": entry, "stop_loss": stop, "target": target,
        "stop_pips": stop_pips, "reward_risk": TARGET_RR,
    }
