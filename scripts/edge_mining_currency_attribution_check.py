"""Currency/direction attribution stability check for the mechanical
edge-mining research loop's validated signals (Book G's mr_stack_2way,
Book H's osc_agrees-alone, Book I's bb_extreme_agrees).

Reconstructs, with real numbers, the "currency attribution instability"
finding that kept Book G both-direction rather than BUY-only-restricted
(discovery and holdout disagreed on which currency drove the BUY-only
slice's apparent edge -- see PROMOTION_DISCIPLINE.md's vbook_G entry). That
check was originally done ad hoc and never committed; this script commits
it, and extends it to Books H and I before their own build decision.

Two checks per signal:
  1. Overall BUY vs SELL, discovery vs holdout -- is there a real,
     STABLE directional skew (matches the known baseline BUY>SELL skew,
     harmless), or an unstable one?
  2. Per-currency WR/PF ranking of the hypothetical BUY-only slice,
     discovery vs holdout -- does the SAME currency drive the effect in
     both slices, or does the ranking flip (the actual Book G finding)?
     Also run on the FULL (both-direction, as actually built) population,
     since that's what matters for the books as proposed.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "mechanical_edge_mining_dataset.csv"


def wr_pf_rows(pips):
    n = len(pips)
    if n == 0:
        return 0, float("nan"), float("nan")
    wins = sum(1 for p in pips if p > 0)
    wr = wins / n * 100
    gp = sum(p for p in pips if p > 0)
    gl = -sum(p for p in pips if p < 0)
    pf = gp / gl if gl > 0 else float("inf")
    return n, wr, pf


def per_currency(d, min_n=20):
    ccy_pips = {}
    for _, row in d.iterrows():
        for ccy in row["pair"].split("/"):
            ccy_pips.setdefault(ccy, []).append(row["net_pips"])
    rows = []
    for ccy, pips in ccy_pips.items():
        n, wr, pf = wr_pf_rows(pips)
        if n < min_n:
            continue
        rows.append((ccy, n, wr, pf))
    rows.sort(key=lambda r: -r[2])
    return rows


def check_signal(name, sig_series, df, baseline_wr=44.3):
    print(f"##### {name} #####")
    print("--- overall BUY vs SELL, discovery vs holdout ---")
    for split in ("discovery", "holdout"):
        d = df[(df["split"] == split) & sig_series]
        for direction in ("BUY", "SELL"):
            sub = d[d["direction"] == direction]
            n, wr, pf = wr_pf_rows(sub["net_pips"].tolist())
            print(f"  {split:<10} {direction}: n={n} WR={wr:.1f}% PF={pf:.3f}")
    print()

    print("--- per-currency, BUY-only slice (hypothetical refinement), discovery vs holdout ---")
    for split in ("discovery", "holdout"):
        d = df[(df["split"] == split) & sig_series & (df["direction"] == "BUY")]
        rows = per_currency(d)
        top3 = [r[0] for r in rows[:3]]
        bot3 = [r[0] for r in rows[-3:]]
        print(f"  {split}: top WR={top3}  bottom WR={bot3}")
    print()

    print("--- per-currency, FULL both-direction population (as actually built), discovery vs holdout ---")
    for split in ("discovery", "holdout"):
        d = df[(df["split"] == split) & sig_series]
        rows = per_currency(d)
        n_below = sum(1 for r in rows if r[2] < baseline_wr)
        print(f"  {split}: {n_below}/{len(rows)} currencies below baseline WR ({baseline_wr}%)")
        for ccy, n, wr, pf in rows:
            flag = "  <-- below baseline" if wr < baseline_wr else ""
            print(f"    {ccy:<5} n={n:>4} WR={wr:>5.1f}% PF={pf:.3f}{flag}")
    print()


def main():
    df = pd.read_csv(DATA_PATH)
    df["mr_stack_2way"] = df["rib_against"] & df["osc_agrees"]
    df["bb_extreme_agrees"] = (
        ((df["direction"] == "BUY") & (df["bb_position"] <= 0.2)) |
        ((df["direction"] == "SELL") & (df["bb_position"] >= 0.8))
    )
    check_signal("Book G (mr_stack_2way)", df["mr_stack_2way"], df)
    check_signal("Book H (osc_agrees alone)", df["osc_agrees"], df)
    check_signal("Book I (bb_extreme_agrees)", df["bb_extreme_agrees"], df)


if __name__ == "__main__":
    main()
