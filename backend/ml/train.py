"""Train a regressor on the joined historical_predictions × historical_actuals
dataset. The trained model maps a feature vector to predicted daily max F.

Models supported:
  - 'linear':  sklearn.linear_model.LinearRegression — interpretable baseline
  - 'gbm':     sklearn.ensemble.GradientBoostingRegressor — usually beats
               linear once we have a few hundred samples

Holdout protocol: chronological 80/20 split on target_date (no leakage of
future into training). We compute the holdout MAE for the trained model AND
for the naive ensemble_max baseline, so the comparison is on the SAME rows.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import db


# Numeric features fed into the model. Strings (city code) get one-hot encoded.
FEATURE_COLUMNS_NUMERIC: List[str] = [
    "gfs_max", "ecmwf_max", "icon_max", "om_max",
    "t850_c", "t700_c", "t500_c", "h500_m", "rh850_pct",
    "ensemble_max",
]
FEATURE_COLUMNS_CATEGORICAL: List[str] = ["city"]
TARGET_COLUMN = "actual_max_f"

MODELS_DIR = Path(__file__).parent / "models"


def _build_dataframe():
    """Lazy-import pandas so import-time failures (missing dep) don't
    break the rest of the backend."""
    import pandas as pd  # type: ignore
    rows = db.list_training_data()
    if not rows:
        return None
    df = pd.DataFrame(rows)
    # Drop rows where the target or all features are null
    df = df.dropna(subset=[TARGET_COLUMN])
    if df.empty:
        return None
    return df


def _split(df, test_frac: float = 0.2):
    """Chronological split. Older rows train, newer rows test."""
    df = df.sort_values("target_date").reset_index(drop=True)
    n = len(df)
    n_test = max(1, int(n * test_frac))
    n_train = n - n_test
    return df.iloc[:n_train], df.iloc[n_train:]


def _featurize(df, fitted_columns: Optional[List[str]] = None):
    """Numeric impute + city one-hot. Returns (X, used_columns).

    When `fitted_columns` is provided (inference path), align the
    one-hot columns to match. Otherwise produce the canonical training
    column order."""
    import pandas as pd  # type: ignore
    base = df[FEATURE_COLUMNS_NUMERIC].copy()
    # Mean-impute numeric NaNs with column mean, fall back to 0
    means = base.mean(numeric_only=True).fillna(0)
    base = base.fillna(means)

    cat = pd.get_dummies(df[FEATURE_COLUMNS_CATEGORICAL], prefix=FEATURE_COLUMNS_CATEGORICAL)
    X = pd.concat([base, cat], axis=1)

    if fitted_columns is not None:
        # Add missing cols (e.g. a city not seen at training time gets a 0 col)
        for col in fitted_columns:
            if col not in X.columns:
                X[col] = 0
        # Drop extra cols not in training
        X = X[fitted_columns]

    return X, list(X.columns)


def train(algorithm: str = "linear") -> Dict:
    """Fit one model run. Persists model to ml/models/{ts}.pkl and
    inserts a row in ml_runs. Returns the run dict (or an error dict)."""
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

    if algorithm == "gbm":
        from sklearn.ensemble import GradientBoostingRegressor  # type: ignore
        model = GradientBoostingRegressor(n_estimators=200, max_depth=3, random_state=42)
    else:
        from sklearn.linear_model import LinearRegression  # type: ignore
        algorithm = "linear"
        model = LinearRegression()

    model.fit(X_train, y_train)
    train_pred = model.predict(X_train)
    test_pred = model.predict(X_test)
    train_mae = float((train_pred - y_train).abs().mean())
    test_mae = float((test_pred - y_test).abs().mean())

    # Same-rows ensemble MAE for head-to-head comparison
    ens_test = test_df["ensemble_max"].astype(float)
    holdout_mae_ensemble = float((ens_test - y_test).abs().mean())

    # Save
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    model_path = MODELS_DIR / f"{algorithm}-{ts}.pkl"
    import joblib  # type: ignore
    joblib.dump(
        {"model": model, "feature_columns": feature_cols, "algorithm": algorithm},
        model_path,
    )

    run = db.insert_ml_run(
        algorithm=algorithm,
        n_train=len(train_df),
        n_test=len(test_df),
        train_mae=round(train_mae, 3),
        test_mae=round(test_mae, 3),
        holdout_mae_ensemble=round(holdout_mae_ensemble, 3),
        feature_columns=feature_cols,
        model_path=str(model_path),
    )
    return run


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
