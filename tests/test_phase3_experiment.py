"""ExperimentSpec generation and Phase 4 -> 3 FailureReport repairs."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from phase3_fakes import CONFIG, GOOD_CODE, GOOD_TEST, code_review, experiment_def, git, make_impl_deps
from research_autopilot.implementation.experiment import (
    ExperimentDefinition,
    ExperimentSpec,
    parse_definition,
    validate_definition,
)
from research_autopilot.implementation.failure import ErrorClass, FailureReport, decide
from research_autopilot.implementation.models import Phase4Handoff, RunState
from research_autopilot.implementation.orchestrator import implement, repair

EXP = {".autopilot/experiment.json": experiment_def(), "configs/h2.json": CONFIG}
GOOD = {"files": {"h2_energy.py": GOOD_CODE, "tests/test_h2.py": GOOD_TEST, **EXP}}
FIX = {"files": {"h2_energy.py": "import json\n\n\ndef energy():\n    return -1.137\n"}}


def codes(m):
    return [b.code for b in m.block_reasons]


def original_run(tmp_path, extra_codex=(), **kw):
    deps, repo, rec, notion, linear = make_impl_deps(tmp_path, [GOOD, *extra_codex], **kw)
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED, m.block_reasons
    return deps, repo, m


def report_for(m, tmp_path, name="r1", **over) -> Path:
    spec = ExperimentSpec.model_validate_json(Path(m.artifact_paths["experiment_spec"]).read_text())
    data = dict(
        report_id=name, task_id=m.task_id, phase3_run_id=m.phase3_run_id, phase4_run_id="p4-run-1",
        failed_commit=m.implementation_commit, experiment_spec_id=m.experiment_spec_id,
        argv=spec.execution.argv, exit_code=1, error_class="implementation_failure",
        stderr_ref="phase4/p4-run-1/stderr.log", summary="ModuleNotFoundError: No module named 'h2_utils'",
        log_excerpt="Traceback ...\nModuleNotFoundError: No module named 'h2_utils'",
        reproduction=["git checkout <commit>", "python h2_energy.py --config configs/h2.json"],
        allowed_repair_scope=["h2_energy.py"], scientific_invariants=spec.validation.scientific_invariants,
        roundtrip=1,
    )
    data.update(over)
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(data))
    return path


# ---------------------------------------------------------------- ExperimentSpec


def test_completed_run_produces_spec_bound_to_fixed_commit(tmp_path):
    deps, repo, m = original_run(tmp_path)
    spec = ExperimentSpec.model_validate_json(Path(m.artifact_paths["experiment_spec"]).read_text())
    assert spec.schema_version == "1.0" and spec.spec_id == m.experiment_spec_id
    ident = spec.identification
    assert ident.commit == m.implementation_commit == git(repo, "rev-parse", m.task.branch)
    assert ident.task_id == "INU-900" and ident.plan_id == m.plan_id and ident.phase3_run_id == m.phase3_run_id
    # everything the spec references exists at that commit
    for rel in ["h2_energy.py", "configs/h2.json", "pyproject.toml", ".autopilot/experiment.json"]:
        assert git(repo, "cat-file", "-t", f"{ident.commit}:{rel}") == "blob"
    assert spec.execution.argv == ["python", "h2_energy.py", "--config", "configs/h2.json"]
    assert spec.resources.walltime_minutes.basis == "estimated" and spec.resources.gpu.basis == "required"
    # validation comes from the task spec (Notion), not from the LLM-authored definition
    assert spec.validation.acceptance_criteria == ["Energy error table for all geometries", "Same Hamiltonian as reference"]
    assert spec.validation.scientific_invariants == ["cc-pVDZ basis, frozen core."]
    assert spec.validation.numeric_checks[0].condition == "le"
    assert str(Path.home()) not in spec.model_dump_json()  # no machine-specific absolute paths
    # round trip + Phase 4 handoff reference
    assert ExperimentSpec.model_validate_json(spec.model_dump_json()) == spec
    h = Phase4Handoff.model_validate_json(Path(m.artifact_paths["phase4_handoff"]).read_text())
    assert h.experiment_spec_id == spec.spec_id and Path(h.experiment_spec_path).exists()


def test_missing_definition_is_sent_back_then_blocked(tmp_path):
    no_def = {"files": {"h2_energy.py": GOOD_CODE, "tests/test_h2.py": GOOD_TEST}}
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [no_def] * 3)
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.BLOCKED and codes(m) == ["revision_limit"]
    assert m.attempts[0].review.categories == ["experiment_spec"]
    assert "experiment.json is missing" in m.attempts[0].review.revision_requests[0]
    assert "experiment_spec" not in m.artifact_paths


def test_invalid_definition_fixed_on_revision(tmp_path):
    bad = {"files": {**GOOD["files"], ".autopilot/experiment.json": experiment_def(
        execution={"argv": ["bash", "-c", "python h2_energy.py"], "working_directory": "."})}}
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [bad, GOOD])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED
    assert m.attempts[0].review.categories == ["experiment_spec"]


def _definition(**over) -> ExperimentDefinition:
    return ExperimentDefinition.model_validate(json.loads(experiment_def(**over)))


EXISTS = {"h2_energy.py", "configs/h2.json", "pyproject.toml", "src/run.py"}


def _validate(d):
    return validate_definition(d, lambda p: p in EXISTS, lambda p: p in {"src"})


def test_valid_definition_has_no_errors():
    assert _validate(_definition()) == []


@pytest.mark.parametrize("execution,needle", [
    ({"argv": ["bash", "run.sh"]}, "launcher"),
    ({"argv": ["python", "-c", "print(1)"]}, "python -c"),
    ({"argv": ["python", "h2_energy.py; rm -rf ~"]}, "shell metacharacters"),
    ({"argv": ["python", "/Users/me/h2_energy.py"]}, "absolute path"),
    ({"argv": ["python", "missing.py"]}, "does not exist"),
    ({"argv": ["python", "run.py"], "working_directory": "src"}, None),
    ({"argv": ["python", "run.py"], "working_directory": "../elsewhere"}, "parent-directory"),
    ({"argv": ["uv", "run", "bash", "x"]}, "shells are not allowed"),
    ({"argv": ["python", "h2_energy.py"], "env_var_names": ["API_KEY=abc"]}, "invalid name"),
])
def test_argv_and_paths_are_validated(execution, needle):
    errors = _validate(_definition(execution={"working_directory": ".", **execution}))
    if needle is None:
        assert errors == []
    else:
        assert any(needle in e for e in errors), errors


def test_missing_dependency_and_input_files_detected():
    d = _definition(environment={"type": "python", "python_version": "3.12", "dependency_files": ["requirements.txt"]},
                    inputs=[{"path": "configs/missing.json", "kind": "config", "scientific": True}])
    errors = _validate(d)
    assert any("requirements.txt" in e for e in errors) and any("configs/missing.json" in e for e in errors)


def test_secret_values_rejected():
    d = _definition(reproduction=["export OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123"])
    assert any("secret" in e for e in _validate(d))


@pytest.mark.parametrize("drop", ["environment", "resources", "outputs", "reproduction", "execution"])
def test_missing_required_sections_are_rejected(drop):
    data = json.loads(experiment_def())
    data.pop(drop)
    d, errors = parse_definition(json.dumps(data))
    assert d is None and any(drop in e for e in errors)


def test_resource_estimate_requires_basis_rationale_confidence():
    data = json.loads(experiment_def())
    data["resources"]["memory_gb"] = {"value": 4}
    d, errors = parse_definition(json.dumps(data))
    assert d is None and any("memory_gb" in e for e in errors)


def test_outputs_need_one_required():
    data = json.loads(experiment_def())
    data["outputs"][0]["required"] = False
    assert parse_definition(json.dumps(data))[0] is None


# ---------------------------------------------------------------- FailureReport policy


def _report(**over) -> FailureReport:
    base = dict(report_id="r", task_id="INU-900", phase3_run_id="x", phase4_run_id="y", failed_commit="abcdef1",
                experiment_spec_id="s", argv=["python", "a.py"], exit_code=1, error_class="implementation_failure",
                summary="ImportError: cannot import name foo", reproduction=["python a.py"],
                allowed_repair_scope=["h2_energy.py"], scientific_invariants=["STO-3G"], roundtrip=1)
    base.update(over)
    return FailureReport.model_validate(base)


def _decide(r, persisted=0):
    return decide(r, approved_scope=["h2_energy.py", "tests/**", "configs/**", ".autopilot/experiment.json"],
                  locked_inputs=["configs/h2.json"], original_invariants=["STO-3G"], persisted_roundtrips=persisted)


@pytest.mark.parametrize("cls,code", [
    ("implementation_failure", "ok"), ("environment_failure", "ok"),
    ("numerical_failure", "human_required"), ("scientific_validation_failure", "human_required"),
    ("unknown", "human_required"), ("infrastructure_failure", "phase4_responsibility"),
    ("resource_exceeded", "phase4_responsibility"),
])
def test_error_classes(cls, code):
    assert _decide(_report(error_class=cls)).code == code


def test_scientific_hint_in_implementation_failure_needs_human():
    d = _decide(_report(summary="SCF did not converge after 50 iterations"))
    assert d.code == "human_required" and not d.allowed


@pytest.mark.parametrize("scope,code", [
    (["setup.py"], "scope_exceeds_approval"),
    (["**"], "scope_exceeds_approval"),
    (["configs/**"], "scope_touches_scientific_inputs"),
    (["tests/test_h2.py", "h2_energy.py"], "ok"),
])
def test_repair_scope_must_stay_within_approval(scope, code):
    assert _decide(_report(allowed_repair_scope=scope)).code == code


def test_invariants_must_match_and_roundtrips_are_limited():
    assert _decide(_report(scientific_invariants=["cc-pVDZ"])).code == "invariants_mismatch"
    assert _decide(_report(roundtrip=3)).code == "roundtrip_limit"
    assert _decide(_report(roundtrip=1), persisted=2).code == "roundtrip_limit"  # persisted count wins


def test_failure_report_schema():
    with pytest.raises(ValidationError):
        _report(failed_commit="not-a-sha")
    with pytest.raises(ValidationError):
        _report(roundtrip=0)
    with pytest.raises(ValidationError):
        _report(error_class="mystery")
    with pytest.raises(ValidationError):
        _report(allowed_repair_scope=[])


# ---------------------------------------------------------------- repair flow


def test_repair_produces_new_commit_and_spec_and_preserves_original(tmp_path):
    deps, repo, m = original_run(tmp_path, extra_codex=[FIX])
    orig_branch_head = git(repo, "rev-parse", m.task.branch)
    orig_spec_text = Path(m.artifact_paths["experiment_spec"]).read_text()
    r = repair(deps, report_for(m, tmp_path))
    assert r.state is RunState.COMPLETED, r.block_reasons
    assert r.parent_run_id == m.phase3_run_id and r.failure_report_id == "r1" and r.repair_round == 1
    assert r.implementation_commit != m.implementation_commit
    assert r.task.base_commit == m.implementation_commit and r.task.branch != m.task.branch
    # original commit / branch / spec untouched
    assert git(repo, "rev-parse", m.task.branch) == orig_branch_head == m.implementation_commit
    assert Path(m.artifact_paths["experiment_spec"]).read_text() == orig_spec_text
    new_spec = ExperimentSpec.model_validate_json(Path(r.artifact_paths["experiment_spec"]).read_text())
    assert new_spec.supersedes_spec_id == m.experiment_spec_id
    assert new_spec.identification.commit == r.implementation_commit
    assert Path(r.artifact_paths["failure_report"]).exists()
    # repair scope enforced: only h2_energy.py (+ experiment definition) may change
    assert r.task.allowed_file_scope == ["h2_energy.py", ".autopilot/experiment.json"]
    assert "configs/h2.json" in r.task.protected_paths
    # budgets carried over, not reset
    assert r.inherited_usage.total == m.usage.total
    # the reviewer was told this is a repair
    assert "REPAIR" in deps.reviewers["codex"].calls[-1][1]
    # Phase 3 never touches Linear
    assert deps.manager.linear.writes == [("uuid-INU-900", "In Progress")]


def test_repair_changing_scientific_input_is_blocked(tmp_path):
    tamper = {"files": {"h2_energy.py": GOOD_CODE + "\n", "configs/h2.json": CONFIG.replace("sto-3g", "cc-pvdz")}}
    deps, repo, m = original_run(tmp_path, extra_codex=[tamper])
    r = repair(deps, report_for(m, tmp_path, allowed_repair_scope=["h2_energy.py"]))
    assert r.state is RunState.BLOCKED and codes(r) == ["unapproved_change"]
    assert "experiment_spec" not in r.artifact_paths  # no new experiment commit approved


def test_repair_weakening_numeric_checks_is_blocked(tmp_path):
    weakened = experiment_def(numeric_checks=[])
    tamper = {"files": {"h2_energy.py": GOOD_CODE + "\n", ".autopilot/experiment.json": weakened}}
    deps, repo, m = original_run(tmp_path, extra_codex=[tamper])
    r = repair(deps, report_for(m, tmp_path))
    assert codes(r) == ["unapproved_change"]
    assert any("numeric checks" in b.message for b in r.block_reasons)


def test_repair_needing_science_change_is_blocked_before_any_backend_call(tmp_path):
    deps, repo, m = original_run(tmp_path)
    calls_before = len(deps.backends["codex"].calls)
    r = repair(deps, report_for(m, tmp_path, error_class="numerical_failure", summary="energy oscillates"))
    assert r.state is RunState.BLOCKED and codes(r) == ["repair_not_allowed"]
    assert r.failure_classification == "human_required"
    assert len(deps.backends["codex"].calls) == calls_before


def test_duplicate_failure_report_is_not_reprocessed(tmp_path):
    deps, repo, m = original_run(tmp_path, extra_codex=[FIX])
    path = report_for(m, tmp_path)
    first = repair(deps, path)
    calls = len(deps.backends["codex"].calls)
    second = repair(deps, path)
    assert second.phase3_run_id == first.phase3_run_id
    assert len(deps.backends["codex"].calls) == calls


def test_roundtrip_limit_across_chain(tmp_path):
    deps, repo, m = original_run(tmp_path, extra_codex=[FIX, {"files": {"h2_energy.py": GOOD_CODE + "# 2\n"}}])
    r1 = repair(deps, report_for(m, tmp_path, name="r1", roundtrip=1))
    assert r1.state is RunState.COMPLETED and r1.repair_round == 1
    r2 = repair(deps, report_for(r1, tmp_path, name="r2", roundtrip=1))  # stale count; persisted chain says 2
    assert r2.state is RunState.COMPLETED and r2.repair_round == 2
    r3 = repair(deps, report_for(r2, tmp_path, name="r3", roundtrip=3))
    assert codes(r3) == ["repair_not_allowed"] and r3.failure_classification == "roundtrip_limit"


def test_repair_respects_inherited_budget(tmp_path):
    deps, repo, m = original_run(tmp_path, extra_codex=[FIX])
    # The budget is checked before every backend call; the repair inherits the original run's usage,
    # so a cap equal to what the original already spent stops the repair before any new call.
    deps.settings = deps.settings.model_copy(update={"max_tokens_per_run": m.usage.total})
    calls = len(deps.backends["codex"].calls)
    r = repair(deps, report_for(m, tmp_path))
    assert codes(r) == ["budget_exceeded"] and len(deps.backends["codex"].calls) == calls
    assert "earlier runs" in r.block_reasons[0].message


def test_repair_internal_revision_limit(tmp_path):
    deps, repo, m = original_run(tmp_path, extra_codex=[FIX] * 3)
    deps.reviewers["codex"].script = [code_review("revise")] * 3
    r = repair(deps, report_for(m, tmp_path))
    assert codes(r) == ["revision_limit"] and len(r.attempts) == 3


def test_repair_commit_mismatch_is_rejected(tmp_path):
    deps, repo, m = original_run(tmp_path)
    r = repair(deps, report_for(m, tmp_path, failed_commit="0" * 40))
    assert codes(r) == ["repair_not_allowed"] and r.failure_classification == "original_run_invalid"


def test_repair_crash_is_recovered(tmp_path):
    class Crash(BaseException):
        pass

    deps, repo, m = original_run(tmp_path, extra_codex=[{**FIX, "raise": Crash()}])
    with pytest.raises(Crash):
        repair(deps, report_for(m, tmp_path))
    crashed = deps.store.find_implementation_by_failure_report("r1")
    assert crashed.state is RunState.IMPLEMENTING
    for key in ("phase3:INU-900", "phase3:__global__"):
        deps.store.release_lease(key, crashed.phase3_run_id)
    resumed = implement(deps, resume_run_id=crashed.phase3_run_id)
    assert resumed.state is RunState.COMPLETED, resumed.block_reasons
    assert resumed.failure_report_id == "r1" and resumed.attempts[0].recovered
    spec = ExperimentSpec.model_validate_json(Path(resumed.artifact_paths["experiment_spec"]).read_text())
    assert spec.supersedes_spec_id == m.experiment_spec_id
