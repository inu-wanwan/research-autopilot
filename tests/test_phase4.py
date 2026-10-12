import json
from pathlib import Path

import pytest

from phase2_fakes import make_issue
from phase4_fakes import (
    ACCEPTANCE,
    INVARIANTS,
    RESULT_OK,
    FakeAnalyst,
    FakeSciReviewer,
    draft,
    host,
    make_exp,
    sci_review,
    variant,
)
from research_autopilot.experiments.analyst import LlmBudgetError
from research_autopilot.experiments.backends.fake import FakeComputeBackend
from research_autopilot.experiments.config import Phase4Settings, load_phase4_settings
from research_autopilot.experiments.models import (
    ExperimentState,
    ParameterChange,
    Phase5Handoff,
    ReviewedExperimentProposal,
    ScopeAssessment,
)
from research_autopilot.experiments.orchestrator import run_experiment
from research_autopilot.experiments.resources import ResourceError, ResourceManager, estimate, heartbeat_seconds, thread_env
from research_autopilot.experiments.scope import assess
from research_autopilot.implementation.experiment import ExperimentSpec

OK = {"state": "succeeded", "exit_code": 0, "outputs": {"results/h2.json": RESULT_OK}}


def jobs_of(m, exp_id):
    return [j for j in m.jobs if j.experiment_id == exp_id]


# ---------------------------------------------------------------- happy path


def test_runs_validates_analyzes_and_hands_off(tmp_path):
    deps, runs, *_ = make_exp(tmp_path, compute=FakeComputeBackend([OK]))
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.COMPLETED, m.block_reason
    (job,) = m.jobs
    assert job.validation.status == "pass" and job.validation.metrics == {"correlation_energy": -0.0205}
    unit_checks = [c for c in job.validation.checks if c.kind == "units"]
    assert {c.name for c in unit_checks if c.status == "pass"} >= {
        "units:results/h2.json:e_rhf", "units:results/h2.json:e_fci", "metric_units:correlation_energy"}
    assert next(c for c in unit_checks if c.name == "units:results/h2.json:e_rhf").value == pytest.approx(-1.1168)
    assert job.validation.fully_verified is False  # prose acceptance criteria stay not_verified
    assert {c.kind for c in job.validation.checks if c.status == "not_verified"} >= {"acceptance_criterion", "scientific_invariant"}
    art = job.collect.artifacts[0]
    assert Path(art.stored_path).read_text() == RESULT_OK and len(art.sha256) == 64
    # thread / memory env set to the allocation (oversubscription guard)
    js = deps.backend.submits[0]
    assert js.env["OMP_NUM_THREADS"] == str(js.allocation.threads) and js.secret_env_names == []
    # analysis + reviewed proposal + Phase 5 handoff
    assert m.analysis.summary == "ok" and len(m.reviewed_proposals) == 1
    h = Phase5Handoff.model_validate_json(Path(m.artifact_paths["phase5_handoff"]).read_text())
    assert h.state is ExperimentState.COMPLETED and h.final_commit == job.commit
    rp = ReviewedExperimentProposal.model_validate_json(Path(h.reviewed_proposal_paths[0]).read_text())
    assert rp.approval_status == "pending_phase5_policy"
    assert rp.scope_assessment.within_scope and not rp.scope_assessment.requires_human_decision
    assert m.llm_cost_known == pytest.approx(0.03)
    assert deps.store.get_experiment(m.experiment_run_id).state is ExperimentState.COMPLETED
    # Phase 4 never touched Linear (only Phase 2's write exists)
    assert deps.manager.linear.writes == [("uuid-INU-900", "In Progress")]


def test_exit_zero_without_outputs_is_not_success(tmp_path):
    deps, runs, *_ = make_exp(tmp_path, compute=FakeComputeBackend([{"state": "succeeded", "exit_code": 0}]),
                              repair_codex=[])
    deps.impl_deps = None  # no repair available -> blocked
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.BLOCKED
    assert m.jobs[0].validation.status == "fail"
    assert m.jobs[0].triage.error_class.value == "implementation_failure"


