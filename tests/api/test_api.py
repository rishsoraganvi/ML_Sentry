import json
import uuid

from fastapi.testclient import TestClient

from tests.conftest import make_event, metrics_for_rmse

EVENTS = "/api/v1/evaluations/events"


def post_baseline(client: TestClient) -> None:
    resp = client.post(EVENTS, json=make_event(event_id="base-1", role="baseline"))
    assert resp.status_code == 201, resp.text


def test_health_endpoints(client: TestClient) -> None:
    assert client.get("/health/live").json()["status"] == "ok"
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"]["migrations"] == "ok"


def test_health_ready_reports_unmigrated_database(client: TestClient) -> None:
    client.app.state.engine.dispose()  # type: ignore[attr-defined]
    with client.app.state.engine.begin() as conn:  # type: ignore[attr-defined]
        conn.exec_driver_sql("DELETE FROM alembic_version")
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "unavailable"


def test_api_requires_token(client: TestClient) -> None:
    for headers in ({"Authorization": ""}, {"Authorization": "Bearer wrong-token"}):
        resp = client.post(EVENTS, json=make_event(), headers=headers)
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "UNAUTHENTICATED"
    assert client.get("/api/v1/incidents", headers={"Authorization": ""}).status_code == 401


def test_healthy_event_records_decision_without_incident(client: TestClient) -> None:
    post_baseline(client)
    resp = client.post(EVENTS, json=make_event(metrics=metrics_for_rmse(25_200)))
    assert resp.status_code == 201
    body = resp.json()
    assert body["severity"] == "none"
    assert body["incident_id"] is None
    assert body["baseline_event_id"] == "base-1"
    assert client.get("/api/v1/incidents").json()["total"] == 0


