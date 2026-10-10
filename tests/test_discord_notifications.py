from datetime import timedelta

import pytest

from phase5_fakes import build
from research_autopilot.autopilot.engine import poll
from research_autopilot.autopilot.models import ApprovalRequest, WorkflowRun, WorkflowStage
from research_autopilot.models import utcnow
from research_autopilot.notifications.discord import (
    DeliveryState,
    DiscordApprovalInput,
    DiscordDelivery,
    DiscordNotifier,
    DiscordRoute,
    DiscordSettings,
    InteractionResult,
    embed_payload,
    load_discord_settings,
    route_event,
)
from research_autopilot.notifications.events import EventKind, NotificationEvent
from research_autopilot.storage import db
from research_autopilot.storage.db import Store


def settings() -> DiscordSettings:
    return DiscordSettings(token="secret-token", guild_id=100, authorized_user_ids={200},
                           approval_channel_id=301, results_channel_id=302,
                           errors_channel_id=303, logs_channel_id=304)


def request(store: Store, *, status="pending", expired=False) -> ApprovalRequest:
    req = ApprovalRequest(kind="onboarding", task_id="INU-1", root_task_id="INU-1", spec_hash="spec-v1",
                          title="approve", what_changes=["run"], diff_from_original=[], budget_summary="1 USD",
                          expires_at=utcnow() - timedelta(seconds=1) if expired else utcnow() + timedelta(days=1),
                          status=status)
    key = f"request:{req.request_id}"
    req.payload["_dedupe"] = key
    store.put(db.approval_requests, req.request_id, req.model_dump(mode="json"), task_id=req.task_id,
              status=req.status.value, dedupe_key=key)
    return req


@pytest.mark.parametrize("kind,route", [
    (EventKind.APPROVAL_REQUIRED, DiscordRoute.APPROVALS),
    (EventKind.EXPERIMENT_COMPLETED, DiscordRoute.RESULTS),
    (EventKind.ANALYSIS_COMPLETED, DiscordRoute.RESULTS),
    (EventKind.TASK_COMPLETED, DiscordRoute.RESULTS),
    (EventKind.UNRECOVERABLE_ERROR, DiscordRoute.ERRORS),
    (EventKind.BUDGET_EXCEEDED, DiscordRoute.ERRORS),
    (EventKind.WORKER_FAILURE, DiscordRoute.ERRORS),
    (EventKind.TASK_STARTED, DiscordRoute.LOGS),
    (EventKind.PHASE_TRANSITION, DiscordRoute.LOGS),
    (EventKind.AUTOMATIC_RETRY, DiscordRoute.LOGS),
    (EventKind.REPAIR_SUCCEEDED, DiscordRoute.LOGS),
    (EventKind.PROPOSAL_GENERATED, DiscordRoute.LOGS),
])
def test_typed_event_routing(kind, route):
    assert route_event(NotificationEvent(kind=kind, flow_name="x")) is route


def test_legacy_completion_routing_and_embed_shape():
    experiment = NotificationEvent(kind=EventKind.COMPLETED, flow_name="run-experiment", flow_run_name="INU-1",
                                   state_name="Completed", details=["metric: 0.1"])
    planning = experiment.model_copy(update={"flow_name": "plan-task"})
    assert route_event(experiment) is DiscordRoute.LOGS
    assert route_event(planning) is DiscordRoute.LOGS
    payload = embed_payload(experiment)
    assert payload["title"] and payload["description"] == "metric: 0.1"
    assert {f["name"] for f in payload["fields"]} == {"Task / Run", "Status"}


def test_discord_config_is_strict_and_token_is_secret():
    env = {
        "RA_DISCORD_BOT_TOKEN": "token-value", "RA_DISCORD_GUILD_ID": "100",
        "RA_DISCORD_AUTHORIZED_USER_IDS": "200,201", "RA_DISCORD_APPROVAL_CHANNEL_ID": "301",
        "RA_DISCORD_RESULTS_CHANNEL_ID": "302", "RA_DISCORD_ERRORS_CHANNEL_ID": "303",
        "RA_DISCORD_LOGS_CHANNEL_ID": "304",
    }
    got = load_discord_settings(env)
    assert got.authorized_user_ids == {200, 201} and "token-value" not in repr(got)
    with pytest.raises(ValueError):
        load_discord_settings({**env, "RA_DISCORD_RESULTS_CHANNEL_ID": "not-an-id"})
    with pytest.raises(ValueError):
        load_discord_settings({**env, "RA_DISCORD_AUTHORIZED_USER_IDS": "200,not-an-id"})
    with pytest.raises(ValueError):
        load_discord_settings({**env, "RA_DISCORD_RESULTS_CHANNEL_ID": "301"})


