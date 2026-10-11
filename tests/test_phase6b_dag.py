from pathlib import Path

from phase2_fakes import PAGE, make_issue, make_page
from phase5_fakes import ACC, INV, SECTIONS, FakeCompletionReviewer, approve, build
from research_autopilot.autopilot.dag import (DAGStatus, DependencyProposal, InferredDependency, TaskDAG,
                                              TaskDAGNode, aggregate_parent_status, apply_dependency_proposal,
                                              discover_and_build, ready_nodes)
from research_autopilot.autopilot.engine import poll
from research_autopilot.autopilot.execution import ExecutionStatus
from research_autopilot.autopilot.models import Budget, CompletionReview, CriterionResult, ResearchAuthorization
from research_autopilot.experiments.analyst import LlmRun
from research_autopilot.models import IssueRef
from research_autopilot.planning.agents import Usage
from research_autopilot.storage import db


def _dag(nodes):
    return TaskDAG(parent_task_id="INU-900", parent_notion_page_id=PAGE,
                   parent_execution_run_id="er-parent", parent_specification_hash="parent-hash",
                   dag_hash="h", nodes=nodes, status=DAGStatus.VALID)


def _node(task, deps=(), repo="h2-test"):
    return TaskDAGNode(task_id=task, notion_page_id=f"page-{task}", parent_task_id="INU-900",
                       project_id="p", repository=repo, specification_hash=f"hash-{task}",
                       dependencies=list(deps), dependency_source="linear" if deps else "none")


def test_ready_nodes_and_parent_status_are_deterministic():
    dag = _dag([_node("A1"), _node("A2", ["A1"]), _node("A3", ["A1"])])
    assert [n.task_id for n in ready_nodes(dag, {})] == ["A1"]
    statuses = {"A1": ExecutionStatus.COMPLETED}
    assert [n.task_id for n in ready_nodes(dag, statuses)] == ["A2", "A3"]
    statuses |= {"A2": ExecutionStatus.AWAITING_APPROVAL, "A3": ExecutionStatus.RUNNING}
    assert aggregate_parent_status(dag, statuses) is ExecutionStatus.RUNNING
    statuses["A3"] = ExecutionStatus.COMPLETED
    assert aggregate_parent_status(dag, statuses) is ExecutionStatus.AWAITING_APPROVAL


def test_cycle_and_missing_dependency_are_blocked(tmp_path):
    deps, linear, notion, *_ = build(tmp_path)
    parent_id = next(iter(notion.pages))
    p1, p2 = "21111111-2222-3333-4444-555555555555", "31111111-2222-3333-4444-555555555555"
    notion.pages[parent_id] = notion.pages[parent_id].model_copy(update={"child_page_ids": [p1, p2]})
    ref1 = IssueRef(identifier="INU-902", title="two", state_name="Todo", state_type="unstarted")
    ref2 = IssueRef(identifier="INU-901", title="one", state_name="Todo", state_type="unstarted")
    i1 = make_issue("INU-901", blocked_by=(ref1,), notion_ids=(p1,)).model_copy(update={"title": "[TEST] one"})
    i2 = make_issue("INU-902", blocked_by=(ref2, IssueRef(identifier="INU-999", title="missing", state_name="Todo", state_type="unstarted")), notion_ids=(p2,)).model_copy(update={"title": "[TEST] two"})
    linear.issues.update({i1.identifier: i1, i2.identifier: i2})
    for page_id, task_id in ((p1, "INU-901"), (p2, "INU-902")):
        notion.pages[page_id] = make_page(page_id=page_id, linear_id=task_id, sections=dict(SECTIONS)).model_copy(
            update={"parent_page_ids": [parent_id]})
    parent = deps.plan.manager.load("INU-900")
    dag = discover_and_build(deps.plan.manager, parent, "er")
    assert dag.status is DAGStatus.BLOCKED
    assert {p.code for p in dag.problems} >= {"cycle", "missing_dependency"}


