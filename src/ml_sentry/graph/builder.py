"""Assemble the governance StateGraph."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ml_sentry.graph import routing
from ml_sentry.graph.nodes import GovernanceNodes
from ml_sentry.graph.state import GovernanceState

GovernanceGraph = CompiledStateGraph[GovernanceState, Any, GovernanceState, GovernanceState]

NODES = (
    "collect_evidence",
    "validate_evidence",
    "compare_baseline",
    "investigate",
    "evaluate_policy",
    "decide_action",
    "request_retraining_approval",
    "await_retraining_approval",
    "create_training_job",
    "wait_for_training_job",
    "evaluate_candidate",
    "check_candidate_gates",
    "request_deployment_approval",
    "await_deployment_approval",
    "release_candidate",
    "verify_release",
    "notify",
    "audit_and_close",
)


def build_graph(
    nodes: GovernanceNodes,
    checkpointer: BaseCheckpointSaver[Any] | None,
    *,
    interrupt_before: Sequence[str] | None = None,
) -> GovernanceGraph:
    graph = StateGraph(GovernanceState)
    for name in NODES:
        graph.add_node(name, getattr(nodes, name))

    graph.add_edge(START, "collect_evidence")
    graph.add_edge("collect_evidence", "validate_evidence")
    graph.add_conditional_edges("validate_evidence", routing.route_after_validation)
    graph.add_edge("compare_baseline", "investigate")
    graph.add_edge("investigate", "evaluate_policy")
    graph.add_edge("evaluate_policy", "decide_action")
    graph.add_conditional_edges("decide_action", routing.route_after_decision)

    # Gate A: retraining approval -> job
    graph.add_edge("request_retraining_approval", "await_retraining_approval")
    graph.add_conditional_edges(
        "await_retraining_approval", routing.route_after_retraining_approval
    )
    graph.add_conditional_edges("create_training_job", routing.route_after_job_creation)
    graph.add_conditional_edges("wait_for_training_job", routing.route_after_job)

    # Candidate vs champion
    graph.add_edge("evaluate_candidate", "check_candidate_gates")
    graph.add_conditional_edges("check_candidate_gates", routing.route_after_gates)

    # Gate B: deployment approval -> release -> verification
    graph.add_edge("request_deployment_approval", "await_deployment_approval")
    graph.add_conditional_edges(
        "await_deployment_approval", routing.route_after_deployment_approval
    )
    graph.add_conditional_edges("release_candidate", routing.route_after_release)
    graph.add_edge("verify_release", "notify")

    graph.add_edge("notify", "audit_and_close")
    graph.add_edge("audit_and_close", END)

    return graph.compile(
        checkpointer=checkpointer,
        interrupt_before=list(interrupt_before) if interrupt_before else None,
    )
