# Running ML Sentry on your own regression model

This guide takes you from "ML Sentry is running" to "my model is governed". If your
model lives in Google Colab, read this first and then [colab.md](colab.md).

## 0. What ML Sentry needs from you

ML Sentry never imports or runs your model. It needs **evaluation evidence**:

| You provide | Example |
|---|---|
| Model identity | `model_name=churn_value_regressor`, `model_version=7` |
| A **fixed, versioned holdout** and how you evaluate on it | `dataset_id=sales-holdout`, `dataset_version=2026-09-30`, `evaluation_protocol_id=holdout-v1`, `target_name=revenue` |
| Metrics on that holdout | `rmse`, `mae`, `mse`, `r2` (and optionally `mape`, `smape`) |
| Sample count | `sample_count=12000` |

Evaluations are only compared when model, target, dataset id + version and protocol
are **identical**. Keep the holdout immutable. When it must change, give it a new
version and register a new baseline.

## 1. Choose how evaluations reach ML Sentry

| Your setup | How | Guide |
|---|---|---|
| Any script, scheduler or notebook | POST JSON events (stdlib client: `examples/colab/ml_sentry_client.py`) | [generic-api-integration.md](generic-api-integration.md) |
| You log to MLflow | Tag the evaluation run, then call `POST /api/v1/evaluations/mlflow {run_id}` or `ml-sentry mlflow-import RUN_ID` | [mlflow-integration.md](mlflow-integration.md) |
| Google Colab | Same client, over an HTTPS tunnel | [colab.md](colab.md) |

## 2. Write the policy for your model

```bash
cp configs/my-model.example.yaml configs/governance.yaml
```

1. **Rename the model.** Change `colab_regressor` to your `model_name`.
2. **Set thresholds** in your target's units. Measure your current champion first,
   then set:
   - `maximum_acceptable` / `minimum_acceptable`: hard limits that you will never accept.
   - `warning_relative_increase` / `critical_relative_increase`: how much worse than
     the baseline is worth a warning or a page.
   - `primary_metric`: the metric that decides whether a candidate is better.
3. **Retraining.** Keep `runner: external` and set `config` to the only
   hyperparameters a job may use. Set `cooldown_hours` (for example 24).
4. **Release target.**
   - `mock` records the champion inside ML Sentry only.
   - `mlflow_alias` moves an MLflow Model Registry alias that your serving reads. This
     needs `ML_SENTRY_DEPLOYMENT_ENABLED=true`; see [deployment.md](deployment.md).
5. **Validate and activate:**

```bash
docker compose exec api ml-sentry validate-policy /app/configs/governance.yaml
```

Then set `ML_SENTRY_POLICY_PATH=/app/configs/governance.yaml` in `.env` and run
`docker compose up -d`.

If you only want monitoring, set `retraining.allowed: false` and `deployment.allowed:
false`. ML Sentry then validates, detects, notifies and audits, and never proposes
remediation.

## 3. Create the tokens

| Token | Who holds it | Can |
|---|---|---|
| `ML_SENTRY_API_TOKEN` (service) | your evaluation job / notebook | submit evaluations, read incidents |
| reviewer tokens (`ml-sentry add-reviewer NAME`) | each human approver, personally | read and decide approvals in the UI |
| `ML_SENTRY_RUNNER_TOKEN` | your training pipeline / notebook | claim approved jobs, report candidates |

Generate the runner token with `openssl rand -hex 32` and put it in `.env`. External
runners also need `ML_SENTRY_RETRAINING_ENABLED=true`. Restart with
`docker compose up -d` and check with `docker compose exec api ml-sentry doctor`.

## 4. Register your champion's baseline (once per holdout version)

```python
from ml_sentry_client import MLSentryClient, regression_metrics

sentry = MLSentryClient("http://127.0.0.1:8000", SERVICE_TOKEN)
HOLDOUT = dict(dataset_id="sales-holdout", dataset_version="2026-09-30",
               evaluation_protocol_id="holdout-v1", target_name="revenue")
sentry.submit_evaluation(
    event_id="churn-v7-baseline-2026-09-30", role="baseline",
    model_name="churn_value_regressor", model_version="7",
    sample_count=len(y_hold), metrics=regression_metrics(y_hold, model.predict(X_hold)),
    **HOLDOUT)
```

## 5. Send monitoring evaluations

Run your evaluation on a schedule (nightly, per release, after data refreshes) and
send `role="monitoring"` events with a unique `event_id` each time. Each response
contains the decision immediately. If an incident opens, the workflow continues in
the background.

> **Monitoring fresh production data.** Baselines are only comparable on the same
> holdout. If each evaluation uses a new labelled window, set `require_baseline:
> false` for the model and rely on absolute limits. Alternatively, keep a fixed
> holdout for relative checks.

## 6. When an incident needs retraining

1. **Gate A.** Reviewers get an approval request (UI at `/ui/`, plus Slack, Telegram
   or webhook if configured). It shows the evidence and the exact job spec.
2. **Your pipeline claims the job and trains.** `run_jobs()` in the client claims
   the approved job, calls your `train_and_evaluate(spec)` with the approved config
   and the champion's holdout identity, and reports the candidate's metrics on
   that **same** holdout.
3. **ML Sentry gates the candidate.** It must use the same holdout, show no
   regression against the champion, and improve the primary metric.
4. **Gate B.** If the candidate passes, a second approval is requested. After
   approval, ML Sentry releases to the target, verifies it, and rolls back if
   verification fails.

See [approvals.md](approvals.md), [training-adapters.md](training-adapters.md) and
[deployment.md](deployment.md).

## 7. Check that it works

- **Configuration:** `docker compose exec api ml-sentry doctor`.
- **Incidents and approvals:** the UI at `http://127.0.0.1:8000/ui/`.
- **Audit trail:** `GET /api/v1/incidents/{id}/audit`, or `docker compose logs -f worker`.
