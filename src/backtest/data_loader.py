"""Read-only loaders for named, versioned historical trade datasets.

Every function here only ever calls pd.read_csv() against the live CSVs —
never .to_csv() back to them. This module has no write path to
data/research_trades.csv or data/trades.csv at all.

Datasets are named (not raw file paths) specifically so an experiment's
report is self-documenting about which population it ran against — the
name carries the strict/loose and pre/post-fix distinctions that caused
real confusion earlier in this project ("which era is this data from").
"""

import subprocess
from pathlib import Path

import pandas as pd

import config
from src.trading import financials as fin

RESEARCH_TRADES_CSV = config.DATA_DIR / "research_trades.csv"
FUND_TRADES_CSV = config.DATA_DIR / "trades.csv"

# research_outcome_checker.py's 2026-07-15 fix (commit 6ed8af37) corrected WIN
# classification to require the true 2R cascade target. Trades whose closure
# was checked before this deployed still carry the retired ~1R exit
# mechanism. See src/trading/financials.py for the full explanation.
EXIT_LOGIC_FIX_UTC = fin.EXIT_LOGIC_FIX_UTC


def _strict_outcome(status: str) -> str | None:
    """WIN/FULL_WIN -> 'WIN', LOSS -> 'LOSS', anything else -> None (excluded).

    "Strict" = true target-hit/stop-hit only. PARTIAL_WIN and EXPIRED are
    excluded entirely, not reclassified by pip sign — same definition used
    throughout this project's fund/research performance reporting.
    """
    s = str(status).upper()
    if s in ("WIN", "FULL_WIN"):
        return "WIN"
    if s == "LOSS":
        return "LOSS"
    return None


def _load_research_v2(strict: bool, postfix: bool) -> pd.DataFrame:
    df = pd.read_csv(RESEARCH_TRADES_CSV, encoding="utf-8-sig")
    closed = df[df["status"] != "OPEN"].copy()
    v2 = closed[closed["system_version"].astype(str) == "v2"].copy()
    v2["closed_at_dt"] = pd.to_datetime(v2["closed_at"], errors="coerce")

    if postfix:
        v2 = v2[v2["closed_at_dt"] >= EXIT_LOGIC_FIX_UTC]

    if strict:
        v2["outcome"] = v2["status"].apply(_strict_outcome)
        v2 = v2[v2["outcome"].notna()]
    else:
        pips = pd.to_numeric(v2["pips"], errors="coerce").fillna(0)
        su = v2["status"].str.upper()
        is_win = su.isin(["WIN", "FULL_WIN", "PARTIAL_WIN"]) | (su.eq("EXPIRED") & (pips > 0))
        is_loss = su.eq("LOSS") | (su.eq("EXPIRED") & (pips <= 0))
        v2 = v2[is_win | is_loss].copy()
        v2["outcome"] = "LOSS"
        v2.loc[is_win.reindex(v2.index, fill_value=False), "outcome"] = "WIN"

    v2["r_multiple"] = pd.to_numeric(v2["r_multiple"], errors="coerce")
    v2["pips"] = pd.to_numeric(v2["pips"], errors="coerce")
    v2["confidence"] = pd.to_numeric(v2["confidence"], errors="coerce")
    return v2.reset_index(drop=True)


# ── Named dataset registry ───────────────────────────────────────────────────
# Add new entries here as new named populations are needed — this is the only
# place dataset definitions live, so every experiment report can name its
# population unambiguously instead of re-describing filter logic each time.

_REGISTRY = {
    "research_v2_strict_postfix": {
        "loader": lambda: _load_research_v2(strict=True, postfix=True),
        "description": (
            "Research v2 trades, strict outcomes only (true TARGET_HIT/STOP_HIT, "
            "PARTIAL_WIN/EXPIRED excluded), closed_at >= the 2026-07-14 exit-logic "
            "fix. This is the checkpoint-tracked, current-rules population."
        ),
    },
    "research_v2_strict_all_time": {
        "loader": lambda: _load_research_v2(strict=True, postfix=False),
        "description": (
            "Research v2 trades, strict outcomes only, ALL dates (mixes the "
            "retired pre-fix ~1R exit mechanism with the current ~2R one). "
            "Historical reference only — not the current-rules population."
        ),
    },
    "research_v2_loose_postfix": {
        "loader": lambda: _load_research_v2(strict=False, postfix=True),
        "description": (
            "Research v2 trades, loose outcomes (WIN/FULL_WIN/PARTIAL_WIN, plus "
            "EXPIRED reclassified by pip sign), closed_at >= the exit-logic fix."
        ),
    },
}


def list_datasets() -> dict:
    """{name: description} for every registered dataset."""
    return {name: spec["description"] for name, spec in _REGISTRY.items()}


def load_dataset(name: str) -> pd.DataFrame:
    if name not in _REGISTRY:
        raise ValueError(
            f"Unknown dataset '{name}'. Available: {', '.join(sorted(_REGISTRY))}"
        )
    return _REGISTRY[name]["loader"]()


def dataset_description(name: str) -> str:
    return _REGISTRY[name]["description"]


def _git_hash_of(path: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", str(path)],
            cwd=config.ROOT, capture_output=True, text=True, timeout=10,
        )
        h = out.stdout.strip()
        return h if h else "unknown (no git history / file not committed)"
    except Exception as e:
        return f"unknown (git lookup failed: {e})"


def snapshot_meta(dataset_name: str, df: pd.DataFrame) -> dict:
    """Freeze the data identity this run used, for traceability."""
    source_csv = (
        RESEARCH_TRADES_CSV if dataset_name.startswith("research_") else FUND_TRADES_CSV
    )
    closed_at_col = "closed_at_dt" if "closed_at_dt" in df.columns else None
    date_range = None
    if closed_at_col:
        date_range = [
            str(df[closed_at_col].min()),
            str(df[closed_at_col].max()),
        ]
    return {
        "dataset_name": dataset_name,
        "dataset_description": dataset_description(dataset_name),
        "source_csv": str(source_csv),
        "source_csv_git_commit": _git_hash_of(source_csv),
        "row_count": int(len(df)),
        "date_range": date_range,
        "exit_logic_fix_utc": str(EXIT_LOGIC_FIX_UTC),
    }
