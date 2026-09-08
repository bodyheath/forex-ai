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

Usage: python scripts/annotate_6987_data_anomaly_sep2026.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

import config

_TRADE_ID = 6987
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
    df = pd.read_csv(config.TRADES_CSV, encoding="utf-8-sig")
    mask = df["id"].astype(str) == str(_TRADE_ID)
    if not mask.any():
        print(f"Trade #{_TRADE_ID} not found -- nothing to do.")
        return
    before = df.loc[mask, "notes"].iloc[0]
    if _ANNOTATION.strip() in str(before):
        print(f"Trade #{_TRADE_ID} already annotated -- nothing to do.")
        return
    after = str(before) + _ANNOTATION
    df.loc[mask, "notes"] = after
    df.to_csv(config.TRADES_CSV, index=False)
    print(f"Trade #{_TRADE_ID} notes updated.")
    print(f"  before: {before!r}")
    print(f"  after:  {after!r}")


if __name__ == "__main__":
    run()
