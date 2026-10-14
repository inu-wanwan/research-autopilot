"""Planner and Reviewer agents (OpenAI Agents SDK).

Both return Pydantic-validated outputs. Errors are normalized to:

- InvalidAgentOutput: the model's output failed schema / consistency validation.
- TransientAgentError: rate limit, timeout, connection or 5xx (safe to retry).
- AgentConfigError: auth, quota, unknown model -- retrying will not help.

With ``RA_LLM_SUBSCRIPTION_FIRST=codex`` every call first tries the ChatGPT/Codex subscription
(``llm_subscription``) and uses the API path below only when that is impossible; the errors above describe the
API path (a failed subscription attempt never raises, it falls back).
"""

import json
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

import openai
from agents import Agent, ModelBehaviorError, RunConfig, Runner
from pydantic import BaseModel, ValidationError

from research_autopilot.models import APPROVALS_ARE_RA, ExecutionPlan, ReviewResult, RevisionRequest, TaskSpec

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
    billing: str = "api"  # "subscription" = ChatGPT/Codex plan (0 USD for budgets), "api" = pay-as-you-go


class Planner(Protocol):
    model: str

    def plan(self, spec: TaskSpec, previous: ExecutionPlan | None,
             feedback: list[RevisionRequest]) -> AgentRun[ExecutionPlan]: ...


class Reviewer(Protocol):
    model: str

    def review(self, spec: TaskSpec, plan: ExecutionPlan,
               previous: list[RevisionRequest] | None = None) -> AgentRun[ReviewResult]: ...


PLANNER_INSTRUCTIONS = """\
You are the Planner of Research Autopilot, an assistant that prepares research tasks
(machine learning / quantum chemistry, e.g. active-space selection) for later execution.

Produce an execution plan for the given task specification. This is planning only:
do not claim that anything has been run and do not invent results or data.

Rules:
- The task's purpose, scientific invariants and acceptance criteria are fixed by the
  researcher. Never change, weaken or reinterpret them. Every acceptance criterion must
  appear in acceptance_mapping, quoted verbatim, with the steps that satisfy it.
- A Definition of Done item may refer to a contract your early discovery step fixes (e.g. "the convergence
  status reported by the chosen PySCF solver is true at every point; the criterion used is recorded"); state
  the pass/fail rule, not an unresolved choice.
- Steps must be concrete enough for an implementer: inputs, outputs, and how each
  result is verified. depends_on and execution_order must be consistent.
- List required environment / libraries, constraints (including scientific
  invariants), verification methods and a rough compute estimate.
- Anything ambiguous goes to open_questions; any decision that needs the researcher
  (changing scope, metrics, data, compute beyond the estimate) goes to approvals_needed.
- Do not escalate ordinary implementation discovery to the researcher. Inspecting the
  repository or an installed library API, selecting a compatible API exposed by the
  already-approved dependency, and recording the exact versions/settings used are
  bounded technical steps. Plan those as early implementation steps with verification.
  Ask the researcher only when the available implementations require a real change to
  the scientific specification, invariant, acceptance criterion, or approved budget.
- Definition of Done (definition_of_done): the pass/fail contract the implementation is accepted against. The
  implementer builds to exactly these items and the code reviewer may send work back only for them (plus a
  fixed system baseline B1-B5: tests pass, experiment.json valid, output units declared, runtime dependencies
  declared, file scope respected -- do not repeat those). Rules:
  * ids D1, D2, ...; at most 10 items; every acceptance criterion lists the items that prove it in
    acceptance_mapping.done_criteria.
  * each statement is checkable: name the exact output fields, settings, behaviours and tolerances (e.g.
    "each result records rhf.conv_tol, rhf.max_cycle, fci.conv_tol, fci.max_cycle and the convergence flags"),
    never "appropriate", "reproducible", "complete", or "A or B" choices left open -- decide them.
  * each item's source quotes the task specification verbatim (or is 'phase4_contract'): copy contiguous
    passages from any section (markdown, quote marks and list markers are ignored); to cite several places,
    separate the passages with a blank line, each passage copied exactly; do not add
    requirements the specification does not ask for (no extra provenance, schemas or tooling "for safety").
  * prefer check="test" (a repository test asserts it); use "review" only for what a test cannot show.
  * only use libraries that ENVIRONMENT FACTS list as available in the test environment, or that the plan
    declares as a runtime dependency (Phase 4 installs the declared dependency files; see the contract there).
- ENVIRONMENT FACTS (if present) are measured facts about the repository, its test environment and how
  Phase 4 builds the run environment. Plan within them.
- If reviewer revision requests are provided, address each of them explicitly while
  still obeying the rules above. A request may report that a Definition of Done item is infeasible: fix the
  item if the evidence holds; keep it unchanged only if the evidence is wrong.
- LITERATURE EVIDENCE (if present in the input) comes from an automated literature review: claims marked
  VERIFIED were found verbatim in the cited source; UNVERIFIED ones were not and must not be treated as facts.
  Use the evidence to ground methods and conditions, cite source ids, and never change the fixed scientific
  specification because of it (put proposed changes into approvals_needed).
- If a question only the literature can answer would materially change the plan, put it into
  literature_requests (concise, answerable questions); otherwise leave it empty.
- Write in the same language as the task specification.
""" + "- " + APPROVALS_ARE_RA + "\n"

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

