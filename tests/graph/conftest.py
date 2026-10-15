from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ml_sentry.graph.nodes import WorkflowDeps
from ml_sentry.notifications.base import IncidentNotification, NotificationError
from ml_sentry.policies.loader import LoadedPolicy, parse_policy
from ml_sentry.schemas.evaluation import EvaluationEvent
from ml_sentry.services.approvals import decide
from ml_sentry.services.ingestion import IngestOutcome, ingest_event
from ml_sentry.storage.models import (
    ApprovalRequestRecord,
    AuditEventRecord,
    JobRecord,
    NotificationDeliveryRecord,
)
from tests.conftest import EXAMPLE_POLICY, NOW, make_event, metrics_for_rmse


class RecordingNotifier:
    name = "recording"

    def __init__(self, fail_times: int = 0) -> None:
        self.sent: list[IncidentNotification] = []
        self.fail_times = fail_times

    def send_incident(self, notification: IncidentNotification) -> None:
        if self.fail_times:
            self.fail_times -= 1
            raise NotificationError("PROVIDER_DOWN")
        self.sent.append(notification)


def policy_with(mutate: Any = None, name: str = "variant.yaml") -> LoadedPolicy:
    raw = yaml.safe_load(EXAMPLE_POLICY.read_bytes())
    if mutate is not None:
        mutate(raw)
    return parse_policy(yaml.safe_dump(raw).encode(), Path(name))


@pytest.fixture
def permissive_policy() -> LoadedPolicy:
    """Retraining (mock pipeline) allowed for house_price_regressor too."""

    def mutate(raw: dict[str, Any]) -> None:
        retraining = raw["models"]["house_price_regressor"]["retraining"]
        retraining.update(allowed=True, pipeline_id="demo_mock_retrain", cooldown_hours=0)

    return policy_with(mutate, "permissive.yaml")


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


REVIEWERS = frozenset({"alice", "bob"})


def make_deps(
    session_factory: sessionmaker[Session],
    policy: LoadedPolicy,
    notifier: Any,
    **overrides: Any,
) -> WorkflowDeps:
    values: dict[str, Any] = {
        "session_factory": session_factory,
        "policy": policy,
        "notifier": notifier,
        "link_base": "http://127.0.0.1:8000",
        "reviewer_names": REVIEWERS,
    }
    values.update(overrides)
    return WorkflowDeps(**values)


def ingest(
    session_factory: sessionmaker[Session], policy: LoadedPolicy, **overrides: Any
) -> IngestOutcome:
    return ingest_event(
        session_factory,
        EvaluationEvent.model_validate(make_event(**overrides)),
        policy,
        actor="test",
        received_at=NOW,
    )


def ingest_with_baseline(
    session_factory: sessionmaker[Session], policy: LoadedPolicy, **overrides: Any
) -> IngestOutcome:
    ingest(session_factory, policy, event_id="base-1", role="baseline")
    return ingest(session_factory, policy, **overrides)


def critical(**overrides: Any) -> dict[str, Any]:
    return {"event_id": "evt-crit", "metrics": metrics_for_rmse(31_000), **overrides}


def audit_actions(session_factory: sessionmaker[Session], incident_id: uuid.UUID) -> list[str]:
    with session_factory() as s:
        return [
            f"{a.action}:{a.outcome}"
            for a in s.scalars(
                select(AuditEventRecord)
                .where(AuditEventRecord.incident_id == incident_id)
                .order_by(AuditEventRecord.id)
            )
        ]


def approval_for(
    session_factory: sessionmaker[Session], incident_id: uuid.UUID, gate: str
) -> ApprovalRequestRecord | None:
    with session_factory() as s:
        return s.scalars(
            select(ApprovalRequestRecord).where(
                ApprovalRequestRecord.incident_id == incident_id,
                ApprovalRequestRecord.gate == gate,
            )
        ).first()


def decide_gate(
    session_factory: sessionmaker[Session],
    incident_id: uuid.UUID,
    gate: str,
    decision: str = "approve",
    reviewer: str = "alice",
    **kwargs: Any,
) -> ApprovalRequestRecord:
    approval = approval_for(session_factory, incident_id, gate)
    assert approval is not None, f"no {gate} approval"
    return decide(
        session_factory,
        approval.id,
        reviewer=f"reviewer:{reviewer}",
        decision=decision,  # type: ignore[arg-type]
        reviewed_spec_digest=approval.spec_digest,
        note="test",
        require_distinct_approvers=kwargs.get("distinct", False),
    )


def jobs_for(session_factory: sessionmaker[Session], incident_id: uuid.UUID) -> list[JobRecord]:
    with session_factory() as s:
        return list(s.scalars(select(JobRecord).where(JobRecord.incident_id == incident_id)))


def deliveries(
    session_factory: sessionmaker[Session], incident_id: uuid.UUID
) -> list[NotificationDeliveryRecord]:
    with session_factory() as s:
        return list(
            s.scalars(
                select(NotificationDeliveryRecord).where(
                    NotificationDeliveryRecord.incident_id == incident_id
                )
            )
        )
