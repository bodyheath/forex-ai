"""One-time backfill (2026-09-09) for the two candidates.csv rows confirmed
affected by the pre-fix stale-descriptive-field bug (see
src/virtual_books.py's module docstring, STALE DESCRIPTIVE FIELDS section,
and project_virtual_books_stale_grade_fix_sep2026.md): candidate #8
(AUD/NZD SELL) and #11 (GBP/CAD BUY), both created before the fix shipped
and therefore never refreshed by it (the fix only touches a row the next
time it's re-evaluated, and both rows are still OPEN from a date the fix
will never revisit).

============================================================================
WHAT THIS DOES AND DOES NOT CORRECT -- PARTIAL, DISCLOSED, NOT A FULL FIX
============================================================================
A real, independent source exists for SOME of the stale fields:
data/research_trades.csv logs every Sonnet-analysed candidate on its own
pipeline, including repeat same-day analyses of the same pair, with real
grade/da_grade_before/confidence values at each analysis. Matching that
log's rows for AUD/NZD SELL and GBP/CAD BUY around 2026-09-06/07 shows a
real, verifiable progression:

  AUD/NZD SELL: conf=7 grade=F  da_before=D   (2026-09-06, matches the
                                                candidate's own frozen
                                                creation-time values exactly)
                conf=7 grade=D  da_before=C   (2026-09-07, real re-analysis)
                conf=8 grade=C  da_before=B   (2026-09-07, real re-analysis,
                                                LATEST -- this is what this
                                                script backfills to)

  GBP/CAD BUY:  conf=6 grade=D  da_before=D   (2026-09-06, matches creation)
                conf=7 grade=D  da_before=C   (2026-09-07, LATEST)

These three fields (confidence, grade, da_grade_before) are backfilled to
their LATEST real value -- matching the convention the live fix now
establishes going forward (a candidate's stored fields always reflect the
most recent real analysis, not necessarily the exact analysis that
admitted every individual book).

NOT backfilled, deliberately: `eff_conf` and `mtf_agreeing_count`.
research_trades.csv does not carry either field, so there is no
independent source to verify what they were on the later analyses --
writing a guessed value would be fabricating a number, not correcting one.
Both are left at their original (stale, first-creation) values. `rr` is
NOT touched either, but for a different reason: it's mechanically fixed at
2.0 by cascade.py's construction regardless of grade, so the stored 2.0 was
never wrong.

ALSO NOT RESOLVED: even the backfilled "latest real" values only reflect
the analysis in effect by the time the LAST book admitted this candidate
(A_control/C_grade_based/E_no_dd_gate, 2026-09-07 11:18, for #8). They do
NOT necessarily reflect the analysis that admitted the EARLIER-admitting
books (B_conf6_rr15/D_no_da, 05:19 for #8) -- a single shared row cannot
represent two distinct real admitting moments for two different books.
This is disclosed in the `notes` field written to each row, not hidden.

Usage: python scripts/backfill_stale_virtual_book_grades_sep2026.py
"""
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import virtual_books as vb

_NOTE_8 = (
    "2026-09-09 backfill: grade/da_grade_before/confidence corrected from "
    "the pre-fix frozen creation-time values (F/D/7) to the LATEST real "
    "analysis (C/B/8), cross-referenced against data/research_trades.csv's "
    "independent log of the same real Sonnet analyses that day. This is "
    "what admitted A_control/C_grade_based/E_no_dd_gate at 11:18 -- it does "
    "NOT necessarily reflect the earlier real analysis (D/C/7) that "
    "admitted B_conf6_rr15/D_no_da at 05:19 that same morning; a single "
    "shared row can't represent two distinct admitting moments. eff_conf "
    "and mtf_agreeing_count are UNCHANGED and UNVERIFIED -- no independent "
    "source exists for either. See project_virtual_books_stale_grade_fix_"
    "sep2026.md for the full investigation."
)
_NOTE_11 = (
    "2026-09-09 backfill: da_grade_before/confidence corrected from the "
    "pre-fix frozen creation-time values (D/6) to the LATEST real analysis "
    "(C/7), cross-referenced against data/research_trades.csv. grade itself "
    "(D) was already correct -- it never changed between analyses. This is "
    "what admitted D_no_da at 05:19. eff_conf and mtf_agreeing_count are "
    "UNCHANGED and UNVERIFIED -- no independent source exists for either. "
    "See project_virtual_books_stale_grade_fix_sep2026.md."
)

_CORRECTIONS = {
    8:  {"confidence": 8, "grade": "C", "da_grade_before": "B", "notes": _NOTE_8},
    11: {"confidence": 7, "grade": "D", "da_grade_before": "C", "notes": _NOTE_11},
}


def run():
    candidates = vb._load_csv(vb.CANDIDATES_CSV)
    changed = False
    for row in candidates:
        cid = int(row["id"]) if str(row.get("id", "")).isdigit() else None
        if cid not in _CORRECTIONS:
            continue
        before = dict(row)
        for field, value in _CORRECTIONS[cid].items():
            row[field] = value
        changed = True
        print(f"candidate #{cid} ({row.get('pair')} {row.get('direction')}):")
        for field in ("confidence", "grade", "da_grade_before"):
            print(f"  {field}: {before.get(field)!r} -> {row.get(field)!r}")
        print(f"  notes: {row['notes'][:80]}...")
        print()

    if not changed:
        print("No matching rows found -- nothing to backfill (already run?).")
        return

    vb._write_csv(vb.CANDIDATES_CSV, candidates, vb.CANDIDATE_FIELDS)
    print(f"Wrote {vb.CANDIDATES_CSV}")


if __name__ == "__main__":
    run()
