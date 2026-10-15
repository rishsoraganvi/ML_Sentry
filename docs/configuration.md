# Configuration

All settings are environment variables with the `ML_SENTRY_` prefix. Docker Compose
reads them from `.env`, or from the file named by `ML_SENTRY_ENV_FILE`. Invalid
values stop startup with a message naming the variable.

## Core

| Variable | Required | Default | Notes |
|---|---|---|---|
| `ML_SENTRY_API_TOKEN` | yes | – | **Service** token: submit evaluations, read. ≥ 32 random chars; cannot approve |
| `ML_SENTRY_DATABASE_URL` | yes | set by Compose | `postgresql+psycopg://user:pass@host:5432/db` |
| `ML_SENTRY_POLICY_PATH` | yes | Compose: `/app/configs/governance.example.yaml` | See [regression-policies.md](regression-policies.md) |
| `ML_SENTRY_PUBLIC_URL` | no | `http://127.0.0.1:8000` | Base of links in notifications |
| `ML_SENTRY_API_URL` | no | set by Compose | Where `doctor` probes the API |
| `ML_SENTRY_ARTIFACT_DIR` | no | `/var/lib/ml-sentry/artifacts` | Must be writable |
| `ML_SENTRY_MAX_REQUEST_BYTES` | no | `65536` | Larger bodies get `413` |
| `ML_SENTRY_LOG_LEVEL` | no | `INFO` | |
| `ML_SENTRY_WORKER_POLL_SECONDS` | no | `2` | Worker cycle interval |

## People and runners

| Variable | Notes |
|---|---|
| `ML_SENTRY_REVIEWERS` | `name:sha256,name2:sha256`; create entries with `ml-sentry add-reviewer NAME`. See [approvals.md](approvals.md) |
| `ML_SENTRY_RUNNER_TOKEN` | Token for external training runners (e.g. Colab). Can only list, claim and report jobs |

## Execution kill switches

| Variable | Default | Effect |
|---|---|---|
| `ML_SENTRY_RETRAINING_ENABLED` | `false` | Must be `true` for **external** runners. Mock runners don't need it |
| `ML_SENTRY_DEPLOYMENT_ENABLED` | `false` | Must be `true` for **non-mock** release targets (`mlflow_alias`) |

The policy must also allow the action (`defaults.*_enabled` and the model's
`retraining.allowed` / `deployment.allowed`), and humans must approve it.

## Notifications

See [notifications.md](notifications.md) for:
- `ML_SENTRY_NOTIFICATION_PROVIDER`
- `ML_SENTRY_SLACK_WEBHOOK_URL`
- `ML_SENTRY_TELEGRAM_BOT_TOKEN`, `ML_SENTRY_TELEGRAM_CHAT_ID`
- `ML_SENTRY_WEBHOOK_URL`, `ML_SENTRY_WEBHOOK_SECRET`

## MLflow

See [mlflow-integration.md](mlflow-integration.md) for:
- `ML_SENTRY_MLFLOW_TRACKING_URI`
- `ML_SENTRY_MLFLOW_ALLOWED_EXPERIMENTS`
- `ML_SENTRY_MLFLOW_TRACKING_TOKEN`
- `ML_SENTRY_MLFLOW_USERNAME`, `ML_SENTRY_MLFLOW_PASSWORD`

## Compose-only

| Variable | Notes |
|---|---|
| `POSTGRES_PASSWORD` | Required |
| `ML_SENTRY_API_PORT` | Host port, default 8000 |
| `POSTGRES_HOST_PORT` | Host port, default 5433 |
| `MLFLOW_HOST_PORT` | Host port, default 5000 |
| `ML_SENTRY_ENV_FILE` | Alternative settings file path |
| `COMPOSE_PROFILES=mlflow` | Also start the bundled MLflow |

## Your own policy

```bash
cp configs/my-model.example.yaml configs/governance.yaml    # template for your model
docker compose exec api ml-sentry validate-policy /app/configs/governance.yaml
# .env: ML_SENTRY_POLICY_PATH=/app/configs/governance.yaml
docker compose up -d
```

`./configs` is mounted read-only into the containers.

## CLI

| Command | Purpose |
|---|---|
| `ml-sentry doctor` | Check configuration, database, migrations, checkpoints, workflows, policy, storage, notifications, reviewers, runner token, MLflow and API |
| `ml-sentry demo [--full]` | Offline demo. `--full` also starts the two-approval mock loop for `demo_regressor` |
| `ml-sentry add-reviewer NAME` | Print a new reviewer token and its configuration entry |
| `ml-sentry mlflow-import RUN_ID [--role baseline]` | Ingest one MLflow run |
| `ml-sentry validate-policy PATH` | Validate a policy file |
| `ml-sentry migrate` | Apply migrations and create the checkpoint tables (automatic at start) |
| `ml-sentry serve` / `ml-sentry worker` | The api and worker processes |
| `ml-sentry generate-token` | Print a random token |
