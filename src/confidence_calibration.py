"""Maps raw AI-stated confidence to an empirically-grounded win probability.

Why this exists: raw confidence (from analyst.py's Haiku/Sonnet prompts) is
built as an additive technical-confluence score — RSI tier, MACD/BB/SMA50
bonuses, Fibonacci proximity, divergence, oscillator confluence, COT
reversal penalty — not a probability estimate. Measured correlation with
real outcomes on the clean (post-2026-06-25) research-trade history was
r=0.045, essentially zero, and confidence 8-9 performed *worse* than 5-7
during a recent poor-signal-quality window. Out-of-sample walk-forward
testing found that a bucketed empirical win rate, keyed on
(confidence, direction, GBP-involvement), recovers real predictive power:
r=0.010 (raw) -> 0.253 (recalibrated) on held-out weeks. Direction and GBP
were the two dimensions found to carry the signal in that investigation —
GBP-involved trades had roughly half the win rate of otherwise-identical
non-GBP trades, and BUY outperformed SELL by ~25 points across every
market regime tested.

min_bucket=20, not the more responsive-looking 5: at min_bucket=5, a few
buckets swung to a spurious 100% off tiny early samples that did not hold
up in later out-of-sample weeks — the exact same "unsustainable hot streak"
failure mode already diagnosed in dynamic_threshold.py's own win-rate
adjustment (see that module's comment on widening ITS window from 20 to 50
decisive trades for the identical reason). A bucket with fewer than 20
decisive trades is not trusted on its own; it falls back to the overall
population's win rate instead of reporting an unstable estimate.

min_bucket alone was not enough. A live re-verification found the
(confidence=7, BUY, non-GBP) bucket at 35 decisive trades — comfortably
over min_bucket=20 — reading 100% as of 2026-07-07, because all 35 opened
and closed inside the system's initial ~10-day unsustainable hot streak
(2026-06-25 to 2026-07-06). The following two weeks that same bucket lost
11/11 live. A count minimum cannot tell "35 trades spread across two
months" from "35 trades from one anomalous week" apart — only a time-span
requirement can. MIN_BUCKET_WEEKS=3 fixes this: a bucket is trusted only
when its decisive trades' close dates span at least 3 distinct calendar
weeks *in addition to* clearing MIN_BUCKET. Verified against the exact
failing case: as of 2026-07-07 that bucket's 35 trades spanned only 2
distinct close-weeks (06-23 and 06-30) — under the 3-week floor, so it
now correctly falls back to the overall population rate instead of
reporting a false 100%.

Walk-forward discipline, preserved going forward: build_calibration_table()
only ever counts a research trade if it had *already closed* strictly
before the `as_of` timestamp (defaults to the real clock at call time). A
candidate is never scored using outcomes that resolved after it existed.
There is no persisted/cached table and therefore no staleness to track —
every call reads the current data fresh. This is a stronger freshness
guarantee than a periodic (e.g. weekly) cache would give, at negligible
extra cost: the underlying data is a few thousand CSV rows and the whole
computation is well under a second.
"""

from datetime import datetime, timezone

MIN_BUCKET = 20
MIN_BUCKET_WEEKS = 3   # distinct close-date calendar weeks required, not just count
_DECISIVE = {"WIN", "LOSS", "PARTIAL_WIN", "FULL_WIN"}
_WIN_STATUSES = {"WIN", "PARTIAL_WIN", "FULL_WIN"}

# Research trades before this date may be affected by the close-price bug
# (research_outcome_checker.py, fixed 2026-06-21) and/or the PARTIAL_WIN /
# financials rewrite (2026-06-24) — excluded so recalibration never trains
# on numbers that predate those fixes.
CLEAN_DATA_START = "2026-06-25"


def _has_gbp(pair: str) -> bool:
    return "GBP" in (pair or "").upper().split("/")


def _week_key(dt: datetime):
    """Monday of dt's calendar week, as a date — a cheap, dependency-free
    stand-in for pandas' W-MON period start, used only to count how many
    distinct weeks a bucket's evidence is spread across."""
    from datetime import timedelta
    return (dt - timedelta(days=dt.weekday())).date()


def build_calibration_table(as_of: datetime = None) -> dict:
    """Return a lookup table: {(confidence:int, direction, has_gbp:bool): win_rate}.

    Built only from research trades that (a) closed on/after CLEAN_DATA_START
    and (b) had already closed strictly before `as_of` — so a table built
    for "now" never includes an outcome that resolved after "now". Always
    includes an "_overall" key (the population mean) as the fallback for
    buckets that don't clear MIN_BUCKET decisive trades AND MIN_BUCKET_WEEKS
    distinct close-weeks, and as the return value when there isn't enough
    history at all yet.

    Returns {} only if research trade history is unavailable/unreadable —
    callers should treat that the same as "no evidence", not as an error.
    """
    as_of = as_of or datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        from src import research_tracker
        rows = research_tracker.load()
    except Exception:
        return {}

    buckets: dict = {}       # key -> [wins, n]
    bucket_weeks: dict = {}  # key -> set of week-start dates
    total_wins = 0
    total_n = 0

    for r in rows:
        try:
            if str(r.get("date", "")) < CLEAN_DATA_START:
                continue
            status = str(r.get("status", "")).upper()
            if status not in _DECISIVE:
                continue
            closed_raw = r.get("closed_at") or ""
            if not closed_raw:
                continue
            closed_dt = datetime.strptime(str(closed_raw)[:19], "%Y-%m-%d %H:%M:%S")
            if closed_dt >= as_of:
                continue  # not yet resolved as of this call — walk-forward discipline
            conf = int(float(r.get("confidence") or 0))
            direction = str(r.get("direction", "")).upper()
            if conf <= 0 or direction not in ("BUY", "SELL"):
                continue
        except (TypeError, ValueError):
            continue

        is_win = status in _WIN_STATUSES
        key = (conf, direction, _has_gbp(r.get("pair", "")))
        b = buckets.setdefault(key, [0, 0])  # [wins, n]
        b[0] += int(is_win)
        b[1] += 1
        bucket_weeks.setdefault(key, set()).add(_week_key(closed_dt))
        total_wins += int(is_win)
        total_n += 1

    if total_n == 0:
        return {}

    table = {"_overall": total_wins / total_n}
    for key, (wins, n) in buckets.items():
        if n >= MIN_BUCKET and len(bucket_weeks.get(key, ())) >= MIN_BUCKET_WEEKS:
            table[key] = wins / n
    return table


def recalibrated_confidence(table: dict, confidence, direction: str, pair: str) -> float:
    """Look up the calibrated win probability (0.0-1.0) for one candidate.

    Falls back to the table's overall population mean if this exact
    (confidence, direction, has_gbp) bucket doesn't clear both MIN_BUCKET
    decisive trades and MIN_BUCKET_WEEKS distinct close-weeks, and to 0.0 if
    the table itself is empty (no history yet, e.g. a fresh deployment) —
    callers should treat 0.0 as "no evidence to grant an override", not as
    an active penalty signal.
    """
    if not table:
        return 0.0
    try:
        conf_i = int(float(confidence))
    except (TypeError, ValueError):
        return table.get("_overall", 0.0)
    direction = (direction or "").upper()
    key = (conf_i, direction, _has_gbp(pair))
    return table.get(key, table.get("_overall", 0.0))
