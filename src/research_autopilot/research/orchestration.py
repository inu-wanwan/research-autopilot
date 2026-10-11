"""10B inside the Phase 5 engine: approved scope -> cycles of child tasks (6B DAG) -> evaluation -> next cycle.

Every step re-checks the authorization (version / content hash / unchanged spec), the user's stop request and the
budget.  Child tasks are created idempotently (outbox markers), each cycle is unique per (authorization, number),
and the number of cycles, new tasks per cycle and retries is bounded by the approved autonomy.
"""

from __future__ import annotations

import json
from pathlib import Path

from research_autopilot.autopilot.dag import (ParentRun, TaskDAG, TaskDAGNode, DAGStatus, dag_hash,
                                              specification_hash)
from research_autopilot.autopilot.execution import ExecutionRequest, ExecutionRun, ExecutionStatus
from research_autopilot.autopilot.models import (ApprovalStatus, OutboxItem, OutboxState, ResearchAuthorization,
                                                 WorkflowRun, WorkflowStage)
from research_autopilot.models import utcnow
from research_autopilot.research import evaluator as evaluator_mod
from research_autopilot.research import workflow as rw
from research_autopilot.research.models import (BudgetItem, CycleStatus, EvaluationDecision, PlannedTask, PlanStatus,
                                                ResearchControl, ResearchCycle, ResearchPlan, ScopeItem)
from research_autopilot.storage import db
from research_autopilot.tasks.notion_writer import bullet, heading, paragraph, table

CATEGORY = {"implementation": "実装", "experiment": "実験", "analysis": "解析"}


# ---------------------------------------------------------------- persistence / controls


def cycles(store, task_id: str) -> list[ResearchCycle]:
    return sorted((ResearchCycle.model_validate(r) for r in store.find(db.research_cycles, task_id=task_id)),
                  key=lambda c: (c.authorization_version, c.number))


def save_cycle(store, c: ResearchCycle) -> None:
    store.put(db.research_cycles, c.cycle_id, c.model_dump(mode="json"), task_id=c.task_id,
              dedupe_key=f"research:{c.task_id}:{c.number}", status=c.status.value)


def controls(store, task_id: str) -> list[ResearchControl]:
    return [ResearchControl.model_validate(r) for r in store.find(db.research_controls, task_id=task_id)]


def paused(store, task_id: str) -> bool:
    cs = controls(store, task_id)
    return bool(cs) and cs[-1].action == "stop"


def _mark_applied(store, c: ResearchControl, note: str) -> None:
    c.applied = True
    store.put(db.research_controls, c.control_id, {**c.model_dump(mode="json"), "note": note[:300]}, task_id=c.task_id)


def _restart_blocked(deps, run: WorkflowRun, control: ResearchControl) -> None:
    """Resume of a blocked research run = a new explicit start (the blocked execution record stays terminal)."""
    page = deps.plan.manager.load(run.task_id).page
    key = f"resume:{control.control_id}"
    req = ExecutionRequest(notion_task_id=page.page_id, linear_issue_id=run.task_id, source="research_resume",
                           specification_hash="resume", status=ExecutionStatus.ACCEPTED, request_key=key)
    if not deps.store.insert_new(db.execution_requests, req.request_id, req.model_dump(mode="json"),
                                 notion_task_id=page.page_id, linear_issue_id=run.task_id, status=req.status.value,
                                 dedupe_key=key):
        return
    er = ExecutionRun(request_id=req.request_id, task_id=run.task_id, notion_task_id=page.page_id,
                      retry_metadata={"resumed_by": control.actor, "previous_execution_run_id": run.execution_run_id})
    deps.store.insert_new(db.execution_runs, er.execution_run_id, er.model_dump(mode="json"), request_id=req.request_id,
                          task_id=run.task_id, status=er.status.value)
    run.execution_run_id, run.execution_request_id = er.execution_run_id, req.request_id
    run.stage, run.blocked_reason, run.waiting_for = WorkflowStage.DISCOVERED, None, None
    run.event("resumed", f"blocked research restarted by {control.actor}: authorization and plan are re-checked")
    auth = rw.research_authorization(deps.store, run.task_id)
    cs = cycles(deps.store, run.task_id)
    if auth is not None and cs and cs[-1].authorization_id == auth.authorization_id and cs[-1].decision \
            and cs[-1].decision.action == "stop":
        last = cs[-1]  # a human resumed a stopped research: the last cycle is evaluated again
        if _retry_blocked_cycle(deps, run, last, auth, control):
            return
        last.status = CycleStatus.EVALUATING
        last.results = []
        last.budget_after = {**last.budget_after, "human_resume": {
            "actor": control.actor, "at": control.at.isoformat(),
            "note": (control.actor.split("(", 1)[1].rstrip(")") if "(" in control.actor else
                     "resumed from Discord: the human asks to continue after the stop reasons were addressed"),
            "previous_decision": last.decision.rationale[:600]}}
        save_cycle(deps.store, last)
        run.event("resumed", f"cycle {last.number} is re-evaluated after the human resume")


