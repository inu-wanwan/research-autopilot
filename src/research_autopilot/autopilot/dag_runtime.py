"""Persistence, scheduling and recovery for Phase 6B parent DAG runs."""

from __future__ import annotations

from pathlib import Path

from research_autopilot.autopilot import policy
from research_autopilot.autopilot.dag import (ArtifactHandoff, DAGProblem, DAGStatus, ParentRun, TaskDAG,
                                              aggregate_parent_status, apply_dependency_proposal,
                                              discover_and_build, evidence_text, inference_payload, needs_dependency_inference,
                                              ready_nodes, sha256_file, specification_hash)
from research_autopilot.autopilot.execution import (ExecutionRequest, ExecutionRun, ExecutionStatus,
                                                    accept, record_blocked, request_key, transition)
from research_autopilot.autopilot.models import ApprovalKind, OutboxItem, OutboxState, WorkflowRun
from research_autopilot.models import utcnow
from research_autopilot.notifications import notify_once
from research_autopilot.notifications.events import EventKind, NotificationEvent
from research_autopilot.storage import db
from research_autopilot.tasks.notion_writer import heading, paragraph

_ACTIVE = {ExecutionStatus.ACCEPTED, ExecutionStatus.RUNNING, ExecutionStatus.AWAITING_APPROVAL}


def intake_parent(deps, bundle, page, *, prefect_flow_run_id: str | None = None) -> tuple[ParentRun | None, list[WorkflowRun]]:
    """Validate before durable acceptance; invalid DAGs are audited and acknowledged Blocked."""
    # A checkbox is level-triggered, not an event. The exact observed page
    # version has already been consumed even when its remote acknowledgement
    # was lost, so never construct another DAG from it.
    if any(r.get("request_key") == request_key(page) for r in deps.store.find(db.execution_requests)):
        return None, []
    provisional = discover_and_build(deps.plan.manager, bundle, "",
                                     registered_repositories=set(deps.impl.registry))
    history = deps.store.find(db.task_dags, parent_task_id=bundle.issue.identifier)
    provisional.version = max((int(item.get("version", 0)) for item in history), default=0) + 1
    if needs_dependency_inference(provisional):
        if deps.dependency_inferer is None:
            provisional.problems.append(DAGProblem(code="dependency_inference_unavailable",
                                                   message="no dependency inferer is configured"))
            provisional.status = DAGStatus.BLOCKED
        else:
            try:
                payload = inference_payload(deps.plan.manager, bundle, provisional)
                inferred = deps.dependency_inferer.infer(payload)
                provisional = apply_dependency_proposal(
                    provisional, inferred.output, model=getattr(inferred, "model", None),
                    cost_usd=getattr(inferred, "cost_usd", None),
                    evidence_corpus=evidence_text(payload))
            except Exception as exc:  # fail closed; no execution from an incomplete proposal
                provisional.problems.append(DAGProblem(
                    code="dependency_inference_failed",
                    message=f"dependency inference failed: {type(exc).__name__}"))
                provisional.status = DAGStatus.BLOCKED
    if provisional.status is DAGStatus.BLOCKED:
        reason = "; ".join(f"{p.code}: {p.message}" for p in provisional.problems)
        req = record_blocked(deps.store, page=page, issue=bundle.issue,
                             spec_hash=_spec_hash(bundle.spec), reason=reason)
        provisional.parent_execution_run_id = None
        _save_dag(deps, provisional, blocked_request_id=req.request_id)
        deps.notion_writer.project_execution(page.page_id, status="Blocked", error=reason[:1900], acknowledge=True)
        _human(deps, bundle.issue.identifier, f"parent-dag-blocked:{req.request_id}", ExecutionStatus.BLOCKED,
               "親タスクの DAG を作れないため開始していません", reason, _dag_problem_steps(provisional.problems))
        return None, []
    execution, reason = accept(deps.store, page=page, issue=bundle.issue, spec_hash=_spec_hash(bundle.spec))
    if execution is None:
        return None, []
    provisional.parent_execution_run_id = execution.execution_run_id
    execution.prefect_flow_run_id = prefect_flow_run_id
    deps.store.put(db.execution_runs, execution.execution_run_id, execution.model_dump(mode="json"),
                   request_id=execution.request_id, task_id=execution.task_id, status=execution.status.value)
    _save_dag(deps, provisional)
    parent = ParentRun(parent_task_id=bundle.issue.identifier, parent_notion_page_id=page.page_id,
                       parent_execution_run_id=execution.execution_run_id, dag_id=provisional.dag_id,
                       dag_hash=provisional.dag_hash)
    _save_parent(deps, parent)
    deps.notion_writer.project_execution(page.page_id, status="Accepted", acknowledge=True)
    if provisional.status is DAGStatus.PENDING_APPROVAL:
        edges = provisional.dependency_proposal.edges
        edge_lines = [f"{e.producer_task_id} -> {e.consumer_task_id}: {e.rationale}"
                      for e in edges] or ["No edges: all child tasks are proposed as independent"]
        req = deps.approvals.request(
            kind=ApprovalKind.DAG, issue=bundle.issue, root_task_id=bundle.issue.identifier,
            spec_hash=provisional.dag_hash, title=f"Approve inferred DAG for {bundle.issue.identifier}",
            what_changes=[f"Freeze DAG version {provisional.version} / {provisional.dag_hash}", *edge_lines],
            diff=[*(f"evidence: {'; '.join(e.evidence)}" for e in edges),
                  *(f"unresolved: {x}" for x in provisional.dependency_proposal.unresolved)],
            budget_summary=f"dependency inference model={provisional.inference_model or 'unknown'} "
                           f"cost=${provisional.inference_cost_usd or 0:.6f}",
            dedupe=provisional.dag_hash,
            payload={"parent_run_id": parent.parent_run_id, "dag_id": provisional.dag_id,
                     "dag_hash": provisional.dag_hash, "approval_purpose": "dag"})
        provisional.approval_request_id = req.request_id
        parent.waiting_for = req.request_id
        _save_dag(deps, provisional); _save_parent(deps, parent)
        _sync_parent_status(deps, parent, ExecutionStatus.AWAITING_APPROVAL, None)
        return parent, []
    _start_parent(deps, parent, execution, prefect_flow_run_id)
    return parent, schedule_ready(deps, parent)


