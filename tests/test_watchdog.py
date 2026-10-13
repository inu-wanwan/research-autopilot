"""Watchdog: deterministic detection, post-once reporting to the report channel, read-only triage."""

from datetime import timedelta

from research_autopilot.autopilot.watchdog import (
    WatchSettings,
    WatchTriage,
    detect,
    detection_tick,
    load_watch_settings,
    split_message,
)
from research_autopilot.autopilot.watchdog_fix import run_watch
from research_autopilot.models import utcnow
from research_autopilot.storage import db
from research_autopilot.storage.db import Store


class FakeREST:
    def __init__(self):
        self.posts, self.threads, self._n = [], [], 100

    def post(self, channel_id, text):
        self._n += 1
        self.posts.append((channel_id, text))
        return str(self._n)

    def start_thread(self, channel_id, message_id, name):
        self.threads.append((channel_id, message_id, name))
        return f"t{message_id}"


def _store(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 'ra.db'}")
    s.init_schema()
    return s


def _settings(**kw):
    return WatchSettings(token="x", report_channel_id="555", authorized_user_ids={"1"}, **kw)


def _run(store, task, stage, reason=None, run_id=None):
    run_id = run_id or f"r-{task}-{stage}"
    store.put(db.workflow_runs, run_id, {"run_id": run_id, "task_id": task, "stage": stage, "blocked_reason": reason,
                                          "attempts": 0}, task_id=task, stage=stage)
    return run_id


def _age(store, table, key, hours):
    kcol = next(iter(table.primary_key.columns))
    with store.engine.begin() as conn:
        conn.execute(table.update().where(kcol == key).values(
            created_at=utcnow() - timedelta(hours=hours), updated_at=utcnow() - timedelta(hours=hours)))


def _triage(calls):
    def run(context):
        calls.append(context)
        return (WatchTriage(category="spec_gap", summary="s", likely_cause="c", next_action="n",
                            needs_human_decision=True, confidence="medium"), "codex:test", {"input_tokens": 10,
                                                                                             "output_tokens": 5})
    return run


def test_first_round_records_existing_stops_without_reporting_them(tmp_path):
    store, rest = _store(tmp_path), FakeREST()
    _run(store, "INU-1", "blocked", "old reason")
    out = detection_tick(store, _settings(), rest, None)
    assert out["baseline"] and len(rest.posts) == 1 and "既に止まっている 1 件" in rest.posts[0][1]
    detection_tick(store, _settings(), rest, None)
    assert len(rest.posts) == 1  # the old stop is never reported


def test_a_new_stop_is_posted_once_with_a_triage_thread(tmp_path):
    store, rest, calls = _store(tmp_path), FakeREST(), []
    detection_tick(store, _settings(), rest, None)  # baseline (nothing stopped)
    _run(store, "INU-2", "blocked", "Phase 4 blocked: units check failed")
    detection_tick(store, _settings(), rest, _triage(calls))
    detection_tick(store, _settings(), rest, _triage(calls))
    alerts = [p for p in rest.posts if p[0] == "555" and "INU-2" in p[1]]
    assert len(alerts) == 1 and "units check failed" in alerts[0][1]
    assert len(rest.threads) == 1 and len(calls) == 1 and "units check failed" in calls[0]
    thread_posts = [p for p in rest.posts if p[0].startswith("t")]
    assert len(thread_posts) == 1 and "仕様の不足" in thread_posts[0][1]


def test_the_same_task_stopping_for_a_new_reason_is_a_new_alert(tmp_path):
    store, rest = _store(tmp_path), FakeREST()
    detection_tick(store, _settings(), rest, None)
    run_id = _run(store, "INU-3", "blocked", "first")
    detection_tick(store, _settings(), rest, None)
    _run(store, "INU-3", "blocked", "second", run_id=run_id)
    detection_tick(store, _settings(), rest, None)
    assert len([p for p in rest.posts[1:] if "INU-3" in p[1]]) == 2


def test_triage_respects_the_daily_cap(tmp_path):
    store, rest, calls = _store(tmp_path), FakeREST(), []
    s = _settings(triage_daily_max=1)
    detection_tick(store, s, rest, None)
    _run(store, "INU-4", "blocked", "a")
    _run(store, "INU-5", "dead_letter", "b")
    detection_tick(store, s, rest, _triage(calls))
    assert len(calls) == 1  # the second error alert is still posted, without a triage
    assert sum("INU-4" in p[1] or "INU-5" in p[1] for p in rest.posts) == 2


