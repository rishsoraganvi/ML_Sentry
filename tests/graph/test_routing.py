"""Exhaustive checks of the conditional edges, including forged/corrupted states."""

import itertools

import pytest

from ml_sentry.graph.routing import (
    route_after_decision,
    route_after_retraining_approval,
    route_after_validation,
)
from ml_sentry.graph.state import GovernanceState

SEVERITIES = ["none", "warning", "critical", "invalid_evidence", None]
ACTIONS = ["none", "notify", "investigate", "request_retraining_approval", None]
BOOLS = [True, False, None]
ERRORS = [None, "DECISION_MISMATCH"]
APPROVALS = [None, "unavailable", "rejected", "expired", "approved"]


def all_states() -> list[GovernanceState]:
    states: list[GovernanceState] = []
    for valid, sev, action, eligible, err, approval in itertools.product(
        BOOLS, SEVERITIES, ACTIONS, BOOLS, ERRORS, APPROVALS
    ):
        state: GovernanceState = {"error_code": err}
        if valid is not None:
            state["evidence_valid"] = valid
        if sev is not None:
            state["severity"] = sev
        if action is not None:
            state["proposed_action"] = action
        if eligible is not None:
            state["retraining_eligible"] = eligible
        if approval is not None:
            state["approval_status"] = approval
        states.append(state)
    return states


def test_invalid_evidence_never_routes_toward_training() -> None:
    for state in all_states():
        if state.get("evidence_valid") is not True:
            assert route_after_decision(state) == "notify"
            assert route_after_retraining_approval(state) == "notify"


def test_training_only_with_valid_critical_eligible_and_approved() -> None:
    for state in all_states():
        allowed = (
            state.get("evidence_valid") is True
            and state.get("severity") == "critical"
            and state.get("retraining_eligible") is True
            and state.get("error_code") is None
            and state.get("approval_status") == "approved"
        )
        assert (route_after_retraining_approval(state) == "create_training_job") is allowed


def test_approval_requested_only_for_eligible_critical_request() -> None:
    for state in all_states():
        expected = (
            state.get("evidence_valid") is True
            and state.get("error_code") is None
            and state.get("proposed_action") == "request_retraining_approval"
            and state.get("severity") == "critical"
            and state.get("retraining_eligible") is True
        )
        assert (route_after_decision(state) == "request_retraining_approval") is expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"evidence_valid": True, "proposed_action": "none"}, "audit_and_close"),
        ({"evidence_valid": True, "proposed_action": "notify"}, "notify"),
        ({"evidence_valid": True, "proposed_action": "investigate"}, "notify"),
        (
            {"evidence_valid": True, "proposed_action": "none", "error_code": "DECISION_MISMATCH"},
            "notify",
        ),
        ({"evidence_valid": False, "proposed_action": "none"}, "notify"),
    ],
)
def test_route_after_decision_cases(state: GovernanceState, expected: str) -> None:
    assert route_after_decision(state) == expected


def test_route_after_validation() -> None:
    assert route_after_validation({"evidence_valid": True}) == "compare_baseline"
    assert route_after_validation({"evidence_valid": False}) == "evaluate_policy"
    assert route_after_validation({}) == "evaluate_policy"
