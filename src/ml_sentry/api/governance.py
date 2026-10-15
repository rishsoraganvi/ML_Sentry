"""Approvals, jobs, MLflow ingestion and admin endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ml_sentry.api.errors import ApiError
from ml_sentry.api.routes import ingest_response
from ml_sentry.api.schemas import ErrorResponse
from ml_sentry.api.security import Principal, require_roles
from ml_sentry.evaluation.mlflow_adapter import MlflowClient, MlflowError
from ml_sentry.jobs.models import CandidateEvaluation, JobResult
from ml_sentry.jobs.service import (
    JobError,
    claim_job,
    complete_job,
    get_job,
    queued_jobs,
)
from ml_sentry.notifications.base import IncidentNotification, NotificationError
from ml_sentry.schemas.evaluation import Identifier
from ml_sentry.services.approvals import ApprovalError, decide
from ml_sentry.storage.models import ApprovalRequestRecord, JobRecord

router = APIRouter(
    prefix="/api/v1", responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}}
)

SERVICE = require_roles("service")
READERS = require_roles("service", "reviewer")
REVIEWER = require_roles("reviewer")
RUNNER = require_roles("runner")
JOB_READERS = require_roles("service", "reviewer", "runner")


def _sessions(request: Request) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    return factory


def _wake(request: Request, background: BackgroundTasks, incident_id: uuid.UUID) -> None:
    background.add_task(request.app.state.workflow.run_incident, incident_id)


# -- approvals ---------------------------------------------------------------


class ApprovalOut(BaseModel):
    id: uuid.UUID
    incident_id: uuid.UUID
    gate: str
    status: str
    spec: dict[str, Any]
    spec_digest: str
    policy_version: str
    requested_by: str
    requested_at: datetime
    expires_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str | None


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    # The digest of the spec the reviewer looked at; must match the pending request.
    spec_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    note: Annotated[str, Field(max_length=1000)] | None = None


def _approval_out(a: ApprovalRequestRecord) -> ApprovalOut:
    return ApprovalOut(
        id=a.id,
        incident_id=a.incident_id,
        gate=a.gate,
        status=a.status,
        spec=a.spec,
        spec_digest=a.spec_digest,
        policy_version=a.policy_version,
        requested_by=a.requested_by,
        requested_at=a.requested_at,
        expires_at=a.expires_at,
        decided_by=a.decided_by,
        decided_at=a.decided_at,
        decision_note=a.decision_note,
    )


@router.get("/approvals/pending", response_model=list[ApprovalOut], tags=["approvals"])
def pending_approvals(
    request: Request, _: Annotated[Principal, Depends(READERS)]
) -> list[ApprovalOut]:
    with _sessions(request)() as session:
        rows = session.scalars(
            select(ApprovalRequestRecord)
            .where(ApprovalRequestRecord.status == "pending")
            .order_by(ApprovalRequestRecord.requested_at)
        ).all()
    return [_approval_out(a) for a in rows]


@router.get(
    "/approvals/{approval_id}",
    response_model=ApprovalOut,
    tags=["approvals"],
    responses={404: {"model": ErrorResponse}},
)
def get_approval(
    approval_id: uuid.UUID, request: Request, _: Annotated[Principal, Depends(READERS)]
) -> ApprovalOut:
    with _sessions(request)() as session:
        approval = session.get(ApprovalRequestRecord, approval_id)
    if approval is None:
        raise ApiError(404, "APPROVAL_NOT_FOUND", f"No approval '{approval_id}'")
    return _approval_out(approval)


@router.post(
    "/approvals/{approval_id}/decision",
    response_model=ApprovalOut,
    tags=["approvals"],
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def decide_approval(
    approval_id: uuid.UUID,
    body: DecisionIn,
    request: Request,
    background: BackgroundTasks,
    reviewer: Annotated[Principal, Depends(REVIEWER)],
) -> ApprovalOut:
    policy = request.app.state.policy.policy
    try:
        approval = decide(
            _sessions(request),
            approval_id,
            reviewer=reviewer.name,
            decision=body.decision,
            reviewed_spec_digest=body.spec_digest,
            note=body.note,
            require_distinct_approvers=policy.defaults.require_distinct_approvers,
        )
    except ApprovalError as exc:
        raise ApiError(exc.status_code, exc.code, exc.message) from None
    _wake(request, background, approval.incident_id)
    return _approval_out(approval)


# -- jobs ----------------------------------------------------------------------


class JobOut(BaseModel):
    id: uuid.UUID
    incident_id: uuid.UUID
    model_name: str
    pipeline_id: str
    runner: str
    status: str
    spec: dict[str, Any]
    spec_digest: str
    attempts: int
    claimed_by: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    deadline_at: datetime
    result: dict[str, Any] | None
    error_code: str | None


def _job_out(j: JobRecord) -> JobOut:
    return JobOut(
        id=j.id,
        incident_id=j.incident_id,
        model_name=j.model_name,
        pipeline_id=j.pipeline_id,
        runner=j.runner,
        status=j.status,
        spec=j.spec,
        spec_digest=j.spec_digest,
        attempts=j.attempts,
        claimed_by=j.claimed_by,
        created_at=j.created_at,
        started_at=j.started_at,
        finished_at=j.finished_at,
        deadline_at=j.deadline_at,
        result=j.result,
        error_code=j.error_code,
    )


@router.get("/jobs", response_model=list[JobOut], tags=["jobs"])
def list_queued_jobs(
    request: Request,
    _: Annotated[Principal, Depends(JOB_READERS)],
    runner: Literal["external", "mock"] = "external",
    model_name: Identifier | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[JobOut]:
    jobs = queued_jobs(_sessions(request), runner=runner, limit=limit)
    return [_job_out(j) for j in jobs if model_name in (None, j.model_name)]


@router.get(
    "/jobs/{job_id}",
    response_model=JobOut,
    tags=["jobs"],
    responses={404: {"model": ErrorResponse}},
)
def get_job_route(
    job_id: uuid.UUID, request: Request, _: Annotated[Principal, Depends(JOB_READERS)]
) -> JobOut:
    job = get_job(_sessions(request), job_id)
    if job is None:
        raise ApiError(404, "JOB_NOT_FOUND", f"No job '{job_id}'")
    return _job_out(job)


@router.post(
    "/jobs/{job_id}/claim",
    response_model=JobOut,
    tags=["jobs"],
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def claim(
    job_id: uuid.UUID, request: Request, runner: Annotated[Principal, Depends(RUNNER)]
) -> JobOut:
    try:
        job = claim_job(_sessions(request), job_id, runner.name, runner="external")
    except JobError as exc:
        raise ApiError(exc.status_code, exc.code, exc.message) from None
    return _job_out(job)


def _evaluation_from_mlflow(request: Request, run_id: str) -> dict[str, Any]:
    from ml_sentry.evaluation.mlflow_adapter import build_event

    client = MlflowClient.from_settings(request.app.state.settings)
    info, data = client.metrics_for_run(run_id)
    event = build_event(run_id, info, data)
    return CandidateEvaluation(
        dataset_id=event.dataset_id,
        dataset_version=event.dataset_version,
        evaluation_protocol_id=event.evaluation_protocol_id,
        target_name=event.target_name,
        sample_count=event.sample_count,
        metrics=event.metrics,
    ).model_dump(mode="json")


@router.post(
    "/jobs/{job_id}/result",
    response_model=JobOut,
    tags=["jobs"],
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def report_result(
    job_id: uuid.UUID,
    body: JobResult,
    request: Request,
    background: BackgroundTasks,
    runner: Annotated[Principal, Depends(RUNNER)],
) -> JobOut:
    resolved = None
    if body.status == "succeeded" and body.mlflow_run_id is not None:
        try:
            resolved = _evaluation_from_mlflow(request, body.mlflow_run_id)
        except MlflowError as exc:
            raise ApiError(422, exc.code, exc.message) from None
    try:
        job = complete_job(
            _sessions(request), job_id, runner.name, body, resolved_evaluation=resolved
        )
    except JobError as exc:
        raise ApiError(exc.status_code, exc.code, exc.message) from None
    _wake(request, background, job.incident_id)
    return _job_out(job)


# -- MLflow ingestion ----------------------------------------------------------


class MlflowIngestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: Identifier
    role: Literal["monitoring", "baseline"] | None = None


@router.post(
    "/evaluations/mlflow",
    tags=["evaluations"],
    status_code=201,
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def ingest_mlflow_run(
    body: MlflowIngestIn,
    request: Request,
    background: BackgroundTasks,
    principal: Annotated[Principal, Depends(SERVICE)],
) -> JSONResponse:
    try:
        client = MlflowClient.from_settings(request.app.state.settings)
        event = client.run_to_event(body.run_id, role=body.role)
    except MlflowError as exc:
        status = 503 if exc.code in ("MLFLOW_UNREACHABLE", "MLFLOW_NOT_CONFIGURED") else 422
        raise ApiError(status, exc.code, exc.message) from None
    return ingest_response(request, background, event, principal.name)


# -- admin -----------------------------------------------------------------------


class TestNotificationOut(BaseModel):
    provider: str
    delivered: bool
    error_code: str | None


@router.post("/admin/test-notification", response_model=TestNotificationOut, tags=["admin"])
def test_notification(
    request: Request, _: Annotated[Principal, Depends(SERVICE)]
) -> TestNotificationOut:
    notifier = request.app.state.workflow.deps.notifier
    notification = IncidentNotification(
        incident_id="00000000-0000-0000-0000-000000000000",
        severity="none",
        model_name="ml-sentry",
        model_version="test",
        status="test",
        proposed_action="none",
        summary="Explicitly requested configuration test. No action is needed.",
        link=f"{request.app.state.settings.link_base}/ui/",
        kind="test",
    )
    try:
        notifier.send_incident(notification)
    except NotificationError as exc:
        return TestNotificationOut(provider=notifier.name, delivered=False, error_code=exc.code)
    return TestNotificationOut(provider=notifier.name, delivered=True, error_code=None)
