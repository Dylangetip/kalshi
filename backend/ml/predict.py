"""Live inference. Loads the latest trained model from ml_runs and
produces ml_max from a feature dict.

Hot-reloads when the underlying file mtime changes — so a new
training run takes effect without a server restart."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import db


_loaded: Optional[Dict[str, Any]] = None
_loaded_path: Optional[str] = None
_loaded_mtime: float = 0.0


def _load_if_changed() -> Optional[Dict[str, Any]]:
    """Lazy + hot-reload. Pulls latest_ml_run() and reloads the joblib
    pickle when path or mtime changes."""
    global _loaded, _loaded_path, _loaded_mtime
    run = db.latest_ml_run()
    if not run:
        return None
    path = run["model_path"]
    if not path or not Path(path).exists():
        return None
    mtime = os.path.getmtime(path)
    if path != _loaded_path or mtime != _loaded_mtime:
        try:
            import joblib  # type: ignore
            payload = joblib.load(path)
            _loaded = payload
            _loaded_path = path
            _loaded_mtime = mtime
        except Exception:
            return None
    return _loaded


def predict_max(features: Dict[str, Any], city: str) -> Optional[float]:
    """Run the latest trained model on `features`. Returns ml_max in °F
    or None if no model is trained yet / inference fails. Handles three
    payload shapes:
      - flat (one model + one feature_columns list): single algo or
        gbm-best winner
      - per-city: dict of city → fit + feature_columns_per_city
      - stack: dict with base + meta + base_order"""
    payload = _load_if_changed()
    if not payload:
        return None

    algo = payload.get("algorithm") or ""

    # Per-city payload
    if algo == "per-city" or "fits_by_city" in payload:
        fits = payload.get("fits_by_city") or {}
        cols_per_city = payload.get("feature_columns_per_city") or {}
        fit = fits.get(city)
        if not fit:
            return None
        return _predict_with(fit["model"], cols_per_city.get(city, []), features, city)

    model = payload.get("model")
    fitted_cols: List[str] = payload.get("feature_columns") or []
    if model is None or not fitted_cols:
        return None

    # Stacking payload — model is a dict {base, meta, base_order}
    if isinstance(model, dict) and "base" in model and "meta" in model:
        try:
            import pandas as pd  # type: ignore
            base_models = model["base"]
            meta = model["meta"]
            base_order = model.get("base_order") or list(base_models.keys())
            base_preds = {}
            for name in base_order:
                v = _predict_with(base_models[name], fitted_cols, features, city)
                if v is None:
                    return None
                base_preds[name] = [v]
            meta_X = pd.DataFrame(base_preds, columns=base_order)
            return float(round(meta.predict(meta_X)[0], 1))
        except Exception:
            return None

    return _predict_with(model, fitted_cols, features, city)


def _predict_with(model, fitted_cols: List[str], features: Dict[str, Any], city: str) -> Optional[float]:
    try:
        import pandas as pd  # type: ignore
        row = {col: 0 for col in fitted_cols}
        for col, val in features.items():
            if col in row and val is not None:
                row[col] = float(val)
        city_col = f"city_{city}"
        if city_col in row:
            row[city_col] = 1
        df = pd.DataFrame([row], columns=fitted_cols)
        pred = model.predict(df)
        return float(round(pred[0], 1))
    except Exception:
        return None


def conformal_interval(features: Dict[str, Any], city: str) -> Optional[Dict[str, float]]:
    """Returns {center, halfwidth, low, high} where the interval has
    guaranteed (1-α) coverage. None if the model wasn't trained with a
    conformal step (older model versions)."""
    payload = _load_if_changed()
    if not payload:
        return None
    q = payload.get("conformal_q90")
    if q is None:
        return None
    pt = predict_max(features, city)
    if pt is None:
        return None
    return {
        "center": pt,
        "halfwidth": float(q),
        "low": round(pt - q, 1),
        "high": round(pt + q, 1),
    }


def predict_quantiles(features: Dict[str, Any], city: str) -> Optional[Dict[str, float]]:
    """If the active model bundle includes a quantile_models trio, run
    each one on the input features and return {p10, p50, p90}. None when
    no quantile models are persisted yet (older model versions) or
    inference fails."""
    payload = _load_if_changed()
    if not payload:
        return None
    quantiles = payload.get("quantile_models") or {}
    if not quantiles:
        return None
    cols = payload.get("feature_columns") or []
    if not cols:
        return None
    out = {}
    for key, model in quantiles.items():
        v = _predict_with(model, cols, features, city)
        if v is None:
            return None
        out[key] = v
    return out


def info() -> Dict:
    payload = _load_if_changed()
    return {
        "loaded": payload is not None,
        "path": _loaded_path,
        "algorithm": (payload or {}).get("algorithm"),
        "has_quantiles": bool((payload or {}).get("quantile_models")),
    }
