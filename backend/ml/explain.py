"""Per-prediction SHAP explanations for the active ML model.

For every city we can answer "we predict NYC=78.3°F because GFS-MOS
contributed +1.2°, prev_actual contributed +0.8°, …". Surfaces in the
city drill-down on the dashboard.

Graceful fallback: if `shap` isn't installed, returns None / empty so
the UI just hides the section.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import predict as ml_predict


_explainer_cache: Dict[str, Any] = {"path": None, "explainer": None}


def _build_explainer(model, X_background):
    try:
        import shap  # type: ignore
    except Exception:
        return None
    try:
        # TreeExplainer for tree-based models (much faster); fallback to KernelExplainer.
        if hasattr(model, "feature_importances_"):
            return shap.TreeExplainer(model)
        # For Pipelines (Ridge wrapped in StandardScaler), use LinearExplainer
        # on the underlying linear step where possible.
        return shap.Explainer(model, X_background)
    except Exception:
        return None


def explain_one(features: Dict[str, Any], city: str, top_k: int = 5) -> Optional[List[Dict]]:
    """Return the top-k features by absolute SHAP value for this single
    prediction, sorted descending. Each row: {feature, value, contribution}.
    """
    payload = ml_predict._load_if_changed()
    if not payload:
        return None
    model = payload.get("model")
    cols = payload.get("feature_columns") or []
    if model is None or not cols:
        return None

    # Cache one explainer per model_path
    path = getattr(ml_predict, "_loaded_path", None)
    if _explainer_cache["path"] != path:
        try:
            import pandas as pd  # type: ignore
            # Use a tiny zero-row as background — fine for tree models, OK
            # approximation for linear. Real shap needs a sampled bg set.
            background = pd.DataFrame([{c: 0.0 for c in cols}], columns=cols)
            _explainer_cache["explainer"] = _build_explainer(model, background)
            _explainer_cache["path"] = path
        except Exception:
            return None

    explainer = _explainer_cache["explainer"]
    if explainer is None:
        return None

    try:
        import pandas as pd  # type: ignore
        row = {col: 0.0 for col in cols}
        for col, val in (features or {}).items():
            if col in row and val is not None:
                row[col] = float(val)
        city_col = f"city_{city}"
        if city_col in row:
            row[city_col] = 1.0
        df = pd.DataFrame([row], columns=cols)
        sv = explainer(df)
        # sv.values shape (1, n_features) for regressors; .values[0] gives the row
        try:
            vals = list(sv.values[0])
        except Exception:
            vals = list(sv[0].values) if hasattr(sv[0], "values") else None
        if vals is None or len(vals) != len(cols):
            return None
        rows = [
            {"feature": cols[i], "value": row[cols[i]],
             "contribution": round(float(vals[i]), 3)}
            for i in range(len(cols))
        ]
        rows.sort(key=lambda r: -abs(r["contribution"]))
        return rows[:top_k]
    except Exception:
        return None
