"""Typed notification events.

Events carry only identifiers and state names. Exception messages, logs,
parameters and environment values are deliberately excluded so that
notifications cannot leak secrets.
"""

from enum import StrEnum

from pydantic import BaseModel


class EventKind(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    APPROVAL_REQUIRED = "approval_required"


class NotificationEvent(BaseModel):
    kind: EventKind
    flow_name: str
    flow_run_name: str | None = None
    flow_run_id: str | None = None
    state_name: str | None = None
    # Exception class name only (e.g. "TimeoutError"), never the message.
    error_type: str | None = None
    ui_url: str | None = None
    # Extra human-readable lines (links, what changes, budget). Callers must not put secrets here.
    details: list[str] = []

    def title(self) -> str:
        return f"[Research Autopilot] {self.flow_name}: {self.kind.value}"

    def body(self) -> str:
        lines = [f"flow: {self.flow_name}"]
        if self.flow_run_name:
            lines.append(f"run: {self.flow_run_name}")
        if self.state_name:
            lines.append(f"state: {self.state_name}")
        if self.error_type:
            lines.append(f"error: {self.error_type}")
        if self.ui_url:
            lines.append(self.ui_url)
        lines += self.details
        return "\n".join(lines)