def test_identity_cross_project_and_missing_spec_are_blocked(tmp_path):
    deps, linear, notion, *_ = build(tmp_path)
    parent_id = next(iter(notion.pages)); child_id = "41111111-2222-3333-4444-555555555555"
    notion.pages[parent_id] = notion.pages[parent_id].model_copy(update={"child_page_ids": [child_id]})
    issue = make_issue("INU-901", notion_ids=(child_id,), project="another").model_copy(update={"title": "[TEST] child"})
    linear.issues[issue.identifier] = issue
    notion.pages[child_id] = make_page(page_id=child_id, linear_id="INU-901", sections={"purpose": "x"})
    dag = discover_and_build(deps.plan.manager, deps.plan.manager.load("INU-900"), "er")
    codes = {p.code for p in dag.problems}
    assert {"identity_mismatch", "cross_project", "missing_implementation_specification",
            "missing_acceptance_criteria", "invalid_repository_mapping"} <= codes


class ParentAwareReviewer:
    model = "fake"

    def __init__(self):
        self.child = FakeCompletionReviewer()

    def review(self, payload):
        if "children" not in payload:
            return self.child.review(payload)
        evidence = payload["allowed_evidence"][:1]
        out = CompletionReview(criteria=[CriterionResult(criterion=c, status="met", evidence=evidence, note="")
                                         for c in payload["task"]["acceptance_criteria"]],
                               hypothesis_outcome="supported", summary="aggregate complete", limitations=[], unresolved=[])
        return LlmRun(output=out, usage=Usage(input_tokens=1, output_tokens=1), model=self.model, cost_usd=0.0)


class FakeDependencyInferer:
    model = "fake-dependency-planner"

    def __init__(self, proposal):
        self.proposal = proposal
        self.payloads = []

    def infer(self, payload):
        self.payloads.append(payload)
        return LlmRun(output=self.proposal, usage=Usage(input_tokens=10, output_tokens=5),
                      model=self.model, cost_usd=0.0001)


def _parent_fixture(tmp_path: Path, count: int = 4):
    deps, linear, notion, writer, channel = build(tmp_path, drafts=[], n=20, completion=ParentAwareReviewer())
    deps.settings.prefect_ui_url = "http://prefect"
    parent_id = next(iter(notion.pages))
    child_pages = []
    previous = None
    for offset in range(count):
        task_id = f"INU-{901 + offset}"
        page_id = f"{offset + 2:08d}-2222-3333-4444-555555555555"
        blockers = () if previous is None else (IssueRef(identifier=previous, title=previous, state_name="Todo", state_type="unstarted"),)
        issue = make_issue(task_id, blocked_by=blockers, notion_ids=(page_id,)).model_copy(update={"title": f"[TEST] child {task_id}"})
        linear.issues[task_id] = issue; linear.histories[task_id] = []
        notion.pages[page_id] = make_page(page_id=page_id, linear_id=task_id, sections=dict(SECTIONS)).model_copy(
            update={"title": f"[TEST] child {task_id}", "parent_page_ids": [parent_id]})
        child_pages.append(page_id); previous = task_id
    notion.pages[parent_id] = notion.pages[parent_id].model_copy(update={"child_page_ids": child_pages})
    auth = ResearchAuthorization(root_task_id="INU-900", approved_by="human", approval_request_id="approved",
                                 spec_hash="parent", purpose=SECTIONS["purpose"], research_target="parent", hypothesis=None,
                                 scientific_invariants=list(INV), acceptance_criteria=list(ACC), budget=Budget(api_usd=100, compute_cpu_hours=100))
    deps.store.init_schema()
    deps.store.put(db.authorizations, auth.authorization_id, auth.model_dump(mode="json"), root_task_id="INU-900")
    return deps, linear, notion, writer, child_pages


def test_parent_intake_runs_children_and_completes_with_artifact_handoffs(tmp_path):
    deps, linear, notion, writer, child_pages = _parent_fixture(tmp_path)
    for _ in range(8):
        poll(deps, prefect_flow_run_id="parent-flow")
        parents = deps.store.find(db.parent_runs)
        if parents and parents[0]["status"] == "Completed":
            break
    parent = deps.store.find(db.parent_runs)[0]
    assert parent["status"] == "Completed"
    assert len(parent["child_execution_run_ids"]) == 4
    assert all(notion.pages[p].execution_status == "Completed" for p in child_pages)
    assert all(notion.pages[p].last_run_url == "http://prefect/runs/flow-run/parent-flow" for p in child_pages)
    assert linear.issues["INU-900"].state_name == "Done"
    assert all(linear.issues[f"INU-{n}"].state_name == "Done" for n in range(901, 905))
    assert deps.store.find(db.artifact_handoffs)
    assert writer.notes


