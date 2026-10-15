"""End-to-end graph runs on SQLite with the in-memory checkpointer."""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.orm import Session, sessionmaker

from ml_sentry.graph.builder import build_graph
from ml_sentry.graph.nodes import GovernanceNodes
from ml_sentry.graph.runtime import WorkflowRunner
from ml_sentry.graph.state import GovernanceState
from ml_sentry.policies.loader import LoadedPolicy
from ml_sentry.storage.models import IncidentRecord
from tests.conftest import metrics_for_rmse
from tests.graph.conftest import (
    RecordingNotifier,
    approval_for,
    audit_actions,
    critical,
    deliveries,
    ingest,
    ingest_with_baseline,
    jobs_for,
    make_deps,
)

TRAINING_NODES = {"request_retraining_approval", "create_training_job"}


def final_state(runner: WorkflowRunner, incident_id: uuid.UUID) -> GovernanceState:
    config: RunnableConfig = {"configurable": {"thread_id": f"incident-{incident_id}"}}
    return runner.graph.get_state(config).values  # type: ignore[return-value]


def incident(sf: sessionmaker[Session], incident_id: uuid.UUID) -> IncidentRecord:
    with sf() as s:
        return s.get_one(IncidentRecord, incident_id)


def test_healthy_evaluation_terminates_without_remediation(
    session_factory: sessionmaker[Session], policy: LoadedPolicy, notifier: RecordingNotifier
) -> None:
    outcome = ingest_with_baseline(
        session_factory, policy, event_id="ok", metrics=metrics_for_rmse(25_100)
    )
    assert outcome.incident is None  # healthy events never start a workflow from ingestion
    graph = build_graph(
        GovernanceNodes(make_deps(session_factory, policy, notifier)), InMemorySaver()
    )
    state = graph.invoke(
        {"thread_id": "event-ok", "incident_id": None, "event_record_id": str(outcome.event.id)},
        {"configurable": {"thread_id": "event-ok"}},
    )
    assert state["visited"] == [
        "collect_evidence",
        "validate_evidence",
        "compare_baseline",
        "investigate",
        "evaluate_policy",
        "decide_action",
        "audit_and_close",
    ]
    assert state["outcome"] == "no_action"
    assert "1 earlier evaluation" in state["investigation_summary"]
    assert notifier.sent == []
    assert state["metric_deltas"]["rmse"]["relative_degradation"] is not None


def test_warning_creates_incident_and_notification(
    session_factory: sessionmaker[Session], policy: LoadedPolicy, notifier: RecordingNotifier
) -> None:
    outcome = ingest_with_baseline(
        session_factory, policy, event_id="warn", metrics=metrics_for_rmse(28_500)
    )
    assert outcome.incident is not None
    runner = WorkflowRunner(make_deps(session_factory, policy, notifier), InMemorySaver())
    assert runner.run_incident(outcome.incident.id) == "completed"

    state = final_state(runner, outcome.incident.id)
    assert state["severity"] == "warning"
    assert state["visited"][-2:] == ["notify", "audit_and_close"]
    assert state["notification_status"] == "delivered"
    assert len(notifier.sent) == 1
    sent = notifier.sent[0]
    assert sent.incident_id == str(outcome.incident.id)
    assert sent.link.endswith(f"/ui/#incident/{outcome.incident.id}")
    record = incident(session_factory, outcome.incident.id)
    assert (record.status, record.workflow_status) == ("awaiting_review", "completed")


def test_invalid_evidence_never_reaches_training_even_when_policy_allows_it(
    session_factory: sessionmaker[Session],
    permissive_policy: LoadedPolicy,
    notifier: RecordingNotifier,
) -> None:
    # Huge regression *and* too few samples: tempting, but the evidence is invalid.
    outcome = ingest_with_baseline(session_factory, permissive_policy, **critical(sample_count=5))
    assert outcome.incident is not None
    runner = WorkflowRunner(
        make_deps(session_factory, permissive_policy, notifier), InMemorySaver()
    )
    runner.run_incident(outcome.incident.id)
    state = final_state(runner, outcome.incident.id)

    assert state["evidence_valid"] is False
    assert not TRAINING_NODES & set(state["visited"])
    assert "compare_baseline" not in state["visited"]
    assert state["outcome"] == "evidence_rejected"
    assert len(notifier.sent) == 1


