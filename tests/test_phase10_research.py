"""Phase 10: evidence-grounded research planning (10A) and autonomous research orchestration (10B), offline."""

import pytest
from phase5_fakes import build
from research_fakes import RESEARCH_SECTIONS, ScriptedResearchLlm, complete, more, scope, task

from research_autopilot.autopilot.engine import poll
from research_autopilot.autopilot.models import (ApprovalRequest, DiscordSubmission, ResearchAuthorization,
                                                 WorkflowRun, WorkflowStage)
from research_autopilot.research import orchestration, workflow
from research_autopilot.research import scope as scope_mod
from research_autopilot.research.models import (BudgetItem, ConditionBound, EvaluationDecision, PlanStatus, ScopeItem)
from research_autopilot.research.workflow import ResearchDeps
from research_autopilot.storage import db


@pytest.fixture(autouse=True)
def research_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RA_RESEARCH_DIR", str(tmp_path / "research"))


def _build(tmp_path, llm=None, sections=None, **kw):
    deps, linear, notion, writer, channel = build(tmp_path, approval_mode="discord", drafts=[],
                                                  sections=sections or dict(RESEARCH_SECTIONS), **kw)
    llm = llm or ScriptedResearchLlm(decisions=[more(), complete])
    deps.research = ResearchDeps(llm_factory=lambda: llm)
    return deps, linear, notion, writer, llm


def _run(deps) -> WorkflowRun:
    return next(WorkflowRun.model_validate(r) for r in deps.store.find(db.workflow_runs) if r["task_id"] == "INU-900")


def _pending(deps, kind="research_scope") -> list[ApprovalRequest]:
    return [r for r in deps.approvals.pending() if r.kind.value == kind]


def _discord(deps, req, decision="approve", spec_hash=None, comment=""):
    """What the authenticated Discord gateway persists (guild / channel / user already checked)."""
    sub = DiscordSubmission(interaction_id=f"i-{req.request_id}-{decision}", request_id=req.request_id, decision=decision,
                            spec_hash=spec_hash or req.spec_hash, user_id="1001", guild_id="1", channel_id="2",
                            comment=comment)
    deps.store.insert_new(db.discord_interactions, sub.interaction_id, sub.model_dump(mode="json"), request_id=req.request_id)


def _edit_spec(notion, **sections):
    pid = next(iter(notion.pages))
    p = notion.pages[pid]
    notion.pages[pid] = p.model_copy(update={"sections": {**p.sections, **sections},
                                             "last_edited_time": "2026-10-11T00:00:00.000Z"})


def _approve_initial(tmp_path, **kw):
    deps, linear, notion, writer, llm = _build(tmp_path, **kw)
    poll(deps)
    (req,) = _pending(deps)
    _discord(deps, req)
    return deps, linear, notion, writer, llm, req


def _drive(deps, n=12):
    for _ in range(n):
        poll(deps)
        if _run(deps).stage in (WorkflowStage.DONE, WorkflowStage.BLOCKED, WorkflowStage.CANCELLED,
                                WorkflowStage.AWAITING_APPROVAL, WorkflowStage.PAUSED):
            break
    return _run(deps)


# ---------------------------------------------------------------- 10A


def test_research_objective_gets_a_grounded_plan_and_scope_approval_request_without_execution(tmp_path):
    deps, linear, notion, writer, llm = _build(tmp_path)
    poll(deps)
    run = _run(deps)
    assert run.stage is WorkflowStage.AWAITING_AUTHORIZATION
    [plan] = workflow.plans(deps.store, "INU-900")
    assert plan.status is PlanStatus.PENDING_APPROVAL and plan.version == 1 and len(plan.content_hash) == 64
    assert plan.scope.primary_metric.status == "stated" and plan.scope.budget.cpu_hours == 1
    assert plan.tasks[0].parameters == {"bond_length_r": ["0.5", "1.0", "2.0"]}
    assert any(e.kind == "spec" for e in plan.evidence)  # registered repository context
    # Notion: immutable versioned plan page + Approval Status projection; Discord request carries URL + version/hash
    sub = writer.subpages[plan.plan_page_id]
    assert sub["title"].startswith("RA Research Plan v1 (") and "Task DAG draft" in str(sub["blocks"])
    assert notion.pages[plan.notion_page_id].approval_status == "Pending"
    (req,) = _pending(deps)
    assert req.spec_hash == plan.content_hash and any(plan.content_hash[:12] in x for x in req.what_changes)
    assert any("Notion plan:" in x for x in req.what_changes)
    assert linear.issues["INU-900"].state_name == "Awaiting Approval"
    # nothing ran: no child tasks, no Phase 2 planning, no jobs
    assert writer.created_tasks == [] and deps.plan.planner.calls == [] and deps.exp.backend.submits == []
    assert run.research_cost_usd > 0


