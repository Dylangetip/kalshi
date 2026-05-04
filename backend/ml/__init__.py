"""Machine-learning pipeline for the Bets weather predictor.

Three modules:
  - backfill: pull historical (predictions, actuals) from Open-Meteo
              Historical Forecast + IEM ASOS hourly archive into the
              historical_predictions / historical_actuals tables
  - train:    fit sklearn regressors against the joined dataset, save
              the pickle to ml/models/{ts}.pkl, record metrics in
              ml_runs
  - predict:  load the latest model and produce ml_max from a feature
              dict at live inference time
"""
