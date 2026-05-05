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


def model_diff() -> Optional[Dict]:
    """Compare the two most-recent persisted models so the UI can show
    "what changed" between them: test_mae delta, train_mae delta, and a
    per-feature importance diff. If only one model has been persisted
    we return None — there's nothing to compare yet."""
    runs = db.list_ml_runs(limit=10)
    persisted = [r for r in runs if r.get("model_path") and r.get("model_path") != "(not-persisted)"]
    if len(persisted) < 2:
        return None
    cur, prev = persisted[0], persisted[1]

    def importances_for(run):
        try:
            import joblib  # type: ignore
            payload = joblib.load(run["model_path"])
        except Exception:  # noqa: BLE001
            return None
        m = payload.get("model")
        cols = payload.get("feature_columns") or []
        imp = None
        if hasattr(m, "feature_importances_"):
            imp = list(m.feature_importances_)
        elif hasattr(m, "coef_"):
            try:
                imp = [abs(float(c)) for c in m.coef_]
            except Exception:  # noqa: BLE001
                imp = None
        elif hasattr(m, "named_steps"):
            for step in reversed(list(m.named_steps.values())):
                if hasattr(step, "coef_"):
                    imp = [abs(float(c)) for c in step.coef_]
                    break
        if imp is None or not cols or len(imp) != len(cols):
            return None
        total = sum(imp) or 1.0
        return {cols[i]: imp[i] / total for i in range(len(cols))}

    cur_imp = importances_for(cur) or {}
    prev_imp = importances_for(prev) or {}
    all_cols = sorted(set(cur_imp) | set(prev_imp))
    importance_delta = []
    for col in all_cols:
        before = prev_imp.get(col, 0.0)
        after = cur_imp.get(col, 0.0)
        delta = after - before
        if abs(delta) >= 0.005:  # 0.5pp threshold so we don't drown in noise
            importance_delta.append({
                "feature": col,
                "before": round(before, 4),
                "after": round(after, 4),
                "delta": round(delta, 4),
            })
    importance_delta.sort(key=lambda r: -abs(r["delta"]))

    def safe_delta(a, b):
        if a is None or b is None:
            return None
        return round(float(a) - float(b), 4)

    return {
        "previous": {
            "id": prev.get("id"),
            "trained_at": prev.get("trained_at"),
            "algorithm": prev.get("algorithm"),
            "test_mae": prev.get("test_mae"),
            "train_mae": prev.get("train_mae"),
            "n_train": prev.get("n_train"),
            "n_test": prev.get("n_test"),
        },
        "current": {
            "id": cur.get("id"),
            "trained_at": cur.get("trained_at"),
            "algorithm": cur.get("algorithm"),
            "test_mae": cur.get("test_mae"),
            "train_mae": cur.get("train_mae"),
            "n_train": cur.get("n_train"),
            "n_test": cur.get("n_test"),
        },
        "test_mae_delta": safe_delta(cur.get("test_mae"), prev.get("test_mae")),
        "train_mae_delta": safe_delta(cur.get("train_mae"), prev.get("train_mae")),
        "importance_delta": importance_delta[:25],
    }


def drift_check() -> Dict:
    """Compare last 7 days of paired actuals vs the prior 28 days. If
    last_week's MAE is significantly worse, the model is drifting and
    we should alert. Returns the comparison numbers + a `drifting` flag.
    Designed to be called right after each retrain so the activity feed
    can log a `drift_alert` event."""
    df = _build_dataframe()
    if df is None or df.empty:
        return {"available": False}
    import pandas as pd  # type: ignore
    df = df.copy()
    df["target_date"] = pd.to_datetime(df["target_date"])
    today = pd.Timestamp.utcnow().normalize()
    last_week = df[df["target_date"] > today - pd.Timedelta(days=7)]
    prior_4w = df[(df["target_date"] <= today - pd.Timedelta(days=7)) &
                  (df["target_date"] > today - pd.Timedelta(days=35))]
    if len(last_week) < 5 or len(prior_4w) < 20:
        return {"available": False, "reason": "not enough data in either window"}

    def mae(window):
        err = (window["ensemble_max"] - window[TARGET_COLUMN]).abs().dropna()
        return float(err.mean()) if len(err) else None

    last_mae = mae(last_week)
    prior_mae = mae(prior_4w)
    if last_mae is None or prior_mae is None or prior_mae == 0:
        return {"available": False}
    ratio = last_mae / prior_mae
    drifting = ratio > 1.3
    return {
        "available": True,
        "drifting": drifting,
        "last_week_mae": round(last_mae, 3),
        "trailing_4w_mae": round(prior_mae, 3),
        "ratio": round(ratio, 3),
        "last_week_n": int(len(last_week)),
        "prior_4w_n": int(len(prior_4w)),
    }


def diagnostics_summary() -> Dict:
    """Bundle everything the /api/ml/diagnostics endpoint returns."""
    return {
        "feature_importances": feature_importances(),
        "correlations": correlations_with_target(),
        "recent_residuals": recent_residuals(limit=200),
        "mae_by_week": mae_by_week(weeks=26),
        "per_city_mae": per_city_mae(),
    }
