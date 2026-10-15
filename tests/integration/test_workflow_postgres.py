"""Durable LangGraph checkpoints on PostgreSQL (needs ML_SENTRY_TEST_DATABASE_URL)."""

from __future__ import annotations

import pytest
from langchain_core.runnables import RunnableConfig
from sqlalchemy import Engine, text

from ml_sentry.graph.runtime import WorkflowRunner, open_checkpointer
from ml_sentry.jobs.worker import Worker
from ml_sentry.policies.loader import LoadedPolicy
from ml_sentry.storage.database import make_session_factory
from ml_sentry.storage.models import IncidentRecord, ModelAliasRecord
from tests.conftest import metrics_for_rmse
from tests.graph.conftest import (
    RecordingNotifier,
    critical,
    decide_gate,
    ingest,
    ingest_with_baseline,
    jobs_for,
    make_deps,
)

pytestmark = pytest.mark.postgres


def _url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def test_two_gate_loop_survives_restarts_between_every_step(
    pg_engine: Engine, policy: LoadedPolicy
) -> None:
    """Each phase runs in a fresh 'process' (new pool, graph and runner)."""
    sf = make_session_factory(pg_engine)
    ingest(sf, policy, event_id="pg-b", role="baseline", model_name="demo_regressor")
    outcome = ingest(
        sf, policy, event_id="pg-c", model_name="demo_regressor", metrics=metrics_for_rmse(31_000)
    )
    assert outcome.incident is not None
    incident_id = outcome.incident.id
    notifier = RecordingNotifier()

    def process() -> tuple[WorkflowRunner, object]:
        cm = open_checkpointer(_url(pg_engine))
        saver = cm.__enter__()
        return WorkflowRunner(make_deps(sf, policy, notifier), saver), cm

    runner, cm = process()
    assert runner.run_incident(incident_id) == "waiting"
    cm.__exit__(None, None, None)  # type: ignore[attr-defined]

    decide_gate(sf, incident_id, "retraining", reviewer="alice")
    runner, cm = process()  # e.g. the worker after a restart
    assert Worker(runner).run_once()["workflows_woken"] == 1  # lost wake-up recovered
    assert len(jobs_for(sf, incident_id)) == 1
    cm.__exit__(None, None, None)  # type: ignore[attr-defined]

    runner, cm = process()
    Worker(runner).run_once()  # mock job, then wake -> deployment gate
    cm.__exit__(None, None, None)  # type: ignore[attr-defined]

    decide_gate(sf, incident_id, "deployment", reviewer="bob")
    runner, cm = process()
    assert runner.run_incident(incident_id) == "completed"
    cm.__exit__(None, None, None)  # type: ignore[attr-defined]

    with sf() as s:
        assert s.get_one(IncidentRecord, incident_id).status == "resolved"
        assert s.get(ModelAliasRecord, ("demo_regressor", "champion")) is not None
    assert len(jobs_for(sf, incident_id)) == 1
    assert [n.kind for n in notifier.sent] == ["approval_request", "approval_request", "incident"]


def test_checkpointer_setup_is_idempotent(pg_engine: Engine) -> None:
    for _ in range(2):
        with open_checkpointer(_url(pg_engine)):
            pass
    with pg_engine.connect() as conn:
        assert conn.scalar(text("SELECT to_regclass('public.checkpoints') IS NOT NULL"))


def test_resume_after_restart_from_postgres_checkpoint(
    pg_engine: Engine, policy: LoadedPolicy
) -> None:
    session_factory = make_session_factory(pg_engine)
    outcome = ingest_with_baseline(session_factory, policy, **critical())
    assert outcome.incident is not None
    thread_id = f"incident-{outcome.incident.id}"
    notifier = RecordingNotifier()
    deps = make_deps(session_factory, policy, notifier)

    # Process 1: runs until just before notify, then the whole process "dies"
    # (runner, graph and connection pool are discarded).
    with open_checkpointer(_url(pg_engine)) as saver:
        assert (
            WorkflowRunner(deps, saver, interrupt_before=["notify"]).run_incident(
                outcome.incident.id
            )
            == "paused"
        )
    with pg_engine.connect() as conn:
        stored = conn.scalar(
            text("SELECT count(*) FROM checkpoints WHERE thread_id = :t"), {"t": thread_id}
        )
    assert stored and stored > 0
    assert notifier.sent == []

    # Process 2: fresh pool, fresh graph, same thread id -> continues where it stopped.
    with open_checkpointer(_url(pg_engine)) as saver:
        runner = WorkflowRunner(deps, saver)
        assert runner.resume_pending() == {outcome.incident.id: "completed"}
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        state = runner.graph.get_state(config).values

    assert state["visited"].count("collect_evidence") == 1
    assert "investigate" in state["visited"]
    assert state["visited"][-2:] == ["notify", "audit_and_close"]
    assert state["outcome"] == "awaiting_human_review"
    assert len(notifier.sent) == 1
    with session_factory() as s:
        record = s.get_one(IncidentRecord, outcome.incident.id)
        assert (record.status, record.workflow_status) == ("awaiting_review", "completed")
