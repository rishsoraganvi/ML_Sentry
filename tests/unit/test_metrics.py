import math

import pytest

from ml_sentry.metrics import regression as m


def test_core_metrics_on_known_values() -> None:
    y_true = [3.0, -0.5, 2.0, 7.0]
    y_pred = [2.5, 0.0, 2.0, 8.0]
    assert m.mae(y_true, y_pred) == pytest.approx(0.5)
    assert m.mse(y_true, y_pred) == pytest.approx(0.375)
    assert m.rmse(y_true, y_pred) == pytest.approx(math.sqrt(0.375))
    assert m.r2(y_true, y_pred) == pytest.approx(0.948608, rel=1e-5)


def test_r2_can_be_negative() -> None:
    assert m.r2([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == pytest.approx(-3.0)


def test_r2_constant_target_is_undefined() -> None:
    with pytest.raises(m.MetricComputationError, match="constant target"):
        m.r2([5.0, 5.0, 5.0], [4.0, 5.0, 6.0])


def test_compute_core_metrics_omits_r2_for_constant_target() -> None:
    result = m.compute_core_metrics([5.0, 5.0], [4.0, 6.0])
    assert "r2" not in result
    assert result["rmse"] == pytest.approx(1.0)


@pytest.mark.parametrize("fn", [m.mae, m.mse, m.rmse, m.r2, m.smape])
def test_empty_evaluation_rejected(fn: object) -> None:
    with pytest.raises(m.MetricComputationError, match="empty"):
        fn([], [])  # type: ignore[operator]


def test_non_finite_inputs_rejected() -> None:
    with pytest.raises(m.MetricComputationError, match="finite"):
        m.mae([1.0, float("nan")], [1.0, 2.0])


def test_length_mismatch_rejected() -> None:
    with pytest.raises(m.MetricComputationError, match="same length"):
        m.mae([1.0], [1.0, 2.0])


def test_mape_undefined_for_zero_target() -> None:
    with pytest.raises(m.MetricComputationError, match="zero"):
        m.mape([0.0, 1.0], [0.1, 1.0])
    assert m.mape([2.0, 4.0], [1.0, 4.0]) == pytest.approx(0.25)


def test_smape_handles_both_zero_and_is_bounded() -> None:
    assert m.smape([0.0, 1.0], [0.0, 1.0]) == 0.0
    assert m.smape([1.0], [-1.0]) == pytest.approx(2.0)


def test_metric_metadata_declares_direction_and_ranges() -> None:
    assert m.REGRESSION_METRICS["rmse"].direction == "minimize"
    assert m.REGRESSION_METRICS["r2"].direction == "maximize"
    assert m.REGRESSION_METRICS["r2"].lower_bound is None  # R² may be negative
    assert m.REGRESSION_METRICS["r2"].upper_bound == 1.0
    assert all(
        m.REGRESSION_METRICS[name].lower_bound == 0.0 for name in ("mae", "mse", "rmse", "mape")
    )
