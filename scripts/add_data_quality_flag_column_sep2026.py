"""One-off migration: add a `data_quality_flag` column to trades.csv and
flag trade #6987 as a confirmed data-quality incident.

2026-09-17: #6987 (USD/JPY, entered 2026-09-08 05:15:56) closed as a real
LOSS, but its recorded entry (156.197) and stop-loss exit (154.6068) were
never real, tradeable prices -- confirmed via two independent sources
(Yahoo Finance and Twelve Data, both 5-minute bars) showing real USD/JPY
traded a tight 153.27-153.86 the entire time, ~150-290 pips away from both
recorded levels. Real root cause: a stale Yahoo daily bar fed the entry
price (documented in the trade's own `notes` field already), but nothing
in trades.csv was ever machine-readable -- every real balance/streak/WR-PF
calculation has counted it identically to a genuine loss since the day it
closed. This gives it a real, structured flag those calculations can
finally see (see calculate_fund_state()'s exclusion logic, same commit).

Uses the csv module, not pandas, deliberately -- a prior pandas-based
annotation attempt on this same file silently reformatted thousands of
unrelated integer cells as floats on round-trip (see
scripts/annotate_6987_data_anomaly_sep2026.py's own history). The csv
module preserves every other field as its exact original string.

Swept the fund's full pre-fix-cutoff trade history (all 29 real fund
candidates entered before the 2026-09-08 21:52 UTC candle-timestamp-guard
fix) against real Yahoo Finance intraday data around each one's own entry
timestamp -- #6987 is the only one whose recorded entry sits meaningfully
outside where the pair was actually trading (1.88% off vs a ~0.2% worst
case among the other 28, itself explained by ordinary weekend-reopen
gaps). Only #6987 gets flagged here.
"""
import csv

TRADES_CSV = "data/trades.csv"
FLAGGED_IDS = {
    "6987": "stale_daily_bar_entry_price",
}


def main():
    with open(TRADES_CSV, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

    if "data_quality_flag" not in fieldnames:
        fieldnames.append("data_quality_flag")

    changed = 0
    for row in rows:
        row.setdefault("data_quality_flag", "")
        if row.get("id") in FLAGGED_IDS:
            row["data_quality_flag"] = FLAGGED_IDS[row["id"]]
            changed += 1

    with open(TRADES_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Added data_quality_flag column. Flagged {changed} row(s): {sorted(FLAGGED_IDS)}")


if __name__ == "__main__":
    main()