def test_notification_outbox_deduplicates_and_survives_restart(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'db.sqlite'}")
    event = NotificationEvent(kind=EventKind.TASK_COMPLETED, flow_name="autopilot", flow_run_id="r1",
                              dedupe_key="done:r1")
    first = DiscordNotifier(store, settings())
    first.send(event)
    first.send(event)
    second = DiscordNotifier(Store(f"sqlite:///{tmp_path / 'db.sqlite'}"), settings())
    pending = second.pending()
    assert len(pending) == 1 and pending[0].dedupe_key == "done:r1"
    failed = pending[0].model_copy(update={"state": DeliveryState.FAILED, "attempts": 2,
                                           "next_attempt_at": utcnow() + timedelta(minutes=1),
                                           "last_error": "ServerDisconnectedError"})
    second.save(failed)
    assert second.pending() == []
    assert len(second.pending(now=utcnow() + timedelta(minutes=2))) == 1


@pytest.mark.parametrize("case,result", [
    ({"user_id": 999}, InteractionResult.UNAUTHORIZED),
    ({"user_id": 200, "user_is_bot": True}, InteractionResult.UNAUTHORIZED),
    ({"user_id": 200, "guild_id": 999}, InteractionResult.WRONG_GUILD),
    ({"user_id": 200, "channel_id": 999}, InteractionResult.WRONG_CHANNEL),
    ({"user_id": 200, "request_id": "missing"}, InteractionResult.UNKNOWN_REQUEST),
])
def test_interaction_authorization_and_scope(tmp_path, case, result):
    store = Store(f"sqlite:///{tmp_path / 'db.sqlite'}")
    store.init_schema()
    req = request(store)
    args = dict(interaction_id="i1", request_id=req.request_id, decision="approve", user_id=200,
                guild_id=100, channel_id=301)
    args.update(case)
    assert DiscordApprovalInput(store, settings()).submit(**args) is result


def test_duplicate_expired_and_processed_interactions(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'db.sqlite'}")
    store.init_schema()
    service = DiscordApprovalInput(store, settings())
    req = request(store)
    args = dict(interaction_id="i1", request_id=req.request_id, decision="approve", user_id=200,
                guild_id=100, channel_id=301)
    assert service.submit(**args) is InteractionResult.ACCEPTED
    assert service.submit(**args) is InteractionResult.DUPLICATE
    assert service.submit(**{**args, "interaction_id": "i1-other", "decision": "reject"}) is InteractionResult.DUPLICATE
    expired = request(store, expired=True)
    assert service.submit(**{**args, "interaction_id": "i2", "request_id": expired.request_id}) is InteractionResult.EXPIRED
    processed = request(store, status="approved")
    assert service.submit(**{**args, "interaction_id": "i3", "request_id": processed.request_id}) is InteractionResult.ALREADY_PROCESSED


def _submit(deps, req, decision, interaction_id="interaction-1", comment=""):
    result = DiscordApprovalInput(deps.store, settings()).submit(
        interaction_id=interaction_id, request_id=req.request_id, decision=decision,
        user_id=200, guild_id=100, channel_id=301, comment=comment)
    assert result is InteractionResult.ACCEPTED


def _stage(deps):
    return WorkflowRun.model_validate(deps.store.find(db.workflow_runs, task_id="INU-900")[0]).stage


def test_discord_approval_resumes_prefect_work_and_updates_linear(tmp_path):
    deps, linear, *_ = build(tmp_path, approval_mode="discord")
    poll(deps)
    (req,) = deps.approvals.pending()
    _submit(deps, req, "approve")
    poll(deps)
    decision = deps.store.get(db.approval_decisions, req.request_id)
    assert decision["channel"] == "discord" and decision["actor"] == "discord:200"
    assert _stage(deps) is WorkflowStage.DONE
    assert linear.get_issue("INU-900").state_name == "Done"


@pytest.mark.parametrize("decision,stage,state", [
    ("reject", WorkflowStage.CANCELLED, "Canceled"),
    ("revise", WorkflowStage.BLOCKED, "Revision Requested"),
])
def test_discord_negative_decisions_are_reflected_in_linear(tmp_path, decision, stage, state):
    deps, linear, *_ = build(tmp_path / decision, approval_mode="discord")
    poll(deps)
    (req,) = deps.approvals.pending()
    _submit(deps, req, decision, comment="reason")
    poll(deps)
    assert _stage(deps) is stage
    assert linear.get_issue("INU-900").state_name == state


def test_spec_edit_makes_discord_interaction_stale(tmp_path):
    deps, linear, notion, *_ = build(tmp_path, approval_mode="discord")
    poll(deps)
    (req,) = deps.approvals.pending()
    page_id = next(iter(notion.pages))
    page = notion.pages[page_id]
    notion.pages[page_id] = page.model_copy(update={
        "sections": {**page.sections, "acceptance_criteria": "- changed after approval request"}})
    _submit(deps, req, "approve")
    poll(deps)
    saved = ApprovalRequest.model_validate(deps.store.get(db.approval_requests, req.request_id))
    assert saved.status.value == "stale"
    assert deps.store.get(db.approval_decisions, req.request_id) is None


def test_secret_env_filter_includes_discord_token():
    from research_autopilot.implementation.backends.base import sanitized_env

    cleaned = sanitized_env({"PATH": "/bin", "RA_DISCORD_BOT_TOKEN": "do-not-leak"})
    assert "RA_DISCORD_BOT_TOKEN" not in cleaned
