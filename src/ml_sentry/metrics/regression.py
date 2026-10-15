"""Regression metric metadata and reference implementations.

Direction and valid range are declared here, never inferred from a metric's
name elsewhere. The implementations use only the standard library so the core
stays framework-neutral; they back the synthetic demo and the tests.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Direction = Literal["minimize", "maximize"]

# Bump when any metric definition below changes; evaluations recorded under a
# different definition version are not comparable.
METRIC_DEFINITIONS_VERSION = "1"


@dataclass(frozen=True)
class MetricSpec:
    name: str
    direction: Direction
    lower_bound: float | None
    upper_bound: float | None
    description: str


REGRESSION_METRICS: dict[str, MetricSpec] = {
    spec.name: spec
    for spec in (
        MetricSpec("mae", "minimize", 0.0, None, "Mean absolute error"),
        MetricSpec("mse", "minimize", 0.0, None, "Mean squared error"),
        MetricSpec("rmse", "minimize", 0.0, None, "Root mean squared error"),
        # R² may be negative (worse than predicting the mean); it cannot exceed 1.
        MetricSpec("r2", "maximize", None, 1.0, "Coefficient of determination"),
        # Percentage errors are expressed as fractions (0.05 == 5 %).
        MetricSpec("mape", "minimize", 0.0, None, "Mean absolute percentage error (fraction)"),
        MetricSpec("smape", "minimize", 0.0, 2.0, "Symmetric MAPE (fraction, 0..2)"),
    )
}


class MetricComputationError(ValueError):
    """Raised when a metric is mathematically undefined for the given data."""


def _check_inputs(y_true: Sequence[float], y_pred: Sequence[float]) -> None:
    if len(y_true) != len(y_pred):
        raise MetricComputationError("y_true and y_pred must have the same length")
    if not y_true:
        raise MetricComputationError("cannot compute metrics on an empty evaluation set")
    if not all(math.isfinite(v) for v in (*y_true, *y_pred)):
        raise MetricComputationError("inputs must be finite numbers")


def mae(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    _check_inputs(y_true, y_pred)
    return math.fsum(abs(t - p) for t, p in zip(y_true, y_pred, strict=True)) / len(y_true)


def mse(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    _check_inputs(y_true, y_pred)
    return math.fsum((t - p) ** 2 for t, p in zip(y_true, y_pred, strict=True)) / len(y_true)


def rmse(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    return math.sqrt(mse(y_true, y_pred))


def r2(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    _check_inputs(y_true, y_pred)
    mean = math.fsum(y_true) / len(y_true)
    ss_tot = math.fsum((t - mean) ** 2 for t in y_true)
    if ss_tot == 0.0:
        # Constant target: R² is undefined. Report it rather than invent 0 or 1.
        raise MetricComputationError("R² is undefined for a constant target")
    ss_res = math.fsum((t - p) ** 2 for t, p in zip(y_true, y_pred, strict=True))
    return 1.0 - ss_res / ss_tot


def mape(y_true: Sequence[float], y_pred: Sequence[float], *, epsilon: float = 1e-12) -> float:
    _check_inputs(y_true, y_pred)
    if any(abs(t) <= epsilon for t in y_true):
        raise MetricComputationError("MAPE is undefined when any target is zero or near zero")
    return math.fsum(abs((t - p) / t) for t, p in zip(y_true, y_pred, strict=True)) / len(y_true)


def smape(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """SMAPE = mean(2|y - ŷ| / (|y| + |ŷ|)); pairs where both are zero contribute 0."""
    _check_inputs(y_true, y_pred)
    total = 0.0
    for t, p in zip(y_true, y_pred, strict=True):
        denom = abs(t) + abs(p)
        total += 0.0 if denom == 0.0 else 2.0 * abs(t - p) / denom
    return total / len(y_true)


def compute_core_metrics(y_true: Sequence[float], y_pred: Sequence[float]) -> dict[str, float]:
    """Compute MAE, MSE, RMSE and R² (R² omitted for a constant target)."""
    metrics = {"mae": mae(y_true, y_pred), "mse": mse(y_true, y_pred)}
    metrics["rmse"] = math.sqrt(metrics["mse"])
    with contextlib.suppress(MetricComputationError):
        metrics["r2"] = r2(y_true, y_pred)
    return metrics
