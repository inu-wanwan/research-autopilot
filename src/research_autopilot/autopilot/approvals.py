"""Approval controller: request -> authenticated human input -> verified decision.

Discord mode accepts only persisted interactions that already passed the guild,
channel and user allowlist checks.  The controller remains authoritative: it
re-checks pending status, expiry and the current Notion spec hash before writing
a decision.  Legacy Linear and Linear+Prefect-form modes remain available only
for migration compatibility.
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
    DiscordSubmission,
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


CLI_DEV = "cli-dev"  # marks a decision recorded by `research-agent research approve` (development only)


def dev_environment() -> bool:
    """RA_ENVIRONMENT=dev: the operator may decide approvals from the CLI (delegated by the user; audited)."""
    import os
    return os.environ.get("RA_ENVIRONMENT", "").strip().lower() == "dev"


def submit_cli_decision(store, req, decision: str, actor: str, comment: str = "") -> DiscordSubmission:
    """Development only: record a decision for a pending request as if from an approver, labelled cli-dev."""
    import uuid
    if not dev_environment():
        raise PermissionError("CLI approval is only available with RA_ENVIRONMENT=dev")
    if not actor.strip():
        raise ValueError("--actor is required (who decides and on whose behalf)")
    if decision == "revise" and not comment.strip():
        raise ValueError("a revise decision needs a comment")
    sub = DiscordSubmission(interaction_id=f"{CLI_DEV}:{uuid.uuid4()}", request_id=req.request_id, decision=decision,
                            spec_hash=req.spec_hash, user_id=actor.strip()[:200], guild_id=CLI_DEV,
                            channel_id=CLI_DEV, comment=comment)
    store.put(db.discord_interactions, sub.interaction_id, sub.model_dump(mode="json"), request_id=req.request_id)
    return sub


class ApprovalController:
    """``mode="linear"``: the Linear status change alone decides, verified by the actor's user id
    (``approver_ids``), non-bot actor, ordering after the request, no own write of that state, unchanged
    spec hash and expiry. ``mode="linear+form"`` additionally requires the Prefect form submission.
    ``mode="discord"`` consumes an authenticated Discord interaction and never treats Linear as proof."""

    def __init__(self, store: Store, linear_ops: LinearOps, channel: ConfirmationChannel | None,
                 notify: Callable[[ApprovalRequest, str], None] = lambda r, m: None, mode: str = "linear+form",
                 approver_ids: set[str] | None = None) -> None:
        if mode not in ("linear", "linear+form", "discord"):
            raise ValueError(f"unknown approval mode {mode}")
        if mode == "linear" and not approver_ids:
            raise ValueError("linear approval mode requires approver Linear user ids (RA_APPROVER_LINEAR_USER_IDS)")
        self.store = store
        self.ops = linear_ops
        self.channel = channel
        self.notify = notify
        self.mode = mode
        self.approver_ids = set(approver_ids or ())

    # ---------------------------------------------------------------- request

    def request(self, *, kind: ApprovalKind, issue, root_task_id: str, spec_hash: str, title: str,
                what_changes: list[str], diff: list[str], budget_summary: str, payload: dict | None = None,
                dedupe: str | None = None) -> ApprovalRequest:
        """Idempotent per (kind, task, spec hash, dedupe) while the request is pending: a repeated call returns it.

        Once that request was decided or closed (budget hit again at the same stage, onboarding re-run after a
        rejection or expiry), a new version with a sequence-suffixed key is created -- returning the decided
        request would leave the caller waiting forever.  Older stale / expired requests of the same task and
        kind record the new request as their replacement (``_superseded_by``) so their messages can point to it."""
        base = f"{kind.value}:{issue.identifier}:{spec_hash}:{dedupe or ''}"
        same = [r for r in self.store.find(db.approval_requests, task_id=issue.identifier)
                if r.get("payload", {}).get("_dedupe_base", r.get("payload", {}).get("_dedupe")) == base]
        for r in same:
            if r["status"] == ApprovalStatus.PENDING.value:
                return ApprovalRequest.model_validate(r)
        key = base if not same else f"{base}#{len(same) + 1}"
        missing = self.ops.missing_states(issue.team_id)
        if self.mode == "discord":
            missing = [state for state in missing if state != "Approved"]
        if missing:
            raise RuntimeError(f"Linear workflow states missing: {missing} (a human must create them)")
        req = ApprovalRequest(kind=kind, task_id=issue.identifier, root_task_id=root_task_id, spec_hash=spec_hash,
                              title=title, what_changes=what_changes, diff_from_original=diff,
                              budget_summary=budget_summary, linear_state_before=issue.state_name,
                              payload={**(payload or {}), "_dedupe": key, "_dedupe_base": base,
                                       "_version": len(same) + 1})
        if not self.store.insert_new(db.approval_requests, req.request_id, req.model_dump(mode="json"),
                                     task_id=req.task_id, status=req.status.value, dedupe_key=key):
            existing = [r for r in self.store.find(db.approval_requests, task_id=issue.identifier)
                        if r["payload"].get("_dedupe") == key]
            return ApprovalRequest.model_validate(existing[0])
        self._link_superseded(req)
        self.ops.set_state(issue, AWAITING, f"approval request {req.request_id} ({kind.value})")
        req.autopilot_set_awaiting_at = utcnow()
        if self.mode == "linear+form" and self.channel is not None:
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
        decision_state = HUMAN_DECISION_STATES.get(issue.state_name)
        if self.mode == "discord":
            subs = [DiscordSubmission.model_validate(s)
                    for s in self.store.find(db.discord_interactions, request_id=req.request_id)]
            if not subs:
                return CheckResult(ApprovalStatus.PENDING, None, "waiting for an authorized Discord interaction")
            if not dev_environment():  # a dev-only CLI decision never counts outside dev
                subs = [x for x in subs if x.guild_id != CLI_DEV]
                if not subs:
                    return CheckResult(ApprovalStatus.PENDING, None, "waiting for an authorized Discord interaction")
            sub = subs[-1]
            if sub.spec_hash != req.spec_hash:
                return CheckResult(ApprovalStatus.PENDING, None,
                                   "Discord interaction refers to a different spec version; ignored")
            if sub.guild_id == CLI_DEV:
                return self._decide(req, sub.decision, f"cli-dev:{sub.user_id}", "cli_dev", issue.state_name,
                                    sub.comment)
            return self._decide(req, sub.decision, f"discord:{sub.user_id}", "discord", issue.state_name, sub.comment)
        if self.mode == "linear":
            if not decision_state:
                return CheckResult(ApprovalStatus.PENDING, None, "waiting for a human Linear status change")
            ok, who = self._verify_transition(req, issue, history)
            if not ok:
                return CheckResult(ApprovalStatus.PENDING, None, f"Linear transition not accepted: {who}")
            return self._decide(req, decision_state, who, "linear_status", issue.state_name, "")
        subs = [FormSubmission.model_validate(s) for s in self.store.find(db.form_submissions, request_id=req.request_id)]
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
        return self._decide(req, sub.decision, sub.approver, "prefect_form", issue.state_name, sub.comment)

    def _decide(self, req: ApprovalRequest, decision_value: str, actor: str, channel: str, linear_state: str,
                comment: str) -> CheckResult:
        decision = ApprovalDecision(request_id=req.request_id, decision=decision_value, actor=actor, channel=channel,
                                    linear_state=linear_state, spec_hash=req.spec_hash, comment=comment)
        if not self.store.insert_new(db.approval_decisions, req.request_id, decision.model_dump(mode="json")):
            existing = ApprovalDecision.model_validate(self.store.get(db.approval_decisions, req.request_id))
            status = {"approve": ApprovalStatus.APPROVED, "reject": ApprovalStatus.REJECTED,
                      "revise": ApprovalStatus.REVISION_REQUESTED}[existing.decision]
            req.status = status
            self._save(req)
            return CheckResult(status, existing, "decision already recorded")
        status = {"approve": ApprovalStatus.APPROVED, "reject": ApprovalStatus.REJECTED,
                  "revise": ApprovalStatus.REVISION_REQUESTED}[decision_value]
        req.status = status
        self._save(req)
        return CheckResult(status, decision, f"verified {decision_value} by {actor} via {channel}")

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
        if self.approver_ids and last["actor"].get("id") not in self.approver_ids:
            return False, f"changed by {last['actor'].get('name')!r}, who is not an authorized approver"
        own = [w for w in self.store.linear_writes_for(issue.identifier) if w["to_state"] == issue.state_name]
        if own:
            return False, "Autopilot itself wrote this state"
        return True, f"{last['actor'].get('name')} ({last['actor'].get('id')})"

    # ---------------------------------------------------------------- helpers

    def pending(self) -> list[ApprovalRequest]:
        return [ApprovalRequest.model_validate(r) for r in self.store.find(db.approval_requests, status="pending")]

    def withdraw(self, req: ApprovalRequest, note: str) -> None:
        """Close a pending request for good (e.g. its research was cancelled): a late Approve then creates nothing,
        and no replacement follows (``_final``), so the gateway edits the message without promising a new one."""
        if req.status is not ApprovalStatus.PENDING:
            return
        req.status = ApprovalStatus.STALE
        req.payload.update({"withdrawn": note[:300], "_final": True})
        self._save(req)

    def _link_superseded(self, new: ApprovalRequest) -> None:
        for r in self.store.find(db.approval_requests, task_id=new.task_id):
            if (r["request_id"] != new.request_id and r.get("kind") == new.kind.value
                    and r["status"] in (ApprovalStatus.STALE.value, ApprovalStatus.EXPIRED.value)
                    and not r.get("payload", {}).get("_superseded_by") and not r.get("payload", {}).get("_final")):
                old = ApprovalRequest.model_validate(r)
                old.payload["_superseded_by"] = new.request_id
                self._save(old)
                new.payload.setdefault("_supersedes", old.request_id)
        if "_supersedes" in new.payload:
            self._save(new)

    def _close(self, req: ApprovalRequest, status: ApprovalStatus, note: str) -> CheckResult:
        req.status = status
        req.payload["_closed_note"] = note[:300]
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
