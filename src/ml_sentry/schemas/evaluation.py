"""Canonical, versioned evaluation event (schema version 1).

Every integration (generic HTTP API, later MLflow) normalizes to this model.
The schema checks *structure* only: identifiers, sizes and types. Whether the
metric values make sense (finite, in range, consistent) is decided by the
evidence validator, so a structurally valid but numerically bad event is still
recorded and becomes an evidence incident instead of disappearing as a 422.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StringConstraints,
    field_validator,
)

SCHEMA_VERSION = 1

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}$")]
MetricName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
MetadataKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
MetricValue = Annotated[StrictFloat | StrictInt, AfterValidator(float)]

# A report URI is stored as a reference and never fetched or executed by ML Sentry.
ALLOWED_URI_SCHEMES = ("https://", "http://", "mlflow-artifacts:/", "s3://", "gs://")


class EvaluationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    event_id: Identifier = Field(description="Unique idempotency key chosen by the sender")
    event_type: Literal["evaluation.completed"] = "evaluation.completed"
    role: Literal["monitoring", "baseline"] = Field(
        default="monitoring",
        description="'baseline' registers the reference evaluation that later "
        "monitoring events are compared with",
    )
    source: Literal["generic", "mlflow"] = "generic"

    model_name: Identifier
    model_version: Identifier
    evaluation_run_id: Identifier | None = None
    task_type: Literal["regression"]
    target_name: Identifier

    dataset_id: Identifier
    dataset_version: Identifier = Field(description="Immutable dataset version")
    evaluation_protocol_id: Identifier
    evaluation_timestamp: AwareDatetime
    sample_count: Annotated[int, Field(ge=0, le=10**12)]

    metrics: Annotated[dict[MetricName, MetricValue], Field(min_length=1, max_length=32)]

    code_revision: Identifier | None = None
    training_run_id: Identifier | None = None
    environment_metadata: Annotated[
        dict[MetadataKey, ShortText], Field(default_factory=dict, max_length=32)
    ]
    report_uri: Annotated[str, StringConstraints(max_length=2048)] | None = None

    @field_validator("report_uri")
    @classmethod
    def _uri_scheme_allowed(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(ALLOWED_URI_SCHEMES):
            raise ValueError(f"scheme must be one of {', '.join(ALLOWED_URI_SCHEMES)}")
        return value

    def comparability_key(self) -> tuple[str, str, str, str, str]:
        """Evaluations are comparable only if all of these are identical."""
        return (
            self.model_name,
            self.target_name,
            self.dataset_id,
            self.dataset_version,
            self.evaluation_protocol_id,
        )


def json_safe_number(value: float) -> float | str:
    """PostgreSQL JSONB and strict JSON cannot hold NaN/Infinity; store them as text."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return value


def json_safe_metrics(metrics: dict[str, float]) -> dict[str, float | str]:
    return {name: json_safe_number(value) for name, value in metrics.items()}


_NON_FINITE = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}


def metrics_from_json(stored: dict[str, Any]) -> dict[str, float]:
    """Inverse of :func:`json_safe_metrics`."""
    return {
        name: _NON_FINITE[value] if isinstance(value, str) else float(value)
        for name, value in stored.items()
    }


def event_from_payload(payload: dict[str, Any]) -> EvaluationEvent:
    """Rebuild the normalized event from its stored JSON payload."""
    return EvaluationEvent.model_validate(
        {**payload, "metrics": metrics_from_json(payload["metrics"])}
    )
