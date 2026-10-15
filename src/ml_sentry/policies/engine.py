"""Deterministic regression policy engine.

``evaluate`` is a pure function of (event, policy, baseline, received_at):
the same inputs always produce the same :class:`PolicyResult`. No LLM, network
or database access happens here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ml_sentry.metrics.regression import METRIC_DEFINITIONS_VERSION
from ml_sentry.metrics.validation import ValidationIssue, validate_evidence
from ml_sentry.policies.loader import LoadedPolicy
from ml_sentry.policies.schemas import MetricPolicy
from ml_sentry.schemas.evaluation import EvaluationEvent

Severity = Literal["none", "warning", "critical", "invalid_evidence"]
ProposedAction = Literal["none", "notify", "investigate", "request_retraining_approval"]

_SEVERITY_RANK: dict[Severity, int] = {"none": 0, "warning": 1, "critical": 2}
# Below this magnitude a baseline is treated as zero and relative change is undefined.
ZERO_BASELINE_EPSILON = 1e-12


class BaselineEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    model_version: str
    metrics: dict[str, float]


class MetricResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    metric: str
    role: Literal["primary", "guardrail"]
    direction: Literal["minimize", "maximize"]
    current: float
    baseline: float | None
    absolute_delta: float | None
    relative_degradation: float | None
    severity: Severity
    findings: list[str]


class PolicyResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    policy_version: str
    metric_definitions_version: str
    event_id: str
    model_name: str
    model_version: str
    role: Literal["monitoring", "baseline"]
    baseline_event_id: str | None
    evidence_valid: bool
    validation_errors: list[ValidationIssue]
    severity: Severity
    metric_results: list[MetricResult]
    retraining_eligible: bool
    proposed_action: ProposedAction
    decision_reason: str

    def digest(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, allow_nan=False)
        return hashlib.sha256(canonical.encode()).hexdigest()


def _evaluate_metric(
    name: str,
    role: Literal["primary", "guardrail"],
    rule: MetricPolicy,
    current: float,
    baseline: float | None,
) -> MetricResult:
    severity: Severity = "none"
    findings: list[str] = []

    def raise_to(level: Severity) -> None:
        nonlocal severity
        if _SEVERITY_RANK[level] > _SEVERITY_RANK[severity]:
            severity = level

    if rule.maximum_acceptable is not None and current > rule.maximum_acceptable:
        raise_to("critical")
        findings.append(
            f"{name}={current:g} exceeds maximum_acceptable {rule.maximum_acceptable:g}"
        )
    if rule.minimum_acceptable is not None and current < rule.minimum_acceptable:
        raise_to("critical")
        findings.append(
            f"{name}={current:g} is below minimum_acceptable {rule.minimum_acceptable:g}"
        )

    absolute_delta: float | None = None
    relative: float | None = None
    if baseline is not None:
        absolute_delta = current - baseline
        degradation = absolute_delta if rule.direction == "minimize" else -absolute_delta
        if abs(baseline) <= ZERO_BASELINE_EPSILON:
            if rule.warning_relative is not None or rule.critical_relative is not None:
                findings.append(
                    f"baseline {name} is zero; relative change is undefined, "
                    "only absolute limits were applied"
                )
        else:
            relative = degradation / abs(baseline)
            critical, warning = rule.critical_relative, rule.warning_relative
            if critical is not None and relative > critical:
                raise_to("critical")
                findings.append(
                    f"{name} degraded {relative:.1%} vs baseline (critical above {critical:.1%})"
                )
            elif warning is not None and relative > warning:
                raise_to("warning")
                findings.append(
                    f"{name} degraded {relative:.1%} vs baseline (warning above {warning:.1%})"
                )

    return MetricResult(
        metric=name,
        role=role,
        direction=rule.direction,
        current=current,
        baseline=baseline,
        absolute_delta=absolute_delta,
        relative_degradation=relative,
        severity=severity,
        findings=findings,
    )


def evaluate(
    event: EvaluationEvent,
    loaded: LoadedPolicy,
    baseline: BaselineEvidence | None,
    *,
    received_at: datetime,
) -> PolicyResult:
    policy = loaded.policy
    model_policy = policy.models.get(event.model_name)
    # A baseline event is never compared against another baseline.
    if event.role == "baseline":
        baseline = None

    issues = validate_evidence(
        event,
        model_policy,
        policy.defaults,
        baseline_available=baseline is not None,
        received_at=received_at,
    )

    common = {
        "policy_version": loaded.version,
        "metric_definitions_version": METRIC_DEFINITIONS_VERSION,
        "event_id": event.event_id,
        "model_name": event.model_name,
        "model_version": event.model_version,
        "role": event.role,
        "baseline_event_id": baseline.event_id if baseline else None,
    }

    if issues or model_policy is None:
        codes = ", ".join(sorted({i.code for i in issues}))
        return PolicyResult(
            **common,
            evidence_valid=False,
            validation_errors=issues,
            severity="invalid_evidence",
            metric_results=[],
            retraining_eligible=False,
            proposed_action="notify",
            decision_reason=f"Evidence rejected ({codes}); not evaluated as a performance "
            "result. Fix the evaluation or configuration and resubmit.",
        )

    results = [
        _evaluate_metric(
            name,
            "primary" if name == model_policy.primary_metric else "guardrail",
            rule,
            event.metrics[name],
            baseline.metrics.get(name) if baseline else None,
        )
        for name, rule in sorted(model_policy.metrics.items())
    ]
    severity: Severity = max(
        (r.severity for r in results), key=lambda s: _SEVERITY_RANK[s], default="none"
    )
    retraining_eligible = policy.defaults.retraining_enabled and model_policy.retraining.allowed

    action: ProposedAction
    if severity == "none":
        action, reason = "none", "All configured metrics are within policy thresholds."
    elif severity == "warning":
        action, reason = "notify", "Warning-level degradation; notify the model owner."
    elif retraining_eligible:
        action = "request_retraining_approval"
        reason = (
            "Critical degradation; retraining is allowed by policy and requires human approval."
        )
    else:
        action = "investigate"
        reason = "Critical degradation; retraining is disabled by policy, investigate manually."

    return PolicyResult(
        **common,
        evidence_valid=True,
        validation_errors=[],
        severity=severity,
        metric_results=results,
        retraining_eligible=retraining_eligible,
        proposed_action=action,
        decision_reason=reason,
    )


def baseline_from_metrics(
    event_id: str, model_version: str, metrics: dict[str, object]
) -> BaselineEvidence:
    """Rebuild baseline evidence from stored JSON, keeping only finite numeric values."""
    numeric = {k: float(v) for k, v in metrics.items() if isinstance(v, int | float)}
    return BaselineEvidence(event_id=event_id, model_version=model_version, metrics=numeric)
