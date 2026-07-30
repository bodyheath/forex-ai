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
requirement can.

First attempt at that requirement counted distinct Monday-anchored
calendar weeks (>=4) touched by a bucket's close dates, and had a real
loophole: by 2026-07-14 the same bucket had grown to 48 trades and picked
up a 4th distinct week — satisfying that check — but 47 of those 48 (98%)
were still the exact same original burst; the "4th week" was a single new
trade that happened to land after a Monday boundary. A week-*count* can be
satisfied by one lucky boundary crossing without any real dilution.

MIN_BUCKET_SPAN_DAYS=21 replaces the week-count check with the thing it
was actually trying to measure: the calendar-day gap between the bucket's
earliest and latest close date. This is immune to boundary-crossing luck —
picking up one trade the day after a Monday doesn't move a day-count span
much, where it could flip a week-count from 3 to 4 outright. Verified
against the exact failing case: span was 11 days as of 2026-07-07 and 17
days as of 2026-07-14 (both correctly still excluded); by 2026-07-21 the
span reached 25 days with 22 of the bucket's 57 trades (39%) now postdating
the original burst — genuinely diluted, so it starts being trusted from
there. 21 days was chosen as the tightest value that still excludes the
known-bad case through 07-14; checked against today's full clean-window
history, mature/legitimate buckets range 22-34 days of span once enough
time has passed, so 21 does not meaningfully hold back a genuinely
diversified bucket once one exists.

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
MIN_BUCKET_SPAN_DAYS = 21   # earliest-to-latest close date must span this many days
_DECISIVE = {"WIN", "LOSS", "PARTIAL_WIN", "FULL_WIN"}
_WIN_STATUSES = {"WIN", "PARTIAL_WIN", "FULL_WIN"}

# Research trades before this date may be affected by the close-price bug
# (research_outcome_checker.py, fixed 2026-06-21) and/or the PARTIAL_WIN /
# financials rewrite (2026-06-24) — excluded so recalibration never trains
# on numbers that predate those fixes.
CLEAN_DATA_START = "2026-06-25"


def _has_gbp(pair: str) -> bool:
    return "GBP" in (pair or "").upper().split("/")


def build_calibration_table(as_of: datetime = None) -> dict:
    """Return a lookup table: {(confidence:int, direction, has_gbp:bool): win_rate}.

    Built only from research trades that (a) closed on/after CLEAN_DATA_START
    and (b) had already closed strictly before `as_of` — so a table built
    for "now" never includes an outcome that resolved after "now". Always
    includes an "_overall" key (the population mean) as the fallback for
    buckets that don't clear both MIN_BUCKET decisive trades and
    MIN_BUCKET_SPAN_DAYS between their earliest and latest close date, and
    as the return value when there isn't enough history at all yet.

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
    bucket_span: dict = {}   # key -> [earliest_closed_dt, latest_closed_dt]
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
        span = bucket_span.setdefault(key, [closed_dt, closed_dt])
        if closed_dt < span[0]:
            span[0] = closed_dt
        if closed_dt > span[1]:
            span[1] = closed_dt
        total_wins += int(is_win)
        total_n += 1

    if total_n == 0:
        return {}

    table = {"_overall": total_wins / total_n}
    for key, (wins, n) in buckets.items():
        earliest, latest = bucket_span[key]
        if n >= MIN_BUCKET and (latest - earliest).days >= MIN_BUCKET_SPAN_DAYS:
            table[key] = wins / n
    return table


def recalibrated_confidence(table: dict, confidence, direction: str, pair: str) -> float:
    """Look up the calibrated win probability (0.0-1.0) for one candidate.

    Falls back to the table's overall population mean if this exact
    (confidence, direction, has_gbp) bucket doesn't clear both MIN_BUCKET
    decisive trades and MIN_BUCKET_SPAN_DAYS of close-date spread, and to
    0.0 if the table itself is empty (no history yet, e.g. a fresh
    deployment) — callers should treat 0.0 as "no evidence to grant an
    override", not as an active penalty signal.
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
