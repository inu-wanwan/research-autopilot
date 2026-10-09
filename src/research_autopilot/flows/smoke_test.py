"""Smoke-test flows for the Phase 1 execution infrastructure.

- ``smoke_success``: normal path with an idempotent side effect.
- ``smoke_failure``: transient-failure task with bounded retries; fails by default.
- ``smoke_timeout``: task exceeding its timeout.
- ``approval_demo``: sends an approval-required notification and pauses.

Run locally (ephemeral Prefect server) with
``uv run python -m research_autopilot.flows.smoke_test <success|failure|timeout>``.
"""

import sys
import time
from typing import Literal

from prefect import flow, get_run_logger, task
from prefect.flow_runs import pause_flow_run
from prefect.runtime import flow_run as runtime_flow_run
from prefect.runtime import task_run as runtime_task_run
from prefect.tasks import exponential_backoff

from research_autopilot.idempotency import ApplyResult, apply_once, state_dir_from_env
from research_autopilot.notifications import safe_send
from research_autopilot.notifications.events import EventKind, NotificationEvent
from research_autopilot.notifications.hooks import notify_completed, notify_failed

HOOKS = {
    "on_completion": [notify_completed],
    "on_failure": [notify_failed],
    "on_crashed": [notify_failed],
}

TASK_TIMEOUT_SECONDS = 2


class TransientError(Exception):
    """Simulated temporary failure (e.g. a network blip)."""


@task
def record_side_effect(operation_id: str, message: str) -> ApplyResult:
    logger = get_run_logger()
    result = apply_once(state_dir_from_env(), operation_id, {"message": message})
    logger.info("side effect %s: %s", operation_id, result)
    return result


@flow(name="smoke-success", **HOOKS)
def smoke_success(message: str = "hello", operation_id: str | None = None) -> ApplyResult:
    """Normal path. Without ``operation_id`` the flow run id is used, so a
    manual retry of the same run does not repeat the side effect."""
    logger = get_run_logger()
    op_id = operation_id or f"smoke-success-{runtime_flow_run.id}"
    logger.info("parameters: message=%r operation_id=%s", message, op_id)
    return record_side_effect(op_id, message)


@task(
    retries=2,
    retry_delay_seconds=exponential_backoff(backoff_factor=1),
    retry_jitter_factor=0.5,
    retry_condition_fn=lambda task, task_run, state: isinstance(
        state.result(raise_on_failure=False), TransientError
    ),
)
def flaky_step(fail_times: int) -> int:
    logger = get_run_logger()
    attempt = runtime_task_run.run_count
    logger.info("attempt %d (fails while attempt <= %d)", attempt, fail_times)
    if attempt <= fail_times:
        raise TransientError(f"simulated transient failure on attempt {attempt}")
    return attempt


@flow(name="smoke-failure", **HOOKS)
def smoke_failure(fail_times: int = 3) -> int:
    """With the default ``fail_times=3`` all 3 attempts (1 + 2 retries) fail
    and the run ends Failed. ``fail_times=1`` recovers on the first retry."""
    return flaky_step(fail_times)


@task(timeout_seconds=TASK_TIMEOUT_SECONDS)
def slow_step(sleep_seconds: float) -> float:
    time.sleep(sleep_seconds)
    return sleep_seconds


@flow(name="smoke-timeout", **HOOKS)
def smoke_timeout(sleep_seconds: float = 5) -> float:
    """Fails when ``sleep_seconds`` exceeds the task timeout."""
    return slow_step(sleep_seconds)


@flow(name="approval-demo", **HOOKS)
def approval_demo(timeout_seconds: int = 3600) -> str:
    """Approval-equivalent event: notify, then pause until resumed from the UI.
    If nobody resumes within ``timeout_seconds`` the run fails."""
    logger = get_run_logger()
    safe_send(
        NotificationEvent(
            kind=EventKind.APPROVAL_REQUIRED,
            flow_name="approval-demo",
            flow_run_name=runtime_flow_run.name,
            flow_run_id=runtime_flow_run.id,
            ui_url=runtime_flow_run.ui_url,
        )
    )
    logger.info("waiting for approval (resume this run in the Prefect UI)")
    pause_flow_run(timeout=timeout_seconds)
    logger.info("approved")
    return "approved"


def main(which: Literal["success", "failure", "timeout"]) -> None:
    flows = {"success": smoke_success, "failure": smoke_failure, "timeout": smoke_timeout}
    state = flows[which](return_state=True)
    print(f"{which}: {state.name}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "success")  # type: ignore[arg-type]
