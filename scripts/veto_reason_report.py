"""On-demand report: aggregate fund-trade vetoes by reason and cross-reference
each against the equivalent research (shadow) trade's actual outcome.

Every fund-trade candidate that reaches a YES recommendation also gets logged
as a research trade in data/research_trades.csv, independent of whether it
survives the fund-side risk filters (drawdown-tier grade, devil's advocate,
capacity/correlation/session/news blocks, etc.). When a fund trade is vetoed
(status=SKIPPED in data/trades.csv, with the reason recorded in `notes`), the
matching research trade — same pair, direction, and date — still runs to
completion and records a real WIN/LOSS/EXPIRED outcome. This script groups
vetoes by reason and reports what the shadow research trade actually did,
so each veto filter's track record can be inspected empirically.

This is a report on data already being written — no new capture mechanism,
no scheduled job, no dashboard. Run on demand:

    python scripts/veto_reason_report.py

IMPORTANT: with the current low fund-trade volume, per-reason sample sizes
are almost certainly too small to draw conclusions from. This tool exists so
the reporting is ready once there's more history — not to justify changing
any gate or threshold today.
"""
import sys
from datetime import datetime, timedelta

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

MIN_DECISIVE_FOR_SIGNAL = 20  # below this, flag the row as too small to mean anything

DECISIVE_WIN  = {"WIN", "FULL_WIN", "PARTIAL_WIN"}
DECISIVE_LOSS = {"LOSS"}
DECISIVE      = DECISIVE_WIN | DECISIVE_LOSS


def classify_veto(notes: str) -> str:
    """Bucket a SKIPPED row's notes into a short veto-reason label."""
    n = (notes or "").strip()
    if n.startswith("VIRTUAL_OUTCOME:"):
        n = n.split("|", 1)[1].strip() if "|" in n else n

    if n.startswith("Drawdown filter"):
        return "Drawdown filter (grade-tier veto)"
    if n.startswith("DA-demoted"):
        return "DA-demoted (devil's advocate)"
    if n.startswith("Demoted:"):
        return "Demoted (eff_conf below threshold, pre-DA)"
    if n.lower().startswith("not viable"):
        return "Not viable (R:R < 1.3 after costs)"
    if n.startswith("Inverse dedup"):
        return "Inverse dedup (same-batch inverse pair)"
    if n.startswith("Ghost trade"):
        return "Ghost trade (never cleared demotion gate)"
    if n.startswith("Blocked:"):
        sub = n[len("Blocked:"):].strip()
        for sep in (" — ", " – ", ": "):
            if sep in sub:
                sub = sub.split(sep, 1)[0]
                break
        return f"Blocked: {sub[:40].strip()}"
    if not n:
        return "(no notes recorded)"
    return f"Other/unrecognized: {n[:40]}"


def is_conditional_entry(notes: str) -> bool:
    """Conditional (LIMIT/BREAKOUT/PULLBACK) setups never had an executable
    entry/stop from the AI — there's nothing to veto, so exclude from the
    veto analysis rather than mis-bucket them."""
    return (notes or "").strip().startswith("Conditional")


def load_research_index(research_csv: str) -> dict:
    """Map (PAIR, DIRECTION, date-str) -> list of research trade rows."""
    rt = pd.read_csv(research_csv, encoding="utf-8-sig")
    idx: dict = {}
    for _, r in rt.iterrows():
        key = (str(r.get("pair", "")).upper(), str(r.get("direction", "")).upper(), str(r.get("date", "")))
        idx.setdefault(key, []).append(r)
    return idx


def find_research_match(idx: dict, pair: str, direction: str, date_str: str):
    """Exact (pair, direction, date) match first, then +/-1 day fallback."""
    pair_u, dir_u = pair.upper(), direction.upper()
    key = (pair_u, dir_u, date_str)
    if key in idx:
        return idx[key][0]
    try:
        d0 = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None
    for delta in (-1, 1):
        alt = (d0 + timedelta(days=delta)).strftime("%Y-%m-%d")
        alt_key = (pair_u, dir_u, alt)
        if alt_key in idx:
            return idx[alt_key][0]
    return None


