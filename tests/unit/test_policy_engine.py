from pathlib import Path

import pytest
import yaml

from ml_sentry.policies.engine import BaselineEvidence, PolicyResult, evaluate
from ml_sentry.policies.loader import LoadedPolicy, parse_policy
from ml_sentry.schemas.evaluation import EvaluationEvent
from tests.conftest import EXAMPLE_POLICY, NOW, make_event, metrics_for_rmse

BASELINE = BaselineEvidence(event_id="base-1", model_version="1", metrics=metrics_for_rmse(25_000))


def run(
    policy: LoadedPolicy, baseline: BaselineEvidence | None = BASELINE, **overrides: object
) -> PolicyResult:
    return evaluate(
        EvaluationEvent.model_validate(make_event(**overrides)), policy, baseline, received_at=NOW
    )


def codes(result: PolicyResult) -> set[str]:
    return {issue.code for issue in result.validation_errors}


def test_healthy_evaluation_needs_no_action(policy: LoadedPolicy) -> None:
    result = run(policy, metrics=metrics_for_rmse(25_500))
    assert result.evidence_valid
    assert result.severity == "none"
    assert result.proposed_action == "none"
    assert result.baseline_event_id == "base-1"
    assert result.policy_version == policy.version


def test_warning_on_relative_increase(policy: LoadedPolicy) -> None:
    result = run(policy, metrics=metrics_for_rmse(28_500))  # +14 %
    assert result.severity == "warning"
    assert result.proposed_action == "notify"
    rmse = next(r for r in result.metric_results if r.metric == "rmse")
    assert rmse.role == "primary"
    assert rmse.relative_degradation == pytest.approx(0.14)
    assert rmse.absolute_delta == pytest.approx(3_500)


def test_critical_on_relative_increase_with_retraining_disabled(policy: LoadedPolicy) -> None:
    result = run(policy, metrics=metrics_for_rmse(31_000))  # +24 %
    assert result.severity == "critical"
    assert result.retraining_eligible is False
    assert result.proposed_action == "investigate"


def test_exactly_at_threshold_is_not_a_breach(policy: LoadedPolicy) -> None:
    result = run(policy, metrics=metrics_for_rmse(27_500))  # exactly +10 %
    assert result.severity == "none"


def test_absolute_limit_is_critical_without_baseline_comparison(policy: LoadedPolicy) -> None:
    result = run(policy, metrics=metrics_for_rmse(26_000, mae_ratio=1.0))  # mae 26 000 < 35 000
    assert result.severity == "none"
    result = run(policy, metrics={**metrics_for_rmse(26_000), "r2": 0.65})
    assert result.severity == "critical"
    assert any("minimum_acceptable" in f for r in result.metric_results for f in r.findings)


def test_maximize_metric_relative_decrease(policy: LoadedPolicy) -> None:
    result = run(policy, metrics=metrics_for_rmse(25_000, r2=0.84))  # r2 0.90 -> 0.84 = -6.7 %
    r2 = next(r for r in result.metric_results if r.metric == "r2")
    assert r2.severity == "warning"
    assert r2.relative_degradation == pytest.approx(0.0666667, rel=1e-4)


def test_negative_r2_is_valid_evidence(policy: LoadedPolicy) -> None:
    result = run(policy, metrics=metrics_for_rmse(25_000, r2=-0.3))
    assert result.evidence_valid
    assert result.severity == "critical"  # below minimum_acceptable


