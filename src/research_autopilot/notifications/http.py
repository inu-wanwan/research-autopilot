"""HTTP notification adapter (ntfy-compatible).

Sends ``POST {base_url}/{topic}`` with the event body as plain text and the
title/priority as headers. The token is sent only in the Authorization header
and is never logged.
"""

import time

import httpx
from prefect.logging import get_logger

from research_autopilot.notifications.events import EventKind, NotificationEvent

logger = get_logger("research_autopilot.notifications")

_PRIORITY = {
    EventKind.COMPLETED: "default",
    EventKind.FAILED: "high",
    EventKind.APPROVAL_REQUIRED: "high",
}


class NotificationError(Exception):
    """Raised when a notification could not be delivered after retries."""


class HttpNotifier:
    def __init__(
        self,
        base_url: str,
        topic: str,
        token: str | None = None,
        timeout: float = 5.0,
        max_retries: int = 2,
        retry_delay: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/{topic}"
        self._token = token
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._transport = transport

    def send(self, event: NotificationEvent) -> None:
        priority = _PRIORITY.get(event.kind, "high" if event.kind.value.endswith(("error", "failure")) else "default")
        headers = {"Title": event.title(), "Priority": priority}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        last_error: str | None = None
        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            for attempt in range(self._max_retries + 1):
                try:
                    response = client.post(self._url, content=event.body(), headers=headers)
                except httpx.TransportError as exc:
                    last_error = type(exc).__name__
                else:
                    if response.status_code < 400:
                        return
                    last_error = f"HTTP {response.status_code}"
                    # Client errors (bad token, bad topic) will not succeed on retry.
                    if response.status_code < 500:
                        break
                if attempt < self._max_retries:
                    time.sleep(self._retry_delay)

        raise NotificationError(f"notification '{event.kind.value}' not delivered: {last_error}")


class LoggingNotifier:
    """Fallback used when no HTTP endpoint is configured."""

    def send(self, event: NotificationEvent) -> None:
        logger.info("notification (no endpoint configured): %s | %s", event.title(), event.body())
