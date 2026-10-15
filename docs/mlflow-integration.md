# MLflow integration

ML Sentry talks to MLflow through its REST API, so no `mlflow` package is needed in
the image. Tested against MLflow **3.10.0** (`ghcr.io/mlflow/mlflow:v3.10.0`).

## Configure

| Variable | Purpose |
|---|---|
| `ML_SENTRY_MLFLOW_TRACKING_URI` | e.g. `https://mlflow.example.com`, or `http://mlflow:5000` for the bundled one |
| `ML_SENTRY_MLFLOW_ALLOWED_EXPERIMENTS` | Comma-separated experiment **names or IDs** ML Sentry may read. Empty means MLflow ingestion is refused |
| `ML_SENTRY_MLFLOW_TRACKING_TOKEN` | Bearer token, if your server uses one |
| `ML_SENTRY_MLFLOW_USERNAME` / `ML_SENTRY_MLFLOW_PASSWORD` | Basic auth (MLflow's built-in auth) |

**Local MLflow.** Start it with `docker compose --profile mlflow up -d`. It listens
on `127.0.0.1:5000`, with a SQLite backend and served artifacts in the `mlflowdata`
volume. From containers its address is `http://mlflow:5000`; from your machine it's
`http://127.0.0.1:5000`.

**Artifact URIs.** ML Sentry never downloads artifacts. It stores run IDs and model
references only, so your artifact store doesn't have to be reachable from ML Sentry.

`ml-sentry doctor` checks reachability and lists the allowed experiments.

## Tag your evaluation run

Log the evaluation of the model on the fixed holdout as a run with these tags (or
params):

| Tag | Example |
|---|---|
| `ml_sentry.model_name` | `house_price_regressor` |
| `ml_sentry.model_version` | `7` |
| `ml_sentry.target_name` | `sale_price` |
| `ml_sentry.dataset_id` / `ml_sentry.dataset_version` | `housing-holdout` / `2026-09-30` |
| `ml_sentry.evaluation_protocol_id` | `holdout-v1` |
| `ml_sentry.role` *(optional)* | `baseline` or `monitoring` (default) |
| `ml_sentry.sample_count` *(optional)* | otherwise the `example_count` metric is used |
| `ml_sentry.metric_prefix` *(optional)* | e.g. `holdout_` if metrics are named `holdout_rmse` |

**Metric names.** ML Sentry recognises the short names `rmse`, `mae`, `mse`, `r2`,
`mape` and `smape`. It also recognises `mlflow.evaluate()`'s names:
`root_mean_squared_error`, `mean_absolute_error`, `mean_squared_error`, `r2_score`
and `mean_absolute_percentage_error`.

```python
import mlflow
with mlflow.start_run(experiment_id=EXP) as run:
    mlflow.set_tags({"ml_sentry.model_name": "house_price_regressor", "ml_sentry.model_version": "7",
                     "ml_sentry.target_name": "sale_price", "ml_sentry.dataset_id": "housing-holdout",
                     "ml_sentry.dataset_version": "2026-09-30", "ml_sentry.evaluation_protocol_id": "holdout-v1"})
    mlflow.log_metrics({"rmse": rmse, "mae": mae, "mse": mse, "r2": r2, "example_count": len(y_hold)})
```

## Send the run to ML Sentry

The run must be `FINISHED` and in an allow-listed experiment. You can send it three
ways:

**API (service token):**
```bash
curl -X POST -H "Authorization: Bearer $ML_SENTRY_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"run_id":"<run id>"}' http://127.0.0.1:8000/api/v1/evaluations/mlflow
```

**CLI:**
```bash
docker compose exec api ml-sentry mlflow-import <run id>
```

**Python client:** `sentry.submit_mlflow_run(run_id)`.

The event ID is `mlflow-<run_id>`, so re-sending is idempotent. An MLflow run and an
equivalent generic event lead to the same decision. That equivalence is tested.

**Errors.** Failures return stable error codes:

| Code | Meaning |
|---|---|
| `MLFLOW_EXPERIMENT_NOT_ALLOWED` | The run's experiment is not in the allow-list |
| `MLFLOW_RUN_NOT_FINISHED` | The run hasn't finished |
| `MLFLOW_RUN_MISSING_TAGS` | One or more required tags are missing |
| `MLFLOW_RUN_NO_METRICS` | No recognised regression metrics |
| `MLFLOW_UNAUTHORIZED` | The credentials were rejected |
| `MLFLOW_UNREACHABLE` | Check `ML_SENTRY_MLFLOW_TRACKING_URI` |

## Candidates from MLflow

An external runner can report a finished candidate run instead of raw metrics:
`{"status":"succeeded", "candidate_model_ref":"models:/house/8",
"candidate_model_version":"8", "mlflow_run_id":"<run>"}`. ML Sentry reads the
holdout metrics from that run, using the same tags as above.

## Releasing with the Model Registry

See [deployment.md](deployment.md). The `mlflow_alias` target moves an alias such as
`champion` to the approved registered model version, verifies it, and restores the
previous alias if verification fails.