def _retry_blocked_cycle(deps, run: WorkflowRun, cycle: ResearchCycle, auth: ResearchAuthorization,
                         control: ResearchControl) -> bool:
    """Retry a failed child DAG in the same research cycle.

    A child implementation/planning failure is a task retry, not new scientific
    information.  Reusing the cycle keeps ``max_cycles`` as a bound on research
    replanning while the separately approved ``max_retries_per_task`` bounds this
    recovery path.  Every old parent/execution record remains terminal for audit.
    """
    from research_autopilot.autopilot.dag_runtime import _save_parent, _start_parent, schedule_ready

    if not cycle.parent_run_id:
        return False
    old_raw = deps.store.get(db.parent_runs, cycle.parent_run_id)
    if not old_raw:
        return False
    old = ParentRun.model_validate(old_raw)
    if old.status not in (ExecutionStatus.BLOCKED, ExecutionStatus.FAILED):
        return False
    failed = []
    for task_id, execution_id in old.child_execution_run_ids.items():
        rec = deps.store.get(db.execution_runs, execution_id)
        if rec and ExecutionRun.model_validate(rec).status in (
                ExecutionStatus.BLOCKED, ExecutionStatus.FAILED, ExecutionStatus.CANCELED):
            failed.append(task_id)
    if not failed:
        return False
    previous = [ParentRun.model_validate(x) for x in deps.store.find(db.parent_runs)
                if x.get("research_cycle_id") == cycle.cycle_id]
    retries_used = max(0, len(previous) - 1)
    max_retries = int(((auth.scope or {}).get("autonomy") or {}).get("max_retries_per_task", 2))
    if retries_used >= max_retries:
        return False

    dag = TaskDAG.model_validate(deps.store.get(db.task_dags, old.dag_id))
    attempt = retries_used + 1
    page = deps.plan.manager.load(run.task_id).page
    key = f"research-cycle:{cycle.cycle_id}:retry:{attempt}"
    req = ExecutionRequest(notion_task_id=page.page_id, linear_issue_id=run.task_id,
                           source="research_cycle_retry", specification_hash=dag.parent_specification_hash,
                           status=ExecutionStatus.ACCEPTED, request_key=key)
    if not deps.store.insert_new(db.execution_requests, req.request_id, req.model_dump(mode="json"),
                                 notion_task_id=page.page_id, linear_issue_id=run.task_id,
                                 status=req.status.value, dedupe_key=key):
        return False
    er = ExecutionRun(request_id=req.request_id, task_id=run.task_id, notion_task_id=page.page_id,
                      retry_metadata={"research_cycle_id": cycle.cycle_id, "retry": attempt,
                                      "previous_parent_run_id": old.parent_run_id})
    deps.store.insert_new(db.execution_runs, er.execution_run_id, er.model_dump(mode="json"),
                          request_id=req.request_id, task_id=run.task_id, status=er.status.value)
    parent = ParentRun(parent_task_id=run.task_id, parent_notion_page_id=page.page_id,
                       parent_execution_run_id=er.execution_run_id, dag_id=dag.dag_id, dag_hash=dag.dag_hash,
                       research_cycle_id=cycle.cycle_id,
                       completion_reasons=[f"retry {attempt} after: {', '.join(failed)}"])
    _save_parent(deps, parent)
    _start_parent(deps, parent, er, None)
    cycle.parent_run_id = parent.parent_run_id
    cycle.status = CycleStatus.RUNNING
    cycle.results = []
    cycle.finished_at = None
    cycle.budget_after = {**cycle.budget_after, "human_resume": {
        "actor": control.actor, "at": control.at.isoformat(),
        "note": (control.actor.split("(", 1)[1].rstrip(")") if "(" in control.actor else
                 "resumed from Discord: retry the failed child DAG in the same research cycle"),
        "previous_decision": cycle.decision.rationale[:600]},
        "cycle_retry": attempt}
    cycle.decision = None
    save_cycle(deps.store, cycle)
    schedule_ready(deps, parent)
    run.stage = WorkflowStage.RESEARCHING
    run.event("resumed", f"cycle {cycle.number} child DAG retry {attempt}/{max_retries}: {', '.join(failed)}")
    return True


def request_control(store, task_id: str, action: str, actor: str, channel: str) -> ResearchControl:
    c = ResearchControl(task_id=task_id, action=action, actor=actor, channel=channel)
    store.put(db.research_controls, c.control_id, c.model_dump(mode="json"), task_id=task_id)
    return c


# ---------------------------------------------------------------- poll hooks


def tick(deps) -> dict:
    """Projection retries, stop / resume requests and re-planning of stale scope requests."""
    from research_autopilot.autopilot.engine import _runs, _save
    out = {"projections": rw.retry_projections(deps), "controls": [], "replanned": []}
    runs = [r for r in _runs(deps) if r.stage not in (WorkflowStage.DONE, WorkflowStage.CANCELLED,
                                                      WorkflowStage.DEAD_LETTER)]
    for run in runs:
        if run.parent_task_id or run.chain_depth:
            continue
        pending = [c for c in controls(deps.store, run.task_id) if not c.applied]
        if pending and pending[-1].action == "resume" and run.stage is WorkflowStage.BLOCKED and run.task_id \
                and (rw.latest_plan(deps.store, run.task_id) is not None):
            _restart_blocked(deps, run, pending[-1])
            _save(deps, run)
            out["controls"].append(f"{run.task_id}: blocked research restarted")
        for c in pending:
            _mark_applied(deps.store, c, f"seen at stage {run.stage.value}")
        stop = paused(deps.store, run.task_id)
        if stop and run.stage is WorkflowStage.RESEARCHING:
            run.stage, run.resume_stage = WorkflowStage.PAUSED, WorkflowStage.RESEARCHING.value
            run.event("paused", "stopped by the user: no new work starts; running jobs finish")
            _save(deps, run)
            _milestone(deps, run.task_id, "paused", ["Research paused by the user (running jobs finish)."],
                       kind=_ev().UNRECOVERABLE_ERROR, buttons="resume")
            out["controls"].append(f"{run.task_id}: paused")
        elif not stop and run.stage is WorkflowStage.PAUSED:
            run.stage, run.resume_stage = WorkflowStage.RESEARCHING, None
            run.event("resumed", "resumed by the user; authorization and budget are re-checked")
            _save(deps, run)
            _milestone(deps, run.task_id, "resumed", ["Research resumed; authorization and budget re-checked."],
                       kind=_ev().PHASE_TRANSITION, buttons=None)
            out["controls"].append(f"{run.task_id}: resumed")
        if run.stage in (WorkflowStage.AWAITING_AUTHORIZATION, WorkflowStage.AWAITING_APPROVAL) and run.waiting_for:
            raw = deps.store.get(db.approval_requests, run.waiting_for)
            if raw and raw.get("kind") == "research_scope" and raw.get("status") in (
                    ApprovalStatus.STALE.value, ApprovalStatus.EXPIRED.value):
                run.stage, run.waiting_for = WorkflowStage.DISCOVERED, None  # present a revised version
                run.event("replan", f"scope request {raw['status']}: a new plan version is drafted")
                _save(deps, run)
                out["replanned"].append(run.task_id)
    return out


