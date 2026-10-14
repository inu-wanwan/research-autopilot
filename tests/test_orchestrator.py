import json

import pytest

from phase2_fakes import (
    PAGE,
    FakeLinear,
    FakeNotion,
    make_deps,
    make_issue,
    make_page,
    make_plan,
    review,
)
from research_autopilot.config import ModelPrice
from research_autopilot.models import BlockedCode, DecisionStatus, IssueRef
from research_autopilot.orchestrator.core import WriteNotPermitted, plan_task
from research_autopilot.planning.agents import AgentConfigError, InvalidAgentOutput, TransientAgentError
from research_autopilot.tasks.errors import ConnectorAuthError, TransientConnectorError


def codes(record):
    return {r.code for r in record.decision.reasons}


def run(deps, mode="dry_run", task="INU-900", **kw):
    return plan_task(deps, task_id=task, mode=mode, **kw)


# ---------------------------------------------------------------- happy path


def test_ready_dry_run_plans_reviews_and_does_not_write(tmp_path):
    linear = FakeLinear(make_issue())
    deps = make_deps(tmp_path, FakeNotion(make_page()), linear, [make_plan()], [review("approved")])
    rec = run(deps)
    d = rec.decision
    assert d.status is DecisionStatus.READY
    assert d.reviewer_verdict == "approved" and d.plan_id == rec.plans[0].plan_id
    assert d.human_approval.approved is True
    assert d.linear_update.action == "skipped_dry_run"
    assert linear.writes == []  # dry-run: no external writes
    assert {s.system for s in d.sources} == {"linear", "notion"}
    assert rec.handoff_path is not None


def test_apply_moves_to_in_progress_never_done(tmp_path):
    linear = FakeLinear(make_issue())
    deps = make_deps(tmp_path, FakeNotion(make_page()), linear, [make_plan()], [review("approved")],
                     linear_write_enabled=True)
    rec = run(deps, mode="apply")
    assert rec.decision.status is DecisionStatus.READY
    assert rec.decision.linear_update.action == "updated"
    assert linear.writes == [("uuid-INU-900", "In Progress")]
    assert all(state != "Done" for _, state in linear.writes)
    audit = deps.store.linear_audit_rows("INU-900")
    assert [(a["action"], a["from_state"], a["to_state"]) for a in audit] == [("updated", "Todo", "In Progress")]


def test_apply_requires_write_permission_setting(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()), [], [])
    with pytest.raises(WriteNotPermitted):
        run(deps, mode="apply")


def test_apply_refuses_target_state_that_is_not_started(tmp_path):
    linear = FakeLinear(make_issue())
    deps = make_deps(tmp_path, FakeNotion(make_page()), linear, [make_plan()], [review("approved")],
                     linear_write_enabled=True, in_progress_state_name="Done")
    rec = run(deps, mode="apply")
    assert rec.decision.status is DecisionStatus.BLOCKED
    assert rec.decision.linear_update.action == "failed"
    assert linear.writes == []


def test_linear_update_transient_failure(tmp_path):
    linear = FakeLinear(make_issue())
    linear.raise_on_set = TransientConnectorError("linear", "HTTP 503")
    deps = make_deps(tmp_path, FakeNotion(make_page()), linear, [make_plan()], [review("approved")],
                     linear_write_enabled=True)
    rec = run(deps, mode="apply")
    assert rec.decision.status is DecisionStatus.TRANSIENT_FAILURE
    assert rec.decision.linear_update.action == "failed"
    assert rec.handoff_path is None
    assert deps.store.linear_audit_rows()[0]["action"] == "failed"


def test_check_only_makes_no_llm_calls(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()), [], [])
    rec = run(deps, mode="check_only")
    assert rec.decision.status is DecisionStatus.READY
    assert rec.llm_calls == [] and rec.plans == []
    assert rec.cost.estimated_cost_usd == 0.0


# ---------------------------------------------------------------- deterministic gates


@pytest.mark.parametrize("state,code", [
    (("Backlog", "backlog"), BlockedCode.NOT_APPROVED),
    (("In Progress", "started"), BlockedCode.INVALID_STATUS),
    (("Done", "completed"), BlockedCode.INVALID_STATUS),
])
def test_status_gates(tmp_path, state, code):
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue(state=state)), [], [])
    rec = run(deps)
    assert rec.decision.status is DecisionStatus.BLOCKED
    assert code in codes(rec)
    assert deps.planner.calls == []  # gates fail before any LLM call


