"""Prefect flow state hooks that emit notifications.

Hooks run after Prefect has settled the final state, and every hook goes
through ``safe_send`` so a notification failure cannot alter that state.
"""

from typing import Any

from prefect.client.schemas.objects import FlowRun
from prefect.flows import Flow
from prefect.states import State
from prefect.utilities.urls import url_for

from research_autopilot.notifications import safe_send
from research_autopilot.notifications.events import EventKind, NotificationEvent


def _error_type(state: State) -> str | None:
    try:
        result = state.result(raise_on_failure=False)
    except Exception:  # noqa: BLE001 - result may be unavailable; type is best-effort
        return None
    return type(result).__name__ if isinstance(result, BaseException) else None


def build_event(kind: EventKind, flow: Flow[Any, Any], flow_run: FlowRun, state: State) -> NotificationEvent:
    return NotificationEvent(
        kind=kind,
        flow_name=flow.name,
        flow_run_name=flow_run.name,
        flow_run_id=str(flow_run.id),
        state_name=state.name,
        error_type=_error_type(state) if kind is EventKind.FAILED else None,
        ui_url=url_for(flow_run),
    )


def notify_completed(flow: Flow[Any, Any], flow_run: FlowRun, state: State) -> None:
    safe_send(build_event(EventKind.COMPLETED, flow, flow_run, state))


def notify_failed(flow: Flow[Any, Any], flow_run: FlowRun, state: State) -> None:
    safe_send(build_event(EventKind.FAILED, flow, flow_run, state))
