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
from datetime import datetime, timezone

import config

ONLINE_MODEL_FILE = config.DATA_DIR / "online_model.pkl"
ONLINE_META_FILE  = config.DATA_DIR / "online_model_meta.json"

WIN_STATUSES  = {"WIN", "FULL_WIN", "PARTIAL_WIN"}
LOSS_STATUSES = {"LOSS"}

_RECENCY_TABLE = [(1, 1.0), (7, 0.8), (14, 0.6), (30, 0.4)]

# R-magnitude sample weights — proportional to expected R earned
# FULL_WIN≈+1.95R, WIN≈+1.35R, PARTIAL_WIN≈+0.35R, LOSS=-1R
_R_WEIGHTS = {"FULL_WIN": 1.00, "WIN": 0.85, "PARTIAL_WIN": 0.55, "LOSS": 1.00}

# Reliability gate (2026-09-01) — mirrors ml_predictor.py's n_consecutive_reliable
# pattern exactly: a model is trusted to actually influence a live decision
# (confidence penalty / hard block) only after 3 consecutive retrains clear a
# holdout-AUC bar, never on a single lucky retrain. Unlike ml_predictor.py's
# own version of this gate (is_model_reliable(), confirmed via a full-codebase
# grep to have ZERO callers anywhere -- a well-designed but entirely unused
# safety check, since removed), THIS gate is actually wired into daily.py's
# penalty/hard-block call site, not just computed and left unread.
_HOLDOUT_FRACTION          = 0.30  # newest N% held out from training, same ~1/3 split ml_predictor.py uses
_MIN_HOLDOUT_N             = 10    # below this, a holdout AUC isn't trustworthy -- treat as unreliable this round
_RELIABILITY_AUC_THRESHOLD = 0.65  # same bar ml_predictor.py's (now-removed) is_model_reliable() used
_RELIABILITY_STREAK_REQUIRED = 3   # same streak length ml_predictor.py's version used


