"""Job lifecycle: create (once per approval), claim, complete, time out."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ml_sentry.jobs.models import JobResult
from ml_sentry.storage.models import AuditEventRecord, JobRecord, utcnow

TERMINAL = ("succeeded", "failed", "timed_out")


class JobError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _audit(session: Session, job: JobRecord, actor: str, action: str, outcome: str) -> None:
    session.add(
        AuditEventRecord(
            created_at=utcnow(),
            actor=actor,
            action=action,
            incident_id=job.incident_id,
            evaluation_event_id=None,
            policy_version=job.spec.get("policy_version"),
            evidence_refs={"job_id": str(job.id), "spec_digest": job.spec_digest},
            outcome=outcome,
            details={"pipeline_id": job.pipeline_id, "runner": job.runner},
        )
    )


def create_job(
    session_factory: sessionmaker[Session],
    *,
    incident_id: uuid.UUID,
    approval_id: uuid.UUID,
    spec: dict[str, Any],
    spec_digest: str,
    now: datetime | None = None,
) -> JobRecord:
    """Create the single job allowed for this approval (idempotent)."""
    now = now or utcnow()
    with session_factory() as session:
        existing = session.scalars(
            select(JobRecord).where(JobRecord.approval_id == approval_id)
        ).first()
        if existing is not None:
            return existing
        job = JobRecord(
            incident_id=incident_id,
            approval_id=approval_id,
            model_name=spec["model_name"],
            pipeline_id=spec["pipeline_id"],
            runner=spec["runner"],
            spec=spec,
            spec_digest=spec_digest,
            status="queued",
            attempts=0,
            created_at=now,
            deadline_at=now + timedelta(minutes=int(spec["timeout_minutes"])),
        )
        session.add(job)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            return session.scalars(
                select(JobRecord).where(JobRecord.approval_id == approval_id)
            ).one()
        _audit(session, job, "governance-workflow", "job.created", "queued")
        session.commit()
        return job


def get_job(session_factory: sessionmaker[Session], job_id: uuid.UUID) -> JobRecord | None:
    with session_factory() as session:
        return session.get(JobRecord, job_id)


def claim_job(
    session_factory: sessionmaker[Session],
    job_id: uuid.UUID,
    claimer: str,
    *,
    runner: str,
    now: datetime | None = None,
) -> JobRecord:
    now = now or utcnow()
    with session_factory() as session:
        job = session.get(JobRecord, job_id)
        if job is None:
            raise JobError(404, "JOB_NOT_FOUND", f"No job '{job_id}'")
        if job.runner != runner:
            raise JobError(403, "WRONG_RUNNER", f"job is for the '{job.runner}' runner")
        if job.deadline_at <= now:
            raise JobError(409, "JOB_DEADLINE_PASSED", "job deadline has passed")
        changed = session.execute(
            update(JobRecord)
            .where(JobRecord.id == job_id, JobRecord.status == "queued")
            .values(
                status="running",
                claimed_by=claimer,
                started_at=now,
                attempts=JobRecord.attempts + 1,
            )
        ).rowcount  # type: ignore[attr-defined]
        if changed != 1:
            session.rollback()
            raise JobError(409, "JOB_NOT_QUEUED", f"job is {job.status}")
        session.refresh(job)
        _audit(session, job, claimer, "job.claimed", "running")
        session.commit()
        return job


def complete_job(
    session_factory: sessionmaker[Session],
    job_id: uuid.UUID,
    claimer: str,
    result: JobResult,
    *,
    resolved_evaluation: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> JobRecord:
    """Store a runner's result. Only the runner that claimed the job may report it."""
    now = now or utcnow()
    with session_factory() as session:
        job = session.get(JobRecord, job_id)
        if job is None:
            raise JobError(404, "JOB_NOT_FOUND", f"No job '{job_id}'")
        if job.status != "running" or job.claimed_by != claimer:
            raise JobError(409, "JOB_NOT_CLAIMED_BY_YOU", "claim the job before reporting it")
        payload = result.model_dump(mode="json")
        if resolved_evaluation is not None:
            payload["evaluation"] = resolved_evaluation
        late = job.deadline_at <= now
        status = "timed_out" if late else result.status
        changed = session.execute(
            update(JobRecord)
            .where(JobRecord.id == job_id, JobRecord.status == "running")
            .values(
                status=status,
                finished_at=now,
                result=payload,
                error_code="DEADLINE_EXCEEDED" if late else result.error_code,
                logs_ref=result.logs_ref,
            )
        ).rowcount  # type: ignore[attr-defined]
        if changed != 1:
            session.rollback()
            raise JobError(409, "JOB_NOT_RUNNING", "job is no longer running")
        session.refresh(job)
        _audit(session, job, claimer, "job.completed", status)
        session.commit()
        return job


def timeout_overdue(
    session_factory: sessionmaker[Session], now: datetime | None = None
) -> list[uuid.UUID]:
    now = now or utcnow()
    incidents: list[uuid.UUID] = []
    with session_factory() as session:
        overdue = session.scalars(
            select(JobRecord).where(
                JobRecord.status.in_(("queued", "running")), JobRecord.deadline_at <= now
            )
        ).all()
        for job in overdue:
            changed = session.execute(
                update(JobRecord)
                .where(JobRecord.id == job.id, JobRecord.status.in_(("queued", "running")))
                .values(status="timed_out", finished_at=now, error_code="DEADLINE_EXCEEDED")
            ).rowcount  # type: ignore[attr-defined]
            if changed == 1:
                _audit(session, job, "system", "job.timed_out", "timed_out")
                incidents.append(job.incident_id)
        session.commit()
    return incidents


def queued_jobs(
    session_factory: sessionmaker[Session], *, runner: str, limit: int = 50
) -> list[JobRecord]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(JobRecord)
                .where(JobRecord.status == "queued", JobRecord.runner == runner)
                .order_by(JobRecord.created_at)
                .limit(limit)
            )
        )
