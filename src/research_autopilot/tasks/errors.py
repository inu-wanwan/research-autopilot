"""Connector error taxonomy.

The orchestrator maps these to WorkflowDecision states:
TransientConnectorError -> transient_failure; the rest -> blocked.
Messages never include credentials or request headers.
"""


class ConnectorError(Exception):
    """Base class. ``service`` is "notion" or "linear"."""

    def __init__(self, service: str, message: str) -> None:
        super().__init__(f"{service}: {message}")
        self.service = service


class TransientConnectorError(ConnectorError):
    """Timeouts, connection errors, HTTP 429 / 5xx after bounded retries."""


class ConnectorAuthError(ConnectorError):
    """HTTP 401 / 403: bad credentials or missing access. Retrying will not help."""


class NotFoundError(ConnectorError):
    """The requested issue / page does not exist or is not shared with the integration."""


class ConnectorResponseError(ConnectorError):
    """Unexpected response shape or a non-retryable API error."""
