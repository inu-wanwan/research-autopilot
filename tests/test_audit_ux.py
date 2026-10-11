"""Cross-phase UX policy (docs/phase_specs/ux_policy.md) and the 2026-10-12 audit findings."""

import asyncio
from datetime import timedelta

import discord
import pytest

from conftest import RecordingNotifier
from phase5_fakes import build
from research_autopilot.autopilot import engine
from research_autopilot.autopilot.engine import notify_approval, poll
from research_autopilot.autopilot.models import (ApprovalKind, ApprovalRequest, ApprovalStatus, WorkflowRun,
                                                 WorkflowStage)
from research_autopilot.models import utcnow
from research_autopilot.notifications.discord import (DeliveryState, DiscordControlInput, DiscordDelivery,
                                                      DiscordGatewayClient, DiscordNotifier, DiscordRoute,
                                                      InteractionResult, approval_closure,
                                                      control_deliveries_to_close, embed_payload, route_event)
from research_autopilot.notifications.events import EventKind, NotificationEvent
from research_autopilot.notifications.guidance import APPROVAL_TEMPLATES, approval_event, block_steps
from research_autopilot.storage import db
from research_autopilot.storage.db import Store
from test_discord_notifications import _submit, settings


def _store(tmp_path) -> Store:
    store = Store(f"sqlite:///{tmp_path / 'db.sqlite'}")
    store.init_schema()
    return store


def _run(deps) -> WorkflowRun:
    return WorkflowRun.model_validate(deps.store.find(db.workflow_runs, task_id="INU-900")[0])


def _req(deps, request_id) -> ApprovalRequest:
    return ApprovalRequest.model_validate(deps.store.get(db.approval_requests, request_id))


def _edit_spec(notion, **sections):
    page_id = next(iter(notion.pages))
    page = notion.pages[page_id]
    notion.pages[page_id] = page.model_copy(update={"sections": {**page.sections, **sections}})


# ---------------------------------------------------------------- routing / embed (policy section 2)


def test_human_actions_route_to_approvals_and_buttons_are_channel_bound(tmp_path):
    assert route_event(NotificationEvent(kind=EventKind.UNRECOVERABLE_ERROR, flow_name="x",
                                         needs_human=True)) is DiscordRoute.APPROVALS
    assert route_event(NotificationEvent(kind=EventKind.PHASE_TRANSITION, flow_name="x", control_task_id="T",
                                         control_actions=["cancel"])) is DiscordRoute.APPROVALS
    assert route_event(NotificationEvent(kind=EventKind.ANALYSIS_COMPLETED, flow_name="x", control_task_id="T",
                                         control_actions=["stop"])) is DiscordRoute.RESULTS
    store = _store(tmp_path)
    notifier = DiscordNotifier(store, settings())
    notifier.send(NotificationEvent(kind=EventKind.PHASE_TRANSITION, flow_name="x", control_task_id="T",
                                    control_actions=["stop"], dedupe_key="log-with-stop"))
    (delivery,) = notifier.pending()
    assert delivery.route is DiscordRoute.LOGS
    assert delivery.event.control_actions == [] and delivery.event.control_task_id is None  # logs: no buttons


def test_embed_has_numbered_steps_and_links():
    event = NotificationEvent(kind=EventKind.UNRECOVERABLE_ERROR, flow_name="autopilot", needs_human=True,
                              details=["止めました", "Notion: https://www.notion.so/abc", "Linear: https://linear.app/x/issue/INU-1"],
                              steps=["Notion の仕様を直す", "Run Autopilot を押す"])
    payload = embed_payload(event)
    fields = {f["name"]: f["value"] for f in payload["fields"]}
    assert fields["次にやること"] == "1. Notion の仕様を直す\n2. Run Autopilot を押す"
    assert "https://www.notion.so/abc" in fields["リンク"] and "INU-1" in fields["リンク"]
    assert "notion.so" not in payload["description"]
    assert "1. Notion の仕様を直す" in event.body()  # ntfy / logging get the steps too


