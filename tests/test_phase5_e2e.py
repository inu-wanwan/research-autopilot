from phase5_fakes import ACC, INV, USER, approve, build, draft
from research_autopilot.autopilot.engine import poll, status_report
from research_autopilot.autopilot.models import ApprovalKind, WorkflowRun, WorkflowStage
from research_autopilot.experiments.backends.fake import FakeComputeBackend
from research_autopilot.experiments.models import ParameterChange
from research_autopilot.storage import db


def runs(deps):
    return {r["task_id"]: WorkflowRun.model_validate(r) for r in deps.store.find(db.workflow_runs)}


def pending(deps):
    return deps.approvals.pending()


def onboard(deps, linear):
    s = poll(deps)
    assert s["discovered"] == ["INU-900"]
    (req,) = pending(deps)
    assert req.kind is ApprovalKind.ONBOARDING and linear.issues["INU-900"].state_name == "Awaiting Approval"
    approve(deps, linear, req.request_id)
    return req


def request_child(notion, child_id):
    """Phase 6A: proposals are registered only; a human must request execution."""
    page_id = next(p.page_id for p in notion.pages.values() if p.linear_issue_id == child_id)
    page = notion.pages[page_id]
    notion.pages[page_id] = page.model_copy(update={"run_requested": True, "execution_status": "Requested"})


def test_full_e2e_with_zero_mid_run_approvals_and_auto_followup(tmp_path):
    deps, linear, notion, writer, channel = build(tmp_path)
    onboard(deps, linear)
    poll(deps)  # approval verified -> plan -> implement -> experiment -> propose -> complete
    r = runs(deps)["INU-900"]
    assert r.stage is WorkflowStage.DONE, (r.blocked_reason, r.events[-3:])
    assert linear.issues["INU-900"].state_name == "Done"
    assert len(r.approval_request_ids) == 1  # only the initial delegation; 0 mid-run approvals
    # Concise Notes written with a folded reference section; Linear comment keeps the link.
    (note,) = writer.notes.values()
    text = str(note["blocks"])
    assert "参照・再現情報" in text and r.experiment_run_ids[0] in text and r.commits[0][:12] in text
    assert "計算結果" in text and "検証状況" in text
    assert any("Notion Notes" in body for _, body in linear.comments)
    # Auto follow-up proposal registered in Notion + Linear (Todo, autopilot label, linked both ways)
    (child_id,) = linear.created
    child = linear.issues[child_id]
    assert child.state_name == "Todo" and "autopilot" in child.labels and child.title.startswith("[TEST]")
    page = next(p for p in notion.pages.values() if p.linear_issue_id == child_id)
    assert page.pending is False and page.sections["scientific_invariants"].strip("- ") == INV[0]
    # Labels do not start children: a new explicit request is required.
    request_child(notion, child_id)
    poll(deps)
    rc = runs(deps)[child_id]
    assert rc.stage is WorkflowStage.DONE, (rc.blocked_reason, rc.events[-3:])
    assert rc.authorization_id == r.authorization_id and rc.chain_depth == 1 and rc.approval_request_ids == []
    assert linear.issues[child_id].state_name == "Done"
    # No further proposals (duplicates of the same idea are deduplicated) -> chain stops
    poll(deps)
    assert len(linear.created) == 1


def test_label_alone_is_not_authorization(tmp_path):
    deps, linear, *_ = build(tmp_path)
    poll(deps)
    poll(deps)  # no human decision yet -> nothing runs
    r = runs(deps)["INU-900"]
    assert r.stage is WorkflowStage.AWAITING_AUTHORIZATION and r.plan_run_id is None


