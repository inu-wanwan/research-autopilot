from pathlib import Path

from prefect.artifacts import Artifact
from typer.testing import CliRunner

from phase3_fakes import CONFIG, GOOD_CODE, GOOD_TEST, experiment_def, make_impl_deps
from research_autopilot import cli
from research_autopilot.implementation import flow as flow_mod

GOOD = {"files": {"h2_energy.py": GOOD_CODE, "tests/test_h2.py": GOOD_TEST,
                  ".autopilot/experiment.json": experiment_def(), "configs/h2.json": CONFIG}}


def test_implement_flow_records_history_and_artifact(tmp_path, monkeypatch, recorder):
    deps, repo, rec, _, _ = make_impl_deps(tmp_path, [GOOD])
    monkeypatch.setattr(flow_mod, "build_impl_deps", lambda: deps)
    state = flow_mod.implement_task_flow(handoff_path=rec.handoff_path, return_state=True)
    assert state.is_completed()
    out = state.result()
    assert out["state"] == "completed" and out["experiment_spec_id"]
    m = deps.store.get_implementation(out["phase3_run_id"])
    assert m.flow_run_id  # linked to the Prefect flow run
    art = Artifact.get("impl-inu-900")
    assert art is not None and "**completed**" in art.data


def test_blocked_run_notifies_human(tmp_path, monkeypatch, recorder):
    deps, repo, rec, _, _ = make_impl_deps(tmp_path, [GOOD], mode="dry_run")
    monkeypatch.setattr(flow_mod, "build_impl_deps", lambda: deps)
    out = flow_mod.implement_task_flow(handoff_path=rec.handoff_path)
    assert out["state"] == "blocked"
    # UX policy: no approval request exists, so no buttonless approval message: reasons + steps, routed to approvals
    human = [e for e in recorder.events if e.needs_human]
    assert human and human[0].kind.value != "approval_required" and human[0].steps
    assert any("not_approved" in d for d in human[0].details)


def test_cli_implement_local_and_listing(tmp_path, monkeypatch):
    deps, repo, rec, _, _ = make_impl_deps(tmp_path, [GOOD])
    monkeypatch.setattr("research_autopilot.implementation.factory.build_impl_deps", lambda: deps)
    monkeypatch.setenv("RA_DATABASE_URL", deps.phase2.resolved_database_url())
    runner = CliRunner()
    res = runner.invoke(cli.app, ["implement", "--handoff", rec.handoff_path, "--local"])
    assert res.exit_code == 0, res.output
    assert "**completed**" in res.output
    assert "completed" in runner.invoke(cli.app, ["impl-runs", "list"]).output
    assert runner.invoke(cli.app, ["implement", "--local"]).exit_code != 0