@pytest.mark.parametrize("action,channel,result", [
    ("cancel", 301, None),
    ("cancel", 302, InteractionResult.WRONG_CHANNEL),
    ("resume", 302, InteractionResult.WRONG_CHANNEL),
    ("resume", 304, InteractionResult.WRONG_CHANNEL),
    ("stop", 302, None),  # results offer Stop on running research
    ("stop", 303, InteractionResult.WRONG_CHANNEL),
])
def test_controls_only_in_approvals_channel(tmp_path, action, channel, result):
    service = DiscordControlInput(_store(tmp_path), settings())
    assert service.allowed(action=action, user_id=200, guild_id=100, channel_id=channel) is result
    assert service.allowed(action=action, user_id=999, guild_id=100, channel_id=301) is InteractionResult.UNAUTHORIZED


def test_cancel_button_asks_for_confirmation_first(tmp_path):
    from research_autopilot.notifications.discord import CancelConfirmView, ControlButton
    store = _store(tmp_path)
    sent = {}

    class Response:
        async def send_message(self, text, ephemeral=False, view=None):
            sent.update(text=text, ephemeral=ephemeral, view=view)

    class User:
        id, bot = 200, False

    class Interaction:
        id, guild_id, channel_id, user, response = 1, 100, 301, User(), Response()

    async def click():
        button = ControlButton(DiscordControlInput(store, settings()), "INU-900", "cancel")
        await button.callback(Interaction())

    asyncio.run(click())
    assert sent["ephemeral"] and isinstance(sent["view"], CancelConfirmView)
    assert store.find(db.research_controls) == []  # nothing recorded before the confirmation


# ---------------------------------------------------------------- approvals: versions (finding 2)


def test_decided_request_is_not_returned_again_a_new_version_is_created(tmp_path):
    deps, linear, *_ = build(tmp_path, approval_mode="discord")
    deps.store.init_schema()
    issue = linear.get_issue("INU-900")
    args = dict(kind=ApprovalKind.BUDGET, issue=issue, root_task_id="INU-900", spec_hash="h", title="budget",
                what_changes=["over"], diff=[], budget_summary="used API $3 / $2", dedupe="run:planning")
    first = deps.approvals.request(**args)
    assert deps.approvals.request(**args).request_id == first.request_id  # pending: idempotent
    first.status = ApprovalStatus.APPROVED
    deps.approvals._save(first)
    second = deps.approvals.request(**args)
    assert second.request_id != first.request_id and second.status is ApprovalStatus.PENDING
    assert second.payload["_dedupe"].endswith("#2") and second.payload["_version"] == 2


# ---------------------------------------------------------------- stale / expired Phase 5 approvals (finding 1, 4)


def test_spec_edit_makes_onboarding_stale_and_the_run_asks_again(tmp_path):
    deps, linear, notion, *_ = build(tmp_path, approval_mode="discord")
    poll(deps)
    (old,) = deps.approvals.pending()
    assert _run(deps).stage is WorkflowStage.AWAITING_AUTHORIZATION
    _edit_spec(notion, acceptance_criteria="- FCI energy below RHF\n- changed after the request")
    poll(deps)
    stale = _req(deps, old.request_id)
    (new,) = deps.approvals.pending()
    assert stale.status is ApprovalStatus.STALE and stale.payload["_superseded_by"] == new.request_id
    assert new.payload["_supersedes"] == old.request_id
    run = _run(deps)
    assert run.stage is WorkflowStage.AWAITING_AUTHORIZATION and run.waiting_for == new.request_id
    _submit(deps, new, "approve")
    poll(deps)
    assert _run(deps).stage is WorkflowStage.DONE


def test_expired_onboarding_is_asked_again_and_onboarding_rerun_after_reject_does_not_hang(tmp_path):
    deps, linear, notion, *_ = build(tmp_path, approval_mode="discord")
    poll(deps)
    (old,) = deps.approvals.pending()
    old.expires_at = utcnow() - timedelta(seconds=1)
    deps.approvals._save(old)
    poll(deps)
    assert _req(deps, old.request_id).status is ApprovalStatus.EXPIRED
    (new,) = deps.approvals.pending()  # same spec hash: a new version, not the expired request
    assert new.request_id != old.request_id and _run(deps).waiting_for == new.request_id


