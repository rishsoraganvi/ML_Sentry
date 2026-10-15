import math

import pytest
from pydantic import ValidationError

from ml_sentry.schemas.evaluation import EvaluationEvent, json_safe_metrics
from tests.conftest import make_event


def test_valid_event_parses_with_defaults() -> None:
    event = EvaluationEvent.model_validate(make_event())
    assert event.role == "monitoring"
    assert event.source == "generic"
    assert event.metrics["rmse"] == 25000.0


def test_integer_metric_is_coerced_to_float() -> None:
    event = EvaluationEvent.model_validate(make_event(metrics={"rmse": 25000}))
    assert isinstance(event.metrics["rmse"], float)


@pytest.mark.parametrize("bad", [True, "25000", None, [1.0]])
def test_non_numeric_metric_rejected(bad: object) -> None:
    with pytest.raises(ValidationError):
        EvaluationEvent.model_validate(make_event(metrics={"rmse": bad}))


def test_non_finite_metric_passes_schema_for_evidence_validation() -> None:
    event = EvaluationEvent.model_validate(make_event(metrics={"rmse": float("nan")}))
    assert math.isnan(event.metrics["rmse"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"unexpected": "field"},
        {"task_type": "classification"},
        {"schema_version": 2},
        {"event_id": "has spaces"},
        {"event_id": "x" * 200},
        {"model_name": "../etc/passwd"},
        {"sample_count": -1},
        {"evaluation_timestamp": "2026-03-01T10:00:00"},  # naive timestamp
        {"metrics": {}},
        {"metrics": {f"m{i}": 1.0 for i in range(33)}},
        {"metrics": {"RMSE": 1.0}},
        {"report_uri": "file:///etc/shadow"},
        {"environment_metadata": {"k": "v" * 300}},
    ],
)
def test_structurally_invalid_events_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EvaluationEvent.model_validate(make_event(**overrides))


def test_report_uri_reference_allowed() -> None:
    event = EvaluationEvent.model_validate(make_event(report_uri="s3://bucket/report.html"))
    assert event.report_uri == "s3://bucket/report.html"


def test_json_safe_metrics() -> None:
    assert json_safe_metrics(
        {"a": float("nan"), "b": float("inf"), "c": -float("inf"), "d": 1.5}
    ) == {
        "a": "NaN",
        "b": "Infinity",
        "c": "-Infinity",
        "d": 1.5,
    }


def test_comparability_key() -> None:
    event = EvaluationEvent.model_validate(make_event())
    assert event.comparability_key() == (
        "house_price_regressor",
        "sale_price",
        "housing-holdout",
        "2026-01-01",
        "holdout-v1",
    )