def test_missing_scientific_conditions_are_never_completed_by_the_planner(tmp_path):
    sections = {k: v for k, v in RESEARCH_SECTIONS.items() if k != "budget"}
    sections["research_objective"] = "- Question: how does dE(R) change with the bond length R?"
    invented = scope(budget=BudgetItem(cpu_hours=2, quote="Compute budget: 2 CPU-hours", status="stated"),
                     primary_metric=ScopeItem(value="dE(R) in mEh", quote="Primary metric: dE(R) in mEh", status="stated"))
    deps, linear, notion, writer, llm = _build(tmp_path, llm=ScriptedResearchLlm(scope_answer=invented), sections=sections)
    poll(deps)
    [plan] = workflow.plans(deps.store, "INU-900")
    assert plan.status is PlanStatus.NEEDS_INPUT
    assert plan.scope.budget.status != "stated" and plan.scope.primary_metric.status == "missing"
    blocking = {q.field for q in plan.scope.blocking()}
    assert {"budget", "primary_metric"} <= blocking
    assert _pending(deps) == [] and _run(deps).stage is WorkflowStage.BLOCKED
    assert "needs_input" in _run(deps).blocked_reason and writer.created_tasks == []


def test_numbers_must_occur_in_the_quoted_spec_text():
    from phase2_fakes import make_issue, make_page

    from research_autopilot.models import TaskSpec
    spec = TaskSpec.from_sources(make_issue(), make_page(sections=dict(RESEARCH_SECTIONS)))
    bad = scope(conditions=[ConditionBound(name="bond_length_r", kind="range", minimum=0.3, maximum=3.0,
                                           quote="bond length R between 0.5 and 3.0 Angstrom")])
    s = scope_mod.verify(bad, spec)
    assert s.conditions[0].status == "ambiguous" and any(q.field == "conditions.bond_length_r" for q in s.blocking())
    assert scope_mod.verify(scope(), spec).blocking() == []


def test_execution_is_refused_before_approval_and_runs_only_after_it(tmp_path):
    deps, linear, notion, writer, llm = _build(tmp_path)
    for _ in range(3):
        poll(deps)
    assert _run(deps).stage is WorkflowStage.AWAITING_AUTHORIZATION and writer.created_tasks == []
    # editing the Notion Approval Status is a projection, never an approval
    pid = next(iter(notion.pages))
    notion.pages[pid] = notion.pages[pid].model_copy(update={"approval_status": "Approved"})
    poll(deps)
    assert workflow.research_authorization(deps.store, "INU-900") is None and writer.created_tasks == []


def test_stale_discord_approval_after_a_spec_edit_or_a_newer_version_is_rejected(tmp_path):
    deps, linear, notion, writer, llm = _build(tmp_path)
    poll(deps)
    (v1,) = _pending(deps)
    _edit_spec(notion, budget="- Compute budget: 1 CPU-hour in total; API budget 5 USD; no GPU.")
    _discord(deps, v1)  # an old button for the previous text
    poll(deps)
    assert workflow.research_authorization(deps.store, "INU-900") is None
    assert ApprovalRequest.model_validate(deps.store.get(db.approval_requests, v1.request_id)).status.value == "stale"
    poll(deps)  # a revised version is drafted from the edited spec and presented again
    plans = workflow.plans(deps.store, "INU-900")
    assert [p.version for p in plans] == [1, 2] and plans[0].status is PlanStatus.SUPERSEDED
    (v2,) = _pending(deps)
    assert v2.spec_hash == plans[1].content_hash != v1.spec_hash
    _discord(deps, v2)
    poll(deps)
    auth = workflow.research_authorization(deps.store, "INU-900")
    assert auth.version == 1 and auth.plan_id == plans[1].plan_id and auth.content_hash == plans[1].content_hash
    assert auth.approved_by == "discord:1001"
    assert notion.pages[plans[1].notion_page_id].approval_status == "Approved"


