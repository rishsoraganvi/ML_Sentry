"""ORM models. Schema changes must ship with an Alembic migration."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Dialect,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetimes on every backend (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed; use UTC-aware values")
        return value.astimezone(UTC) if value is not None else None

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


JSONType = JSON().with_variant(JSONB(), "postgresql")
AutoIncrementId = BigInteger().with_variant(Integer(), "sqlite")
Ident = String(128)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class EvaluationEventRecord(Base):
    __tablename__ = "evaluation_events"
    __table_args__ = (
        Index(
            "ix_evaluation_events_comparability",
            "model_name",
            "target_name",
            "dataset_id",
            "dataset_version",
            "evaluation_protocol_id",
            "role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(Ident, unique=True)
    source: Mapped[str] = mapped_column(String(16))
    role: Mapped[str] = mapped_column(String(16))
    model_name: Mapped[str] = mapped_column(Ident, index=True)
    model_version: Mapped[str] = mapped_column(Ident)
    target_name: Mapped[str] = mapped_column(Ident)
    dataset_id: Mapped[str] = mapped_column(Ident)
    dataset_version: Mapped[str] = mapped_column(Ident)
    evaluation_protocol_id: Mapped[str] = mapped_column(Ident)
    sample_count: Mapped[int] = mapped_column(BigInteger)
    evaluation_timestamp: Mapped[datetime] = mapped_column(UTCDateTime)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONType)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType)
    payload_digest: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    processing_status: Mapped[str] = mapped_column(String(16))
    evidence_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class IncidentRecord(Base):
    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    evaluation_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation_events.id"), unique=True
    )
    model_name: Mapped[str] = mapped_column(Ident, index=True)
    model_version: Mapped[str] = mapped_column(Ident)
    category: Mapped[str] = mapped_column(String(16))  # performance | evidence
    severity: Mapped[str] = mapped_column(String(32), index=True)
    # open -> awaiting_review (a human must act) | closed (nothing to do) | resolved (released)
    status: Mapped[str] = mapped_column(String(16), index=True)
    proposed_action: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # One stable LangGraph thread per incident; never shared between incidents.
    workflow_thread_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    # pending | running | waiting (for an approval or job) | completed | failed
    workflow_status: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    workflow_error: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Cross-process lease: only the holder may advance this incident's workflow.
    workflow_lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workflow_lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ApprovalRequestRecord(Base):
    """A human decision about ONE exact action, identified by ``spec_digest``."""

    __tablename__ = "approval_requests"
    __table_args__ = (UniqueConstraint("incident_id", "gate", name="uq_approval_incident_gate"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"), index=True)
    gate: Mapped[str] = mapped_column(String(16))  # retraining | deployment
    status: Mapped[str] = mapped_column(String(16), index=True)  # pending|approved|rejected|expired
    spec: Mapped[dict[str, Any]] = mapped_column(JSONType)
    spec_digest: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(64))
    requested_by: Mapped[str] = mapped_column(String(128))
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class JobRecord(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"), index=True)
    # One job per approval: a replayed workflow step can never start a second job.
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approval_requests.id"), unique=True)
    model_name: Mapped[str] = mapped_column(Ident, index=True)
    pipeline_id: Mapped[str] = mapped_column(Ident)
    runner: Mapped[str] = mapped_column(String(16))  # mock | external
    spec: Mapped[dict[str, Any]] = mapped_column(JSONType)
    spec_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16), index=True
    )  # queued|running|succeeded|failed|timed_out
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    deadline_at: Mapped[datetime] = mapped_column(UTCDateTime)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    logs_ref: Mapped[str | None] = mapped_column(String(2048), nullable=True)


class ModelReleaseRecord(Base):
    __tablename__ = "model_releases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"), index=True)
    # One release per deployment approval (idempotency key for the release action).
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approval_requests.id"), unique=True)
    model_name: Mapped[str] = mapped_column(Ident, index=True)
    target: Mapped[str] = mapped_column(Ident)
    adapter: Mapped[str] = mapped_column(String(32))
    from_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    to_ref: Mapped[str] = mapped_column(String(512))
    to_version: Mapped[str] = mapped_column(Ident)
    # released | verified | verification_failed | rolled_back | rollback_failed
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType)


class ModelAliasRecord(Base):
    """Champion pointer maintained by the mock release adapter."""

    __tablename__ = "model_aliases"

    model_name: Mapped[str] = mapped_column(Ident, primary_key=True)
    alias: Mapped[str] = mapped_column(Ident, primary_key=True)
    model_ref: Mapped[str] = mapped_column(String(512))
    model_version: Mapped[str] = mapped_column(Ident)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class NotificationDeliveryRecord(Base):
    __tablename__ = "notification_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # "<incident_id>:<kind>": a replayed workflow step never sends twice.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))  # pending | delivered | failed | skipped
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class PolicyDecisionRecord(Base):
    __tablename__ = "policy_decisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    evaluation_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation_events.id"), unique=True
    )
    incident_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("incidents.id"), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(64))
    baseline_event_id: Mapped[str | None] = mapped_column(Ident, nullable=True)
    severity: Mapped[str] = mapped_column(String(32))
    proposed_action: Mapped[str] = mapped_column(String(64))
    decision_reason: Mapped[str] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = mapped_column(JSONType)
    result_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AuditEventRecord(Base):
    """Append-only at the application layer; PostgreSQL also blocks UPDATE/DELETE."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(AutoIncrementId, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("incidents.id"), nullable=True, index=True
    )
    evaluation_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evaluation_events.id"), nullable=True
    )
    policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_refs: Mapped[dict[str, Any]] = mapped_column(JSONType)
    outcome: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONType)
