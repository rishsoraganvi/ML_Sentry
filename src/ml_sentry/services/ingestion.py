"""Evaluation ingestion: persist the event, evaluate policy, open incidents, audit.

Everything for one event happens in a single transaction, so an event is either
fully recorded with its decision and audit trail or not recorded at all.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ml_sentry.graph.state import thread_id_for
from ml_sentry.policies.engine import PolicyResult, baseline_from_metrics, evaluate
from ml_sentry.policies.loader import LoadedPolicy
from ml_sentry.schemas.evaluation import EvaluationEvent, json_safe_metrics
from ml_sentry.storage.models import (
    AuditEventRecord,
    EvaluationEventRecord,
    IncidentRecord,
    PolicyDecisionRecord,
    utcnow,
)


class EventConflictError(Exception):
    """The event_id was already used for a different payload."""


@dataclass(frozen=True)
class IngestOutcome:
    event: EvaluationEventRecord
    decision: PolicyDecisionRecord
    incident: IncidentRecord | None
    created: bool


def canonical_payload(event: EvaluationEvent) -> tuple[dict[str, Any], str]:
    payload = event.model_dump(mode="json")
    payload["metrics"] = json_safe_metrics(event.metrics)
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    return payload, digest


def _find_baseline(session: Session, event: EvaluationEvent) -> EvaluationEventRecord | None:
    model, target, dataset, dataset_version, protocol = event.comparability_key()
    stmt = (
        select(EvaluationEventRecord)
        .where(
            EvaluationEventRecord.role == "baseline",
            EvaluationEventRecord.evidence_valid.is_(True),
            EvaluationEventRecord.model_name == model,
            EvaluationEventRecord.target_name == target,
            EvaluationEventRecord.dataset_id == dataset,
            EvaluationEventRecord.dataset_version == dataset_version,
            EvaluationEventRecord.evaluation_protocol_id == protocol,
        )
        .order_by(
            EvaluationEventRecord.evaluation_timestamp.desc(),
            EvaluationEventRecord.received_at.desc(),
        )
        .limit(1)
    )
    return session.scalars(stmt).first()


def _existing(session: Session, event_id: str) -> IngestOutcome | None:
    record = session.scalars(
        select(EvaluationEventRecord).where(EvaluationEventRecord.event_id == event_id)
    ).first()
    if record is None:
        return None
    decision = session.scalars(
        select(PolicyDecisionRecord).where(PolicyDecisionRecord.evaluation_event_id == record.id)
    ).one()
    incident = session.get(IncidentRecord, decision.incident_id) if decision.incident_id else None
    return IngestOutcome(event=record, decision=decision, incident=incident, created=False)


def _summary(result: PolicyResult) -> str:
    if not result.evidence_valid:
        details = "; ".join(issue.message for issue in result.validation_errors)
        return (
            f"Invalid evaluation evidence for {result.model_name} {result.model_version}: {details}"
        )
    findings = [f for r in result.metric_results for f in r.findings]
    return (
        f"{result.severity.capitalize()} regression for {result.model_name} "
        f"{result.model_version}: {'; '.join(findings)}"
    )


def _record(
    session: Session,
    event: EvaluationEvent,
    loaded: LoadedPolicy,
    actor: str,
    received_at: datetime,
) -> IngestOutcome:
    payload, digest = canonical_payload(event)
    baseline_row = _find_baseline(session, event) if event.role == "monitoring" else None
    baseline = (
        baseline_from_metrics(
            baseline_row.event_id, baseline_row.model_version, baseline_row.metrics
        )
        if baseline_row
        else None
    )
    result = evaluate(event, loaded, baseline, received_at=received_at)

    record = EvaluationEventRecord(
        event_id=event.event_id,
        source=event.source,
        role=event.role,
        model_name=event.model_name,
        model_version=event.model_version,
        target_name=event.target_name,
        dataset_id=event.dataset_id,
        dataset_version=event.dataset_version,
        evaluation_protocol_id=event.evaluation_protocol_id,
        sample_count=event.sample_count,
        evaluation_timestamp=event.evaluation_timestamp,
        metrics=payload["metrics"],
        payload=payload,
        payload_digest=digest,
        received_at=received_at,
        processing_status="processed",
        evidence_valid=result.evidence_valid,
    )
    session.add(record)
    session.flush()

    incident: IncidentRecord | None = None
    if result.severity != "none":
        incident = IncidentRecord(
            evaluation_event_id=record.id,
            model_name=event.model_name,
            model_version=event.model_version,
            category="performance" if result.evidence_valid else "evidence",
            severity=result.severity,
            status="open",
            proposed_action=result.proposed_action,
            summary=_summary(result)[:4000],
            created_at=received_at,
            updated_at=received_at,
            workflow_status="pending",
        )
        session.add(incident)
        session.flush()
        incident.workflow_thread_id = thread_id_for(incident.id)

    result_json = result.model_dump(mode="json")
    decision = PolicyDecisionRecord(
        evaluation_event_id=record.id,
        incident_id=incident.id if incident else None,
        policy_version=result.policy_version,
        baseline_event_id=result.baseline_event_id,
        severity=result.severity,
        proposed_action=result.proposed_action,
        decision_reason=result.decision_reason,
        result=result_json,
        result_digest=result.digest(),
        created_at=received_at,
    )
    session.add(decision)

    evidence_refs = {
        "event_id": event.event_id,
        "payload_digest": digest,
        "baseline_event_id": result.baseline_event_id,
        "dataset": f"{event.dataset_id}@{event.dataset_version}",
        "evaluation_protocol_id": event.evaluation_protocol_id,
        "evaluation_run_id": event.evaluation_run_id,
    }
    audit = [
        ("evaluation.received", "recorded", {"role": event.role, "source": event.source}),
        (
            "policy.evaluated",
            result.severity,
            {"proposed_action": result.proposed_action, "result_digest": result.digest()},
        ),
    ]
    if incident is not None:
        audit.append(("incident.opened", "open", {"category": incident.category}))
    for action, outcome, details in audit:
        session.add(
            AuditEventRecord(
                created_at=received_at,
                actor=actor,
                action=action,
                incident_id=incident.id if incident else None,
                evaluation_event_id=record.id,
                policy_version=result.policy_version,
                evidence_refs=evidence_refs,
                outcome=outcome,
                details=details,
            )
        )
    session.flush()
    return IngestOutcome(event=record, decision=decision, incident=incident, created=True)


def ingest_event(
    session_factory: sessionmaker[Session],
    event: EvaluationEvent,
    loaded: LoadedPolicy,
    *,
    actor: str,
    received_at: datetime | None = None,
) -> IngestOutcome:
    """Record an event idempotently.

    Re-sending the same event_id with an identical payload returns the original
    outcome; a different payload under the same event_id raises EventConflictError.
    """
    received_at = received_at or utcnow()
    _, digest = canonical_payload(event)

    for _attempt in range(2):
        with session_factory() as session:
            existing = _existing(session, event.event_id)
            if existing is not None:
                if existing.event.payload_digest != digest:
                    raise EventConflictError(event.event_id)
                return existing
            try:
                outcome = _record(session, event, loaded, actor, received_at)
                session.commit()
                return outcome
            except IntegrityError:
                # A concurrent request stored the same event_id first; re-read it.
                session.rollback()
    raise RuntimeError(f"could not record event {event.event_id}")
