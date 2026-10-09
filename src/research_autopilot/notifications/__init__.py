"""Workflow notifications.

Configuration (environment variables):

- ``RA_NOTIFY_URL``: base URL of an ntfy-compatible server (e.g. ``https://ntfy.example``).
  If unset, notifications are only written to the log.
- ``RA_NOTIFY_TOPIC``: topic name. Required when ``RA_NOTIFY_URL`` is set.
- ``RA_NOTIFY_TOKEN``: optional bearer token.
"""

import os
from typing import Protocol

from prefect.logging import get_logger

from research_autopilot.notifications.events import EventKind, NotificationEvent
from research_autopilot.notifications.http import HttpNotifier, LoggingNotifier, NotificationError

logger = get_logger("research_autopilot.notifications")


class Notifier(Protocol):
    def send(self, event: NotificationEvent) -> None: ...


def get_notifier(env: dict[str, str] | None = None) -> Notifier:
    env = os.environ if env is None else env
    url = env.get("RA_NOTIFY_URL", "").strip()
    if not url:
        return LoggingNotifier()
    if not url.startswith(("http://", "https://")):
        raise ValueError("RA_NOTIFY_URL must start with http:// or https://")
    topic = env.get("RA_NOTIFY_TOPIC", "").strip()
    if not topic:
        raise ValueError("RA_NOTIFY_TOPIC is required when RA_NOTIFY_URL is set")
    token = env.get("RA_NOTIFY_TOKEN", "").strip() or None
    return HttpNotifier(url, topic, token=token)


def safe_send(event: NotificationEvent, notifier: Notifier | None = None) -> bool:
    """Send a notification without ever raising.

    Job success/failure is decided by Prefect before hooks run; a delivery
    problem must only be logged, never change the run's outcome.
    """
    try:
        (notifier or get_notifier()).send(event)
    except Exception as exc:  # noqa: BLE001 - isolation is the point
        logger.warning("notification failed (%s): %s", type(exc).__name__, event.title())
        return False
    return True


__all__ = [
    "EventKind",
    "HttpNotifier",
    "LoggingNotifier",
    "NotificationError",
    "NotificationEvent",
    "Notifier",
    "get_notifier",
    "safe_send",
]
