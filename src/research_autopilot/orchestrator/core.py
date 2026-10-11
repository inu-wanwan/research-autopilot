"""Orchestrator core: deterministic control flow around the Task Manager and agents.

Steps (docs/phase_specs/phase2.md §6):
 1. fetch task (Linear + Notion)          5. replan (<= max_replans)
 2. deterministic gates                   6. final gate re-check against fresh data
 3. Planner                               7. apply mode: Linear -> In Progress
 4. Reviewer (every plan)                 8./9. persist record, write Phase 3 handoff

This module has no Prefect dependency so it can be unit-tested with fakes;
``orchestrator.flow`` wraps it in a Prefect flow.
"""

from dataclasses import dataclass
from typing import Any, Literal

from research_autopilot.config import Settings
from research_autopilot.models import (
    BlockedCode,
    BlockedReason,
    CostSummary,
    DecisionStatus,
    ExecutionHandoff,
    LinearUpdateResult,
    PlanRunRecord,
    RunEvent,
    WorkflowDecision,
    new_id,
    utcnow,
)
from research_autopilot.planning.agents import Planner, Reviewer
from research_autopilot.planning.loop import LoopOutcome, run_planning
from research_autopilot.storage.artifacts import handoff_path, run_record_path, write_json
from research_autopilot.storage.db import Store
from research_autopilot.tasks import gates
from research_autopilot.tasks.errors import (
    ConnectorError,
    NotFoundError,
    TransientConnectorError,
)
from research_autopilot.tasks.manager import LinearGateway, TaskBundle, TaskManager

Mode = Literal["dry_run", "apply", "check_only"]


class WriteNotPermitted(Exception):
    """apply mode requested without RA_LINEAR_WRITE_ENABLED=true."""


@dataclass
class Deps:
    manager: TaskManager
    linear: LinearGateway
    planner: Planner
    reviewer: Reviewer
    store: Store
    settings: Settings
    catalog: Any = None  # catalog.service.CatalogContext (Phase 7A); None -> catalog requirements block
    literature: Any = None  # Phase 8: (question, task_id, seeds) -> LiteratureReport; None disables research
    external: Any = None  # Phase 9: external.service.ExternalDeps; None disables onboarding of implementations


def plan_task(deps: Deps, *, task_id: str | None = None, next_task: bool = False,
              mode: Mode = "dry_run", flow_run_id: str | None = None, seed=None) -> PlanRunRecord:
    """``seed``: (previous ExecutionPlan, [RevisionRequest]) -- re-plan from an earlier plan with feedback."""
    if (task_id is None) == (not next_task):
        raise ValueError("specify exactly one of task_id or next_task")
    if mode == "apply" and not deps.settings.linear_write_enabled:
        raise WriteNotPermitted("apply mode requires RA_LINEAR_WRITE_ENABLED=true")

    record = PlanRunRecord(
        plan_run_id=new_id(), task_id=task_id, flow_run_id=flow_run_id, mode=mode,
        started_at=utcnow(), planner_model=deps.planner.model, reviewer_model=deps.reviewer.model,
    )
    deps.store.init_schema()
    try:
        record.decision = _run(deps, record, task_id, next_task, mode, seed)
    except Exception as exc:
        record.events.append(RunEvent(kind="error", message=f"unexpected {type(exc).__name__}: {exc}"))
        raise
    finally:
        _finish(deps, record)
    return record


# ---------------------------------------------------------------- steps


