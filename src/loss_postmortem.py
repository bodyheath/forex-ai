"""Loss postmortem classifier.

Buckets every real loss (and every EXPIRED/PARTIAL_WIN/STALE_EXIT/BREAKEVEN
close that nonetheless resolved net-negative) into one of three failure
modes, using only fields whose reliability window is checked per row rather
than assumed:

    THESIS_WRONG
        Price moved against the setup from close to the start -- little or
        no favourable excursion was ever recorded (mfe_r < _THESIS_WRONG_MFE_R,
        where mfe_r = mfe_pips / stop_distance_pips, i.e. MFE expressed as a
        fraction of the risk taken).

    TIMING_WRONG
        The thesis was directionally validated -- price reached at least a
        full risk-unit in the trade's favour (mfe_r >= _TIMING_WRONG_MFE_R)
        -- before the trade still closed as a loss. An exit/management
        failure, not a direction failure.

    NORMAL_VARIANCE
        The residual middle ground: some favourable movement occurred, not
        enough to call it a timing failure, AND -- wherever the fields exist
        to check -- no known red flag (grade D/F, da_fired, rib_against,
        w_d_conflict) was present at entry. "Normal variance on a validated
        pattern" requires the pattern to actually have been clean; a red flag
        in this middle zone is reclassified down to THESIS_WRONG instead,
        since a setup with a known weakness that only got halfway there is
        better explained as a thesis-quality issue than bad luck on a good
        setup.

Two explicit non-blending labels cover the cases above can't honestly reach:

    INSUFFICIENT_DATA
        mfe_pips/mae_pips (this classifier's primary signal) aren't
        reliable for this row -- either missing outright, or the row closed
        before the MFE-overaccumulation fix (see
        project_mfe_overaccumulation_bug.md). There is no defensible
        fallback without real excursion data, so this is reported
        separately rather than guessed at.

    NORMAL_VARIANCE_UNREFINED
        The row lands in the middle MFE zone (real, reliable excursion data
        says so) but the grade/DA/ribbon fields needed to confirm the
        pattern was actually clean aren't populated for this row (real-fund
        trades.csv never has them at all; research trades opened before the
        fields existed don't either). Classified, but flagged as
        not cross-checked.

Reliability gates below were verified directly against research_trades.csv
on 2026-09-06 (`git log`/direct field-population checks), not assumed from
memory alone -- notably, da_fired turned out to start 5 days earlier
(2026-08-27) than rib_against/w_d_conflict/atr_cal (2026-09-01), which the
data confirmed but a single blended cutoff would have hidden.
"""

from datetime import datetime

_MFE_MAE_RELIABLE_FROM = "2026-08-30"   # gates on the row's own closed_at
_DA_RELIABLE_FROM       = "2026-08-27"  # gates on the row's own entry date -- currently
                                          # informational only (da_fired isn't used as an
                                          # independent gate below; see _RIBBON_RELIABLE_FROM)
_RIBBON_RELIABLE_FROM   = "2026-09-01"  # gates on the row's own entry date -- covers
                                          # rib_against/w_d_conflict/atr_cal AND da_fired,
                                          # since all four are read together as one red-flag
                                          # check and da_fired alone isn't informative without
                                          # the other three also being available for the same row

_THESIS_WRONG_MFE_R = 0.3   # below this: never gained real traction
_TIMING_WRONG_MFE_R = 1.0   # at/above this: reached a full risk-unit before reversing

# Statuses this classifier ever applies to. LOSS is unconditionally in scope
# (it's decisive by definition); the rest only count when they closed
# net-negative -- an EXPIRED or PARTIAL_WIN that closed net-positive isn't a
# loss of any kind and this classifier has nothing to say about it.
_LOSS_RELEVANT_STATUSES = {"LOSS", "EXPIRED", "PARTIAL_WIN", "STALE_EXIT", "BREAKEVEN"}

FAILURE_MODES = (
    "THESIS_WRONG", "TIMING_WRONG", "NORMAL_VARIANCE",
    "NORMAL_VARIANCE_UNREFINED", "INSUFFICIENT_DATA",
)


