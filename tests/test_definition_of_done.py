"""Definition of Done (docs/phase_specs/phase2.md / phase3.md): grounded, checkable acceptance items fixed by the
plan; the code reviewer judges only them; an infeasible item is re-planned once, then a human decides."""

import sys
from pathlib import Path

from phase2_fakes import make_plan
from phase3_fakes import CONFIG, GOOD_CODE, GOOD_TEST, code_review, experiment_def, make_impl_deps
from research_autopilot.implementation.models import CodeFinding, InfeasibilityClaim, RunState
from research_autopilot.implementation.orchestrator import implement
from research_autopilot.models import AcceptanceMapping, DoneCriterion, TaskSpec
from research_autopilot.planning import dod

EXP = {".autopilot/experiment.json": experiment_def(), "configs/h2.json": CONFIG}
GOOD = {"files": {"h2_energy.py": GOOD_CODE, "tests/test_h2.py": GOOD_TEST, **EXP}}


def spec(**kw) -> TaskSpec:
    base = dict(task_id="INU-1", title="t", category=None, purpose="Compute the H2 curve.", background=None,
                scientific_rationale=None, specification="Record rhf.conv_tol for every result.",
                acceptance_criteria="- Energy error table for all geometries", dependencies_note=None,
                scientific_invariants="- same basis", artifact_requirements=None, other_sections={}, preamble=None,
                sources=[])
    return TaskSpec.model_construct(**(base | kw))


def plan_with(items, done=("D1",)):
    p = make_plan()
    return p.model_copy(update={"definition_of_done": items, "acceptance_mapping": [
        AcceptanceMapping(criterion="Energy error table for all geometries", covered_by_steps=["S2"],
                          verification="x", done_criteria=list(done))]})


def item(i="D1", statement="Each result records rhf.conv_tol.", source="Record rhf.conv_tol for every result."):
    return DoneCriterion(id=i, statement=statement, source=source, check="test", how="test")


# ---------------------------------------------------------------- Phase 2: checkable, grounded, proportionate


def test_a_grounded_checkable_definition_of_done_is_accepted():
    assert dod.problems(spec(), plan_with([item(source="- Record rhf.conv_tol  for every result")])) == []


def test_definition_of_done_problems_are_detected_deterministically():
    assert dod.problems(spec(), plan_with([], done=()))[0].startswith("definition_of_done is empty")
    invented = dod.problems(spec(), plan_with([item(source="Record the OS and the BLAS vendor.")]))
    assert any("quote the task specification" in p for p in invented)
    vague = dod.problems(spec(), plan_with([item(statement="Settings are recorded appropriately.")]))
    assert any("not pass/fail checkable" in p for p in vague)
    many = dod.problems(spec(), plan_with([item(f"D{i}") for i in range(1, 12)]))
    assert any("at most 10" in p for p in many)
    uncovered = dod.problems(spec(), plan_with([item()], done=()))
    assert any("not covered" in p for p in uncovered)
    assert any("reserved" in p for p in dod.problems(spec(), plan_with([item("B1")], done=("B1",))))


def test_a_rejected_definition_of_done_is_sent_back_to_the_planner_without_a_review(tmp_path):
    from phase2_fakes import FakeLinear, FakeNotion, make_deps, make_issue, make_page, review
    from research_autopilot.orchestrator.core import plan_task
    bad = plan_with([], done=())
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()), [bad, make_plan()],
                     [review("approved")])
    rec = plan_task(deps, task_id="INU-900", mode="dry_run")
    assert rec.decision.status.value == "ready"
    assert len(deps.reviewer.calls) == 1  # only the corrected plan was reviewed
    feedback = deps.planner.calls[1][2]
    assert feedback and feedback[0].issue.startswith("Definition of Done: definition_of_done is empty")