def _run(deps: Deps, record: PlanRunRecord, task_id: str | None, next_task: bool, mode: Mode,
         seed=None) -> WorkflowDecision:
    s = deps.settings
    try:
        bundle = _select_next(deps, record) if next_task else deps.manager.load(task_id)
    except _NoCandidate as nc:
        return _decision(record, DecisionStatus.BLOCKED, nc.reasons, "no eligible task in scope")
    except ConnectorError as exc:
        return _connector_failure(record, exc)

    record.task_id = bundle.issue.identifier
    record.linear_snapshot, record.notion_snapshot, record.task_spec = bundle.issue, bundle.page, bundle.spec
    approval = gates.human_approval(bundle.issue, bundle.page, s)

    reasons = list(bundle.reasons)
    if lease := deps.store.active_lease(bundle.issue.identifier):
        reasons.append(_concurrent(lease))
    if (status := gates.classify(reasons)) is not None:
        return _decision(record, status, reasons, "deterministic gates failed", bundle, approval)
    # Phase 7A: required catalog artifacts must resolve to verified, unambiguous inputs before planning.
    from research_autopilot.catalog.integration import planner_context, resolve_for_task
    resolution, problems = resolve_for_task(deps.catalog, bundle.spec.input_artifacts)
    if problems:
        catalog_reasons = [BlockedReason(code=BlockedCode.DATA_INCONSISTENCY, message=f"artifact catalog: {p}")
                           for p in problems[:20]]
        return _decision(record, DecisionStatus.BLOCKED, catalog_reasons,
                         "required catalog artifacts are not reusable", bundle, approval)
    if resolution is not None:
        record.events.append(RunEvent(kind="catalog", message=f"resolved {len(resolution.artifacts)} artifact(s) "
                                                              f"on {resolution.environment}"))
    if mode == "check_only":
        return _decision(record, DecisionStatus.READY, [], "all deterministic gates passed (check only; no plan generated)",
                         bundle, approval)

    if not deps.store.acquire_lease(bundle.issue.identifier, record.plan_run_id, s.lease_ttl_seconds):
        return _decision(record, DecisionStatus.BLOCKED, [_concurrent(deps.store.active_lease(bundle.issue.identifier))],
                         "another run holds this task", bundle, approval)
    record.events.append(RunEvent(kind="lease", message=f"acquired lease on {bundle.issue.identifier}"))
    try:
        spec = bundle.spec
        if resolution is not None:
            spec = spec.model_copy(update={"input_artifacts": "\n\n".join(
                x for x in (spec.input_artifacts, planner_context(resolution)) if x)})
        reports = []
        if spec.literature:  # Phase 8: the task asks for literature research before planning
            reports += _research(deps, record, [(spec.literature, True)], bundle.issue.identifier, spec.purpose)
            spec = _with_evidence(bundle.spec, spec, reports)
        spec, external = _with_external(deps, record, bundle.spec, spec, reports)  # Phase 9 (Registry only)
        outcome = run_planning(spec, deps.planner, deps.reviewer, s, record, seed=seed)
        requests = (outcome.plan.plan.literature_requests if outcome.status is DecisionStatus.READY and outcome.plan
                    else [])
        if requests and deps.literature is not None:  # the Planner asked for evidence: research, re-plan once
            reports += _research(deps, record, [(q, False) for q in requests[:2]], bundle.issue.identifier, spec.purpose)
            spec = _with_evidence(bundle.spec, spec, reports)
            spec, external = _with_external(deps, record, bundle.spec, spec, reports)
            outcome = run_planning(spec, deps.planner, deps.reviewer, s, record)
        if outcome.status is not DecisionStatus.READY:
            return _decision(record, outcome.status, outcome.reasons, _loop_rationale(outcome), bundle, approval, outcome)

        # Final check against fresh data: state, approval, dependencies and the
        # spec itself may have changed while the agents were running.
        try:
            fresh = deps.manager.load(bundle.issue.identifier)
        except ConnectorError as exc:
            return _connector_failure(record, exc, bundle, approval, outcome)
        changed = list(fresh.reasons)
        if fresh.page and bundle.page and fresh.page.last_edited_time != bundle.page.last_edited_time:
            changed.append(BlockedReason(code=BlockedCode.STATE_CHANGED,
                                         message="Notion spec was edited during planning; re-run to plan against the new spec"))
        if changed:
            for r in changed:
                r.details.setdefault("detected", "final_recheck")
            return _decision(record, gates.classify(changed), changed,
                             "task changed while planning; final re-check failed", fresh, approval, outcome)

        decision = _decision(record, DecisionStatus.READY, [], "deterministic gates passed and Reviewer approved the plan",
                             fresh, approval, outcome)
        decision.linear_update = _update_linear(deps, record, fresh, mode)
        if decision.linear_update.action == "failed":
            transient = "transient" in (decision.linear_update.error or "")
            decision.status = DecisionStatus.TRANSIENT_FAILURE if transient else DecisionStatus.BLOCKED
            decision.reasons.append(BlockedReason(
                code=BlockedCode.TRANSIENT_ERROR if transient else BlockedCode.CONNECTOR_ERROR,
                message=f"plan approved but Linear status update failed: {decision.linear_update.error}"))
            decision.rationale = "plan approved; Linear update failed"
            return decision
        record.handoff_path = str(write_json(
            handoff_path(s.state_dir, fresh.issue.identifier, outcome.plan.plan_id),
            ExecutionHandoff(
                task_id=fresh.issue.identifier, plan_id=outcome.plan.plan_id, plan_run_id=record.plan_run_id,
                decision_id=decision.decision_id, sources=decision.sources,
                purpose=bundle.spec.purpose, scientific_invariants=bundle.spec.scientific_invariants,
                acceptance_criteria=bundle.spec.acceptance_criteria,
                artifact_requirements=bundle.spec.artifact_requirements,
                plan=outcome.plan.plan, review=outcome.review.review, human_approval=approval,
                approvals_needed=outcome.plan.plan.approvals_needed,
                open_questions=outcome.plan.plan.open_questions,
                phase2_mode=mode, specification=bundle.spec.specification,
                repository=bundle.spec.repository, file_scope=bundle.spec.file_scope,
                non_goals=bundle.spec.non_goals, input_artifacts=bundle.spec.input_artifacts,
                catalog_resolution=resolution.model_dump(mode="json") if resolution is not None else None,
                literature_reports=[r.report_id for r in reports],
                external_implementations=external,
            )))
        return decision
    finally:
        deps.store.release_lease(bundle.issue.identifier, record.plan_run_id)


