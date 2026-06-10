"""Persist trade feature snapshots for ML model training.

Every conf>=5 analysis generates a feature vector (src/feature_extractor.py)
which is saved here, keyed by (source_table, trade_id).

When trades close with WIN/LOSS outcomes, ml_predictor.py joins this file with
research_trades.csv / trades.csv to build labelled training examples.

source_table values: "research"  →  research_trades.csv row
                     "main"      →  trades.csv row
"""
import csv
from datetime import datetime

import config
from src.feature_extractor import FEATURE_COLS

FEAT_CSV = config.DATA_DIR / "trade_features.csv"
FIELDS   = ["source_table", "trade_id", "captured_at"] + FEATURE_COLS


def _existing_keys() -> set:
    if not FEAT_CSV.exists():
        return set()
    try:
        with FEAT_CSV.open("r", encoding="utf-8", newline="") as fh:
            return {
                (r.get("source_table", ""), r.get("trade_id", ""))
                for r in csv.DictReader(fh)
            }
    except Exception:
        return set()


def save(source_table: str, trade_id: int, features: dict) -> None:
    """Append one feature row. Silently skips duplicate (source_table, trade_id) pairs."""
    if (source_table, str(trade_id)) in _existing_keys():
        return

    row = {
        "source_table": source_table,
        "trade_id":     str(trade_id),
        "captured_at":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    for col in FEATURE_COLS:
        row[col] = features.get(col, "")

    write_header = not FEAT_CSV.exists()
    with FEAT_CSV.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def load() -> list:
    """Return all rows as a list of dicts."""
    if not FEAT_CSV.exists():
        return []
    with FEAT_CSV.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def count() -> int:
    return len(load())
