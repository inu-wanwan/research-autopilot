"""Shared HTTP call with bounded retries for the Notion / Linear connectors."""

import random
import time
from collections.abc import Callable

import httpx

from research_autopilot.tasks.errors import (
    ConnectorAuthError,
    ConnectorResponseError,
    NotFoundError,
    TransientConnectorError,
)

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class RetryPolicy:
    def __init__(self, attempts: int = 3, base_delay: float = 1.0, max_delay: float = 8.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.attempts = attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.sleep = sleep

    def delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self.max_delay)
            except ValueError:
                pass
        return min(self.base_delay * 2 ** (attempt - 1), self.max_delay) * (1 + random.random() * 0.25)


def request(
    client: httpx.Client,
    service: str,
    method: str,
    url: str,
    policy: RetryPolicy,
    on_retry: Callable[[str], None] | None = None,
    **kwargs,
) -> httpx.Response:
    """Return a 2xx response or raise a typed ConnectorError.

    Only network errors and RETRYABLE_STATUS are retried. Error messages
    contain the status code and API error code, never headers or bodies
    that could echo credentials.
    """
    last = ""
    for attempt in range(1, policy.attempts + 1):
        retry_after = None
        try:
            resp = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            last = type(exc).__name__
        else:
            if resp.status_code < 300:
                return resp
            code = _api_code(resp)
            if resp.status_code in (401, 403):
                raise ConnectorAuthError(service, f"HTTP {resp.status_code} {code}")
            if resp.status_code == 404:
                raise NotFoundError(service, f"HTTP 404 {code}")
            if resp.status_code not in RETRYABLE_STATUS:
                raise ConnectorResponseError(service, f"HTTP {resp.status_code} {code}")
            last = f"HTTP {resp.status_code} {code}"
            retry_after = resp.headers.get("retry-after")
        if attempt < policy.attempts:
            if on_retry:
                on_retry(f"{service} {method} attempt {attempt} failed ({last}); retrying")
            policy.sleep(policy.delay(attempt, retry_after))
    raise TransientConnectorError(service, f"gave up after {policy.attempts} attempts: {last}")


def _api_code(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        return str(body.get("code") or (body.get("errors") or [{}])[0].get("extensions", {}).get("code", ""))
    return ""
