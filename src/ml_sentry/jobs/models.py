"""Schemas for job results reported by runners (mock or external)."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from ml_sentry.schemas.evaluation import Identifier, MetricName, MetricValue

# A model reference is stored and displayed, never fetched or executed by ML Sentry.
ModelRef = Annotated[str, StringConstraints(pattern=r"^[\x21-\x7e]{1,512}$")]
ErrorCode = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9_]{1,64}$")]


class CandidateEvaluation(BaseModel):
    """The candidate's evaluation on the champion's fixed holdout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: Identifier
    dataset_version: Identifier
    evaluation_protocol_id: Identifier
    target_name: Identifier
    sample_count: Annotated[int, Field(ge=0, le=10**12)]
    metrics: Annotated[dict[MetricName, MetricValue], Field(min_length=1, max_length=32)]


class JobResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["succeeded", "failed"]
    candidate_model_ref: ModelRef | None = None
    candidate_model_version: Identifier | None = None
    candidate_run_id: Identifier | None = None
    evaluation: CandidateEvaluation | None = None
    # Alternative to `evaluation`: an MLflow run ML Sentry should read the metrics from.
    mlflow_run_id: Identifier | None = None
    error_code: ErrorCode | None = None
    logs_ref: Annotated[str, StringConstraints(max_length=2048)] | None = None

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.status == "succeeded":
            if not (self.candidate_model_ref and self.candidate_model_version):
                raise ValueError("a succeeded job needs candidate_model_ref and _version")
            if (self.evaluation is None) == (self.mlflow_run_id is None):
                raise ValueError("provide exactly one of evaluation or mlflow_run_id")
        elif self.error_code is None:
            raise ValueError("a failed job needs error_code")
        return self
