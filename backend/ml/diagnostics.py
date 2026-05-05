"""Statistical diagnostics for the ML model — correlations, feature
importance, residual analysis, MAE trend over time. Surfaces what the
model is actually learning so the user (and future model iterations)
can see where the signal lives and where the model still misses.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .. import db
from . import predict as ml_predict
from .train import FEATURE_COLUMNS_NUMERIC, TARGET_COLUMN, _build_dataframe


def feature_importances() -> Optional[List[Dict]]:
    """Pull feature_importances_ off the active GBR/RF model. Linear
    models get their absolute coefficients instead. Returns None for an
    untrained model or types we can't introspect (e.g. stack/per-city)."""
    info = ml_predict.info()
    if not info or not info.get("model_path"):
        return None
    try:
        import joblib  # type: ignore
        payload = joblib.load(info["model_path"])
    except Exception:  # noqa: BLE001
        return None
    model = payload.get("model")
    cols = payload.get("feature_columns") or []

    importances = None
    if hasattr(model, "feature_importances_"):
        importances = list(model.feature_importances_)
    elif hasattr(model, "coef_"):
        try:
            importances = [abs(float(c)) for c in model.coef_]
        except Exception:  # noqa: BLE001
            importances = None
    elif hasattr(model, "named_steps"):
        # Pipeline (Ridge wrapped in StandardScaler) — pull from the last step
        for step in reversed(list(model.named_steps.values())):
            if hasattr(step, "coef_"):
                importances = [abs(float(c)) for c in step.coef_]
                break

    if importances is None or not cols or len(cols) != len(importances):
        return None
    total = sum(importances) or 1.0
    rows = sorted(
        [{"feature": cols[i], "importance": round(importances[i] / total, 4)}
         for i in range(len(cols))],
        key=lambda r: -r["importance"],
    )
    return rows


def correlations_with_target(min_n: int = 60) -> Optional[List[Dict]]:
    """Pearson correlation between each numeric feature and the target.
    Tells the user which raw signals carry the most info before the
    model even fits — useful for sanity-checking that MOS / ensemble
    are actually predictive on the data we have."""
    df = _build_dataframe()
    if df is None or len(df) < min_n:
        return None
    out = []
    target = df[TARGET_COLUMN]
    for col in FEATURE_COLUMNS_NUMERIC:
        if col not in df.columns:
            continue
        s = df[col].astype("float64")
        # Drop rows where either side is NaN before computing corr
        mask = s.notna() & target.notna()
        if mask.sum() < min_n:
            continue
        r = float(s[mask].corr(target[mask]))
        out.append({"feature": col, "r": round(r, 4), "n": int(mask.sum())})
    out.sort(key=lambda x: -abs(x["r"]))
    return out


def recent_residuals(limit: int = 200) -> List[Dict]:
    """Latest predicted vs actual rows (descending by date) so the UI
    can plot a residual scatter. We pull from the joined training data
    using the model's predictions on those rows where available."""
    df = _build_dataframe()
    if df is None or df.empty:
        return []
    df = df.sort_values("target_date", ascending=False).head(limit)
    out = []
    for _, row in df.iterrows():
        pred = row.get("ensemble_max")  # baseline prediction
        actual = row.get(TARGET_COLUMN)
        if pred is None or actual is None:
            continue
        out.append({
            "target_date": str(row.get("target_date")),
            "city": str(row.get("city")),
            "predicted": round(float(pred), 2),
            "actual": round(float(actual), 2),
            "error": round(float(pred) - float(actual), 2),
        })
    return out


def mae_by_week(weeks: int = 26) -> List[Dict]:
    """Rolling MAE per ISO-week. Lets the user see whether the model is
    getting better or worse over time, and catch drift / data quality
    regressions early."""
    df = _build_dataframe()
    if df is None or df.empty:
        return []
    import pandas as pd  # type: ignore
    df = df.copy()
    df["target_date"] = pd.to_datetime(df["target_date"])
    df["week"] = df["target_date"].dt.strftime("%G-W%V")
    df["abs_err"] = (df["ensemble_max"] - df[TARGET_COLUMN]).abs()
    grp = df.dropna(subset=["abs_err"]).groupby("week")
    rows = grp["abs_err"].agg(["mean", "count"]).reset_index()
    rows.columns = ["week", "mae", "n"]
    rows = rows.sort_values("week").tail(weeks)
    return [
        {"week": str(r.week), "mae": round(float(r.mae), 3), "n": int(r.n)}
        for r in rows.itertuples()
    ]


def per_city_mae() -> List[Dict]:
    """Per-city accuracy on the training set. Spotlights cities the
    ensemble systematically over- or under-predicts."""
    df = _build_dataframe()
    if df is None or df.empty:
        return []
    df = df.copy()
    df["abs_err"] = (df["ensemble_max"] - df[TARGET_COLUMN]).abs()
    df["bias"] = df["ensemble_max"] - df[TARGET_COLUMN]
    grp = df.dropna(subset=["abs_err"]).groupby("city")
    rows = grp.agg(mae=("abs_err", "mean"), bias=("bias", "mean"), n=("abs_err", "count")).reset_index()
    rows = rows.sort_values("mae")
    return [
        {"city": r.city, "mae": round(float(r.mae), 3),
         "bias": round(float(r.bias), 3), "n": int(r.n)}
        for r in rows.itertuples()
    ]


def diagnostics_summary() -> Dict:
    """Bundle everything the /api/ml/diagnostics endpoint returns."""
    return {
        "feature_importances": feature_importances(),
        "correlations": correlations_with_target(),
        "recent_residuals": recent_residuals(limit=200),
        "mae_by_week": mae_by_week(weeks=26),
        "per_city_mae": per_city_mae(),
    }
