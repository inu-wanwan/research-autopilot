"""Autopilot engine: deterministic state machine over Phase 2 -> 3 -> 4 -> proposals -> completion.

``poll()`` is the scheduler tick (Prefect runs it every 5 minutes): it retries the outbox, checks pending
approvals, then advances at most ``max_concurrent_tasks`` research task (default 1) until it waits
or finishes. Every transition is persisted; a crashed poll resumes from the stored stage. A global
lease prevents two pollers from working at once.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from research_autopilot.autopilot import policy
from research_autopilot.autopilot.approvals import ApprovalController
from research_autopilot.autopilot.budget import chain_usage
from research_autopilot.autopilot.completion import assess, review_payload
from research_autopilot.autopilot.linear_ops import AWAITING, LinearOps, WriteRefused
from research_autopilot.autopilot.models import (
    ApprovalKind,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    Budget,
    OutboxItem,
    OutboxState,
    ResearchAuthorization,
    ResearchTaskProposal,
    WorkflowRun,
    WorkflowStage,
)
from research_autopilot.autopilot.notes import notes_blocks, proposal_blocks
from research_autopilot.autopilot.outbox import Outbox
from research_autopilot.autopilot.execution import ExecutionStatus, ExecutionRun, accept, record_blocked, transition
from research_autopilot.autopilot.dag_runtime import advance_parents, intake_parent, validate_child_inputs
from research_autopilot.experiments.models import ExperimentState, ReviewedExperimentProposal
from research_autopilot.experiments.orchestrator import ExpDeps, run_experiment
from research_autopilot.experiments.scope import dedupe_key
from research_autopilot.implementation.experiment import spec_lines
from research_autopilot.implementation.models import RunState
from research_autopilot.implementation.orchestrator import ImplDeps, implement
from research_autopilot.models import DecisionStatus, utcnow
from research_autopilot.notifications import notify_once
from research_autopilot.notifications.events import EventKind, NotificationEvent
from research_autopilot.notifications.guidance import approval_event, block_steps, human_event
from research_autopilot.orchestrator.core import Deps as PlanDeps
from research_autopilot.orchestrator.core import plan_task
from research_autopilot.storage import db

TERMINAL = {WorkflowStage.DONE, WorkflowStage.CANCELLED, WorkflowStage.DEAD_LETTER}
WAITING = {WorkflowStage.AWAITING_AUTHORIZATION, WorkflowStage.AWAITING_APPROVAL}
POLL_LEASE = "phase5:poll"


@dataclass
class AutopilotSettings:
    max_concurrent_tasks: int = 1
    max_stage_attempts: int = 3  # transient failures before dead-letter
    default_budget: Budget = field(default_factory=Budget)
    prefect_ui_url: str | None = None
    linear_url: str = "https://linear.app"
    dry_run: bool = False  # discover + evaluate only, no writes, no LLM


@dataclass
class AutopilotDeps:
    settings: AutopilotSettings
    plan: PlanDeps  # Phase 2 (its settings.linear_write_enabled must be true for apply)
    impl: ImplDeps
    exp: ExpDeps
    linear: object  # LinearWriter or fake
    notion_writer: object
    ops: LinearOps
    approvals: ApprovalController
    outbox: Outbox
    completion_reviewer: object | None
    notifier: object | None = None  # Notifier; None -> notifications module default
    dependency_inferer: object | None = None
    external: object | None = None  # Phase 9 external.service.ExternalDeps
    research: object | None = None  # Phase 10 research.workflow.ResearchDeps (None: research objectives block)

    @property
    def store(self):
        return self.plan.store


# ---------------------------------------------------------------- scheduler


def poll(deps: AutopilotDeps, *, only_task: str | None = None, prefect_flow_run_id: str | None = None) -> dict:
    store = deps.store
    store.init_schema()
    if not store.acquire_lease(POLL_LEASE, "poller", 3 * 3600):
        return {"skipped": "another poll is running"}
    summary: dict = {"outbox": [], "approvals": [], "advanced": [], "discovered": []}
    try:
        for item in deps.outbox.pending():
            if not deps.settings.dry_run:
                summary["outbox"].append((item.key, deps.outbox.process(item).state.value))
        if deps.external is not None and not deps.settings.dry_run:
            summary["external"] = _external_tick(deps)
        if deps.research is not None and not deps.settings.dry_run:
            from research_autopilot.research import orchestration
            summary["research"] = orchestration.tick(deps)
        for req in deps.approvals.pending():
            if req.kind in (ApprovalKind.EXTERNAL_CODE, ApprovalKind.EXTERNAL_LICENSE):
                continue  # Phase 9 requests are decided by external.grants (not tied to a Linear state)
            summary["approvals"].append(_check_approval(deps, req))
        if deps.research is not None and not deps.settings.dry_run:
            from research_autopilot.research import orchestration
            # a research-scope request that went stale / expired just now is re-planned in this same poll
            if replanned := orchestration.replan_stale(deps):
                summary.setdefault("research", {}).setdefault("replanned", []).extend(replanned)
        # Recovery-first: persisted parents may have newly-ready children even
        # when no fresh Notion checkbox is present after a process restart.
        advance_parents(deps)
        active = [r for r in _runs(deps) if r.stage not in TERMINAL and r.stage not in WAITING
                  and r.stage not in (WorkflowStage.BLOCKED, WorkflowStage.PAUSED)]
        if only_task:
            active = [r for r in active if r.task_id == only_task]
        if not active:
            # Phase 6A: an autopilot label is only a scope gate, never a start signal.
            for page_id in deps.plan.manager.notion.requested_task_ids():
                page = deps.plan.manager.notion.get_task(page_id)
                if not page.run_requested or not page.linear_issue_id or (only_task and page.linear_issue_id != only_task):
                    continue
                bundle = deps.plan.manager.load(page.linear_issue_id)
                if deps.research is not None and not deps.settings.dry_run:
                    # Phase 10: a research objective keeps one WorkflowRun; Run Autopilot restarts a stopped one
                    from research_autopilot.research import orchestration
                    adopted = orchestration.adopt_existing(deps, page.linear_issue_id)
                    if adopted is not None:
                        deps.notion_writer.project_execution(page.page_id, status="Running", acknowledge=True)
                        if adopted is not True:
                            summary["discovered"].append(page.linear_issue_id)
                            active.append(adopted)
                            break
                        continue
                if page.child_page_ids:
                    if deps.settings.dry_run:
                        from research_autopilot.autopilot.dag import discover_and_build
                        dag = discover_and_build(deps.plan.manager, bundle, "",
                                              registered_repositories=set(deps.impl.registry))
                        summary["discovered"].append({"parent": bundle.issue.identifier,
                                                      "dag_status": dag.status.value,
                                                      "problems": [p.model_dump() for p in dag.problems]})
                        continue
                    parent, child_runs = intake_parent(deps, bundle, page,
                                                       prefect_flow_run_id=prefect_flow_run_id)
                    if parent:
                        summary["discovered"].append(bundle.issue.identifier)
                        active.extend(child_runs)
                    continue
                # A new explicit request may rerun a completed task.  Its Linear
                # lifecycle is reset to the verified Todo state before workflow
                # start; other invalid/blocked states remain fail-closed.
                rerun = bundle.issue.state_type == "completed" and all(r.message.startswith("task is ") for r in bundle.reasons)
                if bundle.spec is None or (bundle.reasons and not rerun):
                    if not deps.settings.dry_run:
                        reason = "; ".join(r.message for r in bundle.reasons) or "missing scientific specification"
                        record_blocked(store, page=page, issue=bundle.issue, spec_hash=_spec_hash_of(bundle.spec) if bundle.spec else "unavailable", reason=reason)
                        deps.notion_writer.project_execution(page.page_id, status="Blocked", error=reason, acknowledge=True)
                    continue
                try:
                    # Refuse before durable acceptance when the configured write
                    # policy cannot legally start this task (notably test-only).
                    deps.ops._guard(bundle.issue)
                except WriteRefused as exc:
                    if not deps.settings.dry_run:
                        record_blocked(store, page=page, issue=bundle.issue, spec_hash=_spec_hash_of(bundle.spec), reason=str(exc))
                        deps.notion_writer.project_execution(page.page_id, status="Blocked", error=str(exc), acknowledge=True)
                    continue
                if rerun and not deps.settings.dry_run:
                    deps.ops.set_state(bundle.issue, "Todo", "explicit rerun requested")
                execution, reason = accept(store, page=page, issue=bundle.issue, spec_hash=_spec_hash_of(bundle.spec))
                if execution is None:
                    if reason != "request already accepted or requires reconciliation" and not deps.settings.dry_run:
                        deps.notion_writer.project_execution(page.page_id, status="Blocked", error=reason, acknowledge=True)
                    continue
                summary["discovered"].append(bundle.issue.identifier)
                if deps.settings.dry_run:
                    continue
                # Durable acceptance precedes checkbox acknowledgement.
                deps.notion_writer.project_execution(page.page_id, status="Accepted", acknowledge=True)
                active.append(_new_run(deps, bundle.issue.identifier, execution, prefect_flow_run_id))
                break
        # Phase 10: a research objective waiting on its cycle only coordinates; it does not take a task slot.
        coordinating = [r for r in active if r.stage is WorkflowStage.RESEARCHING]
        working = [r for r in active if r.stage is not WorkflowStage.RESEARCHING]
        for run in working[: deps.settings.max_concurrent_tasks] + coordinating:
            if deps.settings.dry_run:
                continue
            if input_error := validate_child_inputs(deps, run):
                _block(deps, run, input_error)
                _save(deps, run)
                summary["advanced"].append((run.task_id, run.stage.value, run.blocked_reason))
                continue
            advance(deps, run)
            summary["advanced"].append((run.task_id, run.stage.value, run.blocked_reason))
        # Capture completed child artifacts and project the derived parent
        # status in the same tick. Newly-ready work starts on the next tick.
        advance_parents(deps)
    finally:
        store.release_lease(POLL_LEASE, "poller")
    return summary


def _runs(deps: AutopilotDeps) -> list[WorkflowRun]:
    return [WorkflowRun.model_validate(r) for r in deps.store.find(db.workflow_runs)]


def _save(deps: AutopilotDeps, run: WorkflowRun) -> None:
    run.updated_at = utcnow()
    deps.store.put(db.workflow_runs, run.run_id, run.model_dump(mode="json"), task_id=run.task_id, stage=run.stage.value)
    if run.execution_run_id:
        rec = deps.store.get(db.execution_runs, run.execution_run_id)
        if rec:
            er = ExecutionRun.model_validate(rec)
            states = {WorkflowStage.AWAITING_AUTHORIZATION: "Awaiting Approval", WorkflowStage.AWAITING_APPROVAL: "Awaiting Approval", WorkflowStage.DONE: "Completed", WorkflowStage.BLOCKED: "Blocked", WorkflowStage.CANCELLED: "Canceled", WorkflowStage.DEAD_LETTER: "Failed"}
            er.status = transition(er.status, ExecutionStatus(states.get(run.stage, "Running"))); er.error_summary = run.blocked_reason; er.updated_at = utcnow()
            if er.status in {ExecutionStatus.COMPLETED, ExecutionStatus.BLOCKED, ExecutionStatus.CANCELED, ExecutionStatus.FAILED}: er.finished_at = utcnow()
            deps.store.put(db.execution_runs, er.execution_run_id, er.model_dump(mode="json"), request_id=er.request_id, task_id=er.task_id, status=er.status.value)
            # A newly clicked Button is authoritative UI intent until ingested;
            # never let a delayed old-run projection erase Requested.
            page = deps.plan.manager.notion.get_task(er.notion_task_id)
            if not page.run_requested:
                deps.notion_writer.project_execution(er.notion_task_id, status=er.status.value, error=er.error_summary,
                    run_url=_prefect_url(deps, er.prefect_flow_run_id))


def _prefect_url(deps: AutopilotDeps, flow_run_id: str | None) -> str | None:
    return f"{deps.settings.prefect_ui_url.rstrip('/')}/runs/flow-run/{flow_run_id}" if flow_run_id and deps.settings.prefect_ui_url else None


def _new_run(deps: AutopilotDeps, task_id: str, execution: ExecutionRun | None = None,
             prefect_flow_run_id: str | None = None) -> WorkflowRun:
    origin = _proposal_origin(deps, task_id)
    run = WorkflowRun(task_id=task_id, root_task_id=origin["root_task_id"] if origin else task_id,
                      execution_request_id=execution.request_id if execution else None, execution_run_id=execution.execution_run_id if execution else None,
                      authorization_id=origin.get("authorization_id") if origin else None,
                      chain_depth=origin.get("chain_depth", 0) if origin else 0,
                      parent_task_id=origin.get("parent_task_id") if origin else None,
                      research_cycle_id=origin.get("research_cycle_id") if origin else None)
    run.event("discovered", f"{task_id} (root {run.root_task_id}, depth {run.chain_depth})")
    _save(deps, run)
    if execution:
        execution.status, execution.started_at, execution.updated_at = transition(execution.status, ExecutionStatus.RUNNING), utcnow(), utcnow()
        execution.prefect_flow_run_id = prefect_flow_run_id
        deps.store.put(db.execution_runs, execution.execution_run_id, execution.model_dump(mode="json"), request_id=execution.request_id, task_id=execution.task_id, status=execution.status.value)
        deps.notion_writer.project_execution(execution.notion_task_id, status="Running",
            run_url=_prefect_url(deps, prefect_flow_run_id))
    _notify(deps, run, EventKind.TASK_STARTED, f"started:{run.run_id}",
            [f"Root task: {run.root_task_id}", f"Chain depth: {run.chain_depth}"])
    return run


def _proposal_origin(deps: AutopilotDeps, task_id: str) -> dict | None:
    for rec in deps.store.find(db.outbox):
        if rec.get("kind") == "proposal_task" and rec.get("linear_identifier") == task_id:
            return rec["payload"]
    return None


# ---------------------------------------------------------------- state machine


def advance(deps: AutopilotDeps, run: WorkflowRun, max_steps: int = 12) -> WorkflowRun:
    steps = {
        WorkflowStage.DISCOVERED: _authorize,
        WorkflowStage.PLANNING: _plan,
        WorkflowStage.IMPLEMENTING: _implement,
        WorkflowStage.EXPERIMENTING: _experiment,
        WorkflowStage.PROPOSING: _propose,
        WorkflowStage.COMPLETING: _complete,
        WorkflowStage.RESEARCHING: _research_step,
    }
    for _ in range(max_steps):
        fn = steps.get(run.stage)
        if fn is None:
            break
        before = run.stage
        try:
            fn(deps, run)
            run.attempts = 0  # max_stage_attempts counts consecutive failures, not failures over the run's life
        except WriteRefused as exc:
            _block(deps, run, f"write refused: {exc}")
        except Exception as exc:  # noqa: BLE001 - transient or unexpected: retry, then dead-letter
            run.attempts += 1
            run.event("error", f"{before.value}: {type(exc).__name__}: {str(exc)[:300]}")
            if run.attempts >= deps.settings.max_stage_attempts:
                run.stage = WorkflowStage.DEAD_LETTER
                run.blocked_reason = f"dead-lettered after {run.attempts} failures in {before.value}: {exc}"
                _notify(deps, run, EventKind.UNRECOVERABLE_ERROR, f"dead-letter:{run.run_id}", [run.blocked_reason])
                _notify_human_block(deps, run, run.blocked_reason, f"dead-letter-human:{run.run_id}",
                                    "システムの失敗が続いたため実行を止めました（自動の再試行は使い切りました）")
            else:
                _notify(deps, run, EventKind.AUTOMATIC_RETRY,
                        f"retry:{run.run_id}:{before.value}:{run.attempts}",
                        [f"Stage: {before.value}", f"Attempt: {run.attempts}/{deps.settings.max_stage_attempts}",
                         f"Error type: {type(exc).__name__}"])
            _save(deps, run)
            break
        _save(deps, run)
        if run.stage != before:
            _notify(deps, run, EventKind.PHASE_TRANSITION,
                    f"transition:{run.run_id}:{before.value}:{run.stage.value}",
                    [f"{before.value} → {run.stage.value}"])
        if run.stage == before or run.stage in TERMINAL or run.stage in WAITING or run.stage is WorkflowStage.BLOCKED:
            break
    return run


def _block(deps: AutopilotDeps, run: WorkflowRun, reason: str) -> None:
    run.stage = WorkflowStage.BLOCKED
    run.blocked_reason = reason
    run.event("blocked", reason)
    _notify_human_block(deps, run, reason, f"blocked:{run.run_id}:{reason[:80]}", "人の対応が必要なため実行を止めました")


def _is_child(run: WorkflowRun) -> bool:
    return bool(run.parent_task_id or run.research_cycle_id)


def _notify_human_block(deps: AutopilotDeps, run: WorkflowRun, reason: str, key: str, headline: str,
                        rule: str | None = None) -> None:
    """UX policy: a standalone run that needs a human -> one approvals message with the steps until it continues;
    a child of a parent DAG / research objective -> logs only (the parent / research run reports and decides)."""
    if _is_child(run):
        _notify(deps, run, EventKind.PHASE_TRANSITION, key, [f"子タスクが停止しました（親が対応します）: {reason[:500]}"])
        return
    # Resume / Cancel exist only where something consumes them (research objectives); otherwise the steps say
    # exactly how to start again.
    resumable = run.chain_depth == 0 and bool(deps.store.find(db.research_plans, task_id=run.task_id))
    actions = ["resume", "cancel"] if resumable else []
    notify_once(deps.store, human_event(
        flow_name="autopilot", task_id=run.task_id, state=run.stage.value, headline=headline, reason=reason,
        steps=block_steps(rule or reason, resume_button=resumable), links=_links(deps, run.task_id),
        cost=_cost_line(deps, run), dedupe_key=key, control_task_id=run.task_id, control_actions=actions),
        deps.notifier)


def _links(deps: AutopilotDeps, task_id: str) -> list[str]:
    notion, linear = task_urls(deps.plan.manager, task_id)
    return [x for x in (f"Notion: {notion}" if notion else None, f"Linear: {linear}" if linear else None) if x]


def task_urls(manager, task_id: str) -> tuple[str | None, str | None]:
    """Notion page URL and Linear issue URL of a task (None when unavailable; never raises)."""
    try:
        bundle = manager.load(task_id)
    except Exception:  # noqa: BLE001 - links are best-effort decoration of a message
        return None, None
    return (getattr(bundle.page, "url", None) if bundle.page else None), getattr(bundle.issue, "url", None)


def _cost_line(deps: AutopilotDeps, run: WorkflowRun) -> str | None:
    try:
        chain = [r.model_dump(mode="json") for r in _runs(deps) if r.root_task_id == run.root_task_id]
        u = chain_usage(deps.store, chain)
        auth = _authorization(deps, run)
    except Exception:  # noqa: BLE001
        return None
    if auth is None:
        return f"費用: API ${u.api_usd:.2f}、計算 {u.cpu_hours:.2f} CPU-h（承認済みの予算なし）"
    return (f"費用: API ${u.api_usd:.2f} / 予算 ${auth.budget.api_usd:.2f}、"
            f"計算 {u.cpu_hours:.2f} / {auth.budget.compute_cpu_hours:g} CPU-h")


def _load_spec(deps: AutopilotDeps, task_id: str):
    bundle = deps.plan.manager.load(task_id)
    return bundle


def _spec_hash_of(spec) -> str:
    return policy.spec_hash(spec.purpose, spec_lines(spec.scientific_invariants), spec_lines(spec.acceptance_criteria),
                            spec.specification, spec.title)


def _authorization(deps: AutopilotDeps, run: WorkflowRun) -> ResearchAuthorization | None:
    if run.authorization_id:
        rec = deps.store.get(db.authorizations, run.authorization_id)
        return ResearchAuthorization.model_validate(rec) if rec else None
    for rec in reversed(deps.store.find(db.authorizations, root_task_id=run.root_task_id)):
        a = ResearchAuthorization.model_validate(rec)
        if not policy.check_authorization(a):
            return a
    return None


def _authorize(deps: AutopilotDeps, run: WorkflowRun) -> None:
    bundle = _load_spec(deps, run.task_id)
    if bundle.spec is None:
        _block(deps, run, "no usable Notion spec: " + "; ".join(r.message for r in bundle.reasons))
        return
    from research_autopilot.research import scope as research_scope
    if research_scope.is_research_objective(bundle.spec) and run.chain_depth == 0 and not run.parent_task_id:
        _authorize_research(deps, run, bundle)
        return
    auth = _authorization(deps, run)
    if auth is not None and not policy.check_authorization(auth):
        if run.chain_depth > 0:  # follow-up task: must stay within the root authorization
            inv = spec_lines(bundle.spec.scientific_invariants)
            acc = spec_lines(bundle.spec.acceptance_criteria)
            if {policy._norm(x) for x in inv} != {policy._norm(x) for x in auth.scientific_invariants} or \
                    any(policy._norm(a) not in {policy._norm(x) for x in acc} for a in auth.acceptance_criteria):
                _block(deps, run, "follow-up task spec deviates from the root authorization")
                return
        run.authorization_id = auth.authorization_id
        run.stage = WorkflowStage.PLANNING
        run.event("authorized", f"using authorization {auth.authorization_id[:8]}")
        return
    # Explicit onboarding: the 'autopilot' label alone is not a blanket approval.
    spec = bundle.spec
    h = _spec_hash_of(spec)
    draft = ResearchAuthorization(
        root_task_id=run.root_task_id, approved_by="(pending)", approval_request_id="(pending)", spec_hash=h,
        purpose=spec.purpose, research_target=spec.title, hypothesis=None,
        scientific_invariants=spec_lines(spec.scientific_invariants), acceptance_criteria=spec_lines(spec.acceptance_criteria),
        budget=deps.settings.default_budget)
    req = deps.approvals.request(
        kind=ApprovalKind.ONBOARDING, issue=bundle.issue, root_task_id=run.root_task_id, spec_hash=h,
        title=f"Autopilot delegation for {run.task_id}: {spec.title}",
        what_changes=["Autopilot will plan, implement (Codex/Claude), run and analyze experiments for this task",
                      "and may register follow-up experiments only within the invariants / acceptance criteria below",
                      f"parameter changes allowed without asking: {[d.name for d in draft.policy.parameter_domains]}"],
        diff=[], budget_summary=f"API ${draft.budget.api_usd:.2f}, compute {draft.budget.compute_cpu_hours:g} CPU-h (cumulative)",
        payload={"authorization_draft": draft.model_dump(mode="json")})
    _wait(deps, run, req, WorkflowStage.AWAITING_AUTHORIZATION, WorkflowStage.PLANNING)


def _authorize_research(deps: AutopilotDeps, run: WorkflowRun, bundle) -> None:
    """Phase 10A: a research objective is planned and approved per research scope before anything runs."""
    from research_autopilot.research import workflow as rw
    if deps.research is None:
        _block(deps, run, "research objective found but Phase 10 research planning is not configured")
        return
    auth = rw.research_authorization(deps.store, run.task_id)
    if auth is not None and not rw.authorization_problems(deps.store, auth, bundle.spec):
        run.authorization_id = auth.authorization_id
        run.stage = WorkflowStage.RESEARCHING
        run.event("authorized", f"research scope authorization v{auth.version}")
        return
    latest = rw.latest_plan(deps.store, run.task_id)
    comment = latest.revision_comment if latest is not None and latest.status.value == "revision_requested" else ""
    rw.plan_research(deps, run, bundle, revision_comment=comment)


def _research_step(deps: AutopilotDeps, run: WorkflowRun) -> None:
    from research_autopilot.research import orchestration
    orchestration.step(deps, run)


def _wait(deps: AutopilotDeps, run: WorkflowRun, req: ApprovalRequest, stage: WorkflowStage, resume: WorkflowStage) -> None:
    run.stage = stage
    run.waiting_for = req.request_id
    run.resume_stage = resume.value
    if req.request_id not in run.approval_request_ids:
        run.approval_request_ids.append(req.request_id)
    run.event("approval", f"waiting for {req.kind.value} approval {req.request_id[:8]}")


def _budget_gate(deps: AutopilotDeps, run: WorkflowRun, auth: ResearchAuthorization) -> bool:
    chain = [r.model_dump(mode="json") for r in _runs(deps) if r.root_task_id == run.root_task_id]
    u = chain_usage(deps.store, chain)
    problems = policy.evaluate_budget(auth, u.api_usd, u.cpu_hours, u.unknown_api_cost_calls)
    if not problems:
        return True
    # No separate budget notification: the BUDGET approval message (one per request) carries usage and limits.
    issue = deps.linear.get_issue(run.task_id)
    req = deps.approvals.request(kind=ApprovalKind.BUDGET, issue=issue, root_task_id=run.root_task_id,
                                 spec_hash=_spec_hash_of(_load_spec(deps, run.task_id).spec),
                                 title=f"Budget approval for {run.task_id}", what_changes=problems, diff=[],
                                 budget_summary=f"used API ${u.api_usd:.2f} / ${auth.budget.api_usd:.2f}, "
                                                f"compute {u.cpu_hours:.2f} / {auth.budget.compute_cpu_hours:g} CPU-h",
                                 dedupe=f"{run.run_id}:{run.stage.value}")
    _wait(deps, run, req, WorkflowStage.AWAITING_APPROVAL, run.stage)
    return False


def _plan(deps: AutopilotDeps, run: WorkflowRun) -> None:
    auth = _authorization(deps, run)
    if auth is None:
        run.stage = WorkflowStage.DISCOVERED
        return
    if not _budget_gate(deps, run, auth):
        return
    if run.continue_from and not run.plan_run_id and _continue(deps, run):
        return
    issue = deps.linear.get_issue(run.task_id)
    deps.ops._guard(issue)
    seed = _dod_seed(deps, run)
    rec = plan_task(deps.plan, task_id=run.task_id, mode="apply", seed=seed)
    deps.store.record_linear_write(run.task_id, "In Progress", f"Phase 2 apply {rec.plan_run_id}")
    d = rec.decision
    run.plan_run_id, run.plan_id = rec.plan_run_id, d.plan_id
    run.plan_version += 1
    if seed is not None and d.status is DecisionStatus.READY:
        run.dod_claim = {**run.dod_claim, "consumed": True}  # a transient failure keeps the seed for the retry
    if seed is not None and d.status is DecisionStatus.READY and (kept := _claim_rejected(rec, run.dod_claim)):
        _block(deps, run, "Definition of Done infeasibility not accepted on re-planning (items kept unchanged: "
                          f"{', '.join(kept)}); a human decides. Claim: {run.dod_claim['evidence'][:300]}")
        return
    if d.status is DecisionStatus.READY:
        run.stage = WorkflowStage.IMPLEMENTING
        run.event("planned", f"plan {d.plan_id[:8]} approved by the Planner/Reviewer loop")
    elif d.status is DecisionStatus.TRANSIENT_FAILURE:
        raise RuntimeError("Phase 2 transient failure: " + "; ".join(r.message for r in d.reasons))
    else:
        _block(deps, run, f"Phase 2 {d.status.value}: " + "; ".join(r.message for r in d.reasons)[:400])


def _continue(deps: AutopilotDeps, run: WorkflowRun) -> bool:
    """Reuse the approved plan of a run that was cut off by a non-task limit (usage limit, environment setup).

    The Phase 3 approval gate is re-checked first (apply handoff, Linear In Progress, Notion spec unchanged since
    planning); if it does not pass, the run is planned again as usual."""
    from research_autopilot.implementation.orchestrator import _approval_gate, _load_handoff, _Stop
    from research_autopilot.implementation.models import ImplementationManifest
    c = run.continue_from
    rec = deps.store.get_run(c["plan_run_id"])
    issue = deps.linear.get_issue(run.task_id)
    deps.ops._guard(issue)
    try:
        if rec is None or not rec.handoff_path:
            raise _Stop(RunState.BLOCKED, "handoff_invalid", "no Phase 2 handoff for the reused plan")
        if issue.state_type != "started":
            deps.ops.set_state(issue, deps.plan.settings.in_progress_state_name,
                               f"continue {c['stage']} with plan {str(c.get('plan_id'))[:8]} (not re-planned)")
        probe = ImplementationManifest(task_id=run.task_id)
        _approval_gate(deps.impl, _load_handoff(Path(rec.handoff_path), probe), probe)
    except _Stop as stop:
        run.event("replan", f"plan not reused ({stop.message[:200]}); planning again")
        run.continue_from = None
        if issue.state_type == "started":  # Phase 2 plans only a not-started task: undo the In Progress above
            deps.ops.set_state(issue, "Todo", "plan not reused: planning again")
        return False
    run.plan_run_id, run.plan_id = c["plan_run_id"], c.get("plan_id")
    run.plan_version += 1
    run.event("planned", f"plan {str(run.plan_id)[:8]} reused: the previous run stopped on a non-task limit")
    if c["stage"] == "experimenting":
        run.phase3_run_ids, run.commits = [c["phase3_run_id"]], [c["commit"]]
        run.stage = WorkflowStage.EXPERIMENTING
        run.event("implemented", f"commit {c['commit'][:12]} reused (Phase 3 {c['phase3_run_id'][:8]})")
    else:
        run.stage = WorkflowStage.IMPLEMENTING
    return True


def _implement(deps: AutopilotDeps, run: WorkflowRun) -> None:
    rec = deps.store.get_run(run.plan_run_id)
    c = run.continue_from
    if c and c["stage"] == "implementing" and not run.phase3_run_ids:
        m = implement(deps.impl, continue_run_id=c["phase3_run_id"])
    else:
        m = implement(deps.impl, handoff_path=Path(rec.handoff_path))
    run.phase3_run_ids.append(m.phase3_run_id)
    if m.state is RunState.COMPLETED:
        run.commits.append(m.implementation_commit)
        run.stage = WorkflowStage.EXPERIMENTING
        run.event("implemented", f"commit {m.implementation_commit[:12]} (Phase 3 {m.phase3_run_id[:8]})")
    elif m.dod_infeasible and run.dod_replans == 0:
        _replan_for_dod(deps, run, m)
    else:
        _block(deps, run, "Phase 3: " + "; ".join(b.message for b in m.block_reasons)[:400])


def _replan_for_dod(deps: AutopilotDeps, run: WorkflowRun, m) -> None:
    """Send the plan back to Phase 2 once with the infeasibility claim (docs/phase_specs/phase3.md)."""
    plan = _plan_of(deps, run.plan_run_id, run.plan_id)
    statements = {c.id: c.statement for c in (plan.definition_of_done if plan else [])}
    # Linear first: if this write fails the run is retried with its claim / counter untouched
    issue = deps.linear.get_issue(run.task_id)
    deps.ops.set_state(issue, "Todo", f"re-planning: Definition of Done {', '.join(m.dod_infeasible['criteria'])} "
                                      "reported infeasible")  # Phase 2 starts only from an approved, not-started task
    run.dod_replans += 1
    run.dod_claim = {**m.dod_infeasible, "plan_run_id": run.plan_run_id, "plan_id": run.plan_id,
                     "statements": {i: statements.get(i, "") for i in m.dod_infeasible["criteria"]},
                     "consumed": False}
    run.stage = WorkflowStage.PLANNING
    run.event("dod_infeasible", f"{m.dod_infeasible['by']} reported {', '.join(m.dod_infeasible['criteria'])} "
                                f"infeasible; re-planning once: {m.dod_infeasible['evidence'][:200]}")


def _plan_of(deps: AutopilotDeps, plan_run_id: str | None, plan_id: str | None):
    rec = deps.store.get_run(plan_run_id) if plan_run_id else None
    return next((a.plan for a in (rec.plans if rec else []) if a.plan_id == plan_id), None)


def _dod_seed(deps: AutopilotDeps, run: WorkflowRun):
    c = run.dod_claim
    if not c or c.get("consumed") or ("consumed" not in c and run.plan_run_id != c["plan_run_id"]):
        return None  # no claim, or the re-plan already produced a READY plan (older records: a new plan run)
    plan = _plan_of(deps, c["plan_run_id"], c["plan_id"])
    if plan is None:
        return None
    from research_autopilot.models import RevisionRequest
    return plan, [RevisionRequest(
        aspect="feasibility", severity="major", step_ids=[],
        issue=f"Phase 3 ({c['by']}) reports Definition of Done item(s) {', '.join(c['criteria'])} impossible as "
              f"written: {c['evidence'][:1500]}",
        requested_change="If the evidence holds, rewrite those items so they are achievable with the available "
                         "API / environment without weakening the acceptance criteria; keep them unchanged only "
                         "if the evidence is wrong.")]


def _claim_rejected(rec, claim: dict) -> list[str]:
    """Items whose statement survived the re-plan unchanged: the planner did not accept the claim."""
    plan = next((a.plan for a in rec.plans if a.plan_id == rec.decision.plan_id), None)
    kept = {c.statement.strip() for c in (plan.definition_of_done if plan else [])}
    return [i for i, st in claim.get("statements", {}).items() if st and st.strip() in kept]


def _experiment(deps: AutopilotDeps, run: WorkflowRun) -> None:
    auth = _authorization(deps, run)
    if not _budget_gate(deps, run, auth):
        return
    deps.exp.extra["chain_depth"] = run.chain_depth
    m = run_experiment(deps.exp, phase3_run_ids=[run.phase3_run_ids[-1]])
    run.experiment_run_ids.append(m.experiment_run_id)
    for sp in m.specs.values():
        if sp.identification.commit not in run.commits:
            run.commits.append(sp.identification.commit)
            run.phase3_run_ids.append(sp.identification.phase3_run_id)
    if m.state is ExperimentState.BLOCKED and m.external_approvals:
        raw = deps.store.get(db.approval_requests, m.external_approvals[-1])
        req = ApprovalRequest.model_validate(raw)
        issue = deps.linear.get_issue(run.task_id)
        deps.ops.set_state(issue, AWAITING, f"external code approval {req.request_id[:8]}")
        _wait(deps, run, req, WorkflowStage.AWAITING_APPROVAL, WorkflowStage.EXPERIMENTING)
        run.event("external_code", m.block_reason or "")
        return
    if m.state is ExperimentState.COMPLETED:
        run.stage = WorkflowStage.PROPOSING
        run.event("experimented", f"experiment {m.experiment_run_id[:8]} validated; {len(m.reviewed_proposals)} proposal(s)")
        _notify(deps, run, EventKind.EXPERIMENT_COMPLETED, f"experiment:{m.experiment_run_id}",
                [f"Experiment run: {m.experiment_run_id}", f"Validated proposals: {len(m.reviewed_proposals)}"])
    else:
        _block(deps, run, f"Phase 4 {m.state.value}: {m.block_reason}")


def _propose(deps: AutopilotDeps, run: WorkflowRun) -> None:
    if run.research_cycle_id:  # Phase 10B: the Research Evaluator decides follow-ups for the whole cycle
        run.stage = WorkflowStage.COMPLETING
        return
    auth = _authorization(deps, run)
    exp = deps.store.get_experiment(run.experiment_run_ids[-1])
    bundle = _load_spec(deps, run.task_id)
    chain = [r.model_dump(mode="json") for r in _runs(deps) if r.root_task_id == run.root_task_id]
    u = chain_usage(deps.store, chain)
    for i, pid in enumerate(exp.reviewed_proposals, start=1):
        if pid in run.proposal_ids:
            continue
        rp = ReviewedExperimentProposal.model_validate_json(Path(exp.artifact_paths[f"proposal:{pid}"]).read_text())
        pending = sum(1 for r in deps.approvals.pending() if r.kind is ApprovalKind.PROPOSAL and r.root_task_id == run.root_task_id)
        decision = policy.evaluate_proposal(auth, rp, chain_depth=run.chain_depth + 1, proposals_from_experiment=i,
                                            pending_proposals=pending, used_api_usd=u.api_usd, used_cpu_hours=u.cpu_hours,
                                            unknown_api_cost_calls=u.unknown_api_cost_calls)
        run.proposal_ids.append(pid)
        dup = _duplicate_check(deps, run.root_task_id, rp.proposal.draft)
        if dup == "exact":
            run.event("proposal", f"{rp.proposal.draft.title[:60]!r}: skipped (same proposal already registered in this chain)")
            continue
        if dup == "similar" and decision.outcome == "auto":
            decision = decision.model_copy(update={"outcome": "human", "categories": ["possible_duplicate"],
                                                   "reasons": ["very similar to an existing task in this chain; human check"]})
        run.event("proposal", f"{rp.proposal.draft.title[:60]!r}: {decision.outcome} ({'; '.join(decision.reasons)[:200]})")
        if decision.outcome == "reject":
            continue
        d = rp.proposal.draft
        tp = ResearchTaskProposal(
            proposal_id=pid, root_task_id=run.root_task_id, parent_task_id=run.task_id, authorization_id=auth.authorization_id,
            chain_depth=run.chain_depth + 1, title=("[TEST] " if bundle.issue.title.startswith("[TEST]") else "") + d.title,
            purpose=d.purpose, implementation=d.implementation, acceptance_criteria=d.acceptance_criteria,
            scientific_invariants=d.scientific_invariants, hypothesis=d.hypothesis, rationale=d.expected_information_value,
            review_summary=f"{rp.review.verdict}: {rp.review.summary}", resources=d.estimated_resources,
            evidence_artifacts=rp.proposal.source_artifacts, repository=bundle.spec.repository, file_scope=bundle.spec.file_scope,
            policy=decision.outcome, policy_reasons=decision.reasons)
        item = deps.outbox.enqueue(OutboxItem(key=f"proposal:{pid}", kind="proposal_task", payload={
            **tp.model_dump(mode="json"), "blocks": proposal_blocks(tp), "pending": decision.outcome != "auto",
            "content_key": dedupe_key(run.root_task_id, d), "shingles": sorted(_shingles(d)),
            "category": bundle.page.category if bundle.page else None, "parent_page_id": bundle.page.page_id if bundle.page else None,
            "linear_state": "Todo" if decision.outcome == "auto" else "Awaiting Approval",
            "labels": [deps.plan.settings.scope_label],
            "description": f"Autopilot follow-up of {run.task_id} ({decision.outcome}).\n" + "\n".join(f"- {r}" for r in decision.reasons)}))
        item = deps.outbox.process(item) if item.state is not OutboxState.DONE else item
        if item.state is not OutboxState.DONE:
            raise RuntimeError(f"outbox {item.key} not completed: {item.last_error}")
        _notify(deps, run, EventKind.PROPOSAL_GENERATED, f"proposal:{pid}",
                [f"Proposal: {d.title}", f"Policy: {decision.outcome}", *decision.reasons[:3]])
        if decision.outcome == "human":
            child = deps.linear.get_issue(item.linear_identifier)
            child_spec = _load_spec(deps, item.linear_identifier).spec
            req = deps.approvals.request(
                kind=ApprovalKind.PROPOSAL, issue=child, root_task_id=run.root_task_id,
                spec_hash=_spec_hash_of(child_spec) if child_spec else _proposal_hash(tp),
                title=f"Follow-up experiment needs approval: {d.title}", what_changes=[f"{c.name}: {c.from_value} -> {c.to_value} ({c.kind})" for c in d.parameter_changes],
                diff=decision.reasons, budget_summary=f"chain used API ${u.api_usd:.2f}, compute {u.cpu_hours:.2f} CPU-h",
                payload={"proposal_id": pid, "notion_page_id": item.notion_page_id})
            run.approval_request_ids.append(req.request_id)
    run.stage = WorkflowStage.COMPLETING


def _shingles(d) -> set[str]:
    text = policy._norm(f"{d.research_question} {d.hypothesis} {d.implementation}")
    return {text[i:i + 3] for i in range(max(len(text) - 2, 1))}


def _duplicate_check(deps: AutopilotDeps, root: str, d) -> str | None:
    """'exact' = identical content already registered in this chain (skip);
    'similar' = high character-trigram overlap (never auto-merged; a human decides)."""
    key = dedupe_key(root, d)
    mine = _shingles(d)
    for rec in deps.store.find(db.outbox):
        p = rec.get("payload", {})
        if rec.get("kind") != "proposal_task" or p.get("root_task_id") != root:
            continue
        if p.get("content_key") == key:
            return "exact"
        other = set(p.get("shingles", []))
        if other and len(mine & other) / max(len(mine | other), 1) >= 0.8:
            return "similar"
    return None


def _proposal_hash(tp: ResearchTaskProposal) -> str:
    return policy.spec_hash(tp.purpose, tp.scientific_invariants, tp.acceptance_criteria, tp.implementation, tp.title)


def _complete(deps: AutopilotDeps, run: WorkflowRun) -> None:
    exp = deps.store.get_experiment(run.experiment_run_ids[-1])
    if exp.analysis is not None:
        _notify(deps, run, EventKind.ANALYSIS_COMPLETED, f"analysis:{exp.experiment_run_id}",
                [f"Experiment run: {exp.experiment_run_id}", f"Outcome: {exp.analysis.hypothesis_evaluation}"])
    bundle = _load_spec(deps, run.task_id)
    acceptance = spec_lines(bundle.spec.acceptance_criteria)
    impl = deps.store.get_implementation(run.phase3_run_ids[-1])
    risk = impl.assessment.scientific_risk if impl and impl.assessment else None
    approved_review = run.resume_stage == "completion_approved"
    review = None
    if deps.completion_reviewer is not None:
        r = deps.completion_reviewer.review(review_payload(
            {"task_id": run.task_id, "acceptance_criteria": acceptance, "purpose": bundle.spec.purpose}, exp,
            exp.analysis.summary if exp.analysis else None))
        review = r.output
        run.event("completion_review", f"{getattr(r, 'model', '')} cost={getattr(r, 'cost_usd', None)}")
    a = assess(exp, acceptance, risk, review)
    pending = [x for x in deps.approvals.pending() if x.task_id == run.task_id]
    if pending:
        a.auto_done = False
        a.reasons.append(f"{len(pending)} approval request(s) still pending for this task")
    if not a.deterministic_ok:
        _block(deps, run, "cannot complete: " + "; ".join(a.reasons))
        return
    if not a.auto_done and not approved_review:
        issue = deps.linear.get_issue(run.task_id)
        req = deps.approvals.request(kind=ApprovalKind.COMPLETION_REVIEW, issue=issue, root_task_id=run.root_task_id,
                                     spec_hash=_spec_hash_of(bundle.spec), title=f"Review results of {run.task_id} before Done",
                                     what_changes=["mark the task Done with the results below"], diff=a.reasons,
                                     budget_summary="-", dedupe=run.experiment_run_ids[-1])
        _wait(deps, run, req, WorkflowStage.AWAITING_APPROVAL, WorkflowStage.COMPLETING)
        run.resume_stage = "completion_approved"
        return
    links = {"linear": f"{deps.settings.linear_url}/issue/{run.task_id}",
             "prefect": f"{deps.settings.prefect_ui_url}/runs" if deps.settings.prefect_ui_url else "-"}
    notes = deps.outbox.enqueue(OutboxItem(key=f"notes:{exp.experiment_run_id}", kind="notes", payload={
        "title": f"[AP] {bundle.spec.title} — results", "blocks": notes_blocks(run.task_id, bundle.spec.title, exp, a, links, run.commits)}))
    notes = deps.outbox.process(notes) if notes.state is not OutboxState.DONE else notes
    if notes.state is not OutboxState.DONE:
        raise RuntimeError(f"Notes write failed: {notes.last_error}")  # not Done; retried / dead-lettered
    run.notes_page_id = notes.notion_page_id
    body = "\n".join([f"Autopilot result for {run.task_id}: {a.hypothesis_outcome or 'n/a'}",
                      f"Notion Notes: {deps.notion_writer.page_url(notes.notion_page_id)}",
                      f"Experiment run: {exp.experiment_run_id}", f"Commits: {', '.join(c[:12] for c in run.commits)}",
                      *[f"[{c.status}] {c.criterion}" for c in a.criteria]])
    comment = deps.outbox.enqueue(OutboxItem(key=f"comment:{exp.experiment_run_id}", kind="linear_comment",
                                             payload={"task_id": run.task_id, "body": body}))
    comment = deps.outbox.process(comment) if comment.state is not OutboxState.DONE else comment
    if comment.state is not OutboxState.DONE:
        raise RuntimeError(f"Linear comment failed: {comment.last_error}")
    deps.ops.set_state(deps.linear.get_issue(run.task_id), "Done", f"completion {exp.experiment_run_id}")
    run.stage = WorkflowStage.DONE
    run.event("done", f"hypothesis {a.hypothesis_outcome}; " + "; ".join(a.reasons))
    _notify(deps, run, EventKind.TASK_COMPLETED, f"done:{run.run_id}",
            [f"Notes: {deps.notion_writer.page_url(notes.notion_page_id)}", f"Linear: {links['linear']}",
             f"Result: {a.hypothesis_outcome}; " + (exp.analysis.summary[:200] if exp.analysis else "")])


# ---------------------------------------------------------------- approvals


def _check_approval(deps: AutopilotDeps, req: ApprovalRequest) -> tuple:
    task_runs = [r for r in _runs(deps) if r.task_id == req.task_id]
    if task_runs and all(r.stage is WorkflowStage.CANCELLED for r in task_runs):
        # cancelled (research Cancel / reject): a late Approve must create nothing
        deps.approvals.withdraw(req, "タスクの実行は打ち切られました")
        return (req.request_id[:8], ApprovalStatus.STALE.value, "withdrawn: task cancelled")
    issue = deps.linear.get_issue(req.task_id)
    history = deps.linear.history(req.task_id)
    bundle = _load_spec(deps, req.task_id)  # the task's *current* Notion spec (edits make the approval stale)
    if req.kind is ApprovalKind.DAG:
        from research_autopilot.autopilot.dag_runtime import current_dag_approval_hash
        current = current_dag_approval_hash(deps, req)
    elif req.kind is ApprovalKind.RESEARCH_SCOPE:
        from research_autopilot.research.workflow import current_approval_hash
        current = current_approval_hash(deps, req)
    elif req.payload.get("parent_run_id"):
        # parent completion review: requested with the parent DAG's own spec hash function
        from research_autopilot.autopilot.dag_runtime import _spec_hash as parent_spec_hash
        current = parent_spec_hash(bundle.spec)
    else:
        current = _spec_hash_of(bundle.spec) if bundle.spec else "unavailable"
    result = deps.approvals.check(req, issue, history, current)
    if result.decision is None:
        if result.status in {ApprovalStatus.STALE, ApprovalStatus.EXPIRED}:
            _reopen_closed_request(deps, req, result.status, result.note)
        return (req.request_id[:8], result.status.value, result.note)
    _apply_decision(deps, req, result.decision, issue)
    return (req.request_id[:8], result.status.value, result.note)


def _reopen_closed_request(deps: AutopilotDeps, req: ApprovalRequest, status: ApprovalStatus, note: str) -> None:
    """A stale / expired request must not leave its run AWAITING forever: move the run back to the stage that
    asks again (from the current spec), like research does for research_scope.  The old Discord message is
    edited to point to the new request by the gateway (``_superseded_by``), not answered with a new note."""
    if req.payload.get("parent_run_id") or req.kind is ApprovalKind.DAG:
        from research_autopilot.autopilot.dag_runtime import close_parent_dag_approval
        close_parent_dag_approval(deps, req, note)
        return
    if req.kind is ApprovalKind.RESEARCH_SCOPE:
        return  # research.orchestration.tick re-plans and asks again
    if req.kind is ApprovalKind.PROPOSAL:
        bundle = _load_spec(deps, req.task_id)
        if bundle.spec is None:
            return  # nothing current to approve (the proposal page is unusable); a new proposal is needed
        deps.approvals.request(
            kind=ApprovalKind.PROPOSAL, issue=deps.linear.get_issue(req.task_id), root_task_id=req.root_task_id,
            spec_hash=_spec_hash_of(bundle.spec), title=req.title,
            what_changes=req.what_changes, diff=req.diff_from_original, budget_summary=req.budget_summary,
            payload={k: v for k, v in req.payload.items() if not k.startswith("_")})
        return
    run = next((r for r in _runs(deps) if r.waiting_for == req.request_id), None)
    if run is None or run.stage not in WAITING:
        return
    resume = run.resume_stage
    if req.kind is ApprovalKind.ONBOARDING or not resume:
        run.stage = WorkflowStage.DISCOVERED  # onboarding is asked again from the current spec
    elif resume == "completion_approved":
        run.stage = WorkflowStage.COMPLETING  # the completion review is asked again
    else:
        run.stage = WorkflowStage(resume)  # e.g. the budget gate of that stage asks again
    run.waiting_for, run.resume_stage = None, None
    run.event("rerequest", f"{req.kind.value} request {req.request_id[:8]} {status.value}: asking again")
    _save(deps, run)


def _apply_decision(deps: AutopilotDeps, req: ApprovalRequest, approval: ApprovalDecision, issue) -> None:
    from research_autopilot.autopilot.dag_runtime import apply_parent_approval
    if apply_parent_approval(deps, req, approval):
        return
    decision, actor = approval.decision, approval.actor
    run = next((r for r in _runs(deps) if r.waiting_for == req.request_id), None)
    if req.kind is ApprovalKind.RESEARCH_SCOPE:
        from research_autopilot.research.workflow import apply_decision as apply_research
        apply_research(deps, req, approval, issue, run)
        if run is not None:
            _save(deps, run)
        return
    if req.kind is ApprovalKind.PROPOSAL:
        if decision == "approve":
            page = req.payload.get("notion_page_id")
            if page:
                deps.notion_writer.set_task_links(page, pending=False)  # Phase 2 approval gate: Pending=false
            deps.ops.set_state(issue, "Todo", f"proposal approved ({req.request_id[:8]}) by {actor}")
        if approval.channel == "discord" and decision in {"reject", "revise"}:
            state = "Canceled" if decision == "reject" else "Revision Requested"
            deps.ops.reflect_verified_decision(issue, state, f"verified Discord {decision} ({req.request_id[:8]}) by {actor}")
        return
    if run is None:
        return
    if decision == "approve":
        if req.kind is ApprovalKind.ONBOARDING:
            draft = ResearchAuthorization.model_validate(req.payload["authorization_draft"])
            auth = draft.model_copy(update={"approved_by": actor, "approval_request_id": req.request_id, "approved_at": utcnow()})
            deps.store.put(db.authorizations, auth.authorization_id, auth.model_dump(mode="json"), root_task_id=auth.root_task_id)
            run.authorization_id = auth.authorization_id
        if req.kind is ApprovalKind.BUDGET:
            auth = _authorization(deps, run)
            new_budget = Budget(api_usd=auth.budget.api_usd * 2, compute_cpu_hours=auth.budget.compute_cpu_hours * 2)
            auth = auth.model_copy(update={"budget": new_budget})
            deps.store.put(db.authorizations, auth.authorization_id, auth.model_dump(mode="json"), root_task_id=auth.root_task_id)
        if req.kind in (ApprovalKind.ONBOARDING, ApprovalKind.BUDGET):
            deps.ops.set_state(issue, "Todo" if run.stage is WorkflowStage.AWAITING_AUTHORIZATION else "In Progress",
                               f"approval {req.request_id[:8]} by {actor}")
        resume = run.resume_stage
        run.stage = WorkflowStage.COMPLETING if resume == "completion_approved" else WorkflowStage(resume)
        if resume != "completion_approved":
            run.resume_stage = None
        run.waiting_for = None
        run.event("approved", f"{req.kind.value} approved by {actor}")
    elif decision == "reject":
        if approval.channel == "discord":
            deps.ops.reflect_verified_decision(issue, "Canceled",
                                                f"verified Discord reject ({req.request_id[:8]}) by {actor}")
        run.stage = WorkflowStage.CANCELLED
        run.waiting_for = None
        run.event("rejected", f"{req.kind.value} rejected by {actor}")
    else:
        if approval.channel == "discord":
            deps.ops.reflect_verified_decision(issue, "Revision Requested",
                                                f"verified Discord revise ({req.request_id[:8]}) by {actor}")
        run.stage = WorkflowStage.BLOCKED
        run.blocked_reason = f"revision requested by {actor}: edit the Notion spec / plan, then re-run"
        run.waiting_for = None
        run.event("revision", run.blocked_reason)
        _notify_human_block(deps, run, f"{req.kind.value} の修正依頼（{actor}）: {approval.comment or '(コメントなし)'}",
                            f"revision:{req.request_id}", "承認依頼に修正が求められたため止めています",
                            rule="revision requested")
    _save(deps, run)


# ---------------------------------------------------------------- notifications


def _external_tick(deps: AutopilotDeps) -> dict:
    """Phase 9: apply verified decisions on external code, resume runs that waited, re-verify updated repos."""
    from research_autopilot.external.integration import process_approvals, refresh
    out = {"decisions": [], "updates": []}
    try:
        out["decisions"] = process_approvals(deps.external)
    except Exception as exc:  # never break the poll loop
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    for item in out["decisions"]:
        run = next((r for r in _runs(deps) if r.waiting_for == item["request_id"]), None)
        if run is None or item["status"] == "pending":
            continue
        issue = deps.linear.get_issue(run.task_id)
        run.waiting_for = None
        if item["status"] == "approved":
            deps.ops.set_state(issue, "In Progress", f"external code approved ({item['request_id'][:8]})")
            run.stage = WorkflowStage(run.resume_stage or WorkflowStage.EXPERIMENTING.value)
            run.resume_stage = None
            run.event("approved", f"external code execution approved ({item.get('grant_id')})")
        else:
            run.stage = WorkflowStage.BLOCKED
            run.blocked_reason = (f"external code execution {item['status']} ({item['request_id'][:8]}): "
                                  "change the experiment or ask again with a different scope")
            run.event("external_code", run.blocked_reason)
            label = {"rejected": "却下されました", "revision_requested": "修正を求められました",
                     "stale": "対象のコードが変わったため失効しました",
                     "expired": "期限切れで失効しました"}.get(item["status"], item["status"])
            _notify_human_block(deps, run, f"外部コードの実行依頼 {item['request_id'][:8]} が{label}"
                                           f"（{item.get('note') or ''}）",
                                f"external-decision:{item['request_id']}", "外部コードを実行できないため止めています",
                                rule="external code")
        _save(deps, run)
    try:
        out["updates"] = refresh(deps.external)
    except Exception as exc:
        out["update_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return out


# Per-task results of research cycles are replaced by research milestones; routine progress still goes to the
# (muted) logs channel so the run can be followed there (user decision 2026-10-11).
_ROUTINE = {EventKind.EXPERIMENT_COMPLETED, EventKind.ANALYSIS_COMPLETED, EventKind.TASK_COMPLETED}


def _notify(deps: AutopilotDeps, run: WorkflowRun, kind: EventKind, dedupe: str, details: list[str]) -> None:
    if kind in _ROUTINE and (run.research_cycle_id or deps.store.find(db.research_plans, task_id=run.task_id)):
        return  # Phase 10: research objectives and their cycle tasks report results through research milestones
    notify_once(deps.store,
                NotificationEvent(kind=kind, flow_name="autopilot", flow_run_name=run.task_id,
                                  state_name=run.stage.value, details=details, dedupe_key=dedupe),
                deps.notifier)


def notify_approval(deps_store, notifier, req: ApprovalRequest, message: str, linear_url: str | None = None,
                    form_url: str | None = None, notion_url: str | None = None) -> None:
    """The approvals message of a new request (one template per kind; see notifications.guidance).

    ``linear_url`` is the issue URL (a bare workspace URL is completed with ``/issue/<id>`` for old callers).
    A stale / expired request gets no new message: the gateway edits the original one to point to its
    replacement (UX policy 1.4)."""
    if req.status is not ApprovalStatus.PENDING:
        return
    links = []
    notion = req.payload.get("plan_url") or req.payload.get("notion_url") or notion_url
    if notion:
        links.append(f"Notion: {notion}")
    if linear_url:
        links.append("Linear: " + (linear_url if "/issue/" in linear_url
                                   else f"{linear_url.rstrip('/')}/issue/{req.task_id}"))
    notify_once(deps_store, approval_event(req, links=links, form_url=form_url,
                                           dedupe_key=f"approval:{req.request_id}:{message[:40]}"), notifier)


def status_report(deps: AutopilotDeps) -> dict:
    runs = _runs(deps)
    return {"runs": [{"task": r.task_id, "stage": r.stage.value, "blocked": r.blocked_reason, "waiting_for": r.waiting_for,
                      "commits": [c[:12] for c in r.commits]} for r in runs],
            "pending_approvals": [{"id": a.request_id, "task": a.task_id, "kind": a.kind.value} for a in deps.approvals.pending()],
            "outbox": [{"key": o.key, "state": o.state.value, "error": o.last_error} for o in deps.outbox.pending()]}


__all__ = ["AutopilotDeps", "AutopilotSettings", "poll", "advance", "status_report", "json", "ApprovalStatus"]