def test_missing_declared_json_field_never_passes_units_validation(tmp_path):
    bad = json.dumps({"e_fci": -1.1373, "e_corr": -0.0205})
    deps, runs, *_ = make_exp(tmp_path, compute=FakeComputeBackend([
        {"state": "succeeded", "exit_code": 0, "outputs": {"results/h2.json": bad}}
    ]))
    deps.impl_deps = None
    manifest = run_experiment(deps, phase3_run_ids=runs)
    check = next(c for c in manifest.jobs[0].validation.checks if c.name == "units:results/h2.json:e_rhf")
    assert check.status == "fail" and manifest.state is ExperimentState.BLOCKED


@pytest.mark.parametrize("content,klass", [
    (json.dumps({"e_rhf": -1.1, "e_fci": float("nan"), "e_corr": -0.02}), "numerical_failure"),
    ("{not json", "implementation_failure"),
    (json.dumps({"e_rhf": -1.1, "e_fci": -1.0, "e_corr": 0.1}), "scientific_validation_failure"),
])
def test_corrupt_nan_and_failed_checks(tmp_path, content, klass):
    deps, runs, *_ = make_exp(tmp_path, compute=FakeComputeBackend([{"state": "succeeded", "exit_code": 0,
                                                                     "outputs": {"results/h2.json": content}}]))
    deps.impl_deps = None
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.BLOCKED
    assert m.jobs[0].triage.error_class.value == klass
    assert m.analysis is None  # no scientific conclusions from failed validation


def test_job_modifying_scientific_input_fails_validation(tmp_path):
    out = {**OK["outputs"], "configs/h2.json": '{"basis": "cc-pvdz"}'}
    deps, runs, *_ = make_exp(tmp_path, compute=FakeComputeBackend([{**OK, "outputs": out}]))
    deps.impl_deps = None
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.BLOCKED
    assert any(c.kind == "input_integrity" and c.status == "fail" for c in m.jobs[0].validation.checks)


# ---------------------------------------------------------------- contract / approval


def test_unapproved_execution_is_blocked(tmp_path):
    deps, runs, deps3, _ = make_exp(tmp_path, compute=FakeComputeBackend([OK]))
    deps.manager.linear.issues["INU-900"] = make_issue("INU-900", state=("Todo", "unstarted"))
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.BLOCKED and "not approved" in m.block_reason
    assert deps.backend.submits == []


def test_phase3_run_without_reviewer_pass_is_blocked(tmp_path):
    deps, runs, deps3, _ = make_exp(tmp_path, compute=FakeComputeBackend([OK]))
    impl = deps.store.get_implementation(runs[0])
    impl.final_review = None
    deps.store.save_implementation(impl)
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.BLOCKED and "Code Reviewer pass" in m.block_reason


def test_missing_secret_env_name_blocks(tmp_path):
    deps, runs, *_ = make_exp(tmp_path, compute=FakeComputeBackend([OK]))
    impl = deps.store.get_implementation(runs[0])
    spec_path = Path(impl.artifact_paths["experiment_spec"])
    spec = ExperimentSpec.model_validate_json(spec_path.read_text())
    spec.execution.env_var_names = ["SOME_LICENSE_TOKEN"]
    spec_path.write_text(spec.model_dump_json())
    deps.extra["host_env"] = {}
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.BLOCKED and "SOME_LICENSE_TOKEN" in m.block_reason


def test_tampered_spec_with_shell_argv_is_rejected(tmp_path):
    deps, runs, *_ = make_exp(tmp_path, compute=FakeComputeBackend([OK]))
    impl = deps.store.get_implementation(runs[0])
    spec_path = Path(impl.artifact_paths["experiment_spec"])
    data = json.loads(spec_path.read_text())
    data["execution"]["argv"] = ["bash", "-c", "rm -rf ~"]
    spec_path.write_text(json.dumps(data))
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.BLOCKED and "invalid at its commit" in m.block_reason


# ---------------------------------------------------------------- scheduling / resources


