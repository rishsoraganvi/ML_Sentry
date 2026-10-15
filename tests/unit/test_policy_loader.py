from pathlib import Path

import pytest
import yaml

from ml_sentry.policies.loader import PolicyError, load_policy, parse_policy
from tests.conftest import EXAMPLE_POLICY


def _with(mutate: object) -> bytes:
    data = yaml.safe_load(EXAMPLE_POLICY.read_bytes())
    mutate(data)  # type: ignore[operator]
    return yaml.safe_dump(data).encode()


def _model(data: dict) -> dict:  # type: ignore[type-arg]
    return data["models"]["house_price_regressor"]


def test_example_policy_loads_with_safe_defaults() -> None:
    loaded = load_policy(EXAMPLE_POLICY)
    assert loaded.version.startswith("1-")
    policy = loaded.policy
    model = policy.models["house_price_regressor"]
    assert model.retraining.allowed is False
    assert model.deployment.allowed is False
    # Anything the example allows uses only the built-in mock runner and mock target.
    assert {p.runner for p in policy.pipelines.values()} == {"mock"}
    assert {t.adapter for t in policy.release_targets.values()} == {"mock"}


def test_policy_version_tracks_file_content() -> None:
    raw = EXAMPLE_POLICY.read_bytes()
    assert parse_policy(raw, EXAMPLE_POLICY).version == parse_policy(raw, EXAMPLE_POLICY).version
    assert (
        parse_policy(raw + b"\n# edit\n", EXAMPLE_POLICY).version
        != parse_policy(raw, EXAMPLE_POLICY).version
    )


def test_missing_policy_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="ML_SENTRY_POLICY_PATH"):
        load_policy(tmp_path / "missing.yaml")


@pytest.mark.parametrize("raw", [b"", b"- a list\n", b"key: [unclosed\n", b"schema_version: 1\n"])
def test_malformed_policy_fails_closed(raw: bytes) -> None:
    with pytest.raises(PolicyError):
        parse_policy(raw, Path("p.yaml"))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(schema_version=2), "schema_version"),
        (lambda d: d["defaults"].update(require_human_retraining_approval=False), "require_human"),
        (lambda d: d["defaults"].update(require_human_deployment_approval=False), "require_human"),
        (lambda d: d["defaults"].update(retrain_enabled=True), "retrain_enabled"),  # typo
        (lambda d: _model(d)["metrics"].update(accuracy={"direction": "maximize"}), "unknown"),
        (
            lambda d: _model(d)["metrics"].update(r2={"direction": "minimize"}),
            "must use direction 'maximize'",
        ),
        (lambda d: _model(d).update(primary_metric="mape"), "primary_metric"),
        (
            lambda d: _model(d)["metrics"]["rmse"].update(warning_relative_increase=0.5),
            "warning threshold",
        ),
        (
            lambda d: _model(d)["metrics"]["r2"].update(warning_relative_increase=0.1),
            "not valid",
        ),
        (
            lambda d: _model(d)["metrics"]["rmse"].update(critical_relative_increase=-1),
            "greater than or equal",
        ),
        (lambda d: _model(d).update(task_type="classification"), "task_type"),
        (lambda d: d.update(models={}), "models"),
        (lambda d: _model(d)["deployment"].update(require_same_holdout_dataset=False), "holdout"),
        (lambda d: d["defaults"].update(require_candidate_evaluation=False), "candidate"),
        (lambda d: _model(d)["retraining"].update(allowed=True), "pipeline_id"),
        (
            lambda d: _model(d)["retraining"].update(allowed=True, pipeline_id="nope"),
            "is not defined",
        ),
        (
            lambda d: d["models"]["demo_regressor"]["deployment"].update(target="nope"),
            "is not defined",
        ),
        (
            lambda d: d["models"]["demo_regressor"]["retraining"].update(allowed=False),
            "deployment.allowed needs retraining.allowed",
        ),
        (lambda d: d["pipelines"]["demo_mock_retrain"].update(runner="shell"), "runner"),
        (
            lambda d: d["pipelines"]["demo_mock_retrain"].update(command="rm -rf /"),
            "command",
        ),
        (
            lambda d: d["release_targets"]["demo_local_alias"].update(adapter="mlflow_alias"),
            "registered_model",
        ),
        (lambda d: d["defaults"].update(approval_ttl_hours=0), "approval_ttl_hours"),
    ],
)
def test_invalid_policy_rejected(mutate: object, message: str) -> None:
    with pytest.raises(PolicyError, match=message):
        parse_policy(_with(mutate), Path("p.yaml"))
