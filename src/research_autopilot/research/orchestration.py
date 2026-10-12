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


def research_run(store, task_id: str) -> WorkflowRun | None:
    """The research objective's own (top-level) WorkflowRun: the newest one."""
    runs = [WorkflowRun.model_validate(r) for r in store.find(db.workflow_runs, task_id=task_id)]
    runs = [r for r in runs if not r.parent_task_id and not r.chain_depth]
    return max(runs, key=lambda r: r.created_at) if runs else None


def _effective(cs: list[ResearchControl], run: WorkflowRun | None) -> list[ResearchControl]:
    """Stop / resume controls that act on ``run``: not ignored, not created before the run started, not applied
    to another run (a late Stop from an old message never pauses a later run)."""
    cs = [c for c in cs if c.action in ("stop", "resume") and not c.ignored]
    if run is not None:
        cs = [c for c in cs if c.at >= run.created_at and c.run_id in (None, run.run_id)]
    return cs


def paused(store, task_id: str, run: WorkflowRun | None = None) -> bool:
    cs = _effective(controls(store, task_id), run if run is not None else research_run(store, task_id))
    return bool(cs) and cs[-1].action == "stop"


def _last_control(store, task_id: str, run: WorkflowRun | None) -> ResearchControl | None:
    cs = _effective(controls(store, task_id), run)
    return cs[-1] if cs else None


def _mark_applied(store, c: ResearchControl, note: str, run: WorkflowRun | None = None, *,
                  ignored: bool = False) -> None:
    c.applied = True
    c.ignored = c.ignored or ignored
    if run is not None:
        c.run_id, c.execution_run_id = run.run_id, run.execution_run_id
    store.put(db.research_controls, c.control_id, {**c.model_dump(mode="json"), "note": note[:300]}, task_id=c.task_id)


def _request_by_key(store, task_id: str, key: str) -> ExecutionRequest | None:
    raw = next((x for x in store.find(db.execution_requests, linear_issue_id=task_id) if x.get("request_key") == key),
               None)
    return ExecutionRequest.model_validate(raw) if raw else None


def _keyed_execution(deps, run_task_id: str, page_id: str, key: str, source: str, spec_hash: str,
                     metadata: dict) -> tuple[ExecutionRequest, ExecutionRun]:
    """An idempotent (request, execution run) pair: a request already stored under ``key`` (a crash after its insert,
    or the same control processed twice) is adopted with its execution run instead of being dropped."""
    req = ExecutionRequest(notion_task_id=page_id, linear_issue_id=run_task_id, source=source,
                           specification_hash=spec_hash, status=ExecutionStatus.ACCEPTED, request_key=key)
    if not deps.store.insert_new(db.execution_requests, req.request_id, req.model_dump(mode="json"),
                                 notion_task_id=page_id, linear_issue_id=run_task_id, status=req.status.value,
                                 dedupe_key=key):
        req = _request_by_key(deps.store, run_task_id, key)
        if req is None:
            raise RuntimeError(f"execution request {key} exists but cannot be read")
        raw = next((x for x in deps.store.find(db.execution_runs, request_id=req.request_id)), None)
        if raw:
            return req, ExecutionRun.model_validate(raw)
    er = ExecutionRun(request_id=req.request_id, task_id=run_task_id, notion_task_id=page_id, retry_metadata=metadata)
    deps.store.insert_new(db.execution_runs, er.execution_run_id, er.model_dump(mode="json"), request_id=req.request_id,
                          task_id=run_task_id, status=er.status.value)
    return req, er


def _restart_blocked(deps, run: WorkflowRun, control: ResearchControl, *, replan: bool = False) -> None:
    """Resume of a blocked research run = a new explicit start (the blocked execution record stays terminal).

    ``replan``: the spec was edited (automatic re-plan) -- the stopped cycle is not re-evaluated; the edited spec
    makes the authorization stale, so a new plan version is drafted for approval."""
    page = deps.plan.manager.load(run.task_id).page
    req, er = _keyed_execution(deps, run.task_id, page.page_id, f"resume:{control.control_id}", "research_resume",
                               "resume", {"resumed_by": control.actor,
                                          "previous_execution_run_id": run.execution_run_id})
    if run.execution_run_id != er.execution_run_id:
        run.execution_run_id, run.execution_request_id = er.execution_run_id, req.request_id
    run.stage, run.blocked_reason, run.waiting_for = WorkflowStage.DISCOVERED, None, None
    run.attempts = 0
    run.event("resumed", f"blocked research restarted by {control.actor}: authorization and plan are re-checked")
    if replan:
        return
    auth = rw.research_authorization(deps.store, run.task_id)
    cs = cycles(deps.store, run.task_id)
    if auth is not None and cs and cs[-1].authorization_id == auth.authorization_id and cs[-1].decision \
            and cs[-1].decision.action in ("stop", "scope_change") and cs[-1].status is CycleStatus.COMPLETED:
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
    # only retries in which some child was planned / worked on count against max_retries_per_task: a retry refused
    # by a start gate before any work (e.g. a stale Linear state) or cut off by a backend usage limit did not try
    # the task again (user decisions)
    retries_used = sum(1 for p in previous if p.parent_run_id != _first_parent(previous).parent_run_id
                       and _attempted(deps, p))
    max_retries = int(((auth.scope or {}).get("autonomy") or {}).get("max_retries_per_task", 2))
    if retries_used >= max_retries:
        return False

    dag = TaskDAG.model_validate(deps.store.get(db.task_dags, old.dag_id))
    attempt = retries_used + 1
    carry = {t: c for t in failed if (c := _carry_over(deps, old, t))}
    page = deps.plan.manager.load(run.task_id).page
    # keyed on the ParentRun sequence, not the counted attempt: an uncounted retry must not collide with the next one
    key = f"research-cycle:{cycle.cycle_id}:retry:{attempt}" if len(previous) == attempt else \
        f"research-cycle:{cycle.cycle_id}:parent:{len(previous) + 1}"
    # a key collision (the same Resume processed again after a crash) adopts the stored request / run / parent
    req, er = _keyed_execution(deps, run.task_id, page.page_id, key, "research_cycle_retry",
                               dag.parent_specification_hash,
                               {"research_cycle_id": cycle.cycle_id, "retry": attempt,
                                "previous_parent_run_id": old.parent_run_id})
    adopted = next((ParentRun.model_validate(x) for x in deps.store.find(
        db.parent_runs, parent_execution_run_id=er.execution_run_id)), None)
    for task_id in failed:
        # a blocked/failed child keeps its Linear "In Progress"; its execution is terminal, so put it back to Todo
        # or the Phase 2 start gate refuses the retry as "already In Progress" (a continued child skips Phase 2)
        issue = deps.linear.get_issue(task_id)
        if issue.state_type == "started" and task_id not in carry:
            deps.ops.set_state(issue, "Todo", f"research cycle {cycle.number} retry {attempt}")
    if adopted is not None:
        parent = adopted
    else:
        parent = ParentRun(parent_task_id=run.task_id, parent_notion_page_id=page.page_id,
                           parent_execution_run_id=er.execution_run_id, dag_id=dag.dag_id, dag_hash=dag.dag_hash,
                           research_cycle_id=cycle.cycle_id, carry_over=carry,
                           completion_reasons=[f"retry {attempt} after: {', '.join(failed)}"
                                               + "".join(f"; {t} continues at {c['stage']}" for t, c in carry.items())])
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
    run.stage = WorkflowStage.RESEARCHING
    schedule_ready(deps, parent, research_run=run)
    run.event("resumed", f"cycle {cycle.number} child DAG retry {attempt}/{max_retries}: {', '.join(failed)}")
    return True


