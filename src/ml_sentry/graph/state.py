"""Typed, versioned governance workflow state.

Only identifiers, metrics and bounded summaries live here. Never put secrets,
datasets, predictions, model weights or raw exception traces in the state: it is
checkpointed to the database after every step.
"""

from __future__ import annotations

import operator
import uuid
from typing import Annotated, Any, TypedDict

STATE_VERSION = 2
MAX_ERROR_MESSAGE = 256


class GovernanceState(TypedDict, total=False):
    state_version: int

    # Identity and references
    thread_id: str
    incident_id: str | None
    event_record_id: str
    event_id: str
    model_name: str
    model_version: str
    evaluation_run_id: str | None
    role: str

    # Evidence
    dataset_id: str
    dataset_version: str
    sample_count: int
    current_metrics: dict[str, float | str]  # non-finite values stored as "NaN"/"Infinity"
    baseline_event_id: str | None
    baseline_metrics: dict[str, float]
    # The champion's fixed holdout (identity + metrics); candidates are judged on it.
    holdout: dict[str, Any] | None
    metric_deltas: dict[str, dict[str, float | None]]
    history: list[dict[str, Any]]
    investigation_summary: str
    evidence_valid: bool
    validation_errors: list[dict[str, Any]]

    # Policy and decision
    policy_version: str
    policy_results: dict[str, Any]
    severity: str
    findings: list[str]
    retraining_eligible: bool
    retraining_blocked_reason: str | None
    proposed_action: str
    decision_reason: str

    # Gate A: retraining approval and job
    approval_request_id: str | None
    approval_status: str | None
    approved_by: str | None
    approval_expires_at: str | None
    training_job_id: str | None
    job_status: str | None
    execution_status: str | None

    # Candidate
    candidate_run_id: str | None
    candidate_model_ref: str | None
    candidate_model_version: str | None
    candidate_metrics: dict[str, float] | None
    candidate_evaluation: dict[str, Any] | None
    acceptance_results: dict[str, Any] | None
    deployment_eligible: bool
    deployment_blocked_reason: str | None

    # Gate B: deployment approval and release
    deployment_approval_id: str | None
    deployment_approval_status: str | None
    deployment_approved_by: str | None
    release_id: str | None
    release_status: str | None

    # Outcome
    notification_status: str
    outcome: str | None
    audit_event_ids: Annotated[list[int], operator.add]
    visited: Annotated[list[str], operator.add]
    error_code: str | None
    error_message: str | None


def thread_id_for(incident_id: uuid.UUID) -> str:
    """One stable LangGraph thread per incident; never shared between incidents."""
    return f"incident-{incident_id}"


def bounded(message: str) -> str:
    return message[:MAX_ERROR_MESSAGE]
