"""Typed notification events.

Events carry only identifiers and state names. Exception messages, logs,
parameters and environment values are deliberately excluded so that
notifications cannot leak secrets.
"""

from enum import StrEnum

from hashlib import sha256

from pydantic import BaseModel, Field


class EventKind(StrEnum):
    # Legacy Prefect hook events. Routing also considers the flow name.
    COMPLETED = "completed"
    FAILED = "failed"
    APPROVAL_REQUIRED = "approval_required"
    EXPERIMENT_COMPLETED = "experiment_completed"
    ANALYSIS_COMPLETED = "analysis_completed"
    TASK_COMPLETED = "task_completed"
    UNRECOVERABLE_ERROR = "unrecoverable_error"
    BUDGET_EXCEEDED = "budget_exceeded"
    WORKER_FAILURE = "worker_failure"
    TASK_STARTED = "task_started"
    PHASE_TRANSITION = "phase_transition"
    AUTOMATIC_RETRY = "automatic_retry"
    REPAIR_SUCCEEDED = "repair_succeeded"
    PROPOSAL_GENERATED = "proposal_generated"


def numbered(steps: list[str]) -> list[str]:
    """``1. ...`` lines; callers write the steps without numbers."""
    return [f"{i}. {s}" for i, s in enumerate(steps, start=1)]


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
    details: list[str] = Field(default_factory=list)
    dedupe_key: str | None = None
    approval_request_id: str | None = None
    control_task_id: str | None = None  # Phase 10: research run that Stop / Resume buttons act on
    control_actions: list[str] = Field(default_factory=list)  # which buttons ("stop" / "resume") the message offers
    needs_human: bool = False  # route to approvals: a human must act
    steps: list[str] = Field(default_factory=list)  # numbered actions until the run resumes

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
        if self.steps:
            lines.append("次にやること:")
            lines += numbered(self.steps)
        return "\n".join(lines)

    def stable_dedupe_key(self) -> str:
        """Return a restart-stable key without putting secrets in the key."""
        if self.dedupe_key:
            return self.dedupe_key
        raw = "|".join((self.kind.value, self.flow_name, self.flow_run_id or "",
                        self.flow_run_name or "", self.state_name or ""))
        return f"event:{sha256(raw.encode()).hexdigest()}"