def test_independent_experiments_run_in_parallel_and_dependents_wait(tmp_path):
    variants = [variant("geom-a"), variant("geom-b"), variant("combine", deps=["geom-a", "geom-b"])]
    compute = FakeComputeBackend(by_experiment={e: [{**OK, "polls_until_done": 2}] for e in ("geom-a", "geom-b", "combine")})
    deps, runs, *_ = make_exp(tmp_path, variants=variants, compute=compute, max_parallel_jobs=2)
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.COMPLETED, m.block_reason
    assert compute.max_concurrent == 2
    assert compute.order[-1] == "combine" and set(compute.order[:2]) == {"geom-a", "geom-b"}


def test_caps_serialize_and_record_reason(tmp_path):
    variants = [variant("a"), variant("b"), variant("c")]
    compute = FakeComputeBackend([OK, OK, OK])
    deps, runs, *_ = make_exp(tmp_path, variants=variants, compute=compute, max_parallel_jobs=1)
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.COMPLETED and compute.max_concurrent == 1
    assert any("max_parallel_jobs=1" in n for n in m.serialization_notes)


def test_dependency_failure_blocks_dependent(tmp_path):
    variants = [variant("a"), variant("b", deps=["a"])]
    compute = FakeComputeBackend(by_experiment={"a": [{"state": "failed", "exit_code": 1, "stderr": "weird"}]})
    deps, runs, *_ = make_exp(tmp_path, variants=variants, compute=compute)
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.BLOCKED and jobs_of(m, "b") == []


def test_resource_estimate_planning_and_caps():
    from phase3_fakes import experiment_def
    from research_autopilot.implementation.experiment import Resources
    s = Phase4Settings(max_job_cpu_total=2, max_job_memory_gb_total=4)
    rm = ResourceManager(s, host(cpu=8, mem=16, avail=12))
    spec_res = Resources.model_validate(json.loads(experiment_def())["resources"])

    class S:
        resources = spec_res
    est = estimate(S, [], s)
    assert est.basis == "spec_estimate" and est.confidence == "medium"
    alloc = rm.plan(est)
    assert alloc.cpu == 1 and alloc.memory_gb == 1.5 and alloc.walltime_seconds == 240
    assert thread_env(alloc)["OPENBLAS_NUM_THREADS"] == "1"
    assert heartbeat_seconds(alloc, s) == 80  # ~1/3 of the walltime, within bounds
    hist = estimate(S, [{"peak_rss_gb": 2.0, "wall_seconds": 300}, {"peak_rss_gb": 1.0, "wall_seconds": 60}], s)
    assert hist.basis == "history" and hist.memory_gb == 2.0 and hist.walltime_minutes == 5.0
    with pytest.raises(ResourceError):
        rm.plan(hist, scale=2.0)  # 2GB * 1.5 * 2 > 4GB cap
    gpu = est.model_copy(update={"gpu": 1})
    with pytest.raises(ResourceError):
        rm.plan(gpu)  # no CUDA GPU
    tight = ResourceManager(s, host(cpu=8, mem=16, avail=1.0))  # little free memory right now
    with pytest.raises(ResourceError):
        tight.plan(est)


def test_mac_safe_defaults_and_env_override():
    s = Phase4Settings()
    assert s.max_job_cpu_total == 2 and s.max_job_memory_gb_total == 4 and s.reserve_cpu == 2
    e = load_phase4_settings({"RA_EXP_MAX_JOB_CPU": "host", "RA_EXP_MAX_PARALLEL": "6", "RA_EXP_MAX_ROUNDTRIPS": "1"})
    assert e.max_job_cpu_total is None and e.max_parallel_jobs == 6 and e.max_roundtrips == 1


# ---------------------------------------------------------------- retries / repairs


def test_infrastructure_failure_retried_with_same_conditions(tmp_path):
    compute = FakeComputeBackend([{"state": "failed", "exit_code": 1, "stderr": "OSError: No space left on device"}, OK])
    deps, runs, *_ = make_exp(tmp_path, compute=compute)
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.COMPLETED and m.retries == 1
    a, b = compute.submits
    assert a.commit == b.commit and a.argv == b.argv and a.job_key != b.job_key


