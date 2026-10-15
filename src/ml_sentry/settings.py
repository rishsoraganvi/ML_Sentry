"""Environment-based configuration.

All settings use the ``ML_SENTRY_`` prefix. Loading fails closed: a missing or
malformed value raises :class:`ConfigurationError` with a message that names the
environment variable to fix.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "ML_SENTRY_"

# Values copied verbatim from documentation must never be accepted as a real token.
_PLACEHOLDER_TOKENS = {"change-me", "changeme", "replace-me", "your-token-here"}
MIN_TOKEN_LENGTH = 32


class ConfigurationError(Exception):
    """Raised when the environment configuration is missing or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        description="SQLAlchemy URL, e.g. postgresql+psycopg://user:pass@host:5432/db",
    )
    api_token: SecretStr = Field(description="Bearer token required by the HTTP API")
    policy_path: Path = Field(description="Path to the governance policy YAML file")
    artifact_dir: Path = Field(default=Path("/var/lib/ml-sentry/artifacts"))

    api_url: str = Field(
        default="http://127.0.0.1:8000",
        description="Base URL the doctor command uses to reach the API",
    )
    max_request_bytes: int = Field(default=65_536, ge=1_024, le=10_485_760)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Base URL used in notification links (defaults to api_url).
    public_url: str | None = None

    # Reviewers who may decide approvals: "name:sha256(token),name2:sha256(token2)".
    # Generate entries with: ml-sentry add-reviewer <name>
    reviewers: str = ""
    # Token for external training runners (e.g. a Colab notebook) to claim jobs.
    runner_token: SecretStr | None = None

    notification_provider: Literal["console", "none", "slack", "telegram", "webhook"] = "console"
    slack_webhook_url: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    webhook_url: str | None = None
    webhook_secret: SecretStr | None = None

    mlflow_tracking_uri: str | None = None
    mlflow_tracking_token: SecretStr | None = None
    mlflow_username: str | None = None
    mlflow_password: SecretStr | None = None
    # Comma-separated experiment names or IDs ML Sentry may read. Empty = MLflow ingestion off.
    mlflow_allowed_experiments: str = ""

    # Environment kill switches for side effects outside ML Sentry. Mock runners and
    # mock release targets only touch ML Sentry's own database and ignore these.
    retraining_enabled: bool = False
    deployment_enabled: bool = False

    worker_poll_seconds: float = Field(default=2.0, gt=0, le=300)

    @field_validator("api_token", "runner_token")
    @classmethod
    def _token_is_strong(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value():
            return None
        token = value.get_secret_value()
        if token.strip().lower() in _PLACEHOLDER_TOKENS:
            raise ValueError("is a documentation placeholder; generate a random token")
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(f"must be at least {MIN_TOKEN_LENGTH} characters")
        return value

    @field_validator(
        "mlflow_tracking_uri",
        "public_url",
        "telegram_chat_id",
        "webhook_url",
        "mlflow_username",
    )
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return value or None

    @field_validator(
        "slack_webhook_url",
        "telegram_bot_token",
        "webhook_secret",
        "mlflow_tracking_token",
        "mlflow_password",
    )
    @classmethod
    def _blank_secret_is_none(cls, value: SecretStr | None) -> SecretStr | None:
        return value if value is not None and value.get_secret_value() else None

    @field_validator("reviewers")
    @classmethod
    def _reviewers_well_formed(cls, value: str) -> str:
        parse_reviewers(value)
        return value

    @field_validator("webhook_url", "slack_webhook_url", mode="after")
    @classmethod
    def _https_only(cls, value: object) -> object:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(raw, str) and not raw.startswith("https://"):
            raise ValueError("must be an https:// URL")
        return value

    @model_validator(mode="after")
    def _provider_configured(self) -> Settings:
        requirements: dict[str, list[tuple[str, object]]] = {
            "slack": [("slack_webhook_url", self.slack_webhook_url)],
            "telegram": [
                ("telegram_bot_token", self.telegram_bot_token),
                ("telegram_chat_id", self.telegram_chat_id),
            ],
            "webhook": [("webhook_url", self.webhook_url), ("webhook_secret", self.webhook_secret)],
        }
        missing = requirements.get(self.notification_provider, [])
        unset = [f"{ENV_PREFIX}{name.upper()}" for name, val in missing if val is None]
        if unset:
            raise ValueError(
                f"notification provider '{self.notification_provider}' needs {', '.join(unset)}"
            )
        return self

    @property
    def reviewer_hashes(self) -> dict[str, str]:
        return parse_reviewers(self.reviewers)

    @property
    def link_base(self) -> str:
        return (self.public_url or self.api_url).rstrip("/")

    @property
    def mlflow_experiments(self) -> list[str]:
        return [e.strip() for e in self.mlflow_allowed_experiments.split(",") if e.strip()]


_REVIEWER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def parse_reviewers(value: str) -> dict[str, str]:
    """Parse ``name:sha256hex`` pairs. Only token hashes are configured, never tokens."""
    result: dict[str, str] = {}
    for entry in filter(None, (e.strip() for e in value.split(","))):
        name, sep, digest = entry.partition(":")
        if not sep or not _REVIEWER_NAME.match(name) or not _SHA256_HEX.match(digest):
            raise ValueError(
                "entries must look like 'name:<64 hex sha256>' (use: ml-sentry add-reviewer NAME)"
            )
        if name in result:
            raise ValueError(f"reviewer '{name}' is listed twice")
        result[name] = digest
    return result


_HINTS = {
    "api_token": "generate one with: ml-sentry generate-token",
    "database_url": "for Docker Compose this is set automatically from POSTGRES_PASSWORD",
    "policy_path": "point it at a policy file, e.g. configs/governance.example.yaml",
}


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["Invalid ML Sentry configuration:"]
    for err in exc.errors():
        reason = "is required but not set" if err["type"] == "missing" else err["msg"]
        if not err["loc"]:  # cross-field rule; the message names the variables
            lines.append(f"  - {reason}")
            continue
        field = str(err["loc"][0])
        env_name = f"{ENV_PREFIX}{field.upper()}"
        line = f"  - {env_name}: {reason}"
        if field in _HINTS:
            line += f" ({_HINTS[field]})"
        lines.append(line)
    lines.append("Set these in your .env file (see .env.example) and restart.")
    return "\n".join(lines)


def load_settings() -> Settings:
    try:
        return Settings()  # values come from the environment / .env
    except ValidationError as exc:
        raise ConfigurationError(_format_validation_error(exc)) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