# ---------------------------------------------------------------- main step


def step(deps, run: WorkflowRun) -> None:
    from research_autopilot.autopilot.engine import _load_spec
    store = deps.store
    bundle = _load_spec(deps, run.task_id)
    auth = rw.research_authorization(store, run.task_id)
    problems = rw.authorization_problems(store, auth, bundle.spec)
    if problems:
        if any("specification changed" in p for p in problems):
            run.stage = WorkflowStage.DISCOVERED  # re-plan from the edited spec -> new version for approval
            run.event("replan", problems[0])
            return
        _finish(deps, run, bundle, auth, "stopped", "; ".join(problems))
        return
    run.authorization_id = auth.authorization_id
    if paused(store, run.task_id):
        run.stage, run.resume_stage = WorkflowStage.PAUSED, WorkflowStage.RESEARCHING.value
        return
    over = budget_problems(deps, run, auth)
    if over:
        _finish(deps, run, bundle, auth, "stopped", "; ".join(over), budget=True)
        return
    cs = [c for c in cycles(store, run.task_id)]
    cur = cs[-1] if cs else None
    plan = rw.get_plan(store, auth.plan_id)
    try:
        _advance_cycles(deps, run, bundle, auth, plan, cs)
    except _Stop:
        return


def _advance_cycles(deps, run, bundle, auth, plan, cs) -> None:
    store = deps.store
    cur = cs[-1] if cs else None
    if cur is None:
        _start_cycle(deps, run, bundle, auth, 1, plan.tasks, plan.plan_id)
        return
    if cur.authorization_id != auth.authorization_id and cur.status is CycleStatus.COMPLETED and cur.decision \
            and cur.decision.action in ("stop", "complete") and not any(c.plan_id == plan.plan_id for c in cs):
        # a newly approved scope after a stopped research: its plan starts the next cycle
        _start_cycle(deps, run, bundle, auth, cur.number + 1, plan.tasks, plan.plan_id)
        return
    if cur.status in (CycleStatus.RUNNING, CycleStatus.EVALUATING):
        parent = ParentRun.model_validate(store.get(db.parent_runs, cur.parent_run_id)) if cur.parent_run_id else None
        if parent is not None and parent.status not in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED,
                                                        ExecutionStatus.BLOCKED, ExecutionStatus.CANCELED):
            return  # children still running
        _evaluate(deps, run, bundle, auth, cs)
        return
    d = cur.decision
    if d is None:
        return
    if d.action == "continue":
        _start_cycle(deps, run, bundle, auth, cur.number + 1, d.new_tasks, cur.plan_id)
    elif d.action == "scope_change":
        nxt = rw.get_plan(store, cur.next_plan_id) if cur.next_plan_id else None
        if nxt is not None and nxt.status is PlanStatus.APPROVED and auth.plan_id == nxt.plan_id:
            _start_cycle(deps, run, bundle, auth, cur.number + 1, nxt.tasks, nxt.plan_id)
        elif nxt is None or nxt.status in (PlanStatus.REJECTED, PlanStatus.SUPERSEDED):
            _finish(deps, run, bundle, auth, "stopped", "the proposed scope change was not approved")


# ---------------------------------------------------------------- budget


def usage(deps, task_id: str):
    from research_autopilot.autopilot.budget import chain_usage
    from research_autopilot.autopilot.engine import _runs
    chain = [r.model_dump(mode="json") for r in _runs(deps) if r.root_task_id == task_id or r.task_id == task_id]
    return chain_usage(deps.store, chain)


def budget_problems(deps, run: WorkflowRun, auth: ResearchAuthorization) -> list[str]:
    b = BudgetItem.model_validate((auth.scope or {}).get("budget") or {})
    u = usage(deps, run.task_id)
    out = []
    if b.cpu_hours is not None and u.cpu_hours >= b.cpu_hours:
        out.append(f"compute budget exhausted ({u.cpu_hours:.3f} >= {b.cpu_hours:g} CPU-h)")
    if b.api_usd is not None and u.api_usd >= b.api_usd:
        out.append(f"API budget exhausted (${u.api_usd:.2f} >= ${b.api_usd:g})")
    if u.unknown_api_cost_calls:
        out.append(f"{u.unknown_api_cost_calls} paid call(s) without a known price; budget cannot be verified")
    return out


# ---------------------------------------------------------------- cycles


