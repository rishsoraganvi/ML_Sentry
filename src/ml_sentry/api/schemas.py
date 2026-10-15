"""API response models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class DecisionOut(BaseModel):
    event_id: str
    created: bool
    evidence_valid: bool
    severity: str
    proposed_action: str
    decision_reason: str
    policy_version: str
    baseline_event_id: str | None
    incident_id: uuid.UUID | None
    result_digest: str
    result: dict[str, Any]


class IncidentOut(BaseModel):
    id: uuid.UUID
    event_id: str
    model_name: str
    model_version: str
    category: str
    severity: str
    status: str
    proposed_action: str
    summary: str
    created_at: datetime
    updated_at: datetime
    workflow_thread_id: str | None
    workflow_status: str | None


class NotificationOut(BaseModel):
    provider: str
    kind: str
    status: str
    attempts: int
    error_code: str | None
    delivered_at: datetime | None


class IncidentDetailOut(IncidentOut):
    workflow_error: str | None
    policy_version: str
    decision_reason: str
    decision: dict[str, Any]
    evaluation: dict[str, Any]
    notifications: list[NotificationOut]


class IncidentPage(BaseModel):
    items: list[IncidentOut]
    total: int
    limit: int
    offset: int


class AuditEventOut(BaseModel):
    id: int
    created_at: datetime
    actor: str
    action: str
    outcome: str
    policy_version: str | None
    evidence_refs: dict[str, Any]
    details: dict[str, Any]


class HealthOut(BaseModel):
    status: str
    checks: dict[str, str]