def test_out_of_scope_proposal_waits_for_approval_and_resumes_only_on_valid_human_action(tmp_path):
    bad = draft(title="Switch to a larger basis", changes=[ParameterChange(name="basis", from_value="sto-3g",
                                                                           to_value="cc-pvdz", kind="invariant")])
    deps, linear, notion, writer, channel = build(tmp_path, drafts=[bad])
    onboard(deps, linear)
    poll(deps)
    assert runs(deps)["INU-900"].stage is WorkflowStage.DONE
    (child_id,) = linear.created
    assert linear.issues[child_id].state_name == "Awaiting Approval"
    page = next(p for p in notion.pages.values() if p.linear_issue_id == child_id)
    assert page.pending is True  # Phase 2 gate keeps it from running
    (req,) = [p for p in pending(deps) if p.kind is ApprovalKind.PROPOSAL]
    poll(deps)
    assert child_id not in runs(deps)  # not started without approval
    # bot change -> ignored
    linear.human_move(child_id, "Approved", bot={"name": "integration", "type": "app"})
    from research_autopilot.autopilot.approvals import record_submission
    from research_autopilot.autopilot.models import FormSubmission
    record_submission(deps.store, FormSubmission(request_id=req.request_id, decision="approve", spec_hash=req.spec_hash,
                                                 approver="someone"))
    poll(deps)
    assert child_id not in runs(deps) and pending(deps)
    # a valid human change (non-bot actor after the request) + matching form -> approved, task runs
    linear.human_move(child_id, "Awaiting Approval")
    approve(deps, linear, req.request_id)
    poll(deps)
    assert linear.issues[child_id].state_name in ("Todo", "Done")
    request_child(notion, child_id)
    poll(deps)
    assert runs(deps)[child_id].stage is WorkflowStage.DONE


def test_stale_approval_after_spec_edit_is_not_accepted(tmp_path):
    deps, linear, notion, *_ = build(tmp_path)
    poll(deps)
    (req,) = pending(deps)
    page_id = next(iter(notion.pages))
    p = notion.pages[page_id]
    notion.pages[page_id] = p.model_copy(update={"sections": {**p.sections, "acceptance_criteria": "- relaxed"}})
    approve(deps, linear, req.request_id)
    poll(deps)
    assert runs(deps)["INU-900"].stage is WorkflowStage.AWAITING_AUTHORIZATION
    from research_autopilot.autopilot.models import ApprovalRequest
    assert ApprovalRequest.model_validate(deps.store.get(db.approval_requests, req.request_id)).status.value == "stale"


def test_form_without_linear_change_or_mismatch_does_not_approve(tmp_path):
    deps, linear, *_ = build(tmp_path)
    poll(deps)
    (req,) = pending(deps)
    from research_autopilot.autopilot.approvals import record_submission
    from research_autopilot.autopilot.models import FormSubmission
    record_submission(deps.store, FormSubmission(request_id=req.request_id, decision="approve", spec_hash=req.spec_hash,
                                                 approver="x"))
    poll(deps)  # Linear still "Awaiting Approval"
    assert runs(deps)["INU-900"].stage is WorkflowStage.AWAITING_AUTHORIZATION
    linear.human_move("INU-900", "Revision Requested")  # Linear says revise, form says approve -> mismatch
    poll(deps)
    assert runs(deps)["INU-900"].stage is WorkflowStage.AWAITING_AUTHORIZATION


def test_autopilot_own_status_write_never_counts(tmp_path):
    deps, linear, *_ = build(tmp_path)
    poll(deps)
    (req,) = pending(deps)
    # pretend Autopilot itself had written "Approved" (it is forbidden; guard + audit)
    import pytest
    from research_autopilot.autopilot.linear_ops import WriteRefused
    with pytest.raises(WriteRefused):
        deps.ops.set_state(linear.get_issue("INU-900"), "Approved", "x")
    deps.store.record_linear_write("INU-900", "Approved", "simulated own write")
    approve(deps, linear, req.request_id)
    poll(deps)
    assert runs(deps)["INU-900"].stage is WorkflowStage.AWAITING_AUTHORIZATION


