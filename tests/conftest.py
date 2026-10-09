import pytest
from prefect.testing.utilities import prefect_test_harness

from research_autopilot import notifications
from research_autopilot.notifications.events import NotificationEvent


@pytest.fixture(autouse=True, scope="session")
def prefect_backend():
    with prefect_test_harness():
        yield


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    path = tmp_path / "state"
    monkeypatch.setenv("RA_STATE_DIR", str(path))
    return path


class RecordingNotifier:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[NotificationEvent] = []
        self.fail = fail

    def send(self, event: NotificationEvent) -> None:
        self.events.append(event)
        if self.fail:
            raise RuntimeError("delivery failed")


@pytest.fixture
def recorder(monkeypatch):
    rec = RecordingNotifier()
    monkeypatch.setattr(notifications, "get_notifier", lambda: rec)
    return rec


@pytest.fixture
def failing_notifier(monkeypatch):
    rec = RecordingNotifier(fail=True)
    monkeypatch.setattr(notifications, "get_notifier", lambda: rec)
    return rec