def _start_cycle(deps, run: WorkflowRun, bundle, auth: ResearchAuthorization, number: int, tasks: list[PlannedTask],
                 plan_id: str | None) -> ResearchCycle:
    from research_autopilot.autopilot.dag_runtime import _save_dag, _save_parent, _start_parent, schedule_ready
    store = deps.store
    a = (auth.scope or {}).get("autonomy") or {}
    if number > int(a.get("max_cycles", 5)):
        _finish(deps, run, bundle, auth, "stopped", f"the approved maximum of {a.get('max_cycles')} cycles is reached")
        raise _Stop()
    existing = [c for c in cycles(store, run.task_id) if c.number == number]
    cycle = existing[0] if existing else ResearchCycle(
        task_id=run.task_id, authorization_id=auth.authorization_id, authorization_version=auth.version,
        number=number, tasks=tasks, plan_id=plan_id)
    if not existing:
        u = usage(deps, run.task_id)
        cycle.budget_before = {"cpu_hours": round(u.cpu_hours, 4), "api_usd": round(u.api_usd, 4)}
        if not store.insert_new(db.research_cycles, cycle.cycle_id, cycle.model_dump(mode="json"), task_id=run.task_id,
                                dedupe_key=f"research:{run.task_id}:{number}", status=cycle.status.value):
            return [c for c in cycles(store, run.task_id) if c.number == number][0]
    if cycle.parent_run_id:
        return cycle
    page = bundle.page
    keys = {}
    for t in cycle.tasks:  # idempotent creation (outbox markers adopt already created Notion / Linear objects)
        item = deps.outbox.enqueue(OutboxItem(key=f"rtask:{cycle.cycle_id}:{t.key}", kind="proposal_task",
                                              payload=_child_payload(deps, run, bundle, auth, cycle, t)))
        item = deps.outbox.process(item) if item.state is not OutboxState.DONE else item
        if item.state is not OutboxState.DONE:
            raise RuntimeError(f"research task {t.key} could not be created: {item.last_error}")
        keys[t.key] = (item.linear_identifier, item.notion_page_id)
    cycle.child_task_ids = {k: v[0] for k, v in keys.items()}
    cycle.child_page_ids = {k: v[1] for k, v in keys.items()}
    for t in cycle.tasks:  # Linear blocked-by = the DAG edges (Linear is the Task Status SSOT)
        blocked = deps.linear.get_issue(keys[t.key][0])
        for dep in t.depends_on:
            if dep in keys and keys[dep][0] not in {b.identifier for b in blocked.blocked_by}:
                deps.linear.create_relation(deps.linear.get_issue(keys[dep][0]).id, blocked.id)
    nodes = []
    for t in cycle.tasks:
        child = deps.plan.manager.load(keys[t.key][0])
        nodes.append(TaskDAGNode(task_id=keys[t.key][0], notion_page_id=keys[t.key][1], parent_task_id=run.task_id,
                                 project_id=child.issue.project_id if child.issue else None,
                                 repository=t.repository, specification_hash=specification_hash(child.spec),
                                 dependencies=[keys[d][0] for d in t.depends_on if d in keys],
                                 dependency_source="linear" if t.depends_on else "none"))
    history = store.find(db.task_dags, parent_task_id=run.task_id)
    dag = TaskDAG(parent_task_id=run.task_id, parent_notion_page_id=page.page_id,
                  version=max((int(x.get("version", 0)) for x in history), default=0) + 1,
                  parent_specification_hash=specification_hash(bundle.spec), dag_hash="", nodes=nodes,
                  status=DAGStatus.VALID)
    dag.dag_hash = dag_hash(dag)
    key = f"research-cycle:{cycle.cycle_id}"
    req = ExecutionRequest(notion_task_id=page.page_id, linear_issue_id=run.task_id, source="research_cycle",
                           specification_hash=dag.parent_specification_hash, status=ExecutionStatus.ACCEPTED,
                           request_key=key)
    store.insert_new(db.execution_requests, req.request_id, req.model_dump(mode="json"), notion_task_id=page.page_id,
                     linear_issue_id=run.task_id, status=req.status.value, dedupe_key=key)
    er = ExecutionRun(request_id=req.request_id, task_id=run.task_id, notion_task_id=page.page_id,
                      retry_metadata={"research_cycle_id": cycle.cycle_id})
    store.insert_new(db.execution_runs, er.execution_run_id, er.model_dump(mode="json"), request_id=req.request_id,
                     task_id=run.task_id, status=er.status.value)
    dag.parent_execution_run_id = er.execution_run_id
    _save_dag(deps, dag)
    parent = ParentRun(parent_task_id=run.task_id, parent_notion_page_id=page.page_id,
                       parent_execution_run_id=er.execution_run_id, dag_id=dag.dag_id, dag_hash=dag.dag_hash,
                       research_cycle_id=cycle.cycle_id)
    _save_parent(deps, parent)
    _start_parent(deps, parent, er, None)
    cycle.parent_run_id = parent.parent_run_id
    save_cycle(store, cycle)
    schedule_ready(deps, parent)
    run.event("research_cycle", f"cycle {number} started: {len(cycle.tasks)} task(s) "
                                f"{', '.join(cycle.child_task_ids.values())}")
    return cycle


class _Stop(Exception):
    pass