def test_reject_and_revision(tmp_path):
    deps, linear, *_ = build(tmp_path)
    poll(deps)
    (req,) = pending(deps)
    approve(deps, linear, req.request_id, decision="reject")
    poll(deps)
    assert runs(deps)["INU-900"].stage is WorkflowStage.CANCELLED
    deps2, linear2, *_ = build(tmp_path / "b")
    poll(deps2)
    (req2,) = pending(deps2)
    approve(deps2, linear2, req2.request_id, decision="revise")
    poll(deps2)
    r = runs(deps2)["INU-900"]
    assert r.stage is WorkflowStage.BLOCKED and "revision requested" in r.blocked_reason


def test_missing_linear_states_block_with_clear_reason(tmp_path):
    deps, linear, *_ = build(tmp_path, missing_states=("Awaiting Approval", "Approved", "Revision Requested"))
    poll(deps)
    r = runs(deps)["INU-900"]
    assert r.stage is WorkflowStage.DISCOVERED and "states missing" in r.events[-1]["message"]


def test_test_only_mode_refuses_production_issue(tmp_path):
    deps, linear, notion, *_ = build(tmp_path, root_title="Real research task")
    poll(deps)
    assert runs(deps) == {}
    assert next(iter(notion.pages.values())).execution_status == "Blocked"
    assert linear.issues["INU-900"].state_name == "Todo" and linear.histories["INU-900"] == []


def test_partial_failure_linear_create_is_reconciled_without_duplicates(tmp_path):
    deps, linear, notion, writer, _ = build(tmp_path)
    linear.fail_create = 1  # Notion page created, Linear create times out
    onboard(deps, linear)
    poll(deps)
    r = runs(deps)["INU-900"]
    assert r.stage is WorkflowStage.PROPOSING  # retried next poll, not duplicated
    poll(deps)
    assert runs(deps)["INU-900"].stage is WorkflowStage.DONE
    assert len(linear.created) == 1 and len(writer.created_tasks) == 1  # exactly one Notion page + one issue


def test_notes_failure_prevents_done_and_is_retried(tmp_path):
    deps, linear, notion, writer, _ = build(tmp_path)
    writer.fail_notes = 1
    onboard(deps, linear)
    poll(deps)
    r = runs(deps)["INU-900"]
    assert r.stage is WorkflowStage.COMPLETING and linear.issues["INU-900"].state_name != "Done"
    poll(deps)
    assert runs(deps)["INU-900"].stage is WorkflowStage.DONE and len(writer.notes) == 1


def test_validation_failure_never_done(tmp_path):
    bad = FakeComputeBackend([{"state": "succeeded", "exit_code": 0, "outputs": {"results/h2.json": '{"e_rhf": 1}'}}])
    deps, linear, *_ = build(tmp_path, compute=bad)
    deps.exp.impl_deps = None
    onboard(deps, linear)
    poll(deps)
    r = runs(deps)["INU-900"]
    assert r.stage is WorkflowStage.BLOCKED and linear.issues["INU-900"].state_name != "Done"


def test_unverifiable_criteria_or_fabricated_evidence_need_human_review(tmp_path):
    from phase5_fakes import FakeCompletionReviewer
    deps, linear, *_ = build(tmp_path, completion=FakeCompletionReviewer(evidence=("check:made_up",)))
    onboard(deps, linear)
    poll(deps)
    r = runs(deps)["INU-900"]
    assert r.stage is WorkflowStage.AWAITING_APPROVAL
    req = next(p for p in pending(deps) if p.kind is ApprovalKind.COMPLETION_REVIEW)
    assert any("evidence not backed" in d or "not established" in d for d in req.diff_from_original)
    approve(deps, linear, req.request_id)
    poll(deps)
    assert runs(deps)["INU-900"].stage is WorkflowStage.DONE


def test_refuted_hypothesis_with_met_criteria_is_success(tmp_path):
    from phase5_fakes import FakeCompletionReviewer
    deps, linear, *_ = build(tmp_path, completion=FakeCompletionReviewer(outcome="refuted"))
    onboard(deps, linear)
    poll(deps)
    assert runs(deps)["INU-900"].stage is WorkflowStage.DONE
    assert "refuted" in str(deps.store.find(db.workflow_runs))


