"""Phase 6A explicit Notion request ingestion and execution projection.

The Notion checkbox has no event id and the API has no compare-and-swap.  The
database unique key plus the poll lease are therefore authoritative; a stale
Notion acknowledgement is retried and never creates another run.
"""
from datetime import datetime
from enum import StrEnum
from pydantic import BaseModel, Field
from research_autopilot.models import new_id, utcnow
from research_autopilot.storage import db

class ExecutionStatus(StrEnum):
    IDLE="Idle"; REQUESTED="Requested"; ACCEPTED="Accepted"; RUNNING="Running"; AWAITING_APPROVAL="Awaiting Approval"; BLOCKED="Blocked"; COMPLETED="Completed"; FAILED="Failed"; CANCELED="Canceled"

ALLOWED = {ExecutionStatus.IDLE:{ExecutionStatus.REQUESTED}, ExecutionStatus.REQUESTED:{ExecutionStatus.ACCEPTED,ExecutionStatus.BLOCKED}, ExecutionStatus.ACCEPTED:{ExecutionStatus.RUNNING,ExecutionStatus.AWAITING_APPROVAL,ExecutionStatus.BLOCKED}, ExecutionStatus.RUNNING:{ExecutionStatus.AWAITING_APPROVAL,ExecutionStatus.COMPLETED,ExecutionStatus.FAILED,ExecutionStatus.BLOCKED,ExecutionStatus.CANCELED}, ExecutionStatus.AWAITING_APPROVAL:{ExecutionStatus.RUNNING,ExecutionStatus.CANCELED,ExecutionStatus.BLOCKED}}

def transition(current: ExecutionStatus, target: ExecutionStatus) -> ExecutionStatus:
    """Reject invalid state changes instead of silently rewriting history."""
    if current == target:
        return target
    if target not in ALLOWED.get(current, set()):
        raise ValueError(f"invalid execution transition: {current.value} -> {target.value}")
    return target

class ExecutionRequest(BaseModel):
    request_id:str=Field(default_factory=new_id); notion_task_id:str; linear_issue_id:str; source:str="notion_button"
    observed_at:datetime=Field(default_factory=utcnow); specification_hash:str; status:ExecutionStatus=ExecutionStatus.REQUESTED
    blocked_reason:str|None=None; created_at:datetime=Field(default_factory=utcnow); updated_at:datetime=Field(default_factory=utcnow)
    request_key: str
class ExecutionRun(BaseModel):
    execution_run_id:str=Field(default_factory=new_id); request_id:str; task_id:str; notion_task_id:str
    prefect_flow_run_id:str|None=None; status:ExecutionStatus=ExecutionStatus.ACCEPTED; started_at:datetime|None=None; finished_at:datetime|None=None
    error_summary:str|None=None; retry_metadata:dict=Field(default_factory=dict); created_at:datetime=Field(default_factory=utcnow); updated_at:datetime=Field(default_factory=utcnow)

def request_key(page) -> str:
    return f"{page.page_id}:{page.last_edited_time}"

def record_blocked(store, *, page, issue, spec_hash: str, reason: str) -> ExecutionRequest:
    req = ExecutionRequest(notion_task_id=page.page_id, linear_issue_id=issue.identifier, specification_hash=spec_hash,
                           status=ExecutionStatus.BLOCKED, blocked_reason=reason, request_key=request_key(page))
    if store.insert_new(db.execution_requests, req.request_id, req.model_dump(mode="json"), notion_task_id=page.page_id,
                        linear_issue_id=issue.identifier, status=req.status.value, dedupe_key=req.request_key):
        return req
    return next(ExecutionRequest.model_validate(x) for x in store.find(db.execution_requests)
                if x.get("request_key") == req.request_key)

def accept(store, *, page, issue, spec_hash):
    """Atomic DB acceptance. Existing open request/run wins every ambiguous race."""
    open_runs=[ExecutionRun.model_validate(x) for x in store.find(db.execution_runs, task_id=issue.identifier) if x.get('status') in {ExecutionStatus.ACCEPTED,ExecutionStatus.RUNNING,ExecutionStatus.AWAITING_APPROVAL}]
    if open_runs: return None, "task already has an active execution"
    req=ExecutionRequest(notion_task_id=page.page_id, linear_issue_id=issue.identifier, specification_hash=spec_hash,
                         request_key=request_key(page))
    # Boolean means one outstanding request. Same observed page/version is idempotent.
    if not store.insert_new(db.execution_requests, req.request_id, req.model_dump(mode='json'), notion_task_id=page.page_id, linear_issue_id=issue.identifier, status=req.status.value, dedupe_key=req.request_key):
        existing = next((ExecutionRequest.model_validate(x) for x in store.find(db.execution_requests) if x.get("request_key") == req.request_key), None)
        if existing and existing.status is ExecutionStatus.ACCEPTED:
            run = next((ExecutionRun.model_validate(x) for x in store.find(db.execution_runs) if x["request_id"] == existing.request_id), None)
            return run, None
        return None, "request already processed; operator review required"
    req.status=transition(req.status, ExecutionStatus.ACCEPTED); req.updated_at=utcnow()
    store.put(db.execution_requests, req.request_id, req.model_dump(mode='json'), notion_task_id=page.page_id, linear_issue_id=issue.identifier, status=req.status.value, dedupe_key=req.request_key)
    run=ExecutionRun(request_id=req.request_id, task_id=issue.identifier, notion_task_id=page.page_id)
    store.insert_new(db.execution_runs, run.execution_run_id, run.model_dump(mode='json'), request_id=req.request_id, task_id=issue.identifier, status=run.status.value)
    return run, None
