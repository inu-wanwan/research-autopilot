"""Phase 3 data models (docs/phase_specs/phase3.md).

``CodeReviewResult`` is an LLM output type (strict JSON schema: no defaults,
no free-form dicts). Everything else is produced deterministically.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from research_autopilot.models import ExecutionPlan, SourceRef, new_id, utcnow

Level = Literal["low", "medium", "high"]
Tier = Literal["lightweight", "standard", "high_reasoning"]
BackendName = Literal["codex", "claude", "codex_api"]  # codex_api: Codex CLI billed to the OpenAI API key

TIER_ORDER: list[Tier] = ["lightweight", "standard", "high_reasoning"]


class RepoConfig(BaseModel):
    """One entry of the repository registry (config/research_repos.toml)."""

    id: str
    path: str
    url: str | None = None
    base_branch: str = "main"
    protected_paths: list[str] = Field(default_factory=list)
    test_command: list[str] | None = None
    lint_command: list[str] | None = None
    typecheck_command: list[str] | None = None
    check_timeout_seconds: int = 600
    # Phase 7A: where this repository lives on other environments (e.g. {"eagle": "~/..."}); ``path`` is this host.
    locations: dict[str, str] = Field(default_factory=dict)


class ComplexityAssessment(BaseModel):
    implementation_complexity: Level
    scientific_risk: Level
    classification_reason: list[str]
    signals: dict[str, int | float | list[str]]


class ModelChoice(BaseModel):
    backend: BackendName
    tier: Tier
    model: str
    reasoning_effort: str | None = None


class ImplementationTask(BaseModel):
    """Everything a CodingBackend needs. Built from the Phase 2 handoff +
    repository registry; never from LLM output."""

    task_id: str
    phase2_plan_run_id: str
    plan_id: str
    phase3_run_id: str
    repo_id: str
    repo_path: str
    repo_url: str | None
    base_branch: str
    base_commit: str
    branch: str
    worktree_path: str
    allowed_file_scope: list[str]
    protected_paths: list[str]
    purpose: str | None
    specification: str | None
    scientific_invariants: str | None
    acceptance_criteria: str | None
    non_goals: str | None
    input_artifacts: str | None
    artifact_requirements: str | None
    plan: ExecutionPlan
    sources: list[SourceRef]
    catalog_artifacts: list[dict] = Field(default_factory=list)  # Phase 7A verified read-only inputs
    external_implementations: list[dict] = Field(default_factory=list)  # Phase 9 Registry entries usable via external_code


class BackendStatus(BaseModel):
    backend: BackendName
    installed: bool
    version: str | None = None
    authenticated: bool = False
    billing: Literal["subscription", "api", "unknown"] = "unknown"
    detail: str = ""


ResultStatus = Literal[
    "completed", "failed", "quota_exhausted", "rate_limited", "transient_error",
    "process_error", "timeout", "auth_error", "budget_exceeded",
]


class Usage(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, other: "Usage") -> "Usage":
        return Usage(**{k: getattr(self, k) + getattr(other, k) for k in Usage.model_fields})

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class ImplementationResult(BaseModel):
    """Normalized backend outcome. Conversation text is NOT included; only a
    truncated final summary and a path to the raw log."""

    backend: BackendName
    model: str
    status: ResultStatus
    exit_code: int | None
    summary: str = ""
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float | None = None  # None = unknown (never guessed)
    pid: int | None = None
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime = Field(default_factory=utcnow)
    error: str | None = None
    log_path: str | None = None


# ---------------------------------------------------------------- review


class CheckResult(BaseModel):
    name: str
    command: list[str] | None
    ok: bool
    skipped: bool = False
    exit_code: int | None = None
    output_tail: str = ""


class DangerFinding(BaseModel):
    file: str
    pattern: str
    line: str
    severity: Literal["block", "warn"]


class DeterministicReport(BaseModel):
    changed_files: list[str]
    out_of_scope_files: list[str]
    protected_violations: list[str]
    danger_findings: list[DangerFinding]
    checks: list[CheckResult]
    diff_stat: str

    @property
    def scope_ok(self) -> bool:
        return not self.out_of_scope_files and not self.protected_violations and not any(
            f.severity == "block" for f in self.danger_findings)

    @property
    def checks_ok(self) -> bool:
        return all(c.ok or c.skipped for c in self.checks)

    @property
    def passed(self) -> bool:
        return self.scope_ok and self.checks_ok and bool(self.changed_files)


ReviewAspect = Literal[
    "spec_consistency", "scientific_correctness", "numerical_stability", "reproducibility",
    "evaluation_metrics", "error_handling", "risky_assumptions", "data_handling", "unapproved_changes",
]


class CodeFinding(BaseModel):
    aspect: ReviewAspect
    severity: Literal["blocking", "major", "minor"]
    file: str
    issue: str
    requested_change: str
    criterion: str = Field(default="", description=(
        "Definition of Done id (B1.., D1..) or 'invariant' this finding breaks; a defect outside the Definition of "
        "Done must name the item or invariant it endangers, else it is only a suggestion"))


class CriterionAssessment(BaseModel):
    id: str = Field(description="Definition of Done id (B1.., D1..)")
    status: Literal["met", "not_met", "cannot_judge"]
    evidence: str = Field(description="Where in the diff / tests this was checked, and why")


class InfeasibilityClaim(BaseModel):
    criteria: list[str] = Field(description="Definition of Done ids that cannot be satisfied as written")
    evidence: str = Field(description="Concrete evidence (API / environment facts and how they were checked)")


class CodeReviewResult(BaseModel):
    """Independent LLM reviewer output."""

    verdict: Literal["pass", "revise", "blocked"]
    summary: str
    findings: list[CodeFinding]
    invariant_violations: list[str] = Field(description="Scientific invariants the change violates; empty if none")
    unverified_items: list[str] = Field(description="Claims that tests cannot prove (e.g. scientific correctness)")
    criteria: list[CriterionAssessment] = Field(default_factory=list, description=(
        "One assessment per Definition of Done item (empty only when the task has no Definition of Done)"))
    suggestions: list[str] = Field(default_factory=list, description=(
        "Improvements outside the Definition of Done; recorded, never a reason to send the work back"))
    dod_infeasible: InfeasibilityClaim | None = Field(default=None, description=(
        "Only when a Definition of Done item is impossible as written (not merely hard), with evidence"))

    @model_validator(mode="after")
    def _consistent(self) -> "CodeReviewResult":
        if self.verdict == "revise" and not self.findings and not self.invariant_violations \
                and not any(c.status == "not_met" for c in self.criteria):
            raise ValueError("revise needs at least one finding")
        if self.verdict == "pass" and self.invariant_violations:
            # (blocking findings are judged against the Definition of Done by the reviewer module, not here)
            raise ValueError("pass cannot contain invariant violations")
        return self


FailureCategory = Literal[
    "tests", "lint", "typecheck", "no_changes", "review_findings", "experiment_spec", "invariant_violation",
    "scope_violation", "dangerous_change", "science_change", "dod_infeasible",
]

# Categories where escalating to a stronger model is allowed if they persist.
ESCALATABLE: set[str] = {"tests", "lint", "typecheck", "no_changes", "review_findings", "experiment_spec"}


class ReviewOutcome(BaseModel):
    verdict: Literal["pass", "revise", "blocked"]
    reason: str
    categories: list[FailureCategory]
    deterministic: DeterministicReport
    llm: CodeReviewResult | None
    reviewer: ModelChoice | None
    reviewer_usage: Usage = Field(default_factory=Usage)
    reviewer_cost_usd: float | None = None
    revision_requests: list[str]


# ---------------------------------------------------------------- manifest


class RunState(StrEnum):
    PENDING = "pending"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class AttemptRecord(BaseModel):
    number: int  # 1 = initial implementation, 2..3 = revisions
    kind: Literal["initial", "revision"]
    choice: ModelChoice
    escalation_reason: str | None = None
    fallback_reason: str | None = None
    result: ImplementationResult | None = None
    commit: str | None = None
    diff_path: str | None = None
    review: ReviewOutcome | None = None
    recovered: bool = False


class GitAuditEntry(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    args: list[str]
    cwd: str
    exit_code: int


class ManifestEvent(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    kind: str
    message: str


class BlockReason(BaseModel):
    code: Literal[
        "not_approved", "handoff_invalid", "spec_changed", "repository_invalid", "scope_missing",
        "concurrent_run", "budget_exceeded", "time_exceeded", "revision_limit", "review_blocked",
        "invariant_violation", "unapproved_change", "backend_unavailable", "auth_error", "fallback_unavailable",
        "connector_error", "transient_error", "experiment_spec_invalid", "repair_not_allowed", "dod_infeasible",
    ]
    message: str


class ImplementationManifest(BaseModel):
    schema_version: Literal["1"] = "1"
    phase3_run_id: str = Field(default_factory=new_id)
    task_id: str
    phase2_plan_run_id: str | None = None
    plan_id: str | None = None
    handoff_path: str | None = None
    state: RunState = RunState.PENDING
    recovery_state: str | None = None  # e.g. "resumed_after_interrupt"
    failure_classification: str | None = None
    block_reasons: list[BlockReason] = Field(default_factory=list)
    flow_run_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    task: ImplementationTask | None = None
    assessment: ComplexityAssessment | None = None
    implementer_choice: ModelChoice | None = None
    attempts: list[AttemptRecord] = Field(default_factory=list)
    backend_invocations: int = 0
    fallback_used: bool = False
    fallbacks_used: list[BackendName] = Field(default_factory=list)  # in switch order
    active_backend_pid: int | None = None
    active_backend: BackendName | None = None
    usage: Usage = Field(default_factory=Usage)
    cost_usd_known: float = 0.0
    cost_unknown_calls: int = 0
    implementation_commit: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    final_review: ReviewOutcome | None = None
    git_audit: list[GitAuditEntry] = Field(default_factory=list)
    events: list[ManifestEvent] = Field(default_factory=list)
    artifact_paths: dict[str, str] = Field(default_factory=dict)
    experiment_spec_id: str | None = None
    # Phase 4 -> 3 repair lineage (None / 0 for an original implementation run)
    parent_run_id: str | None = None
    failure_report_id: str | None = None
    repair_round: int = 0
    # Budget used by earlier runs of the same chain; budgets are never reset by a repair.
    inherited_usage: Usage = Field(default_factory=Usage)
    inherited_cost_usd: float = 0.0
    continued_from: str | None = None  # a blocked run cut off by a non-task limit, continued in its worktree
    dod_infeasible: dict | None = None  # {"criteria", "evidence", "by"}: sent back to Phase 2 by the engine
    inherited_review: "ReviewOutcome | None" = None  # continuation: the cut-off run's last review (requests, DoD)

    @property
    def revisions_used(self) -> int:
        return sum(1 for a in self.attempts if a.kind == "revision")

    def event(self, kind: str, message: str) -> None:
        self.events.append(ManifestEvent(kind=kind, message=message))
        self.updated_at = utcnow()


class Phase4Handoff(BaseModel):
    """Input for the Phase 4 Experiment Runner. Produced only for completed runs."""

    schema_version: Literal["1"] = "1"
    task_id: str
    phase2_plan_run_id: str
    plan_id: str
    phase3_run_id: str
    created_at: datetime = Field(default_factory=utcnow)
    repository: dict[str, str | None]  # id, path, url
    branch: str
    base_commit: str
    implementation_commit: str  # fixed commit to run experiments from
    changed_files: list[str]
    checks: list[CheckResult]
    reviewer_verdict: Literal["pass"]
    unverified_items: list[str]
    scientific_invariants: str | None
    acceptance_criteria: str | None
    plan: ExecutionPlan
    manifest_path: str
    sources: list[SourceRef]
    experiment_spec_id: str | None = None
    experiment_spec_path: str | None = None
    parent_run_id: str | None = None
    failure_report_id: str | None = None
