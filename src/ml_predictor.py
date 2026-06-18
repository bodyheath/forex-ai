"""ML win-probability predictor for forex trade setups.

Trains a calibrated classification model on historical closed trade outcomes:
  - data/research_trades.csv  (paper trades, 0.01 lots, conf >= 5)
  - data/trades.csv           (live trade recommendations)

Rich features come from data/trade_features.csv (bundle snapshot at entry).
For older rows without a feature snapshot, layer scores from the trade CSV are
used as a reduced feature set.

Model:
  - n < 50:  LogisticRegression(C=0.5)        — works well on small data
  - n >= 50: GradientBoostingClassifier        — captures non-linear interactions
  Probabilities calibrated with CalibratedClassifierCV for reliable estimates.

Minimum 10 closed trades required before the model activates.
Model persisted to data/ml_model.pkl; metadata in data/ml_model_meta.json.

Usage:
  from src import ml_predictor
  line = ml_predictor.get_win_prob(pair, parsed, bundle)
  # returns "73% (12 trades)" or "Model learning: 3/10 closed trades" or None
"""
import csv
import json
import math
import pickle
from datetime import datetime, timedelta
from typing import Optional

import config
from src.feature_extractor import FEATURE_COLS, extract

MODEL_FILE = config.DATA_DIR / "ml_model.pkl"
META_FILE  = config.DATA_DIR / "ml_model_meta.json"
FEAT_CSV   = config.DATA_DIR / "trade_features.csv"
MIN_TRADES = 10

# Anti-curve-fitting safeguard: each learned decision boundary must be supported
# by at least this many examples in the training set.  Prevents the model from
# building rules around one-off historical coincidences.
MIN_PATTERN_SAMPLES = 15


# ── Persistence helpers ────────────────────────────────────────────────────────

def _load_meta() -> dict:
    try:
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_meta(meta: dict) -> None:
    META_FILE.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _safe_float(v, default: float = 0.0) -> float:
    try:
        f = float(v) if v not in ("", None) else default
        return default if (f != f) else f
    except (TypeError, ValueError):
        return default


# ── Temporal generalisation check ─────────────────────────────────────────────