def test_completion_review_stale_is_asked_again(tmp_path):
    from phase5_fakes import FakeCompletionReviewer
    deps, linear, notion, *_ = build(tmp_path, approval_mode="discord",
                                     completion=FakeCompletionReviewer(evidence=("check:made_up",)))
    poll(deps)
    (onboarding,) = deps.approvals.pending()
    _submit(deps, onboarding, "approve")
    poll(deps)
    review = next(r for r in deps.approvals.pending() if r.kind is ApprovalKind.COMPLETION_REVIEW)
    assert _run(deps).stage is WorkflowStage.AWAITING_APPROVAL
    review.expires_at = utcnow() - timedelta(seconds=1)
    deps.approvals._save(review)
    poll(deps)
    again = next(r for r in deps.approvals.pending() if r.kind is ApprovalKind.COMPLETION_REVIEW)
    assert again.request_id != review.request_id
    assert _run(deps).waiting_for == again.request_id


def test_closed_approval_messages_are_edited_not_answered_with_a_new_note(tmp_path):
    store = _store(tmp_path)
    old = ApprovalRequest(kind="research_scope", task_id="INU-1", root_task_id="INU-1", spec_hash="a", title="v1",
                          what_changes=[], diff_from_original=[], budget_summary="-", status="stale",
                          payload={"_dedupe": "k1"})
    store.put(db.approval_requests, old.request_id, old.model_dump(mode="json"), task_id="INU-1", status="stale",
              dedupe_key="k1")
    text, linked = approval_closure(store, settings(), old.model_dump(mode="json"))
    assert text.startswith("置き換え") and "新しい依頼が届きます" in text and not linked
    new = old.model_copy(update={"request_id": "new-request-id", "status": ApprovalStatus.PENDING})
    store.put(db.approval_requests, new.request_id, new.model_dump(mode="json"), task_id="INU-1", status="pending",
              dedupe_key="k2")
    old.payload["_superseded_by"] = new.request_id
    store.put(db.approval_requests, old.request_id, old.model_dump(mode="json"), task_id="INU-1", status="stale",
              dedupe_key="k1")
    for req, key, msg in ((old, "approval:old", "111"), (new, "approval:new", "222")):
        store.put(db.notifications_sent, key, DiscordDelivery(
            dedupe_key=key, route=DiscordRoute.APPROVALS, state=DeliveryState.SENT, message_id=msg, has_view=True,
            event=NotificationEvent(kind=EventKind.APPROVAL_REQUIRED, flow_name="research", details=["v"],
                                    approval_request_id=req.request_id)).model_dump(mode="json"))
    text, linked = approval_closure(store, settings(), old.model_dump(mode="json"))
    assert linked and text.endswith("https://discord.com/channels/100/301/222")
    # notify_approval never posts a new buttonless note for a closed request
    rec = RecordingNotifier()
    notify_approval(store, rec, old, "task spec changed")
    assert rec.events == []


class _Message:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class _Channel(discord.TextChannel):
    def __init__(self):  # noqa: D107 - bypass discord.py state; only fetch_message is used
        self.messages = {}

    async def fetch_message(self, message_id):
        return self.messages.setdefault(message_id, _Message())


def _gateway(store):
    client = DiscordGatewayClient(DiscordNotifier(store, settings()))
    channel = _Channel()
    client.is_ready = lambda: True
    client.get_channel = lambda _id: channel
    return client, channel


