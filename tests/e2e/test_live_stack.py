"""End-to-end tests against a RUNNING Docker Compose stack (opt-in).

Run with the stack up (api, worker, postgres; mlflow profile for the MLflow tests):

    ML_SENTRY_E2E=1 ML_SENTRY_API_TOKEN=... ML_SENTRY_E2E_REVIEWER_TOKEN=...
    ML_SENTRY_E2E_REVIEWER2_TOKEN=... uv run pytest -m stack

They use the example policy (demo_regressor + mock runner/target) and create
uniquely named demo records; nothing outside the stack is touched.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from ml_sentry.deployment.adapters import MlflowAliasAdapter
from ml_sentry.deployment.base import Previous, ReleasePlan
from ml_sentry.evaluation.mlflow_adapter import MlflowClient

pytestmark = pytest.mark.stack

API = os.environ.get("ML_SENTRY_E2E_API_URL", "http://127.0.0.1:8000")
MLFLOW = os.environ.get("ML_SENTRY_E2E_MLFLOW_URL", "http://127.0.0.1:5000")


def env(name: str) -> str:
    if not os.environ.get("ML_SENTRY_E2E"):
        pytest.skip("set ML_SENTRY_E2E=1 to run live-stack tests")
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set")
    return value


def client(token_var: str) -> httpx.Client:
    return httpx.Client(
        base_url=API, headers={"Authorization": f"Bearer {env(token_var)}"}, timeout=30
    )


def wait_for(predicate: Any, timeout: float = 60, interval: float = 1) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


def event(event_id: str, role: str, rmse: float, model: str = "demo_regressor") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event_id": event_id,
        "role": role,
        "model_name": model,
        "model_version": "1",
        "task_type": "regression",
        "target_name": "sale_price",
        "dataset_id": "e2e-holdout",
        "dataset_version": "2026-10-01",
        "evaluation_protocol_id": "holdout-v1",
        "evaluation_timestamp": datetime.now(UTC).isoformat(),
        "sample_count": 500,
        "metrics": {"rmse": rmse, "mse": rmse * rmse, "mae": rmse * 0.76, "r2": 0.9},
    }


def pending_for(reviewer: httpx.Client, incident_id: str, gate: str) -> dict[str, Any] | None:
    for approval in reviewer.get("/api/v1/approvals/pending").json():
        if approval["incident_id"] == incident_id and approval["gate"] == gate:
            return dict(approval)
    return None


def test_full_remediation_loop_through_api_and_worker() -> None:
    service = client("ML_SENTRY_API_TOKEN")
    alice = client("ML_SENTRY_E2E_REVIEWER_TOKEN")
    bob = client("ML_SENTRY_E2E_REVIEWER2_TOKEN")
    run = uuid.uuid4().hex[:8]

    assert (
        service.post(
            "/api/v1/evaluations/events", json=event(f"e2e-{run}-b", "baseline", 25_000)
        ).status_code
        == 201
    )
    resp = service.post(
        "/api/v1/evaluations/events", json=event(f"e2e-{run}-c", "monitoring", 31_000)
    )
    assert resp.status_code == 201, resp.text
    incident_id = resp.json()["incident_id"]
    assert resp.json()["proposed_action"] == "request_retraining_approval"

    # Gate A appears; the service token cannot decide it.
    gate_a = wait_for(lambda: pending_for(alice, incident_id, "retraining"))
    denied = service.post(
        f"/api/v1/approvals/{gate_a['id']}/decision",
        json={"decision": "approve", "spec_digest": gate_a["spec_digest"]},
    )
    assert denied.status_code == 403
    ok = alice.post(
        f"/api/v1/approvals/{gate_a['id']}/decision",
        json={"decision": "approve", "spec_digest": gate_a["spec_digest"], "note": "e2e"},
    )
    assert ok.status_code == 200, ok.text

    # The worker runs the mock job; Gate B appears after the candidate passes its gates.
    gate_b = wait_for(lambda: pending_for(bob, incident_id, "deployment"), timeout=90)
    assert all(g["passed"] for g in gate_b["spec"]["gates"])
    ok = bob.post(
        f"/api/v1/approvals/{gate_b['id']}/decision",
        json={"decision": "approve", "spec_digest": gate_b["spec_digest"]},
    )
    assert ok.status_code == 200, ok.text

    incident = wait_for(
        lambda: (
            (i := service.get(f"/api/v1/incidents/{incident_id}").json())["status"] == "resolved"
            and i
        ),
        timeout=60,
    )
    assert incident["workflow_status"] == "completed"
    actions = [a["action"] for a in service.get(f"/api/v1/incidents/{incident_id}/audit").json()]
    assert "release.verified" in actions and "job.completed" in actions


def test_rejection_stops_the_loop() -> None:
    service = client("ML_SENTRY_API_TOKEN")
    alice = client("ML_SENTRY_E2E_REVIEWER_TOKEN")
    run = uuid.uuid4().hex[:8]
    service.post("/api/v1/evaluations/events", json=event(f"e2e-{run}-b", "baseline", 25_000))
    incident_id = service.post(
        "/api/v1/evaluations/events", json=event(f"e2e-{run}-c", "monitoring", 31_000)
    ).json()["incident_id"]
    gate_a = wait_for(lambda: pending_for(alice, incident_id, "retraining"))
    alice.post(
        f"/api/v1/approvals/{gate_a['id']}/decision",
        json={"decision": "reject", "spec_digest": gate_a["spec_digest"]},
    )
    incident = wait_for(
        lambda: (
            (i := service.get(f"/api/v1/incidents/{incident_id}").json())["workflow_status"]
            == "completed"
            and i
        )
    )
    assert incident["status"] == "awaiting_review"
    audit = service.get(f"/api/v1/incidents/{incident_id}/audit").json()
    assert "job.created" not in [a["action"] for a in audit]
    assert audit[-1]["outcome"] == "retraining_rejected"


# -- MLflow (docker compose --profile mlflow) -----------------------------------------


def mlflow_rest(path: str, method: str = "POST", **kwargs: Any) -> dict[str, Any]:
    env("ML_SENTRY_E2E_MLFLOW")
    response = httpx.request(method, f"{MLFLOW}/api/2.0/mlflow/{path}", timeout=30, **kwargs)
    response.raise_for_status()
    return dict(response.json()) if response.content else {}


def experiment_id(name: str) -> str:
    try:
        found = mlflow_rest("experiments/get-by-name", "GET", params={"experiment_name": name})
        return str(found["experiment"]["experiment_id"])
    except httpx.HTTPStatusError:
        return str(mlflow_rest("experiments/create", json={"name": name})["experiment_id"])


def logged_run(exp: str, tags: dict[str, str], metrics: dict[str, float]) -> str:
    now = int(time.time() * 1000)
    run_id = str(
        mlflow_rest("runs/create", json={"experiment_id": exp, "start_time": now})["run"]["info"][
            "run_id"
        ]
    )
    mlflow_rest(
        "runs/log-batch",
        json={
            "run_id": run_id,
            "metrics": [
                {"key": k, "value": v, "timestamp": now, "step": 0} for k, v in metrics.items()
            ],
            "tags": [{"key": k, "value": v} for k, v in tags.items()],
        },
    )
    mlflow_rest("runs/update", json={"run_id": run_id, "status": "FINISHED", "end_time": now})
    return run_id


def test_mlflow_run_is_ingested_like_a_generic_event() -> None:
    service = client("ML_SENTRY_API_TOKEN")
    exp = experiment_id("ml-sentry-it")
    run = uuid.uuid4().hex[:8]
    tags = {
        "ml_sentry.model_name": "house_price_regressor",
        "ml_sentry.model_version": f"mlflow-{run}",
        "ml_sentry.target_name": "sale_price",
        "ml_sentry.dataset_id": f"mlflow-holdout-{run}",
        "ml_sentry.dataset_version": "v1",
        "ml_sentry.evaluation_protocol_id": "holdout-v1",
        "ml_sentry.role": "baseline",
    }
    metrics = {
        "root_mean_squared_error": 25_000.0,
        "mean_squared_error": 625_000_000.0,
        "mean_absolute_error": 19_000.0,
        "r2_score": 0.9,
        "example_count": 500.0,
    }
    baseline_run = logged_run(exp, tags, metrics)
    resp = service.post("/api/v1/evaluations/mlflow", json={"run_id": baseline_run})
    assert resp.status_code == 201, resp.text
    assert resp.json()["severity"] == "none"

    worse = {**metrics, "root_mean_squared_error": 31_000.0, "mean_squared_error": 961_000_000.0}
    monitoring_run = logged_run(exp, {**tags, "ml_sentry.role": "monitoring"}, worse)
    resp = service.post("/api/v1/evaluations/mlflow", json={"run_id": monitoring_run})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["event_id"] == f"mlflow-{monitoring_run}"
    assert body["severity"] == "critical"
    assert body["baseline_event_id"] == f"mlflow-{baseline_run}"

    # Experiments outside the allow-list are refused.
    other = logged_run(experiment_id(f"not-allowed-{run}"), tags, metrics)
    refused = service.post("/api/v1/evaluations/mlflow", json={"run_id": other})
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "MLFLOW_EXPERIMENT_NOT_ALLOWED"


def test_mlflow_alias_release_verify_and_rollback() -> None:
    env("ML_SENTRY_E2E_MLFLOW")
    name = f"e2e-model-{uuid.uuid4().hex[:8]}"
    mlflow_rest("registered-models/create", json={"name": name})
    for _ in range(2):
        mlflow_rest(
            "model-versions/create", json={"name": name, "source": "mlflow-artifacts:/e2e/model"}
        )
    adapter = MlflowAliasAdapter(MlflowClient(MLFLOW, allowed_experiments=[]))
    plan = ReleasePlan(
        model_name=name,
        target="e2e",
        adapter="mlflow_alias",
        candidate_ref=f"models:/{name}/2",
        candidate_version="2",
        registered_model=name,
        alias="champion",
    )
    MlflowClient(MLFLOW).set_alias(name, "champion", "1")
    previous = adapter.current(plan)
    assert previous == Previous(f"models:/{name}/1", "1")
    adapter.release(plan)
    assert adapter.verify(plan) is True
    adapter.rollback(plan, previous)
    assert adapter.current(plan).version == "1"
    adapter.rollback(plan, Previous(None, None))  # no previous alias -> alias removed
    assert adapter.current(plan) == Previous(None, None)
