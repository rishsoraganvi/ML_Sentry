"""MLflow REST adapter against a mocked server, plus M4 acceptance: same outcome as a
generic event."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from ml_sentry.evaluation.mlflow_adapter import MlflowClient, MlflowError, build_event
from ml_sentry.policies.loader import LoadedPolicy
from ml_sentry.schemas.evaluation import EvaluationEvent
from ml_sentry.services.ingestion import ingest_event
from tests.conftest import NOW, make_event, metrics_for_rmse

TAGS = {
    "ml_sentry.model_name": "house_price_regressor",
    "ml_sentry.model_version": "1",
    "ml_sentry.target_name": "sale_price",
    "ml_sentry.dataset_id": "housing-holdout",
    "ml_sentry.dataset_version": "2026-01-01",
    "ml_sentry.evaluation_protocol_id": "holdout-v1",
}


def run_payload(
    metrics: dict[str, float], tags: dict[str, str] | None = None, status: str = "FINISHED"
) -> dict[str, Any]:
    return {
        "run": {
            "info": {
                "run_id": "r1",
                "experiment_id": "7",
                "status": status,
                "end_time": 1772359200000,  # 2026-03-01T10:00:00Z
            },
            "data": {
                "metrics": [{"key": k, "value": v} for k, v in metrics.items()],
                "tags": [
                    {"key": k, "value": v} for k, v in (TAGS if tags is None else tags).items()
                ],
            },
        }
    }


def mlflow(
    run: dict[str, Any],
    *,
    allowed: list[str] | None = None,
    extra: Callable[..., Any] | None = None,
) -> MlflowClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/runs/get"):
            return httpx.Response(200, json=run)
        if path.endswith("/experiments/get"):
            return httpx.Response(200, json={"experiment": {"name": "house-prices"}})
        if extra is not None:
            return extra(request)  # type: ignore[no-any-return]
        return httpx.Response(404, json={"error_code": "RESOURCE_DOES_NOT_EXIST"})

    return MlflowClient(
        "http://mlflow.test",
        allowed_experiments=allowed if allowed is not None else ["house-prices"],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


MLFLOW_LONG_NAMES = {
    "root_mean_squared_error": 31_000.0,
    "mean_squared_error": 31_000.0**2,
    "mean_absolute_error": 31_000.0 * 0.76,
    "r2_score": 0.9,
    "example_count": 500.0,
}


def test_run_maps_to_canonical_event() -> None:
    event = mlflow(run_payload(MLFLOW_LONG_NAMES)).run_to_event("r1")
    assert event.event_id == "mlflow-r1" and event.source == "mlflow"
    assert event.metrics["rmse"] == 31_000.0 and event.metrics["r2"] == 0.9
    assert event.sample_count == 500
    assert event.evaluation_run_id == "r1"


def test_metric_prefix_and_short_names() -> None:
    tags = {**TAGS, "ml_sentry.metric_prefix": "holdout_", "ml_sentry.sample_count": "250"}
    metrics = {"holdout_rmse": 10.0, "holdout_mae": 7.0, "train_rmse": 1.0}
    event = mlflow(run_payload(metrics, tags)).run_to_event("r1")
    assert event.metrics == {"rmse": 10.0, "mae": 7.0}
    assert event.sample_count == 250


@pytest.mark.parametrize(
    ("client_kwargs", "run", "code"),
    [
        ({"allowed": []}, run_payload(MLFLOW_LONG_NAMES), "MLFLOW_EXPERIMENT_NOT_ALLOWED"),
        ({"allowed": ["other"]}, run_payload(MLFLOW_LONG_NAMES), "MLFLOW_EXPERIMENT_NOT_ALLOWED"),
        ({}, run_payload(MLFLOW_LONG_NAMES, status="RUNNING"), "MLFLOW_RUN_NOT_FINISHED"),
        ({}, run_payload(MLFLOW_LONG_NAMES, tags={}), "MLFLOW_RUN_MISSING_TAGS"),
        ({}, run_payload({"accuracy": 0.9}), "MLFLOW_RUN_NO_METRICS"),
    ],
)
def test_fail_closed_cases(client_kwargs: dict[str, Any], run: dict[str, Any], code: str) -> None:
    with pytest.raises(MlflowError) as exc:
        mlflow(run, **client_kwargs).run_to_event("r1")
    assert exc.value.code == code


def test_experiment_allowed_by_id_without_lookup() -> None:
    assert mlflow(run_payload(MLFLOW_LONG_NAMES), allowed=["7"]).experiment_allowed("7")


def test_unreachable_and_unauthorized() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("x", request=request)

    client = MlflowClient(
        "http://mlflow.test",
        allowed_experiments=["7"],
        client=httpx.Client(transport=httpx.MockTransport(down)),
    )
    with pytest.raises(MlflowError, match="ML_SENTRY_MLFLOW_TRACKING_URI"):
        client.get_run("r1")
    denied = MlflowClient(
        "http://mlflow.test",
        token="t",
        allowed_experiments=["7"],
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))),
    )
    with pytest.raises(MlflowError) as exc:
        denied.get_run("r1")
    assert exc.value.code == "MLFLOW_UNAUTHORIZED"


def test_alias_calls() -> None:
    calls: list[tuple[str, str]] = []

    def extra(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "DELETE":
            assert request.content and b'"alias"' in request.content  # JSON body, not params
        if request.method == "GET":
            return httpx.Response(200, json={"model_version": {"version": "3"}})
        return httpx.Response(200, json={})

    client = mlflow(run_payload(MLFLOW_LONG_NAMES), extra=extra)
    assert client.get_alias_version("house", "champion") == "3"
    client.set_alias("house", "champion", "4")
    client.delete_alias("house", "champion")
    assert [m for m, _ in calls] == ["GET", "POST", "DELETE"]


def test_mlflow_run_and_generic_event_reach_the_same_decision(
    session_factory: sessionmaker[Session], policy: LoadedPolicy
) -> None:
    """Milestone 4 acceptance: same evidence, same outcome, whatever the integration."""
    ingest_event(
        session_factory,
        EvaluationEvent.model_validate(make_event(event_id="base", role="baseline")),
        policy,
        actor="t",
        received_at=NOW,
    )
    generic = ingest_event(
        session_factory,
        EvaluationEvent.model_validate(
            make_event(event_id="generic-1", metrics=metrics_for_rmse(31_000))
        ),
        policy,
        actor="t",
        received_at=NOW,
    )
    from_mlflow = ingest_event(
        session_factory,
        mlflow(run_payload(MLFLOW_LONG_NAMES)).run_to_event("r1"),
        policy,
        actor="t",
        received_at=NOW,
    )
    for field in ("severity", "proposed_action", "decision_reason", "baseline_event_id"):
        assert getattr(generic.decision, field) == getattr(from_mlflow.decision, field)

    def strip(result: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in result.items() if k != "event_id"}

    assert strip(generic.decision.result) == strip(from_mlflow.decision.result)
    assert from_mlflow.incident is not None and generic.incident is not None
    assert from_mlflow.incident.category == generic.incident.category


def test_build_event_rejects_bad_identifiers() -> None:
    info = {"experiment_id": "7", "end_time": 1772359200000}
    data = {
        "metrics": [{"key": "rmse", "value": 1.0}],
        "tags": [
            {"key": k, "value": "bad value!" if k.endswith("model_name") else v}
            for k, v in TAGS.items()
        ],
    }
    with pytest.raises(MlflowError) as exc:
        build_event("r1", info, data)
    assert exc.value.code == "MLFLOW_RUN_INVALID"