def test_gateway_edits_replaced_request_and_closes_superseded_control_views(tmp_path):
    store = _store(tmp_path)
    old = ApprovalRequest(kind="onboarding", task_id="INU-1", root_task_id="INU-1", spec_hash="a", title="v1",
                          what_changes=[], diff_from_original=[], budget_summary="-", status="expired",
                          payload={"_dedupe": "k1"})
    store.put(db.approval_requests, old.request_id, old.model_dump(mode="json"), task_id="INU-1", status="expired",
              dedupe_key="k1")
    note = ApprovalRequest(kind="onboarding", task_id="INU-2", root_task_id="INU-2", spec_hash="a", title="note",
                           what_changes=[], diff_from_original=[], budget_summary="-", status="stale",
                           payload={"_dedupe": "k3"})
    store.put(db.approval_requests, note.request_id, note.model_dump(mode="json"), task_id="INU-2", status="stale",
              dedupe_key="k3")

    def sent(key, msg, *, has_view, request_id=None, actions=(), task=None, at=None, route=DiscordRoute.APPROVALS):
        store.put(db.notifications_sent, key, DiscordDelivery(
            dedupe_key=key, route=route, state=DeliveryState.SENT, message_id=msg, has_view=has_view,
            delivered_at=at, event=NotificationEvent(kind=EventKind.APPROVAL_REQUIRED, flow_name="x",
                                                     approval_request_id=request_id, control_task_id=task,
                                                     control_actions=list(actions))).model_dump(mode="json"))

    sent("approval:old", "1", has_view=True, request_id=old.request_id)
    sent("approval:note", "2", has_view=False, request_id=note.request_id)  # sent without buttons
    sent("paused:1", "3", has_view=True, actions=["resume", "cancel"], task="INU-9", at=utcnow() - timedelta(hours=1))
    sent("paused:2", "4", has_view=True, actions=["resume", "cancel"], task="INU-9", at=utcnow())
    run = WorkflowRun(task_id="INU-9", root_task_id="INU-9", stage=WorkflowStage.PAUSED)
    store.put(db.workflow_runs, run.run_id, run.model_dump(mode="json"), task_id=run.task_id, stage=run.stage.value)
    assert [d.message_id for d, _ in control_deliveries_to_close(store)] == ["3"]

    client, channel = _gateway(store)
    asyncio.run(client.reconcile_approvals())
    (edit_old,) = channel.messages[1].edits
    assert "view" in edit_old and all(item.disabled for item in edit_old["view"].children)
    assert "失効" in edit_old["embed"].description
    (edit_note,) = channel.messages[2].edits
    assert "view" not in edit_note and "置き換え" in edit_note["embed"].description
    (edit_ctl,) = channel.messages[3].edits
    assert all(item.disabled for item in edit_ctl["view"].children)
    assert 4 not in channel.messages  # the newest control message stays usable while the run is paused
    # the run is resumed: the remaining Resume / Cancel view closes too
    run.stage = WorkflowStage.RESEARCHING
    store.put(db.workflow_runs, run.run_id, run.model_dump(mode="json"), task_id=run.task_id, stage=run.stage.value)
    assert [d.message_id for d, _ in control_deliveries_to_close(store)] == ["4"]


# ---------------------------------------------------------------- approval templates (finding 5, 10)


@pytest.mark.parametrize("kind", [k.value for k in ApprovalKind])
def test_every_approval_kind_has_a_template_with_steps(kind):
    ask, steps = APPROVAL_TEMPLATES[kind]
    assert ask and len(steps) >= 2


def test_approval_message_has_links_budget_and_steps(tmp_path):
    store = _store(tmp_path)
    req = ApprovalRequest(kind="budget", task_id="INU-1", root_task_id="INU-1", spec_hash="a", title="Budget",
                          what_changes=["API budget exceeded"], diff_from_original=[],
                          budget_summary="used API $3.00 / $2.00, compute 0.10 / 1 CPU-h")
    rec = RecordingNotifier()
    notify_approval(store, rec, req, "requested", "https://linear.app/team/issue/INU-1/slug", None,
                    "https://www.notion.so/page")
    (event,) = rec.events
    assert event.needs_human and event.approval_request_id == req.request_id
    assert "Linear: https://linear.app/team/issue/INU-1/slug" in event.details
    assert "Notion: https://www.notion.so/page" in event.details
    assert any("used API $3.00 / $2.00" in d for d in event.details)
    assert event.steps == APPROVAL_TEMPLATES["budget"][1]
    assert route_event(event) is DiscordRoute.APPROVALS


def test_budget_gate_sends_only_the_approval_message(tmp_path):
    from research_autopilot.autopilot.models import Budget
    from research_autopilot.autopilot.engine import AutopilotSettings
    deps, linear, *_ = build(tmp_path, approval_mode="discord",
                             settings=AutopilotSettings(default_budget=Budget(api_usd=0.0, compute_cpu_hours=0.0)))
    rec = RecordingNotifier()
    deps.notifier = rec
    poll(deps)
    (onboarding,) = deps.approvals.pending()
    _submit(deps, onboarding, "approve")
    for _ in range(3):
        poll(deps)
    assert not any(e.kind is EventKind.BUDGET_EXCEEDED for e in rec.events)


