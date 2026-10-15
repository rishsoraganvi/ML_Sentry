"""Evidence validation: decide whether an evaluation can be trusted at all.

Invalid evidence is reported as an evidence incident and is never evaluated as
a performance result.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

from ml_sentry.metrics.regression import REGRESSION_METRICS
from ml_sentry.policies.schemas import Defaults, ModelPolicy
from ml_sentry.schemas.evaluation import EvaluationEvent

MAX_CLOCK_SKEW = timedelta(minutes=5)
_REL_TOL = 1e-6


class ValidationIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    metric: str | None = None


def _range_issue(name: str, value: float) -> ValidationIssue | None:
    spec = REGRESSION_METRICS.get(name)
    if spec is None:
        return None
    if spec.lower_bound is not None and value < spec.lower_bound:
        return ValidationIssue(
            code="METRIC_OUT_OF_RANGE",
            metric=name,
            message=f"{name}={value} is below its minimum possible value {spec.lower_bound}",
        )
    if spec.upper_bound is not None and value > spec.upper_bound:
        return ValidationIssue(
            code="METRIC_OUT_OF_RANGE",
            metric=name,
            message=f"{name}={value} is above its maximum possible value {spec.upper_bound}",
        )
    return None


def _consistency_issues(metrics: dict[str, float]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    mse, rmse, mae = metrics.get("mse"), metrics.get("rmse"), metrics.get("mae")
    if (
        mse is not None
        and rmse is not None
        and not math.isclose(rmse * rmse, mse, rel_tol=_REL_TOL, abs_tol=1e-12)
    ):
        issues.append(
            ValidationIssue(
                code="METRIC_INCONSISTENT",
                metric="rmse",
                message=f"rmse²={rmse * rmse} does not match mse={mse}",
            )
        )
    # MAE <= RMSE always holds for the same predictions.
    if mae is not None and rmse is not None and mae > rmse * (1 + _REL_TOL) + 1e-12:
        issues.append(
            ValidationIssue(
                code="METRIC_INCONSISTENT",
                metric="mae",
                message=f"mae={mae} exceeds rmse={rmse}, which is mathematically impossible",
            )
        )
    return issues


def validate_evidence(
    event: EvaluationEvent,
    model_policy: ModelPolicy | None,
    defaults: Defaults,
    *,
    baseline_available: bool,
    received_at: datetime,
) -> list[ValidationIssue]:
    if model_policy is None:
        return [
            ValidationIssue(
                code="UNKNOWN_MODEL",
                message=f"model '{event.model_name}' has no policy; add it to the policy file",
            )
        ]

    issues: list[ValidationIssue] = []

    minimum = model_policy.minimum_sample_count or defaults.minimum_sample_count
    if event.sample_count == 0:
        issues.append(ValidationIssue(code="EMPTY_EVALUATION", message="sample_count is 0"))
    elif event.sample_count < minimum:
        issues.append(
            ValidationIssue(
                code="INSUFFICIENT_SAMPLES",
                message=f"sample_count {event.sample_count} is below the minimum {minimum}",
            )
        )

    if event.evaluation_timestamp > received_at + MAX_CLOCK_SKEW:
        issues.append(
            ValidationIssue(
                code="TIMESTAMP_IN_FUTURE",
                message="evaluation_timestamp is more than 5 minutes in the future",
            )
        )

    finite: dict[str, float] = {}
    for name, value in event.metrics.items():
        if not math.isfinite(value):
            issues.append(
                ValidationIssue(code="METRIC_NOT_FINITE", metric=name, message=f"{name} is {value}")
            )
            continue
        range_issue = _range_issue(name, value)
        if range_issue is not None:
            issues.append(range_issue)
            continue
        finite[name] = value

    for name in model_policy.metrics:
        if name not in event.metrics:
            issues.append(
                ValidationIssue(
                    code="METRIC_MISSING",
                    metric=name,
                    message=f"policy requires metric '{name}' but the event does not include it",
                )
            )

    issues.extend(_consistency_issues(finite))

    require_baseline = (
        model_policy.require_baseline
        if model_policy.require_baseline is not None
        else defaults.require_baseline
    )
    if event.role == "monitoring" and require_baseline and not baseline_available:
        issues.append(
            ValidationIssue(
                code="BASELINE_MISSING",
                message="no comparable baseline evaluation (same model, target, dataset "
                "id/version and evaluation protocol) has been registered",
            )
        )

    return issues
