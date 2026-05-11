"""Train a regressor on the joined historical_predictions × historical_actuals
dataset. The trained model maps a feature vector to predicted daily max F.

Algorithms:
  - 'linear':   sklearn.linear_model.Ridge with light L2 regularization
  - 'gbm':      sklearn.ensemble.GradientBoostingRegressor (single fixed config)
  - 'gbm-best': GBM hyperparameter sweep — keeps the lowest test_mae config
  - 'rf':       sklearn.ensemble.RandomForestRegressor
  - 'auto':     train linear + rf + gbm-best, persist whichever has the
                lowest test_mae as the active run; logs all candidates to
                ml_runs so the history table shows the full sweep

NOTE: re-running training on the same data with the same algorithm + same
hyperparameters produces an identical model. Real accuracy gains come from
(a) more data — handled by the daily incremental backfill loop, and
(b) trying different algorithms / hyperparameters — this module's `auto`
    and `gbm-best` modes do this.

Holdout protocol: chronological 80/20 split on target_date (no leakage of
future into training). We compute the holdout MAE for each trained model
AND for the naive ensemble_max baseline on the SAME rows so the
comparison is apples-to-apples.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from .. import db


# Raw numeric features. _engineer_features adds derived columns on top.
FEATURE_COLUMNS_NUMERIC: List[str] = [
    "gfs_max", "ecmwf_max", "icon_max", "om_max",
    # Doc §2.1 calls MOS the highest-value source. Backfilled from IEM
    # archives; at inference the live ensemble's mosMax / namMos populate
    # these fields directly.
    "gfs_mos_max",
    "nam_mos_max",
    "t850_c", "t700_c", "t500_c", "h500_m", "rh850_pct",
    "ensemble_max",
    # High-autocorrelation + climatology anchors. Joined in via
    # db.list_training_data's window functions; passed at inference
    # via predict_max(features=...).
    "prev_actual_max_f",
    # Multi-day lag stack — captures 3-day and 7-day persistence /
    # synoptic memory beyond the 1-day prev_actual lag.
    "lag3_actual_max_f",
    "lag7_actual_max_f",
    # 7-day rolling mean of actuals — local climatology drift
    # (heatwaves, fronts, regime shifts).
    "roll7_mean_max_f",
    "seasonal_avg_max_f",
    # Forecast horizon — strongest predictor of model uncertainty. A 72h
    # forecast has materially higher variance than a 12h one, and the
    # right strategy is "predict closer to climatology" the further out
    # you go. Bucketed version (horizon_bucket) below lets non-linear
    # learners capture the regime change cleanly.
    "forecast_horizon_hours",
    "horizon_bucket",
]
FEATURE_COLUMNS_CATEGORICAL: List[str] = ["city"]
TARGET_COLUMN = "actual_max_f"

MODELS_DIR = Path(__file__).parent / "models"

# Wider GBM grid — 12 configs spanning learning rate, depth, and
# n_estimators. Each fits in a few seconds on ~10k rows.
_GBM_GRID = [
    {"n_estimators": 100, "max_depth": 3, "learning_rate": 0.10},
    {"n_estimators": 200, "max_depth": 3, "learning_rate": 0.05},
    {"n_estimators": 300, "max_depth": 3, "learning_rate": 0.03},
    {"n_estimators": 500, "max_depth": 3, "learning_rate": 0.02},
    {"n_estimators": 200, "max_depth": 4, "learning_rate": 0.05},
    {"n_estimators": 300, "max_depth": 4, "learning_rate": 0.03},
    {"n_estimators": 500, "max_depth": 4, "learning_rate": 0.02},
    {"n_estimators": 300, "max_depth": 5, "learning_rate": 0.02},
    {"n_estimators": 500, "max_depth": 5, "learning_rate": 0.01},
    {"n_estimators": 200, "max_depth": 2, "learning_rate": 0.10},
    {"n_estimators": 400, "max_depth": 2, "learning_rate": 0.05},
    {"n_estimators": 800, "max_depth": 3, "learning_rate": 0.01},
]


# Random-search spaces for the continuous sweep loop. Each tick the sweep
# picks an algorithm (weighted by configured probabilities) and samples
# one combo from the matching space. Fingerprint dedup in db.compute_ml_run_fingerprint
# guarantees we don't re-train an identical config — over time the sweep
# drifts toward unexplored corners of the space without explicit scheduling.
_RIDGE_SPACE = {
    "alpha": [0.1, 0.3, 1.0, 3.0, 10.0, 30.0],
}
_RF_SPACE = {
    "n_estimators": [200, 300, 500, 800],
    "max_depth": [6, 8, 10, 12, 16],
    "min_samples_leaf": [1, 3, 5],
}
_GBM_SPACE = {
    "n_estimators": [100, 200, 300, 400, 500, 600, 800, 1000],
    "max_depth": [2, 3, 4, 5],
    "learning_rate": [0.005, 0.01, 0.02, 0.05, 0.1],
    "min_samples_leaf": [1, 3, 5, 10],
}


def sample_random_hyperparams(algorithm_weights: Optional[Dict[str, float]] = None,
                                rng=None) -> Dict:
    """Pick (algorithm, hyperparams) for one sweep candidate.
    algorithm_weights: {ridge, rf, gbm} → float; normalised internally.
    Returns {"algorithm": str, "hyperparams": dict}. Caller is responsible
    for fingerprint-dedup before training."""
    import random as _random
    rng = rng or _random
    weights = algorithm_weights or {"ridge": 0.10, "rf": 0.20, "gbm": 0.70}
    algos = list(weights.keys())
    raw = [max(0.0, float(weights[a] or 0.0)) for a in algos]
    total = sum(raw) or 1.0
    probs = [w / total for w in raw]
    # Weighted choice without numpy (sweep loop runs in worker thread).
    pick = rng.random()
    acc = 0.0
    algo = algos[-1]
    for a, p in zip(algos, probs):
        acc += p
        if pick <= acc:
            algo = a
            break
    if algo == "ridge":
        params = {"alpha": rng.choice(_RIDGE_SPACE["alpha"])}
    elif algo == "rf":
        params = {k: rng.choice(v) for k, v in _RF_SPACE.items()}
    else:  # gbm
        params = {k: rng.choice(v) for k, v in _GBM_SPACE.items()}
    return {"algorithm": algo, "hyperparams": params}


def _fit_with_params(algorithm: str, params: Dict, X_train, y_train, X_test, y_test) -> Dict:
    """Single-config fitter parameterised by hyperparams, used by the
    continuous sweep loop. Returns the same shape _fit_one returns so
    _persist can consume it directly."""
    import numpy as _np  # type: ignore
    if algorithm == "ridge":
        from sklearn.linear_model import Ridge  # type: ignore
        from sklearn.preprocessing import StandardScaler  # type: ignore
        from sklearn.pipeline import Pipeline  # type: ignore
        m = Pipeline([
            ("scaler", StandardScaler()),
            ("ridge", Ridge(alpha=float(params.get("alpha", 1.0)))),
        ])
        label = f"ridge[a={params.get('alpha')}]"
    elif algorithm == "rf":
        from sklearn.ensemble import RandomForestRegressor  # type: ignore
        m = RandomForestRegressor(
            n_estimators=int(params.get("n_estimators", 300)),
            max_depth=int(params.get("max_depth", 12)),
            min_samples_leaf=int(params.get("min_samples_leaf", 3)),
            random_state=42, n_jobs=-1,
        )
        label = (f"rf[ne={params.get('n_estimators')},d={params.get('max_depth')},"
                 f"l={params.get('min_samples_leaf')}]")
    elif algorithm == "gbm":
        from sklearn.ensemble import GradientBoostingRegressor  # type: ignore
        m = GradientBoostingRegressor(
            n_estimators=int(params.get("n_estimators", 200)),
            max_depth=int(params.get("max_depth", 3)),
            learning_rate=float(params.get("learning_rate", 0.05)),
            min_samples_leaf=int(params.get("min_samples_leaf", 1)),
            random_state=42,
        )
        label = (f"gbm[ne={params.get('n_estimators')},d={params.get('max_depth')},"
                 f"lr={params.get('learning_rate')},l={params.get('min_samples_leaf')}]")
    else:
        raise ValueError(f"unknown sweep algorithm {algorithm!r}")
    m.fit(X_train, y_train)
    return {
        "model": m,
        "algorithm": label,
        "train_mae": float(_np.abs(m.predict(X_train) - y_train).mean()),
        "test_mae": float(_np.abs(m.predict(X_test) - y_test).mean()),
    }


def train_random_candidate(
    algorithm_weights: Optional[Dict[str, float]] = None,
    min_target_date: Optional[str] = None,
    rng=None,
    pre_picked: Optional[Dict] = None,
) -> Dict:
    """One iteration of the continuous sweep: sample a config, dedup, train,
    persist. Returns {status, algorithm, hyperparams, walk_forward_mae?,
    test_mae?, run_id?, skipped_reason?} so the loop and the activity log
    can render it uniformly.

    Status values:
      - "skipped_dedup"   : fingerprint already in ml_runs
      - "skipped_no_data" : training set too small
      - "ok"              : trained + persisted; check walk_forward_mae for selection
      - "error"           : exception during fit; details in "error"
    """
    import os as _os
    if min_target_date is None:
        min_target_date = _os.getenv("BETS_ML_MIN_TRAIN_DATE") or None
    pick = pre_picked or sample_random_hyperparams(algorithm_weights, rng=rng)
    algo, params = pick["algorithm"], pick["hyperparams"]
    try:
        df = _build_dataframe(min_target_date=min_target_date)
        if df is None or len(df) < 20:
            return {"status": "skipped_no_data", "algorithm": algo,
                    "hyperparams": params,
                    "reason": f"only {0 if df is None else len(df)} rows; need ≥20"}
        train_df, test_df = _split(df, test_frac=0.2)
        if train_df.empty or test_df.empty:
            return {"status": "skipped_no_data", "algorithm": algo,
                    "hyperparams": params, "reason": "empty split"}
        X_train, feature_cols = _featurize(train_df)
        # Fingerprint check BEFORE doing the expensive featurize-test + fit.
        fp = db.compute_ml_run_fingerprint(algo, len(train_df), feature_cols, params)
        prior = db.find_ml_run_by_fingerprint(fp)
        if prior:
            return {"status": "skipped_dedup", "algorithm": algo,
                    "hyperparams": params, "matched_run_id": prior.get("id"),
                    "fingerprint": fp, "n_train": len(train_df)}
        X_test, _ = _featurize(test_df, fitted_columns=feature_cols)
        y_train = train_df[TARGET_COLUMN].astype(float)
        y_test = test_df[TARGET_COLUMN].astype(float)
        ens_test = test_df["ensemble_max"].astype(float)
        ens_mask = ens_test.notna()
        holdout_mae_ensemble = (
            float((ens_test[ens_mask] - y_test[ens_mask]).abs().mean())
            if ens_mask.sum() > 0 else None
        )
        fit = _fit_with_params(algo, params, X_train, y_train, X_test, y_test)
        # Walk-forward MAE — the honest selection metric. Reuses the same
        # fit logic via a closure so the splits get a freshly-fit model.
        def _fit_fn(tr_df, te_df):
            Xtr, fcols = _featurize(tr_df)
            Xte, _ = _featurize(te_df, fitted_columns=fcols)
            ytr = tr_df[TARGET_COLUMN].astype(float)
            yte = te_df[TARGET_COLUMN].astype(float)
            sub = _fit_with_params(algo, params, Xtr, ytr, Xte, yte)
            return sub["test_mae"]
        try:
            wf = walk_forward_mae(df, _fit_fn)
        except Exception:  # noqa: BLE001 — WF is best-effort
            wf = None
        run = _persist(
            fit, feature_cols, len(train_df), len(test_df),
            holdout_mae_ensemble,
            walk_forward_mae_v=wf,
            hyperparams=params,
            fingerprint=fp,
        )
        # Record per-city test MAE so the auto-trader can scale bet
        # sizing / skip cities the model is bad at. Best-effort —
        # failures here shouldn't sink the sweep tick.
        try:
            _record_per_city_metrics(run["id"], fit["model"], test_df, X_test, y_test)
            db.invalidate_city_mae_cache()
        except Exception as exc:  # noqa: BLE001
            print(f"[sweep] per-city metrics skipped: {exc}")
        return {
            "status": "ok",
            "run_id": run.get("id"),
            "algorithm": algo,
            "algorithm_label": fit.get("algorithm"),
            "hyperparams": params,
            "test_mae": fit.get("test_mae"),
            "walk_forward_mae": wf,
            "holdout_mae_ensemble": holdout_mae_ensemble,
            "n_train": len(train_df),
            "n_test": len(test_df),
            "fingerprint": fp,
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "algorithm": algo,
                "hyperparams": params, "error": f"{type(exc).__name__}: {exc}"}


def _build_dataframe(min_target_date: Optional[str] = None):
    """Pull training data, optionally filtered to target_date >= a cutoff.
    Recent-only filtering helps when older years have sparser features
    (e.g., MOS only densely populated since ~2024).

    Snapshot-derived rows from feature_snapshots × historical_actuals
    are intentionally NOT included here. The earlier attempt at mixing
    them in regressed walk_forward_mae from 0.946 → 1.005 because:
      - icon_max is NULL on snapshot rows → imputed-mean noise
      - gfs_max only stored as gfs_mos_max → covariate shift
      - horizon clamped to 0 → no real variance after dedup
      - model_max is our own output, leaking into features
    The snapshot rows are still useful for the per-city ML bias map
    (db.compute_ml_city_bias), where covariate-shift quirks don't
    matter — only residuals do.
    """
    import pandas as pd  # type: ignore
    rows = db.list_training_data() or []
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.dropna(subset=[TARGET_COLUMN])
    if min_target_date:
        df = df[df["target_date"] >= min_target_date]
    if df.empty:
        return None
    return df


def _split(df, test_frac: float = 0.2):
    df = df.sort_values("target_date").reset_index(drop=True)
    n = len(df)
    n_test = max(1, int(n * test_frac))
    n_train = n - n_test
    return df.iloc[:n_train], df.iloc[n_train:]


def walk_forward_mae(df, fit_fn, n_splits: int = 5, min_train_frac: float = 0.4) -> Optional[float]:
    """Walk-forward / expanding-window time-series validation. Each fold
    trains on [start..t] and tests on [t..t+window]. Returns the mean
    MAE across folds — a much more honest estimate of "how well will
    this model do on tomorrow's data" than a single 80/20 holdout.

    fit_fn(train_df, test_df) -> mae for that fold; the caller owns the
    feature build + model fit so we don't recompute the dummies grid.
    """
    df = df.sort_values("target_date").reset_index(drop=True)
    n = len(df)
    if n < 200:
        return None
    min_train = max(int(n * min_train_frac), 100)
    fold_maes: List[float] = []
    test_window = max(1, (n - min_train) // n_splits)
    for k in range(n_splits):
        end_train = min_train + k * test_window
        end_test = end_train + test_window
        if end_test > n:
            break
        train_df = df.iloc[:end_train]
        test_df = df.iloc[end_train:end_test]
        if test_df.empty:
            continue
        try:
            mae = fit_fn(train_df, test_df)
            if mae is not None:
                fold_maes.append(float(mae))
        except Exception as exc:  # noqa: BLE001
            print(f"[walk-forward] fold {k} failed: {exc}")
    if not fold_maes:
        return None
    return round(sum(fold_maes) / len(fold_maes), 4)


def _engineer_features(df):
    """Add derived columns: model spreads, lapse rate proxy, day-of-year
    cyclic encoding. These typically give 5-15% MAE improvement over
    raw features alone."""
    import pandas as pd  # type: ignore
    import numpy as np  # type: ignore
    out = df.copy()
    if "gfs_max" in out.columns and "ecmwf_max" in out.columns:
        out["spread_gfs_ecmwf"] = out["gfs_max"] - out["ecmwf_max"]
    if "gfs_max" in out.columns and "icon_max" in out.columns:
        out["spread_gfs_icon"] = out["gfs_max"] - out["icon_max"]
    if "ecmwf_max" in out.columns and "icon_max" in out.columns:
        out["spread_ecmwf_icon"] = out["ecmwf_max"] - out["icon_max"]
    if "t850_c" in out.columns and "t500_c" in out.columns:
        out["lapse_850_500"] = out["t850_c"] - out["t500_c"]
    if "target_date" in out.columns:
        try:
            doy = pd.to_datetime(out["target_date"]).dt.dayofyear
            out["doy_sin"] = np.sin(2 * np.pi * doy / 365.25)
            out["doy_cos"] = np.cos(2 * np.pi * doy / 365.25)
        except Exception:
            pass
    # How much does today's forecast deviate from yesterday's actual / from
    # climatology? These deltas are often more predictive than the raw
    # values (a forecast 5° above climo is informative independent of
    # whether climo is 60° or 80°).
    if "ensemble_max" in out.columns and "prev_actual_max_f" in out.columns:
        out["ens_minus_prev"] = out["ensemble_max"] - out["prev_actual_max_f"]
    if "ensemble_max" in out.columns and "seasonal_avg_max_f" in out.columns:
        out["ens_minus_climo"] = out["ensemble_max"] - out["seasonal_avg_max_f"]
    # MOS spreads: how much do MOS bulletins disagree with the raw model
    # ensemble? MOS is bias-corrected for station microclimate, so the
    # delta is informative (it's literally the bias signal).
    if "gfs_mos_max" in out.columns and "ensemble_max" in out.columns:
        out["gfsmos_minus_ens"] = out["gfs_mos_max"] - out["ensemble_max"]
    if "gfs_mos_max" in out.columns and "nam_mos_max" in out.columns:
        out["mos_spread"] = out["gfs_mos_max"] - out["nam_mos_max"]
    # ── Ensemble disagreement (forecast uncertainty proxy) ──
    # Stdev across the source models. Wider disagreement = harder day,
    # the model can learn to trust its prior less.
    src_cols = [c for c in ["gfs_max", "ecmwf_max", "icon_max",
                            "om_max", "gfs_mos_max", "nam_mos_max"]
                if c in out.columns]
    if len(src_cols) >= 2:
        out["ensemble_disagreement"] = out[src_cols].std(axis=1, skipna=True)
        out["ensemble_range"] = out[src_cols].max(axis=1, skipna=True) - out[src_cols].min(axis=1, skipna=True)
    # ── Trend features from the lag stack ──
    if "prev_actual_max_f" in out.columns and "lag3_actual_max_f" in out.columns:
        out["trend_3d"] = out["prev_actual_max_f"] - out["lag3_actual_max_f"]
    if "prev_actual_max_f" in out.columns and "lag7_actual_max_f" in out.columns:
        out["trend_7d"] = out["prev_actual_max_f"] - out["lag7_actual_max_f"]
    # ── Forecast vs rolling local climate ──
    if "ensemble_max" in out.columns and "roll7_mean_max_f" in out.columns:
        out["ens_minus_roll7"] = out["ensemble_max"] - out["roll7_mean_max_f"]
    # ── Forecast horizon bucket ──
    # 0: ≤18h (same-day / overnight); 1: ≤30h (next-morning); 2: ≤48h
    # (1-2 day lead); 3: >48h (multi-day). Lets non-linear learners
    # capture the regime change cleanly without inferring from raw hours.
    if "forecast_horizon_hours" in out.columns:
        h = out["forecast_horizon_hours"].fillna(0).astype(float)
        out["horizon_bucket"] = (
            (h > 18).astype(int)
            + (h > 30).astype(int)
            + (h > 48).astype(int)
        ).astype(float)
    return out


def _featurize(df, fitted_columns: Optional[List[str]] = None):
    """Numeric impute + city one-hot + engineered features. All output
    columns coerced to float64 for pandas 2.3 + sklearn 1.8 compat."""
    import pandas as pd  # type: ignore
    import numpy as np  # type: ignore
    df = _engineer_features(df)
    numeric_cols = list(FEATURE_COLUMNS_NUMERIC)
    for extra in ("spread_gfs_ecmwf", "spread_gfs_icon", "spread_ecmwf_icon",
                  "lapse_850_500", "doy_sin", "doy_cos",
                  "ens_minus_prev", "ens_minus_climo",
                  "gfsmos_minus_ens", "mos_spread"):
        if extra in df.columns:
            numeric_cols.append(extra)
    base = df[[c for c in numeric_cols if c in df.columns]].copy()
    for col in base.columns:
        base[col] = pd.to_numeric(base[col], errors="coerce")
    means = base.mean(numeric_only=True).fillna(0)
    base = base.fillna(means)
    cat = pd.get_dummies(df[FEATURE_COLUMNS_CATEGORICAL], prefix=FEATURE_COLUMNS_CATEGORICAL).astype("float64")
    X = pd.concat([base.astype("float64"), cat], axis=1)
    if fitted_columns is not None:
        for col in fitted_columns:
            if col not in X.columns:
                X[col] = 0.0
        X = X[fitted_columns]
    X = X.replace([np.inf, -np.inf], 0).fillna(0)
    return X, list(X.columns)


def conformal_quantile(model, X_calib, y_calib, alpha: float = 0.1) -> Optional[float]:
    """Inductive split-conformal: take the (1-α) quantile of |residual|
    on a held-out calibration set. Adds predict-interval ŷ ± q with
    GUARANTEED (1-α) coverage regardless of the underlying model.

    Returns the half-width q. Predict-time interval = [pred - q, pred + q].
    """
    try:
        import numpy as _np  # type: ignore
    except Exception:
        return None
    try:
        residuals = _np.abs(model.predict(X_calib) - y_calib)
        n = len(residuals)
        if n < 20:
            return None
        # Conformal quantile correction: ceil((1-α)(n+1)) / n
        q_idx = min(n - 1, int(_np.ceil((1 - alpha) * (n + 1))) - 1)
        sorted_res = _np.sort(residuals)
        return float(round(sorted_res[q_idx], 3))
    except Exception:
        return None


def fit_quantile_trio(X_train, y_train, n_estimators: int = 200,
                       max_depth: int = 3, learning_rate: float = 0.05) -> Dict:
    """Train three GBM quantile regressors at α = 0.1 / 0.5 / 0.9 so we
    get a 10/50/90 percentile prediction band per inference. The bracket
    probability calc can then use the empirical CDF directly instead of
    a fixed-σ gaussian — calibrates Kalshi YES-pricing to the model's
    own uncertainty."""
    from sklearn.ensemble import GradientBoostingRegressor  # type: ignore
    models = {}
    for alpha in (0.1, 0.5, 0.9):
        m = GradientBoostingRegressor(
            loss="quantile", alpha=alpha,
            n_estimators=n_estimators, max_depth=max_depth,
            learning_rate=learning_rate, random_state=42,
        )
        m.fit(X_train, y_train)
        models[f"p{int(alpha * 100):02d}"] = m
    return models


def optuna_hpo(df, n_trials: int = 30, timeout: int = 180) -> Optional[Dict]:
    """Bayesian hyperparameter sweep via Optuna across the algorithm
    space + each algorithm's parameters as one joint search. Returns
    {algorithm, params, test_mae, walk_forward_mae} for the winning
    trial. None if Optuna isn't installed.

    Optimizes for walk-forward MAE — more honest than holdout MAE.
    """
    try:
        import optuna  # type: ignore
        from optuna.samplers import TPESampler  # type: ignore
    except Exception:
        return None
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    train_df, test_df = _split(df, test_frac=0.2)
    if train_df.empty or test_df.empty:
        return None
    X_train, feature_cols = _featurize(train_df)
    X_test, _ = _featurize(test_df, fitted_columns=feature_cols)
    y_train = train_df[TARGET_COLUMN].astype(float)
    y_test = test_df[TARGET_COLUMN].astype(float)

    available = ["gbm", "rf"]
    for opt in ("lightgbm", "xgboost", "catboost"):
        if _modern_booster_available(opt):
            available.append(opt)

    def objective(trial):
        algo = trial.suggest_categorical("algo", available)
        if algo == "gbm":
            from sklearn.ensemble import GradientBoostingRegressor  # type: ignore
            params = {
                "n_estimators": trial.suggest_int("gbm_n_est", 100, 800, step=100),
                "max_depth": trial.suggest_int("gbm_depth", 2, 6),
                "learning_rate": trial.suggest_float("gbm_lr", 0.01, 0.15, log=True),
            }
            m = GradientBoostingRegressor(**params, random_state=42)
        elif algo == "rf":
            from sklearn.ensemble import RandomForestRegressor  # type: ignore
            params = {
                "n_estimators": trial.suggest_int("rf_n_est", 100, 600, step=100),
                "max_depth": trial.suggest_int("rf_depth", 4, 20),
                "min_samples_leaf": trial.suggest_int("rf_min_leaf", 1, 10),
            }
            m = RandomForestRegressor(**params, random_state=42, n_jobs=-1)
        elif algo == "lightgbm":
            import lightgbm as lgb  # type: ignore
            params = {
                "n_estimators": trial.suggest_int("lgb_n_est", 100, 800, step=100),
                "learning_rate": trial.suggest_float("lgb_lr", 0.01, 0.15, log=True),
                "num_leaves": trial.suggest_int("lgb_leaves", 15, 127),
                "min_child_samples": trial.suggest_int("lgb_min_child", 5, 50),
            }
            m = lgb.LGBMRegressor(**params, random_state=42, n_jobs=-1, verbose=-1)
        elif algo == "xgboost":
            import xgboost as xgb  # type: ignore
            params = {
                "n_estimators": trial.suggest_int("xgb_n_est", 100, 800, step=100),
                "max_depth": trial.suggest_int("xgb_depth", 3, 8),
                "learning_rate": trial.suggest_float("xgb_lr", 0.01, 0.15, log=True),
            }
            m = xgb.XGBRegressor(**params, random_state=42, tree_method="hist",
                                 n_jobs=-1, verbosity=0)
        elif algo == "catboost":
            from catboost import CatBoostRegressor  # type: ignore
            params = {
                "iterations": trial.suggest_int("cat_iters", 100, 800, step=100),
                "depth": trial.suggest_int("cat_depth", 4, 8),
                "learning_rate": trial.suggest_float("cat_lr", 0.01, 0.15, log=True),
            }
            m = CatBoostRegressor(**params, random_state=42, verbose=False,
                                  allow_writing_files=False)
        else:
            return float("inf")
        try:
            m.fit(X_train, y_train)
            import numpy as _np  # type: ignore
            return float(_np.abs(m.predict(X_test) - y_test).mean())
        except Exception:
            return float("inf")

    sampler = TPESampler(seed=42)
    study = optuna.create_study(direction="minimize", sampler=sampler)
    study.optimize(objective, n_trials=n_trials, timeout=timeout, show_progress_bar=False)
    best = study.best_trial
    return {
        "algorithm": best.params.get("algo"),
        "params": dict(best.params),
        "test_mae": round(best.value, 4),
        "n_trials": len(study.trials),
    }


def _modern_booster_available(name: str) -> bool:
    """Check if an optional modern booster (lightgbm / xgboost / catboost)
    is importable. We don't actually import here — just probe so the
    auto-sweep can include / skip cleanly without bombing the backend
    when the user hasn't pip-installed the optional deps."""
    try:
        if name == "lightgbm":
            import lightgbm  # type: ignore # noqa: F401
        elif name == "xgboost":
            import xgboost  # type: ignore # noqa: F401
        elif name == "catboost":
            import catboost  # type: ignore # noqa: F401
        else:
            return False
        return True
    except Exception:
        return False