def _child_payload(deps, run, bundle, auth, cycle: ResearchCycle, t: PlannedTask) -> dict:
    from research_autopilot.research.models import ConditionBound
    scope = auth.scope or {}
    invariants = [i.get("value") for i in scope.get("invariants", []) if i.get("value")]
    params = [f"{k}: {', '.join(v)}" for k, v in t.parameters.items()]
    research_scope = [f"研究目的：{(scope.get('objective') or {}).get('value', '')}",
                      f"主指標：{(scope.get('primary_metric') or {}).get('value', '')}",
                      *[f"承認済み条件：{rw._cond(ConditionBound.model_validate(c))}" for c in scope.get("conditions", [])]]
    cycle_plan = [f"{x.key} {x.kind}: {x.title}"
                  + (f"（パラメータ {'; '.join(f'{k}=' + ', '.join(v) for k, v in x.parameters.items())}）" if x.parameters else "")
                  + (f"（依存 {', '.join(x.depends_on)}）" if x.depends_on else "")
                  + ("  ← このタスク" if x.key == t.key else "") for x in cycle.tasks]
    sections = [("目的", [t.objective, f"研究目的 {run.task_id}（{bundle.spec.title}）の研究サイクル {cycle.number} のタスク。"]),
                ("Specification", [t.specification, *(["パラメータ：" + "; ".join(params)] if params else [])]),
                ("Scientific invariants", invariants),
                ("研究スコープ（承認済み・変更不可）", research_scope),
                ("このサイクルの計画", cycle_plan),
                ("Acceptance criteria", t.acceptance_criteria or ["成果物が検証済みで、主指標の算出に使えること"]),
                ("Repository", [t.repository or ""]),
                ("Allowed files", t.file_scope),
                ("Inputs", t.consumes or []),
                ("Non-goals", ["研究目的・主指標・対象・科学的不変条件・承認済み条件範囲を変更しない"])]
    blocks = []
    for title, lines in sections:
        lines = [x for x in lines if x]
        if not lines:
            continue
        blocks.append(heading(title))
        if title in ("目的", "Specification", "Repository"):
            blocks += [paragraph(x[:1900]) for x in lines]
        else:
            blocks += [bullet(x[:1900]) for x in lines]
    prefix = next((p + " " for p in ("[TEST]", "[E2E]") if bundle.spec.title.startswith(p)), "")
    title = (prefix + f"[RA] {t.title}")[:150]
    return {"title": title, "blocks": blocks, "pending": False, "category": CATEGORY.get(t.kind),
            "parent_page_id": bundle.page.page_id, "linear_state": "Todo", "labels": [deps.plan.settings.scope_label],
            "description": f"Research cycle {cycle.number} of {run.task_id} (authorization v{auth.version}). Task {t.key}.",
            "origin": "RA", "approval_status": "Not Required",
            "root_task_id": run.task_id, "parent_task_id": run.task_id, "authorization_id": auth.authorization_id,
            "chain_depth": 0, "research_cycle_id": cycle.cycle_id}


# ---------------------------------------------------------------- results / evaluation


def results_for(deps, cycle: ResearchCycle) -> list[dict]:
    """Validated outputs of a cycle's tasks (metrics, small output files, analysis summaries, failures)."""
    from research_autopilot.autopilot.engine import _runs
    out = []
    by_task = {r.task_id: r for r in _runs(deps) if r.research_cycle_id == cycle.cycle_id}
    for key, task_id in cycle.child_task_ids.items():
        run = by_task.get(task_id)
        t = next((x for x in cycle.tasks if x.key == key), None)
        base = {"cycle": cycle.number, "task": task_id, "key": key, "title": t.title if t else key,
                "parameters": t.parameters if t else {}}
        if run is None:
            out.append({**base, "ref": f"{task_id}:missing", "validated": False, "data": {"status": "not started"}})
            continue
        if run.stage is not WorkflowStage.DONE:
            out.append({**base, "ref": f"{task_id}:{run.stage.value}", "validated": False,
                        "data": {"status": run.stage.value, "reason": (run.blocked_reason or "")[:500]}})
        for eid in run.experiment_run_ids[-1:]:
            m = deps.store.get_experiment(eid)
            if m is None:
                continue
            for j in m.jobs:
                if not j.validation:
                    continue
                files = {}
                for a in (j.collect.artifacts if j.collect else [])[:6]:
                    p = Path(a.stored_path)
                    if p.suffix in (".json", ".csv", ".txt") and p.is_file() and p.stat().st_size < 6000:
                        files[a.path] = p.read_text(errors="replace")[:3000]
                    else:
                        files[a.path] = f"<{a.size_bytes} bytes, sha256 {a.sha256[:12]}, {a.stored_path}>"
                out.append({**base, "ref": f"{task_id}:{eid}:{j.experiment_id}:{j.attempt}",
                            "validated": j.validation.status == "pass",
                            "data": {"metrics": j.validation.metrics, "outputs": files,
                                     "validation_failure": (j.validation.failure_reason or "")[:600] or None,
                                     "environment": m.environment.get(j.experiment_id, {}),
                                     "commit": j.commit[:12],
                                     "analysis": (m.analysis.summary[:1500] if m.analysis else None)}})
    return out


def _used_values(cs: list[ResearchCycle]) -> dict[str, set[str]]:
    from research_autopilot.research.planner import _canon
    used: dict[str, set[str]] = {}
    for c in cs:
        for t in c.tasks:
            for k, v in t.parameters.items():
                used.setdefault(k, set()).update(_canon(x) for x in v)
    return used


