"""Idempotent, retried, persisted notification delivery."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ml_sentry.notifications.base import IncidentNotification, NotificationError, Notifier
from ml_sentry.storage.models import NotificationDeliveryRecord, utcnow

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


def deliver_incident(
    session_factory: sessionmaker[Session],
    notifier: Notifier,
    notification: IncidentNotification,
    *,
    kind: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    backoff_seconds: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Send at most once per (incident, kind) and return the final delivery status.

    Never raises for delivery problems: the status is persisted and returned so the
    caller can record it without changing any governance decision.
    """
    kind = kind or notification.kind
    key = f"{notification.incident_id}:{kind}"
    with session_factory() as session:
        record = session.scalars(
            select(NotificationDeliveryRecord).where(
                NotificationDeliveryRecord.idempotency_key == key
            )
        ).first()
        if record is not None and record.status in ("delivered", "skipped"):
            return record.status
        if record is None:
            now = utcnow()
            record = NotificationDeliveryRecord(
                idempotency_key=key,
                incident_id=uuid.UUID(notification.incident_id),
                provider=notifier.name,
                kind=kind,
                status="pending",
                attempts=0,
                created_at=now,
                updated_at=now,
            )
            session.add(record)
            try:
                session.commit()
            except IntegrityError:
                # Another runner created it concurrently; let that runner deliver.
                session.rollback()
                return "pending"

        if notifier.name == "none":
            record.status = "skipped"
        else:
            for attempt in range(1, max_attempts + 1):
                record.attempts += 1
                try:
                    notifier.send_incident(notification)
                except NotificationError as exc:
                    record.error_code = exc.code
                except Exception as exc:  # a provider bug must not break the workflow
                    record.error_code = f"UNEXPECTED_{type(exc).__name__}"[:64]
                else:
                    record.status = "delivered"
                    record.error_code = None
                    record.delivered_at = utcnow()
                    break
                if attempt < max_attempts:
                    sleep(backoff_seconds * 2 ** (attempt - 1))
            else:
                record.status = "failed"
                logger.error(
                    "notification %s via %s failed after %d attempts (%s)",
                    key,
                    notifier.name,
                    record.attempts,
                    record.error_code,
                )
        record.updated_at = utcnow()
        session.commit()
        return record.status