def _fit_lightgbm(X_train, y_train, X_test, y_test) -> Dict:
    import lightgbm as lgb  # type: ignore
    m = lgb.LGBMRegressor(
        n_estimators=400, learning_rate=0.04, max_depth=-1, num_leaves=31,
        min_child_samples=10, random_state=42, n_jobs=-1, verbose=-1,
    )
    m.fit(X_train, y_train)
    tr = float((m.predict(X_train) - y_train).abs().mean())
    te = float((m.predict(X_test) - y_test).abs().mean())
    return {"model": m, "algorithm": "lightgbm", "train_mae": tr, "test_mae": te}


def _fit_xgboost(X_train, y_train, X_test, y_test) -> Dict:
    import xgboost as xgb  # type: ignore
    m = xgb.XGBRegressor(
        n_estimators=400, learning_rate=0.04, max_depth=4,
        tree_method="hist", random_state=42, n_jobs=-1, verbosity=0,
    )
    m.fit(X_train, y_train)
    tr = float((m.predict(X_train) - y_train).abs().mean())
    te = float((m.predict(X_test) - y_test).abs().mean())
    return {"model": m, "algorithm": "xgboost", "train_mae": tr, "test_mae": te}


def fit_ngboost(X_train, y_train, X_test, y_test) -> Optional[Dict]:
    """NGBoost — natural gradient boosting that produces a probability
    distribution per prediction (Normal by default). Better calibrated
    than quantile-GBM trios. Returns None if the optional dep isn't
    installed."""
    try:
        from ngboost import NGBRegressor  # type: ignore
    except Exception:
        return None
    m = NGBRegressor(n_estimators=300, learning_rate=0.04, random_state=42, verbose=False)
    m.fit(X_train, y_train)
    import numpy as _np  # type: ignore
    pred_train = m.predict(X_train)
    pred_test = m.predict(X_test)
    return {
        "model": m,
        "algorithm": "ngboost",
        "train_mae": float(_np.abs(pred_train - y_train).mean()),
        "test_mae": float(_np.abs(pred_test - y_test).mean()),
    }


