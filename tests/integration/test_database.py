"""Migration and persistence tests.

The PostgreSQL tests run when ML_SENTRY_TEST_DATABASE_URL points at a disposable
database, e.g. the Compose postgres service (see CONTRIBUTING.md).
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from ml_sentry.policies.loader import LoadedPolicy
from ml_sentry.schemas.evaluation import EvaluationEvent
from ml_sentry.services.ingestion import ingest_event
from ml_sentry.storage.database import (
    alembic_config,
    make_engine,
    make_session_factory,
    migration_status,
    upgrade_to_head,
)
from ml_sentry.storage.models import Base, EvaluationEventRecord, IncidentRecord, utcnow
from tests.conftest import make_event, metrics_for_rmse


def assert_models_match_migrations(engine: Engine) -> None:
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"ORM models and migrations differ: {diff}"


def test_sqlite_migrations_match_models(engine: Engine) -> None:
    current, head = migration_status(engine)
    assert current == head == "0003_approvals_jobs_releases"
    assert_models_match_migrations(engine)


def test_upgrade_is_idempotent(engine: Engine) -> None:
    upgrade_to_head(engine)
    assert migration_status(engine)[0] == "0003_approvals_jobs_releases"


@pytest.mark.postgres
def test_postgres_migrations_match_models(pg_engine: Engine) -> None:
    assert_models_match_migrations(pg_engine)


# The incidents table as it existed at revision 0001 (the ORM model is already at head).
_INCIDENTS_0001 = sa.table(
    "incidents",
    *(sa.column(c, sa.Uuid()) for c in ("id", "evaluation_event_id")),
    *(
        sa.column(c, sa.String())
        for c in (
            "model_name",
            "model_version",
            "category",
            "severity",
            "status",
            "proposed_action",
            "summary",
        )
    ),
    *(sa.column(c, sa.DateTime(timezone=True)) for c in ("created_at", "updated_at")),
)


def _workflow_status_after_upgrade_from_0001(engine: Engine, incident_status: str) -> str | None:
    command.downgrade(alembic_config(engine), "base")
    command.upgrade(alembic_config(engine), "0001_initial")
    event_id, incident_id, now = uuid.uuid4(), uuid.uuid4(), utcnow()
    with engine.begin() as conn:
        conn.execute(
            EvaluationEventRecord.__table__.insert().values(
                id=event_id,
                event_id="pre-upgrade",
                source="generic",
                role="monitoring",
                model_name="m",
                model_version="1",
                target_name="t",
                dataset_id="d",
                dataset_version="v",
                evaluation_protocol_id="p",
                sample_count=1,
                evaluation_timestamp=now,
                metrics={},
                payload={},
                payload_digest="x",
                received_at=now,
                processing_status="processed",
                evidence_valid=True,
            )
        )
        conn.execute(
            _INCIDENTS_0001.insert().values(
                id=incident_id,
                evaluation_event_id=event_id,
                model_name="m",
                model_version="1",
                category="performance",
                severity="warning",
                status=incident_status,
                proposed_action="notify",
                summary="s",
                created_at=now,
                updated_at=now,
            )
        )
    upgrade_to_head(engine)
    with make_session_factory(engine)() as s:
        return s.get_one(IncidentRecord, incident_id).workflow_status


@pytest.mark.parametrize(("status", "expected"), [("open", "pending"), ("closed", None)])
def test_upgrade_backfills_open_incidents(engine: Engine, status: str, expected: str) -> None:
    assert _workflow_status_after_upgrade_from_0001(engine, status) == expected


@pytest.mark.postgres
def test_postgres_upgrade_backfills_open_incidents(pg_engine: Engine) -> None:
    assert _workflow_status_after_upgrade_from_0001(pg_engine, "open") == "pending"


@pytest.mark.postgres
def test_postgres_roundtrip_and_append_only_audit(pg_engine: Engine, policy: LoadedPolicy) -> None:
    factory = make_session_factory(pg_engine)
    ingest_event(
        factory,
        EvaluationEvent.model_validate(make_event(event_id="pg-base", role="baseline")),
        policy,
        actor="test",
    )
    nan_metrics = {**metrics_for_rmse(25_000), "rmse": float("nan")}
    outcome = ingest_event(
        factory,
        EvaluationEvent.model_validate(make_event(event_id="pg-nan", metrics=nan_metrics)),
        policy,
        actor="test",
    )
    assert outcome.incident is not None and outcome.incident.category == "evidence"

    # Persistence survives a fresh engine (as after a container restart).
    reopened = make_engine(pg_engine.url.render_as_string(hide_password=False))
    with reopened.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM evaluation_events")) == 2
        assert (
            conn.scalar(
                text("SELECT metrics->>'rmse' FROM evaluation_events WHERE event_id='pg-nan'")
            )
            == "NaN"
        )
    reopened.dispose()

    for statement in ("UPDATE audit_events SET outcome = 'tampered'", "DELETE FROM audit_events"):
        with pytest.raises(DBAPIError, match="append-only"), pg_engine.begin() as conn:
            conn.execute(text(statement))
