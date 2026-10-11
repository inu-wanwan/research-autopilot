"""Discord notification outbox and Gateway approval UI.

The worker only enqueues typed events in PostgreSQL.  A separately running
Gateway client delivers them and records Discord message ids.  Discord is a
human interface; approval policy and final decisions remain in the approval
controller and database.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

import discord
from discord.ext import tasks
from hashlib import sha256

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from research_autopilot.autopilot.models import ApprovalRequest, ApprovalStatus, DiscordSubmission
from research_autopilot.models import utcnow
from research_autopilot.notifications.events import EventKind, NotificationEvent, numbered
from research_autopilot.storage import db
from research_autopilot.storage.db import Store


class DiscordRoute(StrEnum):
    APPROVALS = "approvals"
    RESULTS = "results"
    ERRORS = "errors"
    LOGS = "logs"


class DiscordSettings(BaseModel):
    token: SecretStr
    guild_id: int
    authorized_user_ids: set[int] = Field(min_length=1)
    approval_channel_id: int
    results_channel_id: int
    errors_channel_id: int
    logs_channel_id: int

    @field_validator("guild_id", "approval_channel_id", "results_channel_id", "errors_channel_id", "logs_channel_id")
    @classmethod
    def _positive_snowflake(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Discord IDs must be positive integers")
        return value

    def channel_id(self, route: DiscordRoute) -> int:
        return {
            DiscordRoute.APPROVALS: self.approval_channel_id,
            DiscordRoute.RESULTS: self.results_channel_id,
            DiscordRoute.ERRORS: self.errors_channel_id,
            DiscordRoute.LOGS: self.logs_channel_id,
        }[route]

    @model_validator(mode="after")
    def _distinct_channels(self):
        ids = [self.approval_channel_id, self.results_channel_id, self.errors_channel_id, self.logs_channel_id]
        if len(set(ids)) != len(ids):
            raise ValueError("Discord route channel IDs must be distinct")
        return self


def load_discord_settings(env: dict[str, str] | None = None) -> DiscordSettings:
    env = dict(os.environ) if env is None else env

    def required(name: str) -> str:
        value = env.get(name, "").strip()
        if not value:
            raise ValueError(f"{name} is required when RA_NOTIFICATION_BACKEND=discord")
        return value

    def snowflake(name: str) -> int:
        raw = required(name)
        if not raw.isdecimal():
            raise ValueError(f"{name} must be a decimal Discord ID")
        return int(raw)

    raw_users = [v.strip() for v in required("RA_DISCORD_AUTHORIZED_USER_IDS").split(",") if v.strip()]
    if any(not value.isdecimal() for value in raw_users):
        raise ValueError("RA_DISCORD_AUTHORIZED_USER_IDS must contain only decimal Discord user IDs")
    users = {int(value) for value in raw_users}
    if not users:
        raise ValueError("RA_DISCORD_AUTHORIZED_USER_IDS must contain at least one decimal Discord user ID")
    return DiscordSettings(
        token=required("RA_DISCORD_BOT_TOKEN"),
        guild_id=snowflake("RA_DISCORD_GUILD_ID"),
        authorized_user_ids=users,
        approval_channel_id=snowflake("RA_DISCORD_APPROVAL_CHANNEL_ID"),
        results_channel_id=snowflake("RA_DISCORD_RESULTS_CHANNEL_ID"),
        errors_channel_id=snowflake("RA_DISCORD_ERRORS_CHANNEL_ID"),
        logs_channel_id=snowflake("RA_DISCORD_LOGS_CHANNEL_ID"),
    )


_RESULT_KINDS = {
    EventKind.EXPERIMENT_COMPLETED, EventKind.ANALYSIS_COMPLETED, EventKind.TASK_COMPLETED,
}
_ERROR_KINDS = {
    EventKind.UNRECOVERABLE_ERROR, EventKind.BUDGET_EXCEEDED, EventKind.WORKER_FAILURE, EventKind.FAILED,
}
_LOG_KINDS = {
    EventKind.TASK_STARTED, EventKind.PHASE_TRANSITION, EventKind.AUTOMATIC_RETRY,
    EventKind.REPAIR_SUCCEEDED, EventKind.PROPOSAL_GENERATED,
}


def route_event(event: NotificationEvent) -> DiscordRoute:
    """UX policy (docs/phase_specs/ux_policy.md section 2): everything a human must act on goes to approvals;
    results may carry only Stop; errors and logs never carry buttons (``DiscordNotifier.send`` strips them)."""
    if event.kind is EventKind.APPROVAL_REQUIRED or event.needs_human:
        return DiscordRoute.APPROVALS
    if set(event.control_actions) - {"stop"}:  # Resume / Cancel wait for a human decision
        return DiscordRoute.APPROVALS
    if event.kind in _RESULT_KINDS:
        return DiscordRoute.RESULTS
    if event.kind in _ERROR_KINDS:
        return DiscordRoute.ERRORS
    if event.kind in _LOG_KINDS:
        return DiscordRoute.LOGS
    # Legacy completion hooks are operational logs. Domain result events are explicit.
    if event.kind is EventKind.COMPLETED:
        return DiscordRoute.LOGS
    raise ValueError(f"no Discord route for {event.kind}")


_COLOURS = {
    DiscordRoute.APPROVALS: 0xF1C40F,
    DiscordRoute.RESULTS: 0x2ECC71,
    DiscordRoute.ERRORS: 0xE74C3C,
    DiscordRoute.LOGS: 0x3498DB,
}


_LINK_PREFIXES = ("Notion:", "Notion plan:", "Notion Notes:", "Notes:", "Linear:", "Prefect:", "Approval form:")


def embed_payload(event: NotificationEvent) -> dict:
    """Library-independent embed representation, kept small enough for Discord.

    ``steps`` become a numbered "次にやること" field; Notion / Linear / Notes links in ``details`` get their own
    field so the place to act is visible at a glance."""
    route = route_event(event)
    links = [d for d in event.details if d.startswith(_LINK_PREFIXES) and "http" in d]
    body = [d for d in event.details if d not in links]
    description = "\n".join(body)[:4000] or "詳細はリンク先を確認してください。"
    fields = []
    if event.steps:
        fields.append({"name": "次にやること", "value": "\n".join(numbered(event.steps))[:1024], "inline": False})
    if links:
        fields.append({"name": "リンク", "value": "\n".join(links)[:1024], "inline": False})
    if event.flow_run_name:
        fields.append({"name": "Task / Run", "value": event.flow_run_name[:1024], "inline": True})
    if event.state_name:
        fields.append({"name": "Status", "value": event.state_name[:1024], "inline": True})
    if event.error_type:
        fields.append({"name": "Error", "value": event.error_type[:1024], "inline": True})
    return {
        "title": event.title()[:256],
        "description": description,
        "color": _COLOURS[route],
        "url": event.ui_url,
        "fields": fields[:25],
    }


def _embed(event: NotificationEvent, marker: str | None = None) -> discord.Embed:
    payload = embed_payload(event)
    embed = discord.Embed(title=payload["title"], description=payload["description"],
                          colour=payload["color"], url=payload["url"])
    for field in payload["fields"]:
        embed.add_field(**field)
    if marker:
        embed.set_footer(text=marker)
    return embed


def _delivery_marker(key: str) -> str:
    return f"ra:{sha256(key.encode()).hexdigest()[:24]}"


class DeliveryState(StrEnum):
    PENDING = "pending"
    FAILED = "failed"
    SENT = "sent"


class DiscordDelivery(BaseModel):
    dedupe_key: str
    event: NotificationEvent
    route: DiscordRoute
    state: DeliveryState = DeliveryState.PENDING
    attempts: int = 0
    next_attempt_at: datetime = Field(default_factory=utcnow)
    message_id: str | None = None
    last_error: str | None = None
    controls_closed: bool = False
    delivered_at: datetime | None = None
    has_view: bool | None = None  # buttons attached on delivery (None: rows written before this was tracked)
    superseded_linked: bool = False  # the closed approval message already points to its replacement


def _channel_controls(event: NotificationEvent, route: DiscordRoute) -> NotificationEvent:
    """Errors / logs never carry buttons; results carry at most Stop."""
    if route in (DiscordRoute.ERRORS, DiscordRoute.LOGS) and (event.control_actions or event.control_task_id):
        return event.model_copy(update={"control_actions": [], "control_task_id": None})
    if route is DiscordRoute.RESULTS and set(event.control_actions) - {"stop"}:
        actions = [a for a in event.control_actions if a == "stop"]
        return event.model_copy(update={"control_actions": actions,
                                        "control_task_id": event.control_task_id if actions else None})
    return event


class DiscordNotifier:
    """Synchronous notifier used by flows; ``send`` only persists to the outbox."""

    persistent = True

    def __init__(self, store: Store, settings: DiscordSettings) -> None:
        self.store = store
        self.settings = settings
        self.store.init_schema()

    def send(self, event: NotificationEvent) -> None:
        key = event.stable_dedupe_key()
        route = route_event(event)
        delivery = DiscordDelivery(dedupe_key=key, event=_channel_controls(event, route), route=route)
        self.store.insert_new(db.notifications_sent, key, delivery.model_dump(mode="json"))

    def pending(self, now: datetime | None = None) -> list[DiscordDelivery]:
        now = now or utcnow()
        deliveries = []
        for record in self.store.find(db.notifications_sent):
            if "event" not in record:  # rows written by pre-Discord versions
                continue
            item = DiscordDelivery.model_validate(record)
            if item.state in {DeliveryState.PENDING, DeliveryState.FAILED} and item.next_attempt_at <= now:
                deliveries.append(item)
        return deliveries

    def save(self, delivery: DiscordDelivery) -> None:
        self.store.put(db.notifications_sent, delivery.dedupe_key, delivery.model_dump(mode="json"))


class InteractionResult(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    UNAUTHORIZED = "unauthorized"
    WRONG_GUILD = "wrong_guild"
    WRONG_CHANNEL = "wrong_channel"
    UNKNOWN_REQUEST = "unknown_request"
    EXPIRED = "expired"
    ALREADY_PROCESSED = "already_processed"


class DiscordApprovalInput:
    """Authenticate and persist an interaction; it does not decide approval policy."""

    def __init__(self, store: Store, settings: DiscordSettings) -> None:
        self.store = store
        self.settings = settings

    def submit(self, *, interaction_id: str, request_id: str, decision: Literal["approve", "reject", "revise"],
               user_id: int, guild_id: int | None, channel_id: int, user_is_bot: bool = False,
               comment: str = "") -> InteractionResult:
        if user_is_bot or user_id not in self.settings.authorized_user_ids:
            return InteractionResult.UNAUTHORIZED
        if guild_id != self.settings.guild_id:
            return InteractionResult.WRONG_GUILD
        if channel_id != self.settings.approval_channel_id:
            return InteractionResult.WRONG_CHANNEL
        if self.store.get(db.discord_interactions, interaction_id):
            return InteractionResult.DUPLICATE
        if self.store.find(db.discord_interactions, request_id=request_id):
            return InteractionResult.DUPLICATE
        raw = self.store.get(db.approval_requests, request_id)
        if raw is None:
            return InteractionResult.UNKNOWN_REQUEST
        req = ApprovalRequest.model_validate(raw)
        if req.status is not ApprovalStatus.PENDING:
            return InteractionResult.ALREADY_PROCESSED
        if req.expires_at < utcnow():
            return InteractionResult.EXPIRED
        sub = DiscordSubmission(interaction_id=interaction_id, request_id=request_id, decision=decision,
                                spec_hash=req.spec_hash, user_id=str(user_id), guild_id=str(guild_id),
                                channel_id=str(channel_id), comment=comment.strip()[:1000])
        if not self.store.insert_new(db.discord_interactions, interaction_id, sub.model_dump(mode="json"),
                                     request_id=request_id):
            return InteractionResult.DUPLICATE
        return InteractionResult.ACCEPTED


_RESULT_TEXT = {
    InteractionResult.ACCEPTED: "承認入力を記録しました。ポーラーが仕様ハッシュと期限を再検証します。",
    InteractionResult.DUPLICATE: "この操作はすでに受理済みです。",
    InteractionResult.UNAUTHORIZED: "この操作を行う権限がありません。",
    InteractionResult.WRONG_GUILD: "許可されたサーバーではありません。",
    InteractionResult.WRONG_CHANNEL: "承認チャンネル以外では操作できません。",
    InteractionResult.UNKNOWN_REQUEST: "承認リクエストが見つかりません。",
    InteractionResult.EXPIRED: "この承認リクエストは期限切れです。",
    InteractionResult.ALREADY_PROCESSED: "この承認リクエストは処理済みです。",
}


class RevisionModal(discord.ui.Modal, title="修正依頼"):
    comment = discord.ui.TextInput(label="修正内容", style=discord.TextStyle.paragraph, max_length=1000, required=True)

    def __init__(self, service: DiscordApprovalInput, request_id: str) -> None:
        super().__init__(custom_id=f"ra:revision:{request_id}")
        self.service = service
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        result = self.service.submit(interaction_id=str(interaction.id), request_id=self.request_id, decision="revise",
                                     user_id=interaction.user.id, guild_id=interaction.guild_id,
                                     channel_id=interaction.channel_id, user_is_bot=interaction.user.bot,
                                     comment=str(self.comment))
        await interaction.response.send_message(_RESULT_TEXT[result], ephemeral=True)


class ApprovalButton(discord.ui.Button):
    def __init__(self, service: DiscordApprovalInput, request_id: str,
                 action: Literal["approve", "reject", "revise"], *, disabled: bool = False) -> None:
        labels = {"approve": "Approve", "reject": "Reject", "revise": "Request Revision"}
        styles = {"approve": discord.ButtonStyle.success, "reject": discord.ButtonStyle.danger,
                  "revise": discord.ButtonStyle.secondary}
        super().__init__(label=labels[action], style=styles[action], custom_id=f"ra:{action}:{request_id}",
                         disabled=disabled)
        self.service = service
        self.request_id = request_id
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.action == "revise":
            # Authorization is checked again when the modal is submitted.
            if (interaction.user.bot or interaction.user.id not in self.service.settings.authorized_user_ids
                    or interaction.guild_id != self.service.settings.guild_id
                    or interaction.channel_id != self.service.settings.approval_channel_id):
                await interaction.response.send_message("この操作を行う権限またはチャンネルが正しくありません。", ephemeral=True)
                return
            await interaction.response.send_modal(RevisionModal(self.service, self.request_id))
            return
        result = self.service.submit(interaction_id=str(interaction.id), request_id=self.request_id,
                                     decision=self.action, user_id=interaction.user.id,
                                     guild_id=interaction.guild_id, channel_id=interaction.channel_id,
                                     user_is_bot=interaction.user.bot)
        await interaction.response.send_message(_RESULT_TEXT[result], ephemeral=True)


class ApprovalView(discord.ui.View):
    def __init__(self, service: DiscordApprovalInput, request_id: str, *, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        for action in ("approve", "reject", "revise"):
            self.add_item(ApprovalButton(service, request_id, action, disabled=disabled))


ControlAction = Literal["stop", "resume", "cancel"]


class DiscordControlInput:
    """Phase 10 Stop / Resume / Cancel of a research run: authorized user in the configured guild; controls are
    accepted in the approvals channel only (Stop also in results, where running research offers it)."""

    def __init__(self, store: Store, settings: DiscordSettings) -> None:
        self.store = store
        self.settings = settings

    def allowed(self, *, action: str, user_id: int, guild_id: int | None, channel_id: int,
                user_is_bot: bool = False) -> InteractionResult | None:
        """The authorization / channel check alone (also run before the Cancel confirmation is shown)."""
        if user_is_bot or user_id not in self.settings.authorized_user_ids:
            return InteractionResult.UNAUTHORIZED
        if guild_id != self.settings.guild_id:
            return InteractionResult.WRONG_GUILD
        channels = {self.settings.approval_channel_id}
        if action == "stop":
            channels.add(self.settings.results_channel_id)
        if channel_id not in channels:
            return InteractionResult.WRONG_CHANNEL
        return None

    def submit(self, *, interaction_id: str, task_id: str, action: ControlAction, user_id: int,
               guild_id: int | None, channel_id: int, user_is_bot: bool = False) -> InteractionResult:
        if action not in ("stop", "resume", "cancel"):
            return InteractionResult.UNKNOWN_REQUEST
        refused = self.allowed(action=action, user_id=user_id, guild_id=guild_id, channel_id=channel_id,
                               user_is_bot=user_is_bot)
        if refused is not None:
            return refused
        if self.store.get(db.research_controls, interaction_id):
            return InteractionResult.DUPLICATE
        from research_autopilot.research.models import ResearchControl
        c = ResearchControl(control_id=interaction_id, task_id=task_id, action=action, actor=f"discord:{user_id}",
                            channel="discord")
        if not self.store.insert_new(db.research_controls, c.control_id, c.model_dump(mode="json"), task_id=task_id):
            return InteractionResult.DUPLICATE
        return InteractionResult.ACCEPTED


_CONTROL_TEXT = {
    InteractionResult.ACCEPTED: "受け付けました。次のポーリングで反映します（停止中も実行中のジョブは完了まで続きます）。",
    InteractionResult.DUPLICATE: "この操作はすでに受理済みです。",
    InteractionResult.UNAUTHORIZED: "この操作を行う権限がありません。",
    InteractionResult.WRONG_GUILD: "許可されたサーバーではありません。",
    InteractionResult.WRONG_CHANNEL: "このチャンネルでは操作できません。",
}


_CONTROL_ORDER = ("resume", "stop", "cancel")  # Resume left, Stop, Cancel right
_CONTROL_LABELS = {"stop": "Stop", "resume": "Resume", "cancel": "Cancel"}
_CONTROL_STYLES = {"stop": discord.ButtonStyle.danger, "resume": discord.ButtonStyle.primary,
                   "cancel": discord.ButtonStyle.secondary}
CANCEL_CONFIRM_TEXT = ("この研究タスクと未完了の子タスクの実行を打ち切り、Linear をまとめて Canceled にします。"
                       "取り消せません。打ち切る場合は「打ち切りを確定」を押してください。")


class ControlButton(discord.ui.Button):
    def __init__(self, service: DiscordControlInput, task_id: str, action: ControlAction, *,
                 disabled: bool = False) -> None:
        super().__init__(label=_CONTROL_LABELS[action], style=_CONTROL_STYLES[action],
                         custom_id=f"ra:{action}:{task_id}", disabled=disabled)
        self.service, self.task_id, self.action = service, task_id, action

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.action == "cancel":
            # Cancel is irreversible: an ephemeral confirmation first; authorization is checked again on confirm.
            refused = self.service.allowed(action="cancel", user_id=interaction.user.id,
                                           guild_id=interaction.guild_id, channel_id=interaction.channel_id,
                                           user_is_bot=interaction.user.bot)
            if refused is not None:
                await interaction.response.send_message(_CONTROL_TEXT.get(refused, "受け付けられませんでした。"),
                                                        ephemeral=True)
                return
            await interaction.response.send_message(CANCEL_CONFIRM_TEXT, ephemeral=True,
                                                    view=CancelConfirmView(self.service, self.task_id))
            return
        result = self.service.submit(interaction_id=str(interaction.id), task_id=self.task_id, action=self.action,
                                     user_id=interaction.user.id, guild_id=interaction.guild_id,
                                     channel_id=interaction.channel_id, user_is_bot=interaction.user.bot)
        await interaction.response.send_message(_CONTROL_TEXT.get(result, "受け付けられませんでした。"), ephemeral=True)


class CancelConfirmButton(discord.ui.Button):
    def __init__(self, service: DiscordControlInput, task_id: str) -> None:
        super().__init__(label="打ち切りを確定", style=discord.ButtonStyle.danger)
        self.service, self.task_id = service, task_id

    async def callback(self, interaction: discord.Interaction) -> None:
        result = self.service.submit(interaction_id=str(interaction.id), task_id=self.task_id, action="cancel",
                                     user_id=interaction.user.id, guild_id=interaction.guild_id,
                                     channel_id=interaction.channel_id, user_is_bot=interaction.user.bot)
        await interaction.response.send_message(_CONTROL_TEXT.get(result, "受け付けられませんでした。"), ephemeral=True)


class CancelConfirmView(discord.ui.View):
    """Ephemeral second step of Cancel (not persistent: an unanswered confirmation simply lapses)."""

    def __init__(self, service: DiscordControlInput, task_id: str) -> None:
        super().__init__(timeout=300)
        self.add_item(CancelConfirmButton(service, task_id))


class ControlView(discord.ui.View):
    def __init__(self, service: DiscordControlInput, task_id: str, actions: list[str] | None = None, *,
                 disabled: bool = False) -> None:
        super().__init__(timeout=None)
        for action in sorted([a for a in (actions or ("resume", "stop")) if a in _CONTROL_ORDER],
                             key=_CONTROL_ORDER.index):
            self.add_item(ControlButton(service, task_id, action, disabled=disabled))


def jump_url(settings: DiscordSettings, route: DiscordRoute, message_id: str) -> str:
    return f"https://discord.com/channels/{settings.guild_id}/{settings.channel_id(route)}/{message_id}"


def _sent_deliveries(store: Store) -> list[DiscordDelivery]:
    out = []
    for record in store.find(db.notifications_sent):
        if "event" not in record:
            continue
        delivery = DiscordDelivery.model_validate(record)
        if delivery.state is DeliveryState.SENT and delivery.message_id:
            out.append(delivery)
    return out


_CLOSED_LABEL = {ApprovalStatus.STALE.value: "置き換え", ApprovalStatus.EXPIRED.value: "失効"}
_CLOSED_REASON = {ApprovalStatus.STALE.value: "依頼の後に仕様（または対象）が変わりました",
                  ApprovalStatus.EXPIRED.value: "承認の期限が切れました"}


def approval_closure(store: Store, settings: DiscordSettings, raw: dict) -> tuple[str, bool] | None:
    """Line appended to a replaced / expired approval message, and whether it already links the replacement.

    The approval controller records which request replaced it (``_superseded_by``) in the request payload."""
    status = raw.get("status")
    if status not in _CLOSED_LABEL:
        return None
    payload = raw.get("payload") or {}
    if payload.get("_final"):  # withdrawn for good (e.g. research cancelled): no replacement follows
        return f"無効: {payload.get('withdrawn') or '依頼は取り下げられました'}。この依頼への操作は反映されません。", True
    reason =_CLOSED_REASON[status] + (f"（{payload['withdrawn']}）" if payload.get("withdrawn") else "")
    new_id = payload.get("_superseded_by")
    if not new_id:
        return f"{_CLOSED_LABEL[status]}: {reason}。新しい依頼が届きます（このメッセージのボタンは無効です）。", False
    target = next((d for d in _sent_deliveries(store) if d.event.approval_request_id == new_id), None)
    ref = jump_url(settings, target.route, target.message_id) if target else f"依頼 {new_id[:8]}"
    return f"{_CLOSED_LABEL[status]}: {reason}。新しい依頼へ: {ref}", target is not None


_TERMINAL_STAGES = {"done", "cancelled", "dead_letter"}
_HALTED_STAGES = {"paused", "blocked", "awaiting_authorization", "awaiting_approval"}


def _latest_stage(store: Store, task_id: str) -> str | None:
    runs = store.find(db.workflow_runs, task_id=task_id)
    if not runs:
        return None
    return max(runs, key=lambda r: str(r.get("created_at", ""))).get("stage")


def control_deliveries_to_close(store: Store) -> list[tuple[DiscordDelivery, str]]:
    """Stop / Resume / Cancel messages whose buttons no longer apply: a newer control message for the same task
    was delivered, the run finished, or the run moved on (resumed after a Resume offer, halted after a Stop offer)."""
    by_task: dict[str, list[DiscordDelivery]] = {}
    for d in _sent_deliveries(store):
        if (d.event.control_task_id and d.event.control_actions and not d.controls_closed
                and not d.event.approval_request_id and d.has_view is not False
                and d.route in (DiscordRoute.APPROVALS, DiscordRoute.RESULTS)):
            by_task.setdefault(d.event.control_task_id, []).append(d)
    out = []
    for task_id, items in by_task.items():
        items.sort(key=lambda d: d.delivered_at or d.next_attempt_at)
        stage = _latest_stage(store, task_id)
        for i, d in enumerate(items):
            offers_wait = bool(set(d.event.control_actions) & {"resume", "cancel"})
            if i < len(items) - 1:
                out.append((d, "a newer control message was delivered"))
            elif stage in _TERMINAL_STAGES:
                out.append((d, f"run finished ({stage})"))
            elif stage and offers_wait and stage not in _HALTED_STAGES:
                out.append((d, f"run resumed ({stage})"))
            elif stage and not offers_wait and stage in _HALTED_STAGES:
                out.append((d, f"run halted ({stage})"))
    return out


class DiscordGatewayClient(discord.Client):
    def __init__(self, notifier: DiscordNotifier) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.notifier = notifier
        self.settings = notifier.settings
        self.approvals = DiscordApprovalInput(notifier.store, notifier.settings)
        self.controls = DiscordControlInput(notifier.store, notifier.settings)
        self._views_restored = False
        self._lease_owner = f"discord:{id(self)}"
        self.startup_error: Exception | None = None

    async def setup_hook(self) -> None:
        # Channel objects and the bot member are available only after READY.
        pass

    async def on_ready(self) -> None:
        try:
            await self.validate_configuration()
        except Exception as exc:  # fail closed instead of running with a partial channel configuration
            self.startup_error = exc
            await self.close()
            return
        if not self._views_restored:
            for record in self.notifier.store.find(db.notifications_sent):
                if "event" not in record:
                    continue
                delivery = DiscordDelivery.model_validate(record)
                req_id = delivery.event.approval_request_id
                if delivery.state is DeliveryState.SENT and delivery.message_id and req_id:
                    self.add_view(ApprovalView(self.approvals, req_id), message_id=int(delivery.message_id))
                elif (delivery.state is DeliveryState.SENT and delivery.message_id and delivery.event.control_task_id
                      and not delivery.controls_closed):
                    self.add_view(ControlView(self.controls, delivery.event.control_task_id,
                                              delivery.event.control_actions), message_id=int(delivery.message_id))
            self._views_restored = True
        if not self.deliver_pending.is_running():
            self.deliver_pending.start()
        if not self.reconcile_approvals.is_running():
            self.reconcile_approvals.start()

    async def validate_configuration(self) -> None:
        guild = self.get_guild(self.settings.guild_id)
        if guild is None:
            raise RuntimeError("configured Discord guild is not available to the bot")
        for route in DiscordRoute:
            channel = guild.get_channel(self.settings.channel_id(route))
            if not isinstance(channel, discord.TextChannel):
                raise RuntimeError(f"configured {route.value} channel is not a text channel in the guild")
            me = guild.me
            perms = channel.permissions_for(me) if me else None
            if not perms or not (perms.view_channel and perms.send_messages and perms.embed_links and perms.read_message_history):
                raise RuntimeError(f"bot lacks view/send/embed/history permissions in {route.value} channel")

    @tasks.loop(seconds=5)
    async def deliver_pending(self) -> None:
        if not self.is_ready():
            return
        for delivery in self.notifier.pending():
            channel = self.get_channel(self.settings.channel_id(delivery.route))
            if not isinstance(channel, discord.TextChannel):
                continue
            lease_key = f"discord:{sha256(delivery.dedupe_key.encode()).hexdigest()[:40]}"
            if not self.notifier.store.acquire_lease(lease_key, self._lease_owner, 120):
                continue
            try:
                marker = _delivery_marker(delivery.dedupe_key)
                adopted = None
                async for previous in channel.history(limit=100):
                    if any(embed.footer and embed.footer.text == marker for embed in previous.embeds):
                        adopted = previous
                        break
                if adopted is not None:
                    self.notifier.save(delivery.model_copy(update={
                        "state": DeliveryState.SENT, "message_id": str(adopted.id), "last_error": None,
                    }))
                    continue
                view = None
                if delivery.event.approval_request_id:
                    raw = self.notifier.store.get(db.approval_requests, delivery.event.approval_request_id)
                    if raw and raw.get("status") == ApprovalStatus.PENDING.value:
                        view = ApprovalView(self.approvals, delivery.event.approval_request_id)
                elif delivery.event.control_task_id and delivery.route is not DiscordRoute.LOGS:
                    view = ControlView(self.controls, delivery.event.control_task_id, delivery.event.control_actions)
                message = await channel.send(embed=_embed(delivery.event, marker), view=view)
                delivery = delivery.model_copy(update={"has_view": view is not None, "delivered_at": utcnow()})
            except Exception as exc:  # discord.py handles 429; keep other failures retryable
                attempts = delivery.attempts + 1
                delivery = delivery.model_copy(update={
                    "state": DeliveryState.FAILED,
                    "attempts": attempts,
                    "last_error": type(exc).__name__,
                    "next_attempt_at": utcnow() + timedelta(seconds=min(300, 2 ** min(attempts, 8))),
                })
                self.notifier.save(delivery)
            else:
                delivery = delivery.model_copy(update={"state": DeliveryState.SENT, "message_id": str(message.id),
                                                       "attempts": delivery.attempts + 1, "last_error": None})
                self.notifier.save(delivery)
            finally:
                self.notifier.store.release_lease(lease_key, self._lease_owner)

    @deliver_pending.before_loop
    async def _before_delivery(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=15)
    async def reconcile_approvals(self) -> None:
        """Close buttons that no longer apply (UX policy 1.4): decided / stale / expired approval requests (a
        replaced or expired request is edited to point to its replacement) and Stop / Resume / Cancel views whose
        run has moved on (newer control message for the task, resumed, finished)."""
        if not self.is_ready():
            return
        store = self.notifier.store
        for delivery in _sent_deliveries(store):
            req_id = delivery.event.approval_request_id
            if not req_id or (delivery.controls_closed and delivery.superseded_linked):
                continue
            raw = store.get(db.approval_requests, req_id)
            if not raw or raw.get("status") == ApprovalStatus.PENDING.value:
                continue
            closure = approval_closure(store, self.settings, raw)
            if delivery.controls_closed and closure is None:
                continue
            if delivery.controls_closed and not closure[1]:
                continue  # already says "a new request follows"; edit again once the replacement is delivered
            channel = self.get_channel(self.settings.channel_id(delivery.route))
            if not isinstance(channel, discord.TextChannel):
                continue
            edit: dict = {}
            if closure is not None:
                event = delivery.event.model_copy(update={"details": [*delivery.event.details, closure[0]],
                                                          "steps": []})
                edit["embed"] = _embed(event, _delivery_marker(delivery.dedupe_key))
            if delivery.has_view is not False:  # never add (disabled) buttons to a message that had none
                edit["view"] = ApprovalView(self.approvals, req_id, disabled=True)
            try:
                if edit:
                    message = await channel.fetch_message(int(delivery.message_id))
                    await message.edit(**edit)
            except (discord.NotFound, discord.Forbidden):
                pass
            else:
                self.notifier.save(delivery.model_copy(update={
                    "controls_closed": True, "superseded_linked": closure is None or closure[1]}))
        for delivery, _why in control_deliveries_to_close(store):
            channel = self.get_channel(self.settings.channel_id(delivery.route))
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                message = await channel.fetch_message(int(delivery.message_id))
                await message.edit(view=ControlView(self.controls, delivery.event.control_task_id,
                                                    delivery.event.control_actions, disabled=True))
            except (discord.NotFound, discord.Forbidden):
                pass
            else:
                self.notifier.save(delivery.model_copy(update={"controls_closed": True}))

    @reconcile_approvals.before_loop
    async def _before_reconcile(self) -> None:
        await self.wait_until_ready()


def run_gateway(store: Store, settings: DiscordSettings) -> None:
    notifier = DiscordNotifier(store, settings)
    client = DiscordGatewayClient(notifier)
    client.run(settings.token.get_secret_value(), reconnect=True, log_handler=None)
    if client.startup_error is not None:
        raise RuntimeError(f"Discord Gateway configuration invalid: {client.startup_error}") from client.startup_error


async def connection_check(settings: DiscordSettings, *, send_test: bool = False) -> list[str]:
    """Gateway-backed credential/channel/permission check; never prints the token."""
    checked: list[str] = []
    done = asyncio.get_running_loop().create_future()

    class CheckClient(discord.Client):
        async def on_ready(self) -> None:
            try:
                guild = self.get_guild(settings.guild_id)
                if guild is None or guild.me is None:
                    raise RuntimeError("configured Discord guild is not available to the bot")
                for route in DiscordRoute:
                    channel = guild.get_channel(settings.channel_id(route))
                    if not isinstance(channel, discord.TextChannel):
                        raise RuntimeError(f"{route.value} is not a text channel in the configured guild")
                    perms = channel.permissions_for(guild.me)
                    if not (perms.view_channel and perms.send_messages and perms.embed_links
                            and perms.read_message_history):
                        raise RuntimeError(f"bot lacks view/send/embed/history permissions in {route.value} channel")
                    checked.append(f"{route.value}:{channel.id}")
                    if send_test:
                        event = NotificationEvent(kind={
                            DiscordRoute.APPROVALS: EventKind.APPROVAL_REQUIRED,
                            DiscordRoute.RESULTS: EventKind.TASK_COMPLETED,
                            DiscordRoute.ERRORS: EventKind.WORKER_FAILURE,
                            DiscordRoute.LOGS: EventKind.PHASE_TRANSITION,
                        }[route], flow_name="discord-connection-check", state_name="test",
                            details=["Research Autopilot Discord 接続テスト（実タスクではありません）"])
                        await channel.send(embed=_embed(event))
            except Exception as exc:  # noqa: BLE001 - propagate event callback errors to the CLI
                done.set_exception(exc)
            else:
                done.set_result(None)
            finally:
                await self.close()

    intents = discord.Intents.none()
    intents.guilds = True
    client = CheckClient(intents=intents)
    await client.start(settings.token.get_secret_value(), reconnect=False)
    await done
    return checked


__all__ = [
    "DiscordApprovalInput", "DiscordDelivery", "DiscordGatewayClient", "DiscordNotifier", "DiscordRoute",
    "DiscordSettings", "InteractionResult", "connection_check", "embed_payload", "load_discord_settings",
    "route_event", "run_gateway",
]