def _carry_over(deps, old: ParentRun, task_id: str, any_experiment_block: bool = False) -> dict | None:
    """Where can a failed child continue? A completed implementation whose experiment failed on environment
    setup re-runs Phase 4 only; a Phase 3 run cut off by a non-task limit (usage / rate limit, process or transient
    failure, wall time) continues in its worktree with the same plan. Anything else is planned again."""
    from research_autopilot.experiments.models import ExperimentState
    from research_autopilot.experiments.orchestrator import ENVIRONMENT_FAILURE
    from research_autopilot.implementation.models import RunState
    from research_autopilot.implementation.orchestrator import continuable
    raw = deps.store.get(db.workflow_runs, old.child_workflow_run_ids.get(task_id, ""))
    if not raw or not raw.get("plan_run_id") or not raw.get("phase3_run_ids"):
        return None
    base = {"plan_run_id": raw["plan_run_id"], "plan_id": raw.get("plan_id")}
    try:
        manifests = [m for m in (deps.store.get_implementation(i) for i in reversed(raw["phase3_run_ids"])) if m]
        done = next((m for m in manifests if m.state is RunState.COMPLETED and m.implementation_commit), None)
        if done and raw.get("experiment_run_ids"):
            e = deps.store.get_experiment(raw["experiment_run_ids"][-1])
            # (a human rerun of a stopped parent may re-run Phase 4 once whatever stopped the experiment: the
            # environment, the data or the validator may have been fixed; a second stop is planned again)
            if e and e.state is ExperimentState.BLOCKED \
                    and (any_experiment_block or ENVIRONMENT_FAILURE in (e.block_reason or "")) \
                    and not _phase4_rerun_used(deps, old, task_id):
                return {**base, "stage": "experimenting", "phase3_run_id": done.phase3_run_id,
                        "commit": done.implementation_commit}
        if manifests and not raw.get("experiment_run_ids") and continuable(manifests[0]):
            return {**base, "stage": "implementing", "phase3_run_id": manifests[0].phase3_run_id}
    except Exception:  # an unreadable record never blocks the retry: plan again
        return None
    return None


def _phase4_rerun_used(deps, old: ParentRun, task_id: str) -> bool:
    """At most one Phase-4-only rerun per task and cycle: a second environment failure is planned again."""
    parents = [ParentRun.model_validate(x) for x in deps.store.find(db.parent_runs)
               if old.research_cycle_id and x.get("research_cycle_id") == old.research_cycle_id] or [old]
    return any((p.carry_over.get(task_id) or {}).get("stage") == "experimenting" for p in parents)


def _first_parent(parents: list[ParentRun]) -> ParentRun:
    return min(parents, key=lambda p: p.created_at)


_USAGE_LIMITS = {"quota_exhausted", "rate_limited"}


def _attempted(deps, parent: ParentRun) -> bool:
    """Was any child of this ParentRun actually worked on? Phase 2 planning ran (a plan id, even if the plan was
    then blocked) -- a start-gate refusal never gets one -- and the child was not cut off by a coding backend's
    usage limit (an interruption by the provider, not a failed try of the task; user decision)."""
    for run_id in parent.child_workflow_run_ids.values():
        raw = deps.store.get(db.workflow_runs, run_id)
        if not raw or not (raw.get("plan_id") or raw.get("phase3_run_ids") or raw.get("experiment_run_ids")):
            continue
        if raw.get("stage") == WorkflowStage.BLOCKED.value and raw.get("phase3_run_ids") \
                and _usage_limited(deps, raw["phase3_run_ids"][-1]):
            continue
        return True
    return False


def _usage_limited(deps, phase3_run_id: str) -> bool:
    get = getattr(deps.store, "get_implementation", None)
    try:
        m = get(phase3_run_id) if get else None
    except Exception:
        return False
    return bool(m and m.failure_classification in _USAGE_LIMITS)


def request_control(store, task_id: str, action: str, actor: str, channel: str) -> ResearchControl:
    c = ResearchControl(task_id=task_id, action=action, actor=actor, channel=channel)
    store.put(db.research_controls, c.control_id, c.model_dump(mode="json"), task_id=task_id)
    return c