def test_critical_with_retraining_allowed_waits_for_human_approval(
    session_factory: sessionmaker[Session],
    permissive_policy: LoadedPolicy,
    notifier: RecordingNotifier,
) -> None:
    outcome = ingest_with_baseline(session_factory, permissive_policy, **critical())
    assert outcome.decision.proposed_action == "request_retraining_approval"
    assert outcome.incident is not None
    runner = WorkflowRunner(
        make_deps(session_factory, permissive_policy, notifier), InMemorySaver()
    )
    assert runner.run_incident(outcome.incident.id) == "waiting"
    state = final_state(runner, outcome.incident.id)

    # Paused inside await_retraining_approval (a node records itself once it completes).
    assert state["visited"][-2:] == ["decide_action", "request_retraining_approval"]
    config: RunnableConfig = {"configurable": {"thread_id": f"incident-{outcome.incident.id}"}}
    assert runner.graph.get_state(config).next == ("await_retraining_approval",)
    assert "create_training_job" not in state["visited"]
    approval = approval_for(session_factory, outcome.incident.id, "retraining")
    assert approval is not None and approval.status == "pending"
    assert jobs_for(session_factory, outcome.incident.id) == []
    assert incident(session_factory, outcome.incident.id).workflow_status == "waiting"
    # The reviewer is told, and the notification links to the approval page.
    assert notifier.sent[0].kind == "approval_request"
    assert notifier.sent[0].link.endswith(f"/ui/#approval/{approval.id}")

    # Waking it again without a decision keeps it waiting and creates nothing.
    assert runner.run_incident(outcome.incident.id) == "waiting"
    assert jobs_for(session_factory, outcome.incident.id) == []


def test_forged_approval_in_state_still_cannot_create_a_job(
    session_factory: sessionmaker[Session],
    permissive_policy: LoadedPolicy,
    notifier: RecordingNotifier,
) -> None:
    class ForgedApproval(GovernanceNodes):
        def await_retraining_approval(self, state: GovernanceState) -> GovernanceState:
            # Claims "approved" without any human decision in the database.
            return {"approval_status": "approved", "visited": ["await_retraining_approval"]}

    outcome = ingest_with_baseline(session_factory, permissive_policy, **critical())
    assert outcome.incident is not None
    deps = make_deps(session_factory, permissive_policy, notifier)
    runner = WorkflowRunner(deps, InMemorySaver(), nodes=ForgedApproval(deps))
    runner.run_incident(outcome.incident.id)
    state = final_state(runner, outcome.incident.id)
    # The job node re-validates against the database and refuses.
    assert state["visited"][-3:] == ["create_training_job", "notify", "audit_and_close"]
    assert state["execution_status"] == "blocked"
    assert state["error_code"] == "APPROVAL_PENDING"
    assert jobs_for(session_factory, outcome.incident.id) == []


def test_side_effect_nodes_only_reachable_through_approval_waits(
    session_factory: sessionmaker[Session], policy: LoadedPolicy, notifier: RecordingNotifier
) -> None:
    graph = build_graph(GovernanceNodes(make_deps(session_factory, policy, notifier)), None)
    edges = graph.get_graph().edges

    def sources(target: str) -> set[str]:
        return {e.source for e in edges if e.target == target}

    assert sources("create_training_job") == {"await_retraining_approval"}
    assert sources("request_retraining_approval") == {"decide_action"}
    assert sources("release_candidate") == {"await_deployment_approval"}
    assert sources("request_deployment_approval") == {"check_candidate_gates"}


def test_each_incident_gets_its_own_thread(
    session_factory: sessionmaker[Session], policy: LoadedPolicy, notifier: RecordingNotifier
) -> None:
    ingest(session_factory, policy, event_id="base-1", role="baseline")
    a = ingest(session_factory, policy, event_id="a", metrics=metrics_for_rmse(28_500))
    b = ingest(session_factory, policy, event_id="b", metrics=metrics_for_rmse(28_600))
    assert a.incident is not None and b.incident is not None
    assert a.incident.workflow_thread_id == f"incident-{a.incident.id}"
    assert a.incident.workflow_thread_id != b.incident.workflow_thread_id
    runner = WorkflowRunner(make_deps(session_factory, policy, notifier), InMemorySaver())
    assert set(runner.resume_pending().values()) == {"completed"}
    assert final_state(runner, a.incident.id)["event_id"] == "a"
    assert final_state(runner, b.incident.id)["event_id"] == "b"


def test_resume_after_restart_continues_same_thread(
    session_factory: sessionmaker[Session], policy: LoadedPolicy, notifier: RecordingNotifier
) -> None:
    outcome = ingest_with_baseline(session_factory, policy, **critical())
    assert outcome.incident is not None
    saver = InMemorySaver()  # stands in for the durable store; see the postgres test
    deps = make_deps(session_factory, policy, notifier)

    first = WorkflowRunner(deps, saver, interrupt_before=["notify"])
    assert first.run_incident(outcome.incident.id) == "paused"
    assert incident(session_factory, outcome.incident.id).workflow_status == "running"
    assert notifier.sent == []

    # "Restart": a brand-new runner and graph, recovering from the persisted status.
    second = WorkflowRunner(deps, saver)
    assert second.resume_pending() == {outcome.incident.id: "completed"}
    state = final_state(second, outcome.incident.id)
    assert state["visited"].count("collect_evidence") == 1  # resumed, not restarted
    assert state["visited"][-2:] == ["notify", "audit_and_close"]
    assert len(notifier.sent) == 1
    assert audit_actions(session_factory, outcome.incident.id)[-3:] == [
        "workflow.run:started",
        "workflow.run:resumed",
        "workflow.completed:awaiting_human_review",
    ]


