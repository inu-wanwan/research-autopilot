"""Fake connectors / agents and factories for Phase 2 unit tests."""

from collections.abc import Callable
from datetime import UTC, datetime

from research_autopilot.config import Settings
from research_autopilot.models import (
    AcceptanceMapping,
    DoneCriterion,
    ExecutionPlan,
    IssueRef,
    LinearIssue,
    NotionTask,
    PlanStep,
    ResourceEstimate,
    ReviewCheck,
    ReviewResult,
    RevisionRequest,
)
from research_autopilot.orchestrator.core import Deps
from research_autopilot.planning.agents import AgentRun, Usage
from research_autopilot.storage.db import Store
from research_autopilot.tasks.errors import NotFoundError
from research_autopilot.tasks.manager import TaskManager

PROJECT = "484c0d7b-f027-4a08-b433-6e9984b50436"
PAGE = "11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 10, 10, tzinfo=UTC)

FULL_SECTIONS = {
    "purpose": "Compare entropy-based active-space selection with the oracle.",
    "specification": "Run CASCI on the 9 geometries with the fixed protocol.",
    "acceptance_criteria": "- Energy error table for all geometries\n- Same Hamiltonian as reference",
    "scientific_invariants": "cc-pVDZ basis, frozen core.",
}


def make_issue(identifier: str = "INU-900", state: tuple[str, str] = ("Todo", "unstarted"),
               labels: tuple[str, ...] = ("autopilot",), blocked_by: tuple[IssueRef, ...] = (),
               notion_ids: tuple[str, ...] = (PAGE,), project: str | None = PROJECT,
               priority: int = 0, updated: str = "2026-10-10T00:00:00.000Z") -> LinearIssue:
    return LinearIssue(
        id=f"uuid-{identifier}", identifier=identifier, title=f"Task {identifier}",
        url=f"https://linear.app/x/issue/{identifier}", description="", priority=priority,
        state_id=f"state-{state[0]}", state_name=state[0], state_type=state[1], team_id="team-1",
        labels=list(labels), project_id=project, blocked_by=list(blocked_by), notion_page_ids=list(notion_ids),
        created_at="2026-10-01T00:00:00.000Z", updated_at=updated, fetched_at=NOW,
    )


def make_page(page_id: str = PAGE, linear_id: str | None = "INU-900", pending: bool = False,
              sections: dict[str, str] | None = None, edited: str = "2026-10-10T00:00:00.000Z") -> NotionTask:
    return NotionTask(
        page_id=page_id, url=f"https://www.notion.so/{page_id.replace('-', '')}", title="Task",
        status="To do", pending=pending, linear_issue_id=linear_id, category="実験",
        last_edited_time=edited, sections=dict(FULL_SECTIONS if sections is None else sections),
        other_sections={}, preamble="", fetched_at=NOW,
    )


class FakeNotion:
    def __init__(self, *pages: NotionTask) -> None:
        self.pages = {p.page_id: p for p in pages}
        self.get_calls = 0
        self.raise_on_get: Exception | None = None
        self.after_get: Callable[["FakeNotion"], None] | None = None

    def get_task(self, page_id: str) -> NotionTask:
        self.get_calls += 1
        if self.raise_on_get:
            raise self.raise_on_get
        if page_id not in self.pages:
            raise NotFoundError("notion", page_id)
        page = self.pages[page_id]
        if self.after_get:
            self.after_get(self)
        return page

    def find_page_ids_by_linear_id(self, identifier: str) -> list[str]:
        return [p.page_id for p in self.pages.values() if p.linear_issue_id == identifier]