# ---------------------------------------------------------------- poll hooks


def tick(deps) -> dict:
    """Projection retries, stop / resume / cancel requests, automatic re-planning after a spec edit and re-planning
    of stale scope requests.  One run's failure never aborts the poll for the others."""
    from research_autopilot.autopilot.engine import _runs
    out = {"projections": rw.retry_projections(deps), "controls": [], "replanned": [], "errors": []}
    runs = [r for r in _runs(deps) if r.stage not in (WorkflowStage.DONE, WorkflowStage.CANCELLED)]
    for run in runs:
        if run.parent_task_id or run.chain_depth:
            continue
        try:
            _tick_run(deps, run, out)
        except Exception as exc:  # noqa: BLE001 - reported; the next poll retries this run
            out["errors"].append(f"{run.task_id}: {type(exc).__name__}: {str(exc)[:200]}")
    if not out["errors"]:
        del out["errors"]
    return out


def _tick_run(deps, run: WorkflowRun, out: dict) -> None:
    from research_autopilot.autopilot.engine import _save
    store = deps.store
    is_research = rw.latest_plan(store, run.task_id) is not None
    pending = [c for c in controls(store, run.task_id) if not c.applied]
    for c in [c for c in pending if c.at < run.created_at]:  # pressed on a message of an earlier run
        _mark_applied(store, c, "created before this research run started", run, ignored=True)
    pending = [c for c in pending if c.at >= run.created_at]
    cancel = next((c for c in pending if c.action == "cancel"), None)
    if cancel is not None and is_research:
        cancel_research(deps, run, cancel)
        for c in pending:
            _mark_applied(store, c, f"research cancelled by {cancel.actor}", run, ignored=c is not cancel)
        out["controls"].append(f"{run.task_id}: cancelled")
        return
    stopped = run.stage in (WorkflowStage.BLOCKED, WorkflowStage.DEAD_LETTER)
    acting = [c for c in pending if c.action in ("stop", "resume")]
    if acting and acting[-1].action == "resume" and stopped and is_research:
        _restart_blocked(deps, run, acting[-1])
        _save(deps, run)
        out["controls"].append(f"{run.task_id}: blocked research restarted")
    for c in pending:
        # a Stop on a stopped run has nothing to stop: it must not pause the run after its next Resume
        _mark_applied(store, c, f"seen at stage {run.stage.value}", run,
                      ignored=c.action == "cancel" or (c.action == "stop" and stopped))
    if run.stage is WorkflowStage.DEAD_LETTER:
        return
    if run.stage is WorkflowStage.BLOCKED and is_research and _auto_replan(deps, run):
        _save(deps, run)
        out["replanned"].append(run.task_id)
        return
    stop = paused(store, run.task_id, run)
    if stop and run.stage is WorkflowStage.RESEARCHING:
        run.stage, run.resume_stage = WorkflowStage.PAUSED, WorkflowStage.RESEARCHING.value
        run.event("paused", "stopped by the user: no new work starts; running jobs finish")
        _save(deps, run)
        last = _last_control(store, run.task_id, run)
        # one message per Stop (a constant key would swallow every later pause of the same task)
        _milestone(deps, run.task_id, f"paused:{last.control_id if last else run.execution_run_id}",
                   [f"Research paused by {last.actor if last else 'the user'}: no new work starts; running jobs "
                    "finish.", _page_line(deps, run.task_id)],
                   kind=_ev().UNRECOVERABLE_ERROR, buttons=["resume", "cancel"], needs_human=True,
                   steps=STEPS["paused"])
        out["controls"].append(f"{run.task_id}: paused")
    elif not stop and run.stage is WorkflowStage.PAUSED:
        run.stage, run.resume_stage = WorkflowStage.RESEARCHING, None
        run.event("resumed", "resumed by the user; authorization and budget are re-checked")
        _save(deps, run)
        last = _last_control(store, run.task_id, run)
        _milestone(deps, run.task_id, f"resumed:{last.control_id if last else run.execution_run_id}",
                   ["Research resumed; authorization and budget re-checked."],
                   kind=_ev().PHASE_TRANSITION, buttons=None)
        out["controls"].append(f"{run.task_id}: resumed")
    if replan_stale_run(deps, run):
        out["replanned"].append(run.task_id)


def replan_stale(deps) -> list[str]:
    """Research-scope requests that went stale / expired in this poll: draft the revised version now."""
    from research_autopilot.autopilot.engine import _runs
    done = []
    for run in _runs(deps):
        if run.parent_task_id or run.chain_depth:
            continue
        try:
            if replan_stale_run(deps, run):
                done.append(run.task_id)
        except Exception:  # noqa: BLE001 - the next poll's tick retries
            continue
    return done


def replan_stale_run(deps, run: WorkflowRun) -> bool:
    from research_autopilot.autopilot.engine import _save
    if run.stage not in (WorkflowStage.AWAITING_AUTHORIZATION, WorkflowStage.AWAITING_APPROVAL) or not run.waiting_for:
        return False
    raw = deps.store.get(db.approval_requests, run.waiting_for)
    if not (raw and raw.get("kind") == "research_scope" and raw.get("status") in (
            ApprovalStatus.STALE.value, ApprovalStatus.EXPIRED.value)):
        return False
    # an initial plan is drafted again; a scope change is decided by the research step (re-proposed or stopped)
    run.stage, run.waiting_for = WorkflowStage.DISCOVERED, None
    run.event("replan", f"scope request {raw['status']}: a new plan version is drafted")
    _save(deps, run)
    return True