def test_crash_mid_workflow_resumes_without_duplicate_side_effects(
    session_factory: sessionmaker[Session], policy: LoadedPolicy, notifier: RecordingNotifier
) -> None:
    class CrashOnce(GovernanceNodes):
        crashed = False

        def audit_and_close(self, state: GovernanceState) -> GovernanceState:
            if not CrashOnce.crashed:
                CrashOnce.crashed = True
                raise RuntimeError("simulated crash")
            return super().audit_and_close(state)

    outcome = ingest_with_baseline(session_factory, policy, **critical())
    assert outcome.incident is not None
    saver = InMemorySaver()
    deps = make_deps(session_factory, policy, notifier)
    crashing = WorkflowRunner(deps, saver, nodes=CrashOnce(deps))
    assert crashing.run_incident(outcome.incident.id) == "failed"
    record = incident(session_factory, outcome.incident.id)
    assert record.workflow_status == "failed"
    assert record.workflow_error == "RuntimeError: simulated crash"

    retry = WorkflowRunner(deps, saver, nodes=CrashOnce(deps))
    assert retry.run_incident(outcome.incident.id) == "completed"
    state = final_state(retry, outcome.incident.id)
    assert state["visited"].count("notify") == 1
    assert len(notifier.sent) == 1
    assert len(deliveries(session_factory, outcome.incident.id)) == 1


def test_completed_workflow_is_not_rerun(
    session_factory: sessionmaker[Session], policy: LoadedPolicy, notifier: RecordingNotifier
) -> None:
    outcome = ingest_with_baseline(session_factory, policy, **critical())
    assert outcome.incident is not None
    runner = WorkflowRunner(make_deps(session_factory, policy, notifier), InMemorySaver())
    runner.run_incident(outcome.incident.id)
    assert runner.run_incident(outcome.incident.id) == "completed"
    assert len(notifier.sent) == 1
    assert runner.resume_pending() == {}


def test_notification_failure_does_not_change_the_decision(
    session_factory: sessionmaker[Session], policy: LoadedPolicy
) -> None:
    failing = RecordingNotifier(fail_times=10)
    outcome = ingest_with_baseline(session_factory, policy, **critical())
    assert outcome.incident is not None
    deps = make_deps(session_factory, policy, failing)
    runner = WorkflowRunner(deps, InMemorySaver())
    assert runner.run_incident(outcome.incident.id) == "completed"
    state = final_state(runner, outcome.incident.id)
    assert state["notification_status"] == "failed"
    assert state["severity"] == "critical"
    assert state["proposed_action"] == "investigate"
    assert state["outcome"] == "awaiting_human_review"
    assert "approval_status" not in state
    (delivery,) = deliveries(session_factory, outcome.incident.id)
    assert (delivery.status, delivery.attempts, delivery.error_code) == (
        "failed",
        3,
        "PROVIDER_DOWN",
    )


def test_policy_change_after_ingestion_fails_closed(
    session_factory: sessionmaker[Session],
    policy: LoadedPolicy,
    permissive_policy: LoadedPolicy,
    notifier: RecordingNotifier,
) -> None:
    # Ingested under the strict policy; the workflow runs after the policy was relaxed.
    outcome = ingest_with_baseline(session_factory, policy, **critical())
    assert outcome.incident is not None
    runner = WorkflowRunner(
        make_deps(session_factory, permissive_policy, notifier), InMemorySaver()
    )
    runner.run_incident(outcome.incident.id)
    state = final_state(runner, outcome.incident.id)
    assert state["error_code"] == "DECISION_MISMATCH"
    assert state["policy_results"]["consistent_with_ingestion"] is False
    assert not TRAINING_NODES & set(state["visited"])
    assert state["outcome"] == "decision_mismatch"


def test_state_holds_only_compact_references(
    session_factory: sessionmaker[Session], policy: LoadedPolicy, notifier: RecordingNotifier
) -> None:
    outcome = ingest_with_baseline(session_factory, policy, **critical())
    assert outcome.incident is not None
    runner = WorkflowRunner(make_deps(session_factory, policy, notifier), InMemorySaver())
    runner.run_incident(outcome.incident.id)
    state: dict[str, Any] = dict(final_state(runner, outcome.incident.id))
    assert "payload" not in state
    assert "environment_metadata" not in str(state)
    assert state["state_version"] == 2
