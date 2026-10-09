from datetime import timedelta

import pytest

from phase4_fakes import sci_review
from phase5_fakes import ACC, INV, build, draft
from research_autopilot.autopilot import policy
from research_autopilot.autopilot.models import OutboxItem, OutboxState, ParameterDomain, ResearchAuthorization
from research_autopilot.autopilot.outbox import marker
from research_autopilot.experiments.models import ExperimentProposal, ParameterChange, ReviewedExperimentProposal, ScopeAssessment
from research_autopilot.models import utcnow


def auth(**kw):
    base = dict(root_task_id="INU-900", approved_by="r", approval_request_id="x", spec_hash="h", purpose="p",
                research_target="t", hypothesis=None, scientific_invariants=INV, acceptance_criteria=ACC)
    base.update(kw)
    return ResearchAuthorization(**base)


def rp(d=None, verdict="accept", within=True):
    d = d or draft()
    return ReviewedExperimentProposal(
        proposal=ExperimentProposal(dedupe_key="k", parent_task_id="INU-900", source_experiment_run_id="e",
                                    source_artifacts=[], source_commit="c", chain_depth=1, draft=d),
        review=sci_review(verdict) if verdict else None, review_error=None if verdict else "unavailable",
        scope_assessment=ScopeAssessment(within_scope=within, rationale="r", proposed_changes=[],
                                         requires_human_decision=not within, deterministic_findings=[], reviewer_opinion=within),
        task_spec_ref={}, execution_plan_ref={}, spec_diff=[], limits_metadata={})


def ev(a, r, **kw):
    args = dict(chain_depth=1, proposals_from_experiment=1, pending_proposals=0, used_api_usd=0.1, used_cpu_hours=0.1)
    args.update(kw)
    return policy.evaluate_proposal(a, r, **args)


def test_auto_within_domain():
    assert ev(auth(), rp()).outcome == "auto"


@pytest.mark.parametrize("d,cat", [
    (draft(invariants=["changed"]), "invariant_change"),
    (draft(acceptance=ACC[:1]), "acceptance_change"),
    (draft(changes=[ParameterChange(name="molecule", from_value="H2", to_value="LiH", kind="new_target")]), "target_change"),
    (draft(changes=[ParameterChange(name="metric", from_value="a", to_value="b", kind="objective")]), "metric_change"),
    (draft(changes=[ParameterChange(name="solver", from_value="a", to_value="b", kind="method")]), "undecidable_scope"),
    (draft(changes=[ParameterChange(name="geometry", from_value="0.74", to_value="0.9", kind="within_spec")]), "undecidable_scope"),
    (draft(replicates="50"), "undecidable_scope"),
    (draft(changes=[]), "undecidable_scope"),
])
def test_needs_human(d, cat):
    dec = ev(auth(), rp(d))
    assert dec.outcome == "human" and cat in dec.categories


def test_llm_within_scope_is_not_sufficient_and_cannot_loosen():
    assert ev(auth(), rp(within=False)).outcome == "human"
    assert ev(auth(), rp(draft(invariants=["x"]), within=True)).outcome == "human"


def test_reviewer_must_accept():
    assert ev(auth(), rp(verdict="revise")).outcome == "reject"
    assert ev(auth(), rp(verdict=None)).outcome == "reject"


def test_limits_budget_and_authorization_validity():
    assert "limit" in ev(auth(), rp(), chain_depth=4).categories
    assert "limit" in ev(auth(), rp(), proposals_from_experiment=4).categories
    assert "limit" in ev(auth(), rp(), pending_proposals=5).categories
    assert "budget_exceeded" in ev(auth(), rp(), used_api_usd=5.0).categories
    assert "budget_exceeded" in ev(auth(), rp(), used_cpu_hours=4.0).categories
    assert "budget_exceeded" in ev(auth(), rp(), unknown_api_cost_calls=1).categories
    assert ev(auth(revoked=True), rp()).outcome == "human"
    assert ev(auth(expires_at=utcnow() - timedelta(days=1)), rp()).outcome == "human"
    assert ev(None, rp()).outcome == "human"


def test_custom_parameter_domain():
    a = auth()
    a.policy.parameter_domains.append(ParameterDomain(name="n_points", kind="integer_range", min_value=1, max_value=20))
    d = draft(changes=[ParameterChange(name="n_points", from_value="5", to_value="10", kind="within_spec")])
    assert ev(a, rp(d)).outcome == "auto"


def test_spec_hash_ignores_whitespace_but_not_content():
    h1 = policy.spec_hash("p", ["a  b"], ["c"], "s", "t")
    assert h1 == policy.spec_hash("p", ["a b"], ["c"], "s", "t")
    assert h1 != policy.spec_hash("p", ["a b"], ["c relaxed"], "s", "t")


def test_outbox_adopts_remote_object_after_crash(tmp_path):
    deps, linear, notion, writer, _ = build(tmp_path)
    deps.store.init_schema()
    item = OutboxItem(key="proposal:abc", kind="notes", payload={"title": "Result", "blocks": []})
    # crash after the remote create, before the local state update
    writer.create_note(f"Result {marker(item.key)}", [])
    deps.outbox.enqueue(item)
    done = deps.outbox.process(deps.outbox.get(item.key))
    assert done.state is OutboxState.DONE and len(writer.notes) == 1


def test_outbox_dead_letter(tmp_path):
    deps, linear, notion, writer, _ = build(tmp_path)
    deps.store.init_schema()
    writer.fail_notes = 10
    deps.outbox.enqueue(OutboxItem(key="notes:x", kind="notes", payload={"title": "t", "blocks": []}))
    for _ in range(5):
        it = deps.outbox.process(deps.outbox.get("notes:x"))
    assert it.state is OutboxState.DEAD and "502" in it.last_error
    assert deps.outbox.enqueue(OutboxItem(key="notes:x", kind="notes", payload={})).state is OutboxState.DEAD  # idempotent


def test_linear_ops_guards(tmp_path):
    from research_autopilot.autopilot.linear_ops import LinearOps, WriteRefused
    deps, linear, *_ = build(tmp_path)
    deps.store.init_schema()
    issue = linear.get_issue("INU-900")
    off = LinearOps(linear, deps.store, test_only=True, write_enabled=False)
    with pytest.raises(WriteRefused):
        off.set_state(issue, "Todo", "x")
    on = deps.ops
    for forbidden in ("Approved", "Revision Requested", "Canceled"):
        with pytest.raises(WriteRefused):
            on.set_state(issue, forbidden, "x")
    on.set_state(issue, "In Progress", "x")
    assert deps.store.linear_writes_for("INU-900")[-1]["to_state"] == "In Progress"


def test_notifications_are_deduplicated(tmp_path, recorder):
    from research_autopilot.autopilot.engine import _notify
    from research_autopilot.autopilot.models import WorkflowRun
    from research_autopilot.notifications.events import EventKind
    deps, *_ = build(tmp_path)
    deps.store.init_schema()
    run = WorkflowRun(task_id="INU-900", root_task_id="INU-900")
    for _ in range(3):
        _notify(deps, run, EventKind.FAILED, "blocked:x", ["reason"])
    assert len(recorder.events) == 1 and recorder.events[0].details == ["reason"]