def test_duplicate_parent_request_and_restart_do_not_duplicate_children(tmp_path):
    deps, _, notion, _, _ = _parent_fixture(tmp_path, count=1)
    poll(deps)
    parent_page = notion.pages[PAGE]
    notion.pages[PAGE] = parent_page.model_copy(update={"run_requested": True, "execution_status": "Requested"})
    poll(deps)
    assert len(deps.store.find(db.parent_runs)) == 1
    assert len(deps.store.find(db.execution_runs, task_id="INU-901")) == 1


def test_completed_parent_rerun_creates_new_version_and_history(tmp_path):
    deps, _, notion, _, _ = _parent_fixture(tmp_path, count=1)
    poll(deps)
    page = notion.pages[PAGE]
    notion.pages[PAGE] = page.model_copy(update={"run_requested": True, "execution_status": "Requested",
                                                 "last_edited_time": "2026-10-11T12:00:00.000Z"})
    poll(deps)
    assert len(deps.store.find(db.parent_runs)) == 2
    assert [x["version"] for x in deps.store.find(db.task_dags, parent_task_id="INU-900")] == [1, 2]
    assert len(deps.store.find(db.execution_runs, task_id="INU-901")) == 2


def test_stale_artifact_blocks_consumer(tmp_path):
    # Hash/path validation is fail-closed at scheduling time.
    deps, _, notion, _, _ = _parent_fixture(tmp_path, count=2)
    poll(deps)
    for row in deps.store.find(db.artifact_handoffs):
        if row["consumer_task_id"] == "INU-902":
            Path(row["path"]).write_text("tampered")
            break
    poll(deps)
    runs = deps.store.find(db.execution_runs, task_id="INU-902")
    assert runs and runs[0]["status"] == "Blocked"


def test_inferred_dag_requires_approval_before_any_child_starts(tmp_path):
    deps, linear, notion, _, _ = _parent_fixture(tmp_path, count=2)
    # No Linear dependency: the planner must derive an auditable candidate.
    linear.issues["INU-902"].blocked_by = []
    proposal = DependencyProposal(
        edges=[InferredDependency(producer_task_id="INU-901", consumer_task_id="INU-902",
                                  required_artifacts=["manifest"], rationale="A2 consumes A1 output",
                                  evidence=["Compute a small benchmark value and store it for later comparison."])],
        summary="A1 produces the shared benchmark definition before A2", unresolved=[])
    deps.dependency_inferer = FakeDependencyInferer(proposal)
    poll(deps, prefect_flow_run_id="parent-flow")
    parent = deps.store.find(db.parent_runs)[0]
    dag = deps.store.find(db.task_dags)[0]
    assert parent["status"] == "Awaiting Approval"
    assert dag["status"] == "pending_approval" and dag["inference_approved"] is False
    assert dag["nodes"][1]["dependencies"] == ["INU-901"]
    assert deps.store.find(db.execution_runs, task_id="INU-901") == []
    req = next(r for r in deps.approvals.pending() if r.kind.value == "dag")
    approve(deps, linear, req.request_id, spec_hash=req.spec_hash)
    for _ in range(5):
        poll(deps)
    assert deps.store.find(db.task_dags)[0]["inference_approved"] is True
    assert len(deps.store.find(db.execution_runs, task_id="INU-901")) == 1
    assert len(deps.store.find(db.execution_runs, task_id="INU-902")) == 1
    assert deps.store.find(db.parent_runs)[0]["status"] == "Completed"