# ---------------------------------------------------------------- blocks / dead-letter (finding 6)


def test_standalone_block_goes_to_approvals_with_steps_links_and_cost(tmp_path):
    deps, *_ = build(tmp_path)
    deps.store.init_schema()
    rec = RecordingNotifier()
    deps.notifier = rec
    run = WorkflowRun(task_id="INU-900", root_task_id="INU-900")
    engine._block(deps, run, "Phase 2 needs_input: purpose is ambiguous")
    (event,) = rec.events
    assert event.needs_human and route_event(event) is DiscordRoute.APPROVALS
    assert event.control_actions == []  # no resume path for a standalone Phase 5 run: the steps say what to do
    assert event.steps == block_steps("Phase 2 needs_input")
    assert "Run Autopilot" in event.steps[-1]
    assert any(d.startswith("Notion: https://") for d in event.details)
    assert any(d.startswith("Linear: https://") for d in event.details)
    assert any(d.startswith("費用:") for d in event.details)


def test_child_block_goes_to_logs_only(tmp_path):
    deps, *_ = build(tmp_path)
    deps.store.init_schema()
    rec = RecordingNotifier()
    deps.notifier = rec
    run = WorkflowRun(task_id="INU-900", root_task_id="INU-1", parent_task_id="INU-1")
    engine._block(deps, run, "Phase 3: tests failed")
    (event,) = rec.events
    assert not event.needs_human and route_event(event) is DiscordRoute.LOGS


def test_research_objective_block_offers_resume_and_cancel(tmp_path):
    deps, *_ = build(tmp_path)
    deps.store.init_schema()
    rec = RecordingNotifier()
    deps.notifier = rec
    deps.store.put(db.research_plans, "plan-1", {"plan_id": "plan-1", "task_id": "INU-900"}, task_id="INU-900",
                    version=1, status="approved")
    run = WorkflowRun(task_id="INU-900", root_task_id="INU-900")
    engine._block(deps, run, "write refused: test-only")
    (event,) = rec.events
    assert event.control_actions == ["resume", "cancel"] and event.control_task_id == "INU-900"
    assert "Resume" in event.steps[-1]


# ---------------------------------------------------------------- decisions that block (finding 7)


def test_revise_decision_sends_an_approvals_message_with_steps(tmp_path):
    deps, linear, *_ = build(tmp_path, approval_mode="discord")
    rec = RecordingNotifier()
    deps.notifier = rec
    poll(deps)
    (req,) = deps.approvals.pending()
    _submit(deps, req, "revise", comment="invariants are too loose")
    poll(deps)
    human = [e for e in rec.events if e.needs_human]
    assert len(human) == 1 and any("invariants are too loose" in d for d in human[0].details)
    assert human[0].steps and "Run Autopilot" in human[0].steps[-1]


def test_external_closed_request_notifies_with_steps(tmp_path):
    from research_autopilot.external import grants
    store = _store(tmp_path)
    rec = RecordingNotifier()
    req = ApprovalRequest(kind="external_license", task_id="external:org/repo", root_task_id="external:org/repo",
                          spec_hash="h", title="license", what_changes=[], diff_from_original=[],
                          budget_summary="-", status="expired")
    grants._on_closed(store, rec)(req, "request expired")
    (event,) = rec.events
    assert event.needs_human and "org/repo" in " ".join(event.steps)


def test_failed_onboarding_has_steps():
    from research_autopilot.external.models import RegistryEntry, Verification
    from research_autopilot.external.service import _failed_steps
    entry = RegistryEntry.model_construct(name="org/repo", verification=Verification.model_construct(
        failure_reasons=["license terms do not permit the intended use"]))
    steps = _failed_steps(entry)
    assert "ライセンス" in steps[1] and "Run Autopilot" in steps[-1]


# ---------------------------------------------------------------- parent DAG (finding 8)


