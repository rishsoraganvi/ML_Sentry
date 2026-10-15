"""Mock training runner: simulates retraining without training anything.

The candidate's metrics are derived deterministically from the champion's holdout
metrics and the pipeline's ``simulated_improvement`` config value (negative values
simulate a worse candidate). Useful for demos and for exercising every gate.
"""

from __future__ import annotations

import math
from typing import Any

from ml_sentry.jobs.models import CandidateEvaluation, JobResult
from ml_sentry.metrics.regression import REGRESSION_METRICS

MOCK_ACTOR = "mock-runner"


def simulate(spec: dict[str, Any], job_id: str) -> JobResult:
    improvement = float(spec.get("config", {}).get("simulated_improvement", 0.1))
    if spec.get("config", {}).get("simulate_failure") is True:
        return JobResult(status="failed", error_code="SIMULATED_FAILURE")
    holdout = spec["holdout"]
    metrics: dict[str, float] = {}
    for name, value in holdout["metrics"].items():
        meta = REGRESSION_METRICS.get(name)
        if meta is None or name == "mse":
            continue
        if meta.direction == "minimize":
            metrics[name] = value * (1 - improvement)
        else:  # move toward / away from the upper bound (1.0 for R²)
            upper = meta.upper_bound if meta.upper_bound is not None else value * 2
            metrics[name] = upper - (upper - value) * (1 - improvement)
    if "rmse" in metrics:
        metrics["mse"] = metrics["rmse"] ** 2
    metrics = {k: round(v, 6) for k, v in metrics.items() if math.isfinite(v)}
    version = f"{spec['model_version']}-retrain-{job_id[:8]}"
    return JobResult(
        status="succeeded",
        candidate_model_ref=f"mock://{spec['model_name']}/{version}",
        candidate_model_version=version,
        candidate_run_id=f"mock-run-{job_id[:8]}",
        evaluation=CandidateEvaluation(
            dataset_id=holdout["dataset_id"],
            dataset_version=holdout["dataset_version"],
            evaluation_protocol_id=holdout["evaluation_protocol_id"],
            target_name=holdout["target_name"],
            sample_count=holdout["sample_count"],
            metrics=metrics,
        ),
    )