def test_pending_in_notion_blocks_as_not_approved(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(make_page(pending=True)), FakeLinear(make_issue()), [], [])
    rec = run(deps)
    assert codes(rec) == {BlockedCode.NOT_APPROVED}
    assert rec.decision.human_approval.approved is False


def test_out_of_scope_without_label(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue(labels=())), [], [])
    assert codes(run(deps)) == {BlockedCode.OUT_OF_SCOPE}


def test_out_of_scope_other_project(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue(project="other")), [], [])
    assert codes(run(deps)) == {BlockedCode.OUT_OF_SCOPE}


def test_incomplete_dependency_blocks(tmp_path):
    blockers = (IssueRef(identifier="INU-1", title="a", state_name="Todo", state_type="unstarted"),
                IssueRef(identifier="INU-2", title="b", state_name="Done", state_type="completed"))
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue(blocked_by=blockers)), [], [])
    rec = run(deps)
    assert codes(rec) == {BlockedCode.DEPENDENCY_INCOMPLETE}
    (reason,) = rec.decision.reasons
    assert [b["identifier"] for b in reason.details["blocked_by"]] == ["INU-1"]


def test_missing_sections_is_insufficient_spec(tmp_path):
    page = make_page(sections={"purpose": "x"})
    deps = make_deps(tmp_path, FakeNotion(page), FakeLinear(make_issue()), [], [])
    rec = run(deps)
    assert rec.decision.status is DecisionStatus.INSUFFICIENT_SPEC
    assert rec.decision.reasons[0].details["missing_sections"] == ["specification", "acceptance_criteria"]


def test_placeholder_sections_are_insufficient_spec(tmp_path):
    page = make_page(sections={"purpose": "Check the gate.", "specification": "意図的に未記入。手順は未確定。",
                               "acceptance_criteria": "TBD"})
    deps = make_deps(tmp_path, FakeNotion(page), FakeLinear(make_issue()), [], [])
    rec = run(deps)
    assert rec.decision.status is DecisionStatus.INSUFFICIENT_SPEC
    assert rec.decision.reasons[0].details["placeholder_sections"] == ["specification", "acceptance_criteria"]
    assert deps.planner.calls == []


def test_long_section_mentioning_todo_is_not_placeholder():
    from research_autopilot.tasks.gates import is_placeholder
    assert not is_placeholder("Implement the solver. " * 10 + "TODO markers in code must be removed.")
    assert is_placeholder("N/A")


def test_no_linked_notion_page_is_insufficient_spec(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(), FakeLinear(make_issue(notion_ids=())), [], [])
    assert run(deps).decision.status is DecisionStatus.INSUFFICIENT_SPEC


def test_missing_spec_plus_other_problem_is_blocked(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(make_page(sections={})), FakeLinear(make_issue(labels=())), [], [])
    rec = run(deps)
    assert rec.decision.status is DecisionStatus.BLOCKED
    assert codes(rec) == {BlockedCode.OUT_OF_SCOPE, BlockedCode.MISSING_SPEC}


@pytest.mark.parametrize("page,issue", [
    (make_page(linear_id="INU-999"), make_issue()),                          # Notion points elsewhere
    (make_page(linear_id=None), make_issue()),                               # no back-reference
    (make_page(), make_issue(notion_ids=("99999999-2222-3333-4444-555555555555",))),  # Linear links another page
])
def test_notion_linear_inconsistency(tmp_path, page, issue):
    deps = make_deps(tmp_path, FakeNotion(page), FakeLinear(issue), [], [])
    rec = run(deps)
    assert rec.decision.status is DecisionStatus.BLOCKED
    assert BlockedCode.DATA_INCONSISTENCY in codes(rec)


def test_duplicate_notion_pages_are_inconsistent(tmp_path):
    other = make_page(page_id="22222222-2222-3333-4444-555555555555")
    deps = make_deps(tmp_path, FakeNotion(make_page(), other), FakeLinear(make_issue()), [], [])
    assert BlockedCode.DATA_INCONSISTENCY in codes(run(deps))


# ---------------------------------------------------------------- connector failures


def test_transient_connector_failure(tmp_path):
    linear = FakeLinear(make_issue())
    linear.raise_on_get = TransientConnectorError("linear", "gave up after 3 attempts: HTTP 503")
    deps = make_deps(tmp_path, FakeNotion(make_page()), linear, [], [])
    rec = run(deps)
    assert rec.decision.status is DecisionStatus.TRANSIENT_FAILURE
    assert codes(rec) == {BlockedCode.TRANSIENT_ERROR}


