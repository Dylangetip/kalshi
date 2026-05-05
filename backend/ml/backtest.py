"""Historical replay / backtesting framework.

For every paired (city, target_date) row, evaluates: what would the
current model have predicted, what bracket would it have recommended,
and would that bet have hit? Outputs cumulative P/L, Sharpe, max
drawdown, hit rate — letting the user see the long-run expected value
of the current setup before betting real money.

Simplification: since we don't have historical Kalshi YES prices
backfilled, we use a **fixed-odds proxy** — treat every bet as if
entered at 25¢ (4× payout on a 1°F bracket). Real Kalshi prices
fluctuate but this simulation is faithful to "did we predict the
right bracket and how often."
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .. import db


def _bracket_for(model_max: float, width: float = 1.0) -> tuple:
    """Round model_max to the nearest 1°F bracket center."""
    lo = int(round(model_max - 0.5))
    return (lo, lo + 1)


def run_backtest(stake: float = 500.0, entry_cents: int = 25,
                 max_days: int = 365) -> Optional[Dict]:
    """Replay the last N days through the active model. For each day,
    bet `stake` on the model's predicted ±0.5° bracket at fixed
    `entry_cents`. Win pays (1 - entry_cents/100) × stake / (entry_cents/100).
    Returns cumulative P/L curve + summary stats.
    """
    from . import predict as ml_predict
    rows = db.list_training_data()
    if not rows:
        return {"available": False, "reason": "no training data"}
    rows = sorted(rows, key=lambda r: (r.get("target_date") or "", r.get("city") or ""))[-max_days * 19:]

    history: List[Dict] = []
    cumulative = 0.0
    wins = 0
    losses = 0
    daily_pl: Dict[str, float] = {}

    entry = entry_cents / 100.0
    win_payout = (stake / entry) * (1 - entry) if entry > 0 else 0.0  # net profit on win

    for r in rows:
        target = r.get("actual_max_f")
        ensemble = r.get("ensemble_max")
        if target is None or ensemble is None:
            continue
        # Use the ML model on the row's feature dict; fall back to
        # ensemble_max if no model is loaded.
        feats = {k: v for k, v in r.items()
                 if k not in ("city", "target_date", "actual_max_f")}
        pred = ml_predict.predict_max(feats, city=r.get("city"))
        if pred is None:
            pred = ensemble
        lo, hi = _bracket_for(pred)
        won = (lo <= target <= hi)
        pl = win_payout if won else -stake
        cumulative += pl
        if won:
            wins += 1
        else:
            losses += 1
        date = r.get("target_date")
        daily_pl[date] = daily_pl.get(date, 0.0) + pl
        history.append({
            "date": date,
            "city": r.get("city"),
            "predicted": round(float(pred), 2),
            "actual": round(float(target), 2),
            "won": won,
            "pl": round(pl, 2),
            "cumulative": round(cumulative, 2),
        })

    if not history:
        return {"available": False, "reason": "no rows scored"}

    n = wins + losses
    win_rate = wins / n if n else 0.0
    # Per-day P/L for Sharpe (annualized)
    pls = list(daily_pl.values())
    import statistics as _stats
    mean_pl = _stats.mean(pls) if pls else 0.0
    std_pl = _stats.pstdev(pls) if len(pls) > 1 else 0.0
    sharpe = (mean_pl / std_pl) * (252 ** 0.5) if std_pl > 0 else 0.0
    # Max drawdown from running peak
    peak = 0.0
    max_dd = 0.0
    cum = 0.0
    for p in pls:
        cum += p
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)

    return {
        "available": True,
        "stake": stake,
        "entry_cents": entry_cents,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "total_pl": round(cumulative, 2),
        "sharpe_annualized": round(sharpe, 3),
        "max_drawdown": round(max_dd, 2),
        "n_days": len(daily_pl),
        # Tail-only history to keep response size sane
        "curve": [
            {"date": d, "pl": round(daily_pl[d], 2)}
            for d in sorted(daily_pl.keys())
        ],
    }