def test_the_planner_gets_measured_environment_facts(tmp_path):
    import subprocess
    from research_autopilot.implementation.facts import environment_facts
    from research_autopilot.implementation.models import RepoConfig
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname="x"\ndependencies=["pyscf==2.14"]\n')
    for cmd in (["init", "-q", "-b", "main"], ["add", "."], ["-c", "user.email=a@b", "-c", "user.name=a", "commit",
                                                              "-qm", "init"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    cfg = RepoConfig(id="r1", path=str(repo), base_branch="main", test_command=[sys.executable, "-m", "pytest"])
    facts = environment_facts({"r1": cfg})(spec(repository="r1"))
    assert "uv pip freeze" in facts["phase4_environment"] and "install_argv is not executed" in facts["phase4_environment"]
    assert "pyscf==2.14" in facts["dependency_files"]["pyproject.toml"]
    assert any(p.startswith("pytest==") for p in facts["test_environment_packages"])
    docker = RepoConfig(id="r2", path=str(repo), base_branch="main", test_command=["docker", "compose", "run"])
    assert "unknown" in environment_facts({"r2": docker})(spec(repository="r2"))["test_environment_packages"]


# ---------------------------------------------------------------- Phase 3: the reviewer judges the items only


def test_findings_outside_the_definition_of_done_are_suggestions_not_revisions(tmp_path):
    extra = CodeFinding(aspect="reproducibility", severity="major", file="pyproject.toml",
                        issue="no lock file", requested_change="add uv.lock")  # no criterion
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [GOOD], reviewer=[code_review("revise", findings=[extra])])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED and len(m.attempts) == 1
    assert any("no lock file" in x for x in m.final_review.llm.suggestions)


def test_a_not_met_item_or_a_defect_tied_to_an_item_sends_the_work_back(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [GOOD, GOOD],
                                        reviewer=[code_review("pass", criteria={"D1": "not_met"}), code_review()])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED and len(m.attempts) == 2
    first = m.attempts[0].review
    assert first.verdict == "revise" and first.revision_requests[0].startswith("[D1 not met]")
    # the second round sees the first round's assessment and re-checks it
    prompt = deps.reviewers["codex"].calls[1][1]
    assert "PREVIOUS ASSESSMENT" in prompt and '"not_met"' in prompt


def test_plans_without_a_definition_of_done_keep_the_previous_review_rules(tmp_path):
    import json as _json
    extra = CodeFinding(aspect="reproducibility", severity="major", file="h2_energy.py", issue="x", requested_change="y")
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [GOOD, GOOD], reviewer=[code_review("revise", findings=[extra]),
                                                                         code_review()])
    handoff = Path(rec.handoff_path)  # a handoff written before the Definition of Done existed
    raw = _json.loads(handoff.read_text())
    raw["plan"]["definition_of_done"] = []
    for m in raw["plan"]["acceptance_mapping"]:
        m["done_criteria"] = []
    handoff.write_text(_json.dumps(raw))
    m = implement(deps, handoff_path=handoff)
    assert m.state is RunState.COMPLETED and len(m.attempts) == 2  # the reviewer's own verdict still applies
    assert m.attempts[0].review.verdict == "revise"


# ---------------------------------------------------------------- infeasible items


CLAIM = 'DOD_INFEASIBLE: {"criteria": ["D1"], "evidence": "pyscf.fci.direct_spin1 exposes no such setting (checked dir())"}'


def test_the_implementer_can_report_an_item_infeasible_with_evidence(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [{**GOOD, "summary": "did what I could\n" + CLAIM}])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.BLOCKED and m.failure_classification == "dod_infeasible"
    assert m.dod_infeasible["criteria"] == ["D1"] and m.dod_infeasible["by"] == "implementer"
    assert deps.reviewers["codex"].calls == []  # not reviewed: the plan goes back first


def test_an_unsupported_claim_is_ignored_and_the_work_is_reviewed(tmp_path):
    weak = 'DOD_INFEASIBLE: {"criteria": ["D1"], "evidence": "too hard"}'
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [{**GOOD, "summary": weak}])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED


def test_the_reviewer_can_report_an_item_infeasible(tmp_path):
    claim = InfeasibilityClaim(criteria=["D1"], evidence="the approved library has no API for this (checked docs)")
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [GOOD], reviewer=[code_review("pass", infeasible=claim)])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.failure_classification == "dod_infeasible" and m.dod_infeasible["by"] == "reviewer"


def _engine(tmp_path, replan):
    from phase5_fakes import approve, build
    from research_autopilot.autopilot.engine import poll
    deps, linear, *_ = build(tmp_path)
    codex = deps.impl.backends["codex"]
    codex.script[0] = {**codex.script[0], "summary": CLAIM}
    deps.plan.planner.script[1:1] = [replan]
    poll(deps)
    (req,) = deps.approvals.pending()
    approve(deps, linear, req.request_id)
    poll(deps)
    poll(deps)
    from research_autopilot.autopilot.models import WorkflowRun
    from research_autopilot.storage import db
    return deps, next(WorkflowRun.model_validate(r) for r in deps.store.find(db.workflow_runs)
                      if r["task_id"] == "INU-900")


def test_an_infeasible_item_is_re_planned_once_with_the_claim(tmp_path):
    fixed = make_plan()
    fixed = fixed.model_copy(update={"definition_of_done": [fixed.definition_of_done[0].model_copy(
        update={"statement": "The output has one energy error row per geometry (as a list)."})]})
    deps, run = _engine(tmp_path, fixed)
    assert run.stage.value == "done", (run.blocked_reason, run.events[-4:])
    assert run.dod_replans == 1 and any(e["kind"] == "dod_infeasible" for e in run.events)
    feedback = deps.plan.planner.calls[1][2]  # the re-plan started from the old plan with the claim
    assert deps.plan.planner.calls[1][1] is not None and "impossible as written" in feedback[0].issue


def test_a_claim_the_re_plan_does_not_accept_goes_to_a_human(tmp_path):
    deps, run = _engine(tmp_path, make_plan())  # the re-plan keeps D1 unchanged
    assert run.stage.value == "blocked" and "not accepted on re-planning" in run.blocked_reason