def _temporal_cv_scores(X_s, y) -> dict:
    """Walk-forward holdout: train on oldest 67% of trades, score on newest 33%.

    Directly answers: "do patterns learned from old data still predict outcomes
    on unseen, more-recent trades?"  A large gap between the full-CV ROC-AUC and
    this holdout score is the primary signal of curve-fitting.

    Also checks per-third win rates so we can tell whether the underlying
    trade distribution has been stable over the collection window.

    Returns a dict with keys:
      skipped         — True when there are fewer than 30 trades
      holdout_auc     — ROC-AUC on the held-out recent 33%
      period_win_rates — [early_wr, mid_wr, recent_wr] floats
      period_stable   — True when all three are within 20% of each other
      period_max_diff — max − min of the three rates
    """
    try:
        import numpy as np
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score
    except ImportError:
        return {"skipped": True, "reason": "sklearn unavailable"}

    n = len(y)
    if n < 30:
        return {"skipped": True, "reason": "need 30+ trades"}

    split  = int(n * 0.67)
    X_tr   = X_s[:split];  y_tr = y[:split]
    X_te   = X_s[split:];  y_te = y[split:]

    if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2:
        return {"skipped": True, "reason": "single-class split"}

    try:
        cv_k = min(5, max(2, len(y_tr) // 4))
        if len(y_tr) >= 50:
            base   = GradientBoostingClassifier(
                n_estimators=100, max_depth=3, learning_rate=0.05,
                min_samples_leaf=MIN_PATTERN_SAMPLES, subsample=0.8,
                max_features=0.7, random_state=42,
            )
            method = "isotonic"
        else:
            base   = LogisticRegression(C=0.1, max_iter=1000, random_state=42)
            method = "sigmoid"
        m = CalibratedClassifierCV(base, cv=cv_k, method=method)
        m.fit(X_tr, y_tr)
        proba   = m.predict_proba(X_te)
        win_idx = list(m.classes_).index(1) if 1 in list(m.classes_) else 1
        auc     = round(float(roc_auc_score(y_te, proba[:, win_idx])), 3)
    except Exception:
        return {"skipped": True, "reason": "scoring failed"}

    # Per-third win rates — is the data distribution consistent over time?
    third   = n // 3
    p_rates = []
    for i in range(3):
        seg = y[i * third:(i + 1) * third] if i < 2 else y[2 * third:]
        p_rates.append(round(float(seg.mean()), 3) if len(seg) > 0 else 0.0)
    max_diff = round(float(max(p_rates) - min(p_rates)), 3)

    return {
        "skipped":          False,
        "holdout_auc":      auc,
        "period_win_rates": p_rates,
        "period_stable":    max_diff <= 0.20,
        "period_max_diff":  max_diff,
    }


# ── Decisive trade counter ─────────────────────────────────────────────────────

def _count_decisive_trades() -> tuple:
    """Count training-eligible outcomes: WIN/PARTIAL_WIN (→ y=1) and LOSS (→ y=0).

    Returns (n_wins, n_losses).  Called before training to gate on minimum data.
    PARTIAL_WIN is counted as WIN since it maps to y=1 in the training set.
    EXPIRED/BREAKEVEN excluded — they don't cleanly represent edge direction.
    """
    n_wins = n_losses = 0
    research_csv = config.DATA_DIR / "research_trades.csv"
    if research_csv.exists():
        with research_csv.open("r", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                s = r.get("status", "").upper()
                if s in ("WIN", "PARTIAL_WIN"):
                    n_wins += 1
                elif s == "LOSS":
                    n_losses += 1
    if config.TRADES_CSV.exists():
        with config.TRADES_CSV.open("r", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                s = r.get("status", "").upper()
                if s in ("WIN", "PARTIAL_WIN"):
                    n_wins += 1
                elif s == "LOSS":
                    n_losses += 1
    return n_wins, n_losses


# ── Training data loader ───────────────────────────────────────────────────────

def _load_training_data():
    """Merge closed trade outcomes with features.

    Returns (X_array, y_array, n) or (None, None, n) if < MIN_TRADES available.
    X columns follow FEATURE_COLS order exactly.
    y: 1=WIN/PARTIAL_WIN, 0=LOSS/EXPIRED/BREAKEVEN
    """
    try:
        import numpy as np
    except ImportError:
        return None, None, 0

    # Load rich feature store indexed by (source_table, trade_id)
    feat_map: dict = {}
    if FEAT_CSV.exists():
        with FEAT_CSV.open("r", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                key = (r.get("source_table", ""), r.get("trade_id", ""))
                feat_map[key] = r

    rows_X, rows_y = [], []

    def _add(trade_id, source_table: str, trade_row: dict, outcome: str):
        if outcome in ("WIN", "PARTIAL_WIN"):  # PARTIAL_WIN → WIN for ML training
            y = 1
        elif outcome in ("LOSS", "EXPIRED", "BREAKEVEN"):
            y = 0
        else:
            return   # OPEN / NO_TRADE / NO_PRICE_LEVELS / SKIPPED

        feat_row = feat_map.get((source_table, str(trade_id)))
        if feat_row:
            try:
                vec = [_safe_float(feat_row.get(c, 0.0)) for c in FEATURE_COLS]
            except Exception:
                return
        else:
            # Fallback: build feature vector from scores in trade row.
            # New extended features (beyond original 15) default to neutral/0.
            try:
                ts   = trade_row.get("timestamp") or trade_row.get("date") or ""
                mo   = int(ts.split("-")[1]) if len(ts.split("-")) >= 2 else 6
                ms   = math.sin(2 * math.pi * mo / 12)
                mc   = math.cos(2 * math.pi * mo / 12)
                dirn = (trade_row.get("direction") or "BUY").upper()
                rr   = _safe_float(trade_row.get("reward_risk"), 1.5)
                conf = _safe_float(trade_row.get("confidence"), 5.0)
                # Build a dict matching FEATURE_COLS with best-effort values
                base = {
                    "confidence":    conf,
                    "tech_score":    _safe_float(trade_row.get("technical") or
                                                 trade_row.get("tech_score"), 5.0),
                    "fund_score":    _safe_float(trade_row.get("fundamental") or
                                                 trade_row.get("fund_score"),  5.0),
                    "sent_score":    _safe_float(trade_row.get("sentiment") or
                                                 trade_row.get("sent_score"),  5.0),
                    "pos_score":     _safe_float(trade_row.get("positioning") or
                                                 trade_row.get("pos_score"),   5.0),
                    "macro_score":   _safe_float(trade_row.get("macro") or
                                                 trade_row.get("macro_score"), 5.0),
                    "rsi14":         50.0,
                    "macd_signal":   0.0,
                    "atr_pct":       0.0,
                    "reward_risk":   rr,
                    "direction_buy": 1.0 if dirn == "BUY" else 0.0,
                    "mtf_count":     _safe_float(trade_row.get("mtf_count"), 0.0),
                    "ribbon_aligned":0.0,
                    "month_sin":     ms,
                    "month_cos":     mc,
                    # extended — best-effort from new research_trades columns
                    "grade_num":     _safe_float({"A":5,"B":4,"C":3,"D":2,"F":1}.get(
                                         (trade_row.get("grade") or "").upper(), 0), 0.0),
                    "rr_over_2":     1.0 if rr > 2.0 else 0.0,
                    "high_conf":     1.0 if conf >= 8.0 else 0.0,
                    "fund_aligned_count": _safe_float(trade_row.get("fund_aligned_count"), 0.0),
                    "corr_agreement_count": _safe_float(trade_row.get("corr_agreement_count"), 0.0),
                    "day_of_week":   _safe_float(trade_row.get("day_of_week"), 0.0),
                    "hour_auckland": _safe_float(trade_row.get("hour_auckland"), 0.0),
                }
                vec = [base.get(c, 0.0) for c in FEATURE_COLS]
            except Exception:
                return

        rows_X.append(vec)
        rows_y.append(y)

    # Research paper trades
    research_csv = config.DATA_DIR / "research_trades.csv"
    if research_csv.exists():
        with research_csv.open("r", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                _add(r.get("id"), "research", r, r.get("status", ""))

    # Main live trades
    if config.TRADES_CSV.exists():
        with config.TRADES_CSV.open("r", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                _add(r.get("id"), "main", r, r.get("status", ""))

    n = len(rows_X)
    if n < MIN_TRADES:
        return None, None, n

    return np.array(rows_X, dtype=float), np.array(rows_y, dtype=int), n


# ── Training ───────────────────────────────────────────────────────────────────

def train(quiet: bool = False) -> dict:
    """Train or retrain the model on all available closed trade data.

    Returns a metadata dict — keys: trained_at, model_ready, n_trades, win_rate,
    roc_auc, model_type, importances, error (if any).
    """
    try:
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import cross_val_score
        from sklearn.preprocessing import StandardScaler
        from sklearn.utils.class_weight import compute_sample_weight
        import numpy as np
    except ImportError:
        meta = {"error": "scikit-learn not installed — add scikit-learn to requirements.txt",
                "model_ready": False}
        _save_meta(meta)
        return meta

    # Require a minimum of labelled outcomes before training.
    # WIN/PARTIAL_WIN → y=1, LOSS → y=0.  EXPIRED/BREAKEVEN excluded (ambiguous direction).
    _d_wins, _d_losses = _count_decisive_trades()
    _d_total = _d_wins + _d_losses
    if _d_total < MIN_TRADES:
        msg = (
            f"ML retraining skipped — only {_d_total} decisive trades available "
            f"({_d_wins} wins, {_d_losses} losses) — need {MIN_TRADES} minimum"
        )
        if not quiet:
            print(f"[ml_predictor] {msg}")
        meta = _load_meta()
        meta.update({
            "n_trades":    _d_total,
            "model_ready": False,
            "checked_at":  datetime.now().isoformat(),
            "skip_reason": msg,
        })
        _save_meta(meta)
        return {"skipped": True, "model_ready": False, "n_trades": _d_total, "message": msg}

    X, y, n = _load_training_data()

    if X is None:
        meta = _load_meta()
        meta.update({
            "n_trades":    n,
            "model_ready": False,
            "checked_at":  datetime.now().isoformat(),
        })
        _save_meta(meta)
        if not quiet:
            print(f"[ml_predictor] Waiting: {n}/{MIN_TRADES} closed trades so far")
        return {"error": f"need {MIN_TRADES} closed trades (have {n})", "n_trades": n,
                "model_ready": False}

    scaler = StandardScaler()
    X_s    = scaler.fit_transform(X)

    # Class imbalance correction: weight minority class up so the model can't
    # exploit "predict LOSS always" as a cheap strategy.
    try:
        _sw = compute_sample_weight("balanced", y)
    except Exception:
        _sw = None

    # Determine safe CV fold count.  Cap at the smallest class size so stratified
    # splitting never requests more folds than there are examples of the rarer class.
    n_wins_tr   = int(y.sum())
    n_losses_tr = n - n_wins_tr
    min_class   = min(n_wins_tr, n_losses_tr)

    if min_class < 2:
        # One class has 0 or 1 examples — stratified CV is impossible.
        skip_cv   = True
        cv_k      = None
        no_cv_msg = (
            f"ML model trained without cross-validation — need at least 5 decisive "
            f"outcomes per class for proper validation — currently have "
            f"{n_wins_tr} wins and {n_losses_tr} losses"
        )
    else:
        skip_cv   = False
        # Cap folds at min_class to prevent stratification errors (e.g. 3 wins → max 3 folds).
        cv_k      = min(5, max(2, n // 4), min_class)
        no_cv_msg = None

    if n >= 50:
        # Anti-overfitting params:
        #   min_samples_leaf=15 — no pattern learned from < 15 examples (safeguard 1)
        #   n_estimators=100    — fewer trees = simpler model (complexity penalty)
        #   max_features=0.7    — random feature subsets at each split (complexity penalty)
        # class imbalance handled via sample_weight passed to fit()
        base   = GradientBoostingClassifier(
            n_estimators=100, max_depth=3, learning_rate=0.05,
            min_samples_leaf=MIN_PATTERN_SAMPLES, subsample=0.8,
            max_features=0.7, random_state=42,
        )
        method = "isotonic"
        mtype  = "GradientBoosting"
    else:
        # C=0.1 — strong L2 regularisation; class_weight="balanced" corrects WIN/LOSS ratio
        base   = LogisticRegression(C=0.1, max_iter=1000, random_state=42,
                                    class_weight="balanced")
        method = "sigmoid"
        mtype  = "LogisticRegression"

    _fit_kw = {"sample_weight": _sw} if _sw is not None else {}

    if skip_cv:
        # Fit base model first, then wrap with prefit calibration (no CV required).
        base.fit(X_s, y, **_fit_kw)
        model = CalibratedClassifierCV(base, cv="prefit", method="sigmoid")
        model.fit(X_s, y, **_fit_kw)
        if not quiet:
            print(f"[ml_predictor] {no_cv_msg}")
    else:
        model = CalibratedClassifierCV(base, cv=cv_k, method=method)
        model.fit(X_s, y, **_fit_kw)

    # ROC-AUC cross-validation (in-sample, k-fold) — skipped when min_class < 2
    roc_auc = roc_std = 0.0
    if not skip_cv:
        try:
            cv_s    = cross_val_score(model, X_s, y, cv=cv_k, scoring="roc_auc")
            roc_auc = round(float(np.mean(cv_s)), 3)
            roc_std = round(float(np.std(cv_s)),  3)
        except Exception:
            roc_auc = roc_std = 0.0

    # Temporal holdout (safeguard 2): train on oldest 67%, test on newest 33%.
    # Checks whether patterns discovered in historical data still hold on recent unseen trades.
    _temporal   = _temporal_cv_scores(X_s, y)
    _hold_auc   = _temporal.get("holdout_auc")
    _overfit_g  = round(roc_auc - _hold_auc, 3) if _hold_auc is not None else None
    _is_healthy = _overfit_g is not None and _overfit_g < 0.10

    win_rate = round(float(y.mean()), 3)

    # Bias check: if model predicts LOSS > 80% of the time on training data it hasn't
    # corrected the class imbalance — flag for the Monday learning report.
    try:
        _preds_tr  = model.predict(X_s)
        _pred_lp   = int((1 - _preds_tr).sum() / len(_preds_tr) * 100)
        _is_biased = _pred_lp > 80
    except Exception:
        _pred_lp   = 50
        _is_biased = False

    # Feature importances
    importances: dict = {}
    try:
        cal_clf = model.calibrated_classifiers_[0]
        est     = cal_clf.estimator if hasattr(cal_clf, "estimator") else cal_clf
        if hasattr(est, "coef_"):
            for col, imp in zip(FEATURE_COLS, est.coef_[0]):
                importances[col] = round(abs(float(imp)), 4)
        elif hasattr(est, "feature_importances_"):
            for col, imp in zip(FEATURE_COLS, est.feature_importances_):
                importances[col] = round(float(imp), 4)
    except Exception:
        pass

    # Persist
    MODEL_FILE.write_bytes(pickle.dumps({
        "scaler":       scaler,
        "model":        model,
        "feature_cols": FEATURE_COLS,
    }))

    # Preserve and append to accuracy history for the learning report
    _prev_meta   = _load_meta()
    _acc_history = _prev_meta.get("accuracy_history", [])
    _acc_history.append({
        "trained_at": datetime.now().strftime("%Y-%m-%d"),
        "roc_auc":    roc_auc,
        "n_trades":   int(n),
    })
    _acc_history = _acc_history[-10:]  # keep last 10 retrains

    meta = {
        "trained_at":            datetime.now().isoformat(),
        "model_ready":           True,
        "n_trades":              int(n),
        "win_rate":              win_rate,
        "roc_auc":               roc_auc,
        "roc_auc_std":           roc_std,
        "model_type":            mtype,
        "feature_cols":          FEATURE_COLS,
        "importances":           importances,
        # Anti-curve-fitting safeguards
        "min_samples_enforced":  MIN_PATTERN_SAMPLES,
        "complexity_penalty":    True,
        "temporal_holdout_auc":  _hold_auc,
        "overfit_gap":           _overfit_g,
        "is_healthy":            _is_healthy,
        "period_win_rates":      _temporal.get("period_win_rates"),
        "period_stable":         _temporal.get("period_stable"),
        "accuracy_history":      _acc_history,
    }
    _save_meta(meta)

    if not quiet:
        print(
            f"[ml_predictor] {mtype} trained on {n} trades — "
            f"ROC-AUC {roc_auc:.3f}±{roc_std:.3f}  win rate {win_rate:.1%}"
        )
    return meta


# ── Inference ──────────────────────────────────────────────────────────────────

def predict(features: dict) -> Optional[float]:
    """Return win probability (0–1) or None if model unavailable/broken."""
    if not MODEL_FILE.exists():
        return None
    try:
        import numpy as np
        payload = pickle.loads(MODEL_FILE.read_bytes())
        scaler  = payload["scaler"]
        model   = payload["model"]
        cols    = payload.get("feature_cols", FEATURE_COLS)
        row     = np.array([[float(features.get(c) or 0.0) for c in cols]])
        row_s   = scaler.transform(row)
        proba   = model.predict_proba(row_s)
        classes = list(model.classes_)
        win_idx = classes.index(1) if 1 in classes else 1
        return float(proba[0][win_idx])
    except Exception:
        return None


def retrain_if_stale(force: bool = False, quiet: bool = True) -> Optional[dict]:
    """Retrain if model is missing or was last trained > 7 days ago.

    Returns training metadata dict on retrain, None if still fresh.
    """
    meta = _load_meta()
    if not force:
        trained_at = meta.get("trained_at")
        if trained_at:
            try:
                if datetime.now() - datetime.fromisoformat(trained_at) < timedelta(days=7):
                    return None
            except ValueError:
                pass
    return train(quiet=quiet)


# ── Public helpers for Telegram display ───────────────────────────────────────

def get_win_prob(pair: str, parsed: dict, bundle: dict) -> Optional[str]:
    """Return formatted win-probability string for Telegram, or None.

    When model is active:   "73% (12 trades)"
    When model is learning: "Model learning: 4/10 closed trades"
    On any error:           None
    """
    try:
        meta = _load_meta()
        if not meta.get("model_ready"):
            n      = meta.get("n_trades", 0)
            needed = max(0, MIN_TRADES - n)
            if needed > 0:
                return f"Model learning: {n}/{MIN_TRADES} closed trades"
            return None
        feats = extract(pair, parsed, bundle)
        p     = predict(feats)
        if p is None:
            return None
        n = meta.get("n_trades", "?")
        return f"{round(p * 100):.0f}% ({n} trades)"
    except Exception:
        return None


def get_model_status_line() -> str:
    """One-line summary of model status for scan messages / system health."""
    try:
        meta = _load_meta()
        if not meta.get("model_ready"):
            n      = meta.get("n_trades", 0)
            needed = max(0, MIN_TRADES - n)
            return (f"🤖 ML model: learning — {n}/{MIN_TRADES} closed trades"
                    f" (need {needed} more outcomes)")
        trained   = (meta.get("trained_at") or "")[:10]
        roc       = meta.get("roc_auc", 0.0)
        n         = meta.get("n_trades", "?")
        mtype     = meta.get("model_type", "")[:2].upper()
        healthy   = meta.get("is_healthy")
        hold_auc  = meta.get("temporal_holdout_auc")
        health_tag = ""
        if healthy is True:
            health_tag = " ✅ healthy"
        elif healthy is False:
            gap = meta.get("overfit_gap", 0)
            health_tag = f" ⚠️ overfit gap {gap:.2f}"
        hold_str = f" | holdout {hold_auc:.2f}" if hold_auc is not None else ""
        return (f"🤖 ML model active — {n} trades | ROC-AUC {roc:.2f}{hold_str} | "
                f"{mtype}{health_tag} | last trained {trained}")
    except Exception:
        return "🤖 ML model: unavailable"
