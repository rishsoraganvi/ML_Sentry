"""Offline, deterministic synthetic regression demo.

Builds evaluation events from seeded synthetic house-price predictions. Nothing
is trained, deployed or sent anywhere.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

from ml_sentry.metrics.regression import compute_core_metrics
from ml_sentry.schemas.evaluation import EvaluationEvent

DEMO_MODEL = "house_price_regressor"
# Fixed so re-running the demo produces identical payloads (and is idempotent).
DEMO_TIMESTAMP = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
SAMPLE_COUNT = 500


def synthetic_metrics(error_scale: float, *, seed: int) -> dict[str, float]:
    rng = random.Random(seed)
    y_true = [rng.gauss(300_000, 80_000) for _ in range(SAMPLE_COUNT)]
    noise = random.Random(seed + 1)
    y_pred = [y + noise.gauss(0, error_scale) for y in y_true]
    return {k: round(v, 6) for k, v in compute_core_metrics(y_true, y_pred).items()}


def _event(
    event_id: str, version: str, role: str, metrics: dict[str, float], **kw: object
) -> EvaluationEvent:
    data: dict[str, object] = {
        "schema_version": 1,
        "event_id": event_id,
        "role": role,
        "model_name": DEMO_MODEL,
        "model_version": version,
        "task_type": "regression",
        "target_name": "sale_price",
        "dataset_id": "demo-housing-holdout",
        "dataset_version": "2026-01-01",
        "evaluation_protocol_id": "holdout-v1",
        "evaluation_timestamp": DEMO_TIMESTAMP,
        "sample_count": SAMPLE_COUNT,
        "metrics": metrics,
        "environment_metadata": {"generator": "ml-sentry-demo"},
    }
    data.update(kw)
    return EvaluationEvent.model_validate(data)


def demo_events() -> list[tuple[str, EvaluationEvent]]:
    """(description, event) pairs in submission order; the baseline comes first."""
    return [
        (
            "baseline (champion v1)",
            _event("demo-baseline-v1", "1", "baseline", synthetic_metrics(25_000, seed=7)),
        ),
        (
            "healthy monitoring run",
            _event("demo-monitor-healthy", "1", "monitoring", synthetic_metrics(25_500, seed=7)),
        ),
        (
            "moderate degradation",
            _event("demo-monitor-warning", "1", "monitoring", synthetic_metrics(28_500, seed=7)),
        ),
        (
            "severe degradation",
            _event("demo-monitor-critical", "1", "monitoring", synthetic_metrics(34_000, seed=7)),
        ),
        (
            "invalid evidence (too few samples)",
            _event(
                "demo-monitor-invalid",
                "1",
                "monitoring",
                synthetic_metrics(25_000, seed=7),
                sample_count=12,
            ),
        ),
    ]


REMEDIATION_MODEL = "demo_regressor"


def remediation_events() -> list[tuple[str, EvaluationEvent]]:
    """Baseline + critical regression for the model whose policy allows the mock loop."""
    return [
        (
            "remediation demo: baseline",
            _event(
                "demo-remediation-baseline",
                "1",
                "baseline",
                synthetic_metrics(25_000, seed=11),
                model_name=REMEDIATION_MODEL,
            ),
        ),
        (
            "remediation demo: severe degradation",
            _event(
                "demo-remediation-critical",
                "1",
                "monitoring",
                synthetic_metrics(34_000, seed=11),
                model_name=REMEDIATION_MODEL,
            ),
        ),
    ]
