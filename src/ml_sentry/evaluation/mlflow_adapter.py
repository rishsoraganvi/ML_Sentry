"""MLflow integration over the MLflow REST API (no `mlflow` package required).

* Reads finished runs from allow-listed experiments and normalizes them into the
  canonical :class:`EvaluationEvent`.
* Reads/moves Model Registry aliases for the ``mlflow_alias`` release adapter.

Run → event mapping (tags, or params as a fallback):
    ml_sentry.model_name, ml_sentry.model_version, ml_sentry.target_name,
    ml_sentry.dataset_id, ml_sentry.dataset_version, ml_sentry.evaluation_protocol_id,
    ml_sentry.sample_count (or the `example_count` metric), ml_sentry.role (optional),
    ml_sentry.metric_prefix (optional, e.g. "holdout_").
Metric names: rmse/mae/mse/r2/mape/smape, or MLflow evaluate()'s long names
(root_mean_squared_error, mean_absolute_error, ...).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from ml_sentry.schemas.evaluation import EvaluationEvent
from ml_sentry.settings import Settings

TAG = "ml_sentry."
METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "rmse": ("rmse", "root_mean_squared_error"),
    "mae": ("mae", "mean_absolute_error"),
    "mse": ("mse", "mean_squared_error"),
    "r2": ("r2", "r2_score"),
    "mape": ("mape", "mean_absolute_percentage_error"),
    "smape": ("smape",),
}


class MlflowError(Exception):
    def __init__(self, code: str, message: str, remote_code: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remote_code = remote_code  # MLflow's own error_code, when it sent one


class MlflowClient:
    def __init__(
        self,
        tracking_uri: str,
        *,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        allowed_experiments: list[str] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base = tracking_uri.rstrip("/")
        self.allowed = set(allowed_experiments or [])
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        auth = httpx.BasicAuth(username, password) if username and password else None
        self._client = client or httpx.Client(timeout=15.0)
        self._headers = headers
        self._auth = auth

    @classmethod
    def from_settings(cls, settings: Settings, client: httpx.Client | None = None) -> MlflowClient:
        if not settings.mlflow_tracking_uri:
            raise MlflowError("MLFLOW_NOT_CONFIGURED", "set ML_SENTRY_MLFLOW_TRACKING_URI")
        return cls(
            settings.mlflow_tracking_uri,
            token=settings.mlflow_tracking_token.get_secret_value()
            if settings.mlflow_tracking_token
            else None,
            username=settings.mlflow_username,
            password=settings.mlflow_password.get_secret_value()
            if settings.mlflow_password
            else None,
            allowed_experiments=settings.mlflow_experiments,
            client=client,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(
                method,
                f"{self.base}/api/2.0/mlflow/{path}",
                headers=self._headers,
                auth=self._auth or httpx.USE_CLIENT_DEFAULT,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise MlflowError(
                "MLFLOW_UNREACHABLE",
                f"cannot reach MLflow ({type(exc).__name__}); check ML_SENTRY_MLFLOW_TRACKING_URI",
            ) from None
        if response.status_code in (401, 403):
            raise MlflowError("MLFLOW_UNAUTHORIZED", "MLflow rejected the credentials")
        if response.status_code >= 400:
            try:
                body = response.json()
                remote, detail = str(body.get("error_code")), str(body.get("message", ""))[:300]
            except ValueError:
                remote, detail = None, ""
            if response.status_code == 404 or remote == "RESOURCE_DOES_NOT_EXIST":
                raise MlflowError("MLFLOW_NOT_FOUND", f"MLflow: not found ({path})", remote)
            raise MlflowError(
                "MLFLOW_ERROR", f"MLflow returned HTTP {response.status_code}: {detail}", remote
            )
        data: dict[str, Any] = response.json() if response.content else {}
        return data

    # -- runs ------------------------------------------------------------------

    def get_run(self, run_id: str) -> dict[str, Any]:
        run: dict[str, Any] = self._request("GET", "runs/get", params={"run_id": run_id})["run"]
        return run

    def experiment_allowed(self, experiment_id: str) -> bool:
        if not self.allowed:
            return False
        if experiment_id in self.allowed:
            return True
        name = self._request("GET", "experiments/get", params={"experiment_id": experiment_id})[
            "experiment"
        ]["name"]
        return name in self.allowed

    def run_to_event(self, run_id: str, *, role: str | None = None) -> EvaluationEvent:
        run = self.get_run(run_id)
        info, data = run["info"], run.get("data", {})
        if not self.experiment_allowed(str(info["experiment_id"])):
            raise MlflowError(
                "MLFLOW_EXPERIMENT_NOT_ALLOWED",
                "the run's experiment is not in ML_SENTRY_MLFLOW_ALLOWED_EXPERIMENTS",
            )
        if info.get("status") != "FINISHED":
            raise MlflowError("MLFLOW_RUN_NOT_FINISHED", f"run status is {info.get('status')}")
        return build_event(run_id, info, data, role=role)

    def metrics_for_run(self, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """(info, data) of an allow-listed, finished run."""
        run = self.get_run(run_id)
        if not self.experiment_allowed(str(run["info"]["experiment_id"])):
            raise MlflowError(
                "MLFLOW_EXPERIMENT_NOT_ALLOWED",
                "the run's experiment is not in ML_SENTRY_MLFLOW_ALLOWED_EXPERIMENTS",
            )
        return run["info"], run.get("data", {})

    # -- registry aliases --------------------------------------------------------

    def get_alias_version(self, registered_model: str, alias: str) -> str | None:
        try:
            data = self._request(
                "GET", "registered-models/alias", params={"name": registered_model, "alias": alias}
            )
        except MlflowError as exc:
            # MLflow 3.x answers a missing alias with 400 INVALID_PARAMETER_VALUE "... not found".
            missing_alias = exc.remote_code == "INVALID_PARAMETER_VALUE" and (
                "not found" in exc.message.lower()
            )
            if exc.code == "MLFLOW_NOT_FOUND" or missing_alias:
                return None
            raise
        return str(data["model_version"]["version"])

    def set_alias(self, registered_model: str, alias: str, version: str) -> None:
        self._request(
            "POST",
            "registered-models/alias",
            json={"name": registered_model, "alias": alias, "version": version},
        )

    def delete_alias(self, registered_model: str, alias: str) -> None:
        # MLflow reads DELETE parameters from a JSON body (verified against MLflow 3.10).
        self._request(
            "DELETE", "registered-models/alias", json={"name": registered_model, "alias": alias}
        )


def _kv(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {item["key"]: item["value"] for item in items or []}


def extract_metrics(
    raw_metrics: dict[str, float], prefix: str = ""
) -> tuple[dict[str, float], float | None]:
    metrics: dict[str, float] = {}
    for name, keys in METRIC_ALIASES.items():
        for key in keys:
            if f"{prefix}{key}" in raw_metrics:
                metrics[name] = float(raw_metrics[f"{prefix}{key}"])
                break
    count = raw_metrics.get(f"{prefix}example_count")
    return metrics, count


def build_event(
    run_id: str, info: dict[str, Any], data: dict[str, Any], *, role: str | None = None
) -> EvaluationEvent:
    tags = {**_kv(data.get("params")), **_kv(data.get("tags"))}

    def tag(name: str) -> str | None:
        value = tags.get(TAG + name)
        return str(value) if value is not None else None

    raw_metrics = {k: float(v) for k, v in _kv(data.get("metrics")).items()}
    metrics, count = extract_metrics(raw_metrics, tag("metric_prefix") or "")
    sample_count = tag("sample_count")
    missing = [
        name
        for name in (
            "model_name",
            "model_version",
            "target_name",
            "dataset_id",
            "dataset_version",
            "evaluation_protocol_id",
        )
        if tag(name) is None
    ]
    if missing:
        raise MlflowError(
            "MLFLOW_RUN_MISSING_TAGS",
            "run is missing tags: " + ", ".join(TAG + m for m in missing),
        )
    if not metrics:
        raise MlflowError("MLFLOW_RUN_NO_METRICS", "run has no recognised regression metrics")
    end_ms = info.get("end_time") or info.get("start_time") or 0
    payload = {
        "schema_version": 1,
        "event_id": f"mlflow-{run_id}",
        "source": "mlflow",
        "role": role or tag("role") or "monitoring",
        "model_name": tag("model_name"),
        "model_version": tag("model_version"),
        "evaluation_run_id": run_id,
        "task_type": "regression",
        "target_name": tag("target_name"),
        "dataset_id": tag("dataset_id"),
        "dataset_version": tag("dataset_version"),
        "evaluation_protocol_id": tag("evaluation_protocol_id"),
        "evaluation_timestamp": datetime.fromtimestamp(int(end_ms) / 1000, tz=UTC),
        "sample_count": int(float(sample_count)) if sample_count else int(count or 0),
        "metrics": metrics,
        "code_revision": tags.get("mlflow.source.git.commit"),
        "environment_metadata": {"mlflow_experiment_id": str(info["experiment_id"])},
    }
    try:
        return EvaluationEvent.model_validate(payload)
    except ValueError as exc:
        raise MlflowError(
            "MLFLOW_RUN_INVALID", f"run does not map to a valid event: {exc}"
        ) from None
