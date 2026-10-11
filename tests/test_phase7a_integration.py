"""Phase 7A: Planner (Phase 2) / Implementer (Phase 3) / Executor (Phase 4) use verified catalog artifacts."""

import json
from pathlib import Path
from types import SimpleNamespace

from catalog_fakes import CASCI_ADAPTER, casci_repo, install
from phase2_fakes import FakeLinear, FakeNotion, make_deps, make_issue, make_page, make_plan, review
from research_autopilot.catalog import service
from research_autopilot.catalog.integration import INPUTS_ENV
from research_autopilot.experiments.models import ExperimentManifest
from research_autopilot.implementation.models import RepoConfig
from research_autopilot.models import BlockedCode, DecisionStatus, ExecutionHandoff
from research_autopilot.orchestrator.core import plan_task
from research_autopilot.storage import db

REQ = "- catalog: kind=toy-condition repository=casci molecule=H2 geometry_id=h2-r0.7 k=2..3 expect=2"


def _ctx(tmp_path, store, commits=("aaaaaaa1111",)):
    repo = casci_repo(tmp_path / "casci", commits=commits)
    ctx = service.CatalogContext(store=store, registry={"casci": RepoConfig(id="casci", path=str(repo))},
                                 environment="mac")
    store.init_schema()
    install(ctx, CASCI_ADAPTER, ["casci"])
    service.scan(ctx, "casci")
    return ctx, repo


def _deps(tmp_path, input_artifacts, planner=None):
    page = make_page()
    page = page.model_copy(update={"sections": {**page.sections, "input_artifacts": input_artifacts}})
    return make_deps(tmp_path, FakeNotion(page), FakeLinear(make_issue()), planner or [make_plan()],
                     [review("approved")])


def test_planner_receives_verified_artifacts_and_handoff_freezes_them(tmp_path):
    deps = _deps(tmp_path, "Reuse the landscape.\n" + REQ)
    deps.catalog, _ = _ctx(tmp_path, deps.store)
    rec = plan_task(deps, task_id="INU-900", mode="dry_run")
    assert rec.decision.status is DecisionStatus.READY
    spec_seen = deps.planner.calls[0][0]
    assert "Resolved catalog artifacts (2" in spec_seen.input_artifacts and "Reuse the landscape." in spec_seen.input_artifacts
    handoff = ExecutionHandoff.model_validate_json(Path(rec.handoff_path).read_text())
    assert handoff.input_artifacts.startswith("Reuse the landscape.")  # the human spec itself is unchanged
    frozen = handoff.catalog_resolution
    assert len(frozen["artifacts"]) == 2 and frozen["environment"] == "mac" and not frozen["problems"]


def test_unreusable_artifacts_block_before_any_planning_call(tmp_path):
    deps = _deps(tmp_path, REQ)
    deps.catalog, _ = _ctx(tmp_path, deps.store, commits=("aaaaaaa1111", "bbbbbbb2222"))  # ambiguous
    rec = plan_task(deps, task_id="INU-900", mode="dry_run")
    assert rec.decision.status is DecisionStatus.BLOCKED
    assert {r.code for r in rec.decision.reasons} == {BlockedCode.DATA_INCONSISTENCY}
    assert any("ambiguous" in r.message for r in rec.decision.reasons)
    assert deps.planner.calls == []


def test_catalog_requirements_without_a_catalog_or_with_bad_syntax_block(tmp_path):
    deps = _deps(tmp_path, REQ)
    rec = plan_task(deps, task_id="INU-900", mode="check_only")
    assert rec.decision.status is DecisionStatus.BLOCKED
    assert "no artifact catalog is configured" in rec.decision.reasons[0].message
    deps = _deps(tmp_path / "b", "- catalog: molecule=H2")
    deps.catalog, _ = _ctx(tmp_path / "b", deps.store)
    assert "invalid catalog requirement" in plan_task(deps, task_id="INU-900", mode="check_only").decision.reasons[0].message


