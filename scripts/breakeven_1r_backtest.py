"""Breakeven-at-+1R counterfactual backtest.

NEW ANALYSIS (2026-09-01) -- not a reproduction of an earlier, lost analysis
referenced in session history (methodology unrecoverable; see the 2026-09-01
full-system audit's Priority-3 follow-up for why). Written from scratch with
its own explicit methodology below, and saved here (not just run ad hoc in
the scratchpad) specifically so it can be re-run later without that problem
recurring.

METHODOLOGY (fixed and documented BEFORE results are read):

Population: strict accounting -- decisive (status in WIN, FULL_WIN, LOSS),
system_version=='v2', closed_at >= 2026-07-14 13:46:31 UTC (the exit-logic
fix cutoff used throughout this session). Further excludes ANY row where
mfe_pips exceeds t2_hit_pips by more than 5 pips -- the exact criterion the
2026-08-31 audit used to find 188/496 WIN/FULL_WIN rows contaminated by the
pre-2026-08-30 MFE-overaccumulation bug (see project_mfe_overaccumulation_
bug.md). Only WIN/FULL_WIN rows carry a t2_hit_pips anchor, so this
exclusion only ever removes WIN/FULL_WIN rows, never LOSS rows.

"Actual R": the existing r_multiple column, taken as-is (already the
realised, locked-in outcome per trade -- a LOSS is exactly -1.0 by
construction).

Breakeven-at-+1R rule being tested: "if this system had moved the stop to
breakeven as soon as price reached +1R favourable excursion, what would the
average outcome have been instead?" Implemented as: for every row, convert
mfe_pips to mfe_R using the row's own stop distance (|entry - stop_loss| /
pip size, where pip size is 0.01 for JPY-quoted pairs and 0.0001 otherwise).
A row is "rescued" -- its simulated outcome becomes exactly 0R instead of
its actual R -- if and only if actual_R < 0 (it was a real loss) AND
mfe_R >= 1.0 (price had genuinely reached +1R before eventually reversing
into a loss). WIN/FULL_WIN rows, and LOSS rows that never reached +1R, are
left at their actual R in the simulation -- the rule only ever helps, never
hurts, by construction (a real limitation: it cannot model a rescued trade
later re-entering and running further, or slippage on the breakeven exit
itself; treat the result as an optimistic upper bound on what a real
breakeven-stop rule would achieve).

Comparison: mean(actual_R) vs mean(simulated_R) across the population, plus
the count of rescued rows, reported for three exclusion levels so the
sensitivity of the conclusion to the MFE contamination itself is visible:
  1. All strict-population rows, no MFE-contamination exclusion at all.
  2. Excluding only the two previously-known contaminated batches
     (2026-07-30, 2026-08-19) -- the exclusion level a prior, now-
     unreproducible analysis in this session used.
  3. Excluding ALL 188-criterion-contaminated rows (the full, correct
     exclusion) -- this is the analysis this script exists to answer.

Usage: python scripts/breakeven_1r_backtest.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CUTOFF = pd.Timestamp("2026-07-14 13:46:31", tz="UTC")
CONTAMINATION_PIPS = 5  # mfe_pips - t2_hit_pips > this => contaminated (WIN/FULL_WIN only)
KNOWN_BAD_DATES = {"2026-07-30", "2026-08-19"}


def _pip_size(pair: str) -> float:
    return 0.01 if str(pair).upper().endswith("JPY") else 0.0001


def load_strict_population(csv_path: str = "data/research_trades.csv") -> pd.DataFrame:
    df = pd.read_csv(csv_path, low_memory=False)
    status_u = df["status"].astype(str).str.upper()
    closed_dt = pd.to_datetime(df["closed_at"], errors="coerce", utc=True)
    strict = df[
        status_u.isin(["WIN", "FULL_WIN", "LOSS"])
        & (df["system_version"] == "v2")
        & (closed_dt >= CUTOFF)
    ].copy()
    strict["status_u"] = strict["status"].astype(str).str.upper()
    strict["closed_dt"] = pd.to_datetime(strict["closed_at"], errors="coerce", utc=True)
    strict["pip_sz"] = strict["pair"].apply(_pip_size)
    strict["stop_dist_pips"] = (
        pd.to_numeric(strict["entry"], errors="coerce")
        - pd.to_numeric(strict["stop_loss"], errors="coerce")
    ).abs() / strict["pip_sz"]
    strict["mfe_R"] = pd.to_numeric(strict["mfe_pips"], errors="coerce") / strict["stop_dist_pips"]
    strict["actual_R"] = pd.to_numeric(strict["r_multiple"], errors="coerce")
    strict["t2n"] = pd.to_numeric(strict.get("t2_hit_pips"), errors="coerce")
    strict["mfen"] = pd.to_numeric(strict["mfe_pips"], errors="coerce")
    strict["contaminated"] = strict["status_u"].isin(["WIN", "FULL_WIN"]) & (
        (strict["mfen"] - strict["t2n"]) > CONTAMINATION_PIPS
    )
    return strict


def simulate_breakeven_at_1r(pop: pd.DataFrame) -> pd.DataFrame:
    pop = pop.copy()
    rescued = (pop["actual_R"] < 0) & (pop["mfe_R"] >= 1.0)
    pop["sim_R"] = pop["actual_R"].where(~rescued, 0.0)
    pop["rescued"] = rescued
    return pop


def report(pop: pd.DataFrame, label: str) -> None:
    sim = simulate_breakeven_at_1r(pop)
    n = len(sim)
    actual_mean = sim["actual_R"].mean()
    sim_mean = sim["sim_R"].mean()
    print(f"--- {label} (n={n}) ---")
    print(f"  actual mean R:     {actual_mean:.4f}")
    print(f"  simulated mean R:  {sim_mean:.4f}")
    print(f"  delta:             {sim_mean - actual_mean:+.4f}")
    print(f"  rescued losses:    {int(sim['rescued'].sum())}")
    print()


def main() -> None:
    strict = load_strict_population()
    print(f"Strict population (v2, decisive, closed_at >= {CUTOFF}): n={len(strict)}")
    print(f"Rows meeting the {CONTAMINATION_PIPS}-pip MFE-contamination criterion: "
          f"{int(strict['contaminated'].sum())}")
    print()

    report(strict, "1. ALL strict-population rows (no MFE exclusion)")

    known_bad = strict["closed_dt"].dt.date.astype(str).isin(KNOWN_BAD_DATES)
    report(strict[~known_bad], "2. Excluding only the 2 known-bad batches (partial exclusion)")

    report(strict[~strict["contaminated"]], "3. Excluding ALL 188-criterion-contaminated rows (full exclusion)")


if __name__ == "__main__":
    main()
