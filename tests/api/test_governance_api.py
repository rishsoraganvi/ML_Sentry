"""HTTP-level tests for roles, approvals, jobs, the UI and admin endpoints."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ALICE, BOB, RUNNER, TOKEN, make_event, metrics_for_rmse

EVENTS = "/api/v1/evaluations/events"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def open_remediation_incident(client: TestClient) -> str:
    client.post(EVENTS, json=make_event(event_id="b", role="baseline", model_name="demo_regressor"))
    resp = client.post(
        EVENTS,
        json=make_event(
            event_id="c", model_name="demo_regressor", metrics=metrics_for_rmse(31_000)
        ),
    )
    assert resp.status_code == 201
    assert resp.json()["proposed_action"] == "request_retraining_approval"
    return str(resp.json()["incident_id"])


def pending(client: TestClient, token: str = ALICE) -> list[dict[str, Any]]:
    resp = client.get("/api/v1/approvals/pending", headers=auth(token))
    assert resp.status_code == 200
    return list(resp.json())


def decide(
    client: TestClient, approval: dict[str, Any], token: str, decision: str = "approve"
) -> Any:
    return client.post(
        f"/api/v1/approvals/{approval['id']}/decision",
        headers=auth(token),
        json={"decision": decision, "spec_digest": approval["spec_digest"], "note": "ok"},
    )


@pytest.mark.parametrize(
    ("method", "path", "token", "expected"),
    [
        ("post", EVENTS, ALICE, 403),  # reviewers can't submit evidence
        ("post", EVENTS, RUNNER, 403),  # runners can't submit evidence
        ("get", "/api/v1/incidents", RUNNER, 403),  # runners can't read incidents
        ("get", "/api/v1/incidents", ALICE, 200),
        ("get", "/api/v1/approvals/pending", RUNNER, 403),
        ("get", "/api/v1/approvals/pending", TOKEN, 200),
        ("get", "/api/v1/jobs", RUNNER, 200),
        ("post", "/api/v1/admin/test-notification", ALICE, 403),
        ("get", "/api/v1/incidents", "not-a-token", 401),
    ],
)
def test_role_separation(
    client: TestClient, method: str, path: str, token: str, expected: int
) -> None:
    kwargs: dict[str, Any] = {"headers": auth(token)}
    if method == "post" and path == EVENTS:
        kwargs["json"] = make_event()
    assert getattr(client, method)(path, **kwargs).status_code == expected


def test_full_approval_flow_over_http(client: TestClient) -> None:
    incident_id = open_remediation_incident(client)
    (retraining,) = pending(client)
    assert retraining["gate"] == "retraining"
    assert retraining["spec"]["pipeline_id"] == "demo_mock_retrain"

    # The service token can never decide, even with the right digest.
    assert decide(client, retraining, TOKEN).status_code == 403
    # A stale/forged digest is refused.
    stale = {**retraining, "spec_digest": "0" * 64}
    assert decide(client, stale, ALICE).json()["error"]["code"] == "SPEC_DIGEST_MISMATCH"

    resp = decide(client, retraining, ALICE)
    assert resp.status_code == 200 and resp.json()["decided_by"] == "reviewer:alice"
    assert decide(client, retraining, BOB).json()["error"]["code"] == "APPROVAL_ALREADY_DECIDED"

    # The mock job is queued; run it the way the worker would.
    worker_runner = client.app.state.workflow  # type: ignore[attr-defined]
    from ml_sentry.jobs.worker import Worker

    Worker(worker_runner).run_once()
    (deployment,) = pending(client)
    assert deployment["gate"] == "deployment"
    assert deployment["spec"]["release"]["adapter"] == "mock"
    assert all(g["passed"] for g in deployment["spec"]["gates"])
    assert decide(client, deployment, BOB).status_code == 200

    incident = client.get(f"/api/v1/incidents/{incident_id}").json()
    assert incident["status"] == "resolved"
    actions = [a["action"] for a in client.get(f"/api/v1/incidents/{incident_id}/audit").json()]
    for expected in (
        "approval.requested",
        "approval.decided",
        "job.created",
        "job.completed",
        "release.performed",
        "release.verified",
        "workflow.completed",
    ):
        assert expected in actions


def test_external_runner_flow_over_http(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Switch the demo pipeline to the external runner and enable the environment switch.
    deps = client.app.state.workflow.deps  # type: ignore[attr-defined]
    from tests.graph.conftest import policy_with

    external = policy_with(
        lambda raw: raw["pipelines"]["demo_mock_retrain"].update(runner="external")
    )
    monkeypatch.setattr(deps, "policy", external)
    monkeypatch.setattr(deps, "retraining_enabled_env", True)
    monkeypatch.setattr(client.app.state, "policy", external)  # type: ignore[attr-defined]

    open_remediation_incident(client)
    (retraining,) = pending(client)
    decide(client, retraining, ALICE)

    jobs = client.get("/api/v1/jobs", headers=auth(RUNNER)).json()
    (job,) = jobs
    assert job["runner"] == "external" and job["status"] == "queued"
    # Reporting before claiming is refused; claiming twice is refused.
    result: dict[str, Any] = {"status": "failed", "error_code": "OOM"}
    early = client.post(f"/api/v1/jobs/{job['id']}/result", headers=auth(RUNNER), json=result)
    assert early.json()["error"]["code"] == "JOB_NOT_CLAIMED_BY_YOU"
    assert client.post(f"/api/v1/jobs/{job['id']}/claim", headers=auth(RUNNER)).status_code == 200
    again = client.post(f"/api/v1/jobs/{job['id']}/claim", headers=auth(RUNNER))
    assert again.json()["error"]["code"] == "JOB_NOT_QUEUED"
    # Malformed results are rejected by the schema.
    bad = client.post(
        f"/api/v1/jobs/{job['id']}/result", headers=auth(RUNNER), json={"status": "succeeded"}
    )
    assert bad.status_code == 422

    done = client.post(f"/api/v1/jobs/{job['id']}/result", headers=auth(RUNNER), json=result)
    assert done.status_code == 200 and done.json()["status"] == "failed"
    incident = client.get(f"/api/v1/incidents/{job['incident_id']}").json()
    assert incident["workflow_status"] == "completed"
    assert pending(client) == []  # no deployment approval after a failed job


def test_ui_is_served_with_strict_headers(client: TestClient) -> None:
    page = client.get("/ui/")
    assert page.status_code == 200
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"
    js = client.get("/ui/app.js").text
    for sink in (".innerHTML", ".outerHTML", "insertAdjacentHTML", "eval("):
        assert sink not in js  # all values rendered with textContent
    assert client.get("/ui", follow_redirects=False).status_code == 307


def test_admin_test_notification(client: TestClient) -> None:
    resp = client.post("/api/v1/admin/test-notification")
    assert resp.json() == {"provider": "console", "delivered": True, "error_code": None}


def test_mlflow_endpoint_fails_closed_when_not_configured(client: TestClient) -> None:
    resp = client.post("/api/v1/evaluations/mlflow", json={"run_id": "abc"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "MLFLOW_NOT_CONFIGURED"
