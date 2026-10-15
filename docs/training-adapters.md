# Training runners

A training job exists only after **Gate A** is approved. There is exactly one job per
approval, and its spec is the approved spec: pipeline, config, training dataset,
holdout and timeout. Nothing in an event, an API call or an LLM output can change it.

Pipelines are allow-listed in the policy:

```yaml
pipelines:
  my_retrain:
    runner: external          # or: mock
    training_dataset: {dataset_id: sales-train, dataset_version: 2026-09-30}
    config: {max_depth: 8, learning_rate: 0.05}   # the only knobs; ≤ 32 scalar values
    timeout_minutes: 120
```

## `mock`: simulated training (demos and tests)

The worker "trains" by deriving candidate metrics from the champion's holdout
metrics:
- `config.simulated_improvement: 0.12` makes the candidate 12 % better.
- A negative value makes it worse, which exercises candidate rejection.
- `config.simulate_failure: true` makes the job fail.

Nothing is trained or written outside ML Sentry's database, and no environment
switch is needed.

## `external`: your own pipeline does the training

ML Sentry never runs your code. Your pipeline (a Colab notebook, a CI job, an Airflow
task, a script on a GPU box) **pulls** approved jobs with the runner token:

| Step | Call |
|---|---|
| List queued jobs | `GET /api/v1/jobs?runner=external&model_name=X` |
| Claim one | `POST /api/v1/jobs/{id}/claim` (409 if already claimed or past its deadline) |
| Train with `job.spec.config` and evaluate on `job.spec.holdout` | your code |
| Report the result | `POST /api/v1/jobs/{id}/result` |

The result body looks like this:

```json
{"status": "succeeded",
 "candidate_model_ref": "gs://models/churn/8", "candidate_model_version": "8",
 "candidate_run_id": "optional",
 "evaluation": {"dataset_id": "...", "dataset_version": "...", "evaluation_protocol_id": "...",
                "target_name": "...", "sample_count": 12000,
                "metrics": {"rmse": 0.41, "mae": 0.30, "mse": 0.168, "r2": 0.84}}}
```

Instead of `evaluation` you can send `mlflow_run_id`. A failed job reports
`{"status": "failed", "error_code": "OOM"}`.

The Python client does all of this for you:
`MLSentryClient(url, runner_token=...).run_jobs(train_and_evaluate, model_name=...)`.
See [colab.md](colab.md).

**Requirements:**
- `ML_SENTRY_RUNNER_TOKEN` must be set.
- `ML_SENTRY_RETRAINING_ENABLED=true` (the environment kill switch).

**Deadlines.** `timeout_minutes` is enforced by the worker. A late result is stored
as `timed_out`, and the incident ends as `training_failed`.

## Candidate gates (after a successful job)

All of these must pass before Gate B is requested. If any fails, the champion stays
untouched.

| Gate | Rule |
|---|---|
| `evidence_valid` | The candidate's metrics pass the same evidence checks as any event |
| `same_holdout` | Same dataset id, dataset version, protocol and target as the champion's baseline |
| `no_regression_vs_champion` | The policy evaluation against the champion is `none`: no absolute limit or relative threshold breached |
| `primary_metric_improvement` | The primary metric improves by at least `deployment.min_primary_improvement` (default 0, so not worse) |

## Cooldowns and attempts

- `retraining.cooldown_hours` blocks a new retraining request for the same model
  while another incident's job is recent (`COOLDOWN_ACTIVE`).
- `retraining.max_attempts_per_incident` caps jobs per incident (`MAX_ATTEMPTS_REACHED`).

Both end the incident as `retraining_blocked`, with a notification.