def advance_parents(deps) -> list[WorkflowRun]:
    """Recover every non-terminal parent and schedule newly-ready children."""
    created: list[WorkflowRun] = []
    for raw in deps.store.find(db.parent_runs, status=[ExecutionStatus.ACCEPTED.value, ExecutionStatus.RUNNING.value,
                                                       ExecutionStatus.AWAITING_APPROVAL.value]):
        parent = ParentRun.model_validate(raw)
        dag = TaskDAG.model_validate(deps.store.get(db.task_dags, parent.dag_id))
        if dag.status is DAGStatus.PENDING_APPROVAL:
            _sync_parent_status(deps, parent, ExecutionStatus.AWAITING_APPROVAL, None)
            continue
        statuses = _child_statuses(deps, parent)
        _capture_artifacts(deps, parent, dag, statuses)
        if parent.research_cycle_id and dag.nodes and all(statuses.get(n.task_id) is ExecutionStatus.COMPLETED
                                                          for n in dag.nodes if n.mandatory):
            # A research cycle ends here; the Research Evaluator (not the parent completion gate) decides what next.
            _sync_parent_status(deps, parent, ExecutionStatus.COMPLETED, None, terminal=True)
            continue
        gate_passed = parent.completion_approved
        if dag.nodes and all(statuses.get(n.task_id) is ExecutionStatus.COMPLETED for n in dag.nodes if n.mandatory):
            if not gate_passed and not parent.waiting_for:
                gate_passed, parent.completion_reasons = _parent_completion_gate(deps, parent, dag)
                if not gate_passed:
                    _request_parent_completion_approval(deps, parent)
        derived = aggregate_parent_status(dag, statuses, completion_gate_passed=gate_passed)
        if parent.waiting_for and not gate_passed:
            derived = ExecutionStatus.AWAITING_APPROVAL
        if derived is ExecutionStatus.COMPLETED:
            _complete_parent(deps, parent, dag)
            continue
        if not parent.research_cycle_id and (any(s in _ACTIVE for s in statuses.values())
                                             or ready_nodes(dag, statuses)):
            _early_child_stop_notice(deps, parent, statuses)
        if derived in {ExecutionStatus.FAILED, ExecutionStatus.BLOCKED}:
            # Failure isolation: independent siblings keep running (and ready
            # independent nodes still start); the parent is finalized only once
            # nothing else in the DAG can make progress.
            in_flight = {t: s for t, s in statuses.items() if s in _ACTIVE}
            if in_flight or ready_nodes(dag, statuses):
                waiting_only = in_flight and all(s is ExecutionStatus.AWAITING_APPROVAL for s in in_flight.values())
                derived = (ExecutionStatus.AWAITING_APPROVAL if waiting_only and not ready_nodes(dag, statuses)
                           else ExecutionStatus.RUNNING)
            else:
                reasons = [*parent.completion_reasons, *_child_failure_reasons(deps, parent, statuses)]
                _sync_parent_status(deps, parent, derived,
                                    "; ".join(reasons) or "child execution did not complete", terminal=True)
                if not parent.research_cycle_id:  # a research cycle is reported by the research run
                    _human(deps, parent.parent_task_id, f"parent-ended:{parent.parent_run_id}", derived,
                           "子タスクが完了しなかったため親タスクを止めました",
                           "; ".join(reasons) or "child execution did not complete", _child_failure_steps(statuses))
                continue
        _sync_parent_status(deps, parent, derived, None)
        created.extend(schedule_ready(deps, parent))
    return created


