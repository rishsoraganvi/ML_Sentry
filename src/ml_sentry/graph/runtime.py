"""Run governance workflows: checkpointer setup, per-incident threads, leases, recovery."""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading
import uuid
from collections.abc import Iterator, Sequence
from datetime import timedelta
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import and_, exists, make_url, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from ml_sentry.evaluation.mlflow_adapter import MlflowClient
from ml_sentry.graph.builder import GovernanceGraph, build_graph
from ml_sentry.graph.nodes import GovernanceNodes, WorkflowDeps
from ml_sentry.graph.state import STATE_VERSION, GovernanceState, bounded, thread_id_for
from ml_sentry.notifications.base import Notifier
from ml_sentry.notifications.console import make_notifier
from ml_sentry.policies.loader import LoadedPolicy
from ml_sentry.settings import Settings
from ml_sentry.storage.models import (
    ApprovalRequestRecord,
    AuditEventRecord,
    IncidentRecord,
    JobRecord,
    utcnow,
)

logger = logging.getLogger(__name__)

RESUMABLE = ("pending", "running")
LEASE = timedelta(minutes=5)
RECURSION_LIMIT = 60


def build_deps(
    settings: Settings,
    session_factory: sessionmaker[Session],
    policy: LoadedPolicy,
    *,
    notifier: Notifier | None = None,
) -> WorkflowDeps:
    mlflow = MlflowClient.from_settings(settings) if settings.mlflow_tracking_uri else None
    return WorkflowDeps(
        session_factory=session_factory,
        policy=policy,
        notifier=notifier or make_notifier(settings),
        link_base=settings.link_base,
        reviewer_names=frozenset(settings.reviewer_hashes),
        retraining_enabled_env=settings.retraining_enabled,
        deployment_enabled_env=settings.deployment_enabled,
        mlflow=mlflow,
    )