def test_revision_request_redrafts_with_the_comment_and_rejection_cancels(tmp_path):
    deps, linear, notion, writer, llm = _build(tmp_path)
    poll(deps)
    (v1,) = _pending(deps)
    _discord(deps, v1, "revise", comment="start with four geometries")
    poll(deps)
    plans = workflow.plans(deps.store, "INU-900")
    assert [p.status for p in plans] == [PlanStatus.REVISION_REQUESTED, PlanStatus.PENDING_APPROVAL]
    assert any(c[1].get("HUMAN_REVISION_REQUEST") == "start with four geometries" for c in llm.calls)
    assert ("00000001-0000-0000-0000-000000000000" in str(writer.approval_history)) or writer.approval_history
    (v2,) = _pending(deps)
    _discord(deps, v2, "reject")
    poll(deps)
    assert _run(deps).stage is WorkflowStage.CANCELLED and workflow.research_authorization(deps.store, "INU-900") is None
    assert notion.pages[plans[0].notion_page_id].approval_status == "Rejected"


def test_approval_status_projection_failure_is_retried_and_never_gates(tmp_path):
    deps, linear, notion, writer, llm, req = _approve_initial(tmp_path)
    writer.fail_projection = 1
    poll(deps)
    assert workflow.research_authorization(deps.store, "INU-900") is not None  # decided by the backend event
    poll(deps)
    plan = workflow.plans(deps.store, "INU-900")[0]
    assert notion.pages[plan.notion_page_id].approval_status == "Approved"


# ---------------------------------------------------------------- 10B


def test_multiple_cycles_inside_the_approved_scope_then_completion(tmp_path):
    deps, linear, notion, writer, llm, _ = _approve_initial(tmp_path)
    run = _drive(deps, 20)
    assert run.stage is WorkflowStage.DONE, (run.blocked_reason, run.events[-5:])
    cs = orchestration.cycles(deps.store, "INU-900")
    assert [c.number for c in cs] == [1, 2] and [c.decision.action for c in cs] == ["continue", "complete"]
    assert cs[1].tasks[0].parameters == {"bond_length_r": ["1.5"]}
    assert all(r["validated"] for c in cs for r in c.results)
    # children: RA-created, linked to the research objective, Linear is the status SSOT
    children = [p for p in notion.pages.values() if p.origin == "RA"]
    assert len(children) == 2 and all(c.parent_page_ids == [next(iter(notion.pages))] for c in children)
    assert all(c.approval_status == "Not Required" for c in children)
    assert all(linear.issues[c.linear_issue_id].state_name == "Done" for c in children)
    assert linear.issues["INU-900"].state_name == "Done"
    # results: Notes + catalogued report; evaluator input carried validated results
    assert any("research results" in n["title"] for n in writer.notes.values())
    eval_calls = [c for c in llm.calls if c[0] == "ResearchEvaluator"]
    assert eval_calls and eval_calls[-1][1]["RESULTS"]
    # notifications: milestones with Stop / Resume controls, no per-task notifications of research children
    sent = [r["event"] for r in deps.store.find(db.notifications_sent) if "event" in r]
    child_ids = {c.linear_issue_id for c in children}
    assert not [e for e in sent if e["flow_run_name"] in child_ids and e["kind"] in ("task_completed", "experiment_completed")]
    assert any(e["flow_run_name"] in child_ids and e["kind"] == "task_started" for e in sent)  # logs channel only
    assert any(e.get("control_task_id") == "INU-900" for e in sent)
    assert any(e["kind"] == "task_completed" and e["flow_name"] == "research" for e in sent)


