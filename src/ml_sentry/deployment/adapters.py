"""Release adapters: ``mock`` (ML Sentry's own alias table) and ``mlflow_alias``.

``mlflow_alias`` moves an MLflow Model Registry alias (e.g. ``champion``) to the
candidate's registered model version. That is a registry change, not a deployment:
your serving system must load models by that alias for it to take effect.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from ml_sentry.deployment.base import Previous, ReleaseAdapter, ReleaseError, ReleasePlan
from ml_sentry.evaluation.mlflow_adapter import MlflowClient, MlflowError
from ml_sentry.storage.models import ModelAliasRecord, utcnow


class MockAliasAdapter:
    name = "mock"

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def current(self, plan: ReleasePlan) -> Previous:
        with self._sessions() as session:
            row = session.get(ModelAliasRecord, (plan.model_name, plan.alias))
            return Previous(row.model_ref, row.model_version) if row else Previous(None, None)

    def _set(self, plan: ReleasePlan, ref: str | None, version: str | None) -> None:
        with self._sessions() as session:
            row = session.get(ModelAliasRecord, (plan.model_name, plan.alias))
            if ref is None or version is None:
                if row is not None:
                    session.delete(row)
            elif row is None:
                session.add(
                    ModelAliasRecord(
                        model_name=plan.model_name,
                        alias=plan.alias,
                        model_ref=ref,
                        model_version=version,
                        updated_at=utcnow(),
                    )
                )
            else:
                row.model_ref, row.model_version, row.updated_at = ref, version, utcnow()
            session.commit()

    def release(self, plan: ReleasePlan) -> None:
        self._set(plan, plan.candidate_ref, plan.candidate_version)

    def verify(self, plan: ReleasePlan) -> bool:
        if plan.simulate_verification_failure:
            return False
        return self.current(plan).ref == plan.candidate_ref

    def rollback(self, plan: ReleasePlan, previous: Previous) -> None:
        self._set(plan, previous.ref, previous.version)


class MlflowAliasAdapter:
    name = "mlflow_alias"

    def __init__(self, client: MlflowClient) -> None:
        self._client = client

    def _model(self, plan: ReleasePlan) -> str:
        if plan.registered_model is None:
            raise ReleaseError("NO_REGISTERED_MODEL")
        return plan.registered_model

    def current(self, plan: ReleasePlan) -> Previous:
        try:
            version = self._client.get_alias_version(self._model(plan), plan.alias)
        except MlflowError as exc:
            raise ReleaseError(exc.code) from None
        ref = f"models:/{self._model(plan)}/{version}" if version else None
        return Previous(ref, version)

    def release(self, plan: ReleasePlan) -> None:
        try:
            self._client.set_alias(self._model(plan), plan.alias, plan.candidate_version)
        except MlflowError as exc:
            raise ReleaseError(exc.code) from None

    def verify(self, plan: ReleasePlan) -> bool:
        try:
            return self._client.get_alias_version(self._model(plan), plan.alias) == (
                plan.candidate_version
            )
        except MlflowError:
            return False

    def rollback(self, plan: ReleasePlan, previous: Previous) -> None:
        try:
            if previous.version is None:
                self._client.delete_alias(self._model(plan), plan.alias)
            else:
                self._client.set_alias(self._model(plan), plan.alias, previous.version)
        except MlflowError as exc:
            raise ReleaseError(exc.code) from None


def make_adapter(
    adapter: str, session_factory: sessionmaker[Session], mlflow: MlflowClient | None
) -> ReleaseAdapter:
    if adapter == "mock":
        return MockAliasAdapter(session_factory)
    if adapter == "mlflow_alias":
        if mlflow is None:
            raise ReleaseError("MLFLOW_NOT_CONFIGURED")
        return MlflowAliasAdapter(mlflow)
    raise ReleaseError("UNKNOWN_ADAPTER")