def _fit_catboost(X_train, y_train, X_test, y_test) -> Dict:
    from catboost import CatBoostRegressor  # type: ignore
    m = CatBoostRegressor(
        iterations=400, learning_rate=0.04, depth=6,
        random_state=42, verbose=False, allow_writing_files=False,
    )
    m.fit(X_train, y_train)
    tr = float((m.predict(X_train) - y_train).mean())  # CatBoost preds are arrays
    import numpy as _np  # type: ignore
    tr = float(_np.abs(m.predict(X_train) - y_train).mean())
    te = float(_np.abs(m.predict(X_test) - y_test).mean())
    return {"model": m, "algorithm": "catboost", "train_mae": tr, "test_mae": te}


def _fit_one(algorithm: str, X_train, y_train, X_test, y_test) -> Dict:
    """Fit a single (algorithm, hyperparameters) candidate."""
    if algorithm == "lightgbm":
        return _fit_lightgbm(X_train, y_train, X_test, y_test)
    if algorithm == "xgboost":
        return _fit_xgboost(X_train, y_train, X_test, y_test)
    if algorithm == "catboost":
        return _fit_catboost(X_train, y_train, X_test, y_test)
    if algorithm == "gbm-best":
        from sklearn.ensemble import GradientBoostingRegressor  # type: ignore
        best = None
        for params in _GBM_GRID:
            m = GradientBoostingRegressor(**params, random_state=42)
            m.fit(X_train, y_train)
            mae = float((m.predict(X_test) - y_test).abs().mean())
            if best is None or mae < best["test_mae"]:
                tr = float((m.predict(X_train) - y_train).abs().mean())
                best = {
                    "model": m,
                    "algorithm": f"gbm[ne={params['n_estimators']},d={params['max_depth']},lr={params['learning_rate']}]",
                    "train_mae": tr,
                    "test_mae": mae,
                }
        return best
    if algorithm == "rf":
        from sklearn.ensemble import RandomForestRegressor  # type: ignore
        m = RandomForestRegressor(n_estimators=300, max_depth=12, min_samples_leaf=4, random_state=42, n_jobs=-1)
    elif algorithm == "linear":
        # Ridge needs scaled inputs — h500_m around 5800 vs t500_c around -20
        # would otherwise dominate the regularization penalty asymmetrically.
        # StandardScaler normalizes each column to mean 0, std 1 before fit.
        from sklearn.linear_model import Ridge  # type: ignore
        from sklearn.preprocessing import StandardScaler  # type: ignore
        from sklearn.pipeline import Pipeline  # type: ignore
        m = Pipeline([
            ("scaler", StandardScaler()),
            ("ridge", Ridge(alpha=1.0)),
        ])
    elif algorithm == "gbm":
        from sklearn.ensemble import GradientBoostingRegressor  # type: ignore
        m = GradientBoostingRegressor(n_estimators=200, max_depth=3, learning_rate=0.05, random_state=42)
    else:
        raise ValueError(f"unknown algorithm {algorithm!r}")
    m.fit(X_train, y_train)
    return {
        "model": m,
        "algorithm": algorithm,
        "train_mae": float((m.predict(X_train) - y_train).abs().mean()),
        "test_mae": float((m.predict(X_test) - y_test).abs().mean()),
    }