def _auto_replan(deps, run: WorkflowRun) -> bool:
    """A research blocked on its plan (needs input / invalid) or stopped re-plans by itself once the Notion spec
    was edited: the spec hash must differ from the plan's and be the same on two consecutive polls (the human may
    still be typing).  Resume stays the fallback when the edit is not detected."""
    plan = rw.latest_plan(deps.store, run.task_id)
    if plan is None:
        return False
    if plan.status not in (PlanStatus.NEEDS_INPUT, PlanStatus.INVALID, PlanStatus.REJECTED) \
            and not (run.blocked_reason or "").startswith("research stopped"):
        return False
    from research_autopilot.research import scope as scope_mod
    spec = deps.plan.manager.load(run.task_id).spec
    if spec is None or not scope_mod.is_research_objective(spec):
        return False
    h = scope_mod.spec_hash(spec)
    raw = deps.store.get(db.research_plans, plan.plan_id) or {}
    if h == plan.spec_hash:
        return False
    if raw.get("spec_edit_seen") != h:  # first sighting: wait one poll for the edit to settle
        raw["spec_edit_seen"] = h
        deps.store.put(db.research_plans, plan.plan_id, raw, task_id=raw["task_id"], version=raw["version"],
                       status=raw["status"])
        return False
    control = request_control(deps.store, run.task_id, "resume", "auto:spec-edit", "auto")
    _restart_blocked(deps, run, control, replan=True)
    _mark_applied(deps.store, control, "Notion specification edited: re-planning", run)
    run.event("replan", "the Notion specification was edited: a new plan version is drafted")
    return True


def adopt_existing(deps, task_id: str, actor: str = "notion:run-autopilot") -> WorkflowRun | bool | None:
    """Run Autopilot on a research objective that already has a research run: never a second WorkflowRun.

    Returns the restarted run (it was blocked / dead-lettered), True when the existing run is still going (the
    request is only acknowledged), or None when there is nothing to adopt."""
    from research_autopilot.autopilot.engine import _save
    run = research_run(deps.store, task_id)
    if run is None or run.stage in (WorkflowStage.DONE, WorkflowStage.CANCELLED) \
            or rw.latest_plan(deps.store, task_id) is None:
        return None
    if run.stage not in (WorkflowStage.BLOCKED, WorkflowStage.DEAD_LETTER):
        return True
    control = request_control(deps.store, task_id, "resume", actor, "notion")
    _restart_blocked(deps, run, control)
    _mark_applied(deps.store, control, "Run Autopilot on a stopped research: restarted", run)
    _save(deps, run)
    return run


def cancel_research(deps, run: WorkflowRun, control: ResearchControl) -> None:
    """Verified human Cancel: the research run and its current cycle stop for good and Linear shows Canceled for the
    research objective and every unfinished child (the only path that may write Canceled; audited)."""
    from research_autopilot.autopilot.dag_runtime import _sync_parent_status
    from research_autopilot.autopilot.engine import TERMINAL, _runs
    store = deps.store
    reason = f"cancelled by {control.actor} ({control.channel})"
    cs = cycles(store, run.task_id)
    cycle_ids = {c.cycle_id for c in cs}
    for raw in store.find(db.parent_runs, parent_task_id=run.task_id):
        parent = ParentRun.model_validate(raw)
        if parent.status in (ExecutionStatus.ACCEPTED, ExecutionStatus.RUNNING, ExecutionStatus.AWAITING_APPROVAL):
            _sync_parent_status(deps, parent, ExecutionStatus.CANCELED, reason, terminal=True)
    children = [r for r in _runs(deps) if r.run_id != run.run_id
                and (r.research_cycle_id in cycle_ids or r.parent_task_id == run.task_id)]
    for child in children:
        if child.stage in TERMINAL:
            continue
        child.stage, child.waiting_for = WorkflowStage.CANCELLED, None
        child.event("cancelled", f"research {run.task_id} {reason}")
        _save_cancelled(deps, child)
    linear_done, refused = [], []
    for task_id in [run.task_id, *dict.fromkeys(t for c in cs for t in c.child_task_ids.values())]:
        try:
            issue = deps.linear.get_issue(task_id)
            if issue.state_type in ("completed", "canceled"):
                continue
            deps.ops.cancel_by_human(issue, control.actor, f"research {run.task_id} cancelled")
            linear_done.append(task_id)
        except Exception as exc:  # noqa: BLE001 - recorded; the run is cancelled regardless
            refused.append(f"{task_id}: {type(exc).__name__}: {str(exc)[:120]}")
    # a late Approve on a request of the cancelled research must create nothing: close what is still pending
    tasks = {run.task_id, *(t for c in cs for t in c.child_task_ids.values())}
    for req in deps.approvals.pending():
        if req.task_id in tasks:
            deps.approvals._close(req, ApprovalStatus.STALE, f"研究 {run.task_id} が打ち切られたため無効")
    run.stage, run.waiting_for, run.resume_stage = WorkflowStage.CANCELLED, None, None
    run.blocked_reason = f"research {reason}"
    run.event("cancelled", f"{reason}; Linear Canceled: {', '.join(linear_done) or '-'}"
                           + (f"; not written: {'; '.join(refused)}" if refused else ""))
    _save_cancelled(deps, run)
    _milestone(deps, run.task_id, f"cancelled:{control.control_id}",
               [f"Research cancelled by {control.actor}.",
                f"Linear Canceled: {', '.join(linear_done) or '-'}",
                *([f"Not written (do it by hand): {'; '.join(refused)}"] if refused else []),
                _page_line(deps, run.task_id)], kind=_ev().PHASE_TRANSITION, buttons=None)


def _save_cancelled(deps, run: WorkflowRun) -> None:
    """Save a cancelled run; an execution that already ended (Failed / Completed) stays as recorded."""
    from research_autopilot.autopilot.engine import _save
    rec = deps.store.get(db.execution_runs, run.execution_run_id) if run.execution_run_id else None
    if rec and rec.get("status") in (ExecutionStatus.FAILED.value, ExecutionStatus.COMPLETED.value,
                                     ExecutionStatus.CANCELED.value):
        run.updated_at = utcnow()
        deps.store.put(db.workflow_runs, run.run_id, run.model_dump(mode="json"), task_id=run.task_id,
                       stage=run.stage.value)
        return
    _save(deps, run)


