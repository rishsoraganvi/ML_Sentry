"""Slack, Telegram and generic HTTPS webhook providers.

Error codes are derived from status codes and exception *types* only: request URLs
(the Telegram URL embeds the bot token) must never reach logs or the database.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import asdict

import httpx

from ml_sentry.notifications.base import IncidentNotification, NotificationError

TIMEOUT = httpx.Timeout(10.0)


def _post(client: httpx.Client, url: str, **kwargs: object) -> httpx.Response:
    try:
        response = client.post(url, timeout=TIMEOUT, **kwargs)  # type: ignore[arg-type]
    except httpx.HTTPError as exc:
        raise NotificationError(f"HTTP_{type(exc).__name__.upper()}") from None
    if response.status_code >= 400:
        raise NotificationError(f"HTTP_{response.status_code}")
    return response


class SlackNotifier:
    """Slack incoming webhook: https://api.slack.com/messaging/webhooks"""

    name = "slack"

    def __init__(self, webhook_url: str, client: httpx.Client | None = None) -> None:
        self._url = webhook_url
        self._client = client or httpx.Client()

    def send_incident(self, notification: IncidentNotification) -> None:
        _post(self._client, self._url, json={"text": notification.render_text()})


class TelegramNotifier:
    """Telegram Bot API sendMessage: https://core.telegram.org/bots/api#sendmessage"""

    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, client: httpx.Client | None = None) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._client = client or httpx.Client()

    def send_incident(self, notification: IncidentNotification) -> None:
        response = _post(
            self._client,
            self._url,
            json={
                "chat_id": self._chat_id,
                "text": notification.render_text()[:4096],
                "disable_web_page_preview": True,
            },
        )
        try:
            ok = response.json().get("ok") is True
        except ValueError:
            ok = False
        if not ok:
            raise NotificationError("TELEGRAM_NOT_OK")


class WebhookNotifier:
    """POSTs JSON signed with HMAC-SHA256 over "<timestamp>.<body>".

    Headers: X-ML-Sentry-Timestamp, X-ML-Sentry-Signature: sha256=<hex>.
    Receivers should verify the signature and reject old timestamps.
    """

    name = "webhook"

    def __init__(self, url: str, secret: str, client: httpx.Client | None = None) -> None:
        self._url = url
        self._secret = secret.encode()
        self._client = client or httpx.Client()

    def sign(self, timestamp: str, body: bytes) -> str:
        mac = hmac.new(self._secret, timestamp.encode() + b"." + body, hashlib.sha256)
        return f"sha256={mac.hexdigest()}"

    def send_incident(self, notification: IncidentNotification) -> None:
        body = json.dumps(
            {"type": f"ml_sentry.{notification.kind}", **asdict(notification)}, sort_keys=True
        ).encode()
        timestamp = str(int(time.time()))
        _post(
            self._client,
            self._url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-ML-Sentry-Timestamp": timestamp,
                "X-ML-Sentry-Signature": self.sign(timestamp, body),
            },
        )