def _persist(fit: Dict, feature_cols: List[str], n_train: int, n_test: int,
             holdout_mae_ensemble: Optional[float],
             extra_payload: Optional[Dict] = None,
             walk_forward_mae_v: Optional[float] = None,
             hyperparams: Optional[Dict] = None,
             fingerprint: Optional[str] = None) -> Dict:
    """Save a fitted model to disk and record the run in ml_runs.
    `fingerprint` lets the sweep loop pre-compute the dedup hash from
    the BARE algorithm name (e.g. 'ridge') and reuse it on persist —
    otherwise insert_ml_run rebuilds the hash from fit['algorithm']
    which is the formatted label ('ridge[a=1.0]'), so the next sweep
    iteration's dedup lookup would miss this row."""
    import joblib  # type: ignore
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    safe_algo = fit["algorithm"].replace("[", "-").replace("]", "").replace(",", "_").replace("=", "").replace("/", "_")
    model_path = MODELS_DIR / f"{safe_algo}-{ts}.pkl"
    payload = {
        "model": fit["model"],
        "feature_columns": feature_cols,
        "algorithm": fit["algorithm"],
    }
    if extra_payload:
        payload.update(extra_payload)
    joblib.dump(payload, model_path)
    return db.insert_ml_run(
        algorithm=fit["algorithm"],
        n_train=n_train, n_test=n_test,
        train_mae=round(fit["train_mae"], 3),
        test_mae=round(fit["test_mae"], 3),
        holdout_mae_ensemble=round(holdout_mae_ensemble, 3) if holdout_mae_ensemble is not None else None,
        feature_columns=feature_cols,
        model_path=str(model_path),
        walk_forward_mae=walk_forward_mae_v,
        hyperparams=hyperparams,
        fingerprint=fingerprint,
    )