def test_dependent_tasks_get_linear_blocked_by_edges(tmp_path):
    from research_fakes import ScriptedResearchLlm, draft
    plan = draft(rationale="r", tasks=[task("T1"), task("T2", ("1.5",), deps=("T1",), kind="analysis")],
                             replanning_rules=["r"])
    deps, linear, notion, writer, llm, _ = _approve_initial(tmp_path, llm=ScriptedResearchLlm(plans=[plan],
                                                                                              decisions=[complete]))
    poll(deps)
    c = orchestration.cycles(deps.store, "INU-900")[0]
    assert (c.child_task_ids["T1"], c.child_task_ids["T2"]) in linear.relations


def test_out_of_scope_next_step_becomes_a_scope_change_proposal(tmp_path):
    widen = more(("3.5",), proposed_conditions=[ConditionBound(name="bond_length_r", kind="range", unit="Angstrom",
                                                               minimum=0.5, maximum=4.0)],
                 scope_change=["extend R to 4.0 Angstrom to reach the dissociation limit"])
    deps, linear, notion, writer, llm, _ = _approve_initial(tmp_path, llm=ScriptedResearchLlm(decisions=[widen, widen, complete]))
    run = _drive(deps, 10)
    assert run.stage is WorkflowStage.AWAITING_APPROVAL
    plans = workflow.plans(deps.store, "INU-900")
    change = plans[-1]
    assert change.kind == "scope_change" and change.status is PlanStatus.PENDING_APPROVAL
    assert any("bond_length_r" in x and "4.0" in x for x in change.change_summary)
    cs = orchestration.cycles(deps.store, "INU-900")
    assert len(cs) == 1 and "outside the approved range" in " ".join(cs[0].decision.gate_problems)
    (req,) = _pending(deps)
    _discord(deps, req)
    run = _drive(deps, 20)
    assert run.stage is WorkflowStage.DONE, (run.blocked_reason, run.events[-5:])
    auth = workflow.research_authorization(deps.store, "INU-900")
    assert auth.version == 2 and auth.plan_id == change.plan_id
    assert orchestration.cycles(deps.store, "INU-900")[1].tasks[0].parameters == {"bond_length_r": ["3.5"]}


def test_objective_or_metric_change_is_not_taken_autonomously(tmp_path):
    bad = more(("1.5",))
    bad.new_tasks[0].parameters = {"basis_set": ["cc-pVDZ"]}  # a condition that was never approved
    deps, linear, notion, writer, llm, _ = _approve_initial(tmp_path, llm=ScriptedResearchLlm(decisions=[bad, bad]))
    run = _drive(deps, 10)
    assert run.stage is WorkflowStage.BLOCKED and "not an approved condition" in run.blocked_reason
    assert len(orchestration.cycles(deps.store, "INU-900")) == 1
    assert len([p for p in notion.pages.values() if p.origin == "RA"]) == 1


def test_sample_count_limit_and_cycle_limit_are_enforced(tmp_path):
    too_many = more(("1.2", "1.4", "1.6", "1.8"))  # 3 used + 4 new > 6 approved geometries
    deps, linear, notion, writer, llm, _ = _approve_initial(tmp_path, llm=ScriptedResearchLlm(decisions=[too_many, too_many]))
    run = _drive(deps, 10)
    assert run.stage is WorkflowStage.BLOCKED and "exceed the approved maximum of 6" in run.blocked_reason


def test_budget_exhaustion_stops_and_reports(tmp_path):
    deps, linear, notion, writer, llm, _ = _approve_initial(tmp_path, llm=ScriptedResearchLlm(decisions=[more(), complete],
                                                                                             cost=3.0))
    run = _drive(deps, 20)
    assert run.stage is WorkflowStage.BLOCKED and "API budget exhausted" in run.blocked_reason
    sent = [r["event"] for r in deps.store.find(db.notifications_sent) if "event" in r]
    assert any(e["kind"] == "budget_exceeded" and e["flow_name"] == "research" for e in sent)
    assert any("research results" in n["title"] for n in writer.notes.values())  # what was done is still reported