def validate_child_inputs(deps, run: WorkflowRun) -> str | None:
    """Revalidate immutable dependency artifacts immediately before work.

    Scheduling-time validation alone has a TOCTOU window; this second check
    makes a changed/deleted artifact fail closed before Phase 2 starts.
    """
    if not run.execution_run_id:
        return None
    raw = deps.store.get(db.execution_runs, run.execution_run_id)
    if not raw:
        return None
    execution = ExecutionRun.model_validate(raw)
    parent_run_id = execution.retry_metadata.get("parent_run_id")
    if not parent_run_id:
        return None
    parent_raw = deps.store.get(db.parent_runs, parent_run_id)
    if not parent_raw:
        return "parent DAG run is missing"
    parent = ParentRun.model_validate(parent_raw)
    dag = TaskDAG.model_validate(deps.store.get(db.task_dags, parent.dag_id))
    node = next((n for n in dag.nodes if n.task_id == run.task_id), None)
    if node is None:
        return "child is not present in its fixed DAG snapshot"
    if not node.dependencies:
        return None
    rows = deps.store.find(db.artifact_handoffs, parent_run_id=parent.parent_run_id,
                           consumer_task_id=node.task_id)
    valid = set()
    stale = set()
    for row in rows:
        path = Path(row["path"])
        intact = path.is_file() and sha256_file(path) == row["content_hash"]
        if row.get("validation_status") == "validated" and intact:
            valid.add(row["producer_task_id"])
        elif row.get("validation_status") == "validated":
            stale.add(row["producer_task_id"])
            row["validation_status"] = "stale"
            deps.store.put(db.artifact_handoffs, row["handoff_id"], row,
                           parent_run_id=row["parent_run_id"], producer_task_id=row["producer_task_id"],
                           consumer_task_id=row["consumer_task_id"], dedupe_key=(
                               f"{row['parent_run_id']}:{row['producer_execution_run_id']}:{row['consumer_task_id']}:"
                               f"{row['artifact_id']}:{row['content_hash']}"))
    by_producer = {p: [r for r in rows if r["producer_task_id"] == p and r.get("validation_status") == "validated"]
                   for p in node.dependencies}
    required_missing = {p for p, required in node.artifact_dependencies.items()
                        if required and not set(required) <= {r["artifact_id"] for r in by_producer.get(p, [])}}
    missing = (set(node.dependencies) - valid) | stale | required_missing
    return f"validated dependency artifact is missing or stale for {sorted(missing)}" if missing else None


def _is_research_parent(deps, parent_run_id: str) -> bool:
    raw = deps.store.get(db.parent_runs, parent_run_id)
    return bool(raw and raw.get("research_cycle_id"))


def schedule_ready(deps, parent: ParentRun, research_run: WorkflowRun | None = None) -> list[WorkflowRun]:
    from research_autopilot.autopilot.engine import _new_run
    if parent.research_cycle_id and not _research_may_start(deps, parent, research_run):
        return []  # stopped / paused / cancelled / out of budget: nothing new starts, running children finish
    dag = TaskDAG.model_validate(deps.store.get(db.task_dags, parent.dag_id))
    statuses = _child_statuses(deps, parent)
    active = sum(s in _ACTIVE for s in statuses.values())
    capacity = max(0, deps.settings.max_concurrent_tasks - active)
    if capacity == 0:
        return []
    running_repos = {n.repository for n in dag.nodes if statuses.get(n.task_id) in _ACTIVE}
    made: list[WorkflowRun] = []
    for node in ready_nodes(dag, statuses):
        if len(made) >= capacity:
            break
        # Same-repository mutation is serialized even if global concurrency is raised.
        if node.repository and node.repository in running_repos:
            continue
        if node.dependencies and not _validated_inputs(deps, parent, node):
            _block_unsatisfied_child(deps, parent, node, "validated dependency artifact is missing or stale")
            continue
        issue = deps.linear.get_issue(node.task_id)
        if issue.state_type == "completed":
            deps.ops.set_state(issue, "Todo", f"parent DAG rerun {parent.parent_run_id}")
        key = f"parent-dag:{parent.parent_run_id}:{node.task_id}:{node.specification_hash}"
        req = ExecutionRequest(notion_task_id=node.notion_page_id, linear_issue_id=node.task_id,
                               source="parent_dag", specification_hash=node.specification_hash,
                               status=ExecutionStatus.ACCEPTED, request_key=key)
        if not deps.store.insert_new(db.execution_requests, req.request_id, req.model_dump(mode="json"),
                                     notion_task_id=node.notion_page_id, linear_issue_id=node.task_id,
                                     status=req.status.value, dedupe_key=key):
            continue
        er = ExecutionRun(request_id=req.request_id, task_id=node.task_id, notion_task_id=node.notion_page_id,
                          retry_metadata={"parent_run_id": parent.parent_run_id, "dag_id": dag.dag_id,
                                          "dag_hash": dag.dag_hash})
        deps.store.insert_new(db.execution_runs, er.execution_run_id, er.model_dump(mode="json"),
                              request_id=req.request_id, task_id=node.task_id, status=er.status.value)
        parent.child_execution_run_ids[node.task_id] = er.execution_run_id
        deps.notion_writer.project_execution(node.notion_page_id, status="Accepted")
        parent_execution = ExecutionRun.model_validate(
            deps.store.get(db.execution_runs, parent.parent_execution_run_id))
        wr = _new_run(deps, node.task_id, er,
                      prefect_flow_run_id=parent_execution.prefect_flow_run_id)
        wr.root_task_id = parent.parent_task_id
        wr.parent_task_id = parent.parent_task_id
        wr.continue_from = parent.carry_over.get(node.task_id)
        deps.store.put(db.workflow_runs, wr.run_id, wr.model_dump(mode="json"), task_id=wr.task_id, stage=wr.stage.value)
        parent.child_workflow_run_ids[node.task_id] = wr.run_id
        made.append(wr)
        if node.repository:
            running_repos.add(node.repository)
    _save_parent(deps, parent)
    return made


