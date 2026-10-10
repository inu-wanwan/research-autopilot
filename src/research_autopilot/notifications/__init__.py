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


def get_notifier(env: dict[str, str] | None = None, store=None) -> Notifier:
    env = os.environ if env is None else env
    backend = env.get("RA_NOTIFICATION_BACKEND", "").strip().lower()
    if backend == "discord":
        from research_autopilot.config import load_settings
        from research_autopilot.notifications.discord import DiscordNotifier, load_discord_settings
        from research_autopilot.storage.db import Store

        store = store or Store(load_settings(dict(env)).resolved_database_url())
        return DiscordNotifier(store, load_discord_settings(dict(env)))
    if backend not in ("", "ntfy", "logging"):
        raise ValueError("RA_NOTIFICATION_BACKEND must be discord, ntfy, or logging")
    if backend == "logging":
        return LoggingNotifier()
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


def notify_once(store, event: NotificationEvent, notifier: Notifier | None = None) -> bool:
    """Persist dedupe state and retry failed legacy delivery on a later call.

    DiscordNotifier owns its durable outbox. ntfy/logging delivery is marked sent
    only after success, so a temporary failure never becomes a false dedupe hit.
    """
    if notifier is None:
        try:
            notifier = get_notifier(store=store)
        except TypeError as exc:
            # Compatibility with simple zero-argument notifier factories used by
            # existing callers/tests during the backend migration.
            if "unexpected keyword argument 'store'" not in str(exc):
                raise
            notifier = get_notifier()
    if getattr(notifier, "persistent", False):
        return safe_send(event, notifier)
    from research_autopilot.storage import db

    key = event.stable_dedupe_key()
    existing = store.get(db.notifications_sent, key)
    if existing and existing.get("state") == "sent":
        return True
    record = {"state": "pending", "kind": event.kind.value, "event": event.model_dump(mode="json")}
    store.put(db.notifications_sent, key, record)
    delivered = safe_send(event, notifier)
    record["state"] = "sent" if delivered else "failed"
    store.put(db.notifications_sent, key, record)
    return delivered


__all__ = [
    "EventKind",
    "HttpNotifier",
    "LoggingNotifier",
    "NotificationError",
    "NotificationEvent",
    "Notifier",
    "get_notifier",
    "notify_once",
    "safe_send",
]