def test_only_the_latest_run_of_a_task_counts(tmp_path):
    store = _store(tmp_path)
    _run(store, "INU-6", "blocked", "old", run_id="a")
    _run(store, "INU-6", "done", run_id="b")
    assert detect(store, _settings()) == []


def test_stall_approval_wait_heartbeat_and_discord_backlog(tmp_path):
    store = _store(tmp_path)
    rid = _run(store, "INU-7", "experimenting")
    _age(store, db.workflow_runs, rid, 7)
    store.put(db.approval_requests, "ap1", {"request_id": "ap1", "task_id": "INU-8", "kind": "dag"}, task_id="INU-8",
              status="pending", dedupe_key="d1")
    _age(store, db.approval_requests, "ap1", 30)
    store.put(db.heartbeats, "autopilot_poll", {"at": (utcnow() - timedelta(hours=4)).isoformat()})
    store.put(db.notifications_sent, "n1", {"dedupe_key": "n1", "event": {}, "state": "pending"})
    _age(store, db.notifications_sent, "n1", 1)
    kinds = {a.kind for a in detect(store, _settings())}
    assert kinds == {"run_stalled", "approval_waiting", "poll_stale", "discord_backlog"}


def test_a_parent_blocked_by_a_reported_child_is_not_reported_twice(tmp_path):
    store = _store(tmp_path)
    child = _run(store, "INU-11", "blocked", "child reason")
    store.put(db.parent_runs, "p1", {"parent_run_id": "p1", "parent_task_id": "INU-10", "status": "Blocked",
                                     "child_workflow_run_ids": {"INU-11": child}, "completion_reasons": ["x"]},
              parent_task_id="INU-10", parent_execution_run_id="e1", status="Blocked")
    assert [a.kind for a in detect(store, _settings())] == ["run_stopped"]
    store.put(db.parent_runs, "p2", {"parent_run_id": "p2", "parent_task_id": "INU-12", "status": "Failed",
                                     "child_workflow_run_ids": {}, "completion_reasons": ["gate"]},
              parent_task_id="INU-12", parent_execution_run_id="e2", status="Failed")
    assert "parent_stopped" in [a.kind for a in detect(store, _settings())]


def test_dry_run_posts_nothing_and_a_failed_post_is_retried(tmp_path):
    store, rest = _store(tmp_path), FakeREST()
    _run(store, "INU-13", "blocked", "r")
    out = run_watch(store, _settings(), rest, dry_run=True)
    assert rest.posts == [] and len(out["alerts"]) == 1
    detection_tick(store, _settings(), rest, None)  # baseline
    _run(store, "INU-14", "blocked", "r2")

    class Flaky(FakeREST):
        def post(self, channel_id, text):
            import httpx
            raise httpx.ConnectError("down")
    detection_tick(store, _settings(), Flaky(), None)
    detection_tick(store, _settings(), rest, None)
    assert any("INU-14" in p[1] for p in rest.posts)


def test_settings_and_message_splitting():
    env = {"RA_DISCORD_BOT_TOKEN": "t", "RA_DISCORD_REPORT_CHANNEL_ID": "1559474403400491088",
           "RA_DISCORD_AUTHORIZED_USER_IDS": "1,2", "RA_WATCH_TRIAGE_PROVIDERS": "claude,api"}
    s = load_watch_settings(env)
    assert s.report_channel_id == "1559474403400491088" and s.triage_providers == ["claude"]  # never the API
    try:
        load_watch_settings({**env, "RA_DISCORD_REPORT_CHANNEL_ID": "abc"})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    parts = split_message("a" * 1000 + "\n" + "b" * 1500)
    assert len(parts) == 2 and all(len(x) <= 1900 for x in parts)


def test_the_loop_survives_a_failing_round(tmp_path):
    store = _store(tmp_path)

    class Broken(FakeREST):
        def post(self, channel_id, text):
            raise RuntimeError("boom")

        def me(self):
            raise RuntimeError("discord down")
    slept = []

    def sleep(n):
        slept.append(n)
        if len(slept) == 2:
            raise KeyboardInterrupt
    try:
        run_watch(store, _settings(), Broken(), interval=5, mention_interval=7, sleep=sleep)
    except KeyboardInterrupt:
        pass
    assert slept == [7, 7]
