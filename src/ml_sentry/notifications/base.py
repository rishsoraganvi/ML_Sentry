"""Notification provider interface.

A notification is information only. Delivering (or failing to deliver) one never
changes a governance decision and never grants an approval: approvals happen only
through the authenticated approval API/UI that the notification links to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class NotificationError(Exception):
    """Delivery failed. ``code`` is a short, stable, secret-free identifier."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code[:64]


@dataclass(frozen=True)
class IncidentNotification:
    incident_id: str
    severity: str
    model_name: str
    model_version: str
    status: str
    proposed_action: str
    summary: str
    link: str
    # incident | approval_request | test
    kind: str = "incident"

    def render_text(self) -> str:
        if self.kind == "approval_request":
            head = f"[ML Sentry] APPROVAL NEEDED for incident {self.incident_id}"
        elif self.kind == "test":
            head = "[ML Sentry] Test notification"
        else:
            head = f"[ML Sentry] {self.severity.upper()} incident {self.incident_id}"
        return (
            f"{head}\n"
            f"Model: {self.model_name} {self.model_version}\n"
            f"Status: {self.status} | Proposed action: {self.proposed_action}\n"
            f"{self.summary}\n"
            f"Details: {self.link}"
        )


class Notifier(Protocol):
    name: str

    def send_incident(self, notification: IncidentNotification) -> None:
        """Deliver once; raise NotificationError on failure."""