def _page_line(deps, task_id: str) -> str:
    try:
        page = deps.plan.manager.load(task_id).page
        return f"Task: {page.url}" if page and page.url else ""
    except Exception:  # noqa: BLE001 - a link is optional
        return ""


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
        _finish(deps, run, bundle, auth, "stopped", "; ".join(problems), why="authorization")
        return
    run.authorization_id = auth.authorization_id
    if paused(store, run.task_id, run):
        run.stage, run.resume_stage = WorkflowStage.PAUSED, WorkflowStage.RESEARCHING.value
        return
    over = budget_problems(deps, run, auth)
    if over:
        _finish(deps, run, bundle, auth, "stopped", "; ".join(over), budget=True, why="budget")
        return
    cs = [c for c in cycles(store, run.task_id)]
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
    if cur.status is CycleStatus.RUNNING and not cur.parent_run_id:
        # saved before its child DAG was started (a crash / failed creation): finish starting it, never evaluate it
        _start_cycle(deps, run, bundle, auth, cur.number, cur.tasks, cur.plan_id)
        return
    if cur.status in (CycleStatus.RUNNING, CycleStatus.EVALUATING):
        parent = ParentRun.model_validate(store.get(db.parent_runs, cur.parent_run_id)) if cur.parent_run_id else None
        if parent is not None and parent.status not in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED,
                                                        ExecutionStatus.BLOCKED, ExecutionStatus.CANCELED):
            return  # children still running
        _evaluate(deps, run, bundle, auth, cs)
        return
    _act(deps, run, bundle, auth, plan, cs)


def _act(deps, run, bundle, auth: ResearchAuthorization, plan: ResearchPlan, cs: list[ResearchCycle]) -> None:
    """Carry out the recorded decision of the last (evaluated) cycle.  Re-entrant: a step that failed half-way
    (e.g. ``_finish`` raised after the decision was saved) is retried from here on the next poll."""
    cur = cs[-1]
    if plan is not None and plan.plan_id not in {c.plan_id for c in cs}:
        # a newly approved plan no cycle has run yet (scope change, or a re-approved initial plan after a stop /
        # a superseded scope change): it starts the next cycle, whatever the last decision was
        _start_cycle(deps, run, bundle, auth, cur.number + 1, plan.tasks, plan.plan_id)
        return
    d = cur.decision
    if d is None:
        return
    if d.action == "continue":
        _start_cycle(deps, run, bundle, auth, cur.number + 1, d.new_tasks, cur.plan_id)
    elif d.action in ("complete", "stop"):
        _finish(deps, run, bundle, auth, "completed" if d.action == "complete" else "stopped",
                d.conclusion or d.rationale, decision=d, why=d.stop_kind or "evaluation")
    elif d.action == "scope_change":
        _act_scope_change(deps, run, bundle, auth, cur, d)


def _act_scope_change(deps, run, bundle, auth: ResearchAuthorization, cur: ResearchCycle,
                      d: EvaluationDecision) -> None:
    store = deps.store
    nxt = rw.get_plan(store, cur.next_plan_id) if cur.next_plan_id else None
    if nxt is None:  # the proposal failed before it was linked to the cycle: propose (or adopt) it again
        _propose_scope_change(deps, run, bundle, auth, cur, d)
        return
    if nxt.status in (PlanStatus.PENDING_APPROVAL, PlanStatus.DRAFT):
        raw = store.get(db.approval_requests, nxt.approval_request_id) if nxt.approval_request_id else None
        status = (raw or {}).get("status")
        if nxt.status is PlanStatus.DRAFT or raw is None:
            _propose_scope_change(deps, run, bundle, auth, cur, d)  # adopts the drafted plan, requests approval
        elif status == ApprovalStatus.PENDING.value:
            _wait_for(run, nxt)
        else:
            _finish(deps, run, bundle, auth, "stopped", f"the proposed scope change v{nxt.version} {status} without "
                    "a decision; the research stays within the approved scope", why="scope_expired")
    elif nxt.status is PlanStatus.REVISION_REQUESTED:
        # redraft: the Evaluator decides again with the human's comment (a revised proposal or a step in scope)
        cur.status, cur.results, cur.next_plan_id = CycleStatus.EVALUATING, [], None
        cur.budget_after = {**cur.budget_after, "human_resume": {
            "actor": "approver", "at": utcnow().isoformat(),
            "note": f"the human requested a revision of the proposed scope change v{nxt.version}: "
                    f"{nxt.revision_comment or '(no comment)'}. Revise the proposal accordingly or continue inside "
                    "the approved scope.",
            "previous_decision": d.rationale[:600]}}
        save_cycle(store, cur)
        run.event("revision", f"scope change v{nxt.version}: revision requested; cycle {cur.number} is re-evaluated")
        _evaluate(deps, run, bundle, auth, cycles(store, run.task_id))
    elif nxt.status is PlanStatus.REJECTED:
        _finish(deps, run, bundle, auth, "stopped", f"the proposed scope change v{nxt.version} was rejected; the "
                f"research stays within the approved scope v{auth.version}", why="scope_rejected")
    else:  # superseded (or approved and superseded again): follow the newest version if it waits for a decision
        latest = rw.latest_plan(store, run.task_id)
        if latest is not None and latest.status is PlanStatus.PENDING_APPROVAL and latest.approval_request_id \
                and (store.get(db.approval_requests, latest.approval_request_id) or {}).get("status") \
                == ApprovalStatus.PENDING.value:
            _wait_for(run, latest)
        else:
            _finish(deps, run, bundle, auth, "stopped", f"the proposed scope change v{nxt.version} was not approved "
                    f"({nxt.status.value})", why="scope_rejected")


