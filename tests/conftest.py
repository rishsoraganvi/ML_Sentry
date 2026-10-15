from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from ml_sentry.main import create_app
from ml_sentry.policies.loader import LoadedPolicy, load_policy
from ml_sentry.settings import Settings
from ml_sentry.storage.database import make_engine, make_session_factory, upgrade_to_head

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_POLICY = ROOT / "configs" / "governance.example.yaml"
TOKEN = "test-token-0123456789abcdefghijklmnopqrstuvwxyz"
ALICE = "alice-reviewer-token-0123456789abcdefghijkl"
BOB = "bob-reviewer-token-0123456789abcdefghijklmnop"
RUNNER = "runner-token-0123456789abcdefghijklmnopqrstu"


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def make_event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schema_version": 1,
        "event_id": "evt-001",
        "model_name": "house_price_regressor",
        "model_version": "1",
        "task_type": "regression",
        "target_name": "sale_price",
        "dataset_id": "housing-holdout",
        "dataset_version": "2026-01-01",
        "evaluation_protocol_id": "holdout-v1",
        "evaluation_timestamp": "2026-03-01T10:00:00Z",
        "sample_count": 500,
        "metrics": {"rmse": 25000.0, "mse": 625_000_000.0, "mae": 19000.0, "r2": 0.9},
    }
    event.update(overrides)
    return event


def metrics_for_rmse(rmse: float, *, r2: float = 0.9, mae_ratio: float = 0.76) -> dict[str, float]:
    return {"rmse": rmse, "mse": rmse * rmse, "mae": rmse * mae_ratio, "r2": r2}


@pytest.fixture
def policy() -> LoadedPolicy:
    return load_policy(EXAMPLE_POLICY)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # never pick up a developer's local .env
        database_url=f"sqlite:///{(tmp_path / 'ml_sentry.db').as_posix()}",
        api_token=TOKEN,
        reviewers=f"alice:{sha(ALICE)},bob:{sha(BOB)}",
        runner_token=RUNNER,
        policy_path=EXAMPLE_POLICY,
        artifact_dir=tmp_path / "artifacts",
    )


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    eng = make_engine(settings.database_url)
    upgrade_to_head(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    """A freshly migrated, DISPOSABLE PostgreSQL database (its public schema is dropped)."""
    url = os.environ.get("ML_SENTRY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("ML_SENTRY_TEST_DATABASE_URL not set")
    eng = make_engine(url)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    upgrade_to_head(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def client(settings: Settings, engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c
