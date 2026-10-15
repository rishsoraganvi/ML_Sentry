"""Conditional edges. Pure functions of the state, so they are easy to test exhaustively.

Every guard that protects a side effect is repeated at each step, even when an
earlier step should already have excluded the case: routing must stay safe for any
state, including a corrupted or hand-edited checkpoint. The nodes that perform side
effects additionally re-validate approvals against the database.
"""

from __future__ import annotations

from typing import Literal

from ml_sentry.graph.state import GovernanceState

AfterValidation = Literal["compare_baseline", "evaluate_policy"]
AfterDecision = Literal["request_retraining_approval", "notify", "audit_and_close"]
AfterRetrainingWait = Literal["await_retraining_approval", "create_training_job", "notify"]
AfterJobCreation = Literal["wait_for_training_job", "notify"]
AfterJobWait = Literal["wait_for_training_job", "evaluate_candidate", "notify"]
AfterGates = Literal["request_deployment_approval", "notify"]
AfterDeploymentWait = Literal["await_deployment_approval", "release_candidate", "notify"]
AfterRelease = Literal["verify_release", "notify"]


def _remediation_allowed(state: GovernanceState) -> bool:
    return (
        state.get("evidence_valid") is True
        and state.get("severity") == "critical"
        and state.get("retraining_eligible") is True
        and state.get("retraining_blocked_reason") is None
        and state.get("error_code") is None
    )


def _release_allowed(state: GovernanceState) -> bool:
    acceptance = state.get("acceptance_results") or {}
    return (
        _remediation_allowed(state)
        and state.get("approval_status") == "approved"
        and state.get("job_status") == "succeeded"
        and acceptance.get("passed") is True
        and state.get("deployment_eligible") is True
        and state.get("deployment_blocked_reason") is None
    )


def route_after_validation(state: GovernanceState) -> AfterValidation:
    # Invalid evidence skips baseline comparison; the policy step records it as such.
    return "compare_baseline" if state.get("evidence_valid") is True else "evaluate_policy"


def route_after_decision(state: GovernanceState) -> AfterDecision:
    action = state.get("proposed_action")
    if state.get("evidence_valid") is not True or state.get("error_code") is not None:
        return "notify"
    if action == "none":
        return "audit_and_close"
    if action == "request_retraining_approval" and _remediation_allowed(state):
        return "request_retraining_approval"
    return "notify"


def route_after_retraining_approval(state: GovernanceState) -> AfterRetrainingWait:
    status = state.get("approval_status")
    if status == "pending" and _remediation_allowed(state):
        return "await_retraining_approval"
    if status == "approved" and _remediation_allowed(state):
        return "create_training_job"
    return "notify"


def route_after_job_creation(state: GovernanceState) -> AfterJobCreation:
    if (
        state.get("training_job_id")
        and state.get("execution_status") == "queued"
        and state.get("approval_status") == "approved"
        and _remediation_allowed(state)
    ):
        return "wait_for_training_job"
    return "notify"


def route_after_job(state: GovernanceState) -> AfterJobWait:
    status = state.get("job_status")
    if status in ("queued", "running"):
        return "wait_for_training_job"
    if status == "succeeded" and _remediation_allowed(state):
        return "evaluate_candidate"
    return "notify"


def route_after_gates(state: GovernanceState) -> AfterGates:
    return "request_deployment_approval" if _release_allowed(state) else "notify"


def route_after_deployment_approval(state: GovernanceState) -> AfterDeploymentWait:
    status = state.get("deployment_approval_status")
    if status == "pending" and _release_allowed(state):
        return "await_deployment_approval"
    if status == "approved" and _release_allowed(state):
        return "release_candidate"
    return "notify"


def route_after_release(state: GovernanceState) -> AfterRelease:
    if state.get("release_status") == "released" and _release_allowed(state):
        return "verify_release"
    return "notify"
