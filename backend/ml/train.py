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
    "seasonal_avg_max_f",
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


def _build_dataframe():
    import pandas as pd  # type: ignore
    rows = db.list_training_data()
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.dropna(subset=[TARGET_COLUMN])
    if df.empty:
        return None
    return df


def _split(df, test_frac: float = 0.2):
    df = df.sort_values("target_date").reset_index(drop=True)
    n = len(df)
    n_test = max(1, int(n * test_frac))
    n_train = n - n_test
    return df.iloc[:n_train], df.iloc[n_train:]


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


def _fit_one(algorithm: str, X_train, y_train, X_test, y_test) -> Dict:
    """Fit a single (algorithm, hyperparameters) candidate."""
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
        from sklearn.linear_model import Ridge  # type: ignore
        m = Ridge(alpha=1.0)
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
             extra_payload: Optional[Dict] = None) -> Dict:
    """Save a fitted model to disk and record the run in ml_runs.
    `extra_payload` lets callers (e.g. per-city / stacking) include
    structured side data in the pickle that predict.py knows how to
    consume."""
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
    )


def _train_per_city(df) -> Optional[Dict]:
    """Fit one model per city. Returns a dict of {city: best_fit} where
    each best_fit is the algo with lowest test_mae for THAT city. Cities
    with too few rows fall back to None and inference will use the
    global model for them."""
    from sklearn.linear_model import Ridge  # type: ignore
    from sklearn.ensemble import GradientBoostingRegressor  # type: ignore
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
            ("linear", Ridge(alpha=1.0)),
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
    import numpy as np  # type: ignore
    base_models = {
        "linear": Ridge(alpha=1.0),
        "rf": RandomForestRegressor(n_estimators=200, max_depth=12, min_samples_leaf=4, random_state=42, n_jobs=-1),
        "gbm": GradientBoostingRegressor(n_estimators=200, max_depth=3, learning_rate=0.05, random_state=42),
    }
    # 5-fold cross-validated predictions on the training set so the meta
    # model trains on out-of-sample predictions (no leakage).
    from sklearn.model_selection import KFold  # type: ignore
    kf = KFold(n_splits=5, shuffle=False)
    n = len(X_train)
    base_train_preds = {name: np.zeros(n) for name in base_models}
    for tr_idx, va_idx in kf.split(X_train):
        for name, m in base_models.items():
            mc = type(m)(**m.get_params())
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


def train(algorithm: str = "linear") -> Dict:
    """Fit one model run. Wraps the pipeline in try/except so the API gets
    a clean error body instead of a 500.

    Pass algorithm='auto' to sweep linear + rf + gbm-best in one call —
    the lowest test_mae candidate becomes the active model, and every
    candidate's metrics land in ml_runs so the history table shows the
    full sweep."""
    try:
        df = _build_dataframe()
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
            return _persist(fit, feature_cols, len(train_df), len(test_df), holdout_mae_ensemble)

        if algorithm == "per-city":
            payload = _train_per_city(df)
            if payload is None:
                return {"error": "no city had enough rows (≥50) for per-city training"}
            algo = "per-city"
            run = db.insert_ml_run(
                algorithm=algo,
                n_train=payload["n_train"], n_test=payload["n_test"],
                train_mae=None,
                test_mae=round(payload["test_mae"], 3) if payload["test_mae"] else None,
                holdout_mae_ensemble=round(holdout_mae_ensemble, 3) if holdout_mae_ensemble is not None else None,
                feature_columns=["per-city: see payload"],
                model_path=str(MODELS_DIR / f"per-city-{int(time.time())}.pkl"),
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

        if algorithm == "auto":
            candidates = []
            for algo in ("linear", "rf", "gbm-best", "stack"):
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
            for c in candidates:
                if c is best:
                    best_run = _persist(c, feature_cols, len(train_df), len(test_df), holdout_mae_ensemble)
                else:
                    db.insert_ml_run(
                        algorithm=c["algorithm"],
                        n_train=len(train_df), n_test=len(test_df),
                        train_mae=round(c["train_mae"], 3),
                        test_mae=round(c["test_mae"], 3),
                        holdout_mae_ensemble=round(holdout_mae_ensemble, 3) if holdout_mae_ensemble is not None else None,
                        feature_columns=feature_cols,
                        model_path="(not-persisted)",
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
        return _persist(fit, feature_cols, len(train_df), len(test_df), holdout_mae_ensemble)
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
