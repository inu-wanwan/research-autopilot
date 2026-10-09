from typer.testing import CliRunner

from phase5_fakes import approve, build
from research_autopilot import cli
from research_autopilot.autopilot import flows as flows_mod
from research_autopilot.autopilot.models import WorkflowRun
from research_autopilot.storage import db


def test_poll_flow_publishes_status(tmp_path, monkeypatch):
    deps, linear, *_ = build(tmp_path)
    monkeypatch.setattr("research_autopilot.autopilot.factory.build_autopilot_deps", lambda dry_run=False: deps)
    out = flows_mod.autopilot_poll_flow()
    assert out["summary"]["discovered"] == ["INU-900"]
    assert out["report"]["pending_approvals"][0]["kind"] == "onboarding"
    from prefect.artifacts import Artifact
    assert "Pending approvals" in Artifact.get("autopilot-status").data


def test_cli_cancel_and_resume(tmp_path, monkeypatch):
    deps, linear, *_ = build(tmp_path)
    from research_autopilot.autopilot.engine import poll
    poll(deps)
    monkeypatch.setenv("RA_DATABASE_URL", deps.plan.settings.resolved_database_url())
    r = CliRunner()
    assert r.invoke(cli.app, ["autopilot", "cancel", "INU-900"]).exit_code == 0
    run = WorkflowRun.model_validate(deps.store.find(db.workflow_runs, task_id="INU-900")[0])
    assert run.stage.value == "cancelled" and run.events[-1]["kind"] == "operator"
    assert r.invoke(cli.app, ["autopilot", "resume", "INU-900", "--stage", "discovered"]).exit_code == 0
    assert r.invoke(cli.app, ["autopilot", "resume", "INU-404", "--stage", "planning"]).exit_code == 1


def test_approval_form_flow_records_submission(tmp_path, monkeypatch):
    """The confirm flow suspends for input; here the suspension returns a submitted form immediately."""
    deps, linear, *_ = build(tmp_path)
    deps.store.init_schema()

    class Form:
        request_id, spec_hash, decision, approver, comment = "req-1", "h", "approve", "rikushi", ""

    monkeypatch.setattr(flows_mod, "suspend_flow_run", lambda wait_for_input, timeout: Form())
    monkeypatch.setattr("research_autopilot.autopilot.factory.store_from_env", lambda: deps.store)
    out = flows_mod.approval_confirm_flow(request_id="req-1", spec_hash="h", title="t", details_markdown="d")
    assert out["decision"] == "approve"
    assert deps.store.find(db.form_submissions, request_id="req-1")[0]["approver"] == "rikushi"