def _evaluate(deps, run: WorkflowRun, bundle, auth: ResearchAuthorization, cs: list[ResearchCycle]) -> None:
    from research_autopilot.research.models import ResearchScope
    store = deps.store
    cur = cs[-1]
    cur.status = CycleStatus.EVALUATING
    save_cycle(store, cur)
    cur.results = results_for(deps, cur)
    all_results = [r for c in cs[:-1] for r in c.results] + cur.results
    scope = ResearchScope.model_validate(auth.scope)
    plan = rw.get_plan(store, auth.plan_id)
    u = usage(deps, run.task_id)
    used_values = _used_values(cs)
    limits = {"cycle": cur.number, "max_cycles": scope.autonomy.max_cycles,
              "max_new_tasks_per_cycle": scope.autonomy.max_new_tasks_per_cycle,
              "max_retries_per_task": scope.autonomy.max_retries_per_task,
              "used_parameter_values": {k: sorted(v) for k, v in used_values.items()},
              "max_counts": {c.applies_to: c.count for c in scope.conditions if c.kind == "max_count"},
              "budget_used": {"cpu_hours": round(u.cpu_hours, 4), "api_usd": round(u.api_usd, 4)},
              "budget": scope.budget.model_dump(include={"cpu_hours", "api_usd"})}
    repos = {rid: str(getattr(cfg, "path", "")).rsplit("/", 1)[-1] for rid, cfg in deps.impl.registry.items()}
    llm = deps.research.llm_factory()
    resume = (cur.budget_after or {}).get("human_resume")
    d = evaluator_mod.evaluate(llm, scope=scope, plan=plan, cycles=cs, results=all_results, limits=limits,
                               repositories=repos, human_resume=resume)
    problems = evaluator_mod.gate(d, scope=scope, cycles=cs, results=all_results, registry=deps.impl.registry,
                                  used_values=used_values, used_cpu=u.cpu_hours, used_api=u.api_usd)
    if problems and d.action in ("continue", "complete"):
        d = evaluator_mod.evaluate(llm, scope=scope, plan=plan, cycles=cs, results=all_results, limits=limits,
                                   repositories=repos, feedback=problems, human_resume=resume)
        problems = evaluator_mod.gate(d, scope=scope, cycles=cs, results=all_results, registry=deps.impl.registry,
                                      used_values=used_values, used_cpu=u.cpu_hours, used_api=u.api_usd)
    run.research_cost_usd = round(run.research_cost_usd + getattr(llm, "spent", 0.0), 6)
    cur.cost_usd = round(getattr(llm, "spent", 0.0), 6)
    d.gate_problems = problems
    if problems and d.action == "continue":
        if any(evaluator_mod.is_scope_violation(p) for p in problems) and (d.proposed_conditions or d.proposed_budget):
            d.action = "scope_change"
            d.scope_change = d.scope_change or problems
        else:
            d.action = "stop"
            d.conclusion = d.conclusion or "the next step would leave the approved scope or limits: " + "; ".join(problems)
    elif problems and d.action == "complete":
        d.action = "stop"
        d.conclusion = (d.conclusion + " " if d.conclusion else "") + "Success is not established: " + "; ".join(problems)
    resumes = [c.at for c in controls(store, run.task_id) if c.action == "resume"]
    since = max(resumes) if resumes else None
    # Only cycles of the current scope that started after the last human Resume count towards "no progress":
    # a Resume is the human's decision to continue despite the earlier stalls.
    same_auth = [c for c in cs[:-1] if c.authorization_id == auth.authorization_id
                 and (since is None or c.started_at > since)]
    if resume and d.action == "continue":
        same_auth = []
    stalled = [c for c in same_auth if c.decision and not c.decision.progress][-(scope.autonomy.no_progress_cycles - 1):] \
        if scope.autonomy.no_progress_cycles > 1 else []
    if d.action == "continue" and not d.progress and len(stalled) >= scope.autonomy.no_progress_cycles - 1:
        d.action, d.conclusion = "stop", f"{scope.autonomy.no_progress_cycles} cycles without new information"
    cur.decision = d
    cur.status = CycleStatus.COMPLETED
    cur.finished_at = utcnow()
    u2 = usage(deps, run.task_id)
    cur.budget_after = {"cpu_hours": round(u2.cpu_hours, 4), "api_usd": round(u2.api_usd + cur.cost_usd, 4),
                        **({"human_resume": resume} if resume else {})}
    save_cycle(store, cur)
    run.event("research_evaluation", f"cycle {cur.number}: {d.action} ({d.rationale[:200]})")
    _milestone(deps, run.task_id, f"cycle-{cur.cycle_id}",
               [f"Cycle {cur.number} evaluated: {d.action}", d.rationale[:400],
                f"Results: {sum(1 for r in cur.results if r['validated'])}/{len(cur.results)} validated",
                f"Budget used: {cur.budget_after.get('cpu_hours')} CPU-h, ${cur.budget_after.get('api_usd')}",
                *([f"Next: {len(d.new_tasks)} task(s): " + ", ".join(t.title for t in d.new_tasks)[:300]]
                  if d.action == "continue" else []),
                f"Notion: {bundle.page.url if bundle.page else ''}"])
    if d.action == "continue":
        try:
            _start_cycle(deps, run, bundle, auth, cur.number + 1, d.new_tasks, cur.plan_id)
        except _Stop:
            return
    elif d.action == "scope_change":
        _propose_scope_change(deps, run, bundle, auth, cur, d)
    else:
        _finish(deps, run, bundle, auth, "completed" if d.action == "complete" else "stopped",
                d.conclusion or d.rationale, decision=d)