def test_auth_failure_is_blocked_not_transient(tmp_path):
    notion = FakeNotion(make_page())
    notion.raise_on_get = ConnectorAuthError("notion", "HTTP 401 unauthorized")
    deps = make_deps(tmp_path, notion, FakeLinear(make_issue()), [], [])
    rec = run(deps)
    assert rec.decision.status is DecisionStatus.BLOCKED
    assert codes(rec) == {BlockedCode.CONNECTOR_ERROR}


def test_unknown_task_is_not_found(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(), FakeLinear(), [], [])
    assert codes(run(deps, task="INU-404")) == {BlockedCode.NOT_FOUND}


# ---------------------------------------------------------------- planning loop


def test_revision_then_approved(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()),
                     [make_plan(), make_plan(3)], [review("revision_required"), review("approved")])
    rec = run(deps)
    assert rec.decision.status is DecisionStatus.READY
    assert len(rec.plans) == 2 and len(rec.reviews) == 2
    # second planner call received the previous plan and the reviewer's requests
    _, previous, feedback = deps.planner.calls[1]
    assert previous == rec.plans[0].plan and feedback[0].requested_change == "define metric"
    assert rec.decision.plan_id == rec.plans[1].plan_id


def test_max_two_replans_then_blocked(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()),
                     [make_plan()] * 3, [review("revision_required")] * 3)
    rec = run(deps)
    assert rec.decision.status is DecisionStatus.BLOCKED
    assert codes(rec) == {BlockedCode.REVISION_LIMIT}
    assert len(deps.planner.calls) == 3  # initial + 2 replans
    assert len(deps.reviewer.calls) == 3  # every plan reviewed
    assert rec.handoff_path is None


def test_reviewer_blocked(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()), [make_plan()], [review("blocked")])
    rec = run(deps)
    assert rec.decision.status is DecisionStatus.BLOCKED
    assert codes(rec) == {BlockedCode.REVIEW_BLOCKED}
    assert "spec contradicts invariants" in rec.decision.reasons[0].message


def test_invalid_llm_output_retried_once_then_blocked(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()),
                     [InvalidAgentOutput("bad json"), InvalidAgentOutput("bad json")], [])
    rec = run(deps)
    assert rec.decision.status is DecisionStatus.BLOCKED
    assert codes(rec) == {BlockedCode.LLM_OUTPUT_INVALID}
    assert [c.ok for c in rec.llm_calls] == [False, False]


def test_invalid_llm_output_recovers_on_retry(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()),
                     [InvalidAgentOutput("bad json"), make_plan()], [review("approved")])
    rec = run(deps)
    assert rec.decision.status is DecisionStatus.READY
    assert any(e.kind == "retry" for e in rec.events)


def test_llm_transient_errors_exhaust_to_transient_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("research_autopilot.planning.loop.time.sleep", lambda s: None)
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()),
                     [TransientAgentError("RateLimitError")] * 3, [])
    rec = plan_task(deps, task_id="INU-900")
    assert rec.decision.status is DecisionStatus.TRANSIENT_FAILURE
    assert len(rec.llm_calls) == 3
    assert deps.store.active_lease("INU-900") is None  # lease released


def test_llm_quota_error_is_blocked(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()),
                     [AgentConfigError("insufficient_quota")], [])
    rec = run(deps)
    assert rec.decision.status is DecisionStatus.BLOCKED
    assert codes(rec) == {BlockedCode.CONNECTOR_ERROR}


# ---------------------------------------------------------------- final re-check


def test_spec_edited_during_planning_blocks(tmp_path):
    notion = FakeNotion(make_page())

    def edit_after_first(n):
        if n.get_calls == 1:
            n.pages[PAGE] = make_page(edited="2026-10-10T01:00:00.000Z")

    notion.after_get = edit_after_first
    linear = FakeLinear(make_issue())
    deps = make_deps(tmp_path, notion, linear, [make_plan()], [review("approved")], linear_write_enabled=True)
    rec = run(deps, mode="apply")
    assert rec.decision.status is DecisionStatus.BLOCKED
    assert BlockedCode.STATE_CHANGED in codes(rec)
    assert linear.writes == []


