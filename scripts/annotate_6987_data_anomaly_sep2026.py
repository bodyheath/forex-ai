"""One-off annotation for fund trade #6987 (USD/JPY, 2026-09-08), per the
2026-09-08 investigation into its stop-loss close.

Root cause (see conversation record / PR description, not restated here):
the entry price (156.197) was sourced from src/technical.py's
daily.last_close, itself sourced from Yahoo Finance's own daily bar for
2026-09-07 -- which disagreed with Yahoo's OWN hourly bars for the same
day by ~290 pips (1.875% of price), and with the scan's own
scan_price_snapshot.json live price (153.322) recorded moments earlier in
the same run. A real historical sweep (scripts/yahoo_daily_hourly_
discrepancy_sweep.py) confirms this is a routine, if occasional, feature
of Yahoo's daily FX data, not a one-off fluke.

Per direct instruction: the numeric fields (entry/stop_loss/exit_price/
pips/status/etc.) are NOT touched -- they reflect what actually,
operationally happened to the real fund and must stay as the true record.
Only the notes field is appended to, documenting the anomaly for anyone
reviewing the fund's track record later. Same append-don't-overwrite
notes-field pattern used in Phase 10's virtual-books stale-grade backfill
(scripts/backfill_stale_virtual_book_grades_sep2026.py).

Deliberately does NOT use pandas read_csv/to_csv for the write: a
pandas round-trip of this file infers per-column dtypes (several integer-
valued columns contain NaN in other rows, forcing float64) and rewrites
every "5" as "5.0" across THOUSANDS of unrelated rows -- confirmed by a
real before/after line diff during this script's own development, and
reverted before this version was written. csv.DictReader/DictWriter
preserve every field as the exact string it already was; only the one
target row's notes field is changed.

Usage: python scripts/annotate_6987_data_anomaly_sep2026.py
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config

_TRADE_ID = "6987"
_ANNOTATION = (
    " [2026-09-08 data-quality note: entry price sourced from a Yahoo "
    "Finance daily bar that disagreed with Yahoo's own hourly bars for "
    "the same day by ~290 pips (1.875% of price) -- a confirmed real "
    "data anomaly, not a strategy signal. See "
    "scripts/yahoo_daily_hourly_discrepancy_sweep.py and "
    "src/technical.py's _daily_close_sanity_check(), added after this "
    "incident, to guard future trades against the same failure mode. "
    "Numeric fields left untouched -- this reflects what actually "
    "happened to the real fund.]"
)


def run():
    with config.TRADES_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames
        rows = list(reader)

    target = next((r for r in rows if r.get("id") == _TRADE_ID), None)
    if target is None:
        print(f"Trade #{_TRADE_ID} not found -- nothing to do.")
        return
    before = target["notes"]
    if _ANNOTATION.strip() in before:
        print(f"Trade #{_TRADE_ID} already annotated -- nothing to do.")
        return
    after = before + _ANNOTATION
    target["notes"] = after

    with config.TRADES_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Trade #{_TRADE_ID} notes updated.")
    print(f"  before: {before!r}")
    print(f"  after:  {after!r}")


if __name__ == "__main__":
    run()
