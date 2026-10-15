"""Governance workflow nodes.

Rules every node follows:

* Truth lives in the database. Waiting nodes (approvals, jobs) re-read the database
  when woken; the resume value is only a wake-up signal and is never trusted.
* Side effects (approval requests, jobs, releases, notifications, audit) are keyed
  so that re-running a node after a crash or resume never repeats them.
* Before any side effect outside the workflow (creating a job, releasing a model)
  the approval is re-validated: status, expiry, reviewer still authorized, policy
  version and the exact spec digest.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Any

from langgraph.types import interrupt
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ml_sentry.deployment.adapters import make_adapter
from ml_sentry.deployment.base import Previous, ReleaseError, ReleasePlan
from ml_sentry.evaluation.mlflow_adapter import MlflowClient
from ml_sentry.graph.state import STATE_VERSION, GovernanceState, bounded
from ml_sentry.jobs.service import TERMINAL, create_job, get_job
from ml_sentry.metrics.validation import validate_evidence as run_evidence_checks
from ml_sentry.notifications.base import IncidentNotification, Notifier
from ml_sentry.notifications.delivery import deliver_incident
from ml_sentry.policies.engine import (
    ZERO_BASELINE_EPSILON,
    BaselineEvidence,
    PolicyResult,
    evaluate,
)
from ml_sentry.policies.loader import LoadedPolicy
from ml_sentry.schemas.evaluation import EvaluationEvent, event_from_payload, metrics_from_json
from ml_sentry.services.approvals import (
    approval_status,
    check_for_side_effect,
    request_approval,
    spec_digest,
)
from ml_sentry.storage.models import (
    ApprovalRequestRecord,
    AuditEventRecord,
    EvaluationEventRecord,
    IncidentRecord,
    JobRecord,
    ModelReleaseRecord,
    PolicyDecisionRecord,
    utcnow,
)

WORKFLOW_ACTOR = "governance-workflow"
HISTORY_LIMIT = 5


@dataclass
class WorkflowDeps:
    session_factory: sessionmaker[Session]
    policy: LoadedPolicy
    notifier: Notifier
    link_base: str
    reviewer_names: frozenset[str] = field(default_factory=frozenset)
    # Environment kill switches; mock runners/targets don't need them.
    retraining_enabled_env: bool = False
    deployment_enabled_env: bool = False
    mlflow: MlflowClient | None = None


class GovernanceNodes:
    """Node implementations. Subclass to inject faults in tests."""

    def __init__(self, deps: WorkflowDeps) -> None:
        self.deps = deps

    # -- helpers ---------------------------------------------------------------

    @property
    def _sessions(self) -> sessionmaker[Session]:
        return self.deps.session_factory

    def _load(
        self, session: Session, state: GovernanceState
    ) -> tuple[EvaluationEventRecord, PolicyDecisionRecord]:
        record = session.get_one(EvaluationEventRecord, uuid.UUID(state["event_record_id"]))
        decision = session.scalars(
            select(PolicyDecisionRecord).where(
                PolicyDecisionRecord.evaluation_event_id == record.id
            )
        ).one()
        return record, decision

    def _event(self, state: GovernanceState) -> tuple[EvaluationEvent, EvaluationEventRecord]:
        with self._sessions() as session:
            record, _ = self._load(session, state)
        return event_from_payload(record.payload), record

    def _baseline(self, state: GovernanceState) -> BaselineEvidence | None:
        baseline_id = state.get("baseline_event_id")
        if baseline_id is None:
            return None
        return BaselineEvidence(
            event_id=baseline_id,
            model_version=state.get("model_version", ""),
            metrics=state.get("baseline_metrics", {}),
        )

    def _incident_id(self, state: GovernanceState) -> uuid.UUID:
        incident_id = state.get("incident_id")
        if incident_id is None:
            raise RuntimeError("remediation steps require an incident")
        return uuid.UUID(incident_id)

    def _model_policy(self, state: GovernanceState) -> Any:
        return self.deps.policy.policy.models[state["model_name"]]

    def _send(self, state: GovernanceState, kind: str, status: str, summary: str, link: str) -> str:
        incident_id = state.get("incident_id")
        if incident_id is None:
            return "not_applicable"
        notification = IncidentNotification(
            incident_id=incident_id,
            severity=state.get("severity", "unknown"),
            model_name=state.get("model_name", ""),
            model_version=state.get("model_version", ""),
            status=status,
            proposed_action=state.get("proposed_action", ""),
            summary=bounded(summary),
            link=link,
            kind="approval_request" if kind.startswith("approval:") else "incident",
        )
        return deliver_incident(self._sessions, self.deps.notifier, notification, kind=kind)

    # -- evidence and decision -------------------------------------------------

    def collect_evidence(self, state: GovernanceState) -> GovernanceState:
        holdout: dict[str, Any] | None = None
        with self._sessions() as session:
            record, decision = self._load(session, state)
            baseline_metrics: dict[str, float] = {}
            # Use the baseline chosen at ingestion so the workflow re-checks the same evidence.
            if decision.baseline_event_id is not None:
                baseline = session.scalars(
                    select(EvaluationEventRecord).where(
                        EvaluationEventRecord.event_id == decision.baseline_event_id
                    )
                ).one()
                baseline_metrics = {
                    k: v for k, v in metrics_from_json(baseline.metrics).items() if math.isfinite(v)
                }
                holdout = {
                    "baseline_event_id": baseline.event_id,
                    "champion_model_version": baseline.model_version,
                    "dataset_id": baseline.dataset_id,
                    "dataset_version": baseline.dataset_version,
                    "evaluation_protocol_id": baseline.evaluation_protocol_id,
                    "target_name": baseline.target_name,
                    "sample_count": baseline.sample_count,
                    "metrics": baseline_metrics,
                }
        return {
            "state_version": STATE_VERSION,
            "event_id": record.event_id,
            "model_name": record.model_name,
            "model_version": record.model_version,
            "evaluation_run_id": record.payload.get("evaluation_run_id"),
            "role": record.role,
            "dataset_id": record.dataset_id,
            "dataset_version": record.dataset_version,
            "sample_count": record.sample_count,
            "current_metrics": record.metrics,
            "baseline_event_id": decision.baseline_event_id,
            "baseline_metrics": baseline_metrics,
            "holdout": holdout,
            "error_code": None,
            "error_message": None,
            "outcome": None,
            "visited": ["collect_evidence"],
        }

    def validate_evidence(self, state: GovernanceState) -> GovernanceState:
        event, record = self._event(state)
        policy = self.deps.policy.policy
        issues = run_evidence_checks(
            event,
            policy.models.get(event.model_name),
            policy.defaults,
            baseline_available=state.get("baseline_event_id") is not None,
            received_at=record.received_at,
        )
        return {
            "evidence_valid": not issues,
            "validation_errors": [issue.model_dump() for issue in issues],
            "visited": ["validate_evidence"],
        }

    def compare_baseline(self, state: GovernanceState) -> GovernanceState:
        model_policy = self._model_policy(state)
        current = metrics_from_json(state.get("current_metrics", {}))
        baseline = state.get("baseline_metrics", {})
        deltas: dict[str, dict[str, float | None]] = {}
        for name, rule in sorted(model_policy.metrics.items()):
            if name not in baseline or name not in current:
                continue
            delta = current[name] - baseline[name]
            degradation = delta if rule.direction == "minimize" else -delta
            relative = (
                None
                if abs(baseline[name]) <= ZERO_BASELINE_EPSILON
                else degradation / abs(baseline[name])
            )
            deltas[name] = {"absolute_delta": delta, "relative_degradation": relative}
        return {"metric_deltas": deltas, "visited": ["compare_baseline"]}

    def investigate(self, state: GovernanceState) -> GovernanceState:
        """Deterministic context gathering: recent evaluations of the same model.

        (An optional LLM summary could be added here later; it would only ever
        describe evidence and could never change routing or approvals.)
        """
        primary = self._model_policy(state).primary_metric
        with self._sessions() as session:
            rows = session.execute(
                select(EvaluationEventRecord, PolicyDecisionRecord.severity)
                .join(
                    PolicyDecisionRecord,
                    PolicyDecisionRecord.evaluation_event_id == EvaluationEventRecord.id,
                )
                .where(
                    EvaluationEventRecord.model_name == state["model_name"],
                    EvaluationEventRecord.event_id != state["event_id"],
                )
                .order_by(EvaluationEventRecord.received_at.desc())
                .limit(HISTORY_LIMIT)
            ).all()
        history = [
            {
                "event_id": row.event_id,
                "role": row.role,
                "model_version": row.model_version,
                "dataset_version": row.dataset_version,
                "severity": severity,
                primary: row.metrics.get(primary),
            }
            for row, severity in rows
        ]
        recent_bad = sum(1 for h in history if h["severity"] not in ("none", None))
        summary = (
            f"{len(history)} earlier evaluation(s) of {state['model_name']}; "
            f"{recent_bad} of them also breached policy."
        )
        return {
            "history": history,
            "investigation_summary": summary,
            "visited": ["investigate"],
        }

    def evaluate_policy(self, state: GovernanceState) -> GovernanceState:
        event, record = self._event(state)
        result = evaluate(
            event, self.deps.policy, self._baseline(state), received_at=record.received_at
        )
        with self._sessions() as session:
            _, decision = self._load(session, state)
        consistent = result.digest() == decision.result_digest
        update: GovernanceState = {
            "policy_version": result.policy_version,
            "severity": result.severity,
            "findings": [f for r in result.metric_results for f in r.findings],
            "retraining_eligible": result.retraining_eligible,
            "proposed_action": result.proposed_action,
            "decision_reason": result.decision_reason,
            "policy_results": {
                "consistent_with_ingestion": consistent,
                "ingestion_policy_version": decision.policy_version,
                "ingestion_result_digest": decision.result_digest,
                "workflow_result_digest": result.digest(),
                "metric_severities": {r.metric: r.severity for r in result.metric_results},
            },
            "visited": ["evaluate_policy"],
        }
        if not consistent:
            # The policy file (or code) changed between ingestion and this run. Fail closed:
            # tell a human, never remediate on a decision nobody has seen.
            update["error_code"] = "DECISION_MISMATCH"
            update["error_message"] = bounded(
                f"workflow decision differs from the ingestion decision "
                f"(policy {decision.policy_version} -> {result.policy_version})"
            )
            update["proposed_action"] = "notify"
            update["outcome"] = "decision_mismatch"
        return update

    def decide_action(self, state: GovernanceState) -> GovernanceState:
        update: GovernanceState = {"retraining_blocked_reason": None, "visited": ["decide_action"]}
        if state.get("proposed_action") != "request_retraining_approval":
            return update
        reason = self._retraining_blocker(state)
        if reason is not None:
            update["retraining_blocked_reason"] = reason
            update["outcome"] = "retraining_blocked"
        return update

    def _retraining_blocker(self, state: GovernanceState) -> str | None:
        policy = self.deps.policy.policy
        model = self._model_policy(state)
        pipeline = policy.pipelines.get(model.retraining.pipeline_id or "")
        if pipeline is None:
            return "NO_PIPELINE"
        if pipeline.runner != "mock" and not self.deps.retraining_enabled_env:
            return "RETRAINING_DISABLED_BY_ENVIRONMENT"
        if state.get("holdout") is None:
            return "NO_CHAMPION_BASELINE"
        incident_id = self._incident_id(state)
        with self._sessions() as session:
            attempts = session.scalar(
                select(func.count())
                .select_from(JobRecord)
                .where(JobRecord.incident_id == incident_id)
            )
            last_other = session.scalar(
                select(func.max(JobRecord.created_at)).where(
                    JobRecord.model_name == state["model_name"],
                    JobRecord.incident_id != incident_id,
                )
            )
        if (attempts or 0) >= model.retraining.max_attempts_per_incident:
            return "MAX_ATTEMPTS_REACHED"
        if last_other is not None and model.retraining.cooldown_hours > 0:
            hours = (utcnow() - last_other).total_seconds() / 3600
            if hours < model.retraining.cooldown_hours:
                return "COOLDOWN_ACTIVE"
        return None

    # -- gate A: retraining ----------------------------------------------------

    def retraining_spec(self, state: GovernanceState) -> dict[str, Any]:
        """The exact action Gate A approves. Rebuilt identically before acting on it."""
        policy = self.deps.policy.policy
        model = self._model_policy(state)
        pipeline_id = model.retraining.pipeline_id or ""
        pipeline = policy.pipelines[pipeline_id]
        return {
            "gate": "retraining",
            "incident_id": state.get("incident_id"),
            "model_name": state["model_name"],
            "model_version": state["model_version"],
            "pipeline_id": pipeline_id,
            "runner": pipeline.runner,
            "training_dataset": pipeline.training_dataset.model_dump(),
            "config": dict(pipeline.config),
            "timeout_minutes": pipeline.timeout_minutes,
            "holdout": state.get("holdout"),
            "policy_version": self.deps.policy.version,
            "evidence": {
                "event_id": state.get("event_id"),
                "severity": state.get("severity"),
                "findings": state.get("findings", []),
                "current_metrics": state.get("current_metrics", {}),
                "baseline_event_id": state.get("baseline_event_id"),
                "dataset": f"{state.get('dataset_id')}@{state.get('dataset_version')}",
                "sample_count": state.get("sample_count"),
            },
        }

    def request_retraining_approval(self, state: GovernanceState) -> GovernanceState:
        spec = self.retraining_spec(state)
        approval = request_approval(
            self._sessions,
            incident_id=self._incident_id(state),
            gate="retraining",
            spec=spec,
            policy_version=self.deps.policy.version,
            ttl_hours=self.deps.policy.policy.defaults.approval_ttl_hours,
        )
        self._send(
            state,
            "approval:retraining",
            "awaiting_approval",
            f"Retraining approval requested ({spec['pipeline_id']} via {spec['runner']} runner). "
            f"{'; '.join(state.get('findings', []))}. "
            f"Expires {approval.expires_at:%Y-%m-%d %H:%M} UTC.",
            f"{self.deps.link_base}/ui/#approval/{approval.id}",
        )
        return {
            "approval_request_id": str(approval.id),
            "approval_status": approval.status,
            "approval_expires_at": approval.expires_at.isoformat(),
            "visited": ["request_retraining_approval"],
        }

    def await_retraining_approval(self, state: GovernanceState) -> GovernanceState:
        return self._await_approval(state, "retraining")

    def _await_approval(self, state: GovernanceState, gate: str) -> GovernanceState:
        incident_id = self._incident_id(state)
        status = approval_status(self._sessions, incident_id, gate)  # type: ignore[arg-type]
        if status == "pending":
            # Pause until woken (decision, expiry or job event). The resume value is a
            # wake-up only; the next pass re-reads the database.
            interrupt({"waiting_for": f"{gate}_approval", "incident_id": str(incident_id)})
        with self._sessions() as session:
            approval = session.scalars(
                select(ApprovalRequestRecord).where(
                    ApprovalRequestRecord.incident_id == incident_id,
                    ApprovalRequestRecord.gate == gate,
                )
            ).one()
        node = f"await_{gate}_approval"
        if gate == "retraining":
            update: GovernanceState = {
                "approval_status": approval.status,
                "approved_by": approval.decided_by,
                "visited": [node],
            }
            if approval.status in ("rejected", "expired"):
                update["outcome"] = f"retraining_{approval.status}"
        else:
            update = {
                "deployment_approval_status": approval.status,
                "deployment_approved_by": approval.decided_by,
                "visited": [node],
            }
            if approval.status in ("rejected", "expired"):
                update["outcome"] = f"deployment_{approval.status}"
        return update

    def create_training_job(self, state: GovernanceState) -> GovernanceState:
        spec = self.retraining_spec(state)
        if spec["runner"] != "mock" and not self.deps.retraining_enabled_env:
            return self._blocked("RETRAINING_DISABLED_BY_ENVIRONMENT", "create_training_job")
        check = check_for_side_effect(
            self._sessions,
            incident_id=self._incident_id(state),
            gate="retraining",
            expected_spec=spec,
            policy_version=self.deps.policy.version,
            reviewer_names=set(self.deps.reviewer_names),
        )
        if not check.ok or check.approval is None:
            return self._blocked(check.code, "create_training_job")
        job = create_job(
            self._sessions,
            incident_id=self._incident_id(state),
            approval_id=check.approval.id,
            spec=spec,
            spec_digest=spec_digest(spec),
        )
        return {
            "training_job_id": str(job.id),
            "execution_status": "queued",
            "job_status": job.status,
            "visited": ["create_training_job"],
        }

    def _blocked(self, code: str, node: str) -> GovernanceState:
        return {
            "execution_status": "blocked",
            "error_code": code,
            "error_message": f"side effect refused: {code}",
            "outcome": "blocked",
            "visited": [node],
        }

    def wait_for_training_job(self, state: GovernanceState) -> GovernanceState:
        job_id = uuid.UUID(state["training_job_id"] or "")
        job = get_job(self._sessions, job_id)
        if job is None:
            return self._blocked("JOB_MISSING", "wait_for_training_job")
        if job.status not in TERMINAL:
            interrupt({"waiting_for": "training_job", "job_id": str(job_id)})
            job = get_job(self._sessions, job_id)
            if job is None:
                return self._blocked("JOB_MISSING", "wait_for_training_job")
        update: GovernanceState = {"job_status": job.status, "visited": ["wait_for_training_job"]}
        if job.status == "succeeded" and job.result:
            update.update(
                {
                    "candidate_model_ref": job.result.get("candidate_model_ref"),
                    "candidate_model_version": job.result.get("candidate_model_version"),
                    "candidate_run_id": job.result.get("candidate_run_id"),
                    "candidate_evaluation": job.result.get("evaluation"),
                }
            )
        elif job.status in ("failed", "timed_out"):
            update["outcome"] = "training_failed"
            update["error_message"] = bounded(f"training job {job.status}: {job.error_code}")
        return update

    # -- candidate -------------------------------------------------------------

    def _candidate_result(self, state: GovernanceState) -> PolicyResult:
        evaluation = state.get("candidate_evaluation") or {}
        job = get_job(self._sessions, uuid.UUID(state["training_job_id"] or ""))
        finished = job.finished_at if job and job.finished_at else utcnow()
        event = EvaluationEvent.model_validate(
            {
                "schema_version": 1,
                "event_id": f"candidate-{state['training_job_id']}",
                "model_name": state["model_name"],
                "model_version": state.get("candidate_model_version") or "unknown",
                "task_type": "regression",
                "target_name": evaluation.get("target_name"),
                "dataset_id": evaluation.get("dataset_id"),
                "dataset_version": evaluation.get("dataset_version"),
                "evaluation_protocol_id": evaluation.get("evaluation_protocol_id"),
                "evaluation_timestamp": finished,
                "sample_count": evaluation.get("sample_count", 0),
                "metrics": metrics_from_json(evaluation.get("metrics", {})),
            }
        )
        holdout = state.get("holdout") or {}
        champion = BaselineEvidence(
            event_id=holdout.get("baseline_event_id", ""),
            model_version=holdout.get("champion_model_version", ""),
            metrics=holdout.get("metrics", {}),
        )
        return evaluate(event, self.deps.policy, champion, received_at=finished)

    def evaluate_candidate(self, state: GovernanceState) -> GovernanceState:
        evaluation = state.get("candidate_evaluation") or {}
        result = self._candidate_result(state)
        return {
            "candidate_metrics": {
                k: v
                for k, v in metrics_from_json(evaluation.get("metrics", {})).items()
                if math.isfinite(v)
            },
            "acceptance_results": {
                "candidate_policy_result": result.model_dump(mode="json"),
                "candidate_result_digest": result.digest(),
            },
            "visited": ["evaluate_candidate"],
        }

    def check_candidate_gates(self, state: GovernanceState) -> GovernanceState:
        policy = self.deps.policy.policy
        model = self._model_policy(state)
        result = self._candidate_result(state)
        holdout = state.get("holdout") or {}
        evaluation = state.get("candidate_evaluation") or {}
        identity = ("dataset_id", "dataset_version", "evaluation_protocol_id", "target_name")
        primary = next((r for r in result.metric_results if r.metric == model.primary_metric), None)
        improvement = (
            -primary.relative_degradation
            if primary is not None and primary.relative_degradation is not None
            else None
        )
        gates = [
            {
                "gate": "evidence_valid",
                "passed": result.evidence_valid,
                "detail": "; ".join(i.message for i in result.validation_errors) or "ok",
            },
            {
                "gate": "same_holdout",
                "passed": all(evaluation.get(k) == holdout.get(k) for k in identity),
                "detail": f"champion holdout {holdout.get('dataset_id')}@"
                f"{holdout.get('dataset_version')} / {holdout.get('evaluation_protocol_id')}",
            },
            {
                "gate": "no_regression_vs_champion",
                "passed": result.evidence_valid and result.severity == "none",
                "detail": "; ".join(f for r in result.metric_results for f in r.findings)
                or "all metrics within policy relative to the champion",
            },
            {
                "gate": "primary_metric_improvement",
                "passed": improvement is not None
                and improvement >= model.deployment.min_primary_improvement,
                "detail": f"{model.primary_metric} improvement "
                f"{'n/a' if improvement is None else f'{improvement:.2%}'} "
                f"(required >= {model.deployment.min_primary_improvement:.2%})",
            },
        ]
        passed = all(g["passed"] for g in gates)
        target = policy.release_targets.get(model.deployment.target or "")
        blocked: str | None = None
        if not (policy.defaults.deployment_enabled and model.deployment.allowed) or target is None:
            blocked = "DEPLOYMENT_NOT_ALLOWED_BY_POLICY"
        elif target.adapter != "mock" and not self.deps.deployment_enabled_env:
            blocked = "DEPLOYMENT_DISABLED_BY_ENVIRONMENT"
        acceptance = {**(state.get("acceptance_results") or {}), "gates": gates, "passed": passed}
        update: GovernanceState = {
            "acceptance_results": acceptance,
            "deployment_eligible": blocked is None,
            "deployment_blocked_reason": blocked,
            "visited": ["check_candidate_gates"],
        }
        if not passed:
            update["outcome"] = "candidate_rejected"
        elif blocked is not None:
            update["outcome"] = "candidate_ready_deployment_blocked"
        return update

    # -- gate B: deployment ----------------------------------------------------

    def deployment_spec(self, state: GovernanceState) -> dict[str, Any]:
        policy = self.deps.policy.policy
        model = self._model_policy(state)
        target_id = model.deployment.target or ""
        target = policy.release_targets[target_id]
        holdout = state.get("holdout") or {}
        acceptance = state.get("acceptance_results") or {}
        return {
            "gate": "deployment",
            "incident_id": state.get("incident_id"),
            "model_name": state["model_name"],
            "candidate": {
                "model_ref": state.get("candidate_model_ref"),
                "model_version": state.get("candidate_model_version"),
                "run_id": state.get("candidate_run_id"),
                "training_job_id": state.get("training_job_id"),
                "metrics": state.get("candidate_metrics"),
                "evaluation": state.get("candidate_evaluation"),
            },
            "champion": {
                "model_version": holdout.get("champion_model_version"),
                "baseline_event_id": holdout.get("baseline_event_id"),
                "metrics": holdout.get("metrics"),
            },
            "gates": acceptance.get("gates"),
            "release": {
                "target": target_id,
                "adapter": target.adapter,
                "registered_model": target.registered_model,
                "alias": target.alias,
                "verification": "alias/serving check after release; automatic rollback on failure",
            },
            "policy_version": self.deps.policy.version,
        }

    def request_deployment_approval(self, state: GovernanceState) -> GovernanceState:
        spec = self.deployment_spec(state)
        approval = request_approval(
            self._sessions,
            incident_id=self._incident_id(state),
            gate="deployment",
            spec=spec,
            policy_version=self.deps.policy.version,
            ttl_hours=self.deps.policy.policy.defaults.approval_ttl_hours,
        )
        candidate = spec["candidate"]
        self._send(
            state,
            "approval:deployment",
            "awaiting_approval",
            f"Deployment approval requested: candidate {candidate['model_version']} passed all "
            f"gates; release to '{spec['release']['target']}' ({spec['release']['adapter']}). "
            f"Expires {approval.expires_at:%Y-%m-%d %H:%M} UTC.",
            f"{self.deps.link_base}/ui/#approval/{approval.id}",
        )
        return {
            "deployment_approval_id": str(approval.id),
            "deployment_approval_status": approval.status,
            "visited": ["request_deployment_approval"],
        }

    def await_deployment_approval(self, state: GovernanceState) -> GovernanceState:
        return self._await_approval(state, "deployment")

    def _plan(self, spec: dict[str, Any]) -> ReleasePlan:
        target = self.deps.policy.policy.release_targets[spec["release"]["target"]]
        return ReleasePlan(
            model_name=spec["model_name"],
            target=spec["release"]["target"],
            adapter=target.adapter,
            candidate_ref=spec["candidate"]["model_ref"],
            candidate_version=spec["candidate"]["model_version"],
            registered_model=target.registered_model,
            alias=target.alias,
            simulate_verification_failure=target.simulate_verification_failure,
        )

    def release_candidate(self, state: GovernanceState) -> GovernanceState:
        spec = self.deployment_spec(state)
        if spec["release"]["adapter"] != "mock" and not self.deps.deployment_enabled_env:
            return self._blocked("DEPLOYMENT_DISABLED_BY_ENVIRONMENT", "release_candidate")
        check = check_for_side_effect(
            self._sessions,
            incident_id=self._incident_id(state),
            gate="deployment",
            expected_spec=spec,
            policy_version=self.deps.policy.version,
            reviewer_names=set(self.deps.reviewer_names),
        )
        if not check.ok or check.approval is None:
            return self._blocked(check.code, "release_candidate")
        plan = self._plan(spec)
        adapter = make_adapter(plan.adapter, self._sessions, self.deps.mlflow)

        with self._sessions() as session:
            release = session.scalars(
                select(ModelReleaseRecord).where(
                    ModelReleaseRecord.approval_id == check.approval.id
                )
            ).first()
            if release is None:
                try:
                    previous = adapter.current(plan)
                except ReleaseError as exc:
                    return self._blocked(exc.code, "release_candidate")
                release = ModelReleaseRecord(
                    incident_id=self._incident_id(state),
                    approval_id=check.approval.id,
                    model_name=plan.model_name,
                    target=plan.target,
                    adapter=plan.adapter,
                    from_ref=previous.ref,
                    to_ref=plan.candidate_ref,
                    to_version=plan.candidate_version,
                    status="releasing",
                    created_at=utcnow(),
                    updated_at=utcnow(),
                    details={"previous_version": previous.version},
                )
                session.add(release)
                session.commit()
            if release.status == "releasing":
                try:
                    adapter.release(plan)  # idempotent: sets the alias to the candidate
                except ReleaseError as exc:
                    release.status = "release_failed"
                    release.details = {**release.details, "error_code": exc.code}
                    release.updated_at = utcnow()
                    session.commit()
                    return {
                        "release_id": str(release.id),
                        "release_status": "release_failed",
                        "error_code": exc.code,
                        "outcome": "release_failed",
                        "visited": ["release_candidate"],
                    }
                release.status = "released"
                release.updated_at = utcnow()
                session.add(
                    AuditEventRecord(
                        created_at=utcnow(),
                        actor=WORKFLOW_ACTOR,
                        action="release.performed",
                        incident_id=release.incident_id,
                        evaluation_event_id=None,
                        policy_version=self.deps.policy.version,
                        evidence_refs={
                            "approval_id": str(check.approval.id),
                            "spec_digest": check.approval.spec_digest,
                            "release_id": str(release.id),
                        },
                        outcome="released",
                        details={"to_ref": release.to_ref, "from_ref": release.from_ref},
                    )
                )
                session.commit()
            return {
                "release_id": str(release.id),
                "release_status": release.status,
                "visited": ["release_candidate"],
            }

    def verify_release(self, state: GovernanceState) -> GovernanceState:
        spec = self.deployment_spec(state)
        plan = self._plan(spec)
        adapter = make_adapter(plan.adapter, self._sessions, self.deps.mlflow)
        with self._sessions() as session:
            release = session.get_one(ModelReleaseRecord, uuid.UUID(state["release_id"] or ""))
            if release.status in ("verified", "rolled_back", "rollback_failed"):
                status = release.status
            elif adapter.verify(plan):
                status = "verified"
            else:
                previous = Previous(release.from_ref, release.details.get("previous_version"))
                try:
                    adapter.rollback(plan, previous)
                    status = "rolled_back"
                except ReleaseError:
                    status = "rollback_failed"
            if status != release.status:
                release.status = status
                release.updated_at = utcnow()
                session.add(
                    AuditEventRecord(
                        created_at=utcnow(),
                        actor=WORKFLOW_ACTOR,
                        action="release.verified" if status == "verified" else "release.rollback",
                        incident_id=release.incident_id,
                        evaluation_event_id=None,
                        policy_version=self.deps.policy.version,
                        evidence_refs={"release_id": str(release.id)},
                        outcome=status,
                        details={"to_ref": release.to_ref, "from_ref": release.from_ref},
                    )
                )
                session.commit()
        if status == "verified":
            self._register_new_champion(state)
        return {
            "release_status": status,
            "outcome": "released" if status == "verified" else f"release_{status}",
            "visited": ["verify_release"],
        }

    def _register_new_champion(self, state: GovernanceState) -> None:
        """The released candidate's holdout evaluation becomes the new baseline."""
        from ml_sentry.services.ingestion import ingest_event

        evaluation = state.get("candidate_evaluation") or {}
        event = EvaluationEvent.model_validate(
            {
                "schema_version": 1,
                "event_id": f"release-{state['release_id']}",
                "role": "baseline",
                "source": "generic",
                "model_name": state["model_name"],
                "model_version": state.get("candidate_model_version"),
                "evaluation_run_id": state.get("candidate_run_id"),
                "task_type": "regression",
                "target_name": evaluation.get("target_name"),
                "dataset_id": evaluation.get("dataset_id"),
                "dataset_version": evaluation.get("dataset_version"),
                "evaluation_protocol_id": evaluation.get("evaluation_protocol_id"),
                "evaluation_timestamp": utcnow(),
                "sample_count": evaluation.get("sample_count", 0),
                "metrics": metrics_from_json(evaluation.get("metrics", {})),
                "environment_metadata": {"registered_by": "ml-sentry-release"},
            }
        )
        ingest_event(self._sessions, event, self.deps.policy, actor=WORKFLOW_ACTOR)

    # -- outcome ---------------------------------------------------------------

    def notify(self, state: GovernanceState) -> GovernanceState:
        incident_id = state.get("incident_id")
        if incident_id is None:
            return {"notification_status": "not_applicable", "visited": ["notify"]}
        with self._sessions() as session:
            incident = session.get_one(IncidentRecord, uuid.UUID(incident_id))
            summary = incident.summary
        outcome = state.get("outcome")
        reason = state.get("error_message") or state.get("decision_reason", "")
        text = f"{summary} {reason}".strip()
        if outcome:
            text = f"Outcome: {outcome}. {text}"
        status = self._send(
            state,
            "incident",
            "released" if outcome == "released" else "awaiting_review",
            text,
            f"{self.deps.link_base}/ui/#incident/{incident_id}",
        )
        return {"notification_status": status, "visited": ["notify"]}

    def audit_and_close(self, state: GovernanceState) -> GovernanceState:
        incident_id = state.get("incident_id")
        action = state.get("proposed_action", "none")
        outcome = state.get("outcome")
        if outcome is None:
            if state.get("evidence_valid") is not True:
                outcome = "evidence_rejected"
            elif action == "none":
                outcome = "no_action"
            else:
                outcome = "awaiting_human_review"
        incident_status = {"no_action": "closed", "released": "resolved"}.get(
            outcome, "awaiting_review"
        )

        details: dict[str, Any] = {
            "thread_id": state.get("thread_id"),
            "route": [*state.get("visited", []), "audit_and_close"],
            "severity": state.get("severity"),
            "proposed_action": action,
            "notification_status": state.get("notification_status"),
            "approval_status": state.get("approval_status"),
            "training_job_id": state.get("training_job_id"),
            "job_status": state.get("job_status"),
            "acceptance_passed": (state.get("acceptance_results") or {}).get("passed"),
            "deployment_approval_status": state.get("deployment_approval_status"),
            "release_status": state.get("release_status"),
            "error_code": state.get("error_code"),
            "policy_results": state.get("policy_results"),
        }
        with self._sessions() as session:
            record, _ = self._load(session, state)
            existing = session.scalars(
                select(AuditEventRecord).where(
                    AuditEventRecord.evaluation_event_id == record.id,
                    AuditEventRecord.action == "workflow.completed",
                )
            ).first()
            if existing is not None:
                audit_id = existing.id
            else:
                audit = AuditEventRecord(
                    created_at=utcnow(),
                    actor=WORKFLOW_ACTOR,
                    action="workflow.completed",
                    incident_id=uuid.UUID(incident_id) if incident_id else None,
                    evaluation_event_id=record.id,
                    policy_version=state.get("policy_version"),
                    evidence_refs={
                        "event_id": record.event_id,
                        "baseline_event_id": state.get("baseline_event_id"),
                        "dataset": f"{record.dataset_id}@{record.dataset_version}",
                        "approval_request_id": state.get("approval_request_id"),
                        "deployment_approval_id": state.get("deployment_approval_id"),
                        "release_id": state.get("release_id"),
                    },
                    outcome=outcome,
                    details=details,
                )
                session.add(audit)
                if incident_id is not None:
                    incident = session.get_one(IncidentRecord, uuid.UUID(incident_id))
                    incident.status = incident_status
                    incident.updated_at = utcnow()
                session.commit()
                audit_id = audit.id
        return {
            "outcome": outcome,
            "audit_event_ids": [audit_id],
            "visited": ["audit_and_close"],
        }
