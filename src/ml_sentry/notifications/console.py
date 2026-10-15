"""Safe default sinks and the provider factory."""

from __future__ import annotations

import logging

import httpx

from ml_sentry.notifications.base import IncidentNotification, Notifier
from ml_sentry.settings import Settings

logger = logging.getLogger("ml_sentry.notifications")


class ConsoleNotifier:
    name = "console"

    def send_incident(self, notification: IncidentNotification) -> None:
        logger.warning("%s", notification.render_text())


class NullNotifier:
    """Configured with ML_SENTRY_NOTIFICATION_PROVIDER=none: deliveries are skipped."""

    name = "none"

    def send_incident(self, notification: IncidentNotification) -> None:
        return None


def make_notifier(settings: Settings, client: httpx.Client | None = None) -> Notifier:
    from ml_sentry.notifications.http import SlackNotifier, TelegramNotifier, WebhookNotifier

    provider = settings.notification_provider
    if provider == "console":
        return ConsoleNotifier()
    if provider == "none":
        return NullNotifier()
    # Settings validation guarantees the provider's values are present.
    if provider == "slack" and settings.slack_webhook_url:
        return SlackNotifier(settings.slack_webhook_url.get_secret_value(), client)
    if provider == "telegram" and settings.telegram_bot_token and settings.telegram_chat_id:
        return TelegramNotifier(
            settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id, client
        )
    if provider == "webhook" and settings.webhook_url and settings.webhook_secret:
        return WebhookNotifier(
            settings.webhook_url, settings.webhook_secret.get_secret_value(), client
        )
    raise ValueError(f"notification provider '{provider}' is not configured")
