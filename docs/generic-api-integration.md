# Generic event API

Your evaluation job POSTs one event per completed evaluation. You don't need MLflow.

```
POST /api/v1/evaluations/events
Authorization: Bearer <ML_SENTRY_API_TOKEN>
Content-Type: application/json
```

## Event schema (version 1)

| Field | Required | Description |
|---|---|---|
| `schema_version` | yes | `1` |
| `event_id` | yes | Your unique idempotency key (`[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}`) |
| `event_type` | no | `evaluation.completed` (default) |
| `role` | no | `monitoring` (default) or `baseline` |
| `source` | no | `generic` (default) |
| `model_name`, `model_version` | yes | Model identity; `model_name` must exist in the policy |
| `task_type` | yes | `regression` |
| `target_name` | yes | The single predicted target |
| `dataset_id`, `dataset_version` | yes | Evaluation dataset and its immutable version |
| `evaluation_protocol_id` | yes | Identifies how the evaluation was performed |
| `evaluation_timestamp` | yes | ISO 8601 **with timezone** |
| `sample_count` | yes | Number of evaluated rows |
| `metrics` | yes | 1–32 entries of `name: number` (lower-case names) |
| `evaluation_run_id`, `code_revision`, `training_run_id` | no | References for reproducibility |
| `environment_metadata` | no | Up to 32 short string pairs |
| `report_uri` | no | `https`, `http`, `mlflow-artifacts`, `s3` or `gs` URI. Stored as a reference only, never fetched |

Unknown fields are rejected. NaN and Infinity metric values are accepted by the
schema on purpose, so they're recorded as an evidence incident instead of
disappearing as a 422.

See [examples/sample_event.json](../examples/sample_event.json).

## Baselines

A monitoring event is compared with the most recent **valid** `role: baseline` event
that has the same `model_name`, `target_name`, `dataset_id`, `dataset_version` and
`evaluation_protocol_id`. Evaluations that differ in any of these are never compared.
Register a baseline when you promote a champion model, typically from its evaluation
on your fixed holdout set.

## Responses

| Status | Meaning |
|---|---|
| `201` | Recorded; body contains the decision |
| `200` | Same `event_id` and identical payload already recorded; original decision returned |
| `401` | Missing or invalid token (`UNAUTHENTICATED`) |
| `409` | `event_id` reused with a different payload (`EVENT_ID_CONFLICT`) |
| `413` | Body too large (`PAYLOAD_TOO_LARGE`) |
| `422` | Schema violation (`VALIDATION_ERROR`, with field locations) |

Errors always use `{"error": {"code", "message", "details"}}`.

The decision body includes `severity`, `proposed_action`, `decision_reason`,
`policy_version`, `baseline_event_id`, `incident_id` (null when healthy),
`result_digest` (SHA-256 of the canonical result) and the full per-metric `result`.

If an incident was opened, its governance workflow starts after the response is sent.
Poll the incident to follow `workflow_status` (see [workflow.md](workflow.md)).

## All endpoints and who may call them

| Endpoint | Roles | Purpose |
|---|---|---|
| `POST /api/v1/evaluations/events` | service | Submit an evaluation |
| `POST /api/v1/evaluations/mlflow` | service | Submit an MLflow run ([mlflow-integration.md](mlflow-integration.md)) |
| `GET /api/v1/evaluations/events/{event_id}` | service, reviewer | Decision for a recorded event |
| `GET /api/v1/incidents?model_name=&severity=&status=&limit=&offset=` | service, reviewer | List incidents, newest first. `status` is `open`, `awaiting_review`, `closed` or `resolved` |
| `GET /api/v1/incidents/{id}` | service, reviewer | Incident, decision, evaluation, workflow status and notification deliveries |
| `GET /api/v1/incidents/{id}/audit` | service, reviewer | Audit timeline |
| `GET /api/v1/approvals/pending`, `GET /api/v1/approvals/{id}` | service, reviewer | Approval requests with their exact spec and `spec_digest` |
| `POST /api/v1/approvals/{id}/decision` | **reviewer only** | `{"decision": "approve"\|"reject", "spec_digest": ..., "note": ...}` ([approvals.md](approvals.md)) |
| `GET /api/v1/jobs`, `GET /api/v1/jobs/{id}` | service, reviewer, runner | Training jobs |
| `POST /api/v1/jobs/{id}/claim`, `POST /api/v1/jobs/{id}/result` | **runner only** | External training ([training-adapters.md](training-adapters.md)) |
| `POST /api/v1/admin/test-notification` | service | Send one explicit test notification |
| `GET /health/live`, `GET /health/ready` | none | Liveness and readiness |
| `GET /ui/` | none (the page asks for a token) | Reviewer UI |

The roles map to tokens as follows:
- **service:** `ML_SENTRY_API_TOKEN`
- **reviewer:** a personal token from `ML_SENTRY_REVIEWERS`
- **runner:** `ML_SENTRY_RUNNER_TOKEN`

A wrong role gets `403 FORBIDDEN`. OpenAPI is served at `/docs`.

## Python client

[`examples/colab/ml_sentry_client.py`](../examples/colab/ml_sentry_client.py) is a
single file with no dependencies. It works in scripts, schedulers and Colab:

```python
from ml_sentry_client import MLSentryClient, regression_metrics
sentry = MLSentryClient("http://127.0.0.1:8000", SERVICE_TOKEN)
decision = sentry.submit_evaluation(event_id=..., model_name=..., model_version=...,
    target_name=..., dataset_id=..., dataset_version=..., evaluation_protocol_id=...,
    sample_count=len(y), metrics=regression_metrics(y, y_pred))
```
