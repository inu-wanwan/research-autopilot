"""Phase 4 data models (docs/phase_specs/phase4.md). All persisted / handed-off
models carry a ``schema_version``. ExperimentSpec and FailureReport are reused
from Phase 3 (``implementation.experiment`` / ``implementation.failure``)."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from research_autopilot.implementation.experiment import ExperimentSpec
from research_autopilot.implementation.failure import ErrorClass
from research_autopilot.models import new_id, utcnow

SCHEMA = "1.0"


# ---------------------------------------------------------------- resources


class HostResources(BaseModel):
    platform: str
    cpu_logical: int
    cpu_limit: float  # after cgroup / container limits
    memory_total_gb: float
    memory_limit_gb: float  # after cgroup limits
    memory_available_gb: float
    gpus: list[dict[str, str | float]]  # [{"name", "vendor", "memory_gb", "kind": "cuda|metal"}]
    container: str | None  # "docker" / "wsl" / None
    load_avg_1m: float
    notes: list[str]


class ResourceEstimate(BaseModel):
    cpu: int
    memory_gb: float
    gpu: int
    walltime_minutes: float
    basis: Literal["spec_required", "spec_estimate", "history", "default"]
    confidence: Literal["low", "medium", "high"]
    rationale: list[str]


class Allocation(BaseModel):
    cpu: int
    memory_gb: float
    gpu: int
    walltime_seconds: int
    threads: int
    estimate: ResourceEstimate


# ---------------------------------------------------------------- jobs


class JobState(StrEnum):
    SUBMITTING = "submitting"  # ledger row written, process not yet confirmed
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"  # process exited 0 (NOT the same as validated)
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"  # cannot determine; never resubmitted automatically
    RESOURCE_EXCEEDED = "resource_exceeded"

    @property
    def terminal(self) -> bool:
        return self in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.LOST,
                        JobState.RESOURCE_EXCEEDED)


class JobSpec(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    job_key: str  # idempotency key: run + experiment + attempt
    experiment_run_id: str
    experiment_id: str
    spec_id: str
    commit: str
    argv: list[str]  # resolved argv (launcher replaced by the environment's interpreter)
    working_directory: str  # relative to the checkout
    env: dict[str, str]  # non-secret variables only (threads etc.)
    secret_env_names: list[str]  # passed through from the host at launch; values never stored
    allocation: Allocation
    run_dir: str  # absolute, per job (immutable namespace)
    checkout_dir: str
    outputs: list[str]
    heartbeat_seconds: int


class JobHandle(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    job_key: str
    backend: Literal["local", "slurm", "fake"]
    external_id: str | None  # pid (local) / Slurm job id
    run_dir: str
    submitted_at: datetime = Field(default_factory=utcnow)


class JobStatus(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    job_key: str
    state: JobState
    external_id: str | None = None
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    signal: int | None = None
    peak_rss_gb: float | None = None
    cpu_seconds: float | None = None
    reason: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None


class ArtifactRecord(BaseModel):
    path: str  # relative output path as declared in the spec
    stored_path: str  # immutable copy
    sha256: str
    size_bytes: int


class CollectResult(BaseModel):
    job_key: str
    artifacts: list[ArtifactRecord]
    missing: list[str]
    stdout_path: str
    stderr_path: str


# ---------------------------------------------------------------- validation


CheckStatus = Literal["pass", "fail", "not_verified"]


class ValidationCheck(BaseModel):
    name: str
    kind: Literal["output_exists", "format", "finite", "numeric_check", "metric", "units", "input_integrity",
                  "acceptance_criterion", "scientific_invariant", "exit_status"]
    status: CheckStatus
    detail: str
    value: float | str | None = None


class ValidationResult(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    experiment_id: str
    job_key: str
    status: Literal["pass", "fail"]  # pass only if every machine-checkable check passed
    fully_verified: bool  # False when any acceptance criterion / invariant is only not_verified
    checks: list[ValidationCheck]
    metrics: dict[str, float]
    failure_reason: str | None = None


# ---------------------------------------------------------------- triage


class TriageDecision(BaseModel):
    error_class: ErrorClass
    action: Literal["retry", "retry_more_resources", "phase3_repair", "blocked"]
    reason: str
    evidence: list[str]


# ---------------------------------------------------------------- analysis / proposals


class Observation(BaseModel):
    statement: str
    evidence: list[str] = Field(description="Metric names / artifact paths / run ids backing this statement")


class AnalysisResult(BaseModel):
    """LLM Analyst output. Facts, inferences and hypotheses are kept apart."""

    summary: str
    observed_facts: list[Observation]
    inferences: list[Observation]
    hypotheses: list[Observation]
    hypothesis_evaluation: str
    uncertainties: list[str]
    anomalies: list[str]
    limitations: list[str]
    open_questions: list[str]
    proposals: list["ProposalDraft"]


class ParameterChange(BaseModel):
    name: str
    from_value: str
    to_value: str
    kind: Literal["within_spec", "new_target", "method", "invariant", "acceptance", "objective", "other"]


class ProposalDraft(BaseModel):
    """What the Analyst proposes; the orchestrator adds identifiers / provenance."""

    title: str
    research_question: str
    hypothesis: str
    purpose: str
    implementation: str
    acceptance_criteria: list[str]
    scientific_invariants: list[str]
    expected_information_value: str
    dependencies: list[str]
    estimated_resources: str
    estimated_cost: str
    risks_and_assumptions: list[str]
    parameter_changes: list[ParameterChange]


AnalysisResult.model_rebuild()


class ExperimentProposal(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    proposal_id: str = Field(default_factory=new_id)
    dedupe_key: str  # hash of normalized content; identical proposals are stored once
    parent_task_id: str
    source_experiment_run_id: str
    source_artifacts: list[str]
    source_commit: str
    chain_depth: int  # 1 = proposed from an original task's results
    draft: ProposalDraft
    created_at: datetime = Field(default_factory=utcnow)


class ScientificReviewResult(BaseModel):
    """Independent LLM Scientific Reviewer output."""

    verdict: Literal["accept", "revise", "reject", "blocked"]
    summary: str
    scientific_validity: str
    usefulness: str
    contribution_to_purpose: str
    duplication: str
    feasibility: str
    budget: str
    scope_within_original: bool
    invariants_preserved: bool
    acceptance_not_relaxed: bool
    new_scientific_judgement_required: bool
    required_changes: list[str]


class ScopeAssessment(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    within_scope: bool
    rationale: str
    proposed_changes: list[str]
    requires_human_decision: bool
    deterministic_findings: list[str]
    reviewer_opinion: bool | None  # Reviewer's scope_within_original (advisory only)

    @model_validator(mode="after")
    def _consistent(self) -> "ScopeAssessment":
        if self.requires_human_decision and self.within_scope:
            raise ValueError("an assessment requiring a human decision cannot be within_scope")
        return self


class ReviewedExperimentProposal(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    proposal: ExperimentProposal
    review: ScientificReviewResult | None
    review_error: str | None = None
    scope_assessment: ScopeAssessment
    task_spec_ref: dict[str, str | None]  # task_id, plan_id, phase2_plan_run_id, notion page / version
    execution_plan_ref: dict[str, str | None]
    spec_diff: list[str]
    # Phase 4 never approves; Phase 5's deterministic Approval Policy Gate decides.
    approval_status: Literal["pending_phase5_policy"] = "pending_phase5_policy"
    limits_metadata: dict[str, int | float | str]


# ---------------------------------------------------------------- run manifest


class ExperimentState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    VALIDATING = "validating"
    REPAIRING = "repairing"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class JobRecord(BaseModel):
    experiment_id: str
    spec_id: str
    commit: str
    attempt: int
    job_key: str
    handle: JobHandle | None = None
    status: JobStatus | None = None
    collect: CollectResult | None = None
    validation: ValidationResult | None = None
    triage: TriageDecision | None = None
    allocation: Allocation | None = None


class ManifestEvent(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    kind: str
    message: str


class LlmUsage(BaseModel):
    agent: Literal["analyst", "reviewer"]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    error: str | None = None
    at: datetime = Field(default_factory=utcnow)


class ExperimentManifest(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    experiment_run_id: str = Field(default_factory=new_id)
    task_id: str
    phase3_run_id: str
    phase2_plan_run_id: str | None = None
    plan_id: str | None = None
    state: ExperimentState = ExperimentState.PENDING
    block_reason: str | None = None
    flow_run_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    specs: dict[str, ExperimentSpec] = Field(default_factory=dict)  # experiment_id -> current spec
    spec_history: list[str] = Field(default_factory=list)  # all spec ids used (incl. superseded)
    host: HostResources | None = None
    environment: dict[str, dict[str, str]] = Field(default_factory=dict)  # experiment_id -> provenance
    jobs: list[JobRecord] = Field(default_factory=list)
    serialization_notes: list[str] = Field(default_factory=list)
    repairs: list[dict[str, str]] = Field(default_factory=list)  # report_id, phase3_run_id, result
    roundtrips: int = 0
    retries: int = 0
    analysis: AnalysisResult | None = None
    reviewed_proposals: list[str] = Field(default_factory=list)  # proposal ids
    llm_usage: list[LlmUsage] = Field(default_factory=list)
    events: list[ManifestEvent] = Field(default_factory=list)
    artifact_paths: dict[str, str] = Field(default_factory=dict)

    def event(self, kind: str, message: str) -> None:
        self.events.append(ManifestEvent(kind=kind, message=message))
        self.updated_at = utcnow()

    @property
    def llm_cost_known(self) -> float:
        return round(sum(u.cost_usd or 0.0 for u in self.llm_usage), 6)


class Phase5Handoff(BaseModel):
    """Machine-readable result of Phase 4 for the (future) Phase 5 Approval Policy Gate."""

    schema_version: Literal["1.0"] = SCHEMA
    experiment_run_id: str
    task_id: str
    phase3_run_id: str
    final_commit: str | None
    state: ExperimentState
    validations: list[ValidationResult]
    analysis_path: str | None
    reviewed_proposal_paths: list[str]
    manifest_path: str
    created_at: datetime = Field(default_factory=utcnow)