def _research_may_start(deps, parent: ParentRun, run: WorkflowRun | None) -> bool:
    """A research cycle's next child starts only while its research run goes on: not paused by the user, not
    stopped (budget / revoked scope / max cycles), cancelled or finished, and with budget left.  ``run`` is the
    in-memory research run when the caller is changing it (its stored copy may still say BLOCKED)."""
    from research_autopilot.autopilot.models import WorkflowStage
    from research_autopilot.research import orchestration
    from research_autopilot.research import workflow as rw
    run = run if run is not None else orchestration.research_run(deps.store, parent.parent_task_id)
    if run is not None and run.stage in (WorkflowStage.BLOCKED, WorkflowStage.CANCELLED, WorkflowStage.DONE,
                                         WorkflowStage.DEAD_LETTER):
        return False
    if orchestration.paused(deps.store, parent.parent_task_id, run):
        return False
    auth = rw.research_authorization(deps.store, parent.parent_task_id)
    if auth is None or run is None:
        return False
    return not orchestration.budget_problems(deps, run, auth)


def _start_parent(deps, parent: ParentRun, execution: ExecutionRun, flow_id: str | None) -> None:
    execution.status = transition(execution.status, ExecutionStatus.RUNNING)
    execution.started_at = execution.updated_at = utcnow(); execution.prefect_flow_run_id = flow_id
    deps.store.put(db.execution_runs, execution.execution_run_id, execution.model_dump(mode="json"),
                   request_id=execution.request_id, task_id=execution.task_id, status=execution.status.value)
    parent.status = ExecutionStatus.RUNNING; parent.started_at = parent.updated_at = utcnow(); _save_parent(deps, parent)
    deps.ops.set_state(deps.linear.get_issue(parent.parent_task_id), "In Progress", f"parent DAG {parent.dag_id}")
    deps.notion_writer.project_execution(parent.parent_notion_page_id, status="Running",
                                          run_url=_prefect_url(deps, flow_id))
    _event(deps, parent.parent_task_id, EventKind.TASK_STARTED, f"parent-started:{parent.parent_run_id}",
           parent.status, [f"DAG: {parent.dag_id}"])


def _child_statuses(deps, parent: ParentRun) -> dict[str, ExecutionStatus]:
    out = {}
    for task_id, run_id in parent.child_execution_run_ids.items():
        rec = deps.store.get(db.execution_runs, run_id)
        if rec:
            out[task_id] = ExecutionRun.model_validate(rec).status
    return out


def _capture_artifacts(deps, parent: ParentRun, dag: TaskDAG, statuses: dict[str, ExecutionStatus]) -> None:
    dependents = {n.task_id: [] for n in dag.nodes}
    for node in dag.nodes:
        for dependency in node.dependencies:
            dependents.setdefault(dependency, []).append(node.task_id)
    for producer, status in statuses.items():
        if status is not ExecutionStatus.COMPLETED:
            continue
        wr_raw = deps.store.get(db.workflow_runs, parent.child_workflow_run_ids.get(producer, ""))
        if not wr_raw:
            continue
        wr = WorkflowRun.model_validate(wr_raw)
        if not wr.experiment_run_ids:
            continue
        exp = deps.store.get_experiment(wr.experiment_run_ids[-1])
        if not exp:
            continue
        consumers = dependents.get(producer) or [parent.parent_task_id]
        # the validated outputs of the last attempt of each experiment are what downstream tasks consume; the run's
        # own records (analysis, manifest, proposals) are handed over too
        outputs: dict[str, str] = {}
        for job in exp.jobs:
            if job.validation and job.validation.status == "pass" and job.collect:
                outputs.update({a.path: a.stored_path for a in job.collect.artifacts})
        for name, raw_path in sorted({**exp.artifact_paths, **outputs}.items()):
            path = Path(raw_path)
            if not path.is_file():
                continue
            digest = sha256_file(path)
            for consumer in consumers:
                handoff = ArtifactHandoff(parent_run_id=parent.parent_run_id, producer_task_id=producer,
                                           producer_execution_run_id=parent.child_execution_run_ids[producer],
                                           consumer_task_id=consumer, artifact_id=name, path=str(path),
                                           content_hash=digest, git_commit=wr.commits[-1] if wr.commits else None,
                                           scientific_metadata={"experiment_run_id": exp.experiment_run_id},
                                           validation_status="validated")
                deps.store.insert_new(db.artifact_handoffs, handoff.handoff_id, handoff.model_dump(mode="json"),
                                      parent_run_id=parent.parent_run_id, producer_task_id=producer,
                                      consumer_task_id=consumer, dedupe_key=handoff.dedupe_key)


