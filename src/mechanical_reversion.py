"""Logic for the mechanical-only reversion signals validated in the
2026-09-25/26 edge-mining research loop (see
PROPOSAL_mechanical_reversion_engine.md at the repo root for Book G's full
writeup, and scripts/edge_mining_sweep_round4.py +
edge_mining_holdout_check_round4.py + edge_mining_cluster_bootstrap_
stress_test.py for phenomena 2/3's round-4 discovery/holdout/cluster-
bootstrap results backing oscillator_agrees-alone and
bollinger_extreme_agrees below).

ribbon_against/oscillator_agrees/mechanical_reversion_fires/
compute_mechanical_levels are WIRED INTO src/virtual_books.py's Book G
(_elig_g_mechanical_reversion). oscillator_agrees alone (no ribbon check)
is Book H's signal (_elig_h_oscillator_extremity); bollinger_extreme_agrees
is Book I's (_elig_i_bollinger_extremity). Pure functions only: no I/O, no
API calls, no state.

Book G's validated signal: rib_against AND osc_agrees -- the daily EMA
ribbon is against the trade direction, AND the RSI/Stochastic/CCI
oscillator confluence agrees with the trade's own direction (a reversal
signal in the SAME direction as the trade, i.e. the trade is a
counter-trend bet that the oscillators say is due to bounce).

Book H's validated signal: oscillator_agrees ALONE, no ribbon requirement
-- round 4 found Book G's own population is a STRICT SUBSET of this
broader one (100% of Book G's discovery+holdout rows also satisfy
osc_agrees alone), and the broader population survives discovery, holdout,
AND cluster-bootstrap independently. Book H exists to answer, with real
forward evidence, a question the backtest alone can't fully settle: does
requiring ribbon-against ON TOP of oscillator extremity actually improve
WR/PF, or does it just shrink the sample for no real benefit? Running both
books side by side on the same real candidate stream is the natural
head-to-head test.

Book I's validated signal: bollinger_extreme_agrees -- price at a
Bollinger Band extreme (near/beyond the lower band on a BUY, near/beyond
the upper band on a SELL) confirming the trade's own direction. A
genuinely different indicator family from the RSI/Stochastic/CCI
oscillators (volatility-band position, not momentum), with materially
lower population overlap against Book G/H (Jaccard <=0.59 against every
other validated signal here, vs the near-total overlap between Book G, H,
and the phenomenon2+osc combination that was NOT proposed as its own book
for exactly that reason -- see round 4's report).

Entry construction for all three books reuses this codebase's own
already-validated mechanical 2:1 R:R shape (cascade.py's TARGET_RR), not a
new sizing scheme.
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


_BB_EXTREME_LOW = 0.2
_BB_EXTREME_HIGH = 0.8


def bollinger_extreme_agrees(direction: str, bb_position) -> bool:
    """True when price sits at a Bollinger Band extreme confirming the
    trade's own direction: bb_position <= 0.2 (near or below the lower
    band) on a BUY, or bb_position >= 0.8 (near or above the upper band)
    on a SELL. bb_position is technical.py::_summarise()'s existing
    fraction-of-band-width field (0=lower band, 1=upper band; can go
    negative or above 1 when price pierces a band entirely during a strong
    move -- the >=/<= thresholds here correctly treat that as MORE
    extreme, not out of range). Round 4 of the edge-mining research loop
    validated this exact threshold pair on both the discovery slice
    (n_fire=6369, p=0.00025) and holdout (n_fire=2773, p=0.00003), and it
    survived a cluster bootstrap on holdout (p=0.006) -- see
    scripts/edge_mining_sweep_round4.py and
    edge_mining_cluster_bootstrap_stress_test.py.

    Returns False (never fires) if bb_position is missing/non-numeric --
    fails closed, matching this module's other functions' convention of
    never fabricating a signal from absent data.
    """
    d = (direction or "").upper()
    if d not in ("BUY", "SELL"):
        return False
    try:
        pos = float(bb_position)
    except (TypeError, ValueError):
        return False
    if d == "BUY":
        return pos <= _BB_EXTREME_LOW
    return pos >= _BB_EXTREME_HIGH


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
