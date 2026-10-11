"""Plan -> review -> (replan) loop with bounded retries.

Control flow is deterministic; the agents only produce plans and reviews.
Every agent call (successful or not) is appended to ``record.llm_calls``.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from research_autopilot.config import ReviewPolicy, Settings
from research_autopilot.models import (
    BlockedCode,
    BlockedReason,
    DecisionStatus,
    ExecutionPlan,
    LlmCall,
    PlanAttempt,
    PlanRunRecord,
    ReviewAttempt,
    RevisionRequest,
    RunEvent,
    TaskSpec,
    new_id,
    utcnow,
)
from research_autopilot.planning.agents import (
    AgentConfigError,
    AgentRun,
    InvalidAgentOutput,
    Planner,
    Reviewer,
    TransientAgentError,
)
from research_autopilot.planning import dod
from research_autopilot.planning.cost import estimate_cost



@dataclass
class LoopOutcome:
    status: DecisionStatus
    reasons: list[BlockedReason] = field(default_factory=list)
    plan: PlanAttempt | None = None
    review: ReviewAttempt | None = None


def run_planning(spec: TaskSpec, planner: Planner, reviewer: Reviewer, settings: Settings,
                 record: PlanRunRecord, sleep: Callable[[float], None] = time.sleep,
                 seed: tuple[ExecutionPlan, list[RevisionRequest]] | None = None) -> LoopOutcome:
    """``seed``: re-plan from an earlier plan with feedback (e.g. Phase 3 reported a Definition of Done item
    infeasible) instead of planning from scratch."""
    if settings.review_policy is not ReviewPolicy.ALWAYS:  # pragma: no cover - only ALWAYS exists in Phase 2
        raise NotImplementedError(settings.review_policy)

    previous: ExecutionPlan | None = seed[0] if seed else None
    feedback: list[RevisionRequest] = list(seed[1]) if seed else []
    last_review: ReviewAttempt | None = None

    for iteration in range(settings.max_replans + 1):
        try:
            plan_run = _call(record, settings, "planner", planner.model, iteration, sleep,
                             lambda: planner.plan(spec, previous, feedback))
        except _Stop as stop:
            return stop.outcome
        attempt = PlanAttempt(plan_id=new_id(), iteration=iteration, plan=plan_run.output)
        record.plans.append(attempt)

        # The Definition of Done must be checkable and grounded before a reviewer looks at the plan.
        if dod_problems := dod.problems(spec, attempt.plan):
            previous, feedback = attempt.plan, [RevisionRequest(
                aspect="evaluation", severity="major", issue=f"Definition of Done: {p}",
                requested_change="Fix the definition_of_done as described (see the planner rules).", step_ids=[])
                for p in dod_problems]
            record.events.append(RunEvent(kind="replan", message=(
                f"iteration {iteration}: definition of done rejected ({len(dod_problems)} problem(s))")))
            continue

        try:
            review_run = _call(record, settings, "reviewer", reviewer.model, iteration, sleep,
                               lambda: reviewer.review(spec, attempt.plan))
        except _Stop as stop:
            stop.outcome.plan = attempt
            return stop.outcome
        last_review = ReviewAttempt(plan_id=attempt.plan_id, iteration=iteration, review=review_run.output)
        record.reviews.append(last_review)
        verdict = review_run.output.verdict

        if verdict == "approved":
            return LoopOutcome(DecisionStatus.READY, plan=attempt, review=last_review)
        if verdict == "blocked":
            return LoopOutcome(DecisionStatus.BLOCKED, plan=attempt, review=last_review, reasons=[BlockedReason(
                code=BlockedCode.REVIEW_BLOCKED,
                message="Reviewer blocked the plan: " + "; ".join(review_run.output.blocking_reasons),
                details={"plan_id": attempt.plan_id, "iteration": iteration},
            )])
        previous, feedback = attempt.plan, review_run.output.revision_requests
        record.events.append(RunEvent(kind="replan", message=(
            f"iteration {iteration}: revision_required ({len(feedback)} requests)")))

    return LoopOutcome(DecisionStatus.BLOCKED, plan=attempt, review=last_review, reasons=[BlockedReason(
        code=BlockedCode.REVISION_LIMIT,
        message=f"plan not approved after {settings.max_replans} replans",
        details={"last_plan_id": attempt.plan_id,
                 "open_requests": [r.model_dump(mode="json") for r in feedback]},
    )])


class _Stop(Exception):
    def __init__(self, outcome: LoopOutcome) -> None:
        self.outcome = outcome


def _call(record: PlanRunRecord, settings: Settings, agent: str, model: str, iteration: int,
          sleep: Callable[[float], None], fn: Callable[[], AgentRun]) -> AgentRun:
    invalid_left = settings.llm_invalid_output_retries
    transient_left = settings.llm_transient_retries
    attempt = 0
    while True:
        attempt += 1
        started = utcnow()
        try:
            run = fn()
        except InvalidAgentOutput as exc:
            _log_call(record, settings, agent, model, iteration, attempt, started, error=f"invalid output: {exc}")
            if invalid_left > 0:
                invalid_left -= 1
                record.events.append(RunEvent(kind="retry", message=f"{agent}: invalid structured output; retrying"))
                continue
            raise _Stop(LoopOutcome(DecisionStatus.BLOCKED, reasons=[BlockedReason(
                code=BlockedCode.LLM_OUTPUT_INVALID,
                message=f"{agent} returned invalid structured output after retries",
                details={"agent": agent, "iteration": iteration})])) from exc
        except TransientAgentError as exc:
            _log_call(record, settings, agent, model, iteration, attempt, started, error=f"transient: {exc}")
            if transient_left > 0:
                transient_left -= 1
                record.events.append(RunEvent(kind="retry", message=f"{agent}: {exc}; retrying"))
                sleep(min(2 ** attempt, 30))
                continue
            raise _Stop(LoopOutcome(DecisionStatus.TRANSIENT_FAILURE, reasons=[BlockedReason(
                code=BlockedCode.TRANSIENT_ERROR,
                message=f"{agent}: OpenAI API temporarily unavailable ({exc})",
                details={"agent": agent})])) from exc
        except AgentConfigError as exc:
            _log_call(record, settings, agent, model, iteration, attempt, started, error=f"config: {exc}")
            raise _Stop(LoopOutcome(DecisionStatus.BLOCKED, reasons=[BlockedReason(
                code=BlockedCode.CONNECTOR_ERROR,
                message=f"{agent}: OpenAI API error that retrying will not fix ({exc})",
                details={"agent": agent, "service": "openai"})])) from exc
        _log_call(record, settings, agent, model, iteration, attempt, started, run=run)
        return run


def _log_call(record: PlanRunRecord, settings: Settings, agent: str, model: str, iteration: int,
              attempt: int, started, run: AgentRun | None = None, error: str | None = None) -> None:
    u = run.usage if run else None
    record.llm_calls.append(LlmCall(
        agent=agent, model=model, iteration=iteration, attempt=attempt,
        started_at=started, finished_at=utcnow(), ok=run is not None, error=error,
        input_tokens=u.input_tokens if u else 0, output_tokens=u.output_tokens if u else 0,
        cached_input_tokens=u.cached_input_tokens if u else 0,
        reasoning_tokens=u.reasoning_tokens if u else 0, requests=u.requests if u else 0,
        estimated_cost_usd=estimate_cost(u, settings.model_pricing.get(model)) if u else None,
    ))