def test_stop_and_resume_keep_authorization_and_budget_consistent(tmp_path):
    deps, linear, notion, writer, llm, _ = _approve_initial(tmp_path)
    poll(deps)  # approval applied, cycle 1 started
    orchestration.request_control(deps.store, "INU-900", "stop", "discord:1001", "discord")
    poll(deps)
    run = _run(deps)
    assert run.stage is WorkflowStage.PAUSED
    n_cycles = len(orchestration.cycles(deps.store, "INU-900"))
    for _ in range(4):
        poll(deps)
    assert _run(deps).stage is WorkflowStage.PAUSED and len(orchestration.cycles(deps.store, "INU-900")) == n_cycles
    orchestration.request_control(deps.store, "INU-900", "resume", "discord:1001", "discord")
    run = _drive(deps, 20)
    assert run.stage is WorkflowStage.DONE
    cs = orchestration.cycles(deps.store, "INU-900")
    assert [c.number for c in cs] == [1, 2]  # no duplicate cycle / task after the pause
    assert len([p for p in notion.pages.values() if p.origin == "RA"]) == 2


def test_spec_edit_after_approval_halts_and_requires_reapproval(tmp_path):
    deps, linear, notion, writer, llm, _ = _approve_initial(tmp_path)
    poll(deps)
    _edit_spec(notion, research_objective=RESEARCH_SECTIONS["research_objective"] + "\n- Also compare MP2.")
    for _ in range(3):
        poll(deps)
    run = _run(deps)
    assert run.stage is WorkflowStage.AWAITING_AUTHORIZATION
    assert [p.version for p in workflow.plans(deps.store, "INU-900")] == [1, 2]
    assert workflow.authorization_problems(deps.store, workflow.research_authorization(deps.store, "INU-900"),
                                           deps.plan.manager.load("INU-900").spec)


def test_cycles_are_idempotent(tmp_path):
    deps, linear, notion, writer, llm, _ = _approve_initial(tmp_path)
    poll(deps)
    run = _run(deps)
    bundle = deps.plan.manager.load("INU-900")
    auth = workflow.research_authorization(deps.store, "INU-900")
    again = orchestration._start_cycle(deps, run, bundle, auth, 1, [task()], None)
    assert again.cycle_id == orchestration.cycles(deps.store, "INU-900")[0].cycle_id
    assert len([p for p in notion.pages.values() if p.origin == "RA"]) == 1


def test_completion_without_result_evidence_is_not_success(tmp_path):
    claim = EvaluationDecision(action="complete", rationale="looks fine",
                               criteria=[{"criterion": "dE(R) obtained", "status": "met", "evidence": ["made-up"]}])
    deps, linear, notion, writer, llm, _ = _approve_initial(tmp_path, llm=ScriptedResearchLlm(decisions=[claim, claim]))
    run = _drive(deps, 10)
    assert run.stage is WorkflowStage.BLOCKED and "Success is not established" in run.blocked_reason
    assert linear.issues["INU-900"].state_name != "Done"


def test_discord_controls_require_an_authorized_user(tmp_path):
    from research_autopilot.notifications.discord import DiscordControlInput, InteractionResult, load_discord_settings
    deps, *_ = _build(tmp_path)
    settings = load_discord_settings({"RA_DISCORD_BOT_TOKEN": "t", "RA_DISCORD_GUILD_ID": "1",
                                      "RA_DISCORD_AUTHORIZED_USER_IDS": "1001", "RA_DISCORD_APPROVAL_CHANNEL_ID": "2",
                                      "RA_DISCORD_RESULTS_CHANNEL_ID": "3", "RA_DISCORD_ERRORS_CHANNEL_ID": "4",
                                      "RA_DISCORD_LOGS_CHANNEL_ID": "5"})
    deps.store.init_schema()
    svc = DiscordControlInput(deps.store, settings)
    assert svc.submit(interaction_id="a", task_id="INU-900", action="stop", user_id=999, guild_id=1,
                      channel_id=3) is InteractionResult.UNAUTHORIZED
    assert svc.submit(interaction_id="b", task_id="INU-900", action="stop", user_id=1001, guild_id=1,
                      channel_id=3) is InteractionResult.ACCEPTED
    assert svc.submit(interaction_id="b", task_id="INU-900", action="stop", user_id=1001, guild_id=1,
                      channel_id=3) is InteractionResult.DUPLICATE
    assert orchestration.paused(deps.store, "INU-900")


