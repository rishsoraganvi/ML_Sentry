"""Safety invariants for Milestones 0-1."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from ml_sentry.cli import app as cli
from ml_sentry.demo import demo_events
from ml_sentry.main import create_app
from ml_sentry.policies.loader import LoadedPolicy, PolicyError
from ml_sentry.services.ingestion import ingest_event
from ml_sentry.settings import Settings


def test_app_refuses_to_start_with_missing_policy(settings: Settings, tmp_path: Path) -> None:
    broken = settings.model_copy(update={"policy_path": tmp_path / "nope.yaml"})
    with pytest.raises(PolicyError):
        create_app(broken)


def test_app_refuses_to_start_with_invalid_policy(settings: Settings, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\nmodels: {}\n")
    with pytest.raises(PolicyError):
        create_app(settings.model_copy(update={"policy_path": bad}))


def test_demo_is_deterministic_and_idempotent(
    session_factory: sessionmaker[Session], policy: LoadedPolicy
) -> None:
    first = [ingest_event(session_factory, e, policy, actor="t") for _, e in demo_events()]
    again = [ingest_event(session_factory, e, policy, actor="t") for _, e in demo_events()]
    assert [o.decision.severity for o in first] == [
        "none",
        "none",
        "warning",
        "critical",
        "invalid_evidence",
    ]
    assert all(o.created for o in first)
    assert not any(o.created for o in again)
    assert [o.decision.result_digest for o in first] == [o.decision.result_digest for o in again]
    # With the example policy nothing is eligible for retraining.
    assert all(o.decision.proposed_action != "request_retraining_approval" for o in first)


def test_cli_validate_policy_reports_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\ndefaults:\n  require_human_retraining_approval: false\n")
    result = CliRunner().invoke(cli, ["validate-policy", str(bad)])
    assert result.exit_code == 1
    assert "require_human_retraining_approval" in result.output


def test_cli_doctor_reports_readable_config_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    for key in ("ML_SENTRY_DATABASE_URL", "ML_SENTRY_API_TOKEN", "ML_SENTRY_POLICY_PATH"):
        monkeypatch.delenv(key, raising=False)
    result = CliRunner().invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "[FAIL] configuration" in result.output
    assert "ML_SENTRY_API_TOKEN: is required but not set" in result.output


def test_generate_token_is_strong() -> None:
    token = CliRunner().invoke(cli, ["generate-token"]).output.strip()
    assert len(token) >= 32
