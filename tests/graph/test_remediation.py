"""Milestones 3, 5 and 6: approval gates, training jobs, candidate gates, release."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ml_sentry.graph.runtime import WorkflowRunner
from ml_sentry.graph.state import GovernanceState
from ml_sentry.jobs.models import CandidateEvaluation, JobResult
from ml_sentry.jobs.service import claim_job, complete_job, timeout_overdue
from ml_sentry.jobs.worker import Worker
from ml_sentry.policies.loader import LoadedPolicy
from ml_sentry.services.approvals import ApprovalError, decide, expire_overdue
from ml_sentry.storage.models import (
    EvaluationEventRecord,
    IncidentRecord,
    ModelAliasRecord,
    ModelReleaseRecord,
    utcnow,
)
from tests.conftest import NOW, metrics_for_rmse
from tests.graph.conftest import (
    RecordingNotifier,
    approval_for,
    decide_gate,
    ingest,
    jobs_for,
    make_deps,
    policy_with,
)

MODEL = "demo_regressor"


def setup_incident(
    sf: sessionmaker[Session], policy: LoadedPolicy, event_id: str = "crit"
) -> uuid.UUID:
    ingest(sf, policy, event_id=f"{event_id}-base", role="baseline", model_name=MODEL)
    outcome = ingest(
        sf, policy, event_id=event_id, model_name=MODEL, metrics=metrics_for_rmse(31_000)
    )
    assert outcome.decision.proposed_action == "request_retraining_approval"
    assert outcome.incident is not None
    return outcome.incident.id


def make_runner(
    sf: sessionmaker[Session], policy: LoadedPolicy, notifier: Any = None, **kw: Any
) -> WorkflowRunner:
    return WorkflowRunner(
        make_deps(sf, policy, notifier or RecordingNotifier(), **kw), InMemorySaver()
    )


def state_of(runner: WorkflowRunner, incident_id: uuid.UUID) -> GovernanceState:
    config: RunnableConfig = {"configurable": {"thread_id": f"incident-{incident_id}"}}
    return runner.graph.get_state(config).values  # type: ignore[return-value]


def incident(sf: sessionmaker[Session], incident_id: uuid.UUID) -> IncidentRecord:
    with sf() as s:
        return s.get_one(IncidentRecord, incident_id)


def alias(sf: sessionmaker[Session]) -> ModelAliasRecord | None:
    with sf() as s:
        return s.get(ModelAliasRecord, (MODEL, "champion"))


def releases(sf: sessionmaker[Session]) -> list[ModelReleaseRecord]:
    with sf() as s:
        return list(s.scalars(select(ModelReleaseRecord)))


def to_deployment_gate(
    sf: sessionmaker[Session], policy: LoadedPolicy, **runner_kw: Any
) -> tuple[uuid.UUID, WorkflowRunner]:
    incident_id = setup_incident(sf, policy)
    runner = make_runner(sf, policy, **runner_kw)
    assert runner.run_incident(incident_id) == "waiting"
    decide_gate(sf, incident_id, "retraining")
    assert runner.run_incident(incident_id) == "waiting"  # job queued; waiting for it
    Worker(runner).run_once()  # mock job runs, workflow woken
    return incident_id, runner


def test_full_loop_releases_after_two_separate_approvals(
    session_factory: sessionmaker[Session], policy: LoadedPolicy
) -> None:
    sf = session_factory
    notifier = RecordingNotifier()
    incident_id = setup_incident(sf, policy)
    runner = make_runner(sf, policy, notifier)

    # Gate A
    assert runner.run_incident(incident_id) == "waiting"
    assert jobs_for(sf, incident_id) == []
    decide_gate(sf, incident_id, "retraining", reviewer="alice")
    assert runner.run_incident(incident_id) == "waiting"
    (job,) = jobs_for(sf, incident_id)
    assert (job.status, job.runner) == ("queued", "mock")

    # Mock training + candidate gates -> Gate B requested
    stats = Worker(runner).run_once()
    assert stats["mock_jobs_run"] == 1
    state = state_of(runner, incident_id)
    assert state["acceptance_results"]["passed"] is True
    assert [g["gate"] for g in state["acceptance_results"]["gates"]] == [
        "evidence_valid",
        "same_holdout",
        "no_regression_vs_champion",
        "primary_metric_improvement",
    ]
    deployment = approval_for(sf, incident_id, "deployment")
    assert deployment is not None and deployment.status == "pending"
    assert releases(sf) == []
    assert alias(sf) is None

    # Gate B
    decide_gate(sf, incident_id, "deployment", reviewer="bob")
    assert runner.run_incident(incident_id) == "completed"
    state = state_of(runner, incident_id)
    assert state["outcome"] == "released"
    assert state["release_status"] == "verified"
    champion = alias(sf)
    assert champion is not None and champion.model_ref == state["candidate_model_ref"]
    assert incident(sf, incident_id).status == "resolved"
    # The released candidate's holdout evaluation is the new baseline.
    with sf() as s:
        new_baseline = s.scalars(
            select(EvaluationEventRecord).where(
                EvaluationEventRecord.event_id == f"release-{state['release_id']}"
            )
        ).one()
    assert new_baseline.role == "baseline"
    assert new_baseline.model_version == state["candidate_model_version"]
    kinds = [n.kind for n in notifier.sent]
    assert kinds == ["approval_request", "approval_request", "incident"]


def test_rejected_retraining_never_creates_a_job(
    session_factory: sessionmaker[Session], policy: LoadedPolicy
) -> None:
    incident_id = setup_incident(session_factory, policy)
    runner = make_runner(session_factory, policy)
    runner.run_incident(incident_id)
    decide_gate(session_factory, incident_id, "retraining", decision="reject")
    assert runner.run_incident(incident_id) == "completed"
    assert state_of(runner, incident_id)["outcome"] == "retraining_rejected"
    assert jobs_for(session_factory, incident_id) == []
    assert incident(session_factory, incident_id).status == "awaiting_review"


def test_expired_retraining_approval_never_creates_a_job(
    session_factory: sessionmaker[Session], policy: LoadedPolicy
) -> None:
    incident_id = setup_incident(session_factory, policy)
    runner = make_runner(session_factory, policy)
    runner.run_incident(incident_id)
    later = utcnow() + timedelta(hours=25)
    assert expire_overdue(session_factory, now=later) == [incident_id]
    with pytest.raises(ApprovalError, match="already expired"):
        decide_gate(session_factory, incident_id, "retraining")
    assert runner.run_incident(incident_id) == "completed"
    assert state_of(runner, incident_id)["outcome"] == "retraining_expired"
    assert jobs_for(session_factory, incident_id) == []


def test_late_decision_after_expiry_time_is_refused(
    session_factory: sessionmaker[Session], policy: LoadedPolicy
) -> None:
    incident_id = setup_incident(session_factory, policy)
    make_runner(session_factory, policy).run_incident(incident_id)
    approval = approval_for(session_factory, incident_id, "retraining")
    assert approval is not None
    with pytest.raises(ApprovalError) as exc:
        decide(
            session_factory,
            approval.id,
            reviewer="reviewer:alice",
            decision="approve",
            reviewed_spec_digest=approval.spec_digest,
            note=None,
            require_distinct_approvers=False,
            now=approval.expires_at + timedelta(seconds=1),
        )
    assert exc.value.code == "APPROVAL_EXPIRED"


def test_decisions_need_reviewer_exact_digest_and_happen_once(
    session_factory: sessionmaker[Session], policy: LoadedPolicy
) -> None:
    incident_id = setup_incident(session_factory, policy)
    make_runner(session_factory, policy).run_incident(incident_id)
    approval = approval_for(session_factory, incident_id, "retraining")
    assert approval is not None

    def attempt(**kw: Any) -> str:
        args: dict[str, Any] = {
            "reviewer": "reviewer:alice",
            "decision": "approve",
            "reviewed_spec_digest": approval.spec_digest,
            "note": None,
            "require_distinct_approvers": False,
        }
        args.update(kw)
        try:
            decide(session_factory, approval.id, **args)
        except ApprovalError as exc:
            return exc.code
        return "OK"

    assert attempt(reviewer="api-token") == "FORBIDDEN"
    assert attempt(reviewed_spec_digest="0" * 64) == "SPEC_DIGEST_MISMATCH"
    assert attempt() == "OK"
    assert attempt(decision="reject") == "APPROVAL_ALREADY_DECIDED"


def test_candidate_failing_gates_preserves_champion(
    session_factory: sessionmaker[Session],
) -> None:
    worse = policy_with(
        lambda raw: raw["pipelines"]["demo_mock_retrain"]["config"].update(
            simulated_improvement=-0.3
        )
    )
    incident_id, runner = to_deployment_gate(session_factory, worse)
    state = state_of(runner, incident_id)
    assert state["outcome"] == "candidate_rejected"
    failed = {g["gate"] for g in state["acceptance_results"]["gates"] if not g["passed"]}
    assert failed == {"no_regression_vs_champion", "primary_metric_improvement"}
    assert approval_for(session_factory, incident_id, "deployment") is None
    assert releases(session_factory) == []
    assert alias(session_factory) is None  # champion untouched


def test_rejected_deployment_never_releases(
    session_factory: sessionmaker[Session], policy: LoadedPolicy
) -> None:
    incident_id, runner = to_deployment_gate(session_factory, policy)
    decide_gate(session_factory, incident_id, "deployment", decision="reject")
    assert runner.run_incident(incident_id) == "completed"
    assert state_of(runner, incident_id)["outcome"] == "deployment_rejected"
    assert releases(session_factory) == []
    assert alias(session_factory) is None


def test_expired_deployment_approval_never_releases(
    session_factory: sessionmaker[Session], policy: LoadedPolicy
) -> None:
    incident_id, runner = to_deployment_gate(session_factory, policy)
    expire_overdue(session_factory, now=utcnow() + timedelta(hours=25))
    assert runner.run_incident(incident_id) == "completed"
    assert state_of(runner, incident_id)["outcome"] == "deployment_expired"
    assert releases(session_factory) == []


def test_failed_verification_rolls_back_to_previous_champion(
    session_factory: sessionmaker[Session],
) -> None:
    flaky = policy_with(
        lambda raw: raw["release_targets"]["demo_local_alias"].update(
            simulate_verification_failure=True
        )
    )
    with session_factory() as s:
        s.add(
            ModelAliasRecord(
                model_name=MODEL,
                alias="champion",
                model_ref="mock://demo_regressor/1",
                model_version="1",
                updated_at=utcnow(),
            )
        )
        s.commit()
    incident_id, runner = to_deployment_gate(session_factory, flaky)
    decide_gate(session_factory, incident_id, "deployment")
    runner.run_incident(incident_id)
    state = state_of(runner, incident_id)
    assert state["outcome"] == "release_rolled_back"
    (release,) = releases(session_factory)
    assert release.status == "rolled_back"
    champion = alias(session_factory)
    assert champion is not None and champion.model_ref == "mock://demo_regressor/1"
    assert incident(session_factory, incident_id).status == "awaiting_review"


def test_policy_change_after_approval_invalidates_it(
    session_factory: sessionmaker[Session], policy: LoadedPolicy
) -> None:
    incident_id = setup_incident(session_factory, policy)
    saver = InMemorySaver()  # shared: the same durable thread across the restart
    notifier = RecordingNotifier()
    WorkflowRunner(make_deps(session_factory, policy, notifier), saver).run_incident(incident_id)
    decide_gate(session_factory, incident_id, "retraining")
    # Service restarted with an edited policy: same thread, new policy version.
    changed = policy_with(
        lambda raw: raw["pipelines"]["demo_mock_retrain"]["config"].update(
            simulated_improvement=0.5
        )
    )
    runner = WorkflowRunner(make_deps(session_factory, changed, notifier), saver)
    runner.run_incident(incident_id)
    state = state_of(runner, incident_id)
    assert state["error_code"] == "POLICY_CHANGED"
    assert state["execution_status"] == "blocked"
    assert jobs_for(session_factory, incident_id) == []


def test_revoked_reviewer_approval_is_not_acted_on(
    session_factory: sessionmaker[Session], policy: LoadedPolicy
) -> None:
    incident_id = setup_incident(session_factory, policy)
    make_runner(session_factory, policy).run_incident(incident_id)
    decide_gate(session_factory, incident_id, "retraining", reviewer="alice")
    runner = make_runner(session_factory, policy, reviewer_names=frozenset({"bob"}))
    runner.run_incident(incident_id)
    assert state_of(runner, incident_id)["error_code"] == "APPROVER_NOT_AUTHORIZED"
    assert jobs_for(session_factory, incident_id) == []


def test_distinct_approvers_rule(session_factory: sessionmaker[Session]) -> None:
    strict = policy_with(lambda raw: raw["defaults"].update(require_distinct_approvers=True))
    incident_id, _ = to_deployment_gate(session_factory, strict)
    with pytest.raises(ApprovalError) as exc:
        decide_gate(session_factory, incident_id, "deployment", reviewer="alice", distinct=True)
    assert exc.value.code == "DISTINCT_APPROVER_REQUIRED"
    decide_gate(session_factory, incident_id, "deployment", reviewer="bob", distinct=True)


def test_duplicate_event_and_repeated_wakes_create_one_job(
    session_factory: sessionmaker[Session], policy: LoadedPolicy
) -> None:
    incident_id = setup_incident(session_factory, policy)
    # Re-sending the same event is idempotent: no second incident.
    again = ingest(
        session_factory, policy, event_id="crit", model_name=MODEL, metrics=metrics_for_rmse(31_000)
    )
    assert again.created is False and again.incident is not None
    assert again.incident.id == incident_id
    runner = make_runner(session_factory, policy)
    runner.run_incident(incident_id)
    decide_gate(session_factory, incident_id, "retraining")
    for _ in range(3):
        runner.run_incident(incident_id)
    assert len(jobs_for(session_factory, incident_id)) == 1


def test_cooldown_blocks_a_second_retraining_request(
    session_factory: sessionmaker[Session],
) -> None:
    cooled = policy_with(lambda raw: raw["models"][MODEL]["retraining"].update(cooldown_hours=24))
    first = setup_incident(session_factory, cooled, "first")
    runner = make_runner(session_factory, cooled)
    runner.run_incident(first)
    decide_gate(session_factory, first, "retraining")
    runner.run_incident(first)  # job created now
    second = setup_incident(session_factory, cooled, "second")
    runner.run_incident(second)
    state = state_of(runner, second)
    assert state["retraining_blocked_reason"] == "COOLDOWN_ACTIVE"
    assert state["outcome"] == "retraining_blocked"
    assert approval_for(session_factory, second, "retraining") is None


def test_external_pipeline_needs_environment_switch(
    session_factory: sessionmaker[Session],
) -> None:
    external = policy_with(
        lambda raw: raw["pipelines"]["demo_mock_retrain"].update(runner="external")
    )
    incident_id = setup_incident(session_factory, external)
    runner = make_runner(session_factory, external)  # retraining_enabled_env=False
    runner.run_incident(incident_id)
    state = state_of(runner, incident_id)
    assert state["retraining_blocked_reason"] == "RETRAINING_DISABLED_BY_ENVIRONMENT"
    assert approval_for(session_factory, incident_id, "retraining") is None


def test_external_runner_reports_candidate(session_factory: sessionmaker[Session]) -> None:
    external = policy_with(
        lambda raw: raw["pipelines"]["demo_mock_retrain"].update(runner="external")
    )
    incident_id = setup_incident(session_factory, external)
    runner = make_runner(session_factory, external, retraining_enabled_env=True)
    runner.run_incident(incident_id)
    decide_gate(session_factory, incident_id, "retraining")
    runner.run_incident(incident_id)
    (job,) = jobs_for(session_factory, incident_id)
    assert job.runner == "external"
    Worker(runner).run_once()  # the worker never runs external jobs
    assert jobs_for(session_factory, incident_id)[0].status == "queued"

    claim_job(session_factory, job.id, "runner-token", runner="external")
    holdout = job.spec["holdout"]
    better = {k: v * 0.9 for k, v in holdout["metrics"].items() if k in ("rmse", "mae")}
    better["mse"] = better["rmse"] ** 2
    better["r2"] = min(1.0, holdout["metrics"]["r2"] + 0.01)
    complete_job(
        session_factory,
        job.id,
        "runner-token",
        JobResult(
            status="succeeded",
            candidate_model_ref="gs://bucket/models/demo/2",
            candidate_model_version="2",
            evaluation=CandidateEvaluation(
                dataset_id=holdout["dataset_id"],
                dataset_version=holdout["dataset_version"],
                evaluation_protocol_id=holdout["evaluation_protocol_id"],
                target_name=holdout["target_name"],
                sample_count=holdout["sample_count"],
                metrics=better,
            ),
        ),
    )
    assert runner.run_incident(incident_id) == "waiting"  # now at the deployment gate
    assert state_of(runner, incident_id)["acceptance_results"]["passed"] is True


def test_candidate_on_a_different_holdout_is_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    external = policy_with(
        lambda raw: raw["pipelines"]["demo_mock_retrain"].update(runner="external")
    )
    incident_id = setup_incident(session_factory, external)
    runner = make_runner(session_factory, external, retraining_enabled_env=True)
    runner.run_incident(incident_id)
    decide_gate(session_factory, incident_id, "retraining")
    runner.run_incident(incident_id)
    (job,) = jobs_for(session_factory, incident_id)
    claim_job(session_factory, job.id, "runner-token", runner="external")
    holdout = job.spec["holdout"]
    complete_job(
        session_factory,
        job.id,
        "runner-token",
        JobResult(
            status="succeeded",
            candidate_model_ref="gs://bucket/models/demo/2",
            candidate_model_version="2",
            evaluation=CandidateEvaluation(
                dataset_id=holdout["dataset_id"],
                dataset_version="some-other-version",
                evaluation_protocol_id=holdout["evaluation_protocol_id"],
                target_name=holdout["target_name"],
                sample_count=holdout["sample_count"],
                metrics={k: v * 0.5 for k, v in holdout["metrics"].items() if k != "r2"}
                | {"mse": (holdout["metrics"]["rmse"] * 0.5) ** 2, "r2": 0.99},
            ),
        ),
    )
    runner.run_incident(incident_id)
    state = state_of(runner, incident_id)
    assert state["outcome"] == "candidate_rejected"
    gates = {g["gate"]: g["passed"] for g in state["acceptance_results"]["gates"]}
    assert gates["same_holdout"] is False


def test_job_timeout_ends_in_training_failed(
    session_factory: sessionmaker[Session],
) -> None:
    external = policy_with(
        lambda raw: raw["pipelines"]["demo_mock_retrain"].update(runner="external")
    )
    incident_id = setup_incident(session_factory, external)
    runner = make_runner(session_factory, external, retraining_enabled_env=True)
    runner.run_incident(incident_id)
    decide_gate(session_factory, incident_id, "retraining")
    runner.run_incident(incident_id)
    assert timeout_overdue(session_factory, now=utcnow() + timedelta(hours=2)) == [incident_id]
    assert runner.run_incident(incident_id) == "completed"
    assert state_of(runner, incident_id)["outcome"] == "training_failed"
    assert approval_for(session_factory, incident_id, "deployment") is None


def test_lost_wake_up_is_recovered_by_the_worker(
    session_factory: sessionmaker[Session], policy: LoadedPolicy
) -> None:
    incident_id = setup_incident(session_factory, policy)
    runner = make_runner(session_factory, policy)
    runner.run_incident(incident_id)
    decide_gate(session_factory, incident_id, "retraining")  # nobody wakes the workflow
    assert runner.wake_stale_waiting() == {incident_id: "waiting"}
    assert len(jobs_for(session_factory, incident_id)) == 1


def test_lease_prevents_concurrent_drivers(
    session_factory: sessionmaker[Session], policy: LoadedPolicy
) -> None:
    incident_id = setup_incident(session_factory, policy)
    with session_factory() as s:
        record = s.get_one(IncidentRecord, incident_id)
        record.workflow_lease_owner = "other-process"
        record.workflow_lease_until = utcnow() + timedelta(minutes=5)
        s.commit()
    runner = make_runner(session_factory, policy)
    assert runner.run_incident(incident_id) == "leased"
    assert approval_for(session_factory, incident_id, "retraining") is None
    assert NOW  # keep fixture import used