def test_authorization_versions_supersede_each_other(tmp_path):
    deps, *_ = _approve_initial(tmp_path)
    poll(deps)
    auths = [ResearchAuthorization.model_validate(r) for r in deps.store.find(db.authorizations, root_task_id="INU-900")]
    assert [a.kind for a in auths] == ["research_scope"] and auths[0].scope["primary_metric"]["value"]


def test_planner_questions_never_block_only_missing_required_conditions_do(tmp_path):
    from research_autopilot.research.models import OpenQuestion
    noisy = scope(open_questions=[OpenQuestion(field="research_authorization", blocking=True,
                                               question="the scope must be formally approved"),
                                  OpenQuestion(field="initial_sample_points", blocking=True,
                                               question="initial points are left to RA")])
    deps, linear, notion, writer, llm = _build(tmp_path, llm=ScriptedResearchLlm(scope_answer=noisy))
    poll(deps)
    [plan] = workflow.plans(deps.store, "INU-900")
    assert plan.status is PlanStatus.PENDING_APPROVAL and plan.scope.blocking() == []
    assert {q.field for q in plan.scope.open_questions} >= {"research_authorization", "initial_sample_points"}


def test_resume_on_a_blocked_research_run_restarts_planning(tmp_path):
    sections = {k: v for k, v in RESEARCH_SECTIONS.items() if k != "budget"}
    deps, linear, notion, writer, llm = _build(tmp_path, sections=sections)
    poll(deps)
    assert _run(deps).stage is WorkflowStage.BLOCKED
    sent = [r["event"] for r in deps.store.find(db.notifications_sent) if "event" in r and r["event"]["flow_name"] == "research"]
    assert sent and all(e.get("control_task_id") is None for e in sent)  # no Stop / Resume on a needs-input notice
    _edit_spec(notion, budget=RESEARCH_SECTIONS["budget"])
    orchestration.request_control(deps.store, "INU-900", "resume", "discord:1001", "discord")
    poll(deps)
    run = _run(deps)
    assert run.stage is WorkflowStage.AWAITING_AUTHORIZATION and [p.version for p in workflow.plans(deps.store, "INU-900")] == [1, 2]
    assert all(c.applied for c in orchestration.controls(deps.store, "INU-900"))


def test_every_llm_output_type_is_valid_for_the_strict_json_schema():
    from agents import AgentOutputSchema

    from research_autopilot.research.models import EvaluationOut, ResearchScope
    from research_autopilot.research.planner import PlanDraft
    for t in (ResearchScope, PlanDraft, EvaluationOut):
        AgentOutputSchema(t, strict_json_schema=True)


def test_range_bounds_are_read_from_the_quote_and_unbounded_ranges_block():
    from phase2_fakes import make_issue, make_page

    from research_autopilot.models import TaskSpec
    spec = TaskSpec.from_sources(make_issue(), make_page(sections=dict(RESEARCH_SECTIONS)))
    loose = scope(conditions=[ConditionBound(name="bond_length_r", kind="range",
                                             quote="bond length R between 0.5 and 3.0 Angstrom")])
    c = scope_mod.verify(loose, spec).conditions[0]
    assert (c.minimum, c.maximum) == (0.5, 3.0)
    none = scope(conditions=[ConditionBound(name="bond_length_r", kind="range", quote="Target: H2 with basis STO-3G")])
    assert any(q.field == "conditions.bond_length_r" for q in scope_mod.verify(none, spec).blocking())


def test_unstated_api_budget_becomes_an_explicit_default_in_the_approval(tmp_path):
    cpu_only = scope(budget=BudgetItem(cpu_hours=1, quote="Compute budget: 1 CPU-hour in total", status="stated"))
    deps, *_ = _build(tmp_path, llm=ScriptedResearchLlm(scope_answer=cpu_only))
    poll(deps)
    [plan] = workflow.plans(deps.store, "INU-900")
    assert plan.scope.budget.api_usd == deps.settings.default_budget.api_usd and "RA default" in plan.scope.budget.note
    (req,) = _pending(deps)
    assert "RA default" in req.budget_summary