def _propose_scope_change(deps, run, bundle, auth: ResearchAuthorization, cur: ResearchCycle,
                          d: EvaluationDecision) -> None:
    from research_autopilot.research.models import ResearchScope
    approved = rw.get_plan(deps.store, auth.plan_id)
    scope = ResearchScope.model_validate(auth.scope)
    changes = []
    for c in d.proposed_conditions:
        old = next((x for x in scope.conditions if x.name == c.name and x.kind == c.kind), None)
        c = c.model_copy(update={"status": "stated", "quote": f"proposed scope change (cycle {cur.number})",
                                 "note": "becomes part of the scope only through this approval"})
        changes.append(f"condition {c.name}: {rw._cond(old) if old else 'new'} -> {rw._cond(c)}")
        scope.conditions = [x for x in scope.conditions if not (x.name == c.name and x.kind == c.kind)] + [c]
    if d.proposed_budget is not None:
        b = d.proposed_budget
        changes.append(f"budget: {scope.budget.cpu_hours} CPU-h / ${scope.budget.api_usd} -> {b.cpu_hours} CPU-h / ${b.api_usd}")
        scope.budget = b.model_copy(update={"status": "stated", "quote": f"proposed scope change (cycle {cur.number})"})
    plan = ResearchPlan(task_id=run.task_id, notion_page_id=bundle.page.page_id if bundle.page else None,
                        version=(rw.latest_plan(deps.store, run.task_id).version + 1), kind="scope_change",
                        spec_hash=approved.spec_hash, scope=scope, evidence=approved.evidence,
                        rationale=d.rationale, rationale_evidence=d.evidence, tasks=d.new_tasks,
                        replanning_rules=approved.replanning_rules, assumptions=approved.assumptions,
                        previous_plan_id=approved.plan_id, change_summary=[*changes, *d.scope_change],
                        models=approved.models)
    plan.estimate_cpu_hours = round(sum(t.estimate_cpu_hours for t in plan.tasks), 4)
    plan.content_hash = plan.compute_hash()
    rw._record_page(deps, plan)
    rw.save_plan(deps.store, plan)
    req = rw.request_approval(deps, bundle, plan)
    plan.status, plan.approval_request_id = PlanStatus.PENDING_APPROVAL, req.request_id
    rw.save_plan(deps.store, plan)
    rw.project(deps, plan, "Pending")
    cur.next_plan_id = plan.plan_id
    save_cycle(deps.store, cur)
    run.stage, run.waiting_for, run.resume_stage = WorkflowStage.AWAITING_APPROVAL, req.request_id, WorkflowStage.RESEARCHING.value
    if req.request_id not in run.approval_request_ids:
        run.approval_request_ids.append(req.request_id)
    run.event("scope_change", f"scope change v{plan.version} proposed: {'; '.join(changes)[:300]}")


# ---------------------------------------------------------------- completion / stop


def _finish(deps, run: WorkflowRun, bundle, auth: ResearchAuthorization | None, outcome: str, reason: str, *,
            decision: EvaluationDecision | None = None, budget: bool = False) -> None:
    from research_autopilot.autopilot.engine import _notify
    from research_autopilot.notifications.events import EventKind
    cs = cycles(deps.store, run.task_id)
    report = report_record(deps, run, bundle, auth, cs, outcome, reason, decision)
    path = save_report(report)
    try:
        catalog_report(deps, path)
    except Exception:
        pass
    note = deps.outbox.enqueue(OutboxItem(key=f"research-notes:{run.task_id}:{run.run_id}", kind="notes", payload={
        "title": f"[RA] {bundle.spec.title} — research results", "blocks": report_blocks(report)}))
    note = deps.outbox.process(note) if note.state is not OutboxState.DONE else note
    note_url = deps.notion_writer.page_url(note.notion_page_id) if note.notion_page_id else "(Notes write pending)"
    run.notes_page_id = note.notion_page_id
    issue = deps.linear.get_issue(run.task_id)
    if outcome == "completed":
        deps.ops.set_state(issue, "Done", f"research completed ({len(cs)} cycle(s))")
        run.stage = WorkflowStage.DONE
        run.event("research_completed", reason[:300])
        _milestone(deps, run.task_id, f"done:{run.run_id}",
                   [f"Research completed after {len(cs)} cycle(s)", reason[:600], f"Notes: {note_url}",
                    f"Task: {bundle.page.url if bundle.page else ''}"], kind=EventKind.TASK_COMPLETED, buttons=None)
    else:
        run.stage = WorkflowStage.BLOCKED
        run.blocked_reason = f"research stopped: {reason}"[:900]
        run.event("research_stopped", reason[:300])
        _milestone(deps, run.task_id, f"stopped:{run.run_id}",
                   [f"Research stopped after {len(cs)} cycle(s): {reason[:500]}", f"Notes: {note_url}",
                    f"Task: {bundle.page.url if bundle.page else ''}"],
                   kind=EventKind.BUDGET_EXCEEDED if budget else EventKind.UNRECOVERABLE_ERROR, buttons="resume")


def report_record(deps, run, bundle, auth, cs: list[ResearchCycle], outcome: str, reason: str,
                  decision: EvaluationDecision | None) -> dict:
    plans = rw.plans(deps.store, run.task_id)
    u = usage(deps, run.task_id)
    return {"schema": "1.0", "report_id": f"rr-{run.task_id}-{run.run_id[:8]}", "task_id": run.task_id,
            "title": bundle.spec.title, "outcome": outcome, "reason": reason, "created_at": utcnow().isoformat(),
            "objective": (auth.scope or {}).get("objective", {}).get("value") if auth else None,
            "primary_metric": (auth.scope or {}).get("primary_metric", {}).get("value") if auth else None,
            "authorization": {"id": auth.authorization_id, "version": auth.version, "content_hash": auth.content_hash,
                              "approved_by": auth.approved_by, "approved_at": auth.approved_at.isoformat()} if auth else None,
            "plans": [{"version": p.version, "kind": p.kind, "status": p.status.value, "hash": p.content_hash,
                       "url": p.plan_page_url} for p in plans],
            "cycles": [{"number": c.number, "tasks": c.child_task_ids, "decision": c.decision.model_dump(mode="json")
                        if c.decision else None, "budget_before": c.budget_before, "budget_after": c.budget_after}
                       for c in cs],
            "results": [r for c in cs for r in c.results],
            "criteria": decision.criteria if decision else [],
            "limitations": decision.limitations if decision else [],
            "budget_used": {"cpu_hours": round(u.cpu_hours, 4), "api_usd": round(u.api_usd, 4)}}


