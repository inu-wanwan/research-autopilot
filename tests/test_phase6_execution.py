from phase5_fakes import build
from research_autopilot.autopilot.engine import poll
from research_autopilot.autopilot.execution import ExecutionRun, ExecutionStatus
from research_autopilot.autopilot.models import WorkflowStage
from research_autopilot.storage import db

def request(deps, notion):
    page_id = next(iter(notion.pages)); page = notion.pages[page_id]
    notion.pages[page_id] = page.model_copy(update={"run_requested": True, "execution_status": "Requested"})
    deps.plan.manager.notion.requested_task_ids = lambda: [page_id]
    return page_id

def test_explicit_request_is_accepted_and_started(tmp_path):
    deps, linear, notion, *_ = build(tmp_path)
    page_id = request(deps, notion)
    poll(deps)
    rec = deps.store.find(db.execution_runs, task_id="INU-900")
    assert len(rec) == 1 and ExecutionRun.model_validate(rec[0]).status is ExecutionStatus.AWAITING_APPROVAL
    assert notion.pages[page_id].run_requested is False
    assert linear.issues["INU-900"].state_name == "Awaiting Approval"

def test_label_alone_never_starts(tmp_path):
    deps, *_ = build(tmp_path)
    deps.plan.manager.notion.requested_task_ids = lambda: []
    assert poll(deps)["discovered"] == []
    assert deps.store.find(db.workflow_runs) == []

def test_duplicate_checkbox_request_does_not_create_second_run(tmp_path):
    deps, _, notion, *_ = build(tmp_path)
    request(deps, notion); poll(deps)
    page_id = next(iter(notion.pages)); notion.pages[page_id] = notion.pages[page_id].model_copy(update={"run_requested": True})
    poll(deps)
    assert len(deps.store.find(db.execution_runs, task_id="INU-900")) == 1

def test_completed_task_can_be_explicitly_rerun(tmp_path):
    deps, linear, notion, *_ = build(tmp_path)
    linear.human_move("INU-900", "Done")
    request(deps, notion)
    poll(deps)
    assert linear.issues["INU-900"].state_name == "Awaiting Approval"

def test_prefect_run_url_is_persisted_and_projected(tmp_path):
    deps, _, notion, *_ = build(tmp_path)
    deps.settings.prefect_ui_url = "http://prefect"
    page_id = request(deps, notion)
    poll(deps, prefect_flow_run_id="flow-123")
    run = ExecutionRun.model_validate(deps.store.find(db.execution_runs, task_id="INU-900")[0])
    assert run.prefect_flow_run_id == "flow-123"
    assert notion.pages[page_id].last_run_url == "http://prefect/runs/flow-run/flow-123"

def test_blocked_request_is_acknowledged_once(tmp_path):
    deps, _, notion, *_ = build(tmp_path)
    page_id = request(deps, notion)
    page = notion.pages[page_id]
    notion.pages[page_id] = page.model_copy(update={"sections": {**page.sections, "repository": ""}})
    poll(deps)
    assert notion.pages[page_id].run_requested is False
    assert len(deps.store.find(db.execution_requests)) == 1