def test_retry_limit(tmp_path):
    fail = {"state": "failed", "exit_code": 1, "stderr": "Connection reset by peer"}
    deps, runs, *_ = make_exp(tmp_path, compute=FakeComputeBackend([fail] * 5), max_infra_retries=2)
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.BLOCKED and m.retries == 2 and len(m.jobs) == 3


def test_resource_exceeded_retries_with_more_memory(tmp_path):
    compute = FakeComputeBackend([{"state": "resource_exceeded", "reason": "memory 1.6GB > limit 1.5GB"}, OK])
    deps, runs, *_ = make_exp(tmp_path, compute=compute)
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.COMPLETED
    assert compute.submits[1].allocation.memory_gb > compute.submits[0].allocation.memory_gb


def test_implementation_failure_goes_to_phase3_and_reruns(tmp_path):
    from phase3_fakes import GOOD_CODE
    crash = {"state": "failed", "exit_code": 1,
             "stderr": 'Traceback (most recent call last):\n  File "/x/src/h2_energy.py", line 3, in <module>\nKeyError: \'atoms\'\n'}
    compute = FakeComputeBackend([crash, OK])
    fix = {"files": {"h2_energy.py": GOOD_CODE + "# fixed\n"}}
    deps, runs, deps3, repo = make_exp(tmp_path, compute=compute, repair_codex=[fix])
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.COMPLETED, m.block_reason
    assert m.roundtrips == 1 and m.repairs[0]["state"] == "completed"
    first, second = compute.submits
    assert first.commit != second.commit and first.spec_id != second.spec_id
    assert len(m.spec_history) == 2
    # the original artifacts / run dir are untouched and separate
    assert first.run_dir != second.run_dir
    fr = json.loads((Path(first.run_dir) / "failure_report.json").read_text())
    assert fr["error_class"] == "implementation_failure" and "configs/**" not in fr["allowed_repair_scope"]
    assert fr["scientific_invariants"] == INVARIANTS


def test_roundtrip_limit_two(tmp_path):
    from phase3_fakes import GOOD_CODE
    crash = {"state": "failed", "exit_code": 1,
             "stderr": 'Traceback (most recent call last):\n  File "h2_energy.py", line 3\nKeyError: \'x\'\n'}
    compute = FakeComputeBackend([crash, crash, crash])
    fixes = [{"files": {"h2_energy.py": GOOD_CODE + f"# fix {i}\n"}} for i in range(2)]
    deps, runs, *_ = make_exp(tmp_path, compute=compute, repair_codex=fixes)
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.BLOCKED and m.roundtrips == 2
    assert "round-trip limit" in m.jobs[-1].triage.reason


def test_scientific_failures_are_never_auto_repaired(tmp_path):
    numeric = {"state": "failed", "exit_code": 1, "stderr": "RuntimeError: SCF did not converge"}
    deps, runs, deps3, _ = make_exp(tmp_path, compute=FakeComputeBackend([numeric]))
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.BLOCKED and m.roundtrips == 0
    assert m.jobs[0].triage.error_class.value == "numerical_failure"


def test_lost_job_is_not_resubmitted(tmp_path):
    deps, runs, *_ = make_exp(tmp_path, compute=FakeComputeBackend([{"state": "lost", "reason": "unknown"}]))
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.BLOCKED and len(deps.backend.submits) == 1


# ---------------------------------------------------------------- LLM budget / analysis


def test_analysis_blocked_without_price(tmp_path):
    deps, runs, *_ = make_exp(tmp_path, compute=FakeComputeBackend([OK]),
                              analyst=FakeAnalyst(raises=LlmBudgetError("no price configured for gpt-5.6-terra")))
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.BLOCKED and "no price" in m.block_reason
    assert m.jobs[0].validation.status == "pass"  # validated results are kept
    assert m.llm_usage[0].error


def test_reviewer_skipped_when_budget_exhausted(tmp_path):
    deps, runs, *_ = make_exp(tmp_path, compute=FakeComputeBackend([OK]), max_llm_cost_usd_per_run=0.005)
    m = run_experiment(deps, phase3_run_ids=runs)
    rp = ReviewedExperimentProposal.model_validate_json(Path(m.artifact_paths[f"proposal:{m.reviewed_proposals[0]}"]).read_text())
    assert rp.review is None and rp.scope_assessment.requires_human_decision and not rp.scope_assessment.within_scope


