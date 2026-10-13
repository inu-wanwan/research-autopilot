"""Watchdog fixes by mention: who is heard, worktree / branch isolation, tests, commit, apply / discard."""

import subprocess
import sys
from pathlib import Path

from research_autopilot.autopilot.watchdog import WatchSettings
from research_autopilot.autopilot.watchdog_fix import (
    AgentOutcome,
    apply_fix,
    execute_fix,
    handle_message,
    job_from,
    mention_tick,
    run_watch,
)
from research_autopilot.storage import db
from research_autopilot.storage.db import Store

BOT, USER, STRANGER, CH = "900", "1", "2", "555"


class FakeREST:
    def __init__(self, inbox=None):
        self.posts, self.threads, self.inbox, self._n = [], [], inbox or {}, 1000

    def me(self):
        return BOT

    def post(self, channel_id, text):
        self._n += 1
        self.posts.append((channel_id, text))
        return str(self._n)

    def start_thread(self, channel_id, message_id, name):
        self.threads.append((channel_id, message_id, name))
        return f"t{message_id}"

    def messages(self, channel_id, after=None, limit=50):
        msgs = sorted(self.inbox.get(channel_id, []), key=lambda m: int(m["id"]))
        if after is None:
            return msgs[-limit:]
        return [m for m in msgs if int(m["id"]) > int(after)][:limit]


def _msg(mid, text, author=USER, mention=True, bot=False):
    return {"id": str(mid), "content": (f"<@{BOT}> " if mention else "") + text,
            "author": {"id": author, "bot": bot}, "mentions": [{"id": BOT}] if mention else []}


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path, branch="work"):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("old\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _settings(tmp_path, repo, test_ok=True, **kw):
    code = "pass" if test_ok else "raise SystemExit(1)"
    return WatchSettings(token="x", report_channel_id=CH, authorized_user_ids={USER}, fix_repo=repo,
                         fix_dir=tmp_path / "autofix", fix_test_command=f'{sys.executable} -c "{code}"', **kw)


def _store(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 'ra.db'}")
    s.init_schema()
    return s


def _edit_agent(text="new\n"):
    def agent(worktree: Path, prompt, providers, timeout):
        (worktree / "a.txt").write_text(text)
        return AgentOutcome(True, "fake:model", "a.txt を直しました", {"input_tokens": 1, "output_tokens": 1})
    return agent


def test_history_is_never_acted_on_and_only_mentions_count(tmp_path):
    store, repo = _store(tmp_path), _repo(tmp_path)
    s = _settings(tmp_path, repo)
    rest = FakeREST({CH: [_msg(10, "old request")]})
    assert mention_tick(store, s, rest) == []  # first look only sets the cursor
    rest.inbox[CH] += [_msg(11, "no mention", mention=False), _msg(12, "from a bot", bot=True)]
    assert mention_tick(store, s, rest) == [] and rest.posts == []


def test_an_unauthorized_user_is_refused(tmp_path):
    store, repo = _store(tmp_path), _repo(tmp_path)
    rest = FakeREST()
    assert handle_message(store, _settings(tmp_path, repo), rest, CH, _msg(20, "直して", author=STRANGER)) is None
    assert "認可" in rest.posts[0][1] and store.find(db.watchdog_fixes) == []


def test_a_top_level_request_gets_a_thread_and_a_queued_fix(tmp_path):
    store, repo = _store(tmp_path), _repo(tmp_path)
    s = _settings(tmp_path, repo)
    rest = FakeREST({CH: []})
    mention_tick(store, s, rest)
    rest.inbox[CH].append(_msg(30, "a.txt を new にして"))
    [fix_id] = mention_tick(store, s, rest)
    rec = store.get(db.watchdog_fixes, fix_id)
    assert rec["thread_id"] == "t30" and rec["state"] == "queued" and rec["target_branch"] == "work"
    assert rec["branch"].startswith("autofix/") and rest.threads == [(CH, "30", "fix: a.txt を new にして")]


def test_fix_runs_in_its_own_worktree_and_is_applied_only_on_request(tmp_path):
    store, repo = _store(tmp_path), _repo(tmp_path)
    s = _settings(tmp_path, repo)
    rest = FakeREST({CH: []})
    mention_tick(store, s, rest)
    rest.inbox[CH].append(_msg(40, "a.txt を new にして"))
    mention_tick(store, s, rest)
    out = run_watch(store, s, rest, once=True, agent=_edit_agent(), sleep=lambda n: None)
    [fix] = store.find(db.watchdog_fixes)
    assert out["fixes"][0]["state"] == "committed" and fix["tests_passed"]
    assert (repo / "a.txt").read_text() == "old\n"  # the working checkout is untouched until 適用
    assert _git(repo, "show", f"{fix['branch']}:a.txt") == "new"
    rest.inbox["t40"] = [_msg(41, "適用")]
    mention_tick(store, s, rest)
    assert (repo / "a.txt").read_text() == "new\n" and _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "work"
    assert store.get(db.watchdog_fixes, fix["fix_id"])["state"] == "applied"
    assert any("適用しました" in p[1] for p in rest.posts)


def test_failing_tests_main_branch_and_dirty_checkout_block_apply(tmp_path):
    store, repo = _store(tmp_path), _repo(tmp_path)
    rest = FakeREST()
    s = _settings(tmp_path, repo, test_ok=False)
    handle_message(store, s, rest, "t1", _msg(50, "直して"))
    [rec] = store.find(db.watchdog_fixes)
    rec.update(execute_fix(s, job_from(rec), rest, _edit_agent()))
    assert rec["state"] == "committed" and not rec["tests_passed"]
    assert "テストに合格" in apply_fix(s, rec)
    rec["tests_passed"] = True
    (repo / "a.txt").write_text("dirty\n")
    assert "未 commit" in apply_fix(s, rec)
    _git(repo, "checkout", "-q", "--", "a.txt")
    assert "main" in apply_fix(s, {**rec, "target_branch": "main"})


def test_a_follow_up_continues_on_the_same_branch_and_discard_removes_it(tmp_path):
    store, repo = _store(tmp_path), _repo(tmp_path)
    s = _settings(tmp_path, repo)
    rest = FakeREST()
    handle_message(store, s, rest, "t2", _msg(60, "直して"))
    run_watch(store, s, rest, once=True, agent=_edit_agent("v1\n"), sleep=lambda n: None)
    handle_message(store, s, rest, "t2", _msg(61, "もう少し直して"))
    run_watch(store, s, rest, once=True, agent=_edit_agent("v2\n"), sleep=lambda n: None)
    first, second = store.find(db.watchdog_fixes)
    assert second["branch"] == first["branch"] and second["base"] == first["base"] and "前回の修正" in second["context"]
    assert _git(repo, "show", f"{second['branch']}:a.txt") == "v2"
    handle_message(store, s, rest, "t2", _msg(62, "破棄"))
    assert _git(repo, "branch", "--list", second["branch"]) == ""


def test_an_agent_that_changes_nothing_commits_nothing(tmp_path):
    store, repo = _store(tmp_path), _repo(tmp_path)
    s = _settings(tmp_path, repo)
    rest = FakeREST()
    handle_message(store, s, rest, "t3", _msg(70, "確認だけ"))

    def idle(worktree, prompt, providers, timeout):
        assert "確認だけ" in prompt and "git commit" in prompt
        return AgentOutcome(True, "fake:model", "変更不要です", {})
    out = run_watch(store, s, rest, once=True, agent=idle, sleep=lambda n: None)
    assert out["fixes"][0]["state"] == "no_change"
