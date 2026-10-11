"""Phase 10 audit fixes (2026-10-12): dead ends after scope-change decisions, retries after half-done steps, stale
controls, budget / cancel / auto re-plan UX (docs/phase_specs/ux_policy.md), and engine retry bookkeeping."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from phase5_fakes import build
from research_fakes import RESEARCH_SECTIONS, ScriptedResearchLlm, complete, more, task

from research_autopilot.autopilot.dag import ParentRun
from research_autopilot.autopilot.engine import advance, poll
from research_autopilot.autopilot.execution import ExecutionStatus, transition
from research_autopilot.autopilot.linear_ops import WriteRefused
from research_autopilot.autopilot.models import (ApprovalRequest, DiscordSubmission, WorkflowRun, WorkflowStage)
from research_autopilot.models import utcnow
from research_autopilot.research import evaluator, orchestration, workflow
from research_autopilot.research.models import (BudgetItem, ConditionBound, EvaluationDecision, PlanStatus,
                                                ResearchControl)
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


def _runs(deps, task_id="INU-900") -> list[WorkflowRun]:
    return [WorkflowRun.model_validate(r) for r in deps.store.find(db.workflow_runs) if r["task_id"] == task_id]


def _run(deps) -> WorkflowRun:
    return _runs(deps)[0]


def _pending(deps, kind="research_scope") -> list[ApprovalRequest]:
    return [r for r in deps.approvals.pending() if r.kind.value == kind]


def _discord(deps, req, decision="approve", comment=""):
    sub = DiscordSubmission(interaction_id=f"i-{req.request_id}-{decision}", request_id=req.request_id, decision=decision,
                            spec_hash=req.spec_hash, user_id="1001", guild_id="1", channel_id="2", comment=comment)
    deps.store.insert_new(db.discord_interactions, sub.interaction_id, sub.model_dump(mode="json"), request_id=req.request_id)


def _edit_spec(notion, **sections):
    pid = next(iter(notion.pages))
    p = notion.pages[pid]
    notion.pages[pid] = p.model_copy(update={"sections": {**p.sections, **sections},
                                             "last_edited_time": "2026-10-12T00:00:00.000Z"})


def _approve_initial(tmp_path, **kw):
    deps, linear, notion, writer, llm = _build(tmp_path, **kw)
    poll(deps)
    (req,) = _pending(deps)
    _discord(deps, req)
    return deps, linear, notion, writer, llm


def _drive(deps, n=20):
    for _ in range(n):
        poll(deps)
        if _run(deps).stage in (WorkflowStage.DONE, WorkflowStage.BLOCKED, WorkflowStage.CANCELLED,
                                WorkflowStage.AWAITING_APPROVAL, WorkflowStage.PAUSED):
            break
    return _run(deps)


def _sent(deps, **match) -> list[dict]:
    out = [r["event"] for r in deps.store.find(db.notifications_sent) if "event" in r
           and r["event"]["flow_name"] == "research"]
    return [e for e in out if all(e.get(k) == v for k, v in match.items())]


def _widen(values=("3.5",)):
    return more(values, proposed_conditions=[ConditionBound(name="bond_length_r", kind="range", unit="Angstrom",
                                                            minimum=0.5, maximum=4.0)],
                scope_change=["extend R to 4.0 Angstrom"])


def _to_scope_change(tmp_path, decisions):
    deps, linear, notion, writer, llm = _approve_initial(tmp_path, llm=ScriptedResearchLlm(decisions=decisions))
    run = _drive(deps)
    assert run.stage is WorkflowStage.AWAITING_APPROVAL, (run.blocked_reason, run.events[-4:])
    (req,) = _pending(deps)
    return deps, linear, notion, writer, llm, req


# ---------------------------------------------------------------- 1-3, 6: scope-change decisions never dead-end


def test_revision_of_a_scope_change_re_evaluates_with_the_comment(tmp_path):
    deps, linear, notion, writer, llm, req = _to_scope_change(tmp_path, [_widen(), _widen(), more(("1.5",)), complete])
    _discord(deps, req, "revise", comment="stay below 3.0 and densify instead")
    run = _drive(deps, 25)
    assert run.stage is WorkflowStage.DONE, (run.blocked_reason, run.events[-5:])
    evals = [c[1] for c in llm.calls if c[0] == "ResearchEvaluator"]
    assert any("densify" in (e.get("HUMAN_RESUME") or {}).get("note", "") for e in evals)
    cs = orchestration.cycles(deps.store, "INU-900")
    assert cs[0].evaluations == 2 and cs[0].decision.action == "continue"
    assert cs[1].tasks[0].parameters == {"bond_length_r": ["1.5"]}
    # the re-evaluation is reported again (a per-evaluation key, not swallowed by the first message)
    assert len([e for e in _sent(deps, kind="analysis_completed") if "Cycle 1 evaluated" in e["details"][0]]) == 2


def test_an_expired_scope_change_stops_with_resume_steps_instead_of_idling(tmp_path):
    deps, linear, notion, writer, llm, req = _to_scope_change(tmp_path, [_widen(), _widen()])
    req = req.model_copy(update={"expires_at": utcnow() - timedelta(minutes=1)})
    deps.approvals._save(req)
    run = _drive(deps, 6)
    assert run.stage is WorkflowStage.BLOCKED and "expired" in run.blocked_reason
    [msg] = [e for e in _sent(deps) if "expired" in " ".join(e["details"])]
    assert msg["control_actions"] == ["resume", "cancel"] and msg["needs_human"] and msg["steps"]


def test_rejecting_a_scope_change_keeps_the_scope_and_stops_instead_of_cancelling(tmp_path):
    deps, linear, notion, writer, llm, req = _to_scope_change(tmp_path, [_widen(), _widen()])
    auth = workflow.research_authorization(deps.store, "INU-900")
    _discord(deps, req, "reject")
    run = _drive(deps, 6)
    assert run.stage is WorkflowStage.BLOCKED and "rejected" in run.blocked_reason
    assert linear.issues["INU-900"].state_name != "Canceled"
    assert workflow.research_authorization(deps.store, "INU-900").authorization_id == auth.authorization_id
    stop = [e for e in _sent(deps) if "rejected" in " ".join(e["details"])]
    assert stop and stop[-1]["control_actions"] == ["resume", "cancel"]
    assert any("research results" in n["title"] for n in writer.notes.values())  # stopped with a report


def test_a_new_initial_plan_after_a_superseded_scope_change_starts_a_cycle(tmp_path):
    deps, linear, notion, writer, llm, req = _to_scope_change(tmp_path, [_widen(), _widen(), complete])
    _edit_spec(notion, research_objective=RESEARCH_SECTIONS["research_objective"] + "\n- Report dE in mEh as well.")
    run = _drive(deps, 6)  # the scope-change request is stale: a new initial plan version is drafted
    plans = workflow.plans(deps.store, "INU-900")
    assert plans[-1].kind == "initial" and plans[-2].status is PlanStatus.SUPERSEDED, [p.status for p in plans]
    (new,) = _pending(deps)
    _discord(deps, new)
    run = _drive(deps, 25)
    assert run.stage is WorkflowStage.DONE, (run.blocked_reason, run.events[-5:])
    cs = orchestration.cycles(deps.store, "INU-900")
    assert len(cs) == 2 and cs[1].plan_id == plans[-1].plan_id


def test_a_scope_change_that_failed_before_it_was_linked_is_proposed_once(tmp_path, monkeypatch):
    deps, linear, notion, writer, llm = _approve_initial(tmp_path, llm=ScriptedResearchLlm(decisions=[_widen(), _widen()]))
    real = workflow.request_approval
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("discord down")
        return real(*a, **kw)

    monkeypatch.setattr(workflow, "request_approval", flaky)
    run = _drive(deps, 10)
    assert run.stage is WorkflowStage.AWAITING_APPROVAL, (run.blocked_reason, run.events[-4:])
    changes = [p for p in workflow.plans(deps.store, "INU-900") if p.kind == "scope_change"]
    assert len(changes) == 1 and changes[0].status is PlanStatus.PENDING_APPROVAL
    assert orchestration.cycles(deps.store, "INU-900")[0].next_plan_id == changes[0].plan_id


# ---------------------------------------------------------------- 4: limits per authorization


def test_max_cycles_counts_only_cycles_of_the_current_authorization():
    from research_autopilot.research.models import ResearchCycle
    from research_fakes import scope
    s = scope()
    s.autonomy.max_cycles = 2
    old = [ResearchCycle(task_id="T", authorization_id="a1", authorization_version=1, number=i, tasks=[task()])
           for i in (1, 2)]
    new = old + [ResearchCycle(task_id="T", authorization_id="a2", authorization_version=2, number=3, tasks=[task()])]
    d = more(("1.5",))
    kw = dict(scope=s, results=[], registry={"h2-test": object()}, used_values={}, used_cpu=0, used_api=0)
    assert any("maximum of 2 cycles" in p for p in evaluator.gate(d, cycles=new, **kw))  # legacy: all cycles
    assert not any("cycles" in p for p in evaluator.gate(more(("1.5",)), cycles=new, authorization_id="a2", **kw))


# ---------------------------------------------------------------- 5, 7: half-done steps are retried


def test_a_finish_that_failed_after_the_decision_was_saved_is_retried(tmp_path, monkeypatch):
    deps, linear, notion, writer, llm = _approve_initial(tmp_path, llm=ScriptedResearchLlm(decisions=[complete]))
    real = deps.ops.set_state
    state = {"failed": False}

    def flaky(issue, name, reason):
        if name == "Done" and issue.identifier == "INU-900" and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("linear timeout")
        return real(issue, name, reason)

    monkeypatch.setattr(deps.ops, "set_state", flaky)
    run = _drive(deps, 25)
    assert state["failed"] and run.stage is WorkflowStage.DONE, (run.blocked_reason, run.events[-5:])
    assert len([c for c in llm.calls if c[0] == "ResearchEvaluator"]) == 1  # not evaluated twice
    assert len(_sent(deps, kind="task_completed")) == 1


def test_a_cycle_saved_without_its_child_dag_is_started_not_evaluated(tmp_path):
    deps, linear, notion, writer, llm = _approve_initial(tmp_path)
    linear.fail_create = 1  # the first child task cannot be created: the cycle exists, its DAG does not
    poll(deps)
    [c] = orchestration.cycles(deps.store, "INU-900")
    assert c.parent_run_id is None and c.status.value == "running"
    poll(deps)
    [c] = orchestration.cycles(deps.store, "INU-900")
    assert c.parent_run_id and c.decision is None
    assert not [x for x in llm.calls if x[0] == "ResearchEvaluator"]
    # a cycle whose started DAG was not linked adopts it (no second parent / execution)
    parent_id, c.parent_run_id = c.parent_run_id, None
    orchestration.save_cycle(deps.store, c)
    poll(deps)
    assert orchestration.cycles(deps.store, "INU-900")[0].parent_run_id == parent_id
    assert len([x for x in deps.store.find(db.parent_runs) if x.get("research_cycle_id") == c.cycle_id]) == 1
    assert _drive(deps, 25).stage is WorkflowStage.DONE


# ---------------------------------------------------------------- 8, 9: engine bookkeeping / poll robustness


def test_attempts_reset_after_a_successful_step_and_a_dead_letter_research_can_be_resumed(tmp_path):
    stop = EvaluationDecision(action="stop", rationale="needs a look")
    deps, linear, notion, writer, llm = _approve_initial(tmp_path, llm=ScriptedResearchLlm(decisions=[stop, complete]))
    poll(deps)
    run = _run(deps)
    run.attempts = 2
    advance(deps, run, max_steps=1)
    assert run.attempts == 0
    assert _drive(deps).stage is WorkflowStage.BLOCKED
    run = _run(deps)
    run.stage = WorkflowStage.DEAD_LETTER
    deps.store.put(db.workflow_runs, run.run_id, run.model_dump(mode="json"), task_id=run.task_id, stage="dead_letter")
    orchestration.request_control(deps.store, "INU-900", "resume", "discord:1001", "discord")
    run = _drive(deps, 20)
    assert run.stage is WorkflowStage.DONE and len(_runs(deps)) == 1


def test_one_failing_research_run_does_not_abort_the_tick(tmp_path, monkeypatch):
    deps, *_ = _approve_initial(tmp_path)
    poll(deps)
    monkeypatch.setattr(orchestration, "_tick_run", lambda deps, run, out: (_ for _ in ()).throw(ValueError("boom")))
    out = orchestration.tick(deps)
    assert out["errors"] and "boom" in out["errors"][0]


def test_a_resume_processed_twice_adopts_its_execution_instead_of_dropping_it(tmp_path):
    stop = EvaluationDecision(action="stop", rationale="needs a look")
    deps, *_ = _approve_initial(tmp_path, llm=ScriptedResearchLlm(decisions=[stop, complete]))
    run = _drive(deps)
    assert run.stage is WorkflowStage.BLOCKED
    c = orchestration.request_control(deps.store, "INU-900", "resume", "discord:1001", "discord")
    orchestration._restart_blocked(deps, run, c)
    first = run.execution_run_id
    run.stage = WorkflowStage.BLOCKED  # a crash before the run was saved: the same control is processed again
    orchestration._restart_blocked(deps, run, c)
    assert run.execution_run_id == first and run.stage is WorkflowStage.DISCOVERED


# ---------------------------------------------------------------- 10, 11: messages per control, stale controls


def test_each_resume_after_a_pause_sends_its_own_message(tmp_path):
    deps, *_ = _approve_initial(tmp_path, llm=ScriptedResearchLlm(decisions=[more(), more(("1.2",)), complete]))
    poll(deps)
    for _ in range(2):
        orchestration.request_control(deps.store, "INU-900", "stop", "discord:1001", "discord")
        poll(deps)
        assert _run(deps).stage is WorkflowStage.PAUSED
        orchestration.request_control(deps.store, "INU-900", "resume", "discord:1001", "discord")
        poll(deps)
    resumed = [e for e in _sent(deps) if "Research resumed" in " ".join(e["details"])]
    paused = [e for e in _sent(deps) if "paused" in " ".join(e["details"])]
    assert len(resumed) == 2 and len(paused) == 2
    assert all(e["control_actions"] == ["resume", "cancel"] and e["needs_human"] and e["steps"] for e in paused)


def test_old_or_ineffective_stops_never_pause_a_later_run(tmp_path):
    deps, linear, notion, writer, llm = _build(tmp_path, llm=ScriptedResearchLlm(decisions=[complete]))
    deps.store.init_schema()
    old = ResearchControl(task_id="INU-900", action="stop", actor="discord:1001", channel="discord",
                          at=utcnow() - timedelta(days=1))  # pressed on a message of an earlier run
    deps.store.put(db.research_controls, old.control_id, old.model_dump(mode="json"), task_id="INU-900")
    poll(deps)
    (req,) = _pending(deps)
    _discord(deps, req)
    run = _drive(deps, 25)
    assert run.stage is WorkflowStage.DONE, (run.blocked_reason, run.events[-4:])
    assert orchestration.controls(deps.store, "INU-900")[0].ignored


def test_a_stop_on_a_stopped_research_does_not_pause_it_after_resume(tmp_path):
    stop = EvaluationDecision(action="stop", rationale="needs a look")
    deps, *_ = _approve_initial(tmp_path, llm=ScriptedResearchLlm(decisions=[stop, complete]))
    assert _drive(deps).stage is WorkflowStage.BLOCKED
    orchestration.request_control(deps.store, "INU-900", "stop", "discord:1001", "discord")
    poll(deps)
    orchestration.request_control(deps.store, "INU-900", "resume", "discord:1001", "discord")
    assert _drive(deps, 20).stage is WorkflowStage.DONE


# ---------------------------------------------------------------- 12, 13, 17: budget


def test_an_unstated_budget_limit_is_no_limit_and_scope_changes_keep_old_limits(tmp_path):
    from research_autopilot.research.models import ResearchCycle
    from research_fakes import scope
    deps, linear, notion, writer, llm = _approve_initial(tmp_path, llm=ScriptedResearchLlm(
        scope_answer=scope(budget=BudgetItem(cpu_hours=1, quote="Compute budget: 1 CPU-hour in total",
                                             status="stated"))))
    poll(deps)
    auth = workflow.research_authorization(deps.store, "INU-900")
    assert auth.budget.compute_cpu_hours == 1 and auth.budget.api_usd > 0  # (RA default API budget is explicit)
    a = auth.model_copy(update={"budget": workflow.Budget(api_usd=workflow.NO_LIMIT,
                                                          compute_cpu_hours=workflow.NO_LIMIT)})
    from research_autopilot.autopilot import policy
    assert policy.evaluate_budget(a, 100.0, 100.0) == []
    cur = ResearchCycle(task_id="INU-900", authorization_id=auth.authorization_id, authorization_version=1, number=1,
                        tasks=[task()])
    d = _widen()
    d.proposed_budget = BudgetItem(cpu_hours=3)
    new, _ = orchestration._draft_scope_change(deps, _run(deps), deps.plan.manager.load("INU-900"), auth,
                                               workflow.get_plan(deps.store, auth.plan_id), cur, d)
    assert new.scope.budget.cpu_hours == 3 and new.scope.budget.api_usd == auth.scope["budget"]["api_usd"]


def test_budget_stop_message_gives_numbered_steps_and_no_new_children_start(tmp_path):
    deps, linear, notion, writer, llm = _approve_initial(tmp_path, llm=ScriptedResearchLlm(decisions=[more(), complete],
                                                                                          cost=3.0))
    run = _drive(deps, 20)
    assert run.stage is WorkflowStage.BLOCKED and "budget exhausted" in run.blocked_reason
    [msg] = _sent(deps, kind="budget_exceeded")
    assert msg["control_actions"] == ["resume", "cancel"] and msg["needs_human"]
    assert any("Budget / Authorization" in s for s in msg["steps"]) and any("number" in s for s in msg["steps"])
    assert any(x.startswith("Budget used:") for x in msg["details"])


def test_a_stopped_research_starts_no_new_children(tmp_path):
    from research_autopilot.autopilot.dag_runtime import schedule_ready
    from research_fakes import draft
    plan = draft(rationale="r", tasks=[task("T1"), task("T2", ("1.5",), kind="analysis")], replanning_rules=["r"])
    deps, *_ = _approve_initial(tmp_path, llm=ScriptedResearchLlm(plans=[plan], decisions=[complete]))
    poll(deps)  # one task slot: T1 started, the independent T2 waits for capacity
    [c] = orchestration.cycles(deps.store, "INU-900")
    parent = ParentRun.model_validate(deps.store.get(db.parent_runs, c.parent_run_id))
    assert list(parent.child_execution_run_ids) == [c.child_task_ids["T1"]]
    run = _run(deps)
    run.stage = WorkflowStage.BLOCKED  # e.g. stopped on budget / a revoked scope / max cycles
    deps.store.put(db.workflow_runs, run.run_id, run.model_dump(mode="json"), task_id=run.task_id, stage="blocked")
    child = c.child_task_ids["T1"]
    ex = deps.store.get(db.execution_runs, parent.child_execution_run_ids[child])
    ex["status"] = "Completed"
    deps.store.put(db.execution_runs, ex["execution_run_id"], ex, request_id=ex["request_id"], task_id=child,
                   status="Completed")
    assert schedule_ready(deps, parent) == []
    assert c.child_task_ids["T2"] not in parent.child_execution_run_ids


# ---------------------------------------------------------------- 14, 15: one run per objective, auto re-plan


def test_run_autopilot_on_a_stopped_research_adopts_its_run(tmp_path):
    stop = EvaluationDecision(action="stop", rationale="needs a look")
    deps, linear, notion, writer, llm = _approve_initial(tmp_path, llm=ScriptedResearchLlm(decisions=[stop, complete]))
    assert _drive(deps).stage is WorkflowStage.BLOCKED
    pid = next(iter(notion.pages))
    notion.pages[pid] = notion.pages[pid].model_copy(update={"run_requested": True,
                                                             "last_edited_time": "2026-10-12T01:00:00.000Z"})
    run = _drive(deps, 20)
    assert len(_runs(deps)) == 1 and run.stage is WorkflowStage.DONE, (run.blocked_reason, run.events[-4:])
    assert notion.pages[pid].run_requested is False


def test_editing_the_spec_of_a_needs_input_plan_re_plans_without_resume(tmp_path):
    sections = {k: v for k, v in RESEARCH_SECTIONS.items() if k != "budget"}
    deps, linear, notion, writer, llm = _build(tmp_path, sections=sections)
    poll(deps)
    assert _run(deps).stage is WorkflowStage.BLOCKED
    poll(deps)
    assert len(workflow.plans(deps.store, "INU-900")) == 1  # nothing changed: no re-plan
    _edit_spec(notion, budget=RESEARCH_SECTIONS["budget"])
    poll(deps)  # first sighting of the edit: wait for it to settle
    assert len(workflow.plans(deps.store, "INU-900")) == 1
    poll(deps)
    assert [p.version for p in workflow.plans(deps.store, "INU-900")] == [1, 2]
    assert _run(deps).stage is WorkflowStage.AWAITING_AUTHORIZATION
    assert any(c.actor == "auto:spec-edit" and c.applied for c in orchestration.controls(deps.store, "INU-900"))


# ---------------------------------------------------------------- 18: Cancel


def test_cancel_stops_the_research_and_cancels_unfinished_tasks_in_linear(tmp_path):
    from research_fakes import draft
    plan = draft(rationale="r", tasks=[task("T1"), task("T2", ("1.5",), deps=("T1",), kind="analysis")],
                 replanning_rules=["r"])
    deps, linear, notion, writer, llm = _approve_initial(tmp_path, llm=ScriptedResearchLlm(plans=[plan],
                                                                                          decisions=[complete]))
    poll(deps)
    [c] = orchestration.cycles(deps.store, "INU-900")
    orchestration.request_control(deps.store, "INU-900", "cancel", "discord:1001", "discord")
    poll(deps)
    run = _run(deps)
    assert run.stage is WorkflowStage.CANCELLED
    assert linear.issues["INU-900"].state_name == "Canceled"
    assert all(linear.issues[t].state_name == "Canceled" for t in c.child_task_ids.values())
    writes = deps.store.linear_writes_for("INU-900")
    assert writes[-1]["to_state"] == "Canceled" and "discord:1001" in writes[-1]["reason"]
    assert ParentRun.model_validate(deps.store.get(db.parent_runs, c.parent_run_id)).status is ExecutionStatus.CANCELED
    assert all(r.stage is WorkflowStage.CANCELLED for t in c.child_task_ids.values() for r in _runs(deps, t))
    for _ in range(3):
        poll(deps)
    assert len(orchestration.cycles(deps.store, "INU-900")) == 1 and _run(deps).stage is WorkflowStage.CANCELLED


def test_cancel_of_a_blocked_research_and_the_canceled_state_guards(tmp_path):
    stop = EvaluationDecision(action="stop", rationale="needs a look")
    deps, linear, *_ = _approve_initial(tmp_path, llm=ScriptedResearchLlm(decisions=[stop]))
    assert _drive(deps).stage is WorkflowStage.BLOCKED
    orchestration.request_control(deps.store, "INU-900", "cancel", "discord:1001", "discord")
    poll(deps)
    assert _run(deps).stage is WorkflowStage.CANCELLED and linear.issues["INU-900"].state_name == "Canceled"
    assert transition(ExecutionStatus.BLOCKED, ExecutionStatus.CANCELED) is ExecutionStatus.CANCELED
    assert transition(ExecutionStatus.ACCEPTED, ExecutionStatus.CANCELED) is ExecutionStatus.CANCELED
    issue = linear.issues["INU-900"].model_copy(update={"state_name": "Todo", "state_type": "unstarted"})
    with pytest.raises(WriteRefused):
        deps.ops.set_state(issue, "Canceled", "autopilot never cancels on its own")
    with pytest.raises(WriteRefused):
        deps.ops.cancel_by_human(issue, "auto:spec-edit", "not a human")


# ---------------------------------------------------------------- 19, 20: engine re-plan paths, Phase 4 reruns


def test_replan_for_dod_writes_linear_before_changing_the_run(tmp_path):
    from research_autopilot.autopilot import engine
    deps = SimpleNamespace(linear=SimpleNamespace(get_issue=lambda t: SimpleNamespace(identifier=t)),
                           ops=SimpleNamespace(set_state=lambda *a: (_ for _ in ()).throw(RuntimeError("down"))),
                           store=SimpleNamespace(get_run=lambda i: None))
    run = WorkflowRun(task_id="INU-1", root_task_id="INU-1", plan_run_id="p", plan_id="q")
    m = SimpleNamespace(dod_infeasible={"criteria": ["D1"], "evidence": "no API", "by": "codex"})
    with pytest.raises(RuntimeError):
        engine._replan_for_dod(deps, run, m)
    assert run.dod_replans == 0 and run.dod_claim is None


def test_dod_seed_is_kept_until_a_ready_plan(tmp_path, monkeypatch):
    from research_autopilot.autopilot import engine
    run = WorkflowRun(task_id="INU-1", root_task_id="INU-1", plan_run_id="new-run",
                      dod_claim={"criteria": ["D1"], "evidence": "e", "by": "codex", "plan_run_id": "p",
                                 "plan_id": "q", "statements": {}, "consumed": False})
    monkeypatch.setattr(engine, "_plan_of", lambda deps, a, b: SimpleNamespace(definition_of_done=[]))
    assert engine._dod_seed(None, run) is not None  # a transient re-plan failure changed plan_run_id: still seeded
    run.dod_claim["consumed"] = True
    assert engine._dod_seed(None, run) is None


def test_continue_fallback_puts_the_issue_back_to_todo(tmp_path, monkeypatch):
    from research_autopilot.autopilot import engine
    from research_autopilot.implementation import orchestrator
    from research_autopilot.implementation.models import RunState
    issue = SimpleNamespace(identifier="INU-1", state_type="unstarted")
    moves = []

    def set_state(i, name, reason):
        moves.append(name)
        i.state_type = "started" if name == "In Progress" else "unstarted"

    deps = SimpleNamespace(store=SimpleNamespace(get_run=lambda i: SimpleNamespace(handoff_path="h.json")),
                           linear=SimpleNamespace(get_issue=lambda t: issue),
                           ops=SimpleNamespace(_guard=lambda i: None, set_state=set_state),
                           plan=SimpleNamespace(settings=SimpleNamespace(in_progress_state_name="In Progress")),
                           impl=None)
    monkeypatch.setattr(orchestrator, "_load_handoff", lambda p, probe: None)

    def gate(*a):
        raise orchestrator._Stop(RunState.BLOCKED, "spec_changed", "spec changed since planning")

    monkeypatch.setattr(orchestrator, "_approval_gate", gate)
    run = WorkflowRun(task_id="INU-1", root_task_id="INU-1",
                      continue_from={"stage": "implementing", "plan_run_id": "p", "plan_id": "q", "phase3_run_id": "x"})
    assert engine._continue(deps, run) is False
    assert moves == ["In Progress", "Todo"] and issue.state_type == "unstarted"


def test_only_one_phase4_only_rerun_per_task_and_cycle(tmp_path):
    deps, *_ = _build(tmp_path)
    deps.store.init_schema()
    first = ParentRun(parent_task_id="INU-900", parent_notion_page_id="p", parent_execution_run_id="e1", dag_id="d",
                      dag_hash="h", research_cycle_id="rcyc_1")
    second = first.model_copy(update={"parent_run_id": "pr2", "parent_execution_run_id": "e2",
                                      "carry_over": {"INU-901": {"stage": "experimenting"}}})
    for p in (first, second):
        deps.store.put(db.parent_runs, p.parent_run_id, p.model_dump(mode="json"), parent_task_id=p.parent_task_id,
                       parent_execution_run_id=p.parent_execution_run_id, status=p.status.value)
    assert orchestration._phase4_rerun_used(deps, second, "INU-901")
    assert not orchestration._phase4_rerun_used(deps, second, "INU-902")



def test_a_confirmed_discord_cancel_ends_the_research_and_voids_its_pending_approval(tmp_path):
    from research_autopilot.autopilot.models import ApprovalStatus
    from research_autopilot.notifications.discord import DiscordControlInput, InteractionResult
    from test_discord_notifications import settings
    deps, linear, *_ = _build(tmp_path)
    poll(deps)  # 10A: the research-scope request is pending
    [pending] = [r for r in deps.approvals.pending() if r.kind.value == "research_scope"]
    s = settings()
    result = DiscordControlInput(deps.store, s).submit(interaction_id="c1", task_id="INU-900", action="cancel",
                                                       user_id=200, guild_id=s.guild_id,
                                                       channel_id=s.approval_channel_id)
    assert result is InteractionResult.ACCEPTED
    poll(deps)
    assert _runs(deps)[-1].stage is WorkflowStage.CANCELLED
    assert linear.issues["INU-900"].state_name == "Canceled"
    closed = ApprovalRequest.model_validate(deps.store.get(db.approval_requests, pending.request_id))
    assert closed.status is ApprovalStatus.STALE and "打ち切られた" in closed.payload["_closed_note"]


def test_a_research_child_is_not_sent_to_a_human_completion_review(tmp_path):
    from phase5_fakes import FakeCompletionReviewer
    from research_autopilot.autopilot.models import ApprovalKind
    deps, linear, *_ = _build(tmp_path, completion=FakeCompletionReviewer(status="unverifiable"))
    poll(deps)
    (req,) = [r for r in deps.approvals.pending() if r.kind.value == "research_scope"]
    from test_phase10_research import _discord
    _discord(deps, req)
    for _ in range(25):
        poll(deps)
    assert not [r for r in deps.store.find(db.approval_requests) if r.get("kind") == ApprovalKind.COMPLETION_REVIEW.value]
    kids = [r for r in _runs_all(deps) if r.research_cycle_id]
    assert kids and any(any(e["kind"] == "completion_unestablished" for e in k.events) for k in kids)


def _runs_all(deps):
    return [WorkflowRun.model_validate(r) for r in deps.store.find(db.workflow_runs)]


def test_a_research_task_sees_validated_outputs_of_earlier_cycles(tmp_path):
    from research_autopilot.catalog.integration import upstream_artifacts
    deps, *_ = _build(tmp_path, llm=ScriptedResearchLlm(decisions=[more(), complete]))
    from test_phase10_research import _discord
    poll(deps)
    (req,) = [r for r in deps.approvals.pending() if r.kind.value == "research_scope"]
    _discord(deps, req)
    for _ in range(40):
        poll(deps)
        cs = orchestration.cycles(deps.store, "INU-900")
        if len(cs) >= 2 and cs[1].child_task_ids:
            break
    first, second = orchestration.cycles(deps.store, "INU-900")[:2]
    producers = set(first.child_task_ids.values())
    later = next(iter(second.child_task_ids.values()))
    seen = {a["identity"]["producer_task_id"] for a in upstream_artifacts(deps.store, later)}
    assert seen & producers  # cycle-2 work can read what cycle 1 validated


def test_later_cycles_build_on_the_last_validated_implementation_of_the_research(tmp_path):
    deps, *_ = _build(tmp_path, llm=ScriptedResearchLlm(decisions=[more(), complete]))
    from test_phase10_research import _discord
    poll(deps)
    (req,) = [r for r in deps.approvals.pending() if r.kind.value == "research_scope"]
    _discord(deps, req)
    for _ in range(40):
        poll(deps)
        if _runs(deps)[-1].stage in (WorkflowStage.DONE, WorkflowStage.BLOCKED):
            break
    first, second = orchestration.cycles(deps.store, "INU-900")[:2]
    manifests = [deps.store.get_implementation(rid) for r in _runs_all(deps) if r.research_cycle_id == second.cycle_id
                 for rid in r.phase3_run_ids]
    earlier = {deps.store.get_implementation(rid).implementation_commit for r in _runs_all(deps)
               if r.research_cycle_id == first.cycle_id for rid in r.phase3_run_ids}
    based = [m for m in manifests if m and m.task.based_on]
    assert based and all(m.task.base_commit in earlier | {x.implementation_commit for x in manifests if x}
                         for m in based)
    assert any('"based_on"' in c.prompt for c in deps.impl.backends["codex"].calls)


def test_a_completed_research_closes_its_unused_child_tasks_as_superseded(tmp_path):
    deps, linear, *_ = _build(tmp_path, llm=ScriptedResearchLlm(decisions=[more(), complete]))
    from test_phase10_research import _discord
    poll(deps)
    (req,) = [r for r in deps.approvals.pending() if r.kind.value == "research_scope"]
    _discord(deps, req)
    poll(deps)
    first = orchestration.cycles(deps.store, "INU-900")[0]
    unused = linear.issues[next(iter(first.child_task_ids.values()))].model_copy(
        update={"identifier": "INU-950", "id": "issue-950", "title": "[TEST] never run"})
    linear.issues["INU-950"], linear.histories["INU-950"] = unused, []
    first.child_task_ids["TX"] = "INU-950"
    orchestration.save_cycle(deps.store, first)
    for _ in range(40):
        poll(deps)
        if _runs(deps)[-1].stage is WorkflowStage.DONE:
            break
    assert _runs(deps)[-1].stage is WorkflowStage.DONE
    assert linear.issues["INU-950"].state_name == "Canceled"
    used = [t for c in orchestration.cycles(deps.store, "INU-900") for k, t in c.child_task_ids.items() if k != "TX"]
    assert all(linear.issues[t].state_name == "Done" for t in used)
    done = [r["event"] for r in deps.store.find(db.notifications_sent) if "event" in r
            and r["event"].get("dedupe_key", "").startswith("research:INU-900:done")]
    assert done and any("INU-950" in d for d in done[0]["details"])


def test_dev_only_cli_approval_is_audited_and_ignored_outside_dev(tmp_path, monkeypatch):
    import pytest as _pytest
    from research_autopilot.autopilot.approvals import submit_cli_decision
    deps, *_ = _build(tmp_path)
    poll(deps)
    (req,) = [r for r in deps.approvals.pending() if r.kind.value == "research_scope"]
    monkeypatch.delenv("RA_ENVIRONMENT", raising=False)
    with _pytest.raises(PermissionError):
        submit_cli_decision(deps.store, req, "approve", "claude-code (delegated)")
    monkeypatch.setenv("RA_ENVIRONMENT", "dev")
    submit_cli_decision(deps.store, req, "approve", "claude-code (delegated by the user)")
    monkeypatch.setenv("RA_ENVIRONMENT", "production")
    poll(deps)  # recorded in dev, but production never counts a CLI decision
    assert workflow.research_authorization(deps.store, "INU-900") is None
    monkeypatch.setenv("RA_ENVIRONMENT", "dev")
    poll(deps)
    auth = workflow.research_authorization(deps.store, "INU-900")
    assert auth and auth.approved_by == "cli-dev:claude-code (delegated by the user)"


def test_the_evaluator_knows_publishing_is_done_by_research_autopilot():
    from research_autopilot.research.evaluator import EVAL_INSTRUCTIONS
    assert "Publishing is NOT a task" in EVAL_INSTRUCTIONS and "Artifact" in EVAL_INSTRUCTIONS


def test_exhausted_llm_credits_wait_without_counting_failures_or_dead_lettering(tmp_path):
    from research_autopilot.autopilot import engine
    assert engine.provider_exhausted("Error code: 429 - credit_balance_exhausted: You have no credits remaining")
    assert engine.provider_exhausted(RuntimeError("x")) is False
    deps, *_ = _build(tmp_path)
    calls = {"n": 0}
    real = deps.research.llm_factory

    def broke():
        calls["n"] += 1
        raise RuntimeError("Error code: 429 - {'error': {'message': 'You have no credits remaining.', 'code': "
                           "'credit_balance_exhausted'}}")
    deps.research = ResearchDeps(llm_factory=broke)
    for _ in range(5):
        poll(deps)
    run = _runs(deps)[-1]
    assert run.stage is not WorkflowStage.DEAD_LETTER and run.attempts == 0 and calls["n"] >= 3
    assert any(e["kind"] == "provider_unavailable" for e in run.events)
    notices = [r for r in deps.store.find(db.notifications_sent) if "event" in r
               and r["event"].get("dedupe_key", "").startswith("provider-credits:")]
    assert len(notices) == 1 and notices[0]["event"]["needs_human"]
    deps.research = ResearchDeps(llm_factory=real)  # credits added: the same run continues by itself
    poll(deps)
    assert _runs(deps)[-1].stage is WorkflowStage.AWAITING_AUTHORIZATION