def test_zero_baseline_does_not_divide_by_zero(policy: LoadedPolicy) -> None:
    zero = BaselineEvidence(event_id="b0", model_version="1", metrics={**metrics_for_rmse(0.0)})
    result = run(policy, baseline=zero, metrics=metrics_for_rmse(10.0))
    rmse = next(r for r in result.metric_results if r.metric == "rmse")
    assert rmse.relative_degradation is None
    assert any("relative change is undefined" in f for f in rmse.findings)
    assert result.severity == "none"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"metrics": {**metrics_for_rmse(25_000), "rmse": float("nan")}}, "METRIC_NOT_FINITE"),
        ({"metrics": {**metrics_for_rmse(25_000), "mse": float("inf")}}, "METRIC_NOT_FINITE"),
        ({"metrics": {**metrics_for_rmse(25_000), "mae": -1.0}}, "METRIC_OUT_OF_RANGE"),
        ({"metrics": {**metrics_for_rmse(25_000), "r2": 1.2}}, "METRIC_OUT_OF_RANGE"),
        ({"metrics": {"rmse": 25_000.0, "mae": 19_000.0}}, "METRIC_MISSING"),
        ({"metrics": {**metrics_for_rmse(25_000), "mse": 1.0}}, "METRIC_INCONSISTENT"),
        ({"metrics": {**metrics_for_rmse(25_000), "mae": 30_000.0}}, "METRIC_INCONSISTENT"),
        ({"sample_count": 0}, "EMPTY_EVALUATION"),
        ({"sample_count": 99}, "INSUFFICIENT_SAMPLES"),
        ({"evaluation_timestamp": "2026-03-02T12:00:00Z"}, "TIMESTAMP_IN_FUTURE"),
        ({"model_name": "unknown_model"}, "UNKNOWN_MODEL"),
    ],
)
def test_invalid_evidence_is_not_a_performance_result(
    policy: LoadedPolicy, overrides: dict[str, object], code: str
) -> None:
    result = run(policy, **overrides)
    assert not result.evidence_valid
    assert code in codes(result)
    assert result.severity == "invalid_evidence"
    assert result.metric_results == []
    assert result.proposed_action == "notify"


def test_missing_baseline_is_an_evidence_problem(policy: LoadedPolicy) -> None:
    result = run(policy, baseline=None)
    assert codes(result) == {"BASELINE_MISSING"}


def test_baseline_event_skips_baseline_requirement_but_checks_limits(policy: LoadedPolicy) -> None:
    result = run(policy, baseline=None, role="baseline")
    assert result.evidence_valid and result.severity == "none"
    result = run(policy, baseline=BASELINE, role="baseline", metrics=metrics_for_rmse(60_000))
    assert result.baseline_event_id is None  # never compared with another baseline
    assert result.severity == "critical"


def test_baseline_optional_when_policy_allows(policy: LoadedPolicy) -> None:
    raw = yaml.safe_load(EXAMPLE_POLICY.read_bytes())
    raw["defaults"]["require_baseline"] = False
    relaxed = parse_policy(yaml.safe_dump(raw).encode(), Path("p.yaml"))
    result = run(relaxed, baseline=None)
    assert result.evidence_valid and result.severity == "none"


def test_retraining_proposed_only_when_policy_allows(policy: LoadedPolicy) -> None:
    raw = yaml.safe_load(EXAMPLE_POLICY.read_bytes())
    raw["defaults"]["retraining_enabled"] = True
    raw["models"]["house_price_regressor"]["retraining"].update(
        allowed=True, pipeline_id="demo_mock_retrain"
    )
    permissive = parse_policy(yaml.safe_dump(raw).encode(), Path("p.yaml"))

    critical = run(permissive, metrics=metrics_for_rmse(31_000))
    assert critical.proposed_action == "request_retraining_approval"
    warning = run(permissive, metrics=metrics_for_rmse(28_500))
    assert warning.proposed_action == "notify"
    invalid = run(permissive, sample_count=5, metrics=metrics_for_rmse(90_000))
    assert invalid.proposed_action == "notify"
    assert invalid.retraining_eligible is False


def test_evaluation_is_reproducible(policy: LoadedPolicy) -> None:
    first = run(policy, metrics=metrics_for_rmse(31_000))
    second = run(policy, metrics=metrics_for_rmse(31_000))
    assert first == second
    assert first.digest() == second.digest()
    assert run(policy, metrics=metrics_for_rmse(31_001)).digest() != first.digest()