def _wait_for(run: WorkflowRun, plan: ResearchPlan) -> None:
    run.stage, run.waiting_for = WorkflowStage.AWAITING_APPROVAL, plan.approval_request_id
    run.resume_stage = WorkflowStage.RESEARCHING.value
    if plan.approval_request_id not in run.approval_request_ids:
        run.approval_request_ids.append(plan.approval_request_id)
    run.event("approval", f"waiting for the decision on plan v{plan.version}")


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
    all_cycles = cycles(store, run.task_id)
    # max_cycles bounds the cycles of the current authorization: a re-approved scope may run its own cycles
    in_scope = [c for c in all_cycles if c.authorization_id == auth.authorization_id and c.number < number]
    if len(in_scope) >= int(a.get("max_cycles", 5)):
        _finish(deps, run, bundle, auth, "stopped", f"the approved maximum of {a.get('max_cycles')} cycles is reached",
                why="max_cycles")
        raise _Stop()
    existing = [c for c in all_cycles if c.number == number]
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
    adopted = next((ParentRun.model_validate(x) for x in store.find(db.parent_runs, parent_task_id=run.task_id)
                    if x.get("research_cycle_id") == cycle.cycle_id), None)
    if adopted is not None:  # the child DAG was started but not yet linked (a crash in between)
        cycle.parent_run_id = adopted.parent_run_id
        save_cycle(store, cycle)
        schedule_ready(deps, adopted, research_run=run)
        run.event("research_cycle", f"cycle {number}: adopted its started child DAG {adopted.parent_run_id[:8]}")
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
    req, er = _keyed_execution(deps, run.task_id, page.page_id, f"research-cycle:{cycle.cycle_id}", "research_cycle",
                               dag.parent_specification_hash, {"research_cycle_id": cycle.cycle_id})
    old_dag = next((x for x in store.find(db.task_dags, parent_task_id=run.task_id)
                    if x.get("parent_execution_run_id") == er.execution_run_id), None)
    if old_dag is not None:  # an earlier attempt saved the DAG of this execution: keep it (unique per execution)
        dag = TaskDAG.model_validate(old_dag)
    dag.parent_execution_run_id = er.execution_run_id
    _save_dag(deps, dag)
    parent = ParentRun(parent_task_id=run.task_id, parent_notion_page_id=page.page_id,
                       parent_execution_run_id=er.execution_run_id, dag_id=dag.dag_id, dag_hash=dag.dag_hash,
                       research_cycle_id=cycle.cycle_id)
    _save_parent(deps, parent)
    _start_parent(deps, parent, er, None)
    cycle.parent_run_id = parent.parent_run_id
    save_cycle(store, cycle)
    schedule_ready(deps, parent, research_run=run)
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
    cur.next_plan_id = None  # a new decision replaces an earlier (revised / expired) scope-change proposal
    save_cycle(store, cur)
    cur.results = results_for(deps, cur)
    all_results = [r for c in cs[:-1] for r in c.results] + cur.results
    scope = ResearchScope.model_validate(auth.scope)
    plan = rw.get_plan(store, auth.plan_id)
    u = usage(deps, run.task_id)
    used_values = _used_values(cs)
    in_scope = [c for c in cs if c.authorization_id == auth.authorization_id]
    limits = {"cycle": cur.number, "cycles_in_this_scope": len(in_scope), "max_cycles": scope.autonomy.max_cycles,
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
                                  used_values=used_values, used_cpu=u.cpu_hours, used_api=u.api_usd,
                                  authorization_id=auth.authorization_id)
    if problems and d.action in ("continue", "complete"):
        d = evaluator_mod.evaluate(llm, scope=scope, plan=plan, cycles=cs, results=all_results, limits=limits,
                                   repositories=repos, feedback=problems, human_resume=resume)
        problems = evaluator_mod.gate(d, scope=scope, cycles=cs, results=all_results, registry=deps.impl.registry,
                                      used_values=used_values, used_cpu=u.cpu_hours, used_api=u.api_usd,
                                      authorization_id=auth.authorization_id)
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
            d.stop_kind = "max_cycles" if any("cycles is reached" in p for p in problems) else "limits"
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
        d.stop_kind = "no_progress"
    if d.action == "stop" and not d.stop_kind and any(not r.get("validated") and (r.get("data") or {}).get("status")
                                                      for r in cur.results):
        d.stop_kind = "failure"  # some task of the cycle did not finish (blocked / failed / not started)
    cur.decision = d
    cur.evaluations += 1
    cur.status = CycleStatus.COMPLETED
    cur.finished_at = utcnow()
    u2 = usage(deps, run.task_id)
    cur.budget_after = {"cpu_hours": round(u2.cpu_hours, 4), "api_usd": round(u2.api_usd + cur.cost_usd, 4),
                        **({"human_resume": resume} if resume else {})}
    save_cycle(store, cur)
    run.event("research_evaluation", f"cycle {cur.number}: {d.action} ({d.rationale[:200]})")
    # keyed per evaluation: a re-evaluation (after a resume / a revision request) is reported again
    _milestone(deps, run.task_id, f"cycle-{cur.cycle_id}:{cur.evaluations}",
               [f"Cycle {cur.number} evaluated: {d.action}", d.rationale[:400],
                f"Results: {sum(1 for r in cur.results if r['validated'])}/{len(cur.results)} validated",
                f"Budget used: {cur.budget_after.get('cpu_hours')} CPU-h, ${cur.budget_after.get('api_usd')}",
                *([f"Next: {len(d.new_tasks)} task(s): " + ", ".join(t.title for t in d.new_tasks)[:300]]
                  if d.action == "continue" else []),
                f"Notion: {bundle.page.url if bundle.page else ''}"])
    _act(deps, run, bundle, auth, plan, cycles(store, run.task_id))


