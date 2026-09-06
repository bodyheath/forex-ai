"""Real pairwise correlation model for the correlation/clustering risk gate.

Replaces risk_manager.py's literal currency-code check (apply_correlation_
checks / _currency_exposure), which only catches trades that share an exact
3-letter currency code (e.g. EUR/USD + GBP/USD both touch USD) and, as a
result, both over-flags pairs that share a code but don't actually move
together, and misses real correlation between currency-disjoint pairs.

Investigated 2026-09-07 (scripts/correlation_investigation.py) against two
alternatives before building this: the market_regime.py risk-on/risk-off
currency grouping was checked first (fastest, reuses something already
built) and rejected on real numbers -- of 378 real pair-combinations, it
only agreed with real |corr|>=0.60 on 39.5% of genuinely-correlated pairs,
and wrongly flagged 20.1% of genuinely near-zero-correlation pairs as
correlated. It's a coarse 2-bucket split across 8 currencies with no
magnitude and no pairwise structure -- built for regime currency bias, not
position-risk clustering, and it shows.

This module instead computes correlation directly from real historical daily
returns across the 28 src.selector.UNIVERSE pairs. Out-of-sample split-half
validation (first half of ~3y of real daily closes vs the unseen second
half) showed this is a real, persistent structural feature, not a curve-fit
artifact: 82.4% of pairs with |corr|>=0.60 in the calibration half were still
>=0.60 in the following, unseen half; 81.0% of pairs with |corr|<0.30 stayed
below 0.30. Replayed against real historical multi-pair candidate-days
(Phase 02's mechanical grading backtest, validation half only, matrix built
only from data available before that half -- no lookahead), the OLD literal
check flagged 23.4% of same-day cross-pair combinations as "correlated"
against only 9.7% for real correlation at the same >=0.60 threshold; of
everything the literal check flagged, only 39.2% (32,839/83,873) was also
flagged by real correlation -- the majority was pairs sharing a currency
code with ~zero real co-movement (e.g. EUR/USD + EUR/AUD, real corr +0.01).
Real correlation also caught 1,958 combinations the literal check
structurally cannot see at all, because they share no currency code (e.g.
NZD/USD + AUD/CAD, real corr +0.66 -- both commodity-bloc currencies against
different quotes).

Matrix refreshed at most every REFRESH_INTERVAL_DAYS from trailing
REFRESH_LOOKBACK of real daily closes (yfinance). Fails toward the OLD
literal check (the safe/conservative direction for a risk gate -- it
over-flags rather than under-flags) if the matrix is missing, too stale, or
a pair is outside the 28-pair UNIVERSE -- see are_correlated()'s docstring.
"""
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from src.selector import UNIVERSE

MATRIX_PATH = Path("data/correlation_matrix.json")
REFRESH_INTERVAL_DAYS = 7      # recompute at most this often (correlation drifts slowly)
MAX_STALE_DAYS = 45            # beyond this, treat matrix as unusable -> caller falls back
REFRESH_LOOKBACK = "2y"        # trailing window used for each recompute
MIN_PAIRS_FOR_VALID_REFRESH = int(len(UNIVERSE) * 0.8)   # tolerate a few fetch failures
CORRELATION_THRESHOLD = 0.60   # calibrated in scripts/correlation_investigation.py:
                                # >=0.60 was the stable/persistent bucket out-of-sample

_matrix_cache = None   # in-process cache of the loaded {pair: {pair: corr}} dict


def _yf_ticker(pair: str) -> str:
    return pair.replace("/", "") + "=X"


def _fetch_closes() -> pd.DataFrame:
    import yfinance as yf
    closes = {}
    for pair in UNIVERSE:
        try:
            df = yf.download(_yf_ticker(pair), period=REFRESH_LOOKBACK, interval="1d",
                              progress=False, auto_adjust=False)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [str(c).lower() for c in df.columns]
        if "close" in df.columns:
            closes[pair] = df["close"]
    return pd.DataFrame(closes)


STALE_ALERT_DAYS = REFRESH_INTERVAL_DAYS * 2   # 14 days -- a whole missed refresh cycle,
                                                # not a single transient fetch blip


def _current_matrix_age_days():
    """Age in days of whatever matrix is currently on disk, or None if there
    isn't one at all."""
    if not MATRIX_PATH.exists():
        return None
    try:
        payload = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        return (time.time() - payload.get("computed_at", 0)) / 86400.0
    except Exception:
        return None


def _alert_stale(reason: str, log) -> None:
    """Fires through the existing critical-severity channels (Discord +
    Telegram) -- mirrors src/reconciliation.py's _alert_critical(), same
    no-throttle convention (alerts every time the condition holds, not just
    once) so a sustained problem stays visible until it's actually fixed."""
    age = _current_matrix_age_days()
    age_str = f"{age:.1f} days old" if age is not None else "no matrix has ever been computed"
    title = "\U0001f4c9 CORRELATION MATRIX STALE — silently using literal-currency-code fallback"
    message = (
        f"{reason}\n\n"
        f"Current matrix: {age_str} (alert threshold: {STALE_ALERT_DAYS} days; "
        f"hard fallback at {MAX_STALE_DAYS} days).\n"
        f"apply_correlation_checks() (risk_manager.py) is falling back to the old "
        f"literal currency-code check for any pair combination the stale/missing "
        f"real correlation model can't answer -- position sizing is degraded, "
        f"not broken, but the real-correlation protection is not actually running."
    )
    try:
        from src import discord_notifier as _dn
        _dn.send_correlation_stale_alert(title, message)
    except Exception as exc:
        log(f"[correlation_model] Discord alert failed: {exc}")
    try:
        from src import telegram_alert as _ta
        _ta.send(f"{title}\n\n{message}")
    except Exception as exc:
        log(f"[correlation_model] Telegram alert failed: {exc}")


