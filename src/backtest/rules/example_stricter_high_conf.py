"""Example custom rule — demonstrates the Level-2 escape hatch.

Not used by the two approved v1 worked examples (both are plain Level-1
filters); included so the extensibility mechanism is proven to work, not
just described in the design doc.
"""


def apply(row: dict) -> bool:
    """Would this trade still have been taken if conf>=8 also required fund_score>=6?

    Reflects the earlier diagnostic finding that conf 8 underperformed —
    this asks whether tightening high-confidence entries with a fundamental-
    alignment co-requirement would have screened any of them out.
    """
    conf = row.get("confidence")
    if conf is not None and conf >= 8:
        fund_score = row.get("fund_score")
        if fund_score is None:
            return True  # missing data — don't exclude on an unknown value
        return fund_score >= 6
    return True