class FakeLinear:
    STATES = {"Backlog": "backlog", "Todo": "unstarted", "In Progress": "started", "Done": "completed",
              "Canceled": "canceled"}

    def __init__(self, *issues: LinearIssue) -> None:
        self.issues = {i.identifier: i for i in issues}
        self.writes: list[tuple[str, str]] = []
        self.raise_on_get: Exception | None = None
        self.raise_on_set: Exception | None = None
        self.candidates: list[str] | None = None

    def get_issue(self, identifier: str) -> LinearIssue:
        if self.raise_on_get:
            raise self.raise_on_get
        if identifier not in self.issues:
            raise NotFoundError("linear", identifier)
        return self.issues[identifier].model_copy(deep=True)

    def candidate_identifiers(self, label: str, state_type: str) -> list[str]:
        if self.candidates is not None:
            return self.candidates
        found = [i for i in self.issues.values() if label in i.labels and i.state_type == state_type]
        found.sort(key=lambda i: (i.priority or 5, i.created_at, i.identifier))
        return [i.identifier for i in found]

    def team_state_id(self, team_id: str, name: str) -> tuple[str, str]:
        return f"state-{name}", self.STATES[name]

    def set_state(self, issue_id: str, state_id: str) -> str:
        if self.raise_on_set:
            raise self.raise_on_set
        name = state_id.removeprefix("state-")
        self.writes.append((issue_id, name))
        for i in self.issues.values():
            if i.id == issue_id:
                i.state_name, i.state_type, i.state_id = name, self.STATES[name], state_id
        return name


def make_plan(n_steps: int = 2) -> ExecutionPlan:
    ids = [f"S{i + 1}" for i in range(n_steps)]
    return ExecutionPlan(
        summary="plan",
        steps=[PlanStep(id=s, title=f"step {s}", description="do it", depends_on=ids[:k],
                        inputs=["geometries"], outputs=[f"out-{s}"], verification="check")
               for k, s in enumerate(ids)],
        execution_order=ids, environment=["python 3.12", "pyscf"], constraints=["cc-pVDZ"],
        verification_methods=["compare with reference"],
        acceptance_mapping=[AcceptanceMapping(criterion="Energy error table for all geometries",
                                              covered_by_steps=[ids[-1]], verification="table exists",
                                              done_criteria=["D1"])],
        definition_of_done=[DoneCriterion(id="D1", statement="The output has one energy error row per geometry.",
                                          source="phase4_contract", check="test",  # spec-agnostic fake
                                          how="test counts the rows")],
        resource_estimate=ResourceEstimate(compute="1 node", wall_time="2h", memory="16GB", notes=""),
        open_questions=[], approvals_needed=[],
    )


ASPECTS = ["spec_consistency", "scientific_validity", "feasibility", "reproducibility", "evaluation",
           "resources_cost", "research_risk"]


def review(verdict: str) -> ReviewResult:
    return ReviewResult(
        verdict=verdict, summary=verdict,
        checks=[ReviewCheck(aspect=a, assessment="ok", comment="") for a in ASPECTS],
        revision_requests=[RevisionRequest(aspect="evaluation", severity="major", issue="metric unclear",
                                           requested_change="define metric", step_ids=["S2"])]
        if verdict == "revision_required" else [],
        blocking_reasons=["spec contradicts invariants"] if verdict == "blocked" else [],
    )


class ScriptedAgent:
    """Returns / raises the scripted items in order; records its inputs."""

    def __init__(self, model: str, script: list) -> None:
        self.model = model
        self.script = list(script)
        self.calls: list[tuple] = []

    def _next(self, *args):
        self.calls.append(args)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return AgentRun(output=item, usage=Usage(input_tokens=1000, output_tokens=200, requests=1), model=self.model)

    def plan(self, spec, previous, feedback):
        return self._next(spec, previous, feedback)

    def review(self, spec, plan):
        return self._next(spec, plan)


def make_deps(tmp_path, notion: FakeNotion, linear: FakeLinear, planner_script: list, reviewer_script: list,
              **settings_kw) -> Deps:
    settings = Settings(linear_project_id=PROJECT, state_dir=tmp_path / "state",
                        database_url=f"sqlite:///{tmp_path / 'ra.db'}", **settings_kw)
    return Deps(
        manager=TaskManager(notion, linear, settings), linear=linear,
        planner=ScriptedAgent("gpt-5.6-terra", planner_script),
        reviewer=ScriptedAgent("gpt-5.6-sol", reviewer_script),
        store=Store(settings.resolved_database_url()), settings=settings,
    )