def test_status_changed_during_planning_blocks(tmp_path):
    linear = FakeLinear(make_issue())

    class MovingReviewer:
        model = "gpt-5.6-sol"

        def review(self, spec, plan, previous=None):
            linear.issues["INU-900"] = make_issue(state=("Backlog", "backlog"))
            from research_autopilot.planning.agents import AgentRun, Usage
            return AgentRun(output=review("approved"), usage=Usage(), model=self.model)

    deps = make_deps(tmp_path, FakeNotion(make_page()), linear, [make_plan()], [], linear_write_enabled=True)
    deps.reviewer = MovingReviewer()
    rec = run(deps, mode="apply")
    assert rec.decision.status is DecisionStatus.BLOCKED
    assert BlockedCode.NOT_APPROVED in codes(rec)
    assert linear.writes == []


# ---------------------------------------------------------------- duplicate prevention


def test_concurrent_run_is_blocked_by_lease(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()), [], [])
    deps.store.init_schema()
    assert deps.store.acquire_lease("INU-900", "other-run", 3600)
    rec = run(deps)
    assert codes(rec) == {BlockedCode.CONCURRENT_RUN}
    assert rec.decision.reasons[0].details["plan_run_id"] == "other-run"
    assert deps.planner.calls == []


def test_expired_lease_is_taken_over(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()), [make_plan()], [review("approved")])
    deps.store.init_schema()
    assert deps.store.acquire_lease("INU-900", "stale-run", 3600)  # expired below
    from research_autopilot.storage import db
    with deps.store.engine.begin() as conn:
        conn.execute(db.task_leases.update().values(expires_at=db.utcnow().replace(year=2000)))
    assert run(deps).decision.status is DecisionStatus.READY


def test_lease_released_after_run(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()), [make_plan()], [review("approved")])
    run(deps)
    assert deps.store.active_lease("INU-900") is None


def test_apply_twice_is_idempotent(tmp_path):
    linear = FakeLinear(make_issue())
    deps = make_deps(tmp_path, FakeNotion(make_page()), linear, [make_plan()], [review("approved")],
                     linear_write_enabled=True)
    run(deps, mode="apply")
    second = run(deps, mode="apply")  # now In Progress -> gate blocks, no second write
    assert second.decision.status is DecisionStatus.BLOCKED
    assert codes(second) == {BlockedCode.INVALID_STATUS}
    assert len(linear.writes) == 1


# ---------------------------------------------------------------- next candidate


def test_next_selects_first_eligible_by_priority(tmp_path):
    a = make_issue("INU-901", priority=0, notion_ids=("aaaaaaaa-2222-3333-4444-555555555555",))
    b = make_issue("INU-902", priority=2, notion_ids=("bbbbbbbb-2222-3333-4444-555555555555",))
    c = make_issue("INU-903", priority=1, notion_ids=("cccccccc-2222-3333-4444-555555555555",))
    pages = [make_page("aaaaaaaa-2222-3333-4444-555555555555", "INU-901"),
             make_page("bbbbbbbb-2222-3333-4444-555555555555", "INU-902"),
             make_page("cccccccc-2222-3333-4444-555555555555", "INU-903", pending=True)]
    deps = make_deps(tmp_path, FakeNotion(*pages), FakeLinear(a, b, c), [make_plan()], [review("approved")])
    rec = plan_task(deps, next_task=True)
    # INU-903 (urgent-er) is pending -> skipped; INU-902 (High) beats INU-901 (no priority)
    assert rec.task_id == "INU-902"
    assert any("skipped INU-903" in e.message for e in rec.events)


def test_next_with_no_eligible_candidate(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(make_page(pending=True)), FakeLinear(make_issue()), [], [])
    rec = plan_task(deps, next_task=True)
    assert rec.decision.status is DecisionStatus.BLOCKED
    assert rec.decision.reasons[0].details["skipped"][0]["task_id"] == "INU-900"


# ---------------------------------------------------------------- persistence


