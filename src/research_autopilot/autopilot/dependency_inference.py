"""LLM adapter for auditable DAG dependency proposals.

The model proposes only.  ``dag.apply_dependency_proposal`` validates task
identities/cycles/evidence and Discord approval is required before execution.
"""

from typing import Protocol

from research_autopilot.autopilot.dag import DependencyProposal
from research_autopilot.experiments.analyst import LlmRun, _PricedAgent


INSTRUCTIONS = """\
You are the dependency planner for Research Autopilot. Given one parent and its already-created child tasks,
propose the minimum directed acyclic graph needed to execute them. producer_task_id must finish before
consumer_task_id. Use scientific specifications, explicit dependency prose, input artifacts, artifact requirements,
acceptance criteria, and named interfaces as evidence. Do not infer an edge from A1/A2 numbering or title order alone.
Do not invent tasks. For each edge, list the concrete artifacts or interfaces the consumer needs and quote concise
evidence from the supplied fields. Each evidence item must be an exact substring copied verbatim from
the supplied fields (no labels, no paraphrase, no translation); explanations belong in rationale. Leave truly independent tasks unconnected. Put uncertainties in unresolved.
This is a proposal only; a human will approve the exact hashed DAG before execution.
"""


class DependencyInferer(Protocol):
    model: str

    def infer(self, payload: dict) -> LlmRun: ...


class OpenAIDependencyInferer(_PricedAgent):
    def infer(self, payload: dict) -> LlmRun:
        return self._call("DependencyPlanner", INSTRUCTIONS, payload, DependencyProposal)


__all__ = ["DependencyInferer", "OpenAIDependencyInferer"]
