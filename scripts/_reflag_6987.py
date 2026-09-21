"""One-off: re-apply the data_quality_flag to #6987, silently dropped from
trades.csv on 2026-09-18 by a routine automated commit because the field was
never in tracker.FIELDS (now fixed). Restores the exact original value
confirmed from git history (commit f4c0e61b)."""
from src import tracker

tracker.update_fields(6987, data_quality_flag="stale_daily_bar_entry_price")

rows = tracker.load()
row = next((r for r in rows if str(r.get("id")) == "6987"), None)
print("id:", row.get("id"))
print("data_quality_flag:", repr(row.get("data_quality_flag")))
print("status:", row.get("status"), "pips:", row.get("pips"), "net_pips:", row.get("net_pips"))