def _early_child_stop_notice(deps, parent: ParentRun, statuses: dict[str, ExecutionStatus]) -> None:
    """A child stopped while siblings still run: tell the human now (UX policy: no silent wait), once per child run."""
    for task_id, status in sorted(statuses.items()):
        if status not in {ExecutionStatus.FAILED, ExecutionStatus.BLOCKED}:
            continue
        execution_run_id = parent.child_execution_run_ids.get(task_id, "")
        rec = deps.store.get(db.execution_runs, execution_run_id) or {}
        reason = rec.get("error_summary") or "no cause recorded"
        _human(deps, parent.parent_task_id, f"parent-child-stopped:{execution_run_id}", status,
               f"子タスク {task_id} が止まりました（ほかの子タスクは実行を続けています）", f"{task_id}: {reason}",
               [f"{task_id} の理由を確認し、Notion の仕様（または環境）を直す（今直しておいてよい）",
                "ほかの子タスクが終わると親タスクが止まり、approvals に通知が届く",
                "親タスクで Run Autopilot を押す（完了した子タスクの成果物は再利用される）"])


def _child_failure_reasons(deps, parent: ParentRun, statuses: dict[str, ExecutionStatus]) -> list[str]:
    out = []
    for task_id, status in sorted(statuses.items()):
        if status in {ExecutionStatus.FAILED, ExecutionStatus.BLOCKED, ExecutionStatus.CANCELED}:
            rec = deps.store.get(db.execution_runs, parent.child_execution_run_ids[task_id]) or {}
            out.append(f"{task_id} {status.value}: {rec.get('error_summary') or 'no cause recorded'}")
    return out


def _validated_inputs(deps, parent: ParentRun, node) -> bool:
    rows = deps.store.find(db.artifact_handoffs, parent_run_id=parent.parent_run_id, consumer_task_id=node.task_id)
    by_producer = {p: [] for p in node.dependencies}
    for row in rows:
        if row["producer_task_id"] in by_producer:
            by_producer[row["producer_task_id"]].append(row)
    for producer, items in by_producer.items():
        if not items or not all(r.get("validation_status") == "validated" and Path(r["path"]).is_file() and
                                sha256_file(Path(r["path"])) == r["content_hash"] for r in items):
            return False
        required = set(node.artifact_dependencies.get(producer, []))
        if required and not required <= {r["artifact_id"] for r in items}:
            return False
    return True


def _block_unsatisfied_child(deps, parent: ParentRun, node, reason: str) -> None:
    key = f"parent-dag:{parent.parent_run_id}:{node.task_id}:{node.specification_hash}"
    req = ExecutionRequest(notion_task_id=node.notion_page_id, linear_issue_id=node.task_id,
                           source="parent_dag", specification_hash=node.specification_hash,
                           status=ExecutionStatus.BLOCKED, blocked_reason=reason, request_key=key)
    deps.store.insert_new(db.execution_requests, req.request_id, req.model_dump(mode="json"),
                          notion_task_id=node.notion_page_id, linear_issue_id=node.task_id,
                          status=req.status.value, dedupe_key=key)
    er = ExecutionRun(request_id=req.request_id, task_id=node.task_id, notion_task_id=node.notion_page_id,
                      status=ExecutionStatus.BLOCKED, error_summary=reason, finished_at=utcnow(),
                      retry_metadata={"parent_run_id": parent.parent_run_id})
    deps.store.insert_new(db.execution_runs, er.execution_run_id, er.model_dump(mode="json"),
                          request_id=req.request_id, task_id=node.task_id, status=er.status.value)
    parent.child_execution_run_ids[node.task_id] = er.execution_run_id
    deps.notion_writer.project_execution(node.notion_page_id, status="Blocked", error=reason)


