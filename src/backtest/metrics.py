"""Shared win-rate/profit-factor/expectancy computation, with mandatory
sample-size and statistical-validity context — same caution standard used
throughout this project's other analyses (chi-square validity checks,
n<20/n<50 sample-size flags).
"""

import pandas as pd
from scipy import stats

# Sample-size thresholds. Below MIN_MEANINGFUL, a result is not reported as
# meaningful at all; below MIN_COMFORTABLE, it's reported but flagged as
# directional-only.
MIN_MEANINGFUL = 20
MIN_COMFORTABLE = 50

# Standard chi-square validity rule of thumb: every expected cell should be >= 5.
MIN_EXPECTED_CELL = 5


def group_stats(df: pd.DataFrame) -> dict:
    """n / W / L / win rate / profit factor / expectancy for one group."""
    n = len(df)
    if n == 0:
        return {
            "n": 0, "wins": 0, "losses": 0, "win_rate_pct": None,
            "profit_factor": None, "expectancy_r": None,
            "sample_size_flag": "EMPTY — no trades in this group",
        }

    wins = int((df["outcome"] == "WIN").sum())
    losses = n - wins
    win_rate = wins / n * 100

    gross_win_r = df.loc[df["outcome"] == "WIN", "r_multiple"].sum()
    gross_loss_r = df.loc[df["outcome"] == "LOSS", "r_multiple"].sum()
    profit_factor = (gross_win_r / abs(gross_loss_r)) if gross_loss_r != 0 else None

    expectancy = df["r_multiple"].mean()

    if n < MIN_MEANINGFUL:
        flag = (
            f"TOO SMALL (n={n} < {MIN_MEANINGFUL}) — do not treat this result "
            f"as meaningful, report only alongside the raw counts"
        )
    elif n < MIN_COMFORTABLE:
        flag = (
            f"SMALL (n={n} < {MIN_COMFORTABLE}) — directional only, treat with caution"
        )
    else:
        flag = f"n={n} — usable sample size, still check the significance test below"

    return {
        "n": n, "wins": wins, "losses": losses,
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
        "expectancy_r": round(float(expectancy), 3) if pd.notna(expectancy) else None,
        "sample_size_flag": flag,
    }


def compare_groups(included: pd.DataFrame, excluded: pd.DataFrame) -> dict:
    """2x2 chi-square (included vs excluded) x (WIN vs LOSS), with validity check.

    Compares the two mutually-exclusive halves of the hypothesis split, not
    the included subset against its own superset (which isn't a valid
    independent-samples comparison since one contains the other).
    """
    if len(included) == 0 or len(excluded) == 0:
        return {
            "test": "chi-square (included vs excluded)",
            "applicable": False,
            "reason": "one side of the split is empty — no comparison possible",
        }

    ct = pd.DataFrame({
        "WIN": [
            int((included["outcome"] == "WIN").sum()),
            int((excluded["outcome"] == "WIN").sum()),
        ],
        "LOSS": [
            int((included["outcome"] == "LOSS").sum()),
            int((excluded["outcome"] == "LOSS").sum()),
        ],
    }, index=["included", "excluded"])

    try:
        chi2, p, dof, expected = stats.chi2_contingency(ct)
    except ValueError as e:
        return {
            "test": "chi-square (included vs excluded)",
            "applicable": False,
            "reason": f"could not compute ({e})",
        }

    min_expected = float(expected.min())
    valid = min_expected >= MIN_EXPECTED_CELL

    return {
        "test": "chi-square (included vs excluded)",
        "applicable": True,
        "chi2": round(float(chi2), 4),
        "dof": int(dof),
        "p_value": round(float(p), 5),
        "min_expected_cell": round(min_expected, 2),
        "valid_test": valid,
        "validity_note": (
            "valid — all expected cells >= 5"
            if valid else
            f"INVALID / UNRELIABLE — minimum expected cell ({min_expected:.2f}) is "
            f"below the standard chi-square validity threshold of {MIN_EXPECTED_CELL}; "
            f"this p-value should not be trusted at this sample size"
        ),
        "significant_at_0.05": bool(p < 0.05) if valid else "not applicable (invalid test)",
    }