def test_duplicate_proposals_across_runs_are_stored_once(tmp_path):
    deps, runs, *_ = make_exp(tmp_path, compute=FakeComputeBackend([OK, OK]))
    m1 = run_experiment(deps, phase3_run_ids=runs)
    m2 = run_experiment(deps, phase3_run_ids=runs)
    assert len(m1.reviewed_proposals) == 1 and m2.reviewed_proposals == []
    assert len(deps.store.list_proposals("INU-900")) == 1


def test_proposal_limits(tmp_path):
    drafts = [draft(title=f"p{i}", changes=[ParameterChange(name="replicates", from_value="1", to_value=str(i + 2),
                                                            kind="within_spec")]) for i in range(5)]
    deps, runs, *_ = make_exp(tmp_path, compute=FakeComputeBackend([OK]), analyst=FakeAnalyst(drafts),
                              max_proposals_per_run=2)
    m = run_experiment(deps, phase3_run_ids=runs)
    assert len(m.reviewed_proposals) == 2
    deps2, runs2, *_ = make_exp(tmp_path / "x", compute=FakeComputeBackend([OK]), max_chain_depth=1)
    deps2.extra["chain_depth"] = 1
    m2 = run_experiment(deps2, phase3_run_ids=runs2)
    assert m2.reviewed_proposals == [] and any("chain depth" in e.message for e in m2.events)


# ---------------------------------------------------------------- ScopeAssessment


def _assess(d, review=sci_review()):
    return assess(d, original_invariants=INVARIANTS, original_acceptance=ACCEPTANCE, original_purpose="p", review=review)


def test_scope_within_for_additional_validation():
    s = _assess(draft())
    assert s.within_scope and not s.requires_human_decision and s.reviewer_opinion is True


@pytest.mark.parametrize("d,needle", [
    (draft(changes=[ParameterChange(name="molecule", from_value="H2", to_value="LiH", kind="new_target")]), "out-of-scope"),
    (draft(invariants=["STO-3G basis"]), "invariant not preserved"),
    (draft(acceptance=ACCEPTANCE[:1]), "acceptance criterion missing"),
    (draft(changes=[ParameterChange(name="solver", from_value="FCI", to_value="CCSD(T)", kind="method")]), "human judgement"),
    (draft(changes=[ParameterChange(name="basis", from_value="cc-pVDZ basis", to_value="aug-cc-pVDZ", kind="within_spec")]),
     "fixed by an invariant"),
])
def test_scope_out_of_scope_or_ambiguous(d, needle):
    s = _assess(d)
    assert not s.within_scope and s.requires_human_decision
    assert any(needle in f for f in s.deterministic_findings), s.deterministic_findings


def test_reviewer_accept_alone_does_not_make_it_in_scope():
    s = _assess(draft(invariants=[]), review=sci_review("accept", in_scope=True))
    assert not s.within_scope and s.requires_human_decision


def test_reviewer_doubt_requires_human():
    for r in (sci_review("revise"), sci_review(in_scope=False), sci_review(judgement=True), None):
        s = _assess(draft(), review=r)
        assert not s.within_scope and s.requires_human_decision


def test_scope_assessment_consistency_validator():
    with pytest.raises(ValueError):
        ScopeAssessment(within_scope=True, rationale="x", proposed_changes=[], requires_human_decision=True,
                        deterministic_findings=[], reviewer_opinion=True)


def test_reviewed_proposal_roundtrip_and_phase5_compat(tmp_path):
    deps, runs, *_ = make_exp(tmp_path, compute=FakeComputeBackend([OK]),
                              reviewer=FakeSciReviewer([sci_review("accept")]))
    m = run_experiment(deps, phase3_run_ids=runs)
    path = Path(m.artifact_paths[f"proposal:{m.reviewed_proposals[0]}"])
    rp = ReviewedExperimentProposal.model_validate_json(path.read_text())
    assert ReviewedExperimentProposal.model_validate_json(rp.model_dump_json()) == rp
    assert rp.approval_status == "pending_phase5_policy"  # never approved by Phase 4
    data = json.loads(path.read_text())
    for key in ("proposal", "review", "scope_assessment", "task_spec_ref", "execution_plan_ref", "spec_diff",
                "limits_metadata", "schema_version"):
        assert key in data
    assert data["proposal"]["draft"]["scientific_invariants"] == INVARIANTS