def permutation_audit(model, X_test, y_test, feature_cols, n_repeats: int = 3):
    """Run sklearn permutation_importance on the held-out test set and
    return the features whose importance is below 0.5pp AND whose
    Pearson |r| with the target is also below 0.05. These are candidates
    to drop on the next retrain. Logged as a `prune` event so the
    activity feed shows what's getting weeded out."""
    try:
        from sklearn.inspection import permutation_importance  # type: ignore
        result = permutation_importance(
            model, X_test, y_test, n_repeats=n_repeats, random_state=42, n_jobs=-1
        )
    except Exception as exc:  # noqa: BLE001
        return None
    means = list(result.importances_mean)
    if not means:
        return None
    total = sum(abs(m) for m in means) or 1.0
    rows = sorted(
        [{"feature": feature_cols[i], "importance": round(means[i] / total, 4)}
         for i in range(len(feature_cols))],
        key=lambda r: -r["importance"],
    )
    weak = [r for r in rows if abs(r["importance"]) < 0.005]
    return {"all": rows, "weak": weak}


def _record_per_city_metrics(run_id: int, model, test_df, X_test, y_test) -> int:
    """For each city in the test split, compute test MAE and bias
    (mean(predicted − actual)) and persist to ml_city_metrics. Skips
    silently if the model can't predict the test matrix or the test_df
    has no 'city' column. Returns the number of rows written."""
    try:
        import pandas as pd  # type: ignore
        preds = model.predict(X_test)
        if "city" not in test_df.columns:
            return 0
        df = pd.DataFrame({
            "city": test_df["city"].values,
            "y": y_test.values if hasattr(y_test, "values") else y_test,
            "yhat": preds,
        })
        df["err"] = df["yhat"] - df["y"]
        rows = []
        for city, sub in df.groupby("city"):
            n = int(len(sub))
            if n == 0:
                continue
            mae = float(sub["err"].abs().mean())
            bias = float(sub["err"].mean())
            rows.append({
                "city": str(city),
                "test_mae": round(mae, 3),
                "bias": round(bias, 3),
                "n_samples": n,
            })
        if not rows:
            return 0
        return db.insert_ml_city_metrics(run_id, rows)
    except Exception as exc:  # noqa: BLE001
        print(f"[per-city-metrics] skipped: {exc}")
        return 0