def _research(deps: Deps, record: PlanRunRecord, questions: list[tuple[str, bool]], task_id: str,
              purpose: str | None = None) -> list:
    """Run the Researcher for each question; a failure becomes planner-visible context, never a crash."""
    from research_autopilot.literature.integration import seeds_from
    out = []
    for question, _from_spec in questions:
        if deps.literature is None:
            record.events.append(RunEvent(kind="literature", message="literature research requested but not configured"))
            continue
        try:
            report = deps.literature(question, task_id, seeds_from(question), purpose=purpose)
        except Exception as exc:
            record.events.append(RunEvent(kind="literature", message=f"research failed: {type(exc).__name__}: {exc}"[:300]))
            continue
        if not report.reused:
            record.literature_cost_usd = round(record.literature_cost_usd + report.total_cost_usd, 6)
        record.events.append(RunEvent(kind="literature", message=(
            f"report {report.report_id} ({'reused, no new cost' if report.reused else f'${report.total_cost_usd:.4f}'}): "
            f"{len(report.sources)} source(s), {len(report.verified_claims)} verified claim(s), "
            f"{len(report.failures)} failure(s)")))
        out.append(report)
    return out


def _with_external(deps: Deps, record: PlanRunRecord, original, current, reports: list) -> tuple:
    """Onboard reference implementations (Phase 8 reports, GitHub repositories named in the spec) into the Registry
    and show their status to the Planner.  Execution is never granted here."""
    if deps.external is None:
        return current, []
    import re as _re

    from research_autopilot.external import integration as ext
    from research_autopilot.external import registry as ext_registry
    from research_autopilot.external import service as ext_service
    spent = getattr(deps.external.llm, "spent", 0.0)
    entries = []
    try:
        entries = ext.onboard_from_reports(deps.external, reports)
        text = " ".join(x or "" for x in (original.specification, original.input_artifacts, original.literature))
        named = {m.removesuffix(".git") for m in _re.findall(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", text)}
        for name in sorted(named - {e.name for e in entries}):
            try:
                entries.append(ext_service.onboard(deps.external, name))
            except Exception as exc:
                record.events.append(RunEvent(kind="external", message=f"{name}: onboarding failed: {type(exc).__name__}"))
    except Exception as exc:  # never blocks planning
        record.events.append(RunEvent(kind="external", message=f"onboarding failed: {type(exc).__name__}: {exc}"[:300]))
    record.external_cost_usd = round(record.external_cost_usd + getattr(deps.external.llm, "spent", 0.0) - spent, 6)
    for e in entries:
        record.events.append(RunEvent(kind="external", message=f"{e.name}@{e.commit[:12]}: {e.state.value}"))
    if not entries:
        return current, []
    base = (current.input_artifacts or "").split("\n\nEXTERNAL IMPLEMENTATIONS")[0]
    ctx = ext.planner_context(deps.store, entries)
    registered = [ext.describe(deps.store, e) for e in entries
                  if e.state in (ext_registry.EntryState.REGISTERED, ext_registry.EntryState.SUPERSEDED)]
    return current.model_copy(update={"input_artifacts": "\n\n".join(x for x in (base, ctx) if x)}), registered


def _with_evidence(original, current, reports: list):
    """Literature evidence is appended to the planner's view only; the stored specification never changes."""
    if not reports:
        return current
    from research_autopilot.literature.integration import planner_context
    base = (current.input_artifacts or "").split("\n\nLITERATURE EVIDENCE")[0].split("\n\nEXTERNAL IMPLEMENTATIONS")[0]
    return current.model_copy(update={"input_artifacts": "\n\n".join(x for x in (base, planner_context(reports)) if x)})


class _NoCandidate(Exception):
    def __init__(self, reasons: list[BlockedReason]) -> None:
        self.reasons = reasons


def _select_next(deps: Deps, record: PlanRunRecord) -> TaskBundle:
    """First candidate (priority, then creation order) that passes every gate."""
    skipped = []
    for identifier in deps.manager.next_candidates():
        bundle = deps.manager.load(identifier)
        reasons = list(bundle.reasons)
        if lease := deps.store.active_lease(identifier):
            reasons.append(_concurrent(lease))
        if not reasons:
            record.events.append(RunEvent(kind="select", message=f"selected {identifier}"))
            return bundle
        skipped.append({"task_id": identifier, "codes": sorted({r.code.value for r in reasons})})
        record.events.append(RunEvent(kind="select", message=f"skipped {identifier}: {skipped[-1]['codes']}"))
    raise _NoCandidate([BlockedReason(
        code=BlockedCode.NOT_FOUND,
        message=f"no candidate with label {deps.settings.scope_label!r} in state type "
                f"{deps.settings.approved_state_type!r} passed the gates",
        details={"skipped": skipped})])


def _update_linear(deps: Deps, record: PlanRunRecord, bundle: TaskBundle, mode: Mode) -> LinearUpdateResult:
    s, issue = deps.settings, bundle.issue
    if mode != "apply":
        result = LinearUpdateResult(action="skipped_dry_run", from_state=issue.state_name, to_state=s.in_progress_state_name)
    elif issue.state_name == s.in_progress_state_name:
        result = LinearUpdateResult(action="noop", from_state=issue.state_name, to_state=issue.state_name)
    else:
        try:
            state_id, state_type = deps.linear.team_state_id(issue.team_id, s.in_progress_state_name)
            if state_type != "started":
                # Never move a task to completed/canceled from here.
                raise ValueError(f"target state {s.in_progress_state_name!r} has type {state_type}, expected 'started'")
            new_state = deps.linear.set_state(issue.id, state_id)
            result = LinearUpdateResult(action="updated", from_state=issue.state_name, to_state=new_state)
        except TransientConnectorError as exc:
            result = LinearUpdateResult(action="failed", from_state=issue.state_name, error=f"transient: {exc}")
        except (ConnectorError, ValueError) as exc:
            result = LinearUpdateResult(action="failed", from_state=issue.state_name, error=str(exc))
    deps.store.audit_linear(record.plan_run_id, issue.identifier, result)
    record.events.append(RunEvent(kind="linear", message=f"{result.action}: {result.from_state} -> {result.to_state}"))
    return result


# ---------------------------------------------------------------- helpers


def _concurrent(lease: dict | None) -> BlockedReason:
    return BlockedReason(code=BlockedCode.CONCURRENT_RUN, message="another plan run holds this task",
                         details={"plan_run_id": (lease or {}).get("plan_run_id")})


def _connector_failure(record, exc: ConnectorError, bundle=None, approval=None, outcome=None) -> WorkflowDecision:
    record.events.append(RunEvent(kind="error", message=str(exc)))
    if isinstance(exc, TransientConnectorError):
        return _decision(record, DecisionStatus.TRANSIENT_FAILURE,
                         [BlockedReason(code=BlockedCode.TRANSIENT_ERROR, message=str(exc))],
                         "external API temporarily unavailable; safe to retry", bundle, approval, outcome)
    code = BlockedCode.NOT_FOUND if isinstance(exc, NotFoundError) else BlockedCode.CONNECTOR_ERROR
    return _decision(record, DecisionStatus.BLOCKED, [BlockedReason(code=code, message=str(exc))],
                     "external API error that retrying will not fix", bundle, approval, outcome)


def _loop_rationale(outcome: LoopOutcome) -> str:
    return {
        DecisionStatus.BLOCKED: "planning/review did not produce an approved plan",
        DecisionStatus.TRANSIENT_FAILURE: "LLM API temporarily unavailable; safe to retry",
    }.get(outcome.status, outcome.status.value)


def _decision(record: PlanRunRecord, status: DecisionStatus, reasons: list[BlockedReason], rationale: str,
              bundle: TaskBundle | None = None, approval=None, outcome: LoopOutcome | None = None) -> WorkflowDecision:
    sources = []
    if bundle is not None:
        sources.append(bundle.issue.source_ref())
        if bundle.page is not None:
            sources.append(bundle.page.source_ref())
    return WorkflowDecision(
        status=status, task_id=record.task_id, reasons=reasons, rationale=rationale, sources=sources,
        fetched_at=min((x.fetched_at for x in sources), default=None),
        plan_id=outcome.plan.plan_id if outcome and outcome.plan else None,
        plan_run_id=record.plan_run_id, flow_run_id=record.flow_run_id, human_approval=approval,
        reviewer_verdict=outcome.review.review.verdict if outcome and outcome.review else None,
    )


def _finish(deps: Deps, record: PlanRunRecord) -> None:
    calls = record.llm_calls
    costs = [c.estimated_cost_usd for c in calls if c.ok]
    complete = all(c.ok and c.estimated_cost_usd is not None for c in calls)
    record.cost = CostSummary(
        input_tokens=sum(c.input_tokens for c in calls),
        output_tokens=sum(c.output_tokens for c in calls),
        # Phase 8/9 research and onboarding costs belong to the plan run so the Phase 5 chain budget counts them.
        estimated_cost_usd=round((sum(x for x in costs if x is not None) if complete else 0.0)
                                 + record.literature_cost_usd + record.external_cost_usd, 6)
        if complete or not calls else None,
        cost_complete=complete,
    )
    record.finished_at = utcnow()
    path = run_record_path(deps.settings.state_dir, record.task_id, record.plan_run_id)
    record.artifact_path = str(path)
    write_json(path, record)
    deps.store.save_run(record)