def test_nested_record_units_and_wildcard_metrics_and_checks(tmp_path):
    import json as _json

    from research_autopilot.experiments.models import ArtifactRecord, CollectResult, JobState, JobStatus
    from research_autopilot.experiments.validator import validate
    from research_autopilot.implementation.experiment import Metric, NumericCheck, OutputFile
    deps, runs, *_ = make_exp(tmp_path, compute=FakeComputeBackend([OK]))
    m = run_experiment(deps, phase3_run_ids=runs)
    spec = next(iter(m.specs.values()))
    doc = {"results": [{"r": 0.5, "delta_e_hartree": 0.01}, {"r": 1.0, "delta_e_hartree": 0.03}]}
    out = tmp_path / "res.json"
    out.write_text(_json.dumps(doc))
    spec = spec.model_copy(deep=True, update={
        "outputs": [OutputFile(path="res.json", format="json", required=True, units={"delta_e_hartree": "Hartree"})],
        "inputs": []})
    spec.validation.metrics = [Metric(name="dE", definition="E_RHF-E_FCI", output="res.json",
                                      json_pointer="/results/*/delta_e_hartree", units="Hartree")]
    spec.validation.numeric_checks = [NumericCheck(name="dE_nonneg", output="res.json",
                                                   json_pointer="/results/*/delta_e_hartree", condition="ge", value=0.0)]
    collected = CollectResult(job_key="k", artifacts=[ArtifactRecord(path="res.json", stored_path=str(out),
                                                                     sha256="x", size_bytes=out.stat().st_size)],
                              missing=[], stdout_path="", stderr_path="")
    v = validate(spec, JobStatus(job_key="k", state=JobState.SUCCEEDED, exit_code=0), collected, tmp_path, {})
    assert v.status == "pass", v.failure_reason
    assert v.metrics == {"dE[/results/0/delta_e_hartree]": 0.01, "dE[/results/1/delta_e_hartree]": 0.03}
    doc["results"][1]["delta_e_hartree"] = -0.1
    out.write_text(_json.dumps(doc))
    v = validate(spec, JobStatus(job_key="k", state=JobState.SUCCEEDED, exit_code=0), collected, tmp_path, {})
    assert v.status == "fail" and "failing ['/results/1/delta_e_hartree']" in v.failure_reason


def test_job_waits_for_free_memory_instead_of_blocking(tmp_path):
    from research_autopilot.experiments import resources as res
    from phase4_fakes import host
    s = Phase4Settings(artifact_root=tmp_path, env_cache_dir=tmp_path, reserve_memory_gb=4, max_job_memory_gb_total=4)
    est = res.ResourceEstimate(cpu=1, memory_gb=2.0, gpu=0, walltime_minutes=5, basis="default", confidence="low", rationale=[])
    with pytest.raises(res.ResourceBusy):  # fits the 4GB cap, but only 2GB is free now
        res.ResourceManager(s, host(avail=2.5)).plan(est)
    with pytest.raises(res.ResourceError) as e:  # above the cap itself: a real block
        res.ResourceManager(s, host(avail=12)).plan(res.ResourceEstimate(cpu=1, memory_gb=3.0, gpu=0,
                                                                         walltime_minutes=5, basis="default", confidence="low", rationale=[]))
    assert not isinstance(e.value, res.ResourceBusy)
    # the scheduler re-samples the host and starts the job once memory is free
    deps, runs, *_ = make_exp(tmp_path, compute=FakeComputeBackend([OK]))
    hosts = iter([host(avail=0.6), host(avail=0.6), host(avail=0.6)])
    deps.detect_host = lambda: next(hosts, host())
    m = run_experiment(deps, phase3_run_ids=runs)
    assert m.state is ExperimentState.COMPLETED, m.block_reason
    assert any(e.kind == "waiting" for e in m.events)