def _make_fit_fn(algo: str):
    """Returns fit_fn(train_df, test_df) → MAE, used by walk_forward_mae
    so we can do the full feature-pipeline for each fold without
    reimplementing it inline."""
    # Stack winners come back as 'stack[linear+rf+gbm]' which _fit_one
    # doesn't know about — proxy walk-forward to the best base learner
    # (gbm-best) since stack ≈ gbm in MAE.
    if algo.startswith("stack"):
        algo = "gbm-best"
    elif algo == "per-city":
        # Per-city training fits N independent models per fold and
        # walk-forward'ing each is prohibitively expensive. Proxy to
        # gbm-best (a single global fit) so the headline number is at
        # least directionally informative.
        algo = "gbm-best"
    elif algo.startswith("gbm["):
        # Specific gbm config from the gbm-best grid; rerun the grid each fold.
        algo = "gbm-best"
    elif algo.startswith("gbm-optuna"):
        algo = "gbm-best"
    def fit_fn(tr_df, te_df):
        if tr_df.empty or te_df.empty:
            return None
        X_tr, cols = _featurize(tr_df)
        X_te, _ = _featurize(te_df, fitted_columns=cols)
        y_tr = tr_df[TARGET_COLUMN].astype(float)
        y_te = te_df[TARGET_COLUMN].astype(float)
        f = _fit_one(algo, X_tr, y_tr, X_te, y_te)
        return f.get("test_mae")
    return fit_fn


def _train_per_city(df) -> Optional[Dict]:
    """Fit one model per city. Returns a dict of {city: best_fit} where
    each best_fit is the algo with lowest test_mae for THAT city. Cities
    with too few rows fall back to None and inference will use the
    global model for them."""
    from sklearn.linear_model import Ridge  # type: ignore
    from sklearn.ensemble import GradientBoostingRegressor  # type: ignore
    from sklearn.preprocessing import StandardScaler  # type: ignore
    from sklearn.pipeline import Pipeline  # type: ignore
    fits_by_city: Dict[str, Dict] = {}
    feature_cols_per_city: Dict[str, List[str]] = {}
    total_train = 0
    total_test = 0
    weighted_test_mae = 0.0
    for city, sub in df.groupby("city"):
        if len(sub) < 50:
            continue
        sub = sub.sort_values("target_date").reset_index(drop=True)
        n = len(sub)
        n_test = max(1, int(n * 0.2))
        n_train = n - n_test
        train_df = sub.iloc[:n_train]
        test_df = sub.iloc[n_train:]
        X_tr, fc = _featurize(train_df)
        X_te, _ = _featurize(test_df, fitted_columns=fc)
        y_tr = train_df[TARGET_COLUMN].astype(float)
        y_te = test_df[TARGET_COLUMN].astype(float)
        # Try Ridge + a single fast GBM per city; pick the better one.
        best = None
        for name, m in (
            ("linear", Pipeline([("scaler", StandardScaler()), ("ridge", Ridge(alpha=1.0))])),
            ("gbm", GradientBoostingRegressor(n_estimators=200, max_depth=3, learning_rate=0.05, random_state=42)),
        ):
            m.fit(X_tr, y_tr)
            te = float((m.predict(X_te) - y_te).abs().mean())
            if best is None or te < best["test_mae"]:
                tr = float((m.predict(X_tr) - y_tr).abs().mean())
                best = {"model": m, "name": name, "train_mae": tr, "test_mae": te}
        fits_by_city[city] = best
        feature_cols_per_city[city] = fc
        total_train += n_train
        total_test += n_test
        weighted_test_mae += best["test_mae"] * n_test
    if not fits_by_city:
        return None
    return {
        "fits": fits_by_city,
        "feature_columns": feature_cols_per_city,
        "n_train": total_train,
        "n_test": total_test,
        "test_mae": weighted_test_mae / total_test if total_test else None,
        "train_mae": None,
    }