def _propose_scope_change(deps, run, bundle, auth: ResearchAuthorization, cur: ResearchCycle,
                          d: EvaluationDecision) -> None:
    approved = rw.get_plan(deps.store, auth.plan_id)
    # a proposal of this very evaluation that was saved before a failure is adopted, never drafted twice
    plan = next((p for p in rw.plans(deps.store, run.task_id) if p.kind == "scope_change"
                 and p.source_cycle_id == cur.cycle_id and p.source_evaluation == cur.evaluations
                 and p.status in (PlanStatus.DRAFT, PlanStatus.PENDING_APPROVAL)), None)
    if plan is None:
        plan, changes = _draft_scope_change(deps, run, bundle, auth, approved, cur, d)
    else:
        changes = plan.change_summary
    req = rw.request_approval(deps, bundle, plan)  # deduplicated per plan id
    plan.status, plan.approval_request_id = PlanStatus.PENDING_APPROVAL, req.request_id
    rw.save_plan(deps.store, plan)
    rw.project(deps, plan, "Pending")
    cur.next_plan_id = plan.plan_id
    save_cycle(deps.store, cur)
    run.stage, run.waiting_for, run.resume_stage = WorkflowStage.AWAITING_APPROVAL, req.request_id, WorkflowStage.RESEARCHING.value
    if req.request_id not in run.approval_request_ids:
        run.approval_request_ids.append(req.request_id)
    run.event("scope_change", f"scope change v{plan.version} proposed: {'; '.join(changes)[:300]}")


def _draft_scope_change(deps, run, bundle, auth: ResearchAuthorization, approved: ResearchPlan, cur: ResearchCycle,
                        d: EvaluationDecision) -> tuple[ResearchPlan, list[str]]:
    from research_autopilot.research.models import ResearchScope
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
        # a limit the proposal leaves out keeps its approved value (None would read as "no limit")
        b = b.model_copy(update={"cpu_hours": b.cpu_hours if b.cpu_hours is not None else scope.budget.cpu_hours,
                                 "api_usd": b.api_usd if b.api_usd is not None else scope.budget.api_usd})
        changes.append(f"budget: {scope.budget.cpu_hours} CPU-h / ${scope.budget.api_usd} -> {b.cpu_hours} CPU-h / ${b.api_usd}")
        scope.budget = b.model_copy(update={"status": "stated", "quote": f"proposed scope change (cycle {cur.number})"})
    plan = ResearchPlan(task_id=run.task_id, notion_page_id=bundle.page.page_id if bundle.page else None,
                        version=(rw.latest_plan(deps.store, run.task_id).version + 1), kind="scope_change",
                        spec_hash=approved.spec_hash, scope=scope, evidence=approved.evidence,
                        rationale=d.rationale, rationale_evidence=d.evidence, tasks=d.new_tasks,
                        replanning_rules=approved.replanning_rules, assumptions=approved.assumptions,
                        previous_plan_id=approved.plan_id, change_summary=[*changes, *d.scope_change],
                        models=approved.models, source_cycle_id=cur.cycle_id, source_evaluation=cur.evaluations)
    plan.estimate_cpu_hours = round(sum(t.estimate_cpu_hours for t in plan.tasks), 4)
    plan.content_hash = plan.compute_hash()
    rw._record_page(deps, plan)
    rw.save_plan(deps.store, plan)
    return plan, plan.change_summary


# ---------------------------------------------------------------- completion / stop


def _finish(deps, run: WorkflowRun, bundle, auth: ResearchAuthorization | None, outcome: str, reason: str, *,
            decision: EvaluationDecision | None = None, budget: bool = False, why: str = "evaluation") -> None:
    """Complete (results) or stop (approvals: a human decides with Resume / Cancel and numbered steps).

    Idempotent: the Notes item, the report id and the message keys are stable for the same run / execution run, so
    a step that failed half-way calls it again."""
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
        closed = _close_unused_children(deps, run, cs)
        _milestone(deps, run.task_id, f"done:{run.run_id}",
                   [f"Research completed after {len(cs)} cycle(s)", reason[:600], f"Notes: {note_url}",
                    *([f"使われなかったタスク（Canceled）: {', '.join(closed)}"] if closed else []),
                    f"Task: {bundle.page.url if bundle.page else ''}"], kind=EventKind.TASK_COMPLETED, buttons=None)
    else:
        run.stage = WorkflowStage.BLOCKED
        run.blocked_reason = f"research stopped: {reason}"[:900]
        run.event("research_stopped", reason[:300])
        # a Resume restarts the same WorkflowRun under a new execution run: key on that, or every later stop is
        # deduplicated away and the human never gets a Resume button again
        _milestone(deps, run.task_id, f"stopped:{run.run_id}:{run.execution_run_id}",
                   [f"Research stopped after {len(cs)} cycle(s): {reason[:500]}", f"Notes: {note_url}",
                    f"Task: {bundle.page.url if bundle.page else ''}", _budget_line(deps, run, auth)],
                   kind=EventKind.BUDGET_EXCEEDED if budget else EventKind.UNRECOVERABLE_ERROR,
                   buttons=["resume", "cancel"], needs_human=True, steps=stop_steps(why, auth))


# Numbered "what to do until it resumes" per stop reason (docs/phase_specs/ux_policy.md 1.5).  Saving the Notion page
# re-plans automatically (an edit makes the approved scope stale); Resume is the fallback and the way to continue
# without an edit; Cancel ends the research and cancels its unfinished tasks.
_SAVE = "Save the page: RA re-plans within a few minutes and sends the new scope for approval here; approve it."
_RESUME = "If no new approval request arrives, press Resume."
_NUMBERS = ("Write every number (limit, range, count) in the text itself, e.g. 'Compute budget: 2 CPU-hours in "
            "total; API budget 10 USD': RA never fills in numbers.")