def test_parent_completion_review_is_consistent_and_re_requested_when_closed(tmp_path):
    from research_autopilot.autopilot.dag_runtime import (_request_parent_completion_approval,
                                                          close_parent_dag_approval)
    from test_phase6b_dag import _parent, _seed_parent
    deps, linear, _, parent = _seed_parent(tmp_path, {}, {"INU-901": "repo-a"})
    _request_parent_completion_approval(deps, parent)
    req = _req(deps, _parent(deps, parent.parent_run_id).waiting_for)
    # the parent's own spec hash is used for the staleness check (it was stale on first check before)
    assert engine._check_approval(deps, req)[1] == "pending"
    req.status = ApprovalStatus.EXPIRED
    deps.approvals._save(req)
    close_parent_dag_approval(deps, req, "request expired")
    again = _parent(deps, parent.parent_run_id).waiting_for
    assert again and again != req.request_id and _req(deps, again).status is ApprovalStatus.PENDING


def test_parent_failure_and_invalid_dag_send_steps(tmp_path):
    from research_autopilot.autopilot.dag import DAGProblem
    from research_autopilot.autopilot.dag_runtime import _dag_problem_steps, advance_parents, schedule_ready
    from research_autopilot.autopilot.execution import ExecutionStatus
    from test_phase6b_dag import _seed_parent, _set_child
    steps = _dag_problem_steps([DAGProblem(code="missing_dependency", task_id="INU-902", message="INU-999 unknown")])
    assert steps[0].startswith("INU-902 の Notion ページを直す") and "Run Autopilot" in steps[-1]
    deps, linear, _, parent = _seed_parent(tmp_path, {}, {"INU-901": "repo-a"})
    rec = RecordingNotifier()
    deps.notifier = rec
    schedule_ready(deps, parent)
    _set_child(deps, parent.parent_run_id, "INU-901", ExecutionStatus.FAILED, "experiment crashed")
    advance_parents(deps)
    human = [e for e in rec.events if e.needs_human]
    assert len(human) == 1 and "INU-901" in human[0].steps[0]
    assert any("experiment crashed" in d for d in human[0].details)


def test_approval_event_marks_replacement_version():
    req = ApprovalRequest(kind="research_scope", task_id="INU-1", root_task_id="INU-1", spec_hash="a", title="v2",
                          what_changes=[], diff_from_original=[], budget_summary="-",
                          payload={"_supersedes": "abcdef0123", "_version": 2, "plan_url": "https://www.notion.so/p"})
    event = approval_event(req, links=["Notion: https://www.notion.so/p"])
    assert any("abcdef01" in d and "v2" in d for d in event.details)


def test_confirmed_cancel_records_a_research_control_and_research_buttons_render(tmp_path):
    from research_autopilot.notifications.discord import ControlView
    store = _store(tmp_path)
    service = DiscordControlInput(store, settings())
    args = dict(task_id="INU-900", user_id=200, guild_id=100)
    assert service.submit(interaction_id="c0", action="cancel", channel_id=302, **args) is InteractionResult.WRONG_CHANNEL
    assert service.submit(interaction_id="c1", action="cancel", channel_id=301, **args) is InteractionResult.ACCEPTED
    (control,) = store.find(db.research_controls)
    assert control["action"] == "cancel" and control["actor"] == "discord:200"
    stopped = NotificationEvent(kind=EventKind.UNRECOVERABLE_ERROR, flow_name="research", control_task_id="INU-900",
                                control_actions=["resume", "cancel"])
    assert route_event(stopped) is DiscordRoute.APPROVALS

    async def labels():
        return [b.label for b in ControlView(service, "INU-900", ["resume", "cancel"]).children]

    assert asyncio.run(labels()) == ["Resume", "Cancel"]


def test_pending_requests_of_a_cancelled_task_are_withdrawn(tmp_path):
    deps, linear, *_ = build(tmp_path, approval_mode="discord")
    poll(deps)
    (req,) = deps.approvals.pending()
    run = _run(deps)
    run.stage = WorkflowStage.CANCELLED
    deps.store.put(db.workflow_runs, run.run_id, run.model_dump(mode="json"), task_id=run.task_id,
                   stage=run.stage.value)
    _submit(deps, req, "approve")  # a late Approve
    poll(deps)
    closed = _req(deps, req.request_id)
    assert closed.status is ApprovalStatus.STALE and closed.payload["_final"]
    assert deps.store.get(db.approval_decisions, req.request_id) is None
    text, linked = approval_closure(deps.store, settings(), closed.model_dump(mode="json"))
    assert text.startswith("無効") and linked
