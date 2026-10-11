"""10A inside the Phase 5 engine: plan -> Notion record -> scope approval (Discord) -> versioned authorization.

Approval objects are immutable plan versions identified by a content hash that includes the hash of the task's
Notion text, so an edited spec or a newer plan makes every older Discord button stale.  ``Approval Status`` in
Notion is only a projection of the verified backend decision; projection failures are retried and never gate
execution.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from research_autopilot.autopilot.models import (ApprovalKind, ApprovalRequest, Budget, ResearchAuthorization,
                                                 WorkflowRun, WorkflowStage)
from research_autopilot.models import utcnow
from research_autopilot.research import evidence as evidence_mod
from research_autopilot.research import planner as planner_mod
from research_autopilot.research import scope as scope_mod
from research_autopilot.research.models import Autonomy, PlanStatus, ResearchPlan
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
                 f"Task: {bundle.page.url if bundle.page else ''}", f"Plan: {plan.plan_page_url or ''}"],
                controls=True, steps=input_steps(plan))
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
            # an unstated limit is no limit (0.0 would read as "exhausted" and loop on budget requests)
            budget=Budget(api_usd=s.budget.api_usd if s.budget.api_usd is not None else NO_LIMIT,
                          compute_cpu_hours=s.budget.cpu_hours if s.budget.cpu_hours is not None else NO_LIMIT),
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
    elif approval.decision == "reject" and plan.kind == "scope_change":
        # a rejected scope change keeps the approved scope: the research step stops with a report and Resume / Cancel
        plan.status = PlanStatus.REJECTED
        save_plan(deps.store, plan)
        project(deps, plan, "Rejected")
        if run is not None:
            run.stage, run.waiting_for, run.resume_stage = WorkflowStage.RESEARCHING, None, None
            run.event("rejected", f"scope change v{plan.version} rejected by {approval.actor}: the approved scope stays")
    elif approval.decision == "reject":
        # Reject stops; only an explicit Cancel ends the research (docs/phase_specs/ux_policy.md, user decision)
        plan.status = PlanStatus.REJECTED
        save_plan(deps.store, plan)
        project(deps, plan, "Rejected")
        if run is not None:
            run.stage, run.waiting_for = WorkflowStage.BLOCKED, None
            run.blocked_reason = f"research plan v{plan.version} rejected by {approval.actor}"
            run.event("rejected", f"research scope v{plan.version} rejected by {approval.actor}: stopped (Resume / Cancel)")
        _notify(deps, run, "research-rejected", plan,
                [f"研究計画 v{plan.version} が却下されたため、研究を停止しました（打ち切りではありません）。",
                 *([f"コメント：{approval.comment[:300]}"] if approval.comment else []),
                 f"Notion: {plan.plan_page_url or ''}"],
                controls=True, steps=[
                    "仕様（Notion のタスクページ）を直して保存すると、数分以内に新しい版の計画が届きます。",
                    "仕様は変えずに計画だけ作り直す場合は Resume を押します。",
                    "研究をやめる場合は Cancel を押します（確認のあと打ち切り、Linear も Canceled になります）。"])
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
        try:
            spec = scope_mod.spec_text(deps.plan.manager.load(plan.task_id).spec)
        except Exception:  # the summary only loses the spec-vs-plan quantity check
            spec = ""
        page_id = existing or deps.notion_writer.create_subpage(plan.notion_page_id, title, plan_blocks(plan, spec))[0]
        plan.plan_page_id = page_id
        plan.plan_page_url = deps.notion_writer.page_url(page_id)
    except Exception as exc:  # the backend record is authoritative; the Notion copy is retried on the next version
        plan.validation_problems.append(f"Notion plan page could not be written: {type(exc).__name__}")


_STATUS_JA = {"stated": "仕様に記載", "inferred": "推定", "ra_default": "RA 既定値", "missing": "仕様に記載なし",
              "ambiguous": "曖昧（要確認）"}
_KIND_JA = {"implementation": "実装", "experiment": "計算", "analysis": "解析"}
_APPROX = re.compile(r"(おおむね|約|およそ|approximately|about|roughly|~)\s*(\d+)\s*(点|個|geometr\w*|points?)", re.I)


def _num_key(v: str):
    try:
        return (0, float(v))
    except ValueError:
        return (1, v)


def review_points(plan: ResearchPlan, spec_text: str = "") -> list[str]:
    """What the approver should actually check (docs/phase_specs/ux_policy.md): the values the first cycle will
    use, gaps between approximate quantities in the specification and the plan, and what is not in the
    specification (RA defaults / missing), budget against the estimate, and open questions."""
    s, out = plan.scope, []
    counts: dict[str, int] = {}
    for c in s.conditions:
        if c.kind not in ("range", "values"):
            continue
        used = sorted({v for t in plan.tasks for v in t.parameters.get(c.name, [])}, key=_num_key)
        if used:
            counts[c.name] = len(used)
            out.append(f"最初のサイクルで使う {c.name}: {', '.join(used)} {c.unit or ''}（{len(used)} 点）。"
                       f"承認範囲は {_cond(c)}。")
    for m in _APPROX.finditer(spec_text or ""):
        n = int(m.group(2))
        if counts and all(k != n for k in counts.values()):
            out.append(f"仕様は「{m.group(0)}」ですが、計画は {', '.join(f'{k} 点' for k in counts.values())} です。"
                       "意図どおりか確認してください（違う場合は Request revision のコメントで点数を指定）。")
    defaults = Autonomy()
    a = s.autonomy
    nums = (a.max_cycles, a.max_new_tasks_per_cycle, a.max_retries_per_task, a.no_progress_cycles)
    if nums == (defaults.max_cycles, defaults.max_new_tasks_per_cycle, defaults.max_retries_per_task,
                defaults.no_progress_cycles):
        out.append(f"自律上限は仕様に数値がないため RA 既定値です：最大 {a.max_cycles} サイクル、1 サイクルあたり新規タスク "
                   f"{a.max_new_tasks_per_cycle} 件、再試行 {a.max_retries_per_task} 回、進展なし "
                   f"{a.no_progress_cycles} サイクルで停止。")
    b = s.budget
    est = sum(t.estimate_cpu_hours for t in plan.tasks)
    out.append(f"予算：CPU {b.cpu_hours:g} 時間 / API ${b.api_usd:g}（{_STATUS_JA.get(b.status, b.status)}"
               f"{'、' + b.note if b.note else ''}）。最初のサイクルの見積もり CPU {est:.2f} 時間。")
    gaps = [(name, item) for name, item in (("比較の基準・公平性（baseline）", s.baseline), ("目的", s.objective),
                                            ("主指標", s.primary_metric), ("対象", s.targets))
            if item.status in ("missing", "ra_default", "inferred", "ambiguous")]
    for name, item in gaps:
        out.append(f"{name}：{_STATUS_JA.get(item.status, item.status)}"
                   + (f"（{item.value[:120]}）" if item.value else "") + "。")
    seen, questions = {"autonomy"} if "自律上限" in " ".join(out) else set(), []
    seen |= {"baseline"} if any(name.startswith("比較の基準") for name, _ in gaps) else set()
    for q in s.open_questions:
        if q.field not in seen:
            seen.add(q.field)
            questions.append(f"{'【要回答】' if q.blocking else ''}{q.question[:200]}")
    if questions:
        out.append("未解決の質問（承認は止めません）：" + " / ".join(questions[:4]))
    return out


def plan_blocks(plan: ResearchPlan, spec_text: str = "") -> list[dict]:
    s = plan.scope
    def src(item) -> str:
        return f"{_STATUS_JA.get(item.status, item.status)}" + (f"：「{item.quote[:160]}」" if item.quote else "")
    rows = [["目的", s.objective.value, src(s.objective)], ["主指標", s.primary_metric.value, src(s.primary_metric)],
            ["対象", s.targets.value, src(s.targets)], ["比較の基準・公平性", s.baseline.value, src(s.baseline)],
            *[["仮説", h.value, src(h)] for h in s.hypotheses],
            *[["不変条件", i.value, src(i)] for i in s.invariants],
            *[["成功条件", c.value, src(c)] for c in s.success_criteria],
            *[["停止条件", c.value, src(c)] for c in s.stop_conditions]]
    rows = [[a, (b or "-")[:300], c[:300]] for a, b, c in rows]
    a = s.autonomy
    blocks = [heading("確認してほしい点"), *[bullet(x) for x in review_points(plan, spec_text)],
              heading("最初のサイクルで実行すること"),
              *([bullet(f"{t.key}（{_KIND_JA.get(t.kind, t.kind)}）{t.title[:120]}"
                        + (f" — {'; '.join(f'{k} = {', '.join(v)}' for k, v in t.parameters.items())}" if t.parameters else "")
                        + f"、見積もり CPU {t.estimate_cpu_hours:g} 時間"
                        + ("、ここで次の計画を判断" if t.decision_point else "")
                        + (f"（{', '.join(t.depends_on)} の後）" if t.depends_on else "")) for t in plan.tasks]
                or [paragraph("（なし）")]),
              heading("承認の仕方"),
              bullet("Discord の approvals チャンネルで押します（このページや Approval Status を編集しても承認にはなりません）。"),
              bullet("Approve：この版の範囲で自律実行を始めます。"),
              bullet("Request revision：コメントに直してほしい点（例：「初期計算は 5 点」）を書くと、作り直した版が届きます。"),
              bullet("Reject：研究を停止します（打ち切りではありません。仕様を直すと作り直され、Resume でも再開できます）。"),
              bullet("仕様ページを編集すると、この版への承認は無効になり、新しい版が自動で届きます。")]
    if s.open_questions:
        blocks += [heading("未解決の質問"), *[bullet(f"{'【要回答】' if q.blocking else ''}{q.field}: {q.question}")
                                              for q in s.open_questions]]
    if plan.validation_problems:
        blocks += [heading("検証で見つかった問題"), *[bullet(x) for x in plan.validation_problems[:20]]]
    if plan.change_summary:
        blocks += [heading("承認済みスコープからの変更"), *[bullet(x) for x in plan.change_summary]]
    blocks += [heading("研究スコープ（詳細）"), table(["項目", "内容", "根拠"], rows),
               heading("承認される条件範囲"),
               *([bullet(f"{_cond(c)}（{_STATUS_JA.get(c.status, c.status)}：「{c.quote[:160]}」）") for c in s.conditions]
                 or [paragraph("（なし）")]),
               heading("自律実行の上限"),
               bullet(f"最大 {a.max_cycles} サイクル / 1 サイクルあたり新規タスク {a.max_new_tasks_per_cycle} 件 / 再試行 "
                      f"{a.max_retries_per_task} 回 / 進展なし {a.no_progress_cycles} サイクルで停止"),
               *[bullet(f"許可する行動：{x}") for x in a.allowed_actions],
               heading("計画の理由"), paragraph(plan.rationale[:1900] or "-"),
               heading("タスクの詳細"),
               *[toggle(f"{t.key}: {t.title[:150]}", [paragraph(t.objective[:1900]), paragraph(t.specification[:1900]),
                                                      *[bullet(x) for x in t.acceptance_criteria[:10]],
                                                      paragraph(f"repository {t.repository}; files {t.file_scope}; "
                                                                f"evidence {t.evidence_ids}")]) for t in plan.tasks],
               toggle("再計画のルール / 前提 / 根拠", [
                   *([bullet(r) for r in plan.replanning_rules] or [paragraph("再計画のルール：-")]),
                   *[bullet(f"前提：{x}") for x in plan.assumptions],
                   table(["id", "種類", "状態", "参照", "内容"],
                         [[e.evidence_id, e.kind, e.status, e.ref[:80], e.statement[:250]] for e in plan.evidence]
                         or [["-"] * 5])]),
               paragraph(f"版 v{plan.version}（{plan.kind}）/ content hash {plan.content_hash} / spec hash "
                         f"{plan.spec_hash[:12]} / {plan.status.value} / 作成 {plan.created_at:%Y-%m-%d %H:%M} UTC")]
    return blocks


# ---------------------------------------------------------------- notifications (approvals / milestones / anomalies)


def _notify(deps, run: WorkflowRun | None, key: str, plan: ResearchPlan, details: list[str], *, milestone: bool = False,
            controls: bool = False, steps: list[str] | None = None) -> None:
    """``controls``: a human must act (approvals channel, Resume / Cancel buttons, numbered ``steps``)."""
    from research_autopilot.notifications import notify_once
    from research_autopilot.notifications.events import EventKind, NotificationEvent
    kind = EventKind.PHASE_TRANSITION if milestone else EventKind.UNRECOVERABLE_ERROR
    try:
        notify_once(deps.store, NotificationEvent(kind=kind, flow_name="research", flow_run_name=plan.task_id,
                                                  state_name=plan.status.value, details=[x for x in details if x][:12],
                                                  dedupe_key=f"{key}:{plan.plan_id}",
                                                  control_task_id=plan.task_id if controls else None,
                                                  control_actions=["resume", "cancel"] if controls else [],
                                                  needs_human=controls, steps=list(steps or [])), deps.notifier)
    except Exception:
        pass


# Which Notion section of the research task answers a blocking question (docs/phase_specs/ux_policy.md 1.5).
_SECTION = {"budget": "Budget / Authorization", "invariants": "Scientific Invariants",
            "success_criteria": "Success Criteria", "stop_conditions": "Stop Conditions", "autonomy": "Autonomy"}


def section_for(field: str) -> str:
    """objective, primary metric, targets, conditions, hypotheses and baseline live in 'Research Objective'."""
    return _SECTION.get(field.split(".", 1)[0], "Research Objective")


def input_steps(plan: ResearchPlan) -> list[str]:
    """Numbered steps until a needs-input / invalid plan resumes: what to write in which Notion section."""
    if plan.status is PlanStatus.NEEDS_INPUT:
        by_section: dict[str, list[str]] = {}
        for q in plan.scope.blocking():
            by_section.setdefault(section_for(q.field), []).append(q.question)
        steps = [f"In the Notion section '{sec}' of the research task, answer: " + " / ".join(qs)[:400]
                 for sec, qs in by_section.items()]
    else:
        steps = [f"Fix the Notion research task so that this is resolved: {p[:300]}" for p in plan.validation_problems[:3]]
    return [*steps,
            "Write every number (range, count, budget limit) in the text itself, e.g. 'bond length R between 0.5 and "
            "3.0 Angstrom' or 'Compute budget: 1 CPU-hour in total; API budget 5 USD': RA never fills in numbers.",
            "Save the page: RA re-plans automatically within a few minutes and sends the plan for approval here.",
            "If nothing arrives, press Resume. Cancel ends the research."]


NO_LIMIT = 1e9  # policy Budget has no "unlimited": a limit the approved scope does not state


def llm_factory_from_env(settings) -> Callable[[], Any]:
    from research_autopilot.literature.researcher import PricedLlm
    model = os.environ.get("RA_RESEARCH_MODEL", "").strip() or settings.planner_model
    budget = float(os.environ.get("RA_RESEARCH_STEP_BUDGET_USD") or 2.0)
    return lambda: PricedLlm(model, settings.model_pricing, budget, settings.agents_tracing)