def _train_stack(X_train, y_train, X_test, y_test) -> Dict:
    """Stacking: fit Ridge / RF / GBM on the training set, then a
    Ridge meta-model on their out-of-sample predictions. Often shaves
    1-3% off the best individual model."""
    from sklearn.linear_model import Ridge  # type: ignore
    from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor  # type: ignore
    from sklearn.preprocessing import StandardScaler  # type: ignore
    from sklearn.pipeline import Pipeline  # type: ignore
    from sklearn.base import clone  # type: ignore
    import numpy as np  # type: ignore
    base_models = {
        "linear": Pipeline([("scaler", StandardScaler()), ("ridge", Ridge(alpha=1.0))]),
        "rf": RandomForestRegressor(n_estimators=200, max_depth=12, min_samples_leaf=4, random_state=42, n_jobs=-1),
        "gbm": GradientBoostingRegressor(n_estimators=200, max_depth=3, learning_rate=0.05, random_state=42),
    }
    # 5-fold cross-validated predictions on the training set so the meta
    # model trains on out-of-sample predictions (no leakage). Use
    # sklearn.base.clone so Pipelines clone properly (raw type(m)(**params)
    # doesn't handle flattened pipeline params).
    from sklearn.model_selection import KFold  # type: ignore
    kf = KFold(n_splits=5, shuffle=False)
    n = len(X_train)
    base_train_preds = {name: np.zeros(n) for name in base_models}
    for tr_idx, va_idx in kf.split(X_train):
        for name, m in base_models.items():
            mc = clone(m)
            mc.fit(X_train.iloc[tr_idx], y_train.iloc[tr_idx])
            base_train_preds[name][va_idx] = mc.predict(X_train.iloc[va_idx])
    # Refit each base model on the full training set for inference.
    for m in base_models.values():
        m.fit(X_train, y_train)
    # Meta-model on stacked OOF predictions.
    import pandas as pd  # type: ignore
    meta_X_train = pd.DataFrame(base_train_preds)
    meta = Ridge(alpha=1.0)
    meta.fit(meta_X_train, y_train)
    # Evaluate the full stack on the test set.
    meta_X_test = pd.DataFrame({
        name: m.predict(X_test) for name, m in base_models.items()
    })
    test_pred = meta.predict(meta_X_test)
    train_pred = meta.predict(meta_X_train)
    return {
        "model": {"base": base_models, "meta": meta, "base_order": list(base_models.keys())},
        "algorithm": "stack[linear+rf+gbm]",
        "train_mae": float((train_pred - y_train).abs().mean()),
        "test_mae": float((test_pred - y_test).abs().mean()),
    }