def test_inferred_dag_approval_becomes_stale_when_child_spec_changes(tmp_path):
    deps, linear, notion, _, _ = _parent_fixture(tmp_path, count=2)
    linear.issues["INU-902"].blocked_by = []
    proposal = DependencyProposal(edges=[
        InferredDependency(producer_task_id="INU-901", consumer_task_id="INU-902",
                           required_artifacts=[], rationale="consumer needs producer",
                           evidence=["Compute a small benchmark value and store it for later comparison."])
    ], summary="one edge", unresolved=[])
    deps.dependency_inferer = FakeDependencyInferer(proposal)
    poll(deps)
    req = next(r for r in deps.approvals.pending() if r.kind.value == "dag")
    child_page = notion.pages["00000003-2222-3333-4444-555555555555"]
    notion.pages[child_page.page_id] = child_page.model_copy(
        update={"sections": {**child_page.sections, "purpose": "changed after proposal"}})
    approve(deps, linear, req.request_id, spec_hash=req.spec_hash)
    poll(deps)
    assert deps.store.find(db.parent_runs)[0]["status"] == "Blocked"
    assert deps.store.find(db.execution_runs, task_id="INU-901") == []


def test_inferred_unknown_task_and_cycle_fail_closed():
    dag = _dag([_node("A1"), _node("A2")])
    proposal = DependencyProposal(edges=[
        InferredDependency(producer_task_id="A1", consumer_task_id="A2", rationale="x", evidence=["spec"]),
        InferredDependency(producer_task_id="A2", consumer_task_id="A1", rationale="y", evidence=["spec"]),
        InferredDependency(producer_task_id="OUT", consumer_task_id="A1", rationale="z", evidence=["spec"]),
    ], summary="bad", unresolved=[])
    result = apply_dependency_proposal(dag, proposal)
    assert result.status is DAGStatus.BLOCKED
    assert {p.code for p in result.problems} >= {"cycle", "inferred_unknown_task"}


def test_inferred_edge_without_evidence_fails_closed():
    dag = _dag([_node("A1"), _node("A2")])
    proposal = DependencyProposal(edges=[
        InferredDependency(producer_task_id="A1", consumer_task_id="A2", rationale="", evidence=[]),
    ], summary="unsupported", unresolved=[])
    result = apply_dependency_proposal(dag, proposal)
    assert result.status is DAGStatus.BLOCKED
    assert any(p.code == "inferred_edge_without_evidence" for p in result.problems)


def _seed_parent(tmp_path: Path, edges: dict[str, list[str]], repos: dict[str, str], slots: int = 1):
    """Persist a running parent with a fixed DAG so scheduling can be driven step by step."""
    from research_autopilot.autopilot.dag import ParentRun, dag_hash
    from research_autopilot.autopilot.dag_runtime import _save_dag, _save_parent
    from research_autopilot.autopilot.execution import ExecutionRun

    deps, linear, notion, writer, _ = _parent_fixture(tmp_path, count=len(repos))
    deps.settings.max_concurrent_tasks = slots
    # Intake has already acknowledged the start request.
    notion.pages[PAGE] = notion.pages[PAGE].model_copy(update={"run_requested": False, "execution_status": "Running"})
    dag =discover_and_build(deps.plan.manager, deps.plan.manager.load("INU-900"), "")
    for node in dag.nodes:
        node.dependencies = list(edges.get(node.task_id, []))
        node.repository = repos[node.task_id]
    dag.status = DAGStatus.VALID
    dag.dag_hash = dag_hash(dag)
    execution = ExecutionRun(request_id="parent-request", task_id="INU-900", notion_task_id=PAGE,
                             status=ExecutionStatus.RUNNING, prefect_flow_run_id="parent-flow")
    deps.store.put(db.execution_runs, execution.execution_run_id, execution.model_dump(mode="json"),
                   request_id=execution.request_id, task_id=execution.task_id, status=execution.status.value)
    dag.parent_execution_run_id = execution.execution_run_id
    _save_dag(deps, dag)
    parent = ParentRun(parent_task_id="INU-900", parent_notion_page_id=PAGE,
                       parent_execution_run_id=execution.execution_run_id, dag_id=dag.dag_id,
                       dag_hash=dag.dag_hash, status=ExecutionStatus.RUNNING)
    _save_parent(deps, parent)
    return deps, linear, notion, parent


def _parent(deps, parent_run_id):
    from research_autopilot.autopilot.dag import ParentRun
    return ParentRun.model_validate(deps.store.get(db.parent_runs, parent_run_id))


