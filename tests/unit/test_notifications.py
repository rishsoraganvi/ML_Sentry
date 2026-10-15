import logging
import uuid

import pytest
from sqlalchemy.orm import Session, sessionmaker

from ml_sentry.notifications.base import IncidentNotification
from ml_sentry.notifications.console import ConsoleNotifier, NullNotifier, make_notifier
from ml_sentry.notifications.delivery import deliver_incident
from ml_sentry.settings import Settings
from tests.graph.conftest import RecordingNotifier, deliveries, ingest_with_baseline
from tests.graph.conftest import critical as critical_event


@pytest.fixture
def notification(session_factory: sessionmaker[Session], policy: object) -> IncidentNotification:
    outcome = ingest_with_baseline(session_factory, policy, **critical_event())  # type: ignore[arg-type]
    assert outcome.incident is not None
    return IncidentNotification(
        incident_id=str(outcome.incident.id),
        severity="critical",
        model_name="house_price_regressor",
        model_version="1",
        status="awaiting_review",
        proposed_action="investigate",
        summary="rmse degraded",
        link="http://127.0.0.1:8000/api/v1/incidents/x",
    )


def test_render_text_contains_required_fields(notification: IncidentNotification) -> None:
    text = notification.render_text()
    for part in (notification.incident_id, "CRITICAL", "house_price_regressor 1", "Details:"):
        assert part in text


def test_console_notifier_logs(
    notification: IncidentNotification, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="ml_sentry.notifications"):
        ConsoleNotifier().send_incident(notification)
    assert notification.incident_id in caplog.text


def test_make_notifier(settings: Settings) -> None:
    assert isinstance(make_notifier(settings), ConsoleNotifier)
    none = settings.model_copy(update={"notification_provider": "none"})
    assert isinstance(make_notifier(none), NullNotifier)
    broken = settings.model_copy(update={"notification_provider": "slack"})  # no URL set
    with pytest.raises(ValueError):
        make_notifier(broken)


def test_delivery_is_idempotent(
    session_factory: sessionmaker[Session], notification: IncidentNotification
) -> None:
    notifier = RecordingNotifier()
    assert deliver_incident(session_factory, notifier, notification) == "delivered"
    assert deliver_incident(session_factory, notifier, notification) == "delivered"
    assert len(notifier.sent) == 1


def test_delivery_retries_with_backoff_then_succeeds(
    session_factory: sessionmaker[Session], notification: IncidentNotification
) -> None:
    sleeps: list[float] = []
    notifier = RecordingNotifier(fail_times=2)
    status = deliver_incident(
        session_factory, notifier, notification, backoff_seconds=1, sleep=sleeps.append
    )
    assert status == "delivered"
    assert sleeps == [1, 2]
    (record,) = deliveries(session_factory, uuid.UUID(notification.incident_id))
    assert (record.attempts, record.error_code) == (3, None)


def test_failed_delivery_is_retried_on_a_later_run(
    session_factory: sessionmaker[Session], notification: IncidentNotification
) -> None:
    notifier = RecordingNotifier(fail_times=3)
    assert deliver_incident(session_factory, notifier, notification, sleep=lambda _: None) == (
        "failed"
    )
    assert deliver_incident(session_factory, notifier, notification, sleep=lambda _: None) == (
        "delivered"
    )


def test_unexpected_provider_error_is_contained(
    session_factory: sessionmaker[Session], notification: IncidentNotification
) -> None:
    class Broken:
        name = "broken"

        def send_incident(self, notification: IncidentNotification) -> None:
            raise KeyError("boom")

    status = deliver_incident(session_factory, Broken(), notification, sleep=lambda _: None)
    assert status == "failed"


def test_null_provider_skips(
    session_factory: sessionmaker[Session], notification: IncidentNotification
) -> None:
    assert deliver_incident(session_factory, NullNotifier(), notification) == "skipped"
