"""Continuous online learning model using SGDClassifier.

Updates after every trade closure rather than waiting for weekly batch
retraining.  Complements the batch GBM model — blended predictions use
both, weighted by online model maturity.

Recency sample weights (by age of closed_at date):
  0-1 day:  1.0
  1-7d:     0.8
  8-14d:    0.6
  15-30d:   0.4
  30+d:     0.2

Saved to data/online_model.pkl  +  data/online_model_meta.json.
"""
import json
import pickle
from datetime import datetime

import config

ONLINE_MODEL_FILE = config.DATA_DIR / "online_model.pkl"
ONLINE_META_FILE  = config.DATA_DIR / "online_model_meta.json"

WIN_STATUSES  = {"WIN", "FULL_WIN", "PARTIAL_WIN"}
LOSS_STATUSES = {"LOSS"}

_RECENCY_TABLE = [(1, 1.0), (7, 0.8), (14, 0.6), (30, 0.4)]


def _recency_weight(closed_at: str) -> float:
    try:
        age = (datetime.now() - datetime.fromisoformat(closed_at[:19])).days
    except Exception:
        return 0.7
    for days, w in _RECENCY_TABLE:
        if age <= days:
            return w
    return 0.2


def _load_meta() -> dict:
    try:
        return json.loads(ONLINE_META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_meta(meta: dict) -> None:
    ONLINE_META_FILE.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _load_model():
    """Return (scaler, clf, feature_cols) or (None, None, None)."""
    if not ONLINE_MODEL_FILE.exists():
        return None, None, None
    try:
        payload = pickle.loads(ONLINE_MODEL_FILE.read_bytes())
        return payload["scaler"], payload["clf"], payload["feature_cols"]
    except Exception:
        return None, None, None


def _save_model(scaler, clf, feature_cols: list) -> None:
    ONLINE_MODEL_FILE.write_bytes(pickle.dumps(
        {"scaler": scaler, "clf": clf, "feature_cols": feature_cols}
    ))


def partial_fit_trade(source_table: str, trade_id, outcome: str,
                      closed_at: str = None) -> bool:
    """Update online model with one closed trade.

    Looks up the feature vector from feature_store by (source_table, trade_id).
    Returns True if the model was updated; False if features not found or
    outcome is not decisive (EXPIRED, BREAKEVEN, etc.).
    """
    status = (outcome or "").upper()
    if status in WIN_STATUSES:
        label = 1
    elif status in LOSS_STATUSES:
        label = 0
    else:
        return False

    try:
        from sklearn.linear_model import SGDClassifier
        from sklearn.preprocessing import StandardScaler
        from src.feature_extractor import FEATURE_COLS
        from src import feature_store
        import numpy as np
    except ImportError:
        return False

    rows = feature_store.load()
    feat_row = next(
        (r for r in rows
         if r.get("source_table") == source_table and str(r.get("trade_id")) == str(trade_id)),
        None,
    )
    if feat_row is None:
        return False

    X = np.array([[float(feat_row.get(c) or 0.0) for c in FEATURE_COLS]])

    scaler, clf, f_cols = _load_model()

    if clf is None:
        # class_weight not supported for partial_fit; balance via sample_weight instead
        clf = SGDClassifier(
            loss="log_loss",
            alpha=0.01,
            learning_rate="optimal",
            random_state=42,
        )
        scaler = StandardScaler()
        f_cols = FEATURE_COLS

    scaler.partial_fit(X)
    X_s = scaler.transform(X)

    weight = _recency_weight(closed_at) if closed_at else 0.7
    clf.partial_fit(X_s, [label], classes=[0, 1], sample_weight=[weight])

    _save_model(scaler, clf, f_cols)

    meta = _load_meta()
    meta["n_decisive"] = meta.get("n_decisive", 0) + 1
    meta["last_updated"] = datetime.now().isoformat()[:19]
    meta["last_outcome"] = outcome

    # Track rolling win rate (last 20 decisive)
    history = meta.get("recent_outcomes", [])
    history.append({"label": label, "ts": (closed_at or datetime.now().isoformat()[:10])})
    meta["recent_outcomes"] = history[-20:]
    recent_wins = sum(1 for h in meta["recent_outcomes"] if h["label"] == 1)
    meta["recent_win_rate"] = round(recent_wins / len(meta["recent_outcomes"]), 3)

    _save_meta(meta)
    return True


def predict_proba(features: dict) -> float | None:
    """Return win probability from online model (0–1), or None if not ready."""
    scaler, clf, f_cols = _load_model()
    if clf is None or scaler is None:
        return None
    if not hasattr(clf, "coef_"):
        return None
    try:
        import numpy as np
        X   = np.array([[float(features.get(c) or 0.0) for c in f_cols]])
        X_s = scaler.transform(X)
        proba   = clf.predict_proba(X_s)
        classes = list(clf.classes_)
        win_idx = classes.index(1) if 1 in classes else 1
        return float(proba[0][win_idx])
    except Exception:
        return None


def get_n_decisive() -> int:
    return _load_meta().get("n_decisive", 0)


def build_status_line() -> str:
    meta = _load_meta()
    n    = meta.get("n_decisive", 0)
    if n == 0:
        return "Online learner: accumulating trades (0 so far)"
    wr   = meta.get("recent_win_rate")
    last = meta.get("last_updated", "")[:10]
    wr_str = f", recent win rate {wr:.0%}" if wr is not None else ""
    return f"Online learner: {n} trades{wr_str} (last update {last})"
