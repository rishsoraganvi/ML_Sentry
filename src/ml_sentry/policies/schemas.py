"""Governance policy file schema (schema version 1).

Every model uses ``extra="forbid"`` so a misspelt key fails loading instead of
silently disabling a check.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ml_sentry.metrics.regression import REGRESSION_METRICS, Direction
from ml_sentry.schemas.evaluation import Identifier

NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Finite = Annotated[float, Field(allow_inf_nan=False)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Defaults(_Strict):
    minimum_sample_count: Annotated[int, Field(ge=1)] = 100
    require_baseline: bool = True
    # Human approval is a product invariant, not a tunable: only `true` is accepted.
    require_human_retraining_approval: Literal[True] = True
    require_human_deployment_approval: Literal[True] = True
    require_candidate_evaluation: Literal[True] = True
    retraining_enabled: bool = False
    deployment_enabled: bool = False
    # Pending approvals expire after this many hours and can then never be used.
    approval_ttl_hours: Annotated[float, Field(gt=0, le=24 * 30)] = 24
    # When true, the deployment approver must differ from the retraining approver.
    require_distinct_approvers: bool = False


class MetricPolicy(_Strict):
    direction: Direction
    warning_relative_increase: NonNegative | None = None
    critical_relative_increase: NonNegative | None = None
    warning_relative_decrease: NonNegative | None = None
    critical_relative_decrease: NonNegative | None = None
    maximum_acceptable: Finite | None = None
    minimum_acceptable: Finite | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.direction == "minimize":
            wrong = ("warning_relative_decrease", "critical_relative_decrease")
            warn, crit = self.warning_relative_increase, self.critical_relative_increase
        else:
            wrong = ("warning_relative_increase", "critical_relative_increase")
            warn, crit = self.warning_relative_decrease, self.critical_relative_decrease
        for name in wrong:
            if getattr(self, name) is not None:
                raise ValueError(f"{name} is not valid for a metric that should {self.direction}")
        if warn is not None and crit is not None and warn > crit:
            raise ValueError("warning threshold must not exceed the critical threshold")
        if (
            self.maximum_acceptable is not None
            and self.minimum_acceptable is not None
            and self.minimum_acceptable > self.maximum_acceptable
        ):
            raise ValueError("minimum_acceptable must not exceed maximum_acceptable")
        return self

    @property
    def warning_relative(self) -> float | None:
        if self.direction == "minimize":
            return self.warning_relative_increase
        return self.warning_relative_decrease

    @property
    def critical_relative(self) -> float | None:
        if self.direction == "minimize":
            return self.critical_relative_increase
        return self.critical_relative_decrease


ConfigValue = str | int | float | bool


class DatasetRef(_Strict):
    dataset_id: Identifier
    dataset_version: Identifier


class PipelineSpec(_Strict):
    """An allow-listed training pipeline. Nothing in an event or API call can change it."""

    runner: Literal["mock", "external"]
    description: Annotated[str, Field(max_length=500)] = ""
    training_dataset: DatasetRef
    config: Annotated[dict[Identifier, ConfigValue], Field(max_length=32)] = {}
    timeout_minutes: Annotated[int, Field(ge=1, le=7 * 24 * 60)] = 60


class ReleaseTarget(_Strict):
    """An allow-listed place a candidate may be released to."""

    adapter: Literal["mock", "mlflow_alias"]
    description: Annotated[str, Field(max_length=500)] = ""
    # mlflow_alias: the registered model and alias that serving infrastructure follows.
    registered_model: Identifier | None = None
    alias: Identifier = "champion"
    # mock only: lets demos and tests exercise the rollback path.
    simulate_verification_failure: bool = False

    @model_validator(mode="after")
    def _adapter_settings(self) -> Self:
        if self.adapter == "mlflow_alias" and self.registered_model is None:
            raise ValueError("mlflow_alias targets need registered_model")
        if self.adapter != "mock" and self.simulate_verification_failure:
            raise ValueError("simulate_verification_failure is only valid for mock targets")
        return self


class RetrainingPolicy(_Strict):
    allowed: bool = False
    pipeline_id: Identifier | None = None
    cooldown_hours: Annotated[float, Field(ge=0)] = 24
    max_attempts_per_incident: Annotated[int, Field(ge=0, le=10)] = 1

    @model_validator(mode="after")
    def _pipeline_when_allowed(self) -> Self:
        if self.allowed and self.pipeline_id is None:
            raise ValueError("retraining.allowed requires retraining.pipeline_id")
        return self


class DeploymentPolicy(_Strict):
    allowed: bool = False
    target: Identifier | None = None
    require_same_holdout_dataset: Literal[True] = True
    # Candidate's primary metric must beat the champion by at least this fraction.
    min_primary_improvement: Annotated[float, Field(ge=0, le=1)] = 0.0

    @model_validator(mode="after")
    def _target_when_allowed(self) -> Self:
        if self.allowed and self.target is None:
            raise ValueError("deployment.allowed requires deployment.target")
        return self


class ModelPolicy(_Strict):
    task_type: Literal["regression"]
    primary_metric: str
    minimum_sample_count: Annotated[int, Field(ge=1)] | None = None
    require_baseline: bool | None = None
    metrics: Annotated[dict[str, MetricPolicy], Field(min_length=1)]
    retraining: RetrainingPolicy = RetrainingPolicy()
    deployment: DeploymentPolicy = DeploymentPolicy()

    @model_validator(mode="after")
    def _metrics_known(self) -> Self:
        for name, metric in self.metrics.items():
            spec = REGRESSION_METRICS.get(name)
            if spec is None:
                known = ", ".join(sorted(REGRESSION_METRICS))
                raise ValueError(f"unknown metric '{name}' (supported: {known})")
            if metric.direction != spec.direction:
                raise ValueError(
                    f"metric '{name}' must use direction '{spec.direction}', "
                    f"not '{metric.direction}'"
                )
        if self.primary_metric not in self.metrics:
            raise ValueError(f"primary_metric '{self.primary_metric}' is not listed in metrics")
        return self


class PolicyFile(_Strict):
    schema_version: Literal[1]
    defaults: Defaults = Defaults()
    pipelines: dict[Identifier, PipelineSpec] = {}
    release_targets: dict[Identifier, ReleaseTarget] = {}
    models: Annotated[dict[Identifier, ModelPolicy], Field(min_length=1)]

    @model_validator(mode="after")
    def _references_exist(self) -> Self:
        for name, model in self.models.items():
            pipeline = model.retraining.pipeline_id
            if pipeline is not None and pipeline not in self.pipelines:
                raise ValueError(
                    f"models.{name}.retraining.pipeline_id '{pipeline}' is not defined"
                )
            target = model.deployment.target
            if target is not None and target not in self.release_targets:
                raise ValueError(f"models.{name}.deployment.target '{target}' is not defined")
            if model.deployment.allowed and not model.retraining.allowed:
                raise ValueError(
                    f"models.{name}: deployment.allowed needs retraining.allowed "
                    "(only retrained candidates are released)"
                )
        return self