def test_tasks_without_catalog_requirements_are_unaffected(tmp_path):
    deps = _deps(tmp_path, "free-text inputs only")
    rec = plan_task(deps, task_id="INU-900", mode="dry_run")
    assert rec.decision.status is DecisionStatus.READY
    assert ExecutionHandoff.model_validate_json(Path(rec.handoff_path).read_text()).catalog_resolution is None


def test_implementer_prompt_lists_catalog_artifacts_as_read_only_inputs(tmp_path):
    from research_autopilot.catalog.integration import implementer_view
    from research_autopilot.implementation.prompts import IMPLEMENTER_RULES, _task_block
    deps = _deps(tmp_path, REQ)
    deps.catalog, _ = _ctx(tmp_path, deps.store)
    rec = plan_task(deps, task_id="INU-900", mode="dry_run")
    frozen = ExecutionHandoff.model_validate_json(Path(rec.handoff_path).read_text()).catalog_resolution
    view = implementer_view(frozen)
    task = SimpleNamespace(**{k: None for k in ("task_id", "purpose", "specification", "scientific_invariants",
                                                 "acceptance_criteria", "non_goals", "input_artifacts",
                                                 "artifact_requirements")},
                           allowed_file_scope=[], protected_paths=[], catalog_artifacts=view,
                           plan=SimpleNamespace(model_dump=lambda mode: {}))
    block = _task_block(task)
    assert [a["identity"]["k"] for a in block["catalog_artifacts"]] == [2, 3]
    assert all(Path(f).is_file() for a in block["catalog_artifacts"] for f in a["files"])
    assert "read-only" in IMPLEMENTER_RULES and INPUTS_ENV in IMPLEMENTER_RULES


def test_executor_reverifies_inputs_and_hands_the_job_a_manifest(tmp_path):
    deps2 = _deps(tmp_path, REQ)
    ctx, repo = _ctx(tmp_path, deps2.store)
    deps2.catalog = ctx
    rec = plan_task(deps2, task_id="INU-900", mode="dry_run")
    handoff_path = Path(rec.handoff_path)
    from research_autopilot.experiments import orchestrator as exo
    deps = SimpleNamespace(extra={"catalog": ctx}, store=ctx.store, registry=ctx.registry)
    m = ExperimentManifest(task_id="INU-900", phase3_run_id="p3")
    impl = SimpleNamespace(handoff_path=str(handoff_path))
    path = exo._catalog_inputs(deps, m, impl, tmp_path / "job", "exp1")
    manifest = json.loads(path.read_text())
    assert len(manifest["artifacts"]) == 2 and m.artifact_paths["catalog_inputs:exp1"] == str(path)
    # Bytes changed after planning: the job must not start.
    next((repo / "results").rglob("cond_k2.json")).write_text("{}")
    try:
        exo._catalog_inputs(deps, m, impl, tmp_path / "job2", "exp1")
    except exo._Block as exc:
        assert "not reusable" in str(exc) and "stale" in str(exc)
    else:
        raise AssertionError("tampered catalog input was accepted")


def test_parent_to_job_end_to_end_with_catalog_inputs(tmp_path):
    """Phase 2-5 fakes: the experiment job receives RA_CATALOG_INPUTS pointing to verified artifacts."""
    from phase5_fakes import SECTIONS, approve, build
    from research_autopilot.autopilot.engine import poll
    deps, linear, notion, writer, channel = build(tmp_path, drafts=[], n=20)
    page_id = next(iter(notion.pages))
    notion.pages[page_id] = notion.pages[page_id].model_copy(
        update={"sections": {**SECTIONS, "input_artifacts": REQ}})
    ctx, _ = _ctx(tmp_path, deps.store)
    deps.plan.catalog = ctx
    deps.exp.extra["catalog"] = ctx
    poll(deps)
    (req,) = deps.approvals.pending()  # research authorization (onboarding) is still a human decision
    approve(deps, linear, req.request_id)
    for _ in range(6):
        poll(deps)
    submits = deps.exp.backend.submits
    assert submits, [r["stage"] for r in deps.store.find(db.workflow_runs)]
    inputs = Path(submits[0].env[INPUTS_ENV])
    assert inputs.is_file() and len(json.loads(inputs.read_text())["artifacts"]) == 2