def _parent_completion_gate(deps, parent: ParentRun, dag: TaskDAG) -> tuple[bool, list[str]]:
    bundle = deps.plan.manager.load(parent.parent_task_id)
    criteria = [x.strip().removeprefix("- ") for x in (bundle.spec.acceptance_criteria or "").splitlines() if x.strip()]
    artifacts = deps.store.find(db.artifact_handoffs, parent_run_id=parent.parent_run_id,
                                consumer_task_id=parent.parent_task_id)
    allowed = {f"artifact:{a['producer_task_id']}:{a['artifact_id']}:{a['content_hash']}" for a in artifacts
               if a.get("validation_status") == "validated"}
    if not criteria or not allowed:
        return False, ["parent acceptance criteria or validated aggregate evidence is missing"]
    if deps.completion_reviewer is None:
        return False, ["parent Completion Reviewer is unavailable"]
    payload = {"task": {"task_id": parent.parent_task_id, "acceptance_criteria": criteria,
                         "purpose": bundle.spec.purpose}, "allowed_evidence": sorted(allowed),
               "children": [{"task_id": n.task_id, "status": "Completed"} for n in dag.nodes]}
    result = deps.completion_reviewer.review(payload).output
    by_criterion = {c.criterion.strip(): c for c in result.criteria}
    reasons = []
    for criterion in criteria:
        review = by_criterion.get(criterion)
        if not review or review.status != "met" or not review.evidence or not set(review.evidence) <= allowed:
            reasons.append(f"parent criterion not established: {criterion}")
    return not reasons, reasons or [f"all {len(criteria)} parent criteria established"]


def _request_parent_completion_approval(deps, parent: ParentRun) -> None:
    bundle = deps.plan.manager.load(parent.parent_task_id)
    req = deps.approvals.request(kind=ApprovalKind.COMPLETION_REVIEW, issue=bundle.issue,
                                 root_task_id=parent.parent_task_id, spec_hash=_spec_hash(bundle.spec),
                                 title=f"Review parent DAG results of {parent.parent_task_id}",
                                 what_changes=["mark the parent Done after all mandatory children completed"],
                                 diff=parent.completion_reasons, budget_summary="-",
                                 dedupe=f"parent:{parent.parent_run_id}",
                                 payload={"parent_run_id": parent.parent_run_id,
                                          "approval_purpose": "completion"})
    parent.waiting_for = req.request_id
    _save_parent(deps, parent)


def apply_parent_approval(deps, request, approval) -> bool:
    """Apply an already verified Discord/form decision to a parent run."""
    parent_run_id = request.payload.get("parent_run_id")
    if not parent_run_id:
        return False
    raw = deps.store.get(db.parent_runs, parent_run_id)
    if not raw:
        return True
    parent = ParentRun.model_validate(raw)
    parent.waiting_for = None
    purpose = request.payload.get("approval_purpose", "completion")
    if purpose == "dag" and approval.decision == "approve":
        dag = TaskDAG.model_validate(deps.store.get(db.task_dags, parent.dag_id))
        if current_dag_approval_hash(deps, request) != request.spec_hash:
            parent.completion_reasons.append("DAG approval is stale because a parent/child specification changed")
            _sync_parent_status(deps, parent, ExecutionStatus.BLOCKED, parent.completion_reasons[-1], terminal=True)
            _human(deps, parent.parent_task_id, f"parent-dag-stale:{request.request_id}", ExecutionStatus.BLOCKED,
                   "DAG の承認は届きましたが、その後に仕様が変わったため開始していません",
                   parent.completion_reasons[-1], _DAG_RESTART_STEPS)
            return True
        dag.inference_approved = True
        dag.status = DAGStatus.VALID
        _save_dag(deps, dag)
        execution = ExecutionRun.model_validate(deps.store.get(db.execution_runs, parent.parent_execution_run_id))
        _start_parent(deps, parent, execution, execution.prefect_flow_run_id)
        return True
    if approval.decision == "approve":
        parent.completion_approved = True
        _sync_parent_status(deps, parent, ExecutionStatus.RUNNING, None)
    elif approval.decision == "reject":
        if approval.channel == "discord":
            deps.ops.reflect_verified_decision(deps.linear.get_issue(parent.parent_task_id), "Canceled",
                                                f"verified Discord reject ({request.request_id[:8]}) by {approval.actor}")
        _sync_parent_status(deps, parent, ExecutionStatus.CANCELED,
                            f"parent {purpose} rejected by {approval.actor}", terminal=True)
    else:
        if approval.channel == "discord":
            deps.ops.reflect_verified_decision(deps.linear.get_issue(parent.parent_task_id), "Revision Requested",
                                                f"verified Discord revise ({request.request_id[:8]}) by {approval.actor}")
        parent.completion_reasons.append(f"revision requested by {approval.actor}")
        _sync_parent_status(deps, parent, ExecutionStatus.BLOCKED,
                            parent.completion_reasons[-1], terminal=True)
        _human(deps, parent.parent_task_id, f"parent-revise:{request.request_id}", ExecutionStatus.BLOCKED,
               f"親タスクの{'DAG' if purpose == 'dag' else '完了レビュー'}に修正が求められたため止めています",
               f"revision requested by {approval.actor}: {approval.comment or '(コメントなし)'}",
               _DAG_RESTART_STEPS if purpose == "dag" else [
                   "コメントの修正内容を確認する", "Notion の親タスクの受け入れ基準または子タスクの仕様を直す",
                   "親タスクで Run Autopilot を押す（子タスクを実行し直して完了レビューに進む）"])
    return True


