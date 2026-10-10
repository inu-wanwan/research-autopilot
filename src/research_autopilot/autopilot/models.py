"""Phase 5 models (docs/phase_specs/phase5.md). All schemas are versioned."""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from research_autopilot.models import new_id, utcnow

SCHEMA = "1.0"


# ---------------------------------------------------------------- delegation


class ParameterDomain(BaseModel):
    """Experiment parameters that may change without a human (everything else needs approval)."""

    name: str  # e.g. "replicates", "seed"
    kind: Literal["integer_range", "any_value", "enum"]
    min_value: float | None = None
    max_value: float | None = None
    allowed_values: list[str] = Field(default_factory=list)


DEFAULT_PARAMETER_DOMAINS = [
    ParameterDomain(name="replicates", kind="integer_range", min_value=1, max_value=5),
    ParameterDomain(name="seed", kind="any_value"),
    ParameterDomain(name="seeds", kind="any_value"),
    ParameterDomain(name="random_seed", kind="any_value"),
]


class Budget(BaseModel):
    api_usd: float = 5.0
    compute_cpu_hours: float = 4.0


class ApprovalPolicy(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    max_new_proposals_per_experiment: int = 3
    max_chain_depth: int = 3
    max_pending_proposals: int = 5
    max_backlog_tasks: int = 10
    parameter_domains: list[ParameterDomain] = Field(default_factory=lambda: list(DEFAULT_PARAMETER_DOMAINS))
    # Events that always need a human, regardless of scope evidence.
    always_human: list[str] = Field(default_factory=lambda: [
        "purpose_change", "target_change", "invariant_change", "metric_change", "acceptance_change",
        "unapproved_data_transfer", "dangerous_command", "main_merge", "budget_exceeded", "undecidable_scope",
    ])


class ResearchAuthorization(BaseModel):
    """The user's initial delegation for one root research task (and its follow-ups)."""

    schema_version: Literal["1.0"] = SCHEMA
    authorization_id: str = Field(default_factory=new_id)
    root_task_id: str
    approved_by: str  # human actor as recorded by the confirmation channel
    approved_at: datetime = Field(default_factory=utcnow)
    approval_request_id: str
    spec_hash: str  # hash of the authorized TaskSpec scientific content
    plan_hash: str | None = None
    purpose: str | None
    research_target: str | None  # title / target as approved
    hypothesis: str | None
    scientific_invariants: list[str]
    acceptance_criteria: list[str]
    environments: list[str] = Field(default_factory=lambda: ["local"])
    budget: Budget = Field(default_factory=Budget)
    policy: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    expires_at: datetime = Field(default_factory=lambda: utcnow() + timedelta(days=30))
    revoked: bool = False


# ---------------------------------------------------------------- approvals


class ApprovalKind(StrEnum):
    ONBOARDING = "onboarding"  # initial delegation
    PROPOSAL = "proposal"  # follow-up experiment outside automatic scope
    BUDGET = "budget"
    PLAN = "plan"  # plan needs a new scientific decision
    COMPLETION_REVIEW = "completion_review"  # cannot decide Done automatically


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVISION_REQUESTED = "revision_requested"
    EXPIRED = "expired"
    STALE = "stale"  # the underlying spec changed after the request


class ApprovalRequest(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    request_id: str = Field(default_factory=new_id)
    kind: ApprovalKind
    task_id: str  # Linear identifier the approval is attached to
    root_task_id: str
    spec_hash: str
    title: str
    what_changes: list[str]
    diff_from_original: list[str]
    budget_summary: str
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime = Field(default_factory=lambda: utcnow() + timedelta(days=7))
    status: ApprovalStatus = ApprovalStatus.PENDING
    linear_state_before: str | None = None
    autopilot_set_awaiting_at: datetime | None = None
    confirmation_ref: str | None = None  # Prefect flow run id of the confirmation form
    payload: dict = Field(default_factory=dict)  # e.g. proposal id, authorization draft


class ApprovalDecision(BaseModel):
    """Verified human decision (written only after all checks passed)."""

    schema_version: Literal["1.0"] = SCHEMA
    request_id: str
    decision: Literal["approve", "reject", "revise"]
    actor: str
    channel: Literal["prefect_form", "linear_status", "discord"]
    linear_state: str
    spec_hash: str
    comment: str = ""
    decided_at: datetime = Field(default_factory=utcnow)


class FormSubmission(BaseModel):
    """What the Prefect Human-in-the-loop form returns (untrusted until verified)."""

    request_id: str
    decision: Literal["approve", "reject", "revise"]
    spec_hash: str
    approver: str
    comment: str = ""
    submitted_at: datetime = Field(default_factory=utcnow)


class DiscordSubmission(BaseModel):
    """Discord interaction persisted before the approval controller evaluates it."""

    interaction_id: str
    request_id: str
    decision: Literal["approve", "reject", "revise"]
    spec_hash: str
    user_id: str
    guild_id: str
    channel_id: str
    comment: str = ""
    submitted_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------- proposals / outbox


class ResearchTaskProposal(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    proposal_id: str  # stable id from Phase 4 (ExperimentProposal.proposal_id)
    root_task_id: str
    parent_task_id: str
    authorization_id: str
    chain_depth: int
    title: str
    purpose: str
    implementation: str
    acceptance_criteria: list[str]
    scientific_invariants: list[str]
    hypothesis: str
    rationale: str
    review_summary: str
    resources: str
    evidence_artifacts: list[str]
    repository: str | None
    file_scope: str | None
    policy: Literal["auto", "human"]
    policy_reasons: list[str]


class OutboxState(StrEnum):
    PENDING = "pending"
    NOTION_CREATED = "notion_created"
    LINEAR_CREATED = "linear_created"
    LINKED = "linked"
    DONE = "done"
    DEAD = "dead"


class OutboxItem(BaseModel):
    key: str  # idempotency key (e.g. "proposal:<proposal_id>", "notes:<run>")
    kind: Literal["proposal_task", "notes", "linear_comment"]
    state: OutboxState = OutboxState.PENDING
    payload: dict
    notion_page_id: str | None = None
    linear_issue_id: str | None = None
    linear_identifier: str | None = None
    attempts: int = 0
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------- workflow


class WorkflowStage(StrEnum):
    DISCOVERED = "discovered"
    AWAITING_AUTHORIZATION = "awaiting_authorization"
    PLANNING = "planning"
    IMPLEMENTING = "implementing"
    EXPERIMENTING = "experimenting"
    PROPOSING = "proposing"
    COMPLETING = "completing"
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    DEAD_LETTER = "dead_letter"


class WorkflowRun(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    run_id: str = Field(default_factory=new_id)
    task_id: str
    root_task_id: str
    authorization_id: str | None = None
    stage: WorkflowStage = WorkflowStage.DISCOVERED
    plan_run_id: str | None = None
    plan_id: str | None = None
    plan_version: int = 0
    phase3_run_ids: list[str] = Field(default_factory=list)
    experiment_run_ids: list[str] = Field(default_factory=list)
    commits: list[str] = Field(default_factory=list)
    approval_request_ids: list[str] = Field(default_factory=list)
    proposal_ids: list[str] = Field(default_factory=list)
    notes_page_id: str | None = None
    flow_run_ids: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None
    waiting_for: str | None = None  # approval request id
    resume_stage: str | None = None  # stage to continue with after an approval
    chain_depth: int = 0  # 0 = user-created root task
    parent_task_id: str | None = None
    attempts: int = 0
    events: list[dict] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def event(self, kind: str, message: str) -> None:
        self.events.append({"at": utcnow().isoformat(), "kind": kind, "message": message})
        self.updated_at = utcnow()


class PolicyDecision(BaseModel):
    outcome: Literal["auto", "human", "reject"]
    reasons: list[str]
    categories: list[str]  # e.g. "invariant_change", "budget_exceeded"


class CriterionResult(BaseModel):
    criterion: str
    status: Literal["met", "not_met", "unverifiable"]
    evidence: list[str] = Field(description="Names of passing validation checks / metrics that support the status")
    note: str


class CompletionReview(BaseModel):
    """LLM completion reviewer output; evidence is re-checked deterministically."""

    criteria: list[CriterionResult]
    hypothesis_outcome: Literal["supported", "refuted", "inconclusive"]
    summary: str
    limitations: list[str]
    unresolved: list[str]


class CompletionAssessment(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    auto_done: bool
    reasons: list[str]
    deterministic_ok: bool
    criteria: list[CriterionResult]
    hypothesis_outcome: str | None
    scientific_risk: str | None
