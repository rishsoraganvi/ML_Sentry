# Troubleshooting

Start with `docker compose exec api ml-sentry doctor`. Every failing check names the
setting to fix.

| Symptom | Cause / fix |
|---|---|
| `required variable POSTGRES_PASSWORD is missing a value` | `.env` is missing or incomplete. Run `cp .env.example .env` and fill in both secrets |
| API container restarts; logs say `Invalid ML Sentry configuration` | The listed `ML_SENTRY_*` variable is missing or invalid. Tokens must be at least 32 random characters |
| Logs say `invalid policy:` | Fix the YAML at the path shown, then check it with `ml-sentry validate-policy <path>` |
| `[FAIL] migrations` | Run `docker compose exec api ml-sentry migrate` (it also runs automatically at start) |
| Events return `invalid_evidence` with `BASELINE_MISSING` | Register a `role: baseline` event with the same model, target, dataset id and version, and protocol. Or set `require_baseline: false` |
| `UNKNOWN_MODEL` | Add the model to the policy file |
| `TIMESTAMP_IN_FUTURE` | Send `evaluation_timestamp` with a timezone, e.g. `...Z`, and check the clock |
| No approval request appears | The model's policy must allow retraining (`defaults.retraining_enabled` and `retraining.allowed`). The incident's audit shows `retraining_blocked` with the reason |
| `RETRAINING_DISABLED_BY_ENVIRONMENT` | External pipeline: set `ML_SENTRY_RETRAINING_ENABLED=true` and restart |
| Approve button returns `SPEC_DIGEST_MISMATCH` | The request changed; reload and review again |
| `FORBIDDEN` when approving | You used the service or runner token. Use your **reviewer** token |
| Workflow stuck in `waiting` after approving | Check `docker compose ps worker` and `docker compose logs worker`. The worker re-wakes workflows within a few seconds |
| `workflow_status: failed` | `workflow_error` on the incident has the error type. `doctor` lists failed workflows. Fix the cause and re-ingest under a new event ID |
| Mock job never runs | The worker isn't running: `docker compose up -d worker` |
| External job never claimed | The runner needs `ML_SENTRY_RUNNER_TOKEN` and the right `model_name`; `GET /api/v1/jobs?runner=external` shows the queue |
| `MLFLOW_UNREACHABLE` | From inside Docker use `http://mlflow:5000` (bundled) or a host the container can resolve. `localhost` means the container itself |
| `MLFLOW_EXPERIMENT_NOT_ALLOWED` | Add the experiment name or ID to `ML_SENTRY_MLFLOW_ALLOWED_EXPERIMENTS` |
| Colab: `URLError` / timeout | The tunnel isn't running or the URL changed. Quick-tunnel URLs change every start; update the Colab secret |
| Colab: `HTTP 401` | Wrong token in Colab Secrets (no whitespace, and notebook access enabled) |
| Notification `failed` | `GET /api/v1/incidents/{id}` → `notifications[].error_code`. Run `POST /api/v1/admin/test-notification` to test the provider |

Logs: `docker compose logs -f api worker`. Console notifications appear in the
api/worker logs, starting with `[ML Sentry]`.