def _to_float(v):
    try:
        if v in (None, ""):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _pip_size(pair: str) -> float:
    from src.research_tracker import _pip_size as _ps
    return _ps(pair)


def _date_ge(value: str, cutoff: str) -> bool:
    v = (value or "")[:10]
    if not v:
        return False
    try:
        return datetime.strptime(v, "%Y-%m-%d") >= datetime.strptime(cutoff, "%Y-%m-%d")
    except ValueError:
        return False


def _entry_date(row: dict) -> str:
    # research_trades.csv uses "date"; trades.csv (real fund) uses "timestamp".
    return row.get("date") or row.get("timestamp") or ""


def classify_loss_failure_mode(row: dict) -> str | None:
    """Classify one closed trade row. Returns a FAILURE_MODES value, or None
    if the row isn't loss-relevant at all (a WIN, an OPEN/PENDING row, or a
    non-LOSS status that actually closed net-positive/flat).

    Works on both research_trades.csv rows (full field set) and trades.csv
    (real fund) rows (mfe_pips/mae_pips only -- grade/da_fired/rib_against/
    w_d_conflict/atr_cal don't exist there, so real-fund middle-zone rows
    always land on NORMAL_VARIANCE_UNREFINED, never NORMAL_VARIANCE).
    """
    status = str(row.get("status", "")).upper()
    if status not in _LOSS_RELEVANT_STATUSES:
        return None

    net_pips = _to_float(row.get("net_pips"))
    if net_pips is None:
        net_pips = _to_float(row.get("pips"))
    if status != "LOSS":
        if net_pips is None or net_pips >= 0:
            return None

    if not _date_ge(row.get("closed_at", ""), _MFE_MAE_RELIABLE_FROM):
        return "INSUFFICIENT_DATA"

    mfe   = _to_float(row.get("mfe_pips"))
    entry = _to_float(row.get("entry"))
    stop  = _to_float(row.get("stop_loss"))
    if mfe is None or entry is None or stop is None:
        return "INSUFFICIENT_DATA"

    pip_size = _pip_size(row.get("pair", ""))
    stop_dist_pips = abs(entry - stop) / pip_size if pip_size else 0
    if stop_dist_pips <= 0:
        return "INSUFFICIENT_DATA"

    mfe_r = mfe / stop_dist_pips

    if mfe_r >= _TIMING_WRONG_MFE_R:
        return "TIMING_WRONG"
    if mfe_r < _THESIS_WRONG_MFE_R:
        return "THESIS_WRONG"

    # Middle zone (_THESIS_WRONG_MFE_R <= mfe_r < _TIMING_WRONG_MFE_R): only
    # "normal variance on a validated pattern" if the pattern was actually
    # clean -- check the red-flag fields where this row's entry date makes
    # them available.
    if not _date_ge(_entry_date(row), _RIBBON_RELIABLE_FROM):
        return "NORMAL_VARIANCE_UNREFINED"

    grade        = str(row.get("grade", "")).upper().strip()
    da_fired     = str(row.get("da_fired", "")).upper() == "TRUE"
    rib_against  = str(row.get("rib_against", "")).upper() == "TRUE"
    w_d_conflict = str(row.get("w_d_conflict", "")).upper() == "TRUE"
    red_flag = da_fired or rib_against or w_d_conflict or grade in ("D", "F")
    return "THESIS_WRONG" if red_flag else "NORMAL_VARIANCE"


def mfe_r(row: dict) -> float | None:
    """Expose mfe expressed as a fraction of risk taken, for reporting --
    same computation classify_loss_failure_mode() uses internally."""
    mfe   = _to_float(row.get("mfe_pips"))
    entry = _to_float(row.get("entry"))
    stop  = _to_float(row.get("stop_loss"))
    if mfe is None or entry is None or stop is None:
        return None
    pip_size = _pip_size(row.get("pair", ""))
    stop_dist_pips = abs(entry - stop) / pip_size if pip_size else 0
    if stop_dist_pips <= 0:
        return None
    return mfe / stop_dist_pips