def _set_child(deps, parent_run_id, task_id, status, error=None):
    """Simulate a child's own Phase 2-5 workflow reaching a state."""
    run_id = _parent(deps, parent_run_id).child_execution_run_ids[task_id]
    raw = deps.store.get(db.execution_runs, run_id) | {"status": status.value, "error_summary": error}
    deps.store.put(db.execution_runs, run_id, raw, request_id=raw["request_id"], task_id=task_id, status=status.value)


def _child_runs(deps, task_id):
    return deps.store.find(db.execution_runs, task_id=task_id)


def test_independent_children_run_in_parallel_within_global_slots(tmp_path):
    from research_autopilot.autopilot.dag_runtime import schedule_ready
    repos = {"INU-901": "repo-a", "INU-902": "repo-b", "INU-903": "repo-c"}
    deps, _, _, parent = _seed_parent(tmp_path, {}, repos, slots=2)
    assert sorted(r.task_id for r in schedule_ready(deps, parent)) == ["INU-901", "INU-902"]
    # Both slots are occupied: nothing more starts until one finishes.
    assert schedule_ready(deps, _parent(deps, parent.parent_run_id)) == []
    _set_child(deps, parent.parent_run_id, "INU-901", ExecutionStatus.COMPLETED)
    assert [r.task_id for r in schedule_ready(deps, _parent(deps, parent.parent_run_id))] == ["INU-903"]
    assert all(len(_child_runs(deps, t)) == 1 for t in repos)


def test_shared_repository_children_are_serialized_despite_spare_slots(tmp_path):
    from research_autopilot.autopilot.dag_runtime import schedule_ready
    repos = {"INU-901": "h2-test", "INU-902": "h2-test", "INU-903": "other-repo"}
    deps, _, _, parent = _seed_parent(tmp_path, {}, repos, slots=4)
    # Same repository is never mutated concurrently; a different repository may run alongside.
    assert sorted(r.task_id for r in schedule_ready(deps, parent)) == ["INU-901", "INU-903"]
    assert _child_runs(deps, "INU-902") == []
    _set_child(deps, parent.parent_run_id, "INU-901", ExecutionStatus.AWAITING_APPROVAL)
    assert schedule_ready(deps, _parent(deps, parent.parent_run_id)) == []
    _set_child(deps, parent.parent_run_id, "INU-901", ExecutionStatus.COMPLETED)
    assert [r.task_id for r in schedule_ready(deps, _parent(deps, parent.parent_run_id))] == ["INU-902"]


def test_child_awaiting_approval_does_not_stop_independent_siblings(tmp_path):
    from research_autopilot.autopilot.dag_runtime import advance_parents, schedule_ready
    repos = {"INU-901": "repo-a", "INU-902": "repo-b"}
    deps, _, notion, parent = _seed_parent(tmp_path, {}, repos, slots=2)
    schedule_ready(deps, parent)
    _set_child(deps, parent.parent_run_id, "INU-901", ExecutionStatus.AWAITING_APPROVAL)
    advance_parents(deps)
    assert _parent(deps, parent.parent_run_id).status is ExecutionStatus.RUNNING
    _set_child(deps, parent.parent_run_id, "INU-902", ExecutionStatus.COMPLETED)
    advance_parents(deps)
    # Only once every other mandatory child is done does the parent itself wait on the human.
    assert _parent(deps, parent.parent_run_id).status is ExecutionStatus.AWAITING_APPROVAL
    assert notion.pages[PAGE].execution_status == "Awaiting Approval"


def test_failed_child_is_isolated_and_never_releases_its_dependents(tmp_path):
    from research_autopilot.autopilot.dag_runtime import advance_parents, schedule_ready
    repos = {"INU-901": "repo-a", "INU-902": "repo-a", "INU-903": "repo-c"}
    deps, linear, _, parent = _seed_parent(tmp_path, {"INU-902": ["INU-901"]}, repos, slots=2)
    assert sorted(r.task_id for r in schedule_ready(deps, parent)) == ["INU-901", "INU-903"]
    _set_child(deps, parent.parent_run_id, "INU-901", ExecutionStatus.FAILED, "experiment crashed")
    advance_parents(deps)
    # The independent sibling is still managed; the parent is not finalized under it.
    stored = _parent(deps, parent.parent_run_id)
    assert stored.status is ExecutionStatus.RUNNING and stored.finished_at is None
    assert _child_runs(deps, "INU-903")[0]["status"] == ExecutionStatus.RUNNING.value
    _set_child(deps, parent.parent_run_id, "INU-903", ExecutionStatus.COMPLETED)
    advance_parents(deps)
    stored = _parent(deps, parent.parent_run_id)
    assert stored.status is ExecutionStatus.FAILED and stored.finished_at is not None
    assert _child_runs(deps, "INU-902") == []  # dependent is never started
    parent_run = deps.store.get(db.execution_runs, stored.parent_execution_run_id)
    assert "INU-901 Failed: experiment crashed" in parent_run["error_summary"]
    assert linear.issues["INU-900"].state_name != "Done"


