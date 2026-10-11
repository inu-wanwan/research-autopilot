"""Research Planner (10A): scope -> evidence -> grounded plan with a Task DAG draft.

The LLM drafts; a deterministic gate (shared with the 10B Evaluator) decides whether tasks stay inside the
approved scope: DAG shape, parameter values within the approved conditions, sample counts, budget, repositories.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from research_autopilot.research import scope as scope_mod
from research_autopilot.research.models import (EvidenceItem, OpenQuestion, PlannedTask, PlannedTaskOut, PlanStatus,
                                                ResearchPlan, ResearchScope)

MAX_INITIAL_TASKS = 8
KEY = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


class PlanDraft(BaseModel):
    rationale: str = Field(description="why this plan answers the objective; cite evidence ids like [E3]")
    rationale_evidence: list[str] = Field(default_factory=list)
    tasks: list[PlannedTaskOut]
    replanning_rules: list[str] = Field(description="how results will decide the next cycle (e.g. adaptive sampling rule)")
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)


PLAN_INSTRUCTIONS = f"""\
You are the Research Planner of Research Autopilot. Draft an executable research plan for the approved-to-be
SCOPE: implementation, experiment and analysis tasks forming a DAG (depends_on), small enough to run on the
given research repository. Rules:
- Stay strictly inside SCOPE: never change the objective, primary metric, targets, invariants or baseline; put every
  varied quantity in `parameters` using the names of range / values conditions with explicit values inside the
  approved ranges; max_count conditions are limits (never parameters): respect them over the whole research.
- The plan is the FIRST cycle only: the smallest informative step with concrete parameter values. Tasks whose
  parameters depend on results (adaptive sampling, final aggregation after later cycles) must NOT be in the plan:
  describe them in replanning_rules; the Research Evaluator creates them in later cycles. Mark tasks whose results
  drive the next decision with decision_point=true.
- Every task (implementation, experiment and analysis alike) runs through the implementer and the experiment runner
  in a registered repository (REPOSITORIES) with a minimal file_scope (glob patterns); estimate CPU hours and API
  cost honestly within BUDGET; produces/consumes name artifacts.
- Cite EVIDENCE ids in rationale and evidence_ids; mark assumptions explicitly; add open_questions instead of
  deciding scientific matters the scope leaves open.