def _recency_weight(closed_at: str) -> float:
    try:
        age = (datetime.now(timezone.utc).replace(tzinfo=None) - datetime.fromisoformat(closed_at[:19])).days
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
        # No baseline model exists yet. Don't bootstrap from a single trade —
        # retrain_all_from_feature_store() will create the initial model once
        # enough decisive v2 trades accumulate in the feature store.
        return False

    scaler.partial_fit(X)
    X_s = scaler.transform(X)

    r_mult = _R_WEIGHTS.get(status, 0.70)
    weight = (_recency_weight(closed_at) * r_mult) if closed_at else (0.7 * r_mult)
    clf.partial_fit(X_s, [label], classes=[0, 1], sample_weight=[weight])

    _save_model(scaler, clf, f_cols)

    meta = _load_meta()
    meta["n_decisive"] = meta.get("n_decisive", 0) + 1
    meta["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    meta["last_outcome"] = outcome

    # Track rolling win rate (last 20 decisive)
    history = meta.get("recent_outcomes", [])
    history.append({"label": label, "ts": (closed_at or datetime.now(timezone.utc).strftime("%Y-%m-%d"))})
    meta["recent_outcomes"] = history[-20:]
    recent_wins = sum(1 for h in meta["recent_outcomes"] if h["label"] == 1)
    meta["recent_win_rate"] = round(recent_wins / len(meta["recent_outcomes"]), 3)

    _save_meta(meta)

    # Immediate retrain on loss — learn faster from mistakes
    if label == 0:
        try:
            retrain_all_from_feature_store()
        except Exception:
            pass

    return True


def is_model_reliable() -> bool:
    """Return True only when the model has had holdout AUC >= _RELIABILITY_AUC_THRESHOLD
    for _RELIABILITY_STREAK_REQUIRED+ consecutive retrains.

    Mirrors ml_predictor.py's (now-removed) is_model_reliable() exactly (same
    threshold, same streak length). Until this is True, predict_proba()'s
    output may still be computed and displayed/logged, but must NOT be used
    to apply a confidence penalty or hard-block a real trade candidate --
    see daily.py's call site.
    """
    meta = _load_meta()
    return meta.get("n_consecutive_reliable", 0) >= _RELIABILITY_STREAK_REQUIRED


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


def retrain_all_from_feature_store(log=None) -> dict:
    """Bulk-retrain on all decisive closed research trades in the feature store.

    Resets the model from scratch so historical trades get proper recency
    weighting.  Trades are sorted oldest-first so the scaler sees the full
    distribution before the classifier sees its first sample.

    Returns a dict: {trained, skipped, win_rate}.
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    try:
        from sklearn.linear_model import SGDClassifier
        from sklearn.preprocessing import StandardScaler
        from src.feature_extractor import FEATURE_COLS
        from src import feature_store
        import numpy as np
        import csv
    except ImportError as exc:
        _log(f"[online_learner] Import error: {exc}")
        return {"trained": 0, "skipped": 0, "win_rate": None}

    # Build outcome lookup from research_trades.csv
    rt_path = config.DATA_DIR / "research_trades.csv"
    outcome_map: dict = {}  # trade_id (str) -> (status, closed_at)
    if rt_path.exists():
        try:
            with rt_path.open("r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    tid = str(row.get("id", ""))
                    if tid and row.get("system_version", "") == "v2":
                        outcome_map[tid] = (
                            row.get("status", ""),
                            row.get("closed_at", "") or row.get("date", ""),
                        )
        except Exception as exc:
            _log(f"[online_learner] Could not read research_trades.csv: {exc}")

    # Also build outcome lookup from trades.csv so fund trades retrain alongside research
    fund_path = config.DATA_DIR / "trades.csv"
    fund_outcome_map: dict = {}
    if fund_path.exists():
        try:
            with fund_path.open("r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    tid = str(row.get("id", ""))
                    if tid and row.get("system_version", "") == "v2":
                        fund_outcome_map[tid] = (
                            row.get("status", ""),
                            row.get("closed_at", ""),
                        )
        except Exception as exc:
            _log(f"[online_learner] Could not read trades.csv: {exc}")

    feat_rows = feature_store.load()
    # Include both research and fund (main) feature rows for comprehensive retraining
    research_rows = [r for r in feat_rows if r.get("source_table") in ("research", "main")]

    # Sort oldest first so scaler partial_fit sees full range before clf trains
    research_rows.sort(key=lambda r: r.get("captured_at", ""))

    # Capture the previous reliability streak BEFORE the meta file is deleted
    # below -- _load_meta() called after the unlink() would always see an
    # empty/missing file and read 0, silently capping n_consecutive_reliable
    # at 1 forever regardless of how many good retrains actually occurred in
    # a row (caught via a synthetic 3-good-retrains smoke test before shipping).
    prev_consec = _load_meta().get("n_consecutive_reliable", 0)

    # Reset model for clean retrain
    if ONLINE_MODEL_FILE.exists():
        ONLINE_MODEL_FILE.unlink()
    if ONLINE_META_FILE.exists():
        ONLINE_META_FILE.unlink()

    clf = SGDClassifier(
        loss="log_loss",
        alpha=0.01,
        learning_rate="optimal",
        random_state=42,
    )
    scaler = StandardScaler()

    # Resolve labels first (one pass), THEN split chronologically into a
    # training portion and a holdout portion -- same "oldest N%, newest M%"
    # temporal-holdout shape as ml_predictor.py's _temporal_cv_scores(), so
    # this model's reliability streak is judged on genuinely unseen (future-
    # relative-to-training) data, not a lucky in-sample fit.
    resolved: list = []
    skipped = 0
    for feat_row in research_rows:
        tid = str(feat_row.get("trade_id", ""))
        src = feat_row.get("source_table", "research")
        if src == "main":
            status, closed_at = fund_outcome_map.get(tid, ("", ""))
        else:
            status, closed_at = outcome_map.get(tid, ("", ""))
        status = (status or "").upper()

        if status in WIN_STATUSES:
            label = 1
        elif status in LOSS_STATUSES:
            label = 0
        else:
            skipped += 1
            continue
        resolved.append((feat_row, status, closed_at, label))

    n_resolved = len(resolved)
    n_holdout  = int(n_resolved * _HOLDOUT_FRACTION) if n_resolved >= _MIN_HOLDOUT_N * 2 else 0
    train_rows, holdout_rows = (
        (resolved[:-n_holdout], resolved[-n_holdout:]) if n_holdout > 0 else (resolved, [])
    )

    trained = 0
    history: list = []

    for feat_row, status, closed_at, label in train_rows:
        tid = str(feat_row.get("trade_id", ""))
        try:
            X = np.array([[float(feat_row.get(c) or 0.0) for c in FEATURE_COLS]])
            scaler.partial_fit(X)
            X_s = scaler.transform(X)
            r_mult = _R_WEIGHTS.get(status, 0.70)
            weight = (_recency_weight(closed_at) * r_mult) if closed_at else (0.5 * r_mult)
            clf.partial_fit(X_s, [label], classes=[0, 1], sample_weight=[weight])
            history.append({"label": label, "ts": (closed_at or "")[:10]})
            trained += 1
        except Exception as exc:
            _log(f"[online_learner] Skipped trade {tid}: {exc}")
            skipped += 1

    if trained == 0:
        _log("[online_learner] No decisive trades found in feature store — model not saved")
        return {"trained": 0, "skipped": skipped, "win_rate": None}

    _save_model(scaler, clf, FEATURE_COLS)

    # Holdout evaluation -- predict on rows never seen by partial_fit above.
    holdout_auc = None
    if len(holdout_rows) >= _MIN_HOLDOUT_N:
        try:
            from sklearn.metrics import roc_auc_score
            X_hold = np.array([
                [float(fr.get(c) or 0.0) for c in FEATURE_COLS] for fr, _, _, _ in holdout_rows
            ])
            y_hold = [lbl for _, _, _, lbl in holdout_rows]
            X_hold_s = scaler.transform(X_hold)
            proba = clf.predict_proba(X_hold_s)
            classes = list(clf.classes_)
            win_idx = classes.index(1) if 1 in classes else 1
            p_win = proba[:, win_idx]
            if len(set(y_hold)) >= 2:  # AUC undefined with only one class present
                holdout_auc = round(float(roc_auc_score(y_hold, p_win)), 3)
        except Exception as exc:
            _log(f"[online_learner] Holdout evaluation failed: {exc}")

    if holdout_auc is not None and holdout_auc >= _RELIABILITY_AUC_THRESHOLD:
        n_consecutive_reliable = prev_consec + 1
    else:
        n_consecutive_reliable = 0

    recent = history[-20:]
    recent_wins = sum(1 for h in recent if h["label"] == 1)
    recent_wr = round(recent_wins / len(recent), 3) if recent else None

    all_wins = sum(1 for h in history if h["label"] == 1)
    overall_wr = round(all_wins / len(history), 3) if history else None

    meta = {
        "n_decisive": trained,
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "last_outcome": history[-1]["label"] if history else None,
        "recent_outcomes": recent,
        "recent_win_rate": recent_wr,
        "overall_win_rate": overall_wr,
        "retrain_source": "bulk_retrain",
        "holdout_n": len(holdout_rows),
        "holdout_auc": holdout_auc,
        "n_consecutive_reliable": n_consecutive_reliable,
    }
    _save_meta(meta)

    if holdout_auc is not None:
        _log(f"[online_learner] Holdout AUC {holdout_auc:.3f} on {len(holdout_rows)} unseen trades "
             f"(reliable retrains: {n_consecutive_reliable}/{_RELIABILITY_STREAK_REQUIRED})")
    else:
        _log(f"[online_learner] Holdout not evaluated (n={len(holdout_rows)}, need >= {_MIN_HOLDOUT_N}) "
             f"— reliability streak reset to 0")

    wr_str = f"{recent_wr:.0%}" if recent_wr is not None else "?"
    _log(f"[online_learner] Bulk retrain complete: {trained} trades trained "
         f"(research + fund), {skipped} skipped, recent win rate {wr_str}")
    return {"trained": trained, "skipped": skipped, "win_rate": recent_wr}


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


# 2026-09-02: removed the OnlineLearner class (fund-trade-specific model)
# that used to live below this point. Confirmed dead in practice before
# removing, not just unused: data/fund_feature_store.json had features
# stored for only 8 trade_ids ever (5 never even opened as real trades, 1
# still PENDING, 2 reached EXPIRED -- not even a real WIN/LOSS), and
# data/fund_online_model.pkl / fund_online_model_meta.json never existed at
# all, meaning train_fund_outcome() had never completed successfully in
# production. Root cause: it was only ever called from monitor.py's
# trade-closing loop, but real fund trades actually close through
# outcome_checker.py, which calls partial_fit_trade() above (the module-
# level model this file already implements) and never touched this class.
# It also never had a prediction method at all -- nothing anywhere ever
# read a prediction back out of it, even on the rare occasion training
# worked when called directly for verification. Its 11 features overlapped
# almost entirely with the module-level model's richer 80-feature scheme
# (feature_extractor.FEATURE_COLS) -- no unique signal was lost by removing
# it. See project_full_audit_sep2026.md for the full investigation.