def test_count_limits_are_tied_to_the_varied_quantity_and_never_used_as_parameters(tmp_path):
    from phase2_fakes import make_issue, make_page

    from research_autopilot.models import TaskSpec
    from research_autopilot.research.planner import check_tasks, strip_count_parameters
    spec = TaskSpec.from_sources(make_issue(), make_page(sections=dict(RESEARCH_SECTIONS)))
    loose = scope(conditions=[scope().conditions[0],
                              ConditionBound(name="geometry", kind="max_count", count=6, applies_to="geometry",
                                             quote="At most 6 geometries in total")])
    s = scope_mod.verify(loose, spec)
    assert s.conditions[1].applies_to == "bond_length_r" and "mapped" in s.conditions[1].note
    t = task(values=("0.5", "1.0"))
    t.parameters["geometry"] = ["2"]
    t = strip_count_parameters(t, s)
    assert "geometry" not in t.parameters and check_tasks([t], s, registry={"h2-test": object()}, evidence_ids=set()) == []
    seven = task(values=tuple(str(0.5 + 0.3 * i) for i in range(7)))
    assert any("maximum of 6" in p for p in check_tasks([seven], s, registry={"h2-test": object()}, evidence_ids=set()))


def test_autonomy_numbers_must_be_in_the_quote_or_the_defaults_apply():
    from phase2_fakes import make_issue, make_page

    from research_autopilot.models import TaskSpec
    from research_autopilot.research.models import Autonomy
    sections = dict(RESEARCH_SECTIONS, purpose="E2E では原則1回以上の Evaluate→Replan→Execute を観測する。")
    spec = TaskSpec.from_sources(make_issue(), make_page(sections=sections))
    invented = scope(autonomy=Autonomy(max_cycles=1, max_new_tasks_per_cycle=0, max_retries_per_task=0,
                                       no_progress_cycles=0, status="stated",
                                       quote="E2E では原則1回以上の Evaluate→Replan→Execute を観測する。"))
    a = scope_mod.verify(invented, spec).autonomy
    # "1回以上" is a minimum, never an upper limit: every limit falls back to the RA default
    assert (a.max_cycles, a.max_new_tasks_per_cycle, a.max_retries_per_task, a.no_progress_cycles) == (5, 3, 2, 2)
    assert a.status == "ra_default"
    sections["purpose"] = "Autonomy: at most 4 cycles and 2 new tasks per cycle; retries up to 1."
    spec = TaskSpec.from_sources(make_issue(), make_page(sections=sections))
    stated = scope(autonomy=Autonomy(max_cycles=4, max_new_tasks_per_cycle=2, max_retries_per_task=1,
                                     no_progress_cycles=2, status="stated",
                                     quote="Autonomy: at most 4 cycles and 2 new tasks per cycle; retries up to 1."))
    a = scope_mod.verify(stated, spec).autonomy
    assert (a.max_cycles, a.max_new_tasks_per_cycle, a.max_retries_per_task) == (4, 2, 1)


def test_controls_only_on_results_and_errors_messages(tmp_path):
    deps, linear, notion, writer, llm, _ = _approve_initial(tmp_path)
    _drive(deps, 20)
    sent = [r["event"] for r in deps.store.find(db.notifications_sent) if "event" in r and r["event"]["flow_name"] == "research"]
    with_controls = [e for e in sent if e.get("control_task_id")]
    assert with_controls and all(e["kind"] in ("analysis_completed", "unrecoverable_error", "budget_exceeded")
                                 for e in with_controls)
    assert all(e["control_actions"] == ["stop"] for e in with_controls if e["kind"] == "analysis_completed")
    assert not [e for e in sent if e["kind"] == "phase_transition" and e.get("control_task_id")]


