"""LLM Scientific Analyst and independent Scientific Reviewer (OpenAI Agents SDK).

Paid API: a call is made only if the model's price is configured (RA_MODEL_PRICING)
and the run's LLM budget allows it; otherwise ``LlmBudgetError`` (-> analysis blocked).
With RA_LLM_SUBSCRIPTION_FIRST=codex the call is tried on the subscription first (0 USD).
"""

import json
from dataclasses import dataclass
from typing import Protocol

from research_autopilot.config import ModelPrice
from research_autopilot.experiments.models import AnalysisResult, ExperimentProposal, ScientificReviewResult
from research_autopilot.llm_subscription import subscription_mode
from research_autopilot.planning.agents import AgentConfigError, InvalidAgentOutput, TransientAgentError, Usage, _AgentsSdkBase
from research_autopilot.planning.cost import estimate_cost


class LlmBudgetError(Exception):
    pass


@dataclass
class LlmRun:
    output: object
    usage: Usage
    model: str
    cost_usd: float | None
    billing: str = "api"  # "subscription" calls cost 0 USD (known), "api" calls keep their priced cost


class Analyst(Protocol):
    model: str

    def analyze(self, payload: dict) -> LlmRun: ...


class ScientificReviewer(Protocol):
    model: str

    def review(self, payload: dict) -> LlmRun: ...


ANALYST_INSTRUCTIONS = """\
You are the Scientific Analyst of Research Autopilot (machine learning / quantum chemistry research).
You receive deterministically validated results of an approved experiment together with the original task
specification, plan, experiment spec and manifest.

- Separate strictly: observed_facts (directly supported by validated metrics / artifacts; cite the metric
  names, artifact paths, run ids or commit in `evidence`), inferences (reasoning from facts), hypotheses
  (untested ideas). Never present an inference or hypothesis as a fact.
- If validation status is not "pass", do not draw scientific conclusions; explain what failed instead.
- Items marked not_verified (prose acceptance criteria / invariants) are not established by validation;
  you may comment, but you cannot declare them satisfied.
- Report uncertainties, anomalies, limitations and open questions honestly.
- Propose at most the requested number of next experiments. Each proposal must keep the original research
  purpose, scientific invariants (copy them verbatim) and acceptance criteria (never relax them) unless it
  explicitly declares the change in parameter_changes with the right kind (new_target / invariant /
  acceptance / objective / method / within_spec). Prefer proposals that add information within the original
  scope (e.g. additional validation, replicates, sensitivity within the approved setting).
- Write in the same language as the task specification.
"""

REVIEWER_INSTRUCTIONS = """\
You are the independent Scientific Reviewer of Research Autopilot. You did not write the proposal.
Assess one proposed follow-up experiment against the original task (purpose, hypothesis, scientific invariants,
acceptance criteria), the validated results and the list of already existing proposals / experiments.

Judge: scientific validity, usefulness, contribution to the original purpose, duplication with existing work,
feasibility, budget / compute, whether it stays within the original research scope, whether scientific invariants
are preserved and acceptance criteria are not relaxed, and whether it needs a new scientific judgement by a human.
Verdict: accept / revise (give required_changes) / reject / blocked (cannot be judged without a human).
You do not approve execution; a separate deterministic policy decides. Respond in the language of the task.
"""


class _PricedAgent(_AgentsSdkBase):
    def __init__(self, model: str, prices: dict[str, ModelPrice], remaining_budget, tracing: bool = False) -> None:
        super().__init__(model, tracing)
        self.prices = prices
        self.remaining_budget = remaining_budget  # callable -> float

    def _call(self, name: str, instructions: str, payload: dict, out_type) -> LlmRun:
        from agents import Agent

        price = self.prices.get(self.model)
        if self.remaining_budget() <= 0:
            raise LlmBudgetError("LLM budget for this run is exhausted")
        # an unpriced model may still run on the subscription; only the paid API call is refused
        refusal = (LlmBudgetError(f"no price configured for {self.model} (RA_MODEL_PRICING); paid call refused")
                   if price is None else None)
        if refusal is not None and subscription_mode() is None:
            raise refusal
        agent = Agent(name=name, instructions=instructions, model=self.model, output_type=out_type)
        run = self._run(agent, json.dumps(payload, ensure_ascii=False, default=str), out_type, api_refusal=refusal)
        if run.billing == "subscription":
            return LlmRun(output=run.output, usage=run.usage, model=self.model, cost_usd=0.0, billing="subscription")
        return LlmRun(output=run.output, usage=run.usage, model=self.model, cost_usd=estimate_cost(run.usage, price))


class OpenAIAnalyst(_PricedAgent):
    def analyze(self, payload: dict) -> LlmRun:
        return self._call("ScientificAnalyst", ANALYST_INSTRUCTIONS, payload, AnalysisResult)


class OpenAIScientificReviewer(_PricedAgent):
    def review(self, payload: dict) -> LlmRun:
        return self._call("ScientificReviewer", REVIEWER_INSTRUCTIONS, payload, ScientificReviewResult)


__all__ = ["Analyst", "ScientificReviewer", "OpenAIAnalyst", "OpenAIScientificReviewer", "LlmRun", "LlmBudgetError",
           "AgentConfigError", "InvalidAgentOutput", "TransientAgentError", "ExperimentProposal"]
