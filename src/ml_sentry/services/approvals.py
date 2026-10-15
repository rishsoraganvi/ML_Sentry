"""Human approval gates (A: retraining, B: deployment).

An approval authorizes exactly one action, identified by ``spec_digest`` (SHA-256
of the canonical JSON spec the reviewer saw). Any material change to the action,
the policy version or the evidence produces a different digest, so an earlier
approval no longer matches and cannot be used.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from ml_sentry.storage.models import ApprovalRequestRecord, AuditEventRecord, utcnow

Gate = Literal["retraining", "deployment"]
Decision = Literal["approve", "reject"]
WORKFLOW_ACTOR = "governance-workflow"


class ApprovalError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def spec_digest(spec: dict[str, Any]) -> str:
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _audit(
    session: Session,
    approval: ApprovalRequestRecord,
    actor: str,
    action: str,
    outcome: str,
    details: dict[str, Any],
    now: datetime,
) -> None:
    session.add(
        AuditEventRecord(
            created_at=now,
            actor=actor,
            action=action,
            incident_id=approval.incident_id,
            evaluation_event_id=None,
            policy_version=approval.policy_version,
            evidence_refs={
                "approval_id": str(approval.id),
                "gate": approval.gate,
                "spec_digest": approval.spec_digest,
            },
            outcome=outcome,
            details=details,
        )
    )


def request_approval(
    session_factory: sessionmaker[Session],
    *,
    incident_id: uuid.UUID,
    gate: Gate,
    spec: dict[str, Any],
    policy_version: str,
    ttl_hours: float,
    now: datetime | None = None,
) -> ApprovalRequestRecord:
    """Create the incident's approval request for this gate, or return the existing one."""
    now = now or utcnow()
    with session_factory() as session:
        existing = session.scalars(
            select(ApprovalRequestRecord).where(
                ApprovalRequestRecord.incident_id == incident_id,
                ApprovalRequestRecord.gate == gate,
            )
        ).first()
        if existing is not None:
            return existing
        approval = ApprovalRequestRecord(
            incident_id=incident_id,
            gate=gate,
            status="pending",
            spec=spec,
            spec_digest=spec_digest(spec),
            policy_version=policy_version,
            requested_by=WORKFLOW_ACTOR,
            requested_at=now,
            expires_at=now + timedelta(hours=ttl_hours),
        )
        session.add(approval)
        session.flush()
        _audit(session, approval, WORKFLOW_ACTOR, "approval.requested", "pending", {}, now)
        session.commit()
        return approval


def decide(
    session_factory: sessionmaker[Session],
    approval_id: uuid.UUID,
    *,
    reviewer: str,
    decision: Decision,
    reviewed_spec_digest: str,
    note: str | None,
    require_distinct_approvers: bool,
    now: datetime | None = None,
) -> ApprovalRequestRecord:
    """Record a reviewer's decision exactly once."""
    now = now or utcnow()
    if not reviewer.startswith("reviewer:"):
        raise ApprovalError(403, "FORBIDDEN", "only reviewers can decide approvals")
    with session_factory() as session:
        approval = session.get(ApprovalRequestRecord, approval_id)
        if approval is None:
            raise ApprovalError(404, "APPROVAL_NOT_FOUND", f"No approval '{approval_id}'")
        if approval.status != "pending":
            raise ApprovalError(
                409, "APPROVAL_ALREADY_DECIDED", f"approval is already {approval.status}"
            )
        if approval.expires_at <= now:
            raise ApprovalError(409, "APPROVAL_EXPIRED", "approval has expired")
        if reviewed_spec_digest != approval.spec_digest:
            raise ApprovalError(
                409,
                "SPEC_DIGEST_MISMATCH",
                "the spec you reviewed is not the spec awaiting approval; reload and review again",
            )
        if decision == "approve" and approval.gate == "deployment" and require_distinct_approvers:
            first = session.scalars(
                select(ApprovalRequestRecord.decided_by).where(
                    ApprovalRequestRecord.incident_id == approval.incident_id,
                    ApprovalRequestRecord.gate == "retraining",
                )
            ).first()
            if first == reviewer:
                raise ApprovalError(
                    403,
                    "DISTINCT_APPROVER_REQUIRED",
                    "policy requires a different reviewer than the retraining approver",
                )

        status = "approved" if decision == "approve" else "rejected"
        # Conditional update: two concurrent decisions cannot both succeed.
        changed = session.execute(
            update(ApprovalRequestRecord)
            .where(
                ApprovalRequestRecord.id == approval_id,
                ApprovalRequestRecord.status == "pending",
            )
            .values(
                status=status,
                decided_by=reviewer,
                decided_at=now,
                decision_note=(note or "")[:1000] or None,
            )
        ).rowcount  # type: ignore[attr-defined]
        if changed != 1:
            session.rollback()
            raise ApprovalError(
                409, "APPROVAL_ALREADY_DECIDED", "approval was decided concurrently"
            )
        session.refresh(approval)
        _audit(session, approval, reviewer, "approval.decided", status, {"note": note or ""}, now)
        session.commit()
        return approval


