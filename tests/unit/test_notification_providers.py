"""Slack, Telegram and webhook providers against mocked HTTP (no real network)."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable

import httpx
import pytest

from ml_sentry.notifications.base import IncidentNotification, NotificationError
from ml_sentry.notifications.console import make_notifier
from ml_sentry.notifications.http import SlackNotifier, TelegramNotifier, WebhookNotifier
from ml_sentry.settings import ConfigurationError, Settings, load_settings

NOTE = IncidentNotification(
    incident_id="11111111-1111-1111-1111-111111111111",
    severity="critical",
    model_name="m",
    model_version="1",
    status="awaiting_approval",
    proposed_action="request_retraining_approval",
    summary="rmse degraded 24%",
    link="https://sentry.example/ui/#approval/x",
    kind="approval_request",
)
BOT_TOKEN = "123456:SECRET-BOT-TOKEN"


def client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_slack_posts_text() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    SlackNotifier("https://hooks.slack.com/services/T/B/X", client(handler)).send_incident(NOTE)
    body = json.loads(seen[0].content)
    assert "APPROVAL NEEDED" in body["text"] and NOTE.link in body["text"]


def test_telegram_sends_message_and_never_leaks_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/bot{BOT_TOKEN}/sendMessage"
        payload = json.loads(request.content)
        assert payload["chat_id"] == "42" and payload["disable_web_page_preview"] is True
        return httpx.Response(200, json={"ok": True})

    TelegramNotifier(BOT_TOKEN, "42", client(handler)).send_incident(NOTE)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(NotificationError) as exc:
        TelegramNotifier(BOT_TOKEN, "42", client(down)).send_incident(NOTE)
    assert "SECRET" not in exc.value.code and "SECRET" not in str(exc.value)
    assert exc.value.code == "HTTP_CONNECTERROR"


def test_telegram_not_ok_is_a_failure() -> None:
    notifier = TelegramNotifier(
        BOT_TOKEN, "42", client(lambda r: httpx.Response(200, json={"ok": False}))
    )
    with pytest.raises(NotificationError, match="TELEGRAM_NOT_OK"):
        notifier.send_incident(NOTE)


def test_webhook_is_signed() -> None:
    secret = "whsec-0123456789"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    WebhookNotifier("https://hooks.example/ml", secret, client(handler)).send_incident(NOTE)
    request = seen[0]
    timestamp = request.headers["X-ML-Sentry-Timestamp"]
    expected = hmac.new(
        secret.encode(), timestamp.encode() + b"." + request.content, hashlib.sha256
    ).hexdigest()
    assert request.headers["X-ML-Sentry-Signature"] == f"sha256={expected}"
    body = json.loads(request.content)
    assert body["type"] == "ml_sentry.approval_request"
    assert body["incident_id"] == NOTE.incident_id


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_http_errors_become_stable_codes(status: int) -> None:
    notifier = SlackNotifier("https://x", client(lambda r: httpx.Response(status)))
    with pytest.raises(NotificationError) as exc:
        notifier.send_incident(NOTE)
    assert exc.value.code == f"HTTP_{status}"


def test_provider_settings_are_required(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("ML_SENTRY_DATABASE_URL", "sqlite:///x.db")
    monkeypatch.setenv("ML_SENTRY_API_TOKEN", "a" * 40)
    monkeypatch.setenv("ML_SENTRY_POLICY_PATH", "p.yaml")
    monkeypatch.setenv("ML_SENTRY_NOTIFICATION_PROVIDER", "telegram")
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    assert "ML_SENTRY_TELEGRAM_BOT_TOKEN" in str(exc.value)
    assert "ML_SENTRY_TELEGRAM_CHAT_ID" in str(exc.value)
    monkeypatch.setenv("ML_SENTRY_NOTIFICATION_PROVIDER", "webhook")
    monkeypatch.setenv("ML_SENTRY_WEBHOOK_URL", "http://insecure.example")
    monkeypatch.setenv("ML_SENTRY_WEBHOOK_SECRET", "s")
    with pytest.raises(ConfigurationError, match="https"):
        load_settings()


def test_factory_builds_configured_provider(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "notification_provider": "webhook",
            "webhook_url": "https://hooks.example/ml",
            "webhook_secret": __import__("pydantic").SecretStr("s3cret"),
        }
    )
    assert isinstance(make_notifier(configured), WebhookNotifier)