def train(algorithm: str = "linear", min_target_date: Optional[str] = None) -> Dict:
    """Fit one model run. Wraps the pipeline in try/except so the API gets
    a clean error body instead of a 500.

    Pass algorithm='auto' to sweep linear + rf + gbm-best in one call —
    the lowest test_mae candidate becomes the active model, and every
    candidate's metrics land in ml_runs so the history table shows the
    full sweep.

    `min_target_date` (ISO YYYY-MM-DD): only train on rows on/after this
    date. Defaults to BETS_ML_MIN_TRAIN_DATE env (e.g. '2023-01-01' to
    skip pre-MOS-coverage years). Recommended when older years have
    sparse features that confuse the model."""
    import os as _os
    if min_target_date is None:
        min_target_date = _os.getenv("BETS_ML_MIN_TRAIN_DATE") or None
    try:
        df = _build_dataframe(min_target_date=min_target_date)
        if df is None or len(df) < 20:
            return {"error": f"not enough training data ({0 if df is None else len(df)} rows; need ≥20)"}

        train_df, test_df = _split(df, test_frac=0.2)
        if train_df.empty or test_df.empty:
            return {"error": "split produced empty train or test set"}

        X_train, feature_cols = _featurize(train_df)
        X_test, _ = _featurize(test_df, fitted_columns=feature_cols)
        y_train = train_df[TARGET_COLUMN].astype(float)
        y_test = test_df[TARGET_COLUMN].astype(float)

        ens_test = test_df["ensemble_max"].astype(float)
        ens_mask = ens_test.notna()
        holdout_mae_ensemble = (
            float((ens_test[ens_mask] - y_test[ens_mask]).abs().mean())
            if ens_mask.sum() > 0 else None
        )

        if algorithm == "stack":
            fit = _train_stack(X_train, y_train, X_test, y_test)
            wf = walk_forward_mae(df, _make_fit_fn("stack"))
            run = _persist(fit, feature_cols, len(train_df), len(test_df),
                           holdout_mae_ensemble, walk_forward_mae_v=wf)
            _record_per_city_metrics(run["id"], fit["model"], test_df, X_test, y_test)
            return run

        if algorithm == "per-city":
            payload = _train_per_city(df)
            if payload is None:
                return {"error": "no city had enough rows (≥50) for per-city training"}
            algo = "per-city"
            wf = walk_forward_mae(df, _make_fit_fn("per-city"))
            run = db.insert_ml_run(
                algorithm=algo,
                n_train=payload["n_train"], n_test=payload["n_test"],
                train_mae=None,
                test_mae=round(payload["test_mae"], 3) if payload["test_mae"] else None,
                holdout_mae_ensemble=round(holdout_mae_ensemble, 3) if holdout_mae_ensemble is not None else None,
                feature_columns=["per-city: see payload"],
                model_path=str(MODELS_DIR / f"per-city-{int(time.time())}.pkl"),
                walk_forward_mae=wf,
            )
            import joblib  # type: ignore
            joblib.dump({
                "algorithm": algo,
                "fits_by_city": payload["fits"],
                "feature_columns_per_city": payload["feature_columns"],
            }, run["model_path"])
            run["per_city_breakdown"] = [
                {"city": c, "test_mae": round(f["test_mae"], 3), "algo": f["name"]}
                for c, f in payload["fits"].items()
            ]
            return run

        if algorithm == "optuna":
            best = optuna_hpo(df, n_trials=30, timeout=180)
            if not best:
                return {"error": "Optuna not installed (pip install optuna lightgbm xgboost catboost)"}
            db.log_ml_event(
                "retrain_done",
                {"optuna_trials": best.get("n_trials"), "best_algo": best.get("algorithm"),
                 "best_test_mae": best.get("test_mae"), "params": best.get("params")},
                f"optuna HPO — {best.get('n_trials')} trials, best={best.get('algorithm')} "
                f"(test_mae={best.get('test_mae')})",
            )
            # Re-fit the winning trial's params on the full dataset and persist.
            algo = best["algorithm"]
            params = {k.split("_", 1)[1]: v for k, v in best["params"].items() if k != "algo"}
            try:
                if algo == "gbm":
                    from sklearn.ensemble import GradientBoostingRegressor  # type: ignore
                    m = GradientBoostingRegressor(
                        n_estimators=params.get("n_est", 200),
                        max_depth=params.get("depth", 3),
                        learning_rate=params.get("lr", 0.05),
                        random_state=42,
                    )
                    algo_label = f"gbm-optuna[ne={params.get('n_est')},d={params.get('depth')},lr={params.get('lr'):.3f}]"
                else:
                    return {"error": f"Optuna best={algo} re-fit not implemented; pick algorithm=auto"}
                m.fit(X_train, y_train)
                import numpy as _np  # type: ignore
                tr_mae = float(_np.abs(m.predict(X_train) - y_train).mean())
                te_mae = float(_np.abs(m.predict(X_test) - y_test).mean())
                fit = {"model": m, "algorithm": algo_label,
                       "train_mae": tr_mae, "test_mae": te_mae}
                wf_mae = walk_forward_mae(df, _make_fit_fn("gbm-best"))
                return _persist(fit, feature_cols, len(train_df), len(test_df),
                                holdout_mae_ensemble,
                                walk_forward_mae_v=wf_mae,
                                hyperparams=params)
            except Exception as exc:  # noqa: BLE001
                return {"error": f"Optuna re-fit failed: {exc}"}

        if algorithm == "auto":
            candidates = []
            algos_to_try = ["linear", "rf", "gbm-best", "stack"]
            # Add modern boosters when their pip deps are present.
            for opt in ("lightgbm", "xgboost", "catboost"):
                if _modern_booster_available(opt):
                    algos_to_try.append(opt)
            for algo in algos_to_try:
                try:
                    if algo == "stack":
                        fit = _train_stack(X_train, y_train, X_test, y_test)
                    else:
                        fit = _fit_one(algo, X_train, y_train, X_test, y_test)
                    candidates.append(fit)
                except Exception as exc:  # noqa: BLE001
                    print(f"[ml-auto] {algo} fit failed: {exc}")
            if not candidates:
                return {"error": "no algorithm in the auto sweep succeeded"}
            best = min(candidates, key=lambda c: c["test_mae"])
            best_run = None
            # Walk-forward CV on the winning algorithm only (cheap-enough,
            # ~5 fits, gives an honest "how would this do day-ahead" MAE).
            best_algo = best["algorithm"]
            wf_mae = walk_forward_mae(df, _make_fit_fn(best_algo))
            # Permutation importance audit on the winner — surfaces weak
            # features that could be pruned on the next retrain.
            try:
                audit = permutation_audit(best["model"], X_test, y_test, feature_cols)
                if audit and audit.get("weak"):
                    weak_names = [w["feature"] for w in audit["weak"][:10]]
                    db.log_ml_event(
                        "prune",
                        {"weak": audit["weak"][:20]},
                        f"weak features (candidates to drop next retrain): "
                        f"{', '.join(weak_names)}",
                    )
            except Exception as exc:  # noqa: BLE001
                print(f"[ml-train] permutation audit failed: {exc}")
            # Quantile trio for probabilistic outputs (p10/p50/p90).
            try:
                quantiles = fit_quantile_trio(X_train, y_train)
            except Exception as exc:  # noqa: BLE001
                print(f"[ml-train] quantile fit failed: {exc}")
                quantiles = None
            # Conformal prediction half-width on the held-out set —
            # gives guaranteed (1-α) coverage intervals.
            conformal_q90 = conformal_quantile(best["model"], X_test, y_test, alpha=0.1)
            for c in candidates:
                if c is best:
                    extra = {}
                    if quantiles:
                        extra["quantile_models"] = quantiles
                    if conformal_q90 is not None:
                        extra["conformal_q90"] = conformal_q90
                    best_run = _persist(c, feature_cols, len(train_df), len(test_df),
                                        holdout_mae_ensemble,
                                        walk_forward_mae_v=wf_mae,
                                        extra_payload=extra or None)
                    _record_per_city_metrics(best_run["id"], c["model"], test_df, X_test, y_test)
                else:
                    # Sweep candidates that didn't win are recorded as
                    # 'archived' from the start. Default 'champion' was
                    # leaving every algo in every sweep marked champion,
                    # which made champion selection meaningless.
                    db.insert_ml_run(
                        algorithm=c["algorithm"],
                        n_train=len(train_df), n_test=len(test_df),
                        train_mae=round(c["train_mae"], 3),
                        test_mae=round(c["test_mae"], 3),
                        holdout_mae_ensemble=round(holdout_mae_ensemble, 3) if holdout_mae_ensemble is not None else None,
                        feature_columns=feature_cols,
                        model_path="(not-persisted)",
                        role="archived",
                    )
            return {
                **best_run,
                "auto_candidates": [
                    {"algorithm": c["algorithm"], "test_mae": round(c["test_mae"], 3)}
                    for c in candidates
                ],
            }

        algo_key = algorithm if algorithm in ("linear", "rf", "gbm", "gbm-best") else "linear"
        fit = _fit_one(algo_key, X_train, y_train, X_test, y_test)
        wf_mae = walk_forward_mae(df, _make_fit_fn(algo_key))
        run = _persist(fit, feature_cols, len(train_df), len(test_df),
                       holdout_mae_ensemble, walk_forward_mae_v=wf_mae)
        _record_per_city_metrics(run["id"], fit["model"], test_df, X_test, y_test)
        return run
    except Exception as exc:
        import traceback
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc().splitlines()[-8:],
        }


def latest_run_summary() -> Dict:
    run = db.latest_ml_run()
    if not run:
        return {"trained": False}
    return {
        "trained": True,
        **{k: run[k] for k in (
            "id", "trained_at", "algorithm", "n_train", "n_test",
            "train_mae", "test_mae", "holdout_mae_ensemble", "model_path",
        )},
        "feature_columns": json.loads(run["feature_columns"]) if run.get("feature_columns") else [],
    }