STEPS = {
    "paused": ["Running jobs finish; no new task starts while the research is paused.",
               "Press Resume to continue (the approved scope and the budget are checked again).",
               "Press Cancel to end the research; its unfinished tasks are set to Canceled in Linear."],
    "budget": ["Open the research task in Notion and, in the section 'Budget / Authorization', raise the limit "
               "that was used up.", _NUMBERS, _SAVE, _RESUME + " Cancel ends the research."],
    "max_cycles": ["Read the results in Notes (link above).",
                   "To allow more cycles, write the new limit in the section 'Autonomy' of the Notion task "
                   "(e.g. 'at most 8 cycles').", _SAVE,
                   "Resume re-evaluates the results within the current limits; Cancel ends the research."],
    "no_progress": ["Read the results in Notes (link above): the last cycles added no new information.",
                    "If the objective or the conditions need to change, edit 'Research Objective' in Notion. " + _SAVE,
                    "Press Resume to continue anyway (the no-progress count starts again), or Cancel."],
    "failure": ["Read why the task(s) failed (Notes, errors channel).",
                "If the cause is in the specification, fix the Notion task. " + _SAVE,
                "Press Resume to retry the failed tasks (within the approved retry limit), or Cancel."],
    "authorization": ["The approved research scope is no longer valid (revoked, expired or changed).",
                      "Press Resume: RA drafts a new plan version and asks for approval here.",
                      "Or press Cancel to end the research."],
    "scope_rejected": ["The research keeps the approved scope; the proposed change is not applied.",
                       "Press Resume to let RA evaluate again within the approved scope (it may propose another "
                       "change), or edit the Notion task to re-plan.", "Press Cancel to end the research."],
    "scope_expired": ["The scope change request expired without a decision.",
                      "Press Resume: RA evaluates again and re-proposes the change (or continues in scope).",
                      "Press Cancel to end the research."],
    "limits": ["The next step would leave the approved scope or limits (see the reason above).",
               "To widen the scope, edit the Notion task (conditions in 'Research Objective', limits in "
               "'Budget / Authorization' / 'Autonomy'). " + _NUMBERS, _SAVE,
               "Or press Resume to let RA evaluate again within the approved scope, or Cancel."],
    "evaluation": ["Read the stop reason above and the results in Notes.",
                   "If the specification must change, edit the Notion task. " + _SAVE,
                   "Press Resume to let RA evaluate again and continue, or Cancel to end the research."],
}


def stop_steps(why: str, auth: ResearchAuthorization | None = None) -> list[str]:
    steps = list(STEPS.get(why) or STEPS["evaluation"])
    if why == "failure" and auth is not None:
        n = ((auth.scope or {}).get("autonomy") or {}).get("max_retries_per_task")
        if n is not None:
            steps[2] = steps[2].replace("approved retry limit", f"approved retry limit of {n}")
    return steps


def _budget_line(deps, run: WorkflowRun, auth: ResearchAuthorization | None) -> str:
    try:
        u = usage(deps, run.task_id)
        b = BudgetItem.model_validate((auth.scope or {}).get("budget") or {}) if auth else BudgetItem()
        cpu = "no limit" if b.cpu_hours is None else f"{b.cpu_hours:g}"
        api = "no limit" if b.api_usd is None else f"${b.api_usd:g}"
        return f"Budget used: {u.cpu_hours:.3f} / {cpu} CPU-h, ${u.api_usd:.2f} / {api}"
    except Exception:  # noqa: BLE001 - informational
        return ""


def _close_unused_children(deps, run: WorkflowRun, cs: list[ResearchCycle]) -> list[str]:
    """A completed research leaves no unfinished child behind: tasks that never ran or stopped are closed as
    superseded (Linear Canceled with the reason, their runs cancelled). Done children stay Done."""
    from research_autopilot.autopilot.engine import TERMINAL, _runs
    closed = []
    child_runs = {r.task_id: r for r in _runs(deps) if r.root_task_id == run.task_id and r.task_id != run.task_id}
    for task_id in dict.fromkeys(t for c in cs for t in c.child_task_ids.values()):
        child = child_runs.get(task_id)
        if child is not None and child.stage is WorkflowStage.DONE:
            continue
        try:
            if deps.ops.close_superseded(deps.linear.get_issue(task_id), run.task_id):
                closed.append(task_id)
        except Exception as exc:  # noqa: BLE001 - recorded; completion is not undone for a leftover issue
            run.event("cleanup", f"{task_id} not closed: {type(exc).__name__}: {str(exc)[:120]}")
        if child is not None and child.stage not in TERMINAL:
            child.stage, child.waiting_for = WorkflowStage.CANCELLED, None
            child.event("superseded", f"research {run.task_id} completed without this task")
            _save_cancelled(deps, child)
    if closed:
        run.event("cleanup", f"unused child tasks closed as superseded: {', '.join(closed)}")
    return closed


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


def _milestone(deps, task_id: str, key: str, details: list[str], *, kind=None,
               buttons: str | list[str] | None = "stop", needs_human: bool = False,
               steps: list[str] | None = None) -> None:
    """Research milestones. ``buttons``: "stop" on cycle evaluations (results), ["resume", "cancel"] with numbered
    ``steps`` on stops / pauses (``needs_human``: routed to approvals, a human decides), None elsewhere -- the logs
    channel never carries controls."""
    from research_autopilot.notifications import notify_once
    from research_autopilot.notifications.events import EventKind, NotificationEvent
    actions = [buttons] if isinstance(buttons, str) else list(buttons or [])
    try:
        notify_once(deps.store, NotificationEvent(kind=kind or EventKind.ANALYSIS_COMPLETED, flow_name="research",
                                                  flow_run_name=task_id, details=[x for x in details if x][:12],
                                                  dedupe_key=f"research:{task_id}:{key}",
                                                  control_task_id=task_id if actions else None,
                                                  control_actions=actions, needs_human=needs_human,
                                                  steps=list(steps or [])), deps.notifier)
    except Exception:
        pass
