"""Single-file ML Sentry client (Python standard library only).

Copy this file next to your training/evaluation code (or into Google Colab) to:

* submit regression evaluations          -> MLSentryClient(url, token).submit_evaluation(...)
* act as an external training runner     -> MLSentryClient(url, runner_token=...).run_jobs(...)

It never needs a reviewer token: approvals are made by humans in the ML Sentry UI.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

__all__ = ["MLSentryClient", "MLSentryError", "regression_metrics"]


class MLSentryError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


def regression_metrics(y_true: Sequence[float], y_pred: Sequence[float]) -> dict[str, float]:
    """MAE, MSE, RMSE and R² (R² omitted for a constant target), as ML Sentry defines them."""
    if len(y_true) != len(y_pred) or not y_true:
        raise ValueError("y_true and y_pred must be non-empty and the same length")
    n = len(y_true)
    errors = [float(t) - float(p) for t, p in zip(y_true, y_pred, strict=True)]
    mse = math.fsum(e * e for e in errors) / n
    metrics = {"mae": math.fsum(abs(e) for e in errors) / n, "mse": mse, "rmse": math.sqrt(mse)}
    mean = math.fsum(float(t) for t in y_true) / n
    ss_tot = math.fsum((float(t) - mean) ** 2 for t in y_true)
    if ss_tot > 0:
        metrics["r2"] = 1 - (mse * n) / ss_tot
    return metrics


class MLSentryClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        runner_token: str | None = None,
        timeout: float = 30.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.runner_token = runner_token
        self.timeout = timeout
        self.extra_headers = extra_headers or {}

    # -- HTTP -----------------------------------------------------------------------

    def _call(self, method: str, path: str, body: Any = None, *, runner: bool = False) -> Any:
        token = self.runner_token if runner else self.token
        if not token:
            raise ValueError("this call needs " + ("runner_token" if runner else "token"))
        data = None if body is None else json.dumps(body, allow_nan=True).encode()
        request = urllib.request.Request(  # noqa: S310 - the operator supplies the URL
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                **self.extra_headers,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as err:
            try:
                error = json.loads(err.read()).get("error", {})
            except ValueError:
                error = {}
            raise MLSentryError(
                err.code, error.get("code", "HTTP_ERROR"), error.get("message", str(err))
            ) from None
        return json.loads(raw) if raw else None

    # -- evaluations ----------------------------------------------------------------

    def submit_evaluation(
        self,
        *,
        event_id: str,
        model_name: str,
        model_version: str,
        target_name: str,
        dataset_id: str,
        dataset_version: str,
        evaluation_protocol_id: str,
        sample_count: int,
        metrics: dict[str, float],
        role: str = "monitoring",
        evaluation_timestamp: datetime | None = None,
        **optional: Any,
    ) -> dict[str, Any]:
        """Send one evaluation. Re-sending the same event_id with the same data is safe."""
        timestamp = evaluation_timestamp or datetime.now(UTC)
        event = {
            "schema_version": 1,
            "event_id": event_id,
            "role": role,
            "model_name": model_name,
            "model_version": model_version,
            "task_type": "regression",
            "target_name": target_name,
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
            "evaluation_protocol_id": evaluation_protocol_id,
            "evaluation_timestamp": timestamp.isoformat(),
            "sample_count": int(sample_count),
            "metrics": {k: float(v) for k, v in metrics.items()},
            **optional,
        }
        result: dict[str, Any] = self._call("POST", "/api/v1/evaluations/events", event)
        return result

    def submit_mlflow_run(self, run_id: str, role: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            "POST", "/api/v1/evaluations/mlflow", {"run_id": run_id, "role": role}
        )
        return result

    def incident(self, incident_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call("GET", f"/api/v1/incidents/{incident_id}")
        return result

    # -- external runner ------------------------------------------------------------

    def queued_jobs(self, model_name: str | None = None) -> list[dict[str, Any]]:
        query = f"?runner=external&model_name={model_name}" if model_name else "?runner=external"
        result: list[dict[str, Any]] = self._call("GET", f"/api/v1/jobs{query}", runner=True)
        return result

    def claim(self, job_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call("POST", f"/api/v1/jobs/{job_id}/claim", runner=True)
        return result

    def report(self, job_id: str, result: dict[str, Any]) -> dict[str, Any]:
        response: dict[str, Any] = self._call(
            "POST", f"/api/v1/jobs/{job_id}/result", result, runner=True
        )
        return response

    def run_jobs(
        self,
        train_and_evaluate: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        model_name: str,
        wait_seconds: float = 0,
        poll_seconds: float = 10,
        max_jobs: int = 1,
        log: Callable[[str], None] = print,
    ) -> list[dict[str, Any]]:
        """Claim approved jobs for ``model_name`` and run ``train_and_evaluate(spec)``.

        ``train_and_evaluate`` receives the approved job spec (pipeline config, training
        dataset, and the champion's holdout identity) and must return::

            {"candidate_model_ref": "...", "candidate_model_version": "...",
             "metrics": {...}, "sample_count": N, "candidate_run_id": "..." (optional)}

        with the metrics computed on that SAME holdout. Exceptions are reported as a
        failed job (error code only; no traceback leaves this process).
        """
        deadline = time.monotonic() + wait_seconds
        finished: list[dict[str, Any]] = []
        while len(finished) < max_jobs:
            jobs = self.queued_jobs(model_name)
            if not jobs:
                if time.monotonic() >= deadline:
                    break
                time.sleep(poll_seconds)
                continue
            job = self.claim(jobs[0]["id"])
            spec, holdout = job["spec"], job["spec"]["holdout"]
            log(f"Claimed job {job['id']} ({spec['pipeline_id']}); training...")
            try:
                out = train_and_evaluate(spec)
                result = {
                    "status": "succeeded",
                    "candidate_model_ref": out["candidate_model_ref"],
                    "candidate_model_version": out["candidate_model_version"],
                    "candidate_run_id": out.get("candidate_run_id"),
                    "evaluation": {
                        "dataset_id": holdout["dataset_id"],
                        "dataset_version": holdout["dataset_version"],
                        "evaluation_protocol_id": holdout["evaluation_protocol_id"],
                        "target_name": holdout["target_name"],
                        "sample_count": int(out["sample_count"]),
                        "metrics": {k: float(v) for k, v in out["metrics"].items()},
                    },
                }
            except Exception as exc:  # report, don't leak details
                log(f"Training failed: {type(exc).__name__}")
                result = {"status": "failed", "error_code": type(exc).__name__.upper()[:64]}
            finished.append(self.report(job["id"], result))
            log(f"Reported job {job['id']}: {finished[-1]['status']}")
        return finished
