"""Watchdog: report stops / stalls to a Discord report channel and triage them.

User request (2026-10-13): keep a resident monitor like the one used in the Phase A E2E, without spending tokens
while things are healthy.

- Detection is deterministic (database only, no LLM): latest workflow run blocked / dead-lettered or not moving,
  parent DAG blocked / failed, long-pending approvals, dead outbox items, Discord deliveries not going out (the
  gateway is down) and a poller that stopped completing ticks (``autopilot_poll`` heartbeat).
- Each new alert is posted once to ``RA_DISCORD_REPORT_CHANNEL_ID`` over the Discord REST API (independent of the
  gateway, so it also reports a dead gateway). The first run only records what is already stopped.
- Error alerts get one read-only triage on a subscription (Codex, then Claude; never the pay-as-you-go API), at
  most ``RA_WATCH_TRIAGE_DAILY_MAX`` per day, posted in a thread under the alert. The triage only reads the alert
  text and the task's recent run history; it has no tools and changes nothing.
- Mentions of the bot by an authorized user in the report channel or its threads are fix requests
  (``watchdog_fix``, user approval 2026-10-13): the fix runs in its own git worktree / branch, is tested and
  committed there, and is applied to the working branch only on an explicit "適用". Never main, never push.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal

import httpx
import sqlalchemy as sa
from pydantic import BaseModel

from research_autopilot.models import utcnow
from research_autopilot.storage import db
from research_autopilot.storage.db import Store

log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
_ACTIVE_STAGES = {"discovered", "planning", "implementing", "experimenting", "proposing", "completing", "researching"}
_STOPPED_STAGES = {"blocked", "dead_letter"}


# ---------------------------------------------------------------- settings

@dataclass
class WatchSettings:
    token: str
    report_channel_id: str
    authorized_user_ids: set[str]
    stall_hours: float = 6.0
    approval_hours: float = 24.0
    poll_stale_minutes: float = 180.0  # one poll tick may legitimately run a long experiment
    discord_backlog_minutes: float = 15.0
    triage: bool = True
    triage_daily_max: int = 10
    triage_providers: list[str] = field(default_factory=lambda: ["codex", "claude"])
    fix: bool = True
    fix_providers: list[str] = field(default_factory=lambda: ["codex", "claude"])
    fix_timeout: int = 2400
    fix_test_command: str = "uv run pytest -q -x -p no:cacheprovider"
    fix_test_timeout: int = 1800
    fix_repo: Path = Path(".")
    fix_dir: Path = Path(".prefect/research-autopilot-state/autofix")


def load_watch_settings(env: dict[str, str] | None = None) -> WatchSettings:
    env = dict(os.environ) if env is None else env

    def need(name: str) -> str:
        if not (v := env.get(name, "").strip()):
            raise ValueError(f"{name} is required for the watchdog")
        return v

    def num(name: str, default: float) -> float:
        return float(env.get(name, "").strip() or default)

    def flag(name: str, default: bool) -> bool:
        v = env.get(name, "").strip().lower()
        return default if not v else v in ("1", "true", "yes", "on")

    def providers(name: str) -> list[str]:
        v = env.get(name, "").strip().lower() or "codex,claude"
        return [p for p in (x.strip() for x in v.split(",")) if p in ("codex", "claude")]

    channel = need("RA_DISCORD_REPORT_CHANNEL_ID")
    users = {u.strip() for u in need("RA_DISCORD_AUTHORIZED_USER_IDS").split(",") if u.strip()}
    if not channel.isdecimal() or not users or not all(u.isdecimal() for u in users):
        raise ValueError("RA_DISCORD_REPORT_CHANNEL_ID / RA_DISCORD_AUTHORIZED_USER_IDS must be decimal Discord IDs")
    return WatchSettings(
        token=need("RA_DISCORD_BOT_TOKEN"), report_channel_id=channel, authorized_user_ids=users,
        stall_hours=num("RA_WATCH_STALL_HOURS", 6), approval_hours=num("RA_WATCH_APPROVAL_HOURS", 24),
        poll_stale_minutes=num("RA_WATCH_POLL_STALE_MINUTES", 180),
        discord_backlog_minutes=num("RA_WATCH_DISCORD_BACKLOG_MINUTES", 15),
        triage=flag("RA_WATCH_TRIAGE", True), triage_daily_max=int(num("RA_WATCH_TRIAGE_DAILY_MAX", 10)),
        triage_providers=providers("RA_WATCH_TRIAGE_PROVIDERS"),
        fix=flag("RA_WATCH_FIX", True), fix_providers=providers("RA_WATCH_FIX_PROVIDERS"),
        fix_timeout=int(num("RA_WATCH_FIX_TIMEOUT", 2400)),
        fix_test_command=env.get("RA_WATCH_FIX_TEST_COMMAND", "").strip() or "uv run pytest -q -x -p no:cacheprovider",
        fix_test_timeout=int(num("RA_WATCH_FIX_TEST_TIMEOUT", 1800)),
        fix_repo=Path(env.get("RA_WATCH_FIX_REPO", "").strip() or ".").resolve(),
        fix_dir=Path(env.get("RA_WATCH_FIX_DIR", "").strip()
                     or Path(env.get("RA_STATE_DIR", "").strip() or ".prefect/research-autopilot-state") / "autofix"
                     ).resolve(),
    )


# ---------------------------------------------------------------- Discord REST

class DiscordREST:
    """The few REST calls the watchdog needs (bot token; mentions are readable without the content intent)."""

    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(base_url=API, timeout=30,
                                              headers={"Authorization": f"Bot {token}"})
        self._me: str | None = None

    def _call(self, method: str, path: str, **kw) -> dict | list:
        for _ in range(3):
            r = self._client.request(method, path, **kw)
            if r.status_code == 429:  # rate limited: wait as told, then retry
                time.sleep(min(float((r.json() or {}).get("retry_after", 1.0)), 10.0))
                continue
            r.raise_for_status()
            return r.json() if r.content else {}
        r.raise_for_status()
        return {}

    def me(self) -> str:
        if self._me is None:
            self._me = str(self._call("GET", "/users/@me")["id"])
        return self._me

    def post(self, channel_id: str, text: str) -> str:
        """Post (split at 1900 characters); returns the first message id."""
        first = ""
        for chunk in split_message(text):
            m = self._call("POST", f"/channels/{channel_id}/messages",
                           json={"content": chunk, "allowed_mentions": {"parse": []}})
            first = first or str(m["id"])
        return first

    def start_thread(self, channel_id: str, message_id: str, name: str) -> str:
        t = self._call("POST", f"/channels/{channel_id}/messages/{message_id}/threads",
                       json={"name": name[:90] or "thread", "auto_archive_duration": 4320})
        return str(t["id"])

    def messages(self, channel_id: str, after: str | None = None, limit: int = 50) -> list[dict]:
        params: dict = {"limit": limit}
        if after:
            params["after"] = after
        got = self._call("GET", f"/channels/{channel_id}/messages", params=params)
        return sorted(got if isinstance(got, list) else [], key=lambda m: int(m["id"]))


def split_message(text: str, size: int = 1900) -> list[str]:
    text = text.strip() or "(empty)"
    out = []
    while text:
        cut = text.rfind("\n", 0, size) if len(text) > size else len(text)
        cut = cut if cut > size // 2 else min(size, len(text))
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out


# ---------------------------------------------------------------- detection

@dataclass
class Alert:
    key: str
    kind: str
    severity: Literal["error", "warning", "info"]
    title: str
    detail: str
    task_id: str | None = None
    context: dict = field(default_factory=dict)


def _h(text: str | None) -> str:
    return sha256((text or "").encode()).hexdigest()[:10]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _rows(store: Store, table: sa.Table, **where) -> list[tuple[dict, datetime, datetime]]:
    q = sa.select(table.c.record, table.c.created_at, table.c.updated_at).order_by(table.c.created_at)
    for k, v in where.items():
        q = q.where(table.c[k] == v)
    with store.engine.connect() as conn:
        return [(r[0], _aware(r[1]), _aware(r[2])) for r in conn.execute(q).all()]


def _latest_by(rows, key: str) -> dict[str, tuple[dict, datetime, datetime]]:
    latest: dict = {}
    for row in rows:  # ordered by created_at, so the last one wins
        latest[row[0].get(key)] = row
    return latest


def _ago(now: datetime, then: datetime) -> str:
    m = int((now - then).total_seconds() // 60)
    return f"{m // 60} 時間 {m % 60} 分" if m >= 60 else f"{m} 分"


def detect(store: Store, s: WatchSettings, now: datetime | None = None) -> list[Alert]:
    now = now or utcnow()
    alerts: list[Alert] = []
    runs = _latest_by(_rows(store, db.workflow_runs), "task_id")
    stopped_run_ids = set()
    for task, (r, _created, updated) in runs.items():
        stage = r.get("stage")
        if stage in _STOPPED_STAGES:
            stopped_run_ids.add(r.get("run_id"))
            reason = r.get("blocked_reason") or ""
            alerts.append(Alert(f"run:{r['run_id']}:{stage}:{_h(reason)}", "run_stopped", "error",
                                f"{task} が {stage} で止まりました", reason[:1500], task,
                                {"run_id": r["run_id"], "stage": stage}))
        elif stage in _ACTIVE_STAGES and now - updated > timedelta(hours=s.stall_hours):
            alerts.append(Alert(f"stall:{r['run_id']}:{updated.isoformat()}", "run_stalled", "warning",
                                f"{task} が {stage} のまま {_ago(now, updated)} 進んでいません",
                                "長い実験の実行中の可能性もあります（poll の 1 tick が実験の完了を待つため）。", task,
                                {"run_id": r["run_id"], "stage": stage}))
    for p, _created, _updated in _latest_by(_rows(store, db.parent_runs), "parent_task_id").values():
        if p.get("status") not in ("Blocked", "Failed"):
            continue
        if set((p.get("child_workflow_run_ids") or {}).values()) & stopped_run_ids:
            continue  # the stopped child is reported itself
        reason = (p.get("completion_reasons") or [""])[-1]
        alerts.append(Alert(f"parent:{p['parent_run_id']}:{p['status']}:{_h(reason)}", "parent_stopped", "error",
                            f"親タスク {p['parent_task_id']} が {p['status']} です", reason[:1500],
                            p["parent_task_id"], {"parent_run_id": p["parent_run_id"]}))
    for a, created, _updated in _rows(store, db.approval_requests, status="pending"):
        if now - created > timedelta(hours=s.approval_hours):
            alerts.append(Alert(f"approval:{a['request_id']}", "approval_waiting", "info",
                                f"{a.get('task_id')} の承認（{a.get('kind')}）が {_ago(now, created)} 待っています",
                                a.get("summary") or a.get("title") or "", a.get("task_id")))
    for o, _created, _updated in _rows(store, db.outbox, state="dead"):
        alerts.append(Alert(f"outbox:{o.get('key')}", "outbox_dead", "error",
                            f"Notion / Linear への書き込みが諦められました（{o.get('kind')}）",
                            str(o.get("last_error") or "")[:1000], None))
    backlog = [(d, created) for d, created, _u in _rows(store, db.notifications_sent)
               if "event" in d and d.get("state") in ("pending", "failed")
               and now - created > timedelta(minutes=s.discord_backlog_minutes)]
    if backlog:
        oldest, created = backlog[0]
        alerts.append(Alert(f"discord-backlog:{oldest['dedupe_key']}", "discord_backlog", "warning",
                            f"Discord への通知 {len(backlog)} 件が {_ago(now, created)} 以上送られていません",
                            "Discord gateway（`research-agent discord run`）が止まっている可能性があります。"
                            f" 最後のエラー: {oldest.get('last_error') or 'なし'}", None))
    hb = store.get(db.heartbeats, "autopilot_poll")
    if hb and (at := datetime.fromisoformat(hb["at"])) and now - _aware(at) > timedelta(minutes=s.poll_stale_minutes):
        alerts.append(Alert(f"poll-stale:{hb['at']}", "poll_stale", "warning",
                            f"autopilot poll が {_ago(now, _aware(at))} 完了していません",
                            "poll ループ / Prefect worker が止まっているか、1 tick が長い実験を待っています。", None))
    return alerts


# ---------------------------------------------------------------- triage

class WatchTriage(BaseModel):
    category: Literal["spec_gap", "mechanism_bug", "environment", "quota_or_budget", "transient",
                      "scientific_decision", "unknown"]
    summary: str
    likely_cause: str
    next_action: str
    needs_human_decision: bool
    confidence: Literal["low", "medium", "high"]


CATEGORY_JA = {"spec_gap": "仕様の不足", "mechanism_bug": "仕組みの不具合", "environment": "実行環境",
               "quota_or_budget": "上限・予算", "transient": "一時的な失敗", "scientific_decision": "科学的判断が必要",
               "unknown": "不明"}

TRIAGE_INSTRUCTIONS = """あなたは研究自動化システム Research Autopilot の運用監視担当です。
停止したタスクの情報だけを読み、原因の見立てを返してください。コードやファイルは読めません。推測は推測と書くこと。
分類: spec_gap（タスク仕様に必要な情報がない・矛盾）、mechanism_bug（システム側の不具合）、environment（Docker・メモリ・
依存・認証など実行環境）、quota_or_budget（サブスク上限・トークン/費用上限）、transient（再試行で直る可能性が高い）、
scientific_decision（科学的仕様・許容誤差・評価規則など人が決めること）、unknown。
科学的仕様の変更を勧めるときは、人の承認が必要だと書くこと。
summary / likely_cause / next_action はそれぞれ日本語で 1〜3 文。next_action は具体的なコマンドや確認先を書く
（Notion の該当節に追記、など）。コマンドは次の実在するものだけを使うこと（引数の形も含めて）:
`research-agent autopilot status`（引数なし、全体の状態）、`research-agent autopilot resume <TASK> --stage <stage>`、
`research-agent autopilot cancel <TASK>`、`research-agent research status <TASK>`、`research-agent research resume <TASK>`、
`research-agent research stop <TASK>`。親タスクの再実行は Notion の Run Autopilot（Run Requested）で行う。"""


def triage_context(store: Store, alert: Alert, limit: int = 6000) -> str:
    lines = [f"アラート: {alert.title}", f"種別: {alert.kind}", f"詳細:\n{alert.detail}"]
    if alert.task_id:
        history = [r for r, _c, _u in _rows(store, db.workflow_runs, task_id=alert.task_id)][-5:]
        if history:
            lines.append("このタスクの直近の実行（古い順）:")
            lines += [f"- stage={r.get('stage')} attempts={r.get('attempts')} reason={(r.get('blocked_reason') or '')[:400]}"
                      for r in history]
    return "\n".join(lines)[:limit]


TriageFn = Callable[[str], tuple[WatchTriage, str, dict] | None]


def subscription_triage(providers: list[str]) -> TriageFn:
    """Read-only triage on a subscription CLI; ``None`` when no subscription answered (never the paid API)."""
    from research_autopilot import llm_subscription as sub

    def run(context: str):
        for p in providers:
            try:
                if p == "codex":
                    model = os.environ.get("RA_WATCH_TRIAGE_CODEX_MODEL", "").strip() or "gpt-5.6-terra"
                    out, usage = sub.codex_structured(model, TRIAGE_INSTRUCTIONS, context, WatchTriage, 600, "low")
                else:
                    model = os.environ.get("RA_WATCH_TRIAGE_CLAUDE_MODEL", "").strip() or "claude-sonnet-5-5"
                    out, usage = sub.claude_structured(TRIAGE_INSTRUCTIONS, context, WatchTriage, 600, model=model)
                return out, f"{p}:{model}", usage
            except (sub.SubscriptionCallError, OSError) as exc:
                log.warning("watchdog triage on %s failed: %s", p, exc)
        return None
    return run


def format_triage(t: WatchTriage, provider: str, usage: dict) -> str:
    tokens = f"入力 {usage.get('input_tokens', 0):,} / 出力 {usage.get('output_tokens', 0):,} tokens"
    return (f"🔎 **見立て**（{provider}、{tokens}、確度 {t.confidence}）\n"
            f"分類: {CATEGORY_JA.get(t.category, t.category)}　人の判断: {'必要' if t.needs_human_decision else '不要の見込み'}\n"
            f"要約: {t.summary}\n原因の見込み: {t.likely_cause}\n次の一手: {t.next_action}\n"
            "-# 読み取り専用の見立てです（何も変更していません）。直す場合はこのスレッドで bot をメンションして指示してください。")


# ---------------------------------------------------------------- one detection tick

_ICON = {"error": "🛑", "warning": "⚠️", "info": "⏳"}


def format_alert(a: Alert) -> str:
    body = f"{_ICON[a.severity]} **{a.title}**"
    return body + (f"\n```\n{a.detail[:1500]}\n```" if a.detail else "")


def _state_get(store: Store, name: str):
    rec = store.get(db.watchdog_state, name)
    return rec.get("value") if rec else None


def _state_put(store: Store, name: str, value) -> None:
    store.put(db.watchdog_state, name, {"name": name, "value": value})


def _triage_allowed(store: Store, s: WatchSettings, now: datetime) -> bool:
    name = f"triage_count:{now.date().isoformat()}"
    used = int(_state_get(store, name) or 0)
    if used >= s.triage_daily_max:
        return False
    _state_put(store, name, used + 1)
    return True


def detection_tick(store: Store, s: WatchSettings, rest: DiscordREST, triage: TriageFn | None,
                   now: datetime | None = None, dry_run: bool = False) -> dict:
    now = now or utcnow()
    alerts = detect(store, s, now)
    if dry_run:
        return {"alerts": [a.__dict__ for a in alerts], "posted": []}
    if _state_get(store, "baseline_done") is None:
        for a in alerts:
            store.insert_new(db.watchdog_alerts, a.key, {"alert": a.__dict__, "state": "baseline"})
        rest.post(s.report_channel_id, f"👀 監視を開始しました。既に止まっている {len(alerts)} 件は通知しません"
                  "（`research-agent autopilot status` で確認できます）。以後、新しい停止・停滞だけを報告します。")
        _state_put(store, "baseline_done", now.isoformat())
        return {"alerts": [a.key for a in alerts], "posted": [], "baseline": True}
    posted = []
    for a in alerts:
        rec = store.get(db.watchdog_alerts, a.key) or {}
        if rec.get("state") in ("sent", "baseline") or int(rec.get("attempts", 0)) >= 5:
            continue
        rec = {"alert": a.__dict__, "state": "pending", "attempts": int(rec.get("attempts", 0)) + 1}
        store.put(db.watchdog_alerts, a.key, rec)
        try:
            rec["message_id"] = rest.post(s.report_channel_id, format_alert(a))
            rec["state"], rec["posted_at"] = "sent", now.isoformat()
            store.put(db.watchdog_alerts, a.key, rec)
            posted.append(a.key)
            if a.severity == "error" and triage is not None and _triage_allowed(store, s, now):
                rec["thread_id"] = rest.start_thread(s.report_channel_id, rec["message_id"], a.title)
                store.put(db.watchdog_alerts, a.key, rec)
                try:
                    got = triage(triage_context(store, a))
                except Exception as exc:  # noqa: BLE001 -- a failed triage must not stop the other alerts
                    log.warning("watchdog triage failed for %s: %s", a.key, exc)
                    got = None
                text = format_triage(*got) if got else "見立ては取れませんでした（サブスクが使えないか失敗）。"
                if got:
                    rec["triage"] = got[0].model_dump()
                rest.post(rec["thread_id"], text)
                store.put(db.watchdog_alerts, a.key, rec)
        except (httpx.HTTPError, KeyError) as exc:
            log.warning("watchdog could not post %s: %s", a.key, exc)
            rec["last_error"] = str(exc)[:300]
            store.put(db.watchdog_alerts, a.key, rec)
    return {"alerts": [a.key for a in alerts], "posted": posted}


def alert_for_thread(store: Store, thread_id: str) -> dict | None:
    for rec in store.find(db.watchdog_alerts):
        if rec.get("thread_id") == thread_id:
            return rec
    return None


def recent_threads(store: Store, now: datetime, days: int = 7, cap: int = 20) -> list[str]:
    """Threads followed for mentions: alert threads and fix threads updated in the last ``days``."""
    since = now - timedelta(days=days)
    ids = [rec["thread_id"] for rec, _c, updated in _rows(store, db.watchdog_alerts)
           if rec.get("thread_id") and updated > since]
    ids += [rec["thread_id"] for rec, _c, updated in _rows(store, db.watchdog_fixes)
            if rec.get("thread_id") and updated > since]
    return list(dict.fromkeys(reversed(ids)))[:cap]


def status_text(store: Store, s: WatchSettings) -> str:
    alerts = detect(store, s)
    if not alerts:
        return "✅ 止まっている・停滞しているものはありません。"
    return "現在の状態:\n" + "\n".join(f"{_ICON[a.severity]} {a.title}" for a in alerts[:20])


__all__ = [
    "Alert",
    "DiscordREST",
    "WatchSettings",
    "WatchTriage",
    "alert_for_thread",
    "detect",
    "detection_tick",
    "load_watch_settings",
    "recent_threads",
    "split_message",
    "status_text",
    "subscription_triage",
]
