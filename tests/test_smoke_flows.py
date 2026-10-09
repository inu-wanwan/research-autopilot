from research_autopilot.flows.smoke_test import (
    TransientError,
    smoke_failure,
    smoke_success,
    smoke_timeout,
)
from research_autopilot.notifications.events import EventKind


def test_success_completes_and_notifies(recorder):
    state = smoke_success(message="hi", return_state=True)
    assert state.is_completed()
    assert state.result() == "applied"
    assert [e.kind for e in recorder.events] == [EventKind.COMPLETED]
    assert recorder.events[0].flow_name == "smoke-success"


def test_duplicate_runs_apply_side_effect_once(state_dir, recorder):
    first = smoke_success(message="hi", operation_id="dup-op")
    second = smoke_success(message="hi", operation_id="dup-op")
    assert (first, second) == ("applied", "already_applied")
    assert [p.name for p in state_dir.iterdir()] == ["dup-op.json"]


def test_failure_retries_then_fails_and_notifies(recorder):
    state = smoke_failure(fail_times=3, return_state=True)
    assert state.is_failed()
    assert isinstance(state.result(raise_on_failure=False), TransientError)
    assert [e.kind for e in recorder.events] == [EventKind.FAILED]
    assert recorder.events[0].error_type == "TransientError"


def test_transient_failure_recovers_on_retry(recorder):
    # attempt 1 fails, attempt 2 (first retry) succeeds
    assert smoke_failure(fail_times=1) == 2
    assert [e.kind for e in recorder.events] == [EventKind.COMPLETED]


def test_timeout_fails_run(recorder):
    state = smoke_timeout(sleep_seconds=5, return_state=True)
    assert state.is_failed()
    assert state.name == "TimedOut"
    assert [e.kind for e in recorder.events] == [EventKind.FAILED]


def test_notification_failure_does_not_change_run_state(failing_notifier):
    state = smoke_success(message="hi", return_state=True)
    assert state.is_completed()
    assert len(failing_notifier.events) == 1  # attempted, failed, isolated


def test_sleep_flow_completes(recorder):
    from research_autopilot.flows.smoke_test import smoke_sleep

    assert smoke_sleep(seconds=1) == 1
