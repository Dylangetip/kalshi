"""Incremental / online learning via the River library.

The batch-trained model retrains daily, so it lags any sudden regime
shift (heatwave start, frontal passage) by ~24 hours. The online model
maintained here learns one sample at a time as new historical_actuals
are paired with predictions — picking up regime shifts within hours.

The active prediction is α·online + (1−α)·batch. α is a runtime config
on _model_state ("online_alpha", default 0.0 = batch only) so the user
can opt in once they're happy with the online side.

Graceful fallback: if `river` isn't installed, every function returns
None / 0 / False. The backend still boots; online learning is just
skipped until `pip install river` runs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

ONLINE_PATH = Path(__file__).parent / "models" / "online.json"

_state: Dict[str, Any] = {
    "model": None,           # river LinearRegression instance
    "n_updates": 0,
    "last_update_ts": 0,
}


def _import_river():
    try:
        from river import linear_model, optim, preprocessing  # type: ignore
        return linear_model, optim, preprocessing
    except Exception:
        return None


def _ensure_model():
    """Lazy-load the online model. Falls back to a fresh model when no
    saved state exists yet, or when River isn't installed (returns None)."""
    if _state["model"] is not None:
        return _state["model"]
    river = _import_river()
    if river is None:
        return None
    linear_model, optim, preprocessing = river
    pipeline = preprocessing.StandardScaler() | linear_model.LinearRegression(
        optimizer=optim.SGD(0.01)
    )
    # Try to load saved state.
    if ONLINE_PATH.exists():
        try:
            import joblib  # type: ignore
            saved = joblib.load(str(ONLINE_PATH).replace(".json", ".pkl"))
            _state["model"] = saved.get("model") or pipeline
            _state["n_updates"] = int(saved.get("n_updates") or 0)
            _state["last_update_ts"] = int(saved.get("last_update_ts") or 0)
            return _state["model"]
        except Exception:
            pass
    _state["model"] = pipeline
    return pipeline


def _save_state():
    try:
        import joblib  # type: ignore
        ONLINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({
            "model": _state["model"],
            "n_updates": _state["n_updates"],
            "last_update_ts": _state["last_update_ts"],
        }, str(ONLINE_PATH).replace(".json", ".pkl"))
    except Exception:
        pass


def learn_one(features: Dict[str, float], target: float) -> bool:
    """Update the online model with a single (features, target) pair.
    Returns True on success, False if River isn't installed or fails."""
    m = _ensure_model()
    if m is None:
        return False
    # River expects strict float dict; drop None / non-numeric.
    clean = {k: float(v) for k, v in features.items()
             if v is not None and isinstance(v, (int, float))}
    if not clean:
        return False
    try:
        import time
        m.learn_one(clean, float(target))
        _state["n_updates"] += 1
        _state["last_update_ts"] = int(time.time())
        # Save every 10 updates to keep disk in sync without thrashing.
        if _state["n_updates"] % 10 == 0:
            _save_state()
        return True
    except Exception:
        return False


def predict(features: Dict[str, float]) -> Optional[float]:
    """Online-only prediction. Returns None until the model has seen at
    least 50 updates (cold-start guard — random init is worse than
    falling back to batch)."""
    m = _ensure_model()
    if m is None or _state["n_updates"] < 50:
        return None
    clean = {k: float(v) for k, v in features.items()
             if v is not None and isinstance(v, (int, float))}
    if not clean:
        return None
    try:
        return float(m.predict_one(clean))
    except Exception:
        return None


def info() -> Dict:
    return {
        "available": _import_river() is not None,
        "n_updates": _state["n_updates"],
        "last_update_ts": _state["last_update_ts"],
        "ready": _state["n_updates"] >= 50,
    }
