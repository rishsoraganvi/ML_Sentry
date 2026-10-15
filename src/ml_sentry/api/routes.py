"""HTTP routes: health checks and the versioned /api/v1 surface."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ml_sentry.api.errors import ApiError
from ml_sentry.api.schemas import (
    AuditEventOut,
    DecisionOut,
    ErrorResponse,
    HealthOut,
    IncidentDetailOut,
    IncidentOut,
    IncidentPage,
    NotificationOut,
)
from ml_sentry.api.security import authenticate, require_roles, require_token
from ml_sentry.schemas.evaluation import EvaluationEvent
from ml_sentry.services.ingestion import EventConflictError, IngestOutcome, ingest_event
from ml_sentry.storage.database import migration_status, ping
from ml_sentry.storage.models import (
    AuditEventRecord,
    EvaluationEventRecord,
    IncidentRecord,
    NotificationDeliveryRecord,
    PolicyDecisionRecord,
)

health = APIRouter(prefix="/health", tags=["health"])
v1 = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(authenticate)],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
READERS = [Depends(require_roles("service", "reviewer"))]


def _sessions(request: Request) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    return factory


@health.get("/live", response_model=HealthOut)
def live() -> HealthOut:
    return HealthOut(status="ok", checks={"process": "ok"})


@health.get("/ready", response_model=HealthOut, responses={503: {"model": HealthOut}})
def ready(request: Request, response: Response) -> HealthOut:
    checks: dict[str, str] = {}
    try:
        ping(request.app.state.engine)
        checks["database"] = "ok"
        current, head = migration_status(request.app.state.engine)
        checks["migrations"] = "ok" if current == head else f"at {current}, expected {head}"
    except Exception as exc:  # readiness must report, not crash
        checks["database"] = f"unavailable: {type(exc).__name__}"
    checks["policy"] = f"ok ({request.app.state.policy.version})"
    healthy = all(v.startswith("ok") for v in checks.values())
    if not healthy:
        response.status_code = 503
    return HealthOut(status="ok" if healthy else "unavailable", checks=checks)


def _decision_out(outcome: IngestOutcome) -> DecisionOut:
    d = outcome.decision
    return DecisionOut(
        event_id=outcome.event.event_id,
        created=outcome.created,
        evidence_valid=bool(outcome.event.evidence_valid),
        severity=d.severity,
        proposed_action=d.proposed_action,
        decision_reason=d.decision_reason,
        policy_version=d.policy_version,
        baseline_event_id=d.baseline_event_id,
        incident_id=d.incident_id,
        result_digest=d.result_digest,
        result=d.result,
    )


@v1.post(
    "/evaluations/events",
    response_model=DecisionOut,
    status_code=201,
    tags=["evaluations"],
    responses={
        200: {"model": DecisionOut, "description": "Duplicate event; original result returned"},
        409: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
def post_evaluation_event(
    event: EvaluationEvent,
    request: Request,
    background: BackgroundTasks,
    actor: Annotated[str, Depends(require_token)],
) -> JSONResponse:
    return ingest_response(request, background, event, actor)


def ingest_response(
    request: Request, background: BackgroundTasks, event: EvaluationEvent, actor: str
) -> JSONResponse:
    try:
        outcome = ingest_event(_sessions(request), event, request.app.state.policy, actor=actor)
    except EventConflictError:
        raise ApiError(
            409,
            "EVENT_ID_CONFLICT",
            f"event_id '{event.event_id}' was already used for a different payload",
        ) from None
    if outcome.created and outcome.incident is not None:
        # The governance workflow runs after the response is sent; its progress is
        # visible on the incident (workflow_status) and in the audit timeline.
        background.add_task(request.app.state.workflow.run_incident, outcome.incident.id)
    body = _decision_out(outcome)
    return JSONResponse(
        status_code=201 if outcome.created else 200, content=body.model_dump(mode="json")
    )


@v1.get(
    "/evaluations/events/{event_id}",
    response_model=DecisionOut,
    tags=["evaluations"],
    dependencies=READERS,
    responses={404: {"model": ErrorResponse}},
)
def get_evaluation_event(event_id: str, request: Request) -> DecisionOut:
    with _sessions(request)() as session:
        record = session.scalars(
            select(EvaluationEventRecord).where(EvaluationEventRecord.event_id == event_id)
        ).first()
        if record is None:
            raise ApiError(404, "EVENT_NOT_FOUND", f"No evaluation event '{event_id}'")
        decision = session.scalars(
            select(PolicyDecisionRecord).where(
                PolicyDecisionRecord.evaluation_event_id == record.id
            )
        ).one()
        return _decision_out(
            IngestOutcome(event=record, decision=decision, incident=None, created=False)
        )


def _incident_out(incident: IncidentRecord, event_id: str) -> IncidentOut:
    return IncidentOut(
        id=incident.id,
        event_id=event_id,
        model_name=incident.model_name,
        model_version=incident.model_version,
        category=incident.category,
        severity=incident.severity,
        status=incident.status,
        proposed_action=incident.proposed_action,
        summary=incident.summary,
        created_at=incident.created_at,
        updated_at=incident.updated_at,
        workflow_thread_id=incident.workflow_thread_id,
        workflow_status=incident.workflow_status,
    )


@v1.get("/incidents", response_model=IncidentPage, tags=["incidents"], dependencies=READERS)
def list_incidents(
    request: Request,
    model_name: str | None = None,
    severity: Literal["warning", "critical", "invalid_evidence"] | None = None,
    status: Literal["open", "awaiting_review", "closed", "resolved"] | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> IncidentPage:
    filters = []
    if model_name is not None:
        filters.append(IncidentRecord.model_name == model_name)
    if severity is not None:
        filters.append(IncidentRecord.severity == severity)
    if status is not None:
        filters.append(IncidentRecord.status == status)
    with _sessions(request)() as session:
        total = session.scalar(select(func.count()).select_from(IncidentRecord).where(*filters))
        rows = session.execute(
            select(IncidentRecord, EvaluationEventRecord.event_id)
            .join(
                EvaluationEventRecord,
                IncidentRecord.evaluation_event_id == EvaluationEventRecord.id,
            )
            .where(*filters)
            .order_by(IncidentRecord.created_at.desc(), IncidentRecord.id)
            .limit(limit)
            .offset(offset)
        ).all()
    return IncidentPage(
        items=[_incident_out(inc, ev) for inc, ev in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@v1.get(
    "/incidents/{incident_id}",
    response_model=IncidentDetailOut,
    tags=["incidents"],
    dependencies=READERS,
    responses={404: {"model": ErrorResponse}},
)
def get_incident(incident_id: uuid.UUID, request: Request) -> IncidentDetailOut:
    with _sessions(request)() as session:
        incident = session.get(IncidentRecord, incident_id)
        if incident is None:
            raise ApiError(404, "INCIDENT_NOT_FOUND", f"No incident '{incident_id}'")
        event = session.get_one(EvaluationEventRecord, incident.evaluation_event_id)
        decision = session.scalars(
            select(PolicyDecisionRecord).where(PolicyDecisionRecord.incident_id == incident.id)
        ).one()
        deliveries = session.scalars(
            select(NotificationDeliveryRecord)
            .where(NotificationDeliveryRecord.incident_id == incident.id)
            .order_by(NotificationDeliveryRecord.created_at)
        ).all()
    base = _incident_out(incident, event.event_id)
    return IncidentDetailOut(
        **base.model_dump(),
        workflow_error=incident.workflow_error,
        policy_version=decision.policy_version,
        decision_reason=decision.decision_reason,
        decision=decision.result,
        evaluation=event.payload,
        notifications=[
            NotificationOut(
                provider=d.provider,
                kind=d.kind,
                status=d.status,
                attempts=d.attempts,
                error_code=d.error_code,
                delivered_at=d.delivered_at,
            )
            for d in deliveries
        ],
    )


@v1.get(
    "/incidents/{incident_id}/audit",
    response_model=list[AuditEventOut],
    tags=["incidents"],
    dependencies=READERS,
    responses={404: {"model": ErrorResponse}},
)
def get_incident_audit(incident_id: uuid.UUID, request: Request) -> list[AuditEventOut]:
    with _sessions(request)() as session:
        if session.get(IncidentRecord, incident_id) is None:
            raise ApiError(404, "INCIDENT_NOT_FOUND", f"No incident '{incident_id}'")
        rows = session.scalars(
            select(AuditEventRecord)
            .where(AuditEventRecord.incident_id == incident_id)
            .order_by(AuditEventRecord.id)
        ).all()
    return [
        AuditEventOut(
            id=r.id,
            created_at=r.created_at,
            actor=r.actor,
            action=r.action,
            outcome=r.outcome,
            policy_version=r.policy_version,
            evidence_refs=r.evidence_refs,
            details=r.details,
        )
        for r in rows
    ]