Technical details that an implementer can determine by inspecting the repository,
installed dependency, or official API are not missing scientific specifications. A
plan may deliberately resolve them in an early, bounded discovery step, then record
the selected version/API/settings in provenance and test the resulting contract. Do
not require those facts to be known before execution unless choosing among them would
change a researcher-approved invariant, metric, acceptance criterion, or budget.

Definition of Done (judge it under spec_consistency and evaluation): every item must be pass/fail checkable
(exact fields, settings, behaviours, tolerances; no open "A or B" choices), grounded in the quoted
specification, and proportionate -- request removal of items the specification does not require (extra
provenance, schemas, tooling) as well as additions where an acceptance criterion is not proven. The code
reviewer will judge the implementation only against these items, so this is where the acceptance bar is set.

A Definition of Done item may be stated against a contract that the plan's early, bounded discovery step fixes
(e.g. "every point reports the convergence status of the chosen PySCF solver as true, and the criterion used is
recorded in the output") -- that is pass/fail checkable; do not require the plan to pick the API or exact
schema layout up front. Likewise, when the specification explicitly leaves a detail to the implementation
(e.g. "decided at implementation time", "実装時に決める"), do not demand that the plan fix it: require only that
the implementation records the values it chose (in its outputs / provenance); anything more is minor.
Approve when only minor issues remain.

Later iterations (PREVIOUS REVISION REQUESTS given): check whether each of your previous requests was
addressed. Raise a new major or blocking request only if, without it, an acceptance criterion or a scientific
invariant could not be proven; everything else is minor. Do not reverse your earlier requests.

Be specific and conservative: if the plan alters the task's purpose, invariants or
acceptance criteria, that is at least a blocking revision request. Write in the same
language as the task specification.
"""


def _spec_payload(spec: TaskSpec) -> dict:
    return spec.model_dump(mode="json", exclude={"sources"})


class _AgentsSdkBase:
    reasoning_effort: str | None = None  # subscription path; None = llm_subscription.DEFAULT_EFFORT

    def __init__(self, model: str, tracing: bool = False) -> None:
        self.model = model
        self._run_config = RunConfig(tracing_disabled=not tracing, workflow_name="research-autopilot")

    def _run(self, agent: Agent, prompt: str, output_type: type[T],
             api_refusal: Exception | None = None) -> AgentRun[T]:
        """Subscription first (when enabled), then the API. ``api_refusal`` is raised instead of an API call
        (e.g. an unpriced model) when the subscription could not serve the call."""
        from research_autopilot import llm_subscription

        sub = llm_subscription.try_structured(self.model, str(agent.instructions or ""), prompt, output_type,
                                              reasoning_effort=self.reasoning_effort)
        if sub is not None:
            out, u = sub
            usage = Usage(input_tokens=u.get("input_tokens", 0), output_tokens=u.get("output_tokens", 0),
                          cached_input_tokens=u.get("cached_input_tokens", 0),
                          reasoning_tokens=u.get("reasoning_tokens", 0), requests=1)
            return AgentRun(output=out, usage=usage, model=self.model, billing="subscription")
        if api_refusal is not None:
            raise api_refusal
        return self._run_api(agent, prompt, output_type)

    def _run_api(self, agent: Agent, prompt: str, output_type: type[T]) -> AgentRun[T]:
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
    # (spec) -> facts about the repository / test environment / Phase 4 environment contract, or None
    environment_facts = None

    def plan(self, spec: TaskSpec, previous: ExecutionPlan | None,
             feedback: list[RevisionRequest]) -> AgentRun[ExecutionPlan]:
        agent = Agent(name="Planner", instructions=PLANNER_INSTRUCTIONS, model=self.model,
                      output_type=ExecutionPlan)
        payload: dict = {"task_specification": _spec_payload(spec)}
        if self.environment_facts is not None:
            try:
                facts = self.environment_facts(spec)
            except Exception:  # facts help the plan; their absence never blocks it
                facts = None
            if facts:
                payload["environment_facts"] = facts
        if previous is not None:
            payload["previous_plan"] = previous.model_dump(mode="json")
            payload["reviewer_revision_requests"] = [f.model_dump(mode="json") for f in feedback]
        return self._run(agent, json.dumps(payload, ensure_ascii=False), ExecutionPlan)


class OpenAIReviewer(_AgentsSdkBase):
    def review(self, spec: TaskSpec, plan: ExecutionPlan,
               previous: list[RevisionRequest] | None = None) -> AgentRun[ReviewResult]:
        agent = Agent(name="Reviewer", instructions=REVIEWER_INSTRUCTIONS, model=self.model,
                      output_type=ReviewResult)
        payload = {"task_specification": _spec_payload(spec), "plan": plan.model_dump(mode="json")}
        if previous:
            payload["previous_revision_requests"] = [r.model_dump(mode="json") for r in previous]
        return self._run(agent, json.dumps(payload, ensure_ascii=False), ReviewResult)