def main() -> None:
    fund = pd.read_csv("data/trades.csv", encoding="utf-8-sig")
    skipped = fund[fund["status"].astype(str) == "SKIPPED"].copy()

    conditional_mask = skipped["notes"].apply(is_conditional_entry)
    n_conditional = int(conditional_mask.sum())
    vetoed = skipped[~conditional_mask].copy()

    vetoed["veto_reason"] = vetoed["notes"].apply(classify_veto)
    vetoed["date"] = vetoed["timestamp"].astype(str).str.slice(0, 10)

    research_idx = load_research_index("data/research_trades.csv")

    rows = []
    for _, r in vetoed.iterrows():
        match = find_research_match(research_idx, str(r.get("pair", "")),
                                     str(r.get("direction", "")), r["date"])
        rows.append({
            "veto_reason": r["veto_reason"],
            "matched":     match is not None,
            "shadow_status": str(match["status"]) if match is not None else None,
            "shadow_r":      pd.to_numeric(match.get("r_multiple"), errors="coerce") if match is not None else None,
        })
    detail = pd.DataFrame(rows)

    print("=" * 100)
    print("FUND TRADE VETO REPORT — grouped by reason, cross-referenced against shadow research outcomes")
    print("=" * 100)
    print(f"Total SKIPPED fund rows: {len(skipped)}  "
          f"({n_conditional} conditional-entry setups excluded — no executable price, nothing to veto)")
    print(f"Vetoed candidates analysed: {len(vetoed)}")
    print()

    if detail.empty:
        print("No vetoed fund trades to report on.")
        return

    header = (
        f"{'VETO REASON':<45} {'VETOES':>7} {'MATCHED':>8} {'DECISIVE':>9} "
        f"{'WIN%':>7} {'AVG R':>8}  FLAG"
    )
    print(header)
    print("-" * len(header))

    summary = []
    for reason, grp in detail.groupby("veto_reason"):
        n_vetoes = len(grp)
        n_matched = int(grp["matched"].sum())
        decisive = grp[grp["shadow_status"].isin(DECISIVE)]
        n_decisive = len(decisive)
        n_win = int(decisive["shadow_status"].isin(DECISIVE_WIN).sum())
        win_rate = (100.0 * n_win / n_decisive) if n_decisive else float("nan")
        avg_r = decisive["shadow_r"].dropna().mean() if n_decisive else float("nan")
        flag = "TOO FEW TO CONCLUDE" if n_decisive < MIN_DECISIVE_FOR_SIGNAL else ""
        summary.append((reason, n_vetoes, n_matched, n_decisive, win_rate, avg_r, flag))

    summary.sort(key=lambda row: row[1], reverse=True)
    for reason, n_vetoes, n_matched, n_decisive, win_rate, avg_r, flag in summary:
        win_str = f"{win_rate:.0f}%" if win_rate == win_rate else "—"
        avg_str = f"{avg_r:+.2f}" if avg_r == avg_r else "—"
        print(f"{reason:<45} {n_vetoes:>7} {n_matched:>8} {n_decisive:>9} {win_str:>7} {avg_str:>8}  {flag}")

    print()
    print("MATCHED   = a same pair/direction/date research trade was found (any status).")
    print("DECISIVE  = matched research trade resolved WIN/FULL_WIN/PARTIAL_WIN/LOSS (excludes EXPIRED/OPEN/etc).")
    print("WIN%/AVG R are computed only over DECISIVE rows.")
    print()
    print("*** Reporting only. Do not use this to change any gate or threshold yet — sample sizes")
    print(f"*** per reason are almost certainly below the {MIN_DECISIVE_FOR_SIGNAL}-trade minimum this script")
    print("*** flags as meaningful. Re-run this once fund-trade (and vetoed-candidate) volume grows.")


if __name__ == "__main__":
    main()
