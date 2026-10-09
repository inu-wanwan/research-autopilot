import httpx
import pytest

from research_autopilot.notifications import (
    HttpNotifier,
    LoggingNotifier,
    NotificationError,
    get_notifier,
    safe_send,
)
from research_autopilot.notifications.events import EventKind, NotificationEvent

TOKEN = "tk_secret_value"

EVENT = NotificationEvent(
    kind=EventKind.FAILED,
    flow_name="smoke-failure",
    flow_run_name="brave-otter",
    flow_run_id="00000000-0000-0000-0000-000000000000",
    state_name="Failed",
    error_type="TransientError",
    ui_url="https://lab.example.ts.net/runs/flow-run/0",
)


def notifier(handler, **kw) -> HttpNotifier:
    return HttpNotifier(
        "https://ntfy.example/", "ra-test", token=TOKEN, retry_delay=0,
        transport=httpx.MockTransport(handler), **kw,
    )


def test_posts_minimal_payload_with_auth_header():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200)

    notifier(handler).send(EVENT)

    (req,) = seen
    assert str(req.url) == "https://ntfy.example/ra-test"
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert req.headers["Priority"] == "high"
    body = req.content.decode()
    assert "smoke-failure" in body and "TransientError" in body
    assert TOKEN not in body and TOKEN not in str(req.url)


def test_event_body_has_no_error_message():
    # Only the exception type is representable; there is no field for messages.
    assert "error_message" not in NotificationEvent.model_fields
    assert "simulated" not in EVENT.body()


def test_retries_server_errors_then_raises():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    with pytest.raises(NotificationError) as exc:
        notifier(handler, max_retries=2).send(EVENT)
    assert len(calls) == 3
    assert TOKEN not in str(exc.value)


def test_does_not_retry_client_errors():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401)

    with pytest.raises(NotificationError):
        notifier(handler, max_retries=2).send(EVENT)
    assert len(calls) == 1


def test_transport_error_is_retried_then_raises():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(NotificationError):
        notifier(handler, max_retries=1).send(EVENT)
    assert len(calls) == 2


def test_safe_send_swallows_failures():
    def handler(request):
        return httpx.Response(500)

    assert safe_send(EVENT, notifier(handler, max_retries=0)) is False


def test_get_notifier_defaults_to_logging():
    assert isinstance(get_notifier({}), LoggingNotifier)


def test_get_notifier_builds_http_notifier():
    n = get_notifier({"RA_NOTIFY_URL": "https://ntfy.example", "RA_NOTIFY_TOPIC": "t"})
    assert isinstance(n, HttpNotifier)


@pytest.mark.parametrize(
    "env",
    [
        {"RA_NOTIFY_URL": "ftp://ntfy.example", "RA_NOTIFY_TOPIC": "t"},
        {"RA_NOTIFY_URL": "https://ntfy.example"},
    ],
)
def test_get_notifier_rejects_invalid_config(env):
    with pytest.raises(ValueError):
        get_notifier(env)