def test_independent_ready_child_still_starts_after_sibling_fails(tmp_path):
    from research_autopilot.autopilot.dag_runtime import advance_parents, schedule_ready
    repos = {"INU-901": "repo-a", "INU-902": "repo-b", "INU-903": "repo-c"}
    deps, _, _, parent = _seed_parent(tmp_path, {"INU-902": ["INU-901"]}, repos, slots=1)
    assert [r.task_id for r in schedule_ready(deps, parent)] == ["INU-901"]
    _set_child(deps, parent.parent_run_id, "INU-901", ExecutionStatus.FAILED, "experiment crashed")
    created = advance_parents(deps)
    assert [r.task_id for r in created] == ["INU-903"]
    assert _parent(deps, parent.parent_run_id).status is ExecutionStatus.RUNNING
    _set_child(deps, parent.parent_run_id, "INU-903", ExecutionStatus.AWAITING_APPROVAL)
    advance_parents(deps)
    # Only a human decision can move anything now.
    assert _parent(deps, parent.parent_run_id).status is ExecutionStatus.AWAITING_APPROVAL
    _set_child(deps, parent.parent_run_id, "INU-903", ExecutionStatus.COMPLETED)
    advance_parents(deps)
    assert _parent(deps, parent.parent_run_id).status is ExecutionStatus.FAILED
    assert _child_runs(deps, "INU-902") == []


def test_child_blocked_by_scientific_invariant_blocks_parent_and_dependents(tmp_path):
    from research_autopilot.autopilot.dag_runtime import advance_parents, schedule_ready
    repos = {"INU-901": "repo-a", "INU-902": "repo-b"}
    deps, linear, notion, parent = _seed_parent(tmp_path, {"INU-902": ["INU-901"]}, repos, slots=2)
    schedule_ready(deps, parent)
    reason = "scientific invariant violated: Output schema is fixed"
    _set_child(deps, parent.parent_run_id, "INU-901", ExecutionStatus.BLOCKED, reason)
    advance_parents(deps)
    advance_parents(deps)  # a terminal parent is not revisited
    assert _parent(deps, parent.parent_run_id).status is ExecutionStatus.BLOCKED
    assert _child_runs(deps, "INU-902") == []
    assert _child_runs(deps, "INU-901")[0]["error_summary"] == reason
    assert notion.pages[PAGE].execution_status == "Blocked"
    assert linear.issues["INU-900"].state_name != "Done"


def test_partial_dag_recovery_resumes_without_rerunning_completed_children(tmp_path):
    from research_autopilot.autopilot.dag_runtime import advance_parents, schedule_ready
    repos = {"INU-901": "repo-a", "INU-902": "repo-b", "INU-903": "repo-c"}
    deps, _, _, parent = _seed_parent(tmp_path, {}, repos, slots=1)
    schedule_ready(deps, parent)
    first = next(t for t in repos if _child_runs(deps, t))
    _set_child(deps, parent.parent_run_id, first, ExecutionStatus.COMPLETED)
    # Restart: nothing in memory survives; the poller rebuilds state from PostgreSQL only.
    created = advance_parents(deps)
    assert len(created) == 1 and created[0].task_id != first
    advance_parents(deps)  # repeated recovery passes must not duplicate the in-flight child
    assert len(_child_runs(deps, first)) == 1
    assert sum(len(_child_runs(deps, t)) for t in repos) == 2
    assert _parent(deps, parent.parent_run_id).status is ExecutionStatus.RUNNING
