from datetime import timedelta

import pytest

from phase5_fakes import USER, build
from research_autopilot.autopilot.approvals import ApprovalController
from research_autopilot.autopilot.engine import poll
from research_autopilot.autopilot.models import WorkflowRun, WorkflowStage
from research_autopilot.models import utcnow
from research_autopilot.storage import db


def stage(deps, task="INU-900"):
    return WorkflowRun.model_validate(deps.store.find(db.workflow_runs, task_id=task)[0]).stage


def test_linear_status_alone_approves_when_actor_is_authorized(tmp_path):
    deps, linear, *_ = build(tmp_path, approval_mode="linear")
    poll(deps)
    (req,) = deps.approvals.pending()
    assert req.confirmation_ref is None  # no Prefect form in linear mode
    linear.human_move("INU-900", "Approved", at=utcnow() + timedelta(seconds=1))
    poll(deps)
    assert stage(deps) is WorkflowStage.DONE
    d = deps.store.get(db.approval_decisions, req.request_id)
    assert d["channel"] == "linear_status" and USER["id"] in d["actor"]


def test_linear_mode_rejects_unauthorized_actor_bot_and_own_writes(tmp_path):
    deps, linear, *_ = build(tmp_path, approval_mode="linear")
    poll(deps)
    linear.human_move("INU-900", "Approved", actor={"id": "someone-else", "name": "Mallory"}, at=utcnow() + timedelta(seconds=1))
    poll(deps)
    assert stage(deps) is WorkflowStage.AWAITING_AUTHORIZATION
    linear.human_move("INU-900", "Approved", bot={"name": "zapier", "type": "integration"}, at=utcnow() + timedelta(seconds=2))
    poll(deps)
    assert stage(deps) is WorkflowStage.AWAITING_AUTHORIZATION
    deps.store.record_linear_write("INU-900", "Approved", "simulated own write")
    linear.human_move("INU-900", "Approved", at=utcnow() + timedelta(seconds=3))
    poll(deps)
    assert stage(deps) is WorkflowStage.AWAITING_AUTHORIZATION


def test_linear_mode_transition_before_request_is_ignored(tmp_path):
    deps, linear, *_ = build(tmp_path, approval_mode="linear")
    linear._hist("INU-900", "Todo", "Approved", at=utcnow() - timedelta(days=1))  # old, unrelated change
    poll(deps)
    linear.issues["INU-900"].state_name = "Approved"  # state shows Approved but no new transition
    poll(deps)
    assert stage(deps) is WorkflowStage.AWAITING_AUTHORIZATION


def test_linear_mode_requires_configured_approvers(tmp_path):
    deps, linear, *_ = build(tmp_path)
    with pytest.raises(ValueError):
        ApprovalController(deps.store, deps.ops, None, mode="linear", approver_ids=set())


def test_coding_agent_environment_has_no_credentials(tmp_path, monkeypatch):
    import sys
    from research_autopilot.implementation.backends.base import run_process
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_SECRET")
    monkeypatch.setenv("NOTION_API_TOKEN", "ntn_SECRET")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-SECRET")
    monkeypatch.setenv("PREFECT_API_AUTH_STRING", "admin:SECRET")
    out = run_process([sys.executable, "-c", "import os; print(sorted(k for k in os.environ if 'SECRET' in os.environ[k]))"],
                      tmp_path, "", 30, tmp_path / "log.txt", lambda pid: None)
    assert out.stdout.strip() == "[]"
