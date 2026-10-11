"""Phase 10 records (docs/phase_specs/phase10.md).

Every scope value carries the verbatim spec excerpt it came from; a value whose excerpt cannot be found in the
task's Notion text is never treated as stated (the Planner may not invent research conditions or numbers).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from research_autopilot.models import new_id, utcnow

SCHEMA = "1.0"
ItemStatus = Literal["stated", "missing", "ambiguous", "conflicting", "ra_default"]


def stable_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


# ---------------------------------------------------------------- research scope


class ScopeItem(BaseModel):
    value: str = ""
    quote: str = ""                  # verbatim excerpt of the task's Notion text
    status: ItemStatus = "missing"
    note: str = ""


class ConditionBound(BaseModel):
    """An approved range for something the research may vary (e.g. bond length, number of geometries)."""

    name: str                        # snake_case parameter name used by planned tasks
    kind: Literal["range", "values", "max_count", "fixed"]
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    values: list[str] = Field(default_factory=list)
    count: int | None = None         # max_count: largest number of distinct values over the whole research
    applies_to: str | None = None    # max_count: the parameter it counts (e.g. bond_length_r)
    quote: str = ""
    status: ItemStatus = "stated"
    note: str = ""


class BudgetItem(BaseModel):
    cpu_hours: float | None = None
    api_usd: float | None = None
    quote: str = ""
    cpu_hours_quote: str = ""  # verbatim excerpt stating cpu_hours (falls back to quote)
    api_usd_quote: str = ""    # verbatim excerpt stating api_usd (falls back to quote)
    status: ItemStatus = "missing"
    note: str = ""


class Autonomy(BaseModel):
    """Operational discretion of the autopilot; defaults are proposed by RA and approved with the scope."""

    max_cycles: int = 5
    max_new_tasks_per_cycle: int = 3
    max_retries_per_task: int = 2
    no_progress_cycles: int = 2
    allowed_actions: list[str] = Field(default_factory=lambda: [
        "additional experiments within the approved conditions", "auxiliary analyses", "retries",
        "reordering / restructuring of the task DAG"])
    status: ItemStatus = "ra_default"
    quote: str = ""


class OpenQuestion(BaseModel):
    field: str
    question: str
    blocking: bool                   # True: the scope cannot be approved before a human answers it


class ResearchScope(BaseModel):
    objective: ScopeItem = Field(default_factory=ScopeItem)
    primary_metric: ScopeItem = Field(default_factory=ScopeItem)
    hypotheses: list[ScopeItem] = Field(default_factory=list)
    targets: ScopeItem = Field(default_factory=ScopeItem)          # systems / data / objects under study
    conditions: list[ConditionBound] = Field(default_factory=list)
    invariants: list[ScopeItem] = Field(default_factory=list)
    baseline: ScopeItem = Field(default_factory=ScopeItem)         # baseline and fairness of comparison
    success_criteria: list[ScopeItem] = Field(default_factory=list)
    stop_conditions: list[ScopeItem] = Field(default_factory=list)
    budget: BudgetItem = Field(default_factory=BudgetItem)
    autonomy: Autonomy = Field(default_factory=Autonomy)
    open_questions: list[OpenQuestion] = Field(default_factory=list)

    def blocking(self) -> list[OpenQuestion]:
        return [q for q in self.open_questions if q.blocking]


# ---------------------------------------------------------------- evidence / plan


class EvidenceItem(BaseModel):
    evidence_id: str                 # E1, E2, ...
    kind: Literal["artifact", "literature", "implementation", "prior_result", "spec"]
    ref: str                         # catalog artifact id / report id:claim id / registry entry / run id
    title: str
    statement: str
    status: Literal["verified", "unverified", "unavailable"]
    locator: str = ""


class PlannedTask(BaseModel):
    key: str                          # T1, T2, ... (unique within a plan / cycle)
    title: str
    kind: Literal["implementation", "experiment", "analysis"]
    objective: str
    specification: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    parameters: dict[str, list[str]] = Field(default_factory=dict)  # condition name -> values used by this task
    produces: list[str] = Field(default_factory=list)
    consumes: list[str] = Field(default_factory=list)
    estimate_cpu_hours: float = 0.0
    estimate_api_usd: float = 0.0
    decision_point: bool = False      # results here feed the Evaluator's next decision
    evidence_ids: list[str] = Field(default_factory=list)
    repository: str | None = None
    file_scope: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- LLM-facing (strict JSON schema: no free dicts)


class TaskParameter(BaseModel):
    name: str = Field(description="condition name from SCOPE.conditions")
    values: list[str]


class PlannedTaskOut(BaseModel):
    key: str
    title: str
    kind: Literal["implementation", "experiment", "analysis"]
    objective: str
    specification: str
    acceptance_criteria: list[str]
    depends_on: list[str]
    parameters: list[TaskParameter]
    produces: list[str]
    consumes: list[str]
    estimate_cpu_hours: float
    estimate_api_usd: float
    decision_point: bool
    evidence_ids: list[str]
    repository: str | None
    file_scope: list[str]

    def to_task(self) -> PlannedTask:
        data = self.model_dump(exclude={"parameters"})
        params: dict[str, list[str]] = {}
        for p in self.parameters:
            params.setdefault(p.name, []).extend(p.values)
        return PlannedTask(**data, parameters=params)

    @classmethod
    def of(cls, t: PlannedTask) -> "PlannedTaskOut":
        return cls(**t.model_dump(exclude={"parameters"}),
                   parameters=[TaskParameter(name=k, values=v) for k, v in t.parameters.items()])


class PlanStatus(StrEnum):
    DRAFT = "draft"
    NEEDS_INPUT = "needs_input"       # blocking scientific questions; no approval can be requested
    INVALID = "invalid"               # the plan failed deterministic validation
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVISION_REQUESTED = "revision_requested"
    SUPERSEDED = "superseded"


class ResearchPlan(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    plan_id: str = Field(default_factory=lambda: "rplan_" + new_id()[:12])
    task_id: str                      # Linear identifier of the research objective
    notion_page_id: str | None = None
    version: int = 1
    kind: Literal["initial", "scope_change"] = "initial"
    created_at: datetime = Field(default_factory=utcnow)
    spec_hash: str                    # hash of the task's Notion text the plan was made from
    scope: ResearchScope
    evidence: list[EvidenceItem] = Field(default_factory=list)
    rationale: str = ""
    rationale_evidence: list[str] = Field(default_factory=list)
    tasks: list[PlannedTask] = Field(default_factory=list)
    replanning_rules: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    estimate_cpu_hours: float = 0.0
    estimate_api_usd: float = 0.0
    validation_problems: list[str] = Field(default_factory=list)
    status: PlanStatus = PlanStatus.DRAFT
    previous_plan_id: str | None = None
    revision_comment: str = ""
    change_summary: list[str] = Field(default_factory=list)   # scope_change: what differs from the approved scope
    approval_request_id: str | None = None
    plan_page_id: str | None = None   # Notion sub-page "RA Research Plan vN"
    plan_page_url: str | None = None
    models: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    content_hash: str = ""
    source_cycle_id: str | None = None   # scope_change: the cycle (and its evaluation number) that proposed it
    source_evaluation: int = 0

    def compute_hash(self) -> str:
        """What a human approves: the scope, the plan and the source text version (not ids / bookkeeping)."""
        return stable_hash(self.model_dump(mode="json", include={
            "task_id", "version", "kind", "spec_hash", "scope", "evidence", "rationale", "rationale_evidence", "tasks",
            "replanning_rules", "assumptions", "estimate_cpu_hours", "estimate_api_usd", "change_summary"}))


# ---------------------------------------------------------------- 10B cycles


class CycleStatus(StrEnum):
    RUNNING = "running"
    EVALUATING = "evaluating"
    COMPLETED = "completed"           # evaluated; next action decided
    FAILED = "failed"


class EvaluationDecision(BaseModel):
    action: Literal["continue", "complete", "stop", "scope_change"]
    rationale: str
    evidence: list[str] = Field(default_factory=list)          # result / artifact references used
    criteria: list[dict] = Field(default_factory=list)         # {"criterion", "status": met|not_met|insufficient, "evidence"}
    new_tasks: list[PlannedTask] = Field(default_factory=list)
    scope_change: list[str] = Field(default_factory=list)      # proposed changes beyond the approved scope
    progress: bool = True                                      # did this cycle add information?
    limitations: list[str] = Field(default_factory=list)
    proposed_conditions: list[ConditionBound] = Field(default_factory=list)  # scope_change: new / widened conditions
    proposed_budget: BudgetItem | None = None                                # scope_change: new budget limits
    conclusion: str = ""                                                     # complete / stop: what the results show
    gate_problems: list[str] = Field(default_factory=list)     # deterministic scope / budget gate findings
    # why a stop was decided (selects the human's resume steps): "" (evaluator) / "no_progress" / "failure"
    stop_kind: str = ""


class CriterionAssessment(BaseModel):
    criterion: str
    status: Literal["met", "not_met", "insufficient", "not_assessable"]
    evidence: list[str]


class EvaluationOut(BaseModel):
    """What the Evaluator LLM returns (strict schema); converted to ``EvaluationDecision``."""

    action: Literal["continue", "complete", "stop", "scope_change"]
    rationale: str
    evidence: list[str]
    criteria: list[CriterionAssessment]
    new_tasks: list[PlannedTaskOut]
    scope_change: list[str]
    progress: bool
    limitations: list[str]
    proposed_conditions: list[ConditionBound]
    proposed_budget: BudgetItem | None
    conclusion: str

    def to_decision(self) -> "EvaluationDecision":
        return EvaluationDecision(**self.model_dump(exclude={"criteria", "new_tasks", "proposed_conditions",
                                                             "proposed_budget"}),
                                  criteria=[c.model_dump() for c in self.criteria],
                                  new_tasks=[t.to_task() for t in self.new_tasks],
                                  proposed_conditions=self.proposed_conditions, proposed_budget=self.proposed_budget)


class ResearchCycle(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    cycle_id: str = Field(default_factory=lambda: "rcyc_" + new_id()[:12])
    task_id: str
    authorization_id: str
    authorization_version: int
    number: int
    tasks: list[PlannedTask]
    child_task_ids: dict[str, str] = Field(default_factory=dict)    # plan key -> Linear identifier
    child_page_ids: dict[str, str] = Field(default_factory=dict)    # plan key -> Notion page id
    parent_run_id: str | None = None
    status: CycleStatus = CycleStatus.RUNNING
    results: list[dict] = Field(default_factory=list)
    decision: EvaluationDecision | None = None
    next_plan_id: str | None = None   # scope_change plan proposed by this cycle's evaluation
    plan_id: str | None = None        # plan whose tasks this cycle runs
    budget_before: dict = Field(default_factory=dict)
    budget_after: dict = Field(default_factory=dict)
    cost_usd: float = 0.0
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    evaluations: int = 0              # how often this cycle was evaluated (re-evaluated after a resume / revision)


class ResearchControl(BaseModel):
    """A verified stop / resume / cancel request (Discord button of an authorized user, or CLI).

    A control acts on the research run that applied it (``run_id`` / ``execution_run_id`` are set then); a control
    created before that run started, or one that had nothing to act on (``ignored``), never pauses a later run.
    """

    control_id: str = Field(default_factory=new_id)
    task_id: str
    action: Literal["stop", "resume", "cancel"]
    actor: str
    channel: str
    at: datetime = Field(default_factory=utcnow)
    applied: bool = False
    run_id: str | None = None             # WorkflowRun the control was applied to
    execution_run_id: str | None = None   # its execution run at that time
    ignored: bool = False                 # applied without effect (e.g. Stop on an already stopped run)