def test_budget_exceeded_requires_approval(tmp_path):
    from research_autopilot.autopilot.engine import AutopilotSettings
    from research_autopilot.autopilot.models import Budget
    deps, linear, *_ = build(tmp_path, settings=AutopilotSettings(default_budget=Budget(api_usd=0.0001, compute_cpu_hours=4)))
    onboard(deps, linear)
    poll(deps)
    r = runs(deps)["INU-900"]
    assert r.stage is WorkflowStage.AWAITING_APPROVAL
    assert any(p.kind is ApprovalKind.BUDGET for p in pending(deps))


def test_proposal_limits_and_chain_depth(tmp_path):
    topics = ["thermal noise floor", "grid convergence order", "timing reproducibility", "seed independence", "io latency"]
    drafts = []
    for i, t in enumerate(topics):
        d = draft(title=f"p{i}", replicates=str(i + 2))
        drafts.append(d.model_copy(update={"research_question": f"How does {t} affect the benchmark? ({i})",
                                           "hypothesis": f"{t} has no effect beyond 1e-8 ({i}).",
                                           "implementation": f"Repeat the run to quantify {t}; variant {i * 7919}."}))
    deps, linear, *_ = build(tmp_path, drafts=drafts)
    deps.exp.settings.max_proposals_per_run = 5
    onboard(deps, linear)
    poll(deps)
    states = [linear.issues[i].state_name for i in linear.created]
    assert states.count("Todo") == 3  # policy: max 3 new proposals per experiment registered automatically
    assert states.count("Awaiting Approval") == 2  # the rest are registered but wait for a human
    assert len([p for p in pending(deps) if p.kind is ApprovalKind.PROPOSAL]) == 2


def test_concurrent_pollers_do_not_double_start(tmp_path):
    deps, linear, *_ = build(tmp_path)
    deps.store.init_schema()
    assert deps.store.acquire_lease("phase5:poll", "other", 600)
    assert poll(deps) == {"skipped": "another poll is running"}
    assert runs(deps) == {}
    from research_autopilot.storage import db
    assert deps.store.get(db.heartbeats, "autopilot_poll") is None  # a skipped tick is not a completed one


def test_completed_poll_leaves_a_heartbeat_for_the_watchdog(tmp_path):
    from research_autopilot.storage import db
    deps, linear, *_ = build(tmp_path)
    onboard(deps, linear)
    poll(deps)
    assert deps.store.get(db.heartbeats, "autopilot_poll")["at"]


def test_crash_mid_workflow_resumes_from_stage(tmp_path):
    deps, linear, *_ = build(tmp_path)
    onboard(deps, linear)
    import research_autopilot.autopilot.engine as eng
    orig = eng.run_experiment
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("api timeout")
        return orig(*a, **k)

    eng.run_experiment = flaky
    try:
        poll(deps)
        r = runs(deps)["INU-900"]
        assert r.stage is WorkflowStage.EXPERIMENTING and r.attempts == 1
        poll(deps)
        assert runs(deps)["INU-900"].stage is WorkflowStage.DONE
        assert len(runs(deps)["INU-900"].phase3_run_ids) >= 1
    finally:
        eng.run_experiment = orig


def test_dead_letter_after_repeated_failures(tmp_path):
    deps, linear, *_ = build(tmp_path)
    onboard(deps, linear)
    import research_autopilot.autopilot.engine as eng
    orig = eng.run_experiment
    eng.run_experiment = lambda *a, **k: (_ for _ in ()).throw(TimeoutError("down"))
    try:
        for _ in range(4):
            poll(deps)
        assert runs(deps)["INU-900"].stage is WorkflowStage.DEAD_LETTER
    finally:
        eng.run_experiment = orig


def test_status_report_and_dry_run(tmp_path):
    from research_autopilot.autopilot.engine import AutopilotSettings
    deps, linear, *_ = build(tmp_path, settings=AutopilotSettings(dry_run=True))
    s = poll(deps)
    assert s["discovered"] == ["INU-900"] and runs(deps) == {} and linear.histories["INU-900"] == []
    assert status_report(deps)["runs"] == []
