"""Phase 2 data models (docs/phase_specs/phase2.md §7).

``ExecutionPlan`` and ``ReviewResult`` are LLM output types: they avoid
defaults and free-form dicts so they map to strict JSON schemas, and their
validators enforce cross-field consistency (a plan whose steps reference
unknown ids is rejected as invalid output).
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


# ---------------------------------------------------------------- sources


class SourceRef(BaseModel):
    """Pointer to the exact external record a decision was based on."""

    system: Literal["notion", "linear"]
    id: str
    identifier: str | None = None
    url: str | None = None
    version: str | None = None  # Notion last_edited_time / Linear updatedAt
    fetched_at: datetime


class IssueRef(BaseModel):
    identifier: str
    title: str
    state_name: str
    state_type: str


class LinearIssue(BaseModel):
    id: str
    identifier: str
    title: str
    url: str
    description: str
    priority: int  # 0 none, 1 urgent ... 4 low
    state_id: str
    state_name: str
    state_type: str  # backlog / unstarted / started / completed / canceled / triage
    team_id: str
    labels: list[str]
    project_id: str | None
    blocked_by: list[IssueRef]
    notion_page_ids: list[str]
    created_at: str
    updated_at: str
    fetched_at: datetime

    def source_ref(self) -> SourceRef:
        return SourceRef(
            system="linear", id=self.id, identifier=self.identifier, url=self.url,
            version=self.updated_at, fetched_at=self.fetched_at,
        )


class NotionTask(BaseModel):
    page_id: str
    url: str
    title: str
    status: str | None
    pending: bool
    linear_issue_id: str | None
    category: str | None
    last_edited_time: str
    sections: dict[str, str]
    other_sections: dict[str, str]
    preamble: str
    fetched_at: datetime
    # Phase 6A execution-control properties.  ``status`` above remains the
    # Linear mirror and is deliberately never used for this state machine.
    run_requested: bool = False
    execution_status: str | None = None
    last_run_url: str | None = None
    execution_error: str | None = None
    # Phase 6B self-relation.  Keep both directions because Notion exposes
    # them as separate properties even though they target the same data source.
    parent_page_ids: list[str] = Field(default_factory=list)
    child_page_ids: list[str] = Field(default_factory=list)
    origin: str | None = None            # Phase 10: Human / RA
    approval_status: str | None = None   # Phase 10: projection of backend approval events (read-only meaning)

    def source_ref(self) -> SourceRef:
        return SourceRef(
            system="notion", id=self.page_id, identifier=self.linear_issue_id, url=self.url,
            version=self.last_edited_time, fetched_at=self.fetched_at,
        )


class TaskSpec(BaseModel):
    """Research task specification as given to the agents. Sourced from Notion
    (the spec SSOT); never modified by the system."""

    task_id: str
    title: str
    category: str | None
    purpose: str | None
    background: str | None
    scientific_rationale: str | None
    specification: str | None
    acceptance_criteria: str | None
    dependencies_note: str | None
    scientific_invariants: str | None
    artifact_requirements: str | None
    other_sections: dict[str, str]
    preamble: str | None
    sources: list[SourceRef]
    # Phase 3 additions (optional for backward compatibility)
    repository: str | None = None
    file_scope: str | None = None
    non_goals: str | None = None
    input_artifacts: str | None = None
    literature: str | None = None  # Phase 8
    # Phase 10 research objective (scope-level) sections
    research_objective: str | None = None
    budget: str | None = None
    success_criteria: str | None = None
    stop_conditions: str | None = None
    autonomy: str | None = None

    @classmethod
    def from_sources(cls, issue: LinearIssue, page: NotionTask) -> "TaskSpec":
        s = page.sections
        return cls(
            task_id=issue.identifier,
            title=page.title or issue.title,
            category=page.category,
            purpose=s.get("purpose"),
            background=s.get("background"),
            scientific_rationale=s.get("scientific_rationale"),
            specification=s.get("specification"),
            acceptance_criteria=s.get("acceptance_criteria"),
            dependencies_note=s.get("dependencies"),
            scientific_invariants=s.get("scientific_invariants"),
            artifact_requirements=s.get("artifact_requirements"),
            other_sections=page.other_sections,
            preamble=page.preamble or None,
            sources=[issue.source_ref(), page.source_ref()],
            repository=s.get("repository"),
            file_scope=s.get("file_scope"),
            non_goals=s.get("non_goals"),
            input_artifacts=s.get("input_artifacts"),
            literature=s.get("literature"),
            research_objective=s.get("research_objective"),
            budget=s.get("budget"),
            success_criteria=s.get("success_criteria"),
            stop_conditions=s.get("stop_conditions"),
            autonomy=s.get("autonomy"),
        )

    def section(self, name: str) -> str | None:
        field = "dependencies_note" if name == "dependencies" else name
        value = getattr(self, field)
        return value if value and value.strip() else None


# ---------------------------------------------------------------- LLM outputs


class PlanStep(BaseModel):
    id: str = Field(description="Short unique step id, e.g. S1")
    title: str
    description: str
    depends_on: list[str] = Field(description="Ids of steps that must finish first")
    inputs: list[str]
    outputs: list[str]
    verification: str = Field(description="How the step's result is checked")


class AcceptanceMapping(BaseModel):
    criterion: str = Field(description="Acceptance criterion quoted from the task spec")
    covered_by_steps: list[str]
    verification: str


class ResourceEstimate(BaseModel):
    compute: str
    wall_time: str
    memory: str
    notes: str


class ExecutionPlan(BaseModel):
    summary: str
    steps: list[PlanStep]
    execution_order: list[str] = Field(description="All step ids in execution order")
    environment: list[str] = Field(description="Required environment, libraries, versions")
    constraints: list[str] = Field(description="Implementation / experimental constraints, incl. scientific invariants")
    verification_methods: list[str]
    acceptance_mapping: list[AcceptanceMapping]
    resource_estimate: ResourceEstimate
    open_questions: list[str]
    approvals_needed: list[str] = Field(description="Decisions that need human approval before execution")
    literature_requests: list[str] = Field(default_factory=list, description=(
        "Literature questions whose evidence would materially change this plan (empty if none); Research "
        "Autopilot investigates them and asks for a revised plan"))

    @model_validator(mode="after")
    def _consistent(self) -> "ExecutionPlan":
        ids = [s.id for s in self.steps]
        if not ids:
            raise ValueError("plan has no steps")
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate step ids")
        known = set(ids)
        for s in self.steps:
            if missing := set(s.depends_on) - known:
                raise ValueError(f"step {s.id} depends on unknown steps {sorted(missing)}")
        if sorted(self.execution_order) != sorted(ids):
            raise ValueError("execution_order must list every step id exactly once")
        position = {sid: i for i, sid in enumerate(self.execution_order)}
        for s in self.steps:
            if any(position[d] > position[s.id] for d in s.depends_on):
                raise ValueError(f"execution_order runs {s.id} before its dependencies")
        for m in self.acceptance_mapping:
            if missing := set(m.covered_by_steps) - known:
                raise ValueError(f"acceptance mapping references unknown steps {sorted(missing)}")
        return self


ReviewAspect = Literal[
    "spec_consistency", "scientific_validity", "feasibility", "reproducibility",
    "evaluation", "resources_cost", "research_risk",
]


class ReviewCheck(BaseModel):
    aspect: ReviewAspect
    assessment: Literal["ok", "concern", "fail"]
    comment: str


class RevisionRequest(BaseModel):
    aspect: ReviewAspect
    severity: Literal["blocking", "major", "minor"]
    issue: str
    requested_change: str
    step_ids: list[str]


class ReviewResult(BaseModel):
    verdict: Literal["approved", "revision_required", "blocked"]
    summary: str
    checks: list[ReviewCheck] = Field(description="One entry per review aspect")
    revision_requests: list[RevisionRequest]
    blocking_reasons: list[str] = Field(description="Why the task cannot proceed (verdict=blocked)")

    @model_validator(mode="after")
    def _consistent(self) -> "ReviewResult":
        if self.verdict == "revision_required" and not self.revision_requests:
            raise ValueError("revision_required needs at least one revision request")
        if self.verdict == "blocked" and not self.blocking_reasons:
            raise ValueError("blocked needs at least one blocking reason")
        if self.verdict == "approved" and any(r.severity == "blocking" for r in self.revision_requests):
            raise ValueError("approved review cannot contain blocking revision requests")
        return self


# ---------------------------------------------------------------- decisions


class DecisionStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"
    INSUFFICIENT_SPEC = "insufficient_spec"
    TRANSIENT_FAILURE = "transient_failure"


class BlockedCode(StrEnum):
    NOT_FOUND = "not_found"
    OUT_OF_SCOPE = "out_of_scope"
    NOT_APPROVED = "not_approved"
    INVALID_STATUS = "invalid_status"
    DEPENDENCY_INCOMPLETE = "dependency_incomplete"
    CONCURRENT_RUN = "concurrent_run"
    DATA_INCONSISTENCY = "data_inconsistency"
    MISSING_SPEC = "missing_spec"
    REVIEW_BLOCKED = "review_blocked"
    REVISION_LIMIT = "revision_limit"
    LLM_OUTPUT_INVALID = "llm_output_invalid"
    CONNECTOR_ERROR = "connector_error"
    TRANSIENT_ERROR = "transient_error"
    STATE_CHANGED = "state_changed"


class BlockedReason(BaseModel):
    code: BlockedCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class HumanApproval(BaseModel):
    """Evidence of *human* approval (distinct from the Reviewer's verdict)."""

    linear_state: str
    linear_state_type: str
    notion_pending: bool | None
    approved: bool


class LinearUpdateResult(BaseModel):
    action: Literal["updated", "noop", "skipped_dry_run", "skipped_not_ready", "failed"]
    from_state: str | None = None
    to_state: str | None = None
    error: str | None = None
    at: datetime = Field(default_factory=utcnow)


class WorkflowDecision(BaseModel):
    decision_id: str = Field(default_factory=new_id)
    status: DecisionStatus
    task_id: str | None
    reasons: list[BlockedReason] = Field(default_factory=list)
    rationale: str
    sources: list[SourceRef] = Field(default_factory=list)
    fetched_at: datetime | None = None
    plan_id: str | None = None
    plan_run_id: str
    flow_run_id: str | None = None
    human_approval: HumanApproval | None = None
    reviewer_verdict: str | None = None
    linear_update: LinearUpdateResult | None = None
    decided_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------- run record


class LlmCall(BaseModel):
    agent: Literal["planner", "reviewer"]
    model: str
    iteration: int
    attempt: int
    started_at: datetime
    finished_at: datetime
    ok: bool
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    requests: int = 0
    estimated_cost_usd: float | None = None  # None = price unknown, not estimated
    error: str | None = None


class RunEvent(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    kind: str  # e.g. retry, error, gate, lease
    message: str


class PlanAttempt(BaseModel):
    plan_id: str
    iteration: int
    plan: ExecutionPlan


class ReviewAttempt(BaseModel):
    plan_id: str
    iteration: int
    review: ReviewResult


class CostSummary(BaseModel):
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float | None
    cost_complete: bool  # False when any call used a model without a configured price


class PlanRunRecord(BaseModel):
    plan_run_id: str
    task_id: str | None
    flow_run_id: str | None
    mode: Literal["dry_run", "apply", "check_only"]
    started_at: datetime
    finished_at: datetime | None = None
    planner_model: str
    reviewer_model: str
    linear_snapshot: LinearIssue | None = None
    notion_snapshot: NotionTask | None = None
    task_spec: TaskSpec | None = None
    plans: list[PlanAttempt] = Field(default_factory=list)
    reviews: list[ReviewAttempt] = Field(default_factory=list)
    llm_calls: list[LlmCall] = Field(default_factory=list)
    events: list[RunEvent] = Field(default_factory=list)
    decision: WorkflowDecision | None = None
    cost: CostSummary | None = None
    literature_cost_usd: float = 0.0  # Phase 8 research run for this plan (reused reports cost nothing)
    external_cost_usd: float = 0.0  # Phase 9 onboarding (test planning) during this plan run
    artifact_path: str | None = None
    handoff_path: str | None = None


class ExecutionHandoff(BaseModel):
    """Structured input for the Phase 3 Implementer. Only produced for ready decisions."""

    schema_version: Literal["1"] = "1"
    task_id: str
    plan_id: str
    plan_run_id: str
    decision_id: str
    created_at: datetime = Field(default_factory=utcnow)
    sources: list[SourceRef]
    purpose: str | None
    scientific_invariants: str | None
    acceptance_criteria: str | None
    artifact_requirements: str | None
    plan: ExecutionPlan
    review: ReviewResult
    human_approval: HumanApproval
    approvals_needed: list[str]
    open_questions: list[str]
    # Phase 3 additions (optional; absent in handoffs written before Phase 3)
    phase2_mode: Literal["dry_run", "apply"] | None = None
    specification: str | None = None
    repository: str | None = None
    file_scope: str | None = None
    non_goals: str | None = None
    input_artifacts: str | None = None
    # Phase 7A: frozen catalog resolution (verified artifacts) the plan was made against.
    catalog_resolution: dict | None = None
    # Phase 8: literature reports (Research Artifacts) the plan was grounded on.
    literature_reports: list[str] = Field(default_factory=list)
    external_implementations: list[dict] = Field(default_factory=list)  # Phase 9 registered entries (no grant implied)
