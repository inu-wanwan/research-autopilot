import json

from prefect.artifacts import Artifact
from typer.testing import CliRunner

from phase2_fakes import FakeLinear, FakeNotion, make_deps, make_issue, make_page, make_plan, review
from research_autopilot import cli
from research_autopilot.orchestrator import flow as flow_mod


def test_flow_returns_decision_and_publishes_artifact(tmp_path, monkeypatch, recorder):
    linear = FakeLinear(make_issue())
    deps = make_deps(tmp_path, FakeNotion(make_page()), linear, [make_plan()], [review("approved")])
    monkeypatch.setattr(flow_mod, "build_deps", lambda settings, on_retry=None: deps)
    state = flow_mod.plan_task_flow(task_id="INU-900", return_state=True)
    assert state.is_completed()
    decision = state.result()
    assert decision["status"] == "ready" and decision["flow_run_id"]
    assert deps.store.get_run(decision["plan_run_id"]).flow_run_id == decision["flow_run_id"]
    art = Artifact.get("plan-inu-900")
    assert art is not None and "**ready**" in art.data
    assert linear.writes == []


def test_flow_blocked_decision_is_not_a_flow_failure(tmp_path, monkeypatch, recorder):
    deps = make_deps(tmp_path, FakeNotion(make_page(pending=True)), FakeLinear(make_issue()), [], [])
    monkeypatch.setattr(flow_mod, "build_deps", lambda settings, on_retry=None: deps)
    state = flow_mod.plan_task_flow(task_id="INU-900", return_state=True)
    assert state.is_completed() and state.result()["status"] == "blocked"


def test_cli_local_dry_run_and_history(tmp_path, monkeypatch):
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()), [make_plan()], [review("approved")])
    monkeypatch.setattr("research_autopilot.orchestrator.factory.build_deps", lambda settings: deps)
    monkeypatch.setenv("RA_DATABASE_URL", deps.settings.resolved_database_url())
    runner = CliRunner()
    res = runner.invoke(cli.app, ["plan", "--task", "INU-900", "--local", "--json"])
    assert res.exit_code == 0, res.output
    decision = json.loads(res.output)
    assert decision["status"] == "ready" and decision["linear_update"]["action"] == "skipped_dry_run"

    res = runner.invoke(cli.app, ["runs", "list"])
    assert "INU-900" in res.output and "ready" in res.output
    res = runner.invoke(cli.app, ["runs", "show", decision["plan_run_id"]])
    assert json.loads(res.output)["decision"]["status"] == "ready"


def test_cli_exit_codes_and_validation(tmp_path, monkeypatch):
    deps = make_deps(tmp_path, FakeNotion(make_page(sections={})), FakeLinear(make_issue()), [], [])
    monkeypatch.setattr("research_autopilot.orchestrator.factory.build_deps", lambda settings: deps)
    runner = CliRunner()
    assert runner.invoke(cli.app, ["plan", "--task", "INU-900", "--local"]).exit_code == 3  # insufficient_spec
    assert runner.invoke(cli.app, ["plan", "--local"]).exit_code != 0  # neither --task nor --next
    assert runner.invoke(cli.app, ["plan", "--task", "X", "--apply", "--check-only"]).exit_code != 0