def test_record_persisted_to_db_and_json(tmp_path):
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()), [make_plan()], [review("approved")],
                     model_pricing={"gpt-5.6-terra": ModelPrice(input_per_1m=1.0, output_per_1m=10.0)})
    rec = run(deps)
    stored = deps.store.get_run(rec.plan_run_id)
    assert stored.decision.status is DecisionStatus.READY
    assert stored.linear_snapshot.identifier == "INU-900"
    assert stored.notion_snapshot.last_edited_time == "2026-10-10T00:00:00.000Z"
    assert [(c.agent, c.model, c.input_tokens, c.output_tokens) for c in stored.llm_calls] == [
        ("planner", "gpt-5.6-terra", 1000, 200), ("reviewer", "gpt-5.6-sol", 1000, 200)]
    # planner price known (0.001 + 0.002), reviewer price unknown -> total not estimable
    assert stored.llm_calls[0].estimated_cost_usd == pytest.approx(0.003)
    assert stored.llm_calls[1].estimated_cost_usd is None
    assert stored.cost.estimated_cost_usd is None and stored.cost.cost_complete is False
    assert stored.cost.input_tokens == 2000
    row = deps.store.list_runs("INU-900")[0]
    assert row["status"] == "ready" and row["planner_model"] == "gpt-5.6-terra"
    on_disk = json.loads(open(rec.artifact_path).read())
    assert on_disk["plan_run_id"] == rec.plan_run_id
    handoff = json.loads(open(rec.handoff_path).read())
    assert handoff["schema_version"] == "1" and handoff["plan_id"] == rec.decision.plan_id
    assert handoff["acceptance_criteria"].startswith("- Energy error table")


def test_cost_estimated_when_all_prices_known(tmp_path):
    prices = {m: ModelPrice(input_per_1m=1.0, output_per_1m=10.0) for m in ("gpt-5.6-terra", "gpt-5.6-sol")}
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()), [make_plan()], [review("approved")],
                     model_pricing=prices)
    assert run(deps).cost.estimated_cost_usd == pytest.approx(0.006)


def test_no_secrets_in_persisted_record(tmp_path):
    from pydantic import SecretStr
    deps = make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()), [make_plan()], [review("approved")])
    deps.settings.openai_api_key = SecretStr("sk-test-SECRET")
    deps.settings.linear_api_key = SecretStr("lin_api_SECRET")
    rec = run(deps)
    blob = open(rec.artifact_path).read() + json.dumps(deps.store.list_runs(), default=str)
    assert "SECRET" not in blob


def test_handoff_carries_phase3_fields_and_mode(tmp_path):
    sections = {**__import__("phase2_fakes").FULL_SECTIONS, "repository": "h2-test",
                "file_scope": "- src/**\n- tests/**", "non_goals": "No production runs"}
    linear = FakeLinear(make_issue())
    deps = make_deps(tmp_path, FakeNotion(make_page(sections=sections)), linear, [make_plan()], [review("approved")],
                     linear_write_enabled=True)
    rec = run(deps, mode="apply")
    handoff = json.loads(open(rec.handoff_path).read())
    assert handoff["phase2_mode"] == "apply" and handoff["repository"] == "h2-test"
    assert handoff["file_scope"].startswith("- src/**") and handoff["non_goals"] == "No production runs"


def test_old_handoff_without_phase3_fields_still_loads():
    from research_autopilot.models import ExecutionHandoff
    from phase2_fakes import make_plan as mp
    old = {"schema_version": "1", "task_id": "INU-1", "plan_id": "p", "plan_run_id": "r", "decision_id": "d",
           "sources": [], "purpose": None, "scientific_invariants": None, "acceptance_criteria": None,
           "artifact_requirements": None, "plan": mp().model_dump(mode="json"),
           "review": review("approved").model_dump(mode="json"),
           "human_approval": {"linear_state": "Todo", "linear_state_type": "unstarted", "notion_pending": False,
                              "approved": True},
           "approvals_needed": [], "open_questions": []}
    h = ExecutionHandoff.model_validate(old)
    assert h.phase2_mode is None and h.repository is None


def test_planner_and_plan_reviewer_hand_overs_go_to_logs(tmp_path):
    """User request 2026-10-14: #logs shows every agent hand-over in planning (planner <-> plan reviewer)."""
    class Recorder:
        persistent = True

        def __init__(self):
            self.events = []

        def send(self, event):
            self.events.append(event)
    linear = FakeLinear(make_issue())
    deps = make_deps(tmp_path, FakeNotion(make_page()), linear, [make_plan(), make_plan()],
                     [review("revision_required"), review("approved")])
    deps.notifier = Recorder()
    rec = run(deps)
    assert rec.decision.status is DecisionStatus.READY
    msgs = [e.details[0].split(" (")[0] for e in deps.notifier.events]
    assert msgs == ["planner round 1 started", "planner round 1 finished", "plan review round 1 started",
                    "plan review round 1 finished: revision_required", "planner round 2 started",
                    "planner round 2 finished", "plan review round 2 started", "plan review round 2 finished: approved"]
    assert all(e.kind.value == "phase_transition" and e.flow_run_name == "INU-900" for e in deps.notifier.events)
    assert sum(1 for e in rec.events if e.kind == "agent") == 8
