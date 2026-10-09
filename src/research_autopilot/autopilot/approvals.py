"""Approval controller: request -> (Linear status signal + Prefect form) -> verified decision.

A decision is accepted only if ALL of these hold:
- a form submission exists for this request id (channel: authenticated Prefect UI);
- the submission's spec hash equals the request's and the task's *current* spec hash (else stale);
- the request is still pending and not expired;
- the Linear issue is in the state matching the decision (Approved / Revision Requested / Canceled);
- that state was entered after Autopilot set "Awaiting Approval", by a non-bot actor, and Autopilot
  never wrote that state itself.
Linear alone cannot prove *who* changed the status (a personal API key acts as the user), so the
form is the authoritative confirmation; the Linear status is the visible signal.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from research_autopilot.autopilot.linear_ops import AWAITING, HUMAN_DECISION_STATES, LinearOps
from research_autopilot.autopilot.models import (
    ApprovalDecision,
    ApprovalKind,
    ApprovalRequest,
    ApprovalStatus,
    FormSubmission,
)
from research_autopilot.models import utcnow
from research_autopilot.storage import db
from research_autopilot.storage.db import Store


class ConfirmationChannel(Protocol):
    def open(self, request: ApprovalRequest) -> str | None:
        """Create the human confirmation form (e.g. a suspended Prefect flow run); returns a reference."""
        ...

    def url(self, ref: str | None) -> str | None: ...


@dataclass
class CheckResult:
    status: ApprovalStatus
    decision: ApprovalDecision | None
    note: str


class ApprovalController:
    def __init__(self, store: Store, linear_ops: LinearOps, channel: ConfirmationChannel,
                 notify: Callable[[ApprovalRequest, str], None] = lambda r, m: None) -> None:
        self.store = store
        self.ops = linear_ops
        self.channel = channel
        self.notify = notify

    # ---------------------------------------------------------------- request

    def request(self, *, kind: ApprovalKind, issue, root_task_id: str, spec_hash: str, title: str,
                what_changes: list[str], diff: list[str], budget_summary: str, payload: dict | None = None,
                dedupe: str | None = None) -> ApprovalRequest:
        """Idempotent per (kind, task, spec hash, dedupe): a repeated call returns the existing request."""
        key = f"{kind.value}:{issue.identifier}:{spec_hash}:{dedupe or ''}"
        for r in self.store.find(db.approval_requests, task_id=issue.identifier):
            if r.get("payload", {}).get("_dedupe") == key and r["status"] == ApprovalStatus.PENDING.value:
                return ApprovalRequest.model_validate(r)
        missing = self.ops.missing_states(issue.team_id)
        if missing:
            raise RuntimeError(f"Linear workflow states missing: {missing} (a human must create them)")
        req = ApprovalRequest(kind=kind, task_id=issue.identifier, root_task_id=root_task_id, spec_hash=spec_hash,
                              title=title, what_changes=what_changes, diff_from_original=diff,
                              budget_summary=budget_summary, linear_state_before=issue.state_name,
                              payload={**(payload or {}), "_dedupe": key})
        if not self.store.insert_new(db.approval_requests, req.request_id, req.model_dump(mode="json"),
                                     task_id=req.task_id, status=req.status.value, dedupe_key=key):
            existing = [r for r in self.store.find(db.approval_requests, task_id=issue.identifier)
                        if r["payload"].get("_dedupe") == key]
            return ApprovalRequest.model_validate(existing[0])
        self.ops.set_state(issue, AWAITING, f"approval request {req.request_id} ({kind.value})")
        req.autopilot_set_awaiting_at = utcnow()
        req.confirmation_ref = self.channel.open(req)
        self._save(req)
        self.notify(req, "requested")
        return req

    # ---------------------------------------------------------------- check

    def check(self, req: ApprovalRequest, issue, history: list[dict], current_spec_hash: str) -> CheckResult:
        if req.status is not ApprovalStatus.PENDING:
            d = self.store.get(db.approval_decisions, req.request_id)
            return CheckResult(req.status, ApprovalDecision.model_validate(d) if d else None, "already decided")
        if req.expires_at < utcnow():
            return self._close(req, ApprovalStatus.EXPIRED, "request expired")
        if current_spec_hash != req.spec_hash:
            return self._close(req, ApprovalStatus.STALE, "task spec changed after the request; a new request is needed")
        subs = [FormSubmission.model_validate(s) for s in self.store.find(db.form_submissions, request_id=req.request_id)]
        decision_state = HUMAN_DECISION_STATES.get(issue.state_name)
        if not subs:
            note = (f"Linear shows {issue.state_name!r}; waiting for confirmation in the Prefect form"
                    if decision_state else "waiting for a human decision")
            return CheckResult(ApprovalStatus.PENDING, None, note)
        sub = subs[-1]
        if sub.spec_hash != req.spec_hash:
            return CheckResult(ApprovalStatus.PENDING, None, "form submission refers to a different spec version; ignored")
        if decision_state != sub.decision:
            return CheckResult(ApprovalStatus.PENDING, None,
                               f"form says {sub.decision!r} but Linear state is {issue.state_name!r}; both must agree")
        ok, why = self._verify_transition(req, issue, history)
        if not ok:
            return CheckResult(ApprovalStatus.PENDING, None, f"Linear transition not accepted: {why}")
        decision = ApprovalDecision(request_id=req.request_id, decision=sub.decision, actor=sub.approver,
                                    channel="prefect_form", linear_state=issue.state_name, spec_hash=req.spec_hash,
                                    comment=sub.comment)
        if not self.store.insert_new(db.approval_decisions, req.request_id, decision.model_dump(mode="json")):
            return CheckResult(ApprovalStatus.PENDING, None, "decision already recorded")
        status = {"approve": ApprovalStatus.APPROVED, "reject": ApprovalStatus.REJECTED,
                  "revise": ApprovalStatus.REVISION_REQUESTED}[sub.decision]
        req.status = status
        self._save(req)
        return CheckResult(status, decision, f"verified {sub.decision} by {sub.approver}")

    def _verify_transition(self, req: ApprovalRequest, issue, history: list[dict]) -> tuple[bool, str]:
        since = req.autopilot_set_awaiting_at or req.created_at
        entered = [h for h in history if (h.get("toState") or {}).get("name") == issue.state_name
                   and _ts(h["createdAt"]) >= since]
        if not entered:
            return False, f"no transition into {issue.state_name!r} after the request was made"
        last = entered[-1]
        if last.get("botActor"):
            return False, f"changed by a bot/integration ({last['botActor']})"
        if not last.get("actor"):
            return False, "actor of the change is unknown"
        own = [w for w in self.store.linear_writes_for(issue.identifier) if w["to_state"] == issue.state_name]
        if own:
            return False, "Autopilot itself wrote this state"
        return True, f"changed by {last['actor'].get('name')}"

    # ---------------------------------------------------------------- helpers

    def pending(self) -> list[ApprovalRequest]:
        return [ApprovalRequest.model_validate(r) for r in self.store.find(db.approval_requests, status="pending")]

    def _close(self, req: ApprovalRequest, status: ApprovalStatus, note: str) -> CheckResult:
        req.status = status
        self._save(req)
        self.notify(req, note)
        return CheckResult(status, None, note)

    def _save(self, req: ApprovalRequest) -> None:
        self.store.put(db.approval_requests, req.request_id, req.model_dump(mode="json"), task_id=req.task_id,
                       status=req.status.value, dedupe_key=req.payload.get("_dedupe", req.request_id))


def record_submission(store: Store, sub: FormSubmission) -> None:
    """Called by the Prefect confirmation flow after the human submitted the form."""
    from research_autopilot.models import new_id

    store.insert_new(db.form_submissions, new_id(), sub.model_dump(mode="json"), request_id=sub.request_id)


def _ts(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00"))
