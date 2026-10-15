import pytest

from ml_sentry.settings import ConfigurationError, load_settings

REQUIRED = {
    "ML_SENTRY_DATABASE_URL": "sqlite:///x.db",
    "ML_SENTRY_API_TOKEN": "a" * 40,
    "ML_SENTRY_POLICY_PATH": "configs/governance.example.yaml",
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    monkeypatch.chdir(tmp_path)  # no .env in cwd
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)


def test_missing_required_values_name_the_variables() -> None:
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    text = str(exc.value)
    for key in REQUIRED:
        assert f"{key}: is required but not set" in text
    assert "ml-sentry generate-token" in text


@pytest.mark.parametrize(
    ("token", "reason"), [("short", "at least 32"), ("change-me", "placeholder")]
)
def test_weak_tokens_rejected(monkeypatch: pytest.MonkeyPatch, token: str, reason: str) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ML_SENTRY_API_TOKEN", token)
    with pytest.raises(ConfigurationError, match=reason):
        load_settings()


def test_valid_settings_default_to_safe_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
    assert settings.retraining_enabled is False
    assert settings.deployment_enabled is False
    assert settings.notification_provider == "console"
    assert "a" * 40 not in repr(settings)  # token is a SecretStr
