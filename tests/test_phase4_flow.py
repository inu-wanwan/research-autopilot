from prefect.artifacts import Artifact

from phase4_fakes import RESULT_OK, make_exp
from research_autopilot.experiments import flow as flow_mod
from research_autopilot.experiments.backends.fake import FakeComputeBackend


def test_flow_records_history_and_artifact(tmp_path, monkeypatch, recorder):
    deps, runs, *_ = make_exp(tmp_path, compute=FakeComputeBackend([{"state": "succeeded", "exit_code": 0,
                                                                     "outputs": {"results/h2.json": RESULT_OK}}]))
    monkeypatch.setattr(flow_mod, "build_exp_deps", lambda: deps)
    state = flow_mod.run_experiment_flow(phase3_run_ids=runs, return_state=True)
    out = state.result()
    assert state.is_completed() and out["state"] == "completed" and out["phase5_handoff"]
    assert deps.store.get_experiment(out["experiment_run_id"]).flow_run_id
    assert "**completed**" in Artifact.get("exp-inu-900").data
