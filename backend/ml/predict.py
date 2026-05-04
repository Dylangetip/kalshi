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
    or None if no model is trained yet / inference fails."""
    payload = _load_if_changed()
    if not payload:
        return None
    model = payload.get("model")
    fitted_cols: List[str] = payload.get("feature_columns") or []
    if model is None or not fitted_cols:
        return None
    try:
        import pandas as pd  # type: ignore
        # Build a one-row DataFrame matching the training feature schema
        row = {col: 0 for col in fitted_cols}
        for col, val in features.items():
            if col in row and val is not None:
                row[col] = float(val)
        # City one-hot
        city_col = f"city_{city}"
        if city_col in row:
            row[city_col] = 1
        df = pd.DataFrame([row], columns=fitted_cols)
        # Mean-impute remaining zeros for numeric features that should have
        # a sensible default (relies on training-time fill behavior; here
        # we just leave 0 since the linear model handles it gracefully).
        pred = model.predict(df)
        return float(round(pred[0], 1))
    except Exception:
        return None


def info() -> Dict:
    payload = _load_if_changed()
    return {
        "loaded": payload is not None,
        "path": _loaded_path,
        "algorithm": (payload or {}).get("algorithm"),
    }