At most {MAX_INITIAL_TASKS} tasks. {scope_mod.UNTRUSTED}
"""


def draft_plan(llm, scope: ResearchScope, evidence: list[EvidenceItem], repositories: dict[str, str],
               feedback: list[str] | None = None, revision_comment: str = "") -> PlanDraft:
    payload = {"SCOPE": scope.model_dump(mode="json"),
               "EVIDENCE": [e.model_dump(mode="json") for e in evidence],
               "REPOSITORIES": repositories,
               "BUDGET": scope.budget.model_dump(mode="json")}
    if feedback:
        payload["VALIDATION_PROBLEMS_OF_PREVIOUS_DRAFT"] = feedback
    if revision_comment:
        payload["HUMAN_REVISION_REQUEST"] = revision_comment
    return llm("ResearchPlanner", PLAN_INSTRUCTIONS, payload, PlanDraft)


# ---------------------------------------------------------------- deterministic gate (10A and 10B)


def strip_count_parameters(t: PlannedTask, scope: ResearchScope) -> PlannedTask:
    """A max_count condition is a limit, not a value a task can set: drop it from task parameters."""
    counts = {c.name for c in scope.conditions if c.kind == "max_count"}
    if not counts & set(t.parameters):
        return t
    return t.model_copy(update={"parameters": {k: v for k, v in t.parameters.items() if k not in counts}})


def _float(v: str) -> float | None:
    try:
        return float(str(v).strip().split()[0])
    except (ValueError, IndexError):
        return None


def check_tasks(tasks: list[PlannedTask], scope: ResearchScope, *, registry: dict, evidence_ids: set[str],
                used_values: dict[str, set[str]] | None = None, budget_used_cpu: float = 0.0,
                budget_used_api: float = 0.0, max_tasks: int = MAX_INITIAL_TASKS) -> list[str]:
    p: list[str] = []
    if not tasks:
        return ["the plan has no tasks"]
    if len(tasks) > max_tasks:
        p.append(f"{len(tasks)} tasks exceed the limit of {max_tasks}")
    keys = [t.key for t in tasks]
    if len(set(keys)) != len(keys) or not all(KEY.match(k) for k in keys):
        p.append("task keys must be unique short identifiers")
    known = set(keys)
    for t in tasks:
        for d in t.depends_on:
            if d not in known:
                p.append(f"{t.key}: depends on unknown task {d}")
        if True:  # every task is implemented / run in a registered research repository
            if not t.repository or t.repository not in registry:
                p.append(f"{t.key}: repository {t.repository!r} is not a registered research repository")
            if not t.file_scope:
                p.append(f"{t.key}: file_scope is required")
        for e in t.evidence_ids:
            if e not in evidence_ids:
                p.append(f"{t.key}: unknown evidence id {e}")
    if _cyclic(tasks):
        p.append("depends_on contains a cycle")
    conds = {c.name: c for c in scope.conditions if c.kind != "max_count"}
    counts = [c for c in scope.conditions if c.kind == "max_count"]
    used = {k: set(v) for k, v in (used_values or {}).items()}
    for t in tasks:
        for name, values in t.parameters.items():
            c = conds.get(name)
            if c is None:
                p.append(f"{t.key}: parameter {name!r} is not an approved condition")
                continue
            if c.status != "stated":
                p.append(f"{t.key}: condition {name} is not established in the spec ({c.status})")
            for v in values:
                if c.kind == "range":
                    x = _float(v)
                    if x is None or (c.minimum is not None and x < c.minimum - 1e-12) or \
                            (c.maximum is not None and x > c.maximum + 1e-12):
                        p.append(f"{t.key}: {name}={v} outside the approved range [{c.minimum}, {c.maximum}] {c.unit or ''}")
                elif c.values and str(v).strip().lower() not in {x.strip().lower() for x in c.values}:
                    p.append(f"{t.key}: {name}={v} not among the approved values {c.values}")
            used.setdefault(name, set()).update(_canon(v) for v in values)
    for c in counts:
        n = len(used.get(c.applies_to or "", set()))
        if c.count is not None and n > c.count:
            p.append(f"{n} distinct {c.applies_to} values exceed the approved maximum of {c.count}")
    cpu = budget_used_cpu + sum(t.estimate_cpu_hours for t in tasks)
    api = budget_used_api + sum(t.estimate_api_usd for t in tasks)
    if scope.budget.cpu_hours is not None and cpu > scope.budget.cpu_hours + 1e-9:
        p.append(f"estimated {cpu:.3f} CPU-h exceeds the approved {scope.budget.cpu_hours:g} CPU-h")
    if scope.budget.api_usd is not None and api > scope.budget.api_usd + 1e-9:
        p.append(f"estimated ${api:.2f} exceeds the approved ${scope.budget.api_usd:g}")
    return p


def _canon(v) -> str:
    x = _float(v)
    return f"{x:g}" if x is not None else str(v).strip().lower()


def _cyclic(tasks: list[PlannedTask]) -> bool:
    deps = {t.key: set(t.depends_on) for t in tasks}
    seen, stack = set(), set()

    def visit(k: str) -> bool:
        if k in stack:
            return True
        if k in seen:
            return False
        seen.add(k)
        stack.add(k)
        hit = any(visit(d) for d in deps.get(k, ()) if d in deps)
        stack.discard(k)
        return hit
    return any(visit(k) for k in deps)


def build_plan(llm, spec, *, task_id: str, notion_page_id: str | None, evidence: list[EvidenceItem], registry: dict,
               version: int, previous: ResearchPlan | None = None, revision_comment: str = "",
               max_attempts: int = 2, default_budget=None) -> ResearchPlan:
    """Scope extraction + verification, then a plan draft revised once on deterministic problems."""
    spent0 = getattr(llm, "spent", 0.0)
    raw = scope_mod.extract_scope(llm, spec)
    scope = scope_mod.verify(raw, spec)
    if default_budget is not None and scope.budget.status == "stated":
        # A limit the spec leaves open is filled with the RA default and shown as such in the approval request.
        notes = []
        if scope.budget.api_usd is None:
            scope.budget.api_usd = float(default_budget.api_usd)
            notes.append(f"API limit not stated: RA default ${default_budget.api_usd:g} (approved with the scope)")
        if scope.budget.cpu_hours is None:
            scope.budget.cpu_hours = float(default_budget.compute_cpu_hours)
            notes.append(f"CPU limit not stated: RA default {default_budget.compute_cpu_hours:g} CPU-h")
        if notes:
            scope.budget.note = "; ".join([scope.budget.note, *notes]).strip("; ")
    plan = ResearchPlan(task_id=task_id, notion_page_id=notion_page_id, version=version,
                        spec_hash=scope_mod.spec_hash(spec), scope=scope, evidence=evidence,
                        previous_plan_id=previous.plan_id if previous else None, revision_comment=revision_comment,
                        models=[getattr(llm, "model", "")])
    if scope.blocking():
        plan.status = PlanStatus.NEEDS_INPUT
        plan.cost_usd = round(getattr(llm, "spent", 0.0) - spent0, 6)
        plan.content_hash = plan.compute_hash()
        return plan
    repos = {rid: f"{getattr(cfg, 'path', '')}".rsplit("/", 1)[-1] for rid, cfg in registry.items()}
    feedback: list[str] = []
    for _ in range(max_attempts):
        d = draft_plan(llm, scope, evidence, repos, feedback, revision_comment)
        tasks = [strip_count_parameters(t.to_task(), scope) for t in d.tasks]
        problems = check_tasks(tasks, scope, registry=registry, evidence_ids={e.evidence_id for e in evidence})
        problems += [f"rationale cites unknown evidence {e}" for e in d.rationale_evidence
                     if e not in {x.evidence_id for x in evidence}]
        plan.rationale, plan.rationale_evidence, plan.tasks = d.rationale, d.rationale_evidence, tasks
        plan.replanning_rules, plan.assumptions = d.replanning_rules, d.assumptions
        plan.scope.open_questions += [q.model_copy(update={"blocking": False}) for q in d.open_questions
                                      if q not in plan.scope.open_questions]
        if not problems:
            break
        feedback = problems
    plan.validation_problems = problems
    plan.estimate_cpu_hours = round(sum(t.estimate_cpu_hours for t in plan.tasks), 4)
    plan.estimate_api_usd = round(sum(t.estimate_api_usd for t in plan.tasks), 4)
    if plan.scope.blocking():
        plan.status = PlanStatus.NEEDS_INPUT
    elif problems:
        plan.status = PlanStatus.INVALID
    else:
        plan.status = PlanStatus.DRAFT
    plan.cost_usd = round(getattr(llm, "spent", 0.0) - spent0, 6)
    plan.content_hash = plan.compute_hash()
    return plan
