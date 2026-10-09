"""Phase 4 fixtures: real Phase 3 runs (with Phase 3 fakes) -> ExperimentSpecs, fake compute / analyst / reviewer."""

import json
from pathlib import Path

from phase3_fakes import CONFIG, GOOD_CODE, GOOD_TEST, experiment_def, make_impl_deps
from research_autopilot.experiments.analyst import LlmRun
from research_autopilot.experiments.backends.fake import FakeComputeBackend
from research_autopilot.experiments.config import Phase4Settings
from research_autopilot.experiments.models import (
    AnalysisResult,
    HostResources,
    Observation,
    ParameterChange,
    ProposalDraft,
    ScientificReviewResult,
)
from research_autopilot.experiments.orchestrator import ExpDeps
from research_autopilot.implementation.orchestrator import implement
from research_autopilot.planning.agents import Usage

INVARIANTS = ["cc-pVDZ basis, frozen core."]
ACCEPTANCE = ["Energy error table for all geometries", "Same Hamiltonian as reference"]
RESULT_OK = json.dumps({"e_rhf": -1.1168, "e_fci": -1.1373, "e_corr": -0.0205})


def variant(exp_id: str, deps: list[str] | None = None) -> dict:
    d = json.loads(experiment_def(experiment_id=exp_id))
    d["resources"]["job_dependencies"] = deps or []
    return {"files": {"h2_energy.py": GOOD_CODE, "tests/test_h2.py": GOOD_TEST, "configs/h2.json": CONFIG,
                      ".autopilot/experiment.json": json.dumps(d)}}


def host(cpu=8, mem=16.0, avail=12.0) -> HostResources:
    return HostResources(platform="test", cpu_logical=cpu, cpu_limit=cpu, memory_total_gb=mem, memory_limit_gb=mem,
                         memory_available_gb=avail, gpus=[], container=None, load_avg_1m=0.1, notes=[])


def draft(title="Replicate at tighter convergence", invariants=None, acceptance=None, changes=None) -> ProposalDraft:
    return ProposalDraft(
        title=title, research_question="Is the correlation energy stable across replicates?",
        hypothesis="Replicates reproduce e_corr within 1e-8 Hartree.", purpose="Verify reproducibility of the H2 result.",
        implementation="Run the same experiment twice with fixed seeds.",
        acceptance_criteria=list(ACCEPTANCE if acceptance is None else acceptance),
        scientific_invariants=list(INVARIANTS if invariants is None else invariants),
        expected_information_value="Confirms numerical reproducibility.", dependencies=[], estimated_resources="1 CPU, 1 min",
        estimated_cost="negligible", risks_and_assumptions=["deterministic SCF"],
        parameter_changes=changes if changes is not None else [ParameterChange(name="replicates", from_value="1",
                                                                             to_value="2", kind="within_spec")])


class FakeAnalyst:
    model = "gpt-5.6-terra"

    def __init__(self, drafts=None, raises=None):
        self.drafts = drafts if drafts is not None else [draft()]
        self.raises = raises
        self.calls = []

    def analyze(self, payload):
        self.calls.append(payload)
        if self.raises:
            raise self.raises
        out = AnalysisResult(
            summary="ok", observed_facts=[Observation(statement="e_fci < e_rhf", evidence=["metric:correlation_energy"])],
            inferences=[], hypotheses=[], hypothesis_evaluation="consistent", uncertainties=[], anomalies=[],
            limitations=["mocked"], open_questions=[], proposals=self.drafts)
        return LlmRun(output=out, usage=Usage(input_tokens=1000, output_tokens=300), model=self.model, cost_usd=0.01)


def sci_review(verdict="accept", in_scope=True, inv=True, acc=True, judgement=False) -> ScientificReviewResult:
    return ScientificReviewResult(verdict=verdict, summary=verdict, scientific_validity="ok", usefulness="ok",
                                  contribution_to_purpose="ok", duplication="none", feasibility="ok", budget="ok",
                                  scope_within_original=in_scope, invariants_preserved=inv, acceptance_not_relaxed=acc,
                                  new_scientific_judgement_required=judgement, required_changes=[])


class FakeSciReviewer:
    model = "gpt-5.6-sol"

    def __init__(self, reviews=None):
        self.reviews = list(reviews or [])
        self.calls = []

    def review(self, payload):
        self.calls.append(payload)
        r = self.reviews.pop(0) if self.reviews else sci_review()
        return LlmRun(output=r, usage=Usage(input_tokens=800, output_tokens=200), model=self.model, cost_usd=0.02)


def make_exp(tmp_path: Path, variants: list[dict] | None = None, compute=None, analyst=None, reviewer=None,
             repair_codex: list[dict] | None = None, **settings_kw):
    """Run Phase 3 (fakes) once per variant, then build Phase 4 deps around the results."""
    variants = variants or [variant("h2-sto3g")]
    deps3, repo, rec, notion, linear = make_impl_deps(tmp_path, list(variants) + list(repair_codex or []))
    runs = []
    for _ in variants:
        m = implement(deps3, handoff_path=Path(rec.handoff_path))
        assert m.state.value == "completed", m.block_reasons
        runs.append(m.phase3_run_id)
    settings = Phase4Settings(artifact_root=tmp_path / "experiments", env_cache_dir=tmp_path / "envs",
                              retry_backoff_seconds=0, **settings_kw)
    deps = ExpDeps(settings=settings, phase2=deps3.phase2, store=deps3.store, manager=deps3.manager,
                   backend=compute or FakeComputeBackend(), registry=deps3.registry,
                   analyst=analyst if analyst is not None else FakeAnalyst(),
                   reviewer=reviewer if reviewer is not None else FakeSciReviewer(), impl_deps=deps3,
                   detect_host=lambda: host(), sleep=lambda s: None, prepare_env=False)
    return deps, runs, deps3, repo
