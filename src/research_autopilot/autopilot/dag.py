"""Validated, persisted parent-task DAGs for Phase 6B.

Discovery is deliberately deterministic.  Notion defines membership, Linear
``blocked-by`` defines executable edges, and prose is never silently promoted
to an edge.  Invalid or ambiguous input is retained as a blocked DAG so an
operator can repair the source records without losing the audit trail.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from research_autopilot.autopilot.execution import ExecutionStatus
from research_autopilot.models import BlockedCode, TaskSpec, new_id, utcnow


class DAGStatus(StrEnum):
    VALID = "valid"
    BLOCKED = "blocked"


class DAGProblem(BaseModel):
    code: str
    message: str
    task_id: str | None = None


class TaskDAGNode(BaseModel):
    task_id: str
    notion_page_id: str
    parent_task_id: str
    project_id: str | None
    repository: str | None
    specification_hash: str
    dependencies: list[str] = Field(default_factory=list)
    dependency_source: Literal["linear", "none"] = "none"
    mandatory: bool = True


class TaskDAG(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    dag_id: str = Field(default_factory=new_id)
    parent_task_id: str
    parent_notion_page_id: str
    parent_execution_run_id: str | None = None
    version: int = 1
    parent_specification_hash: str
    dag_hash: str
    nodes: list[TaskDAGNode]
    status: DAGStatus
    problems: list[DAGProblem] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ParentRun(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    parent_run_id: str = Field(default_factory=new_id)
    parent_task_id: str
    parent_notion_page_id: str
    parent_execution_run_id: str
    dag_id: str
    dag_hash: str
    status: ExecutionStatus = ExecutionStatus.ACCEPTED
    child_execution_run_ids: dict[str, str] = Field(default_factory=dict)
    child_workflow_run_ids: dict[str, str] = Field(default_factory=dict)
    completion_reasons: list[str] = Field(default_factory=list)
    waiting_for: str | None = None
    completion_approved: bool = False
    notes_page_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ArtifactHandoff(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    handoff_id: str = Field(default_factory=new_id)
    parent_run_id: str
    producer_task_id: str
    producer_execution_run_id: str
    consumer_task_id: str
    artifact_id: str
    path: str
    content_hash: str
    git_commit: str | None = None
    artifact_schema_version: str = "1.0"
    scientific_metadata: dict = Field(default_factory=dict)
    validation_status: Literal["validated", "invalid", "stale"]
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def dedupe_key(self) -> str:
        return f"{self.parent_run_id}:{self.producer_execution_run_id}:{self.consumer_task_id}:{self.artifact_id}:{self.content_hash}"


def specification_hash(spec: TaskSpec) -> str:
    payload = spec.model_dump(mode="json", exclude={"sources"})
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def discover_and_build(manager, parent_bundle, parent_execution_run_id: str) -> TaskDAG:
    """Discover direct children and return a validated immutable snapshot."""
    problems: list[DAGProblem] = []
    nodes: list[TaskDAGNode] = []
    parent = parent_bundle.page
    if parent is None:
        problems.append(DAGProblem(code="missing_parent_page", message="parent has no Notion page"))
        child_ids: list[str] = []
    else:
        child_ids = list(parent.child_page_ids)
    if parent_bundle.spec is None or not parent_bundle.spec.acceptance_criteria:
        problems.append(DAGProblem(code="missing_parent_acceptance_criteria",
                                   message="parent canonical Acceptance Criteria section is required"))
    if not child_ids:
        problems.append(DAGProblem(code="missing_child", message="parent has no existing child tasks"))
    if len(child_ids) != len(set(child_ids)):
        problems.append(DAGProblem(code="duplicate_child", message="Notion child relation contains duplicates"))
    if parent and parent.page_id in child_ids:
        problems.append(DAGProblem(code="circular_parent", message="parent relates to itself as a child"))
    for page_id in dict.fromkeys(child_ids):
        page = manager.notion.get_task(page_id)
        if parent and parent.page_id not in page.parent_page_ids:
            problems.append(DAGProblem(code="identity_mismatch", task_id=page.linear_issue_id,
                                       message="child does not point back to the requested parent"))
        if not page.linear_issue_id:
            problems.append(DAGProblem(code="missing_linear_identity", message=f"child page {page_id} has no Linear Issue ID"))
            continue
        bundle = manager.load(page.linear_issue_id)
        if bundle.page is None or bundle.page.page_id != page.page_id:
            problems.append(DAGProblem(code="identity_mismatch", task_id=page.linear_issue_id,
                                       message="Notion/Linear child identity is not one-to-one"))
            continue
        if bundle.issue.project_id != parent_bundle.issue.project_id:
            problems.append(DAGProblem(code="cross_project", task_id=bundle.issue.identifier,
                                       message="child belongs to a different Linear project"))
        if bundle.spec is None:
            problems.append(DAGProblem(code="missing_specification", task_id=bundle.issue.identifier,
                                       message="child has no usable scientific specification"))
            continue
        for reason in bundle.reasons:
            # An open blocker is the edge this scheduler is responsible for;
            # it is not an intake failure when the blocker is inside the DAG.
            if reason.code is BlockedCode.DEPENDENCY_INCOMPLETE:
                continue
            if bundle.issue.state_type == "completed" and reason.message.startswith("task is "):
                continue
            problems.append(DAGProblem(code=reason.code.value, task_id=bundle.issue.identifier, message=reason.message))
        spec = bundle.spec
        if not spec.specification:
            problems.append(DAGProblem(code="missing_implementation_specification", task_id=bundle.issue.identifier,
                                       message="canonical Specification section is required"))
        if not spec.acceptance_criteria:
            problems.append(DAGProblem(code="missing_acceptance_criteria", task_id=bundle.issue.identifier,
                                       message="canonical Acceptance Criteria section is required"))
        if not spec.repository:
            problems.append(DAGProblem(code="invalid_repository_mapping", task_id=bundle.issue.identifier,
                                       message="canonical Repository section is required"))
        if spec.dependencies_note and re.search(r"\b[A-Z]+-\d+\b", spec.dependencies_note):
            problems.append(DAGProblem(code="ambiguous_dependency", task_id=bundle.issue.identifier,
                                       message="prose dependency cannot become an executable edge without approval"))
        nodes.append(TaskDAGNode(task_id=bundle.issue.identifier, notion_page_id=page.page_id,
                                 parent_task_id=parent_bundle.issue.identifier,
                                 project_id=bundle.issue.project_id, repository=spec.repository,
                                 specification_hash=specification_hash(spec),
                                 dependencies=sorted({x.identifier for x in bundle.issue.blocked_by}),
                                 dependency_source="linear" if bundle.issue.blocked_by else "none"))
    _validate_graph(nodes, parent_bundle.issue.identifier, problems)
    parent_hash = specification_hash(parent_bundle.spec) if parent_bundle.spec else "unavailable"
    canonical = {"parent": {"task_id": parent_bundle.issue.identifier, "page": parent.page_id if parent else "",
                            "spec": parent_hash},
                 "nodes": [{"task_id": n.task_id, "page": n.notion_page_id, "spec": n.specification_hash,
                            "dependencies": n.dependencies, "repository": n.repository}
                           for n in sorted(nodes, key=lambda x: x.task_id)]}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
    return TaskDAG(parent_task_id=parent_bundle.issue.identifier,
                   parent_notion_page_id=parent.page_id if parent else "",
                   parent_execution_run_id=parent_execution_run_id, parent_specification_hash=parent_hash,
                   dag_hash=digest, nodes=nodes,
                   status=DAGStatus.BLOCKED if problems else DAGStatus.VALID, problems=problems)


def _validate_graph(nodes: list[TaskDAGNode], parent_task_id: str, problems: list[DAGProblem]) -> None:
    ids = [n.task_id for n in nodes]
    known = set(ids)
    if len(ids) != len(known):
        problems.append(DAGProblem(code="duplicate_task", message="DAG contains duplicate task identities"))
    deps = {n.task_id: set(n.dependencies) for n in nodes}
    for node in nodes:
        for dependency in node.dependencies:
            if dependency == parent_task_id:
                problems.append(DAGProblem(code="circular_parent", task_id=node.task_id,
                                           message="child depends on its parent task"))
            elif dependency not in known:
                problems.append(DAGProblem(code="missing_dependency", task_id=node.task_id,
                                           message=f"dependency {dependency} is not a child in this DAG"))
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(task_id: str) -> None:
        if task_id in visiting:
            problems.append(DAGProblem(code="cycle", task_id=task_id, message="dependency cycle detected"))
            return
        if task_id in visited:
            return
        visiting.add(task_id)
        for dep in deps.get(task_id, ()):
            if dep in known:
                visit(dep)
        visiting.remove(task_id); visited.add(task_id)
    for task_id in ids:
        visit(task_id)


def ready_nodes(dag: TaskDAG, child_statuses: dict[str, ExecutionStatus]) -> list[TaskDAGNode]:
    terminal_bad = {ExecutionStatus.BLOCKED, ExecutionStatus.FAILED, ExecutionStatus.CANCELED}
    ready = []
    for node in dag.nodes:
        if node.task_id in child_statuses:
            continue
        dep_states = [child_statuses.get(d) for d in node.dependencies]
        if any(s in terminal_bad for s in dep_states):
            continue
        if all(s is ExecutionStatus.COMPLETED for s in dep_states):
            ready.append(node)
    return ready


def aggregate_parent_status(dag: TaskDAG, child_statuses: dict[str, ExecutionStatus], *,
                            completion_gate_passed: bool = False) -> ExecutionStatus:
    if dag.status is DAGStatus.BLOCKED:
        return ExecutionStatus.BLOCKED
    states = [child_statuses.get(n.task_id, ExecutionStatus.IDLE) for n in dag.nodes if n.mandatory]
    if any(s is ExecutionStatus.FAILED for s in states):
        return ExecutionStatus.FAILED
    if any(s in {ExecutionStatus.RUNNING, ExecutionStatus.ACCEPTED} for s in states):
        return ExecutionStatus.RUNNING
    if any(s is ExecutionStatus.AWAITING_APPROVAL for s in states):
        return ExecutionStatus.AWAITING_APPROVAL if all(s in {ExecutionStatus.COMPLETED, ExecutionStatus.AWAITING_APPROVAL} for s in states) else ExecutionStatus.RUNNING
    if any(s in {ExecutionStatus.BLOCKED, ExecutionStatus.CANCELED} for s in states):
        return ExecutionStatus.BLOCKED
    if states and all(s is ExecutionStatus.COMPLETED for s in states):
        return ExecutionStatus.COMPLETED if completion_gate_passed else ExecutionStatus.RUNNING
    return ExecutionStatus.RUNNING


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