def test_critical_event_opens_incident_with_audit_trail(client: TestClient) -> None:
    post_baseline(client)
    resp = client.post(
        EVENTS, json=make_event(event_id="evt-crit", metrics=metrics_for_rmse(31_000))
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["severity"] == "critical"
    assert body["proposed_action"] == "investigate"
    incident_id = body["incident_id"]

    detail = client.get(f"/api/v1/incidents/{incident_id}").json()
    assert detail["event_id"] == "evt-crit"
    assert detail["category"] == "performance"
    # TestClient runs background tasks before returning, so the workflow has finished.
    assert detail["status"] == "awaiting_review"
    assert detail["workflow_status"] == "completed"
    assert detail["workflow_thread_id"] == f"incident-{incident_id}"
    assert detail["notifications"] == [
        {
            "provider": "console",
            "kind": "incident",
            "status": "delivered",
            "attempts": 1,
            "error_code": None,
            "delivered_at": detail["notifications"][0]["delivered_at"],
        }
    ]
    assert detail["policy_version"] == body["policy_version"]
    assert "rmse degraded" in detail["summary"]
    assert detail["evaluation"]["dataset_version"] == "2026-01-01"

    audit = client.get(f"/api/v1/incidents/{incident_id}/audit").json()
    assert [a["action"] for a in audit] == [
        "evaluation.received",
        "policy.evaluated",
        "incident.opened",
        "workflow.run",
        "workflow.completed",
    ]
    assert all(a["actor"] == "api-token" for a in audit[:3])
    assert all(a["actor"] == "governance-workflow" for a in audit[3:])
    assert audit[0]["evidence_refs"]["baseline_event_id"] == "base-1"
    assert audit[1]["details"]["result_digest"] == body["result_digest"]
    completed = audit[4]
    assert completed["outcome"] == "awaiting_human_review"
    assert completed["details"]["route"] == [
        "collect_evidence",
        "validate_evidence",
        "compare_baseline",
        "investigate",
        "evaluate_policy",
        "decide_action",
        "notify",
        "audit_and_close",
    ]
    assert completed["details"]["policy_results"]["consistent_with_ingestion"] is True


def test_duplicate_event_is_idempotent(client: TestClient) -> None:
    post_baseline(client)
    payload = make_event(event_id="evt-dup", metrics=metrics_for_rmse(31_000))
    first = client.post(EVENTS, json=payload)
    second = client.post(EVENTS, json=payload)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["created"] is False
    assert second.json()["incident_id"] == first.json()["incident_id"]
    assert client.get("/api/v1/incidents").json()["total"] == 1


def test_reused_event_id_with_different_payload_conflicts(client: TestClient) -> None:
    post_baseline(client)
    client.post(EVENTS, json=make_event(event_id="evt-x", metrics=metrics_for_rmse(25_000)))
    resp = client.post(EVENTS, json=make_event(event_id="evt-x", metrics=metrics_for_rmse(26_000)))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EVENT_ID_CONFLICT"


def test_nan_metric_becomes_evidence_incident(client: TestClient) -> None:
    post_baseline(client)
    raw = json.dumps(
        make_event(event_id="evt-nan", metrics={**metrics_for_rmse(25_000), "rmse": float("nan")})
    )
    assert "NaN" in raw
    resp = client.post(EVENTS, content=raw, headers={"Content-Type": "application/json"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["evidence_valid"] is False
    assert body["severity"] == "invalid_evidence"
    detail = client.get(f"/api/v1/incidents/{body['incident_id']}").json()
    assert detail["category"] == "evidence"
    assert detail["evaluation"]["metrics"]["rmse"] == "NaN"


def test_schema_errors_use_stable_envelope(client: TestClient) -> None:
    resp = client.post(EVENTS, json=make_event(task_type="classification", extra="x"))
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert {tuple(d["loc"])[-1] for d in error["details"]} >= {"task_type", "extra"}


def test_oversized_body_rejected(client: TestClient) -> None:
    big = make_event(environment_metadata={f"k{i}": "v" * 250 for i in range(300)})
    resp = client.post(EVENTS, json=big)
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_get_event_decision(client: TestClient) -> None:
    post_baseline(client)
    resp = client.get(f"{EVENTS}/base-1")
    assert resp.status_code == 200
    assert resp.json()["severity"] == "none"
    assert client.get(f"{EVENTS}/nope").json()["error"]["code"] == "EVENT_NOT_FOUND"


def test_incident_listing_filters_and_paginates(client: TestClient) -> None:
    post_baseline(client)
    for i, rmse in enumerate([28_500, 31_000, 32_000]):
        client.post(EVENTS, json=make_event(event_id=f"evt-{i}", metrics=metrics_for_rmse(rmse)))
    client.post(EVENTS, json=make_event(event_id="evt-bad", sample_count=1))

    page = client.get("/api/v1/incidents", params={"limit": 2}).json()
    assert page["total"] == 4 and len(page["items"]) == 2
    rest = client.get("/api/v1/incidents", params={"limit": 2, "offset": 2}).json()
    ids = {i["id"] for i in page["items"]} | {i["id"] for i in rest["items"]}
    assert len(ids) == 4

    critical = client.get("/api/v1/incidents", params={"severity": "critical"}).json()
    assert critical["total"] == 2
    invalid = client.get("/api/v1/incidents", params={"severity": "invalid_evidence"}).json()
    assert invalid["items"][0]["event_id"] == "evt-bad"
    assert client.get("/api/v1/incidents", params={"model_name": "other"}).json()["total"] == 0
    assert client.get("/api/v1/incidents", params={"limit": 500}).status_code == 422


def test_unknown_incident_is_404(client: TestClient) -> None:
    missing = uuid.uuid4()
    assert client.get(f"/api/v1/incidents/{missing}").status_code == 404
    assert client.get(f"/api/v1/incidents/{missing}/audit").status_code == 404


def test_openapi_documents_v1(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/api/v1/evaluations/events", "/api/v1/incidents", "/health/ready"} <= set(paths)