def refresh_correlation_matrix(force: bool = False, log=lambda *a: None) -> bool:
    """Recompute and persist the correlation matrix if stale.

    Returns True if a usable matrix exists on disk after this call (freshly
    computed, or already fresh enough that no recompute was needed), False
    only if a refresh was attempted and failed AND no prior matrix exists to
    fall back on.

    Alerts (Discord + Telegram, see _alert_stale()) whenever a refresh
    attempt fails AND the matrix currently on disk (or its total absence) is
    already past STALE_ALERT_DAYS -- a single missed refresh isn't alerted
    on (transient yfinance/network hiccups happen and the next scan a few
    hours later will simply retry), but a matrix that's been silently stale
    for two whole refresh cycles is exactly the "false confidence" failure
    mode this check exists to catch.
    """
    if MATRIX_PATH.exists() and not force:
        try:
            payload = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
            age_days = (time.time() - payload.get("computed_at", 0)) / 86400.0
            if age_days < REFRESH_INTERVAL_DAYS:
                return True
        except Exception:
            pass   # corrupt cache file -- fall through and try to recompute

    closes = _fetch_closes()
    if len(closes.columns) < MIN_PAIRS_FOR_VALID_REFRESH:
        log(f"[correlation_model] refresh failed: only {len(closes.columns)}/{len(UNIVERSE)} "
            f"pairs fetched -- keeping existing matrix if any")
        stale_age = _current_matrix_age_days()
        if stale_age is None or stale_age >= STALE_ALERT_DAYS:
            _alert_stale(
                f"Refresh attempt fetched only {len(closes.columns)}/{len(UNIVERSE)} "
                f"pairs (need >= {MIN_PAIRS_FOR_VALID_REFRESH}) -- refresh could not complete.",
                log,
            )
        return MATRIX_PATH.exists()

    rets = np.log(closes / closes.shift(1)).dropna(how="all")
    corr = rets.corr(min_periods=200)
    payload = {
        "computed_at": time.time(),
        "computed_date": pd.Timestamp.now("UTC").isoformat(),
        "lookback": REFRESH_LOOKBACK,
        "n_pairs": len(closes.columns),
        "matrix": corr.round(4).where(pd.notna(corr), None).to_dict(),
    }
    try:
        MATRIX_PATH.write_text(json.dumps(payload), encoding="utf-8")
        log(f"[correlation_model] refreshed matrix from {len(closes)} days, {len(closes.columns)} pairs")
    except OSError as e:
        log(f"[correlation_model] failed to persist matrix: {e}")
        stale_age = _current_matrix_age_days()
        if stale_age is None or stale_age >= STALE_ALERT_DAYS:
            _alert_stale(f"Refresh computed a fresh matrix but failed to persist it: {e}", log)
        return MATRIX_PATH.exists()

    global _matrix_cache
    _matrix_cache = None
    return True


def _load_matrix():
    global _matrix_cache
    if _matrix_cache is not None:
        return _matrix_cache
    if not MATRIX_PATH.exists():
        return None
    try:
        payload = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        age_days = (time.time() - payload.get("computed_at", 0)) / 86400.0
        if age_days > MAX_STALE_DAYS:
            return None
        _matrix_cache = payload["matrix"]
        return _matrix_cache
    except Exception:
        return None


def get_correlation(pair1: str, pair2: str):
    """Real correlation between two pairs' daily returns, or None if unavailable.

    Does NOT trigger a refresh itself (that would mean a network call on
    every pairwise lookup inside a scan's hot path, and would make this
    function's tests network-dependent) -- refresh_correlation_matrix() is
    called once per scan from daily.py's startup sequence, the same place
    market_regime.detect() and other once-per-scan state refreshes live.
    """
    if pair1 == pair2:
        return 1.0
    matrix = _load_matrix()
    if matrix is None:
        return None
    row = matrix.get(pair1)
    if row is not None and pair2 in row and row[pair2] is not None:
        return row[pair2]
    row = matrix.get(pair2)
    if row is not None and pair1 in row and row[pair1] is not None:
        return row[pair1]
    return None


def are_correlated(pair1: str, direction1: str, pair2: str, direction2: str,
                    threshold: float = CORRELATION_THRESHOLD):
    """Direction-adjusted position-return correlation check.

    A BUY earns the pair's raw return; a SELL earns its negative. Two
    positions reinforce the same risk when their direction-adjusted returns
    move together, so raw pairwise correlation is multiplied by each
    position's direction sign before comparing to `threshold`.

    Returns True/False, or None if no real correlation data is available for
    this pair combination (matrix missing/stale, or a pair outside
    UNIVERSE). Callers MUST fall back to the literal currency-code check in
    that case -- a risk gate should fail toward more caution, not silently
    skip the check.
    """
    corr = get_correlation(pair1, pair2)
    if corr is None:
        return None
    sign1 = 1 if direction1.upper() == "BUY" else -1
    sign2 = 1 if direction2.upper() == "BUY" else -1
    eff_corr = corr * sign1 * sign2
    return bool(eff_corr >= threshold)
