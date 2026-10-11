"""10A inside the Phase 5 engine: plan -> Notion record -> scope approval (Discord) -> versioned authorization.

Approval objects are immutable plan versions identified by a content hash that includes the hash of the task's
Notion text, so an edited spec or a newer plan makes every older Discord button stale.  ``Approval Status`` in
Notion is only a projection of the verified backend decision; projection failures are retried and never gate
execution.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable

from research_autopilot.autopilot.models import (ApprovalKind, ApprovalRequest, Budget, ResearchAuthorization,
                                                 WorkflowRun, WorkflowStage)
from research_autopilot.models import utcnow
from research_autopilot.research import evidence as evidence_mod
from research_autopilot.research import planner as planner_mod
from research_autopilot.research import scope as scope_mod
from research_autopilot.research.models import PlanStatus, ResearchPlan
from research_autopilot.storage import db
from research_autopilot.tasks.notion_writer import bullet, heading, paragraph, table, toggle


@dataclass
class ResearchDeps:
    llm_factory: Callable[[], Any]    # a fresh priced LLM (with its own budget) per planning / evaluation step
    evaluator: Any = None             # research.evaluator.ResearchEvaluator (10B); None -> uses llm_factory
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------- persistence


def plans(store, task_id: str) -> list[ResearchPlan]:
    return sorted((ResearchPlan.model_validate(r) for r in store.find(db.research_plans, task_id=task_id)),
                  key=lambda p: p.version)


def latest_plan(store, task_id: str) -> ResearchPlan | None:
    ps = plans(store, task_id)
    return ps[-1] if ps else None


def save_plan(store, plan: ResearchPlan) -> None:
    store.put(db.research_plans, plan.plan_id, plan.model_dump(mode="json"), task_id=plan.task_id,
              version=plan.version, status=plan.status.value)


def get_plan(store, plan_id: str) -> ResearchPlan | None:
    raw = store.get(db.research_plans, plan_id)
    return ResearchPlan.model_validate(raw) if raw else None


def research_authorization(store, task_id: str) -> ResearchAuthorization | None:
    """The current (latest, not superseded / revoked) research-scope authorization of a research objective."""
    auths = [ResearchAuthorization.model_validate(r) for r in store.find(db.authorizations, root_task_id=task_id)]
    auths = [a for a in auths if a.kind == "research_scope" and not a.revoked and not a.superseded_by]
    return max(auths, key=lambda a: a.version) if auths else None


def _next_auth_version(store, task_id: str) -> int:
    """Versions are never reused (revoked / superseded authorizations keep theirs)."""
    vs = [r.get("version", 1) for r in store.find(db.authorizations, root_task_id=task_id)
          if r.get("kind") == "research_scope"]
    return max(vs, default=0) + 1


def authorization_problems(store, auth: ResearchAuthorization | None, spec) -> list[str]:
    """Re-checked before execution, before every replanning step and on resume."""
    if auth is None:
        return ["no approved research scope (ResearchAuthorization) for this research objective"]
    out = []
    if auth.revoked or auth.superseded_by:
        out.append(f"authorization v{auth.version} is no longer current")
    if auth.expires_at < utcnow():
        out.append(f"authorization v{auth.version} expired")
    plan = get_plan(store, auth.plan_id) if auth.plan_id else None
    if plan is None or plan.content_hash != auth.content_hash:
        out.append("the approved plan version is missing or was changed after approval")
    elif spec is None or scope_mod.spec_hash(spec) != plan.spec_hash:
        out.append("the Notion specification changed after approval: the research scope must be re-approved")
    return out


# ---------------------------------------------------------------- 10A planning


def plan_research(deps, run: WorkflowRun, bundle, *, revision_comment: str = "") -> ResearchPlan:
    """Build plan v(N+1), record it in Notion, and request a scope approval (or block when input is missing)."""
    store = deps.store
    spec = bundle.spec
    prev = latest_plan(store, run.task_id)
    llm = deps.research.llm_factory()
    text = scope_mod.spec_text(spec)
    ext_entries = []
    if getattr(deps.exp, "external", None) is not None:
        from research_autopilot.external import registry as ext_registry
        ext_entries = ext_registry.entries(store)
    ev = evidence_mod.collect(spec_text=text, store=store, catalog=getattr(deps.plan, "catalog", None),
                              literature_reports=evidence_mod.literature_reports(run.task_id, text),
                              external_entries=ext_entries, registry=deps.impl.registry)
    plan = planner_mod.build_plan(llm, spec, task_id=run.task_id, default_budget=deps.settings.default_budget,
                                  notion_page_id=bundle.page.page_id if bundle.page else None, evidence=ev,
                                  registry=deps.impl.registry, version=(prev.version + 1) if prev else 1,
                                  previous=prev, revision_comment=revision_comment)
    if prev is not None and prev.status in (PlanStatus.PENDING_APPROVAL, PlanStatus.DRAFT, PlanStatus.NEEDS_INPUT,
                                            PlanStatus.INVALID):  # human decisions (revise / reject) stay recorded
        prev.status = PlanStatus.SUPERSEDED
        save_plan(store, prev)
    run.research_cost_usd = round(getattr(run, "research_cost_usd", 0.0) + plan.cost_usd, 6)
    _record_page(deps, plan)
    save_plan(store, plan)
    run.event("research_plan", f"plan v{plan.version} {plan.status.value} ({plan.content_hash[:12]}), "
                               f"{len(plan.tasks)} task(s), ${plan.cost_usd:.4f}")
    if plan.status in (PlanStatus.NEEDS_INPUT, PlanStatus.INVALID):
        why = ([f"{q.field}: {q.question}" for q in plan.scope.blocking()] if plan.status is PlanStatus.NEEDS_INPUT
               else plan.validation_problems)
        run.stage = WorkflowStage.BLOCKED
        run.blocked_reason = (f"research plan v{plan.version} {plan.status.value}: " + "; ".join(why))[:900]
        _notify(deps, run, "research-needs-input", plan,
                [f"Plan v{plan.version} cannot be approved yet ({plan.status.value})", *why[:6],
                 f"Notion: {plan.plan_page_url or ''}",
                 "Edit the Notion specification, then press Run Autopilot again."], controls=False)
        return plan
    req = request_approval(deps, bundle, plan)
    plan.status, plan.approval_request_id = PlanStatus.PENDING_APPROVAL, req.request_id
    save_plan(store, plan)
    project(deps, plan, "Pending")
    run.stage = WorkflowStage.AWAITING_AUTHORIZATION
    run.waiting_for = req.request_id
    run.resume_stage = WorkflowStage.RESEARCHING.value
    if req.request_id not in run.approval_request_ids:
        run.approval_request_ids.append(req.request_id)
    return plan


def request_approval(deps, bundle, plan: ResearchPlan) -> ApprovalRequest:
    s = plan.scope
    lines = [f"Notion plan: {plan.plan_page_url or bundle.page.url if bundle.page else ''}",
             f"Version v{plan.version} / hash {plan.content_hash[:12]} ({plan.kind})",
             f"Objective: {s.objective.value[:200]}",
             f"Primary metric: {s.primary_metric.value[:160]}",
             f"Tasks: {len(plan.tasks)} ({', '.join(t.key + ':' + t.kind for t in plan.tasks)[:200]})",
             f"Conditions: " + "; ".join(_cond(c) for c in s.conditions)[:300],
             f"Autonomy: {s.autonomy.max_cycles} cycles, {s.autonomy.max_new_tasks_per_cycle} new tasks/cycle, "
             f"{s.autonomy.max_retries_per_task} retries, stop after {s.autonomy.no_progress_cycles} cycles without progress"]
    open_q = [f"open question: {q.field}: {q.question}" for q in s.open_questions][:5]
    budget = (f"approved budget: {s.budget.cpu_hours if s.budget.cpu_hours is not None else '-'} CPU-h, "
              f"${s.budget.api_usd if s.budget.api_usd is not None else '-'} API"
              + (f" ({s.budget.note})" if s.budget.note else "") + f"; plan estimate "
              f"{plan.estimate_cpu_hours:g} CPU-h, ${plan.estimate_api_usd:.2f}")
    return deps.approvals.request(
        kind=ApprovalKind.RESEARCH_SCOPE, issue=bundle.issue, root_task_id=plan.task_id, spec_hash=plan.content_hash,
        title=f"Approve research scope v{plan.version} for {plan.task_id}: {bundle.spec.title[:80]}",
        what_changes=lines, diff=[*plan.change_summary, *open_q], budget_summary=budget,
        payload={"plan_id": plan.plan_id, "version": plan.version, "plan_url": plan.plan_page_url},
        dedupe=f"plan:{plan.plan_id}")


def _cond(c) -> str:
    if c.kind == "range":
        return f"{c.name} in [{c.minimum}, {c.maximum}] {c.unit or ''}".strip()
    if c.kind == "max_count":
        return f"at most {c.count} {c.applies_to}"
    return f"{c.name} in {c.values}"


def current_approval_hash(deps, req: ApprovalRequest) -> str:
    """The request stays valid only while it targets the newest plan version made from the current spec."""
    plan = get_plan(deps.store, req.payload.get("plan_id", ""))
    newest = latest_plan(deps.store, req.task_id)
    if plan is None or newest is None or newest.plan_id != plan.plan_id:
        return "stale:newer-plan"
    bundle = deps.plan.manager.load(req.task_id)
    if bundle.spec is None or scope_mod.spec_hash(bundle.spec) != plan.spec_hash:
        return "stale:spec-changed"
    return plan.content_hash


def apply_decision(deps, req: ApprovalRequest, approval, issue, run: WorkflowRun | None) -> None:
    """Record a verified scope decision atomically, then project it (Notion / Linear / Discord)."""
    plan = get_plan(deps.store, req.payload["plan_id"])
    if plan is None:
        return
    if approval.decision == "approve":
        prev = research_authorization(deps.store, plan.task_id)
        s = plan.scope
        auth = ResearchAuthorization(
            root_task_id=plan.task_id, approved_by=approval.actor, approval_request_id=req.request_id,
            spec_hash=plan.spec_hash, purpose=s.objective.value, research_target=s.targets.value or None,
            hypothesis="; ".join(h.value for h in s.hypotheses) or None,
            scientific_invariants=[i.value for i in s.invariants],
            acceptance_criteria=[c.value for c in s.success_criteria],
            budget=Budget(api_usd=s.budget.api_usd if s.budget.api_usd is not None else 0.0,
                          compute_cpu_hours=s.budget.cpu_hours if s.budget.cpu_hours is not None else 0.0),
            version=_next_auth_version(deps.store, plan.task_id), kind="research_scope", plan_id=plan.plan_id,
            content_hash=plan.content_hash, scope=s.model_dump(mode="json"))
        deps.store.put(db.authorizations, auth.authorization_id, auth.model_dump(mode="json"), root_task_id=plan.task_id)
        if prev is not None:
            prev.superseded_by = auth.authorization_id
            deps.store.put(db.authorizations, prev.authorization_id, prev.model_dump(mode="json"),
                           root_task_id=prev.root_task_id)
        plan.status = PlanStatus.APPROVED
        save_plan(deps.store, plan)
        project(deps, plan, "Approved")
        if run is not None:
            run.authorization_id = auth.authorization_id
            run.stage, run.waiting_for, run.resume_stage = WorkflowStage.RESEARCHING, None, None
            run.event("approved", f"research scope v{plan.version} approved by {approval.actor} "
                                  f"(authorization v{auth.version})")
            deps.ops.set_state(issue, "In Progress", f"research scope v{plan.version} approved ({req.request_id[:8]})")
        _notify(deps, run, f"research-approved:{plan.plan_id}", plan,
                [f"Research scope v{plan.version} approved by {approval.actor}", f"Notion: {plan.plan_page_url or ''}"],
                milestone=True)
    elif approval.decision == "reject":
        plan.status = PlanStatus.REJECTED
        save_plan(deps.store, plan)
        project(deps, plan, "Rejected")
        if approval.channel == "discord":
            deps.ops.reflect_verified_decision(issue, "Canceled", f"verified Discord reject ({req.request_id[:8]}) by {approval.actor}")
        if run is not None:
            run.stage, run.waiting_for = WorkflowStage.CANCELLED, None
            run.event("rejected", f"research scope v{plan.version} rejected by {approval.actor}")
    else:
        plan.status = PlanStatus.REVISION_REQUESTED
        plan.revision_comment = approval.comment
        save_plan(deps.store, plan)
        project(deps, plan, "Revision Requested")
        if run is not None:  # a revised version is drafted and re-presented on the next tick
            run.stage, run.waiting_for = WorkflowStage.DISCOVERED, None
            run.event("revision", f"revision of v{plan.version} requested by {approval.actor}: {approval.comment[:200]}")


# ---------------------------------------------------------------- Notion record / projection


def project(deps, plan: ResearchPlan, status: str) -> None:
    """Approval Status projection; a failure is remembered and retried by ``retry_projections``."""
    try:
        if plan.notion_page_id:
            deps.notion_writer.set_approval_status(plan.notion_page_id, status)
        _set_pending_projection(deps.store, plan.plan_id, None)
    except Exception:
        _set_pending_projection(deps.store, plan.plan_id, status)


def _set_pending_projection(store, plan_id: str, status: str | None) -> None:
    raw = store.get(db.research_plans, plan_id)
    if raw is None:
        return
    raw["projection_pending"] = status
    store.put(db.research_plans, plan_id, raw, task_id=raw["task_id"], version=raw["version"], status=raw["status"])


def retry_projections(deps) -> list[str]:
    done = []
    for raw in deps.store.find(db.research_plans):
        status = raw.get("projection_pending")
        if status and raw.get("notion_page_id"):
            try:
                deps.notion_writer.set_approval_status(raw["notion_page_id"], status)
                _set_pending_projection(deps.store, raw["plan_id"], None)
                done.append(f"{raw['task_id']} v{raw['version']}: {status}")
            except Exception:
                continue
    return done


def _record_page(deps, plan: ResearchPlan) -> None:
    if not plan.notion_page_id:
        return
    title = f"RA Research Plan v{plan.version} ({plan.content_hash[:12]})"
    try:
        existing = deps.notion_writer.find_subpage(plan.notion_page_id, f"RA Research Plan v{plan.version} ")
        page_id = existing or deps.notion_writer.create_subpage(plan.notion_page_id, title, plan_blocks(plan))[0]
        plan.plan_page_id = page_id
        plan.plan_page_url = deps.notion_writer.page_url(page_id)
    except Exception as exc:  # the backend record is authoritative; the Notion copy is retried on the next version
        plan.validation_problems.append(f"Notion plan page could not be written: {type(exc).__name__}")


def plan_blocks(plan: ResearchPlan) -> list[dict]:
    s = plan.scope
    rows = [["objective", s.objective.value, s.objective.status, s.objective.quote],
            ["primary metric", s.primary_metric.value, s.primary_metric.status, s.primary_metric.quote],
            ["targets", s.targets.value, s.targets.status, s.targets.quote],
            ["baseline / fairness", s.baseline.value, s.baseline.status, s.baseline.quote],
            *[["hypothesis", h.value, h.status, h.quote] for h in s.hypotheses],
            *[["invariant", i.value, i.status, i.quote] for i in s.invariants],
            *[["success criterion", c.value, c.status, c.quote] for c in s.success_criteria],
            *[["stop condition", c.value, c.status, c.quote] for c in s.stop_conditions],
            ["budget", f"{s.budget.cpu_hours} CPU-h / ${s.budget.api_usd}", s.budget.status, s.budget.quote]]
    rows = [[a, (b or "-")[:300], c, (d or "-")[:300]] for a, b, c, d in rows]
    blocks = [paragraph(f"Version v{plan.version} ({plan.kind}) / content hash {plan.content_hash} / spec hash "
                        f"{plan.spec_hash[:12]} / status {plan.status.value} / created {plan.created_at:%Y-%m-%d %H:%M} UTC"),
              paragraph("承認は Discord のボタンでのみ成立します（このページや Approval Status の編集は承認になりません）。"
                        "仕様ページを編集するとこの版への承認は無効になります。"),
              heading("Research scope"), table(["field", "value", "status", "source quote"], rows),
              heading("Approved conditions"),
              *([bullet(f"{c.name}: {_cond(c)} — “{c.quote[:200]}”") for c in s.conditions] or [paragraph("(none)")]),
              heading("Autonomy (approved with this scope)"),
              bullet(f"max cycles {s.autonomy.max_cycles}; max new tasks per cycle {s.autonomy.max_new_tasks_per_cycle}; "
                     f"max retries per task {s.autonomy.max_retries_per_task}; stop after {s.autonomy.no_progress_cycles} "
                     f"cycles without progress ({s.autonomy.status})"),
              *[bullet(a) for a in s.autonomy.allowed_actions]]
    if s.open_questions:
        blocks += [heading("Open questions"), *[bullet(f"{'[blocking] ' if q.blocking else ''}{q.field}: {q.question}")
                                                for q in s.open_questions]]
    if plan.validation_problems:
        blocks += [heading("Validation problems"), *[bullet(x) for x in plan.validation_problems[:20]]]
    if plan.change_summary:
        blocks += [heading("Changes from the approved scope"), *[bullet(x) for x in plan.change_summary]]
    blocks += [heading("Rationale"), paragraph(plan.rationale[:1900] or "-"),
               heading("Task DAG draft"),
               table(["key", "kind", "title", "depends on", "parameters", "est. CPU-h", "decision"],
                     [[t.key, t.kind, t.title[:120], ", ".join(t.depends_on) or "-",
                       "; ".join(f"{k}={','.join(v)}" for k, v in t.parameters.items())[:200] or "-",
                       f"{t.estimate_cpu_hours:g}", "yes" if t.decision_point else ""] for t in plan.tasks] or [["-"] * 7]),
               *[toggle(f"{t.key}: {t.title[:150]}", [paragraph(t.objective[:1900]), paragraph(t.specification[:1900]),
                                                      *[bullet(a) for a in t.acceptance_criteria[:10]],
                                                      paragraph(f"repository {t.repository}; files {t.file_scope}; "
                                                                f"evidence {t.evidence_ids}")]) for t in plan.tasks],
               heading("Replanning rules"), *([bullet(r) for r in plan.replanning_rules] or [paragraph("-")]),
               heading("Assumptions"), *([bullet(a) for a in plan.assumptions] or [paragraph("-")]),
               heading("Evidence"),
               table(["id", "kind", "status", "reference", "statement"],
                     [[e.evidence_id, e.kind, e.status, e.ref[:80], e.statement[:250]] for e in plan.evidence] or [["-"] * 5])]
    return blocks


# ---------------------------------------------------------------- notifications (approvals / milestones / anomalies)


def _notify(deps, run: WorkflowRun | None, key: str, plan: ResearchPlan, details: list[str], *, milestone: bool = False,
            controls: bool = True) -> None:
    from research_autopilot.notifications import notify_once
    from research_autopilot.notifications.events import EventKind, NotificationEvent
    kind = EventKind.PHASE_TRANSITION if milestone else EventKind.UNRECOVERABLE_ERROR
    try:
        notify_once(deps.store, NotificationEvent(kind=kind, flow_name="research", flow_run_name=plan.task_id,
                                                  state_name=plan.status.value, details=details[:12],
                                                  dedupe_key=f"{key}:{plan.plan_id}",
                                                  control_task_id=None), deps.notifier)
    except Exception:
        pass


def llm_factory_from_env(settings) -> Callable[[], Any]:
    from research_autopilot.literature.researcher import PricedLlm
    model = os.environ.get("RA_RESEARCH_MODEL", "").strip() or settings.planner_model
    budget = float(os.environ.get("RA_RESEARCH_STEP_BUDGET_USD") or 2.0)
    return lambda: PricedLlm(model, settings.model_pricing, budget, settings.agents_tracing)