def current_dag_approval_hash(deps, request) -> str:
    """Return the approved hash only while every source snapshot is current."""
    dag_id = request.payload.get("dag_id")
    raw = deps.store.get(db.task_dags, dag_id) if dag_id else None
    if not raw:
        return "missing"
    dag = TaskDAG.model_validate(raw)
    parent = deps.plan.manager.load(dag.parent_task_id)
    if parent.spec is None or specification_hash(parent.spec) != dag.parent_specification_hash:
        return "stale"
    if parent.page is None or set(parent.page.child_page_ids) != {n.notion_page_id for n in dag.nodes}:
        return "stale"
    for node in dag.nodes:
        bundle = deps.plan.manager.load(node.task_id)
        if bundle.spec is None or specification_hash(bundle.spec) != node.specification_hash:
            return "stale"
    return dag.dag_hash


def close_parent_dag_approval(deps, request, note: str) -> None:
    """A parent approval expired or became stale.

    * DAG approval: the frozen DAG no longer matches the specs, so the parent stops and the human is told how to
      start again (Run Autopilot on the parent re-infers the DAG and asks again).
    * Completion review: asked again (new request version) so the parent never stays Awaiting forever."""
    raw = deps.store.get(db.parent_runs, request.payload.get("parent_run_id", ""))
    if not raw:
        return
    parent = ParentRun.model_validate(raw)
    if parent.waiting_for not in (None, request.request_id) or parent.status in (
            ExecutionStatus.COMPLETED, ExecutionStatus.CANCELED, ExecutionStatus.FAILED, ExecutionStatus.BLOCKED):
        return
    parent.waiting_for = None
    if request.payload.get("approval_purpose") != "dag":
        _request_parent_completion_approval(deps, parent)
        return
    parent.completion_reasons.append(note)
    _sync_parent_status(deps, parent, ExecutionStatus.BLOCKED, note, terminal=True)
    expired = request.status == "expired"
    _human(deps, parent.parent_task_id, f"parent-dag-closed:{request.request_id}", ExecutionStatus.BLOCKED,
           "DAG の承認依頼が" + ("期限切れになった" if expired else "仕様の変更で失効した") + "ため親タスクを止めました",
           note, _DAG_RESTART_STEPS)


def _complete_parent(deps, parent: ParentRun, dag: TaskDAG) -> None:
    if parent.status is ExecutionStatus.COMPLETED:
        return
    blocks = [heading("Parent DAG result"), paragraph(f"DAG {dag.dag_id} ({dag.dag_hash})"),
              heading("Children"), *[paragraph(f"{n.task_id}: Completed") for n in dag.nodes],
              heading("Completion Gate"), *[paragraph(x) for x in parent.completion_reasons]]
    item = deps.outbox.enqueue(OutboxItem(key=f"parent-notes:{parent.parent_run_id}", kind="notes",
                                         payload={"title": f"[AP] {parent.parent_task_id} — parent DAG results", "blocks": blocks}))
    item = deps.outbox.process(item) if item.state is not OutboxState.DONE else item
    if item.state is not OutboxState.DONE:
        raise RuntimeError(f"parent Notes write failed: {item.last_error}")
    parent.notes_page_id = item.notion_page_id
    deps.ops.set_state(deps.linear.get_issue(parent.parent_task_id), "Done", f"parent completion {parent.parent_run_id}")
    parent.status = ExecutionStatus.COMPLETED; parent.finished_at = parent.updated_at = utcnow(); _save_parent(deps, parent)
    rec = ExecutionRun.model_validate(deps.store.get(db.execution_runs, parent.parent_execution_run_id))
    rec.status = transition(rec.status, ExecutionStatus.COMPLETED); rec.finished_at = rec.updated_at = utcnow()
    deps.store.put(db.execution_runs, rec.execution_run_id, rec.model_dump(mode="json"), request_id=rec.request_id,
                   task_id=rec.task_id, status=rec.status.value)
    _project_parent(deps, parent, None)
    _event(deps, parent.parent_task_id, EventKind.TASK_COMPLETED, f"parent-completed:{parent.parent_run_id}",
           parent.status, [f"DAG: {dag.dag_id}", f"Notes: {deps.notion_writer.page_url(item.notion_page_id)}"])


def _save_dag(deps, dag: TaskDAG, blocked_request_id: str | None = None) -> None:
    # Invalid DAGs have no ExecutionRun, but retain the request id in this audit-only slot.
    parent_execution = dag.parent_execution_run_id or f"blocked:{blocked_request_id or dag.dag_id}"
    deps.store.put(db.task_dags, dag.dag_id, dag.model_dump(mode="json"), parent_task_id=dag.parent_task_id,
                   parent_execution_run_id=parent_execution, dag_hash=dag.dag_hash)


def _save_parent(deps, parent: ParentRun) -> None:
    parent.updated_at = utcnow()
    deps.store.put(db.parent_runs, parent.parent_run_id, parent.model_dump(mode="json"),
                   parent_task_id=parent.parent_task_id, parent_execution_run_id=parent.parent_execution_run_id,
                   status=parent.status.value)


