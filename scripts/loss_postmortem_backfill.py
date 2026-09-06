"""One-time backfill: apply src/loss_postmortem.py's classifier to every
historical loss-relevant row in research_trades.csv (persisted) and
trades.csv (report-only -- see below for why it isn't persisted there).

research_trades.csv: writes the loss_failure_mode column directly, in one
bulk rewrite (not one update_fields() call per row -- that pattern already
caused a runaway multi-thousand-line bloat once this session on a similar
backfill; see project memory on the shadow_mode backfill incident). New
closes are classified live going forward by research_outcome_checker.py's
_record_loss_classification() -- this script only needs to run once, for
history.

trades.csv (real fund): NOT persisted as a new column -- the real fund's
tracker.py/outcome_checker.py were deliberately left unmodified (the task
scope was "wire it into research-trade logging", not the fund's), and
trades.csv structurally lacks grade/da_fired/rib_against/w_d_conflict/
atr_cal entirely, so every real-fund middle-zone row would be
NORMAL_VARIANCE_UNREFINED regardless. Written instead to
data/loss_postmortem_fund_report.csv as a standalone reference artifact.

Prints the full breakdown (by bucket, and by grade/regime/pair for
"does thesis-wrong concentrate anywhere" -- research trades only, since
that's the only population with grade/regime fields at all).
"""
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import loss_postmortem as lpm
from src import research_tracker


def backfill_research() -> list:
    rows = research_tracker.load()
    changed = 0
    classified = []
    for row in rows:
        mode = lpm.classify_loss_failure_mode(row)
        if mode:
            row["loss_failure_mode"] = mode
            classified.append(row)
            changed += 1
        # Leave existing loss_failure_mode alone for non-applicable rows
        # (there shouldn't be any pre-existing values -- this is the first
        # run -- but don't clobber anything unexpected either).
    research_tracker._write_all(rows)
    print(f"[research_trades.csv] {changed} rows classified and written "
          f"(bulk rewrite, {len(rows)} total rows).")
    return classified


def classify_fund() -> list:
    fund_path = Path("data/trades.csv")
    with fund_path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    out_rows = []
    for row in rows:
        mode = lpm.classify_loss_failure_mode(row)
        if mode:
            out_rows.append({
                "id": row.get("id"), "pair": row.get("pair"),
                "direction": row.get("direction"), "status": row.get("status"),
                "closed_at": row.get("closed_at"), "net_pips": row.get("net_pips"),
                "mfe_pips": row.get("mfe_pips"), "mae_pips": row.get("mae_pips"),
                "mfe_r": lpm.mfe_r(row),
                "loss_failure_mode": mode,
            })
    out_path = Path("data/loss_postmortem_fund_report.csv")
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()) if out_rows else
                                 ["id", "pair", "direction", "status", "closed_at",
                                  "net_pips", "mfe_pips", "mae_pips", "mfe_r", "loss_failure_mode"])
        writer.writeheader()
        writer.writerows(out_rows)
    print(f"[trades.csv / real fund] {len(out_rows)} rows classified, "
          f"written to {out_path} (report-only, not persisted to trades.csv).")
    return out_rows


def report_breakdown(label: str, classified: list) -> None:
    print(f"\n=== {label}: failure-mode breakdown (n={len(classified)}) ===")
    counts = Counter(r.get("loss_failure_mode") for r in classified)
    for mode in lpm.FAILURE_MODES:
        n = counts.get(mode, 0)
        pct = (n / len(classified) * 100) if classified else 0
        print(f"  {mode:28s} {n:5d}  ({pct:5.1f}%)")


def report_concentration(classified: list) -> None:
    """Where does THESIS_WRONG concentrate? Research trades only -- the only
    population with grade/regime/pair fields alongside the classification."""
    substantive = [r for r in classified
                   if r.get("loss_failure_mode") in
                   ("THESIS_WRONG", "TIMING_WRONG", "NORMAL_VARIANCE", "NORMAL_VARIANCE_UNREFINED")]
    if not substantive:
        print("\n(no substantively-classified research rows to check concentration on)")
        return

    for dim in ("grade", "market_regime", "pair"):
        print(f"\n--- THESIS_WRONG share by {dim} (n>=8 only) ---")
        by_dim = defaultdict(lambda: Counter())
        for r in substantive:
            key = (r.get(dim) or "").strip() or "(blank)"
            by_dim[key]["total"] += 1
            if r.get("loss_failure_mode") == "THESIS_WRONG":
                by_dim[key]["thesis_wrong"] += 1
        rows_out = []
        for key, c in by_dim.items():
            if c["total"] >= 8:
                rows_out.append((key, c["thesis_wrong"], c["total"], c["thesis_wrong"] / c["total"] * 100))
        rows_out.sort(key=lambda x: -x[3])
        for key, n_tw, n_tot, pct in rows_out:
            print(f"  {key:20s} {n_tw:4d}/{n_tot:4d}  ({pct:5.1f}%)")


if __name__ == "__main__":
    research_classified = backfill_research()
    fund_classified = classify_fund()
    report_breakdown("research_trades.csv", research_classified)
    report_breakdown("trades.csv (real fund)", fund_classified)
    report_concentration(research_classified)
