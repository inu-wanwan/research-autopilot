"""Planner and Reviewer agents (OpenAI Agents SDK).

Both return Pydantic-validated outputs. Errors are normalized to:

- InvalidAgentOutput: the model's output failed schema / consistency validation.
- TransientAgentError: rate limit, timeout, connection or 5xx (safe to retry).
- AgentConfigError: auth, quota, unknown model -- retrying will not help.
"""

import json
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

import openai
from agents import Agent, ModelBehaviorError, RunConfig, Runner
from pydantic import BaseModel, ValidationError

from research_autopilot.models import ExecutionPlan, ReviewResult, RevisionRequest, TaskSpec

T = TypeVar("T", bound=BaseModel)


class AgentError(Exception):
    pass


class InvalidAgentOutput(AgentError):
    pass


class TransientAgentError(AgentError):
    pass


class AgentConfigError(AgentError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    requests: int = 0


@dataclass
class AgentRun(Generic[T]):
    output: T
    usage: Usage
    model: str


class Planner(Protocol):
    model: str

    def plan(self, spec: TaskSpec, previous: ExecutionPlan | None,
             feedback: list[RevisionRequest]) -> AgentRun[ExecutionPlan]: ...


class Reviewer(Protocol):
    model: str

    def review(self, spec: TaskSpec, plan: ExecutionPlan) -> AgentRun[ReviewResult]: ...


PLANNER_INSTRUCTIONS = """\
You are the Planner of Research Autopilot, an assistant that prepares research tasks
(machine learning / quantum chemistry, e.g. active-space selection) for later execution.

Produce an execution plan for the given task specification. This is planning only:
do not claim that anything has been run and do not invent results or data.

Rules:
- The task's purpose, scientific invariants and acceptance criteria are fixed by the
  researcher. Never change, weaken or reinterpret them. Every acceptance criterion must
  appear in acceptance_mapping, quoted verbatim, with the steps that satisfy it.
- Steps must be concrete enough for an implementer: inputs, outputs, and how each
  result is verified. depends_on and execution_order must be consistent.
- List required environment / libraries, constraints (including scientific
  invariants), verification methods and a rough compute estimate.
- Anything ambiguous goes to open_questions; any decision that needs the researcher
  (changing scope, metrics, data, compute beyond the estimate) goes to approvals_needed.
- If reviewer revision requests are provided, address each of them explicitly while
  still obeying the rules above.
- LITERATURE EVIDENCE (if present in the input) comes from an automated literature review: claims marked
  VERIFIED were found verbatim in the cited source; UNVERIFIED ones were not and must not be treated as facts.
  Use the evidence to ground methods and conditions, cite source ids, and never change the fixed scientific
  specification because of it (put proposed changes into approvals_needed).
- If a question only the literature can answer would materially change the plan, put it into
  literature_requests (concise, answerable questions); otherwise leave it empty.
- Write in the same language as the task specification.
"""

REVIEWER_INSTRUCTIONS = """\
You are the independent Reviewer of Research Autopilot. You check a Planner's execution
plan against the original research task specification. You do not rewrite the plan.

Evaluate exactly these aspects, one check each:
spec_consistency (purpose, invariants and acceptance criteria preserved and covered),
scientific_validity (scientific / mathematical soundness), feasibility,
reproducibility, evaluation (metrics and verification methods), resources_cost,
research_risk (data leakage, unfair comparisons, invalid baselines, etc.).

Verdict:
- approved: no blocking or major problems remain (minor suggestions allowed).
- revision_required: the plan can be fixed by the Planner; give structured
  revision_requests (aspect, severity, issue, requested_change, step_ids).
- blocked: the task cannot proceed without the researcher (e.g. the specification is
  contradictory or infeasible as written); give blocking_reasons.

Be specific and conservative: if the plan alters the task's purpose, invariants or
acceptance criteria, that is at least a blocking revision request. Write in the same
language as the task specification.
"""


def _spec_payload(spec: TaskSpec) -> dict:
    return spec.model_dump(mode="json", exclude={"sources"})


class _AgentsSdkBase:
    def __init__(self, model: str, tracing: bool = False) -> None:
        self.model = model
        self._run_config = RunConfig(tracing_disabled=not tracing, workflow_name="research-autopilot")

    def _run(self, agent: Agent, prompt: str, output_type: type[T]) -> AgentRun[T]:
        try:
            result = Runner.run_sync(agent, prompt, run_config=self._run_config, max_turns=1)
        except ModelBehaviorError as exc:
            raise InvalidAgentOutput(str(exc)[:500]) from exc
        except (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError) as exc:
            raise TransientAgentError(type(exc).__name__) from exc
        except openai.RateLimitError as exc:
            code = getattr(exc, "code", None) or ""
            if code == "insufficient_quota":
                raise AgentConfigError("insufficient_quota (check API billing)") from exc
            raise TransientAgentError(f"RateLimitError {code}".strip()) from exc
        except (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError,
                openai.BadRequestError) as exc:
            raise AgentConfigError(f"{type(exc).__name__} {getattr(exc, 'code', '') or ''}".strip()) from exc

        out = result.final_output
        if not isinstance(out, output_type):
            try:
                out = output_type.model_validate(out)
            except ValidationError as exc:
                raise InvalidAgentOutput(str(exc)[:500]) from exc
        u = result.context_wrapper.usage
        usage = Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cached_input_tokens=getattr(u.input_tokens_details, "cached_tokens", 0) or 0,
            reasoning_tokens=getattr(u.output_tokens_details, "reasoning_tokens", 0) or 0,
            requests=u.requests,
        )
        return AgentRun(output=out, usage=usage, model=self.model)


class OpenAIPlanner(_AgentsSdkBase):
    def plan(self, spec: TaskSpec, previous: ExecutionPlan | None,
             feedback: list[RevisionRequest]) -> AgentRun[ExecutionPlan]:
        agent = Agent(name="Planner", instructions=PLANNER_INSTRUCTIONS, model=self.model,
                      output_type=ExecutionPlan)
        payload: dict = {"task_specification": _spec_payload(spec)}
        if previous is not None:
            payload["previous_plan"] = previous.model_dump(mode="json")
            payload["reviewer_revision_requests"] = [f.model_dump(mode="json") for f in feedback]
        return self._run(agent, json.dumps(payload, ensure_ascii=False), ExecutionPlan)


class OpenAIReviewer(_AgentsSdkBase):
    def review(self, spec: TaskSpec, plan: ExecutionPlan) -> AgentRun[ReviewResult]:
        agent = Agent(name="Reviewer", instructions=REVIEWER_INSTRUCTIONS, model=self.model,
                      output_type=ReviewResult)
        payload = {"task_specification": _spec_payload(spec), "plan": plan.model_dump(mode="json")}
        return self._run(agent, json.dumps(payload, ensure_ascii=False), ReviewResult)