def psycopg_conninfo(sqlalchemy_url: str) -> str:
    """Convert ``postgresql+psycopg://`` to the plain URL psycopg expects."""
    return (
        make_url(sqlalchemy_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )


@contextlib.contextmanager
def open_checkpointer(database_url: str) -> Iterator[BaseCheckpointSaver[Any]]:
    """PostgreSQL checkpointer for real deployments; in-memory for SQLite (tests/dev only)."""
    if not database_url.startswith("postgresql"):
        logger.warning("Non-PostgreSQL database: workflow checkpoints are kept in memory only")
        yield InMemorySaver()
        return
    with ConnectionPool(
        psycopg_conninfo(database_url),
        min_size=1,
        max_size=5,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=True,
    ) as pool:
        saver = PostgresSaver(pool)  # type: ignore[arg-type]
        saver.setup()  # idempotent; creates/migrates LangGraph's own checkpoint tables
        yield saver


class WorkflowRunner:
    """Drives one graph thread per incident.

    Any process (API or worker) may drive any incident, but only while it holds the
    incident's lease, so two processes never advance the same thread concurrently.
    """

    def __init__(
        self,
        deps: WorkflowDeps,
        checkpointer: BaseCheckpointSaver[Any],
        *,
        nodes: GovernanceNodes | None = None,
        interrupt_before: Sequence[str] | None = None,
        owner: str | None = None,
    ) -> None:
        self.deps = deps
        self.graph: GovernanceGraph = build_graph(
            nodes or GovernanceNodes(deps), checkpointer, interrupt_before=interrupt_before
        )
        self.owner = owner or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, thread_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(thread_id, threading.Lock())

    @property
    def _sessions(self) -> sessionmaker[Session]:
        return self.deps.session_factory

    def _acquire_lease(self, incident_id: uuid.UUID) -> bool:
        now = utcnow()
        with self._sessions() as session:
            changed = session.execute(
                update(IncidentRecord)
                .where(
                    IncidentRecord.id == incident_id,
                    or_(
                        IncidentRecord.workflow_lease_until.is_(None),
                        IncidentRecord.workflow_lease_until < now,
                        IncidentRecord.workflow_lease_owner == self.owner,
                    ),
                )
                .values(workflow_lease_owner=self.owner, workflow_lease_until=now + LEASE)
            ).rowcount  # type: ignore[attr-defined]
            session.commit()
            return bool(changed == 1)

    def _finish(self, incident_id: uuid.UUID, status: str, error: str | None = None) -> None:
        with self._sessions() as session:
            incident = session.get_one(IncidentRecord, incident_id)
            incident.workflow_status = status
            incident.workflow_error = error
            incident.updated_at = utcnow()
            if incident.workflow_lease_owner == self.owner:
                incident.workflow_lease_owner = None
                incident.workflow_lease_until = None
            session.commit()

    def config(self, incident_id: uuid.UUID) -> RunnableConfig:
        return {
            "configurable": {"thread_id": thread_id_for(incident_id)},
            "recursion_limit": RECURSION_LIMIT,
        }

    def run_incident(self, incident_id: uuid.UUID) -> str:
        """Start, resume or wake the incident's workflow; return its workflow status."""
        thread_id = thread_id_for(incident_id)
        lock = self._lock(thread_id)
        if not lock.acquire(blocking=False):
            return "running"  # another thread of this process is driving it
        try:
            with self._sessions() as session:
                incident = session.get_one(IncidentRecord, incident_id)
                if incident.workflow_status == "completed":
                    return "completed"
                if incident.workflow_thread_id not in (None, thread_id):
                    raise RuntimeError("incident is bound to a different workflow thread")
            if not self._acquire_lease(incident_id):
                return "leased"  # another process is driving it right now
            with self._sessions() as session:
                incident = session.get_one(IncidentRecord, incident_id)
                run_kind = "started" if incident.workflow_status in (None, "pending") else "resumed"
                incident.workflow_thread_id = thread_id
                incident.workflow_status = "running"
                session.add(
                    AuditEventRecord(
                        created_at=utcnow(),
                        actor="governance-workflow",
                        action="workflow.run",
                        incident_id=incident.id,
                        evaluation_event_id=incident.evaluation_event_id,
                        policy_version=self.deps.policy.version,
                        evidence_refs={"thread_id": thread_id, "runner": self.owner},
                        outcome=run_kind,
                        details={},
                    )
                )
                session.commit()
                event_record_id = str(incident.evaluation_event_id)

            config = self.config(incident_id)
            snapshot = self.graph.get_state(config)
            if not snapshot.values:
                initial: GovernanceState = {
                    "state_version": STATE_VERSION,
                    "thread_id": thread_id,
                    "incident_id": str(incident_id),
                    "event_record_id": event_record_id,
                }
                self.graph.invoke(initial, config, durability="sync")
            elif snapshot.interrupts:
                # Wake-up only: the waiting node re-reads approvals/jobs from the database.
                wake: Command[Any] = Command(resume={"wake": utcnow().isoformat()})
                self.graph.invoke(wake, config, durability="sync")
            elif snapshot.next:
                self.graph.invoke(None, config, durability="sync")

            after = self.graph.get_state(config)
            if after.interrupts:
                status = "waiting"
            elif after.next:
                status = "running"  # stopped by interrupt_before (tests)
            else:
                status = "completed"
            self._finish(incident_id, status)
            return "paused" if status == "running" else status
        except Exception as exc:
            logger.exception("workflow %s failed", thread_id)
            self._finish(incident_id, "failed", bounded(f"{type(exc).__name__}: {exc}"))
            return "failed"
        finally:
            lock.release()

    def resume_pending(self) -> dict[uuid.UUID, str]:
        """Start pending workflows and resume ones interrupted by a crash or restart."""
        now = utcnow()
        with self._sessions() as session:
            ids = list(
                session.scalars(
                    select(IncidentRecord.id)
                    .where(
                        IncidentRecord.workflow_status.in_(RESUMABLE),
                        or_(
                            IncidentRecord.workflow_lease_until.is_(None),
                            IncidentRecord.workflow_lease_until < now,
                        ),
                    )
                    .order_by(IncidentRecord.created_at)
                )
            )
        return {incident_id: self.run_incident(incident_id) for incident_id in ids}

    def wake_stale_waiting(self) -> dict[uuid.UUID, str]:
        """Safety net for lost wake-ups: wake waiting workflows whose approval was decided
        or whose job finished after the workflow last ran."""
        decided = exists().where(
            and_(
                ApprovalRequestRecord.incident_id == IncidentRecord.id,
                ApprovalRequestRecord.status != "pending",
                ApprovalRequestRecord.decided_at > IncidentRecord.updated_at,
            )
        )
        finished = exists().where(
            and_(
                JobRecord.incident_id == IncidentRecord.id,
                JobRecord.finished_at.is_not(None),
                JobRecord.finished_at > IncidentRecord.updated_at,
            )
        )
        with self._sessions() as session:
            ids = list(
                session.scalars(
                    select(IncidentRecord.id).where(
                        IncidentRecord.workflow_status == "waiting", or_(decided, finished)
                    )
                )
            )
        return {incident_id: self.run_incident(incident_id) for incident_id in ids}
