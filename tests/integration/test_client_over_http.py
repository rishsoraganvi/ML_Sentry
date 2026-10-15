"""The single-file client (examples/colab) against a real HTTP server (uvicorn in a thread)."""

from __future__ import annotations

import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
import yaml

from ml_sentry.jobs.worker import Worker
from ml_sentry.main import create_app
from ml_sentry.settings import Settings
from tests.conftest import ALICE, BOB, EXAMPLE_POLICY, ROOT, RUNNER, TOKEN, metrics_for_rmse

sys.path.insert(0, str(ROOT / "examples" / "colab"))
from ml_sentry_client import MLSentryClient, MLSentryError, regression_metrics


@pytest.fixture
def server(settings: Settings, engine: object, tmp_path: Path) -> Iterator[tuple[str, Any]]:
    raw = yaml.safe_load(EXAMPLE_POLICY.read_bytes())
    raw["pipelines"]["demo_mock_retrain"]["runner"] = "external"
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(yaml.safe_dump(raw))
    app = create_app(
        settings.model_copy(update={"policy_path": policy_file, "retraining_enabled": True})
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", app
    server.should_exit = True
    thread.join(timeout=10)


def approve_pending(base: str, token: str, gate: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    (approval,) = [
        a
        for a in httpx.get(f"{base}/api/v1/approvals/pending", headers=headers).json()
        if a["gate"] == gate
    ]
    response = httpx.post(
        f"{base}/api/v1/approvals/{approval['id']}/decision",
        headers=headers,
        json={"decision": "approve", "spec_digest": approval["spec_digest"]},
    )
    assert response.status_code == 200, response.text


def test_regression_metrics_match_ml_sentry_definitions() -> None:
    from ml_sentry.metrics.regression import compute_core_metrics

    y_true, y_pred = [3.0, -0.5, 2.0, 7.0], [2.5, 0.0, 2.0, 8.0]
    assert regression_metrics(y_true, y_pred) == pytest.approx(compute_core_metrics(y_true, y_pred))


def test_client_submits_and_runs_an_external_job(server: tuple[str, Any]) -> None:
    base, app = server
    sentry = MLSentryClient(base, TOKEN, runner_token=RUNNER)
    common: dict[str, Any] = {
        "model_name": "demo_regressor",
        "target_name": "sale_price",
        "dataset_id": "holdout",
        "dataset_version": "v1",
        "evaluation_protocol_id": "p1",
        "sample_count": 500,
    }
    sentry.submit_evaluation(
        event_id="c-b",
        role="baseline",
        model_version="1",
        metrics=metrics_for_rmse(25_000),
        **common,
    )
    decision = sentry.submit_evaluation(
        event_id="c-m", model_version="2", metrics=metrics_for_rmse(31_000), **common
    )
    assert decision["proposed_action"] == "request_retraining_approval"
    assert sentry.queued_jobs("demo_regressor") == []  # nothing until a human approves

    approve_pending(base, ALICE, "retraining")
    seen: list[dict[str, Any]] = []

    def train(spec: dict[str, Any]) -> dict[str, Any]:
        seen.append(spec)
        return {
            "candidate_model_ref": "colab://demo/3",
            "candidate_model_version": "3",
            "metrics": metrics_for_rmse(24_000),
            "sample_count": 500,
        }

    # The job is created by a background task right after the approval response.
    results = sentry.run_jobs(
        train, model_name="demo_regressor", wait_seconds=15, poll_seconds=0.2, log=lambda _: None
    )
    assert [r["status"] for r in results] == ["succeeded"]
    assert seen[0]["holdout"]["dataset_version"] == "v1"
    assert seen[0]["config"] == {"simulated_improvement": 0.12}

    approve_pending(base, BOB, "deployment")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if sentry.incident(decision["incident_id"])["status"] == "resolved":
            break
        Worker(app.state.workflow).run_once()
        time.sleep(0.2)
    assert sentry.incident(decision["incident_id"])["status"] == "resolved"


def test_client_errors_and_failed_training(server: tuple[str, Any]) -> None:
    base, _ = server
    with pytest.raises(MLSentryError) as exc:
        MLSentryClient(base, "wrong-token-0123456789abcdefghijkl").incident("x")
    assert exc.value.status == 401 and exc.value.code == "UNAUTHENTICATED"
    with pytest.raises(ValueError, match="runner_token"):
        MLSentryClient(base, TOKEN).queued_jobs()