def expire_overdue(
    session_factory: sessionmaker[Session], now: datetime | None = None
) -> list[uuid.UUID]:
    """Mark pending approvals past their expiry as expired; return affected incidents."""
    now = now or utcnow()
    incidents: list[uuid.UUID] = []
    with session_factory() as session:
        overdue = session.scalars(
            select(ApprovalRequestRecord).where(
                ApprovalRequestRecord.status == "pending",
                ApprovalRequestRecord.expires_at <= now,
            )
        ).all()
        for approval in overdue:
            changed = session.execute(
                update(ApprovalRequestRecord)
                .where(
                    ApprovalRequestRecord.id == approval.id,
                    ApprovalRequestRecord.status == "pending",
                )
                .values(status="expired", decided_at=now)
            ).rowcount  # type: ignore[attr-defined]
            if changed == 1:
                _audit(session, approval, "system", "approval.expired", "expired", {}, now)
                incidents.append(approval.incident_id)
        session.commit()
    return incidents


@dataclass(frozen=True)
class ApprovalCheck:
    ok: bool
    code: str
    approval: ApprovalRequestRecord | None


def check_for_side_effect(
    session_factory: sessionmaker[Session],
    *,
    incident_id: uuid.UUID,
    gate: Gate,
    expected_spec: dict[str, Any],
    policy_version: str,
    reviewer_names: set[str],
) -> ApprovalCheck:
    """Re-validate an approval immediately before acting on it."""
    with session_factory() as session:
        approval = session.scalars(
            select(ApprovalRequestRecord).where(
                ApprovalRequestRecord.incident_id == incident_id,
                ApprovalRequestRecord.gate == gate,
            )
        ).first()
    if approval is None:
        return ApprovalCheck(False, "APPROVAL_MISSING", None)
    if approval.status != "approved":
        return ApprovalCheck(False, f"APPROVAL_{approval.status.upper()}", approval)
    if approval.decided_at is None or approval.decided_at > approval.expires_at:
        return ApprovalCheck(False, "APPROVAL_EXPIRED", approval)
    decided_by = approval.decided_by or ""
    if not decided_by.startswith("reviewer:") or decided_by[9:] not in reviewer_names:
        # The reviewer's access was revoked since they decided.
        return ApprovalCheck(False, "APPROVER_NOT_AUTHORIZED", approval)
    if approval.policy_version != policy_version:
        return ApprovalCheck(False, "POLICY_CHANGED", approval)
    if approval.spec_digest != spec_digest(expected_spec):
        return ApprovalCheck(False, "SPEC_CHANGED", approval)
    return ApprovalCheck(True, "OK", approval)


def approval_status(
    session_factory: sessionmaker[Session], incident_id: uuid.UUID, gate: Gate
) -> str | None:
    with session_factory() as session:
        return session.scalars(
            select(ApprovalRequestRecord.status).where(
                ApprovalRequestRecord.incident_id == incident_id,
                ApprovalRequestRecord.gate == gate,
            )
        ).first()