def research_store() -> Path:
    import os
    return Path(os.environ.get("RA_RESEARCH_DIR") or Path.home() / ".research-autopilot" / "research")


def save_report(report: dict) -> Path:
    d = research_store() / "reports"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{report['report_id']}.json"
    p.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    return p


REPORT_ADAPTER = {
    "name": "ra-research", "version": "1.0.0",
    "description": "Research Autopilot research reports (Phase 10): outcome, cycles, results and provenance.",
    "artifacts": [{"rule": "report", "kind": "research-report", "pattern": "reports/{report_id}.json",
                   "fields": {"doc_id": "/report_id", "task_id": "/task_id", "outcome": "/outcome",
                              "created_at": "/created_at", "schema": "/schema"},
                   "identity": ["report_id"], "metadata": ["task_id", "outcome", "created_at"],
                   "checks": [{"type": "equals_field", "field": "doc_id", "other": "report_id"},
                              {"type": "equals", "field": "schema", "value": "1.0"}]}]}


def catalog_report(deps, path: Path) -> None:
    from research_autopilot.catalog import adapters, service
    from research_autopilot.catalog.models import AdapterKind, AdapterOrigin, AdapterState
    from research_autopilot.implementation.models import RepoConfig
    ctx = getattr(deps.plan, "catalog", None)
    if ctx is None:
        return
    ctx.registry.setdefault("ra-research", RepoConfig(id="ra-research", path=str(research_store())))
    rec = adapters.register(ctx.store, kind=AdapterKind.DECLARATIVE, origin=AdapterOrigin.MANUAL, actor="ra-builtin",
                            definition=REPORT_ADAPTER)
    if rec.state is not AdapterState.ACTIVE:
        rec = adapters.validate(ctx.store, rec.adapter_id, repository_id="ra-research", root=research_store(),
                                actor="ra-builtin")
        if rec.state is AdapterState.VALIDATED:
            adapters.activate(ctx.store, rec.adapter_id, actor="ra-builtin")
    adapters.bind(ctx.store, "ra-research", ["ra-research"], actor="ra-builtin", registry=ctx.registry)
    service.scan(ctx, "ra-research", actor="ra-builtin")


def report_blocks(r: dict) -> list[dict]:
    rows = []
    for x in r["results"]:
        data = x.get("data", {})
        rows.append([f"C{x['cycle']} {x['task']}", x["title"][:60],
                     "; ".join(f"{k}={','.join(v)}" for k, v in (x.get("parameters") or {}).items())[:120] or "-",
                     json.dumps(data.get("metrics", data.get("status", "")), ensure_ascii=False)[:200],
                     "yes" if x.get("validated") else "no"])
    blocks = [heading("結論" if r["outcome"] == "completed" else "停止理由"), paragraph(r["reason"][:1900] or "-"),
              paragraph(f"研究目的：{r.get('objective') or '-'}"), paragraph(f"主指標：{r.get('primary_metric') or '-'}"),
              heading("成功判定"),
              *([bullet(f"{c.get('criterion')}: {c.get('status')} ({', '.join(c.get('evidence') or [])})")
                for c in r["criteria"]] or [paragraph("（判定なし）")]),
              heading("限界・未確認事項"), *([bullet(x) for x in r["limitations"]] or [paragraph("-")]),
              heading("結果"), table(["task", "title", "parameters", "metrics / status", "validated"], rows or [["-"] * 5]),
              heading("研究サイクルと判断")]
    for c in r["cycles"]:
        d = c.get("decision") or {}
        blocks.append(bullet(f"cycle {c['number']}: {d.get('action', '-')} — {str(d.get('rationale', ''))[:400]} "
                             f"(tasks {', '.join(c['tasks'].values())}; budget {c.get('budget_after')})"))
    a = r.get("authorization") or {}
    blocks += [heading("再現情報・承認"),
               bullet(f"authorization v{a.get('version')} {str(a.get('content_hash'))[:12]} approved by "
                      f"{a.get('approved_by')} at {a.get('approved_at')}"),
               *[bullet(f"plan v{p['version']} ({p['kind']}, {p['status']}, {str(p['hash'])[:12]}) {p.get('url') or ''}")
                 for p in r["plans"]],
               bullet(f"budget used: {r['budget_used']}"), bullet(f"report: {r['report_id']} (Catalog ra-research)")]
    for x in r["results"]:
        env = (x.get("data") or {}).get("environment")
        if env:
            blocks.append(bullet(f"{x['task']}: environment {json.dumps(env, ensure_ascii=False)[:300]}"))
    return blocks


def _ev():
    from research_autopilot.notifications.events import EventKind
    return EventKind


def _milestone(deps, task_id: str, key: str, details: list[str], *, kind=None, buttons: str | None = "stop") -> None:
    """Research milestones. ``buttons``: "stop" on cycle evaluations (results), "resume" on stops / anomalies
    (errors), None elsewhere -- the logs channel never carries controls."""
    from research_autopilot.notifications import notify_once
    from research_autopilot.notifications.events import EventKind, NotificationEvent
    try:
        notify_once(deps.store, NotificationEvent(kind=kind or EventKind.ANALYSIS_COMPLETED, flow_name="research",
                                                  flow_run_name=task_id, details=[x for x in details if x][:12],
                                                  dedupe_key=f"research:{task_id}:{key}",
                                                  control_task_id=task_id if buttons else None,
                                                  control_actions=[buttons] if buttons else []), deps.notifier)
    except Exception:
        pass