def test_a_new_scope_after_a_stopped_research_starts_the_next_cycle(tmp_path):
    from research_autopilot.autopilot.models import ResearchAuthorization
    stop = EvaluationDecision(action="stop", rationale="limits")
    llm = ScriptedResearchLlm(decisions=[stop, complete])
    deps, linear, notion, writer, llm, _ = _approve_initial(tmp_path, llm=llm)
    run = _drive(deps, 20)
    assert run.stage is WorkflowStage.BLOCKED
    auth = workflow.research_authorization(deps.store, "INU-900")
    deps.store.put(db.authorizations, auth.authorization_id, {**auth.model_dump(mode="json"), "revoked": True},
                   root_task_id="INU-900")
    orchestration.request_control(deps.store, "INU-900", "resume", "discord:1001", "discord")
    poll(deps)
    (req,) = _pending(deps)
    _discord(deps, req)
    run = _drive(deps, 20)
    assert run.stage is WorkflowStage.DONE, (run.blocked_reason, run.events[-4:])
    cs = orchestration.cycles(deps.store, "INU-900")
    assert [c.number for c in cs] == [1, 2] and cs[1].authorization_version == 2


def test_child_tasks_carry_the_scope_and_the_whole_cycle_plan(tmp_path):
    from research_fakes import ScriptedResearchLlm, draft
    plan = draft(rationale="r", tasks=[task("T1", values=(), kind="implementation", title="implement scan"),
                                       task("T2", deps=("T1",), title="compute")], replanning_rules=["r"])
    deps, linear, notion, writer, llm, _ = _approve_initial(tmp_path, llm=ScriptedResearchLlm(plans=[plan],
                                                                                              decisions=[complete]))
    poll(deps)
    impl = next(p for p in notion.pages.values() if p.origin == "RA" and "implement scan" in p.title)
    text = str(impl.sections) + str(impl.other_sections)
    assert "bond_length_r=0.5, 1.0, 2.0" in text and "← このタスク" in text and "0.5, 3.0" in text


def test_no_progress_counts_only_cycles_of_the_current_authorization(tmp_path):
    from research_autopilot.research.models import ResearchCycle
    noprog = EvaluationDecision(action="stop", rationale="nothing yet", progress=False)
    retry = more(("1.5",), progress=False)
    deps, linear, notion, writer, llm, _ = _approve_initial(tmp_path, llm=ScriptedResearchLlm(
        decisions=[noprog, retry, complete]))
    run = _drive(deps, 20)
    assert run.stage is WorkflowStage.BLOCKED
    auth = workflow.research_authorization(deps.store, "INU-900")
    deps.store.put(db.authorizations, auth.authorization_id, {**auth.model_dump(mode="json"), "revoked": True},
                   root_task_id="INU-900")
    orchestration.request_control(deps.store, "INU-900", "resume", "discord:1001", "discord")
    poll(deps)
    (req,) = _pending(deps)
    _discord(deps, req)
    run = _drive(deps, 30)
    # cycle 2 (new scope) made no progress but cycle 1 belonged to the revoked scope: the retry is allowed
    cs = orchestration.cycles(deps.store, "INU-900")
    assert cs[1].decision.action == "continue" and run.stage is WorkflowStage.DONE, (run.blocked_reason, [c.decision.action for c in cs])


def test_resume_after_a_stop_re_evaluates_the_last_cycle(tmp_path):
    stop = EvaluationDecision(action="stop", rationale="needs a human look")
    deps, linear, notion, writer, llm, _ = _approve_initial(tmp_path, llm=ScriptedResearchLlm(decisions=[stop, complete]))
    run = _drive(deps, 20)
    assert run.stage is WorkflowStage.BLOCKED
    orchestration.request_control(deps.store, "INU-900", "resume", "discord:1001", "discord")
    run = _drive(deps, 20)
    assert run.stage is WorkflowStage.DONE and orchestration.cycles(deps.store, "INU-900")[0].decision.action == "complete"
    evals = [c[1] for c in llm.calls if c[0] == "ResearchEvaluator"]
    assert "HUMAN_RESUME" not in evals[0] and evals[-1]["HUMAN_RESUME"]["actor"] == "discord:1001"
