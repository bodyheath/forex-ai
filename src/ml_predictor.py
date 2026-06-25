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
                n_estimators=50, max_depth=3, learning_rate=0.05,
                min_samples_leaf=5, subsample=0.8,
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
                if s in ("WIN", "PARTIAL_WIN", "FULL_WIN"):
                    n_wins += 1
                elif s == "LOSS":
                    n_losses += 1
    if config.TRADES_CSV.exists():
        with config.TRADES_CSV.open("r", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                s = r.get("status", "").upper()
                if s in ("WIN", "PARTIAL_WIN", "FULL_WIN"):
                    n_wins += 1
                elif s == "LOSS":
                    n_losses += 1
    return n_wins, n_losses


# ── Soft label weight computation ─────────────────────────────────────────────

def _soft_weight(status: str, trade_row: dict) -> float:
    """Return a quality-based sample weight (0.0–1.0) for this trade.

    WIN/LOSS get full weight.  PARTIAL_WIN gets 0.7 (less certain signal).
    EXPIRED trades are weighted by how close to target price was at expiry —
    a trade that got 90% of the way to target before expiring is still
    informative and is weighted 0.85 rather than 0.4.
    """
    s = status.upper()
    if s == "WIN":
        return 1.0
    if s == "PARTIAL_WIN":
        return 0.7   # solid win but only partial target — high quality signal
    if s == "LOSS":
        return 1.0   # definitive loss — full training weight
    if s in ("EXPIRED", "EXPIRED_PROFITABLE", "EXPIRED_LOSS", "EXPIRED_NEUTRAL"):
        try:
            direction = (trade_row.get("direction") or "BUY").upper()
            entry = _safe_float(trade_row.get("entry"))
            tgt   = _safe_float(trade_row.get("target"))
            close = _safe_float(trade_row.get("close_price"))
            if entry and tgt and close and abs(tgt - entry) > 1e-10:
                prog = ((close - entry) / (tgt - entry)
                        if direction == "BUY"
                        else (entry - close) / (entry - tgt))
                if prog >= 0.75:
                    return 0.85  # nearly reached target — strong near-WIN signal
                if prog >= 0.25:
                    return 0.50  # meaningful progress — moderate signal
                return 0.30      # little progress — weak signal
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        return 0.40   # EXPIRED with no price data — low weight
    if s == "BREAKEVEN":
        return 0.30   # very ambiguous outcome
    return 0.50       # unknown outcome — moderate weight


# ── Training data loader ───────────────────────────────────────────────────────

def _load_training_data():
    """Merge closed trade outcomes with features.

    Returns (X_array, y_array, soft_weights_array, n) or (None, None, None, n).
    X columns follow FEATURE_COLS order exactly.
    y:  1=WIN/PARTIAL_WIN, 0=LOSS/EXPIRED/BREAKEVEN
    sw: per-sample quality weight reflecting outcome certainty (0.3–1.0)
    """
    try:
        import numpy as np
    except ImportError:
        return None, None, None, 0

    # Load rich feature store indexed by (source_table, trade_id)
    feat_map: dict = {}
    if FEAT_CSV.exists():
        with FEAT_CSV.open("r", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                key = (r.get("source_table", ""), r.get("trade_id", ""))
                feat_map[key] = r

    rows_X, rows_y, rows_w = [], [], []

    def _add(trade_id, source_table: str, trade_row: dict, outcome: str):
        if outcome in ("WIN", "PARTIAL_WIN", "FULL_WIN"):
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
            try:
                ts   = trade_row.get("timestamp") or trade_row.get("date") or ""
                mo   = int(ts.split("-")[1]) if len(ts.split("-")) >= 2 else 6
                ms   = math.sin(2 * math.pi * mo / 12)
                mc   = math.cos(2 * math.pi * mo / 12)
                dirn = (trade_row.get("direction") or "BUY").upper()
                rr   = _safe_float(trade_row.get("reward_risk"), 1.5)
                conf = _safe_float(trade_row.get("confidence"), 5.0)
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
        rows_w.append(_soft_weight(outcome, trade_row))

    research_csv = config.DATA_DIR / "research_trades.csv"
    if research_csv.exists():
        with research_csv.open("r", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                _add(r.get("id"), "research", r, r.get("status", ""))

    if config.TRADES_CSV.exists():
        with config.TRADES_CSV.open("r", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                _add(r.get("id"), "main", r, r.get("status", ""))

    n = len(rows_X)
    if n < MIN_TRADES:
        return None, None, None, n

    return (
        np.array(rows_X, dtype=float),
        np.array(rows_y, dtype=int),
        np.array(rows_w, dtype=float),
        n,
    )


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

    X, y, soft_weights, n = _load_training_data()

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

    # ── Soft label sample weights ──────────────────────────────────────────────
    # Combine per-outcome quality weights (1.0 for WIN/LOSS, 0.7 for PARTIAL_WIN,
    # 0.3–0.85 for EXPIRED based on target proximity) with class-balance correction.
    try:
        _balance_sw = compute_sample_weight("balanced", y)
        if soft_weights is not None:
            import numpy as _np_sw
            _sw = soft_weights * _balance_sw
        else:
            _sw = _balance_sw
    except Exception:
        _sw = soft_weights if soft_weights is not None else None

    # ── SMOTE: synthetic minority oversampling ─────────────────────────────────
    # Generate synthetic WIN examples by interpolating between existing wins in
    # feature space until we reach a 1:2 WIN:LOSS ratio.  Falls back gracefully
    # when imbalanced-learn is not installed.
    n_real_wins   = int(y.sum())
    n_losses_orig = n - n_real_wins
    n_synthetic   = 0
    smote_applied = False

    try:
        from imblearn.over_sampling import SMOTE as _SMOTE
        import numpy as _np_sm

        # Target 1:2 WIN:LOSS ratio — never exceed n_losses for a 1:1 cap
        target_wins = min(n_losses_orig, max(n_real_wins, n_losses_orig // 2))

        if target_wins > n_real_wins and n_real_wins >= 2:
            k_nb = min(5, n_real_wins - 1)  # k_neighbors must be < minority count
            _smote = _SMOTE(
                sampling_strategy={1: target_wins},
                k_neighbors=k_nb,
                random_state=42,
            )
            X_s_res, y_res = _smote.fit_resample(X_s, y)
            n_synthetic   = target_wins - n_real_wins

            # Build combined weight array: original sample weights preserved;
            # synthetic WIN samples assigned 90% of the average real-WIN soft weight
            _avg_win_sw = float(_np_sm.mean(soft_weights[y == 1])) if (
                soft_weights is not None and n_real_wins > 0
            ) else 1.0
            _synth_w = _np_sm.full(n_synthetic, min(0.9, _avg_win_sw * 0.9))
            _sw = _np_sm.concatenate([_sw, _synth_w]) if _sw is not None else None

            X_s = X_s_res
            y   = y_res
            n   = len(y)
            smote_applied = True
            if not quiet:
                print(
                    f"[ml_predictor] SMOTE: {n_real_wins} real WIN → +{n_synthetic} synthetic "
                    f"= {target_wins} total WIN vs {n_losses_orig} LOSS (1:{n_losses_orig/target_wins:.1f})"
                )
    except ImportError:
        if not quiet:
            print("[ml_predictor] imbalanced-learn not installed — SMOTE skipped (pip install imbalanced-learn)")
    except Exception:
        pass  # SMOTE failed — continue with original data

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
        # Anti-overfitting params for small dataset:
        #   n_estimators=50     — fewer trees = simpler model (less memorisation)
        #   max_depth=3         — shallow trees generalise better
        #   min_samples_leaf=5  — each leaf needs 5 samples (regularisation)
        #   subsample=0.8       — 80% of data per tree (stochastic gradient boosting)
        #   max_features=0.7    — random feature subsets per split (decorrelation)
        # class imbalance handled via sample_weight passed to fit()
        base   = GradientBoostingClassifier(
            n_estimators=50, max_depth=3, learning_rate=0.05,
            min_samples_leaf=5, subsample=0.8,
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
    cv_fold_scores: list = []
    if not skip_cv:
        try:
            cv_s        = cross_val_score(model, X_s, y, cv=cv_k, scoring="roc_auc")
            roc_auc     = round(float(np.mean(cv_s)), 3)
            roc_std     = round(float(np.std(cv_s)),  3)
            cv_fold_scores = [round(float(s), 3) for s in cv_s]
        except Exception:
            roc_auc = roc_std = 0.0

    # Temporal holdout (safeguard 2): train on oldest 67%, test on newest 33%.
    # Checks whether patterns discovered in historical data still hold on recent unseen trades.
    _temporal   = _temporal_cv_scores(X_s, y)
    _hold_auc   = _temporal.get("holdout_auc")
    _overfit_g  = round(roc_auc - _hold_auc, 3) if _hold_auc is not None else None
    _is_healthy = _overfit_g is not None and _overfit_g < 0.10

    win_rate = round(float(y.mean()), 3)

    # Track consecutive healthy holdouts (holdout AUC > 0.65) for the reliability gate.
    # A model is trusted for display (but NOT for score adjustments) only after 3
    # consecutive weekly retrains with holdout_auc >= 0.65.
    _prev_consec = _load_meta().get("n_consecutive_reliable", 0)
    if _hold_auc is not None and _hold_auc >= 0.65:
        _n_consecutive_reliable = _prev_consec + 1
    else:
        _n_consecutive_reliable = 0

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
        # Training data composition
        "n_wins_training":       n_wins_tr,
        "n_losses_training":     n_losses_tr,
        "partial_wins_as_wins":  True,
        "class_weights_applied": True,
        "prediction_loss_pct":   _pred_lp,
        "is_biased":             _is_biased,
        # SMOTE synthetic oversampling metadata
        "smote_applied":         smote_applied,
        "n_real_wins":           n_real_wins,
        "n_synthetic_wins":      n_synthetic,
        "n_losses_orig":         n_losses_orig,
        # Soft label quality weighting metadata
        "soft_labels_applied":   True,
        "n_interaction_features": 5,
        # Anti-curve-fitting safeguards
        "min_samples_enforced":  MIN_PATTERN_SAMPLES,
        "complexity_penalty":    True,
        "temporal_holdout_auc":    _hold_auc,
        "overfit_gap":             _overfit_g,
        "is_healthy":              _is_healthy,
        "period_win_rates":        _temporal.get("period_win_rates"),
        "period_stable":           _temporal.get("period_stable"),
        "accuracy_history":        _acc_history,
        "cv_fold_scores":          cv_fold_scores,
        "n_consecutive_reliable":  _n_consecutive_reliable,
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


def predict_blended(features: dict) -> Optional[float]:
    """Return blended win probability combining batch GBM + online SGD models.

    Online model weight scales with number of decisive outcomes it has seen:
      n >= 50:  40% online + 60% batch
      n >= 20:  25% online + 75% batch
      n <  20:  10% online + 90% batch
    Falls back to batch-only if online model not ready.
    """
    batch_p = predict(features)
    try:
        from src import online_learner as _ol
        n_online = _ol.get_n_decisive()
        online_p = _ol.predict_proba(features) if n_online > 0 else None
    except Exception:
        online_p = None
        n_online = 0

    if batch_p is None:
        return online_p
    if online_p is None or n_online < 5:
        return batch_p

    if n_online >= 50:
        w_online = 0.40
    elif n_online >= 20:
        w_online = 0.25
    else:
        w_online = 0.10

    return round(online_p * w_online + batch_p * (1.0 - w_online), 4)


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

def is_model_reliable() -> bool:
    """Return True only when the model has had holdout AUC >= 0.65 for 3+ consecutive retrains.

    Until this threshold is met the model is displayed for information only
    and must NOT be used to adjust confidence scores.
    """
    meta = _load_meta()
    return meta.get("model_ready", False) and meta.get("n_consecutive_reliable", 0) >= 3


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
        p     = predict_blended(feats)
        if p is None:
            return None
        n = meta.get("n_trades", "?")
        try:
            from src import online_learner as _ol_wgp
            _n_ol = _ol_wgp.get_n_decisive()
            suffix = f" +{_n_ol} live" if _n_ol >= 5 else ""
        except Exception:
            suffix = ""
        return f"{round(p * 100):.0f}% ({n} trades{suffix})"
    except Exception:
        return None


def get_model_status_line() -> str:
    """Plain-English one-line summary for Telegram messages / system health."""
    try:
        meta = _load_meta()
        if not meta.get("model_ready"):
            n      = meta.get("n_trades", 0)
            needed = max(0, MIN_TRADES - n)
            return (f"🤖 ML model: still learning — {n}/{MIN_TRADES} closed trades so far"
                    f" (need {needed} more outcomes before predictions activate)")

        n          = meta.get("n_trades", "?")
        hold_auc   = meta.get("temporal_holdout_auc")
        n_consec   = meta.get("n_consecutive_reliable", 0)
        reliable   = n_consec >= 3

        if hold_auc is not None:
            hold_pct = round(hold_auc * 100)
        else:
            hold_pct = round(meta.get("roc_auc", 0.0) * 100)

        if not reliable:
            return (
                f"🤖 ML model active but accuracy not yet reliable — predictions shown for "
                f"information only — not yet influencing confidence scores "
                f"({hold_pct}% on unseen trades, need {3 - n_consec} more reliable retrains)"
            )

        if hold_pct >= 65:
            return (
                f"🤖 AI performing well: {hold_pct}% accuracy on new trades — "
                f"genuine patterns found ({n} trades studied)"
            )
        # Reliable (3+ good retrains) but current holdout dipped slightly
        return (
            f"🤖 AI learning: The AI has studied {n} trades and can predict outcomes "
            f"with {hold_pct}% accuracy on trades it has never seen — "
            f"slightly better than random — still learning"
        )
    except Exception:
        return "🤖 ML model: unavailable"