def _project_parent(deps, parent: ParentRun, error: str | None) -> None:
    if parent.research_cycle_id:
        return  # the research objective's own status is projected by the research orchestration
    page = deps.plan.manager.notion.get_task(parent.parent_notion_page_id)
    if not page.run_requested:
        deps.notion_writer.project_execution(parent.parent_notion_page_id, status=parent.status.value, error=error)


def _sync_parent_status(deps, parent: ParentRun, status: ExecutionStatus, error: str | None,
                        terminal: bool = False) -> None:
    changed = parent.status is not status
    if changed:
        parent.status = _transition_via_running(parent.status, status)
    if terminal:
        parent.finished_at = utcnow()
    _save_parent(deps, parent)
    rec = ExecutionRun.model_validate(deps.store.get(db.execution_runs, parent.parent_execution_run_id))
    if rec.status is not status:
        rec.status = _transition_via_running(rec.status, status)
    rec.error_summary = error
    if terminal:
        rec.finished_at = utcnow()
    rec.updated_at = utcnow()
    deps.store.put(db.execution_runs, rec.execution_run_id, rec.model_dump(mode="json"),
                   request_id=rec.request_id, task_id=rec.task_id, status=rec.status.value)
    if changed:
        _project_parent(deps, parent, error)


def _transition_via_running(current: ExecutionStatus, target: ExecutionStatus) -> ExecutionStatus:
    # A parent waiting on a child's approval can see that child fail; the wait
    # has ended, so record Running before the terminal Failed state.
    if current is ExecutionStatus.AWAITING_APPROVAL and target is ExecutionStatus.FAILED:
        current = transition(current, ExecutionStatus.RUNNING)
    return transition(current, target)


def _spec_hash(spec) -> str:
    if spec is None:
        return "unavailable"
    return policy.spec_hash(spec.purpose, spec.scientific_invariants or "", spec.acceptance_criteria or "",
                            spec.specification or "", spec.title)


def _prefect_url(deps, flow_id: str | None) -> str | None:
    return f"{deps.settings.prefect_ui_url.rstrip('/')}/runs/flow-run/{flow_id}" if flow_id and deps.settings.prefect_ui_url else None


_DAG_RESTART_STEPS = [
    "Notion の親タスクと子タスクの仕様・依存関係（どの子がどの子の出力を使うか）を確認し、必要なら直す",
    "親タスクで Run Autopilot を押す（DAG を推論し直し、新しい承認依頼が届く）",
]


def _dag_problem_steps(problems: list[DAGProblem]) -> list[str]:
    """One step per problem, naming the child task (or the parent) whose Notion page must change."""
    spec = [p for p in problems if not p.code.startswith(("inferred_", "dependency_inference_"))]
    steps = [f"{p.task_id or '親タスク'} の Notion ページを直す: {p.message}（{p.code}）" for p in spec[:6]]
    if len(spec) > 6:
        steps.append(f"ほか {len(spec) - 6} 件（下の理由を参照）")
    if len(spec) < len(problems):
        # the dependency planner's proposal failed validation: a model output problem, not a spec problem
        steps.append("依存関係の自動推定が検証を通りませんでした（仕様の誤りではありません）。"
                     "そのまま Run Autopilot を押せば推定をやり直します。"
                     "繰り返す場合は Linear で子タスクの blocked-by を明示してください")
    steps.append("親タスクで Run Autopilot を押す（DAG を作り直す）")
    return steps


def _child_failure_steps(statuses: dict[str, ExecutionStatus]) -> list[str]:
    failed = sorted(t for t, s in statuses.items()
                    if s in {ExecutionStatus.FAILED, ExecutionStatus.BLOCKED, ExecutionStatus.CANCELED})
    return [f"止まった子タスク（{', '.join(failed) or '下の理由を参照'}）の理由を確認する",
            "その子タスクの Notion の仕様（または環境）を直す",
            "親タスクで Run Autopilot を押す（完了した子タスクの成果物は再利用される）"]


def _human(deps, task_id: str, key: str, status: ExecutionStatus, headline: str, reason: str,
           steps: list[str]) -> None:
    """Approvals message with steps for a parent that needs a human (UX policy)."""
    from research_autopilot.autopilot.engine import task_urls
    from research_autopilot.notifications.guidance import human_event
    notion, linear = task_urls(deps.plan.manager, task_id)
    links = [x for x in (f"Notion: {notion}" if notion else None, f"Linear: {linear}" if linear else None) if x]
    try:
        notify_once(deps.store, human_event(flow_name="autopilot-parent", task_id=task_id, state=status.value,
                                            headline=headline, reason=reason, steps=steps, links=links,
                                            dedupe_key=key), deps.notifier)
    except Exception:  # notification failure never changes the parent state
        pass


def _event(deps, task_id: str, kind: EventKind, key: str, status: ExecutionStatus, details: list[str]) -> None:
    notify_once(deps.store, NotificationEvent(kind=kind, flow_name="autopilot-parent", flow_run_name=task_id,
                                              state_name=status.value, details=details, dedupe_key=key), deps.notifier)
