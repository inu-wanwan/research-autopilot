"""Mention-driven fixes for the watchdog (user request 2026-10-13: "メンションしたメッセージからの指示で自己修正").

Only an authorized Discord user mentioning the bot in the report channel (or a thread under it) is heard.

- ``@bot <指示>``: a coding agent (Codex ``workspace-write`` sandbox, else Claude with the implementer's tool
  allow-list; subscriptions only) edits a fresh git worktree on ``autofix/<id>`` branched from the working branch.
  The watchdog then runs the test command there and commits (even on failing tests, so the result can be looked
  at). A further instruction in the same thread continues on the same branch.
- ``@bot 適用``: fast-forwards (or cherry-picks onto) the working branch checkout -- only when the tests passed, the
  checkout is clean and on the branch the fix started from, and that branch is not main / master. Never pushes.
- ``@bot 破棄``: removes that fix's worktree and branch.
- ``@bot 状態``: the current alerts.

The agent never commits / pushes / merges itself and gets the sanitized environment (no Notion / Linear / OpenAI /
Discord / DB credentials).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from research_autopilot.autopilot.watchdog import (
    DiscordREST,
    WatchSettings,
    _state_get,
    _state_put,
    alert_for_thread,
    recent_threads,
    status_text,
)
from research_autopilot.models import new_id, utcnow
from research_autopilot.storage import db
from research_autopilot.storage.db import Store

log = logging.getLogger(__name__)

_PROTECTED_BRANCHES = {"main", "master"}
_MENTION = re.compile(r"<@!?\d+>")


# ---------------------------------------------------------------- git

class FixError(Exception):
    pass


def git(repo: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], check=False, cwd=repo, capture_output=True, text=True, timeout=300,
                       stdin=subprocess.DEVNULL)
    if check and p.returncode != 0:
        raise FixError(f"git {' '.join(args)} failed: {(p.stderr or p.stdout).strip()[:300]}")
    return p.stdout


# ---------------------------------------------------------------- the coding agent

FIX_PROMPT = """あなたは Research Autopilot リポジトリの保守エージェントです。Discord の監視チャンネルで、認可されたユーザーから
次の修正依頼を受けました。この作業ツリー（git worktree、専用ブランチ）の中だけで作業してください。

## 依頼
{instruction}

## 関連する監視アラート・これまでの経緯
{context}

## ルール
- リポジトリの AGENTS.md を読み、それに従う。
- git commit / push / merge / checkout / reset はしない（commit はシステムが行う）。
- 科学的仕様（計算条件・評価指標・データセット・acceptance criteria、docs/phase_specs/）は変更しない。依頼がそれを
  求めるなら、変更せずに理由を書いて終える。
- 秘密情報をコード・文書に書かない。
- 仕組みの修正には pytest のテストを足し、関係するテストを `uv run pytest <path> -q` で実行する。
- 依頼が不明確・危険・範囲外なら、何も変更せず、確認したいことを書いて終える。
- 最後に日本語で、何を・なぜ変えたか、実行したテストと結果、残る懸念を 10 行以内で書く。
"""


@dataclass
class AgentOutcome:
    ok: bool
    provider: str
    summary: str
    usage: dict = field(default_factory=dict)
    error: str = ""


def run_agent(worktree: Path, prompt: str, providers: list[str], timeout: int) -> AgentOutcome:
    """Codex (workspace-write sandbox), else Claude (acceptEdits + allow-list). Subscriptions only."""
    from research_autopilot.implementation.backends.base import (
        classify_error_text,
        run_process,
        sanitized_env,
        tail,
    )

    log_dir = worktree.parent / f"{worktree.name}-logs"
    errors = []
    for p in providers:
        try:
            if p == "codex":
                from research_autopilot.implementation.backends.codex import (
                    parse_events,
                )
                exe = os.environ.get("RA_LLM_SUBSCRIPTION_CODEX", "").strip() or "codex"
                if not shutil.which(exe):
                    errors.append("codex: not installed")
                    continue
                model = os.environ.get("RA_WATCH_FIX_CODEX_MODEL", "").strip() or "gpt-5.6-terra"
                cmd = [exe, "exec", "--json", "--ephemeral", "--ignore-user-config", "-C", str(worktree),
                       "-s", "workspace-write", "-m", model, "-c", 'model_reasoning_effort="medium"', "-"]
                out = run_process(cmd, worktree, prompt, timeout, log_dir / "codex.log", lambda pid: None,
                                  env=sanitized_env())
                usage, message, errs = parse_events(out.stdout)
                if out.exit_code == 0 and not errs and not out.timed_out:
                    return AgentOutcome(True, f"codex:{model}", message, usage.model_dump())
                text = " ".join(errs) + " " + tail(out.stderr, 1000)
                errors.append(f"codex: {classify_error_text(text) or ('timeout' if out.timed_out else 'failed')}")
                if out.timed_out:
                    break  # the worktree may be half-edited; do not let a second agent start on it
            else:
                from research_autopilot.implementation.backends.claude import (
                    ALLOWED_TOOLS,
                    DISALLOWED_TOOLS,
                    ClaudeBackend,
                )
                exe = os.environ.get("RA_LLM_SUBSCRIPTION_CLAUDE", "").strip() or "claude"
                if not shutil.which(exe):
                    errors.append("claude: not installed")
                    continue
                if ClaudeBackend(exe).billing() != "subscription":
                    errors.append("claude: login is not a subscription")
                    continue
                model = os.environ.get("RA_WATCH_FIX_CLAUDE_MODEL", "").strip() or "claude-opus-5-5"
                cmd = [exe, "-p", "--output-format", "json", "--model", model, "--no-session-persistence",
                       "--strict-mcp-config", "--permission-mode", "acceptEdits", "--allowedTools", ALLOWED_TOOLS,
                       "--disallowedTools", DISALLOWED_TOOLS]
                out = run_process(cmd, worktree, prompt, timeout, log_dir / "claude.log", lambda pid: None,
                                  env=sanitized_env())
                try:
                    r = json.loads(out.stdout)
                except ValueError:
                    r = {}
                u = r.get("usage") or {}
                usage = {"input_tokens": int(u.get("input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0)),
                         "cached_input_tokens": int(u.get("cache_read_input_tokens", 0)),
                         "output_tokens": int(u.get("output_tokens", 0))}
                if out.exit_code == 0 and r and not r.get("is_error") and not out.timed_out:
                    return AgentOutcome(True, f"claude:{model}", str(r.get("result", "")), usage)
                text = f"{r.get('result', '')} {r.get('subtype', '')} {tail(out.stderr, 1000)}"
                errors.append(f"claude: {classify_error_text(text) or ('timeout' if out.timed_out else 'failed')}")
                if out.timed_out:
                    break
        except OSError as exc:
            errors.append(f"{p}: {exc}")
    return AgentOutcome(False, "", "", {}, "; ".join(errors) or "no provider configured")


# ---------------------------------------------------------------- one fix

@dataclass
class FixJob:
    fix_id: str
    thread_id: str
    instruction: str
    context: str
    repo: Path
    worktree: Path
    branch: str
    base: str
    target_branch: str
    continued: bool = False


def _tests(s: WatchSettings, worktree: Path) -> tuple[bool, str]:
    from research_autopilot.implementation.backends.base import sanitized_env
    env = sanitized_env()
    env.pop("VIRTUAL_ENV", None)  # let uv use the worktree's own environment
    try:
        p = subprocess.run(shlex.split(s.fix_test_command), check=False, cwd=worktree, capture_output=True, text=True,
                           timeout=s.fix_test_timeout, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {s.fix_test_timeout}s"
    except OSError as exc:
        return False, str(exc)
    tail_lines = [ln for ln in (p.stdout + "\n" + p.stderr).strip().splitlines() if ln.strip()][-6:]
    return p.returncode == 0, "\n".join(tail_lines)


def prepare_worktree(job: FixJob) -> None:
    if job.continued and (job.worktree / ".git").exists():
        return
    job.worktree.parent.mkdir(parents=True, exist_ok=True)
    git(job.repo, "worktree", "add", "-b", job.branch, str(job.worktree), job.base)


def execute_fix(s: WatchSettings, job: FixJob, rest: DiscordREST,
                agent: Callable[[Path, str, list[str], int], AgentOutcome] = run_agent) -> dict:
    """Runs in the worker thread: no database access here, only git / the agent / Discord."""
    result: dict = {"state": "failed"}
    try:
        prepare_worktree(job)
        rest.post(job.thread_id, f"🛠️ 作業を始めました（ブランチ `{job.branch}`、起点 `{job.target_branch}` "
                  f"{job.base[:10]}）。")
        out = agent(job.worktree, FIX_PROMPT.format(instruction=job.instruction, context=job.context or "（なし）"),
                    s.fix_providers, s.fix_timeout)
        result.update(provider=out.provider, usage=out.usage)
        if not out.ok:
            rest.post(job.thread_id, f"❌ エージェントを実行できませんでした: {out.error}")
            result.update(state="failed", error=out.error)
            return result
        changed = git(job.worktree, "status", "--porcelain").strip()
        tokens = (f"入力 {out.usage.get('input_tokens', 0):,} / 出力 {out.usage.get('output_tokens', 0):,} tokens"
                  if out.usage else "")
        if not changed:
            rest.post(job.thread_id, f"ℹ️ 変更はありませんでした（{out.provider}、{tokens}）。\n{out.summary[:1500]}")
            result.update(state="no_change", summary=out.summary[:3000])
            return result
        passed, test_tail = _tests(s, job.worktree)
        git(job.worktree, "add", "-A")
        first = job.instruction.strip().splitlines()[0][:60] if job.instruction.strip() else "fix"
        msg = (f"autofix: {first}\n\nRequested by an authorized user in the Discord report channel (fix {job.fix_id}, "
               f"{out.provider}). Tests: {'passed' if passed else 'FAILED'} ({s.fix_test_command}).")
        git(job.worktree, "commit", "-q", "-m", msg)
        commit = git(job.worktree, "rev-parse", "HEAD").strip()
        stat = git(job.worktree, "diff", "--stat", f"{job.base}..{commit}").strip()
        verdict = "✅ テスト合格" if passed else "❌ テスト失敗（適用できません）"
        rest.post(job.thread_id,
                  f"{verdict} — commit `{commit[:10]}`（{out.provider}、{tokens}）\n"
                  f"**変更内容**\n{out.summary[:1200]}\n```\n{stat[-600:]}\n```\n```\n{test_tail[-500:]}\n```\n"
                  + ("このスレッドで「適用」とメンションすると作業ブランチに取り込みます。続けて指示すると同じブランチで直します。"
                     if passed else "続けて指示すると同じブランチで直します。不要なら「破棄」。"))
        result.update(state="committed", commit=commit, tests_passed=passed, summary=out.summary[:3000])
    except (FixError, OSError, subprocess.SubprocessError) as exc:
        result.update(state="failed", error=str(exc)[:500])
        try:
            rest.post(job.thread_id, f"❌ 失敗しました: {str(exc)[:500]}")
        except Exception:  # noqa: BLE001 -- reporting the failure must not mask it
            log.warning("watchdog could not report fix %s failure", job.fix_id)
    return result


def apply_fix(s: WatchSettings, fix: dict) -> str:
    """Bring a committed, tested fix into the working branch checkout. Returns a message for Discord."""
    if fix.get("state") != "committed" or not fix.get("tests_passed"):
        return "テストに合格したコミットがないため適用できません。"
    repo, target = Path(fix["repo"]), fix["target_branch"]
    if target in _PROTECTED_BRANCHES:
        return f"`{target}` には適用しません（main への反映はユーザーの指示で人が行います）。"
    current = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if current != target:
        return f"作業ツリーが `{current}` にあり、修正の起点 `{target}` と違うため適用しません。"
    if git(repo, "status", "--porcelain", "--untracked-files=no").strip():
        return "作業ツリーに未 commit の変更があるため適用しません。"
    before = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "merge", "--ff-only", "-q", fix["branch"], check=False)
    if git(repo, "rev-parse", "HEAD").strip() == fix["commit"]:
        how = "fast-forward"
    else:
        p = subprocess.run(["git", "cherry-pick", f"{fix['base']}..{fix['branch']}"], check=False, cwd=repo,
                           capture_output=True,
                           text=True, timeout=300, stdin=subprocess.DEVNULL)
        if p.returncode != 0:
            git(repo, "cherry-pick", "--abort", check=False)
            return f"`{target}` が先に進んでいて、取り込みが衝突しました（中止済み、変更なし）: {p.stderr.strip()[:300]}"
        how = "cherry-pick"
    after = git(repo, "rev-parse", "HEAD").strip()
    return (f"✅ `{target}` に適用しました（{how}、{before[:10]} → {after[:10]}）。push はしていません。\n"
            "poll ループは次の tick から新しいコードを使います。Discord gateway と watchdog 自身は再起動するまで古いコードです。"
            "研究室サーバー（本番）には反映されません。")


def discard_fix(fix: dict) -> str:
    repo = Path(fix["repo"])
    git(repo, "worktree", "remove", "--force", fix["worktree"], check=False)
    git(repo, "branch", "-D", fix["branch"], check=False)
    return f"🗑️ `{fix['branch']}` と作業ツリーを削除しました。"


# ---------------------------------------------------------------- mentions

def _fixes_in_thread(store: Store, thread_id: str) -> list[dict]:
    return [f for f in store.find(db.watchdog_fixes) if f.get("thread_id") == thread_id]


def _new_job(store: Store, s: WatchSettings, thread_id: str, instruction: str) -> dict:
    prior = [f for f in _fixes_in_thread(store, thread_id) if f.get("state") in ("committed", "no_change")]
    fix_id = new_id()[:8]
    context = []
    if alert := alert_for_thread(store, thread_id):
        a = alert["alert"]
        context.append(f"アラート: {a['title']}\n{a['detail']}")
        if alert.get("triage"):
            context.append("監視の見立て: " + json.dumps(alert["triage"], ensure_ascii=False))
    if prior:
        last = prior[-1]
        context.append(f"このスレッドの前回の修正（ブランチ {last['branch']}）: 依頼「{last['instruction'][:300]}」\n"
                       f"結果: {(last.get('summary') or '')[:800]}")
        rec = {k: last[k] for k in ("repo", "worktree", "branch", "base", "target_branch")}
        rec["continued"] = True
    else:
        repo = s.fix_repo
        target = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        stamp = utcnow().strftime("%Y%m%d-%H%M")
        rec = {"repo": str(repo), "worktree": str(s.fix_dir / f"{stamp}-{fix_id}"), "branch": f"autofix/{stamp}-{fix_id}",
               "base": git(repo, "rev-parse", "HEAD").strip(), "target_branch": target, "continued": False}
    rec.update(fix_id=fix_id, thread_id=thread_id, instruction=instruction, context="\n\n".join(context),
               state="queued", requested_at=utcnow().isoformat())
    store.put(db.watchdog_fixes, fix_id, rec)
    return rec


def job_from(rec: dict) -> FixJob:
    return FixJob(fix_id=rec["fix_id"], thread_id=rec["thread_id"], instruction=rec["instruction"],
                  context=rec.get("context", ""), repo=Path(rec["repo"]), worktree=Path(rec["worktree"]),
                  branch=rec["branch"], base=rec["base"], target_branch=rec["target_branch"],
                  continued=bool(rec.get("continued")))


def handle_message(store: Store, s: WatchSettings, rest: DiscordREST, channel_id: str, msg: dict) -> str | None:
    """React to one mention. Returns the queued fix id, if a fix was requested."""
    text = _MENTION.sub("", msg.get("content", "")).strip()
    if str(msg["author"]["id"]) not in s.authorized_user_ids:
        rest.post(channel_id, "この操作は認可されたユーザーだけが行えます。")
        return None
    thread_id = channel_id
    if channel_id == s.report_channel_id:  # a top-level request gets its own thread
        thread_id = rest.start_thread(channel_id, msg["id"], f"fix: {text[:60] or '指示'}")
        _state_put(store, f"cursor:{thread_id}", msg["id"])
    fixes = _fixes_in_thread(store, thread_id)
    head = text.split()[0] if text else ""
    if head in ("状態", "status"):
        rest.post(thread_id, status_text(store, s))
        return None
    if head in ("適用", "apply", "破棄", "discard"):
        wanted = ("committed",) if head in ("適用", "apply") else ("committed", "no_change", "failed", "interrupted")
        done = [f for f in fixes if f.get("state") in wanted]
        if not done:
            rest.post(thread_id, "このスレッドには対象の修正がありません。")
            return None
        fix = done[-1]
        try:
            reply = apply_fix(s, fix) if head in ("適用", "apply") else discard_fix(fix)
        except FixError as exc:
            reply = f"❌ {exc}"
        if reply.startswith(("✅", "🗑️")):
            fix["state"] = "applied" if head in ("適用", "apply") else "discarded"
            store.put(db.watchdog_fixes, fix["fix_id"], fix)
        rest.post(thread_id, reply)
        return None
    if not s.fix:
        rest.post(thread_id, "修正の受け付けは無効です（RA_WATCH_FIX=false）。")
        return None
    if not text:
        rest.post(thread_id, "指示が空です。例: `@bot この停止の原因を直して` / `状態` / `適用` / `破棄`")
        return None
    if any(f.get("state") in ("queued", "running") for f in fixes):
        rest.post(thread_id, "このスレッドの修正がまだ実行中です。終わってから続けて指示してください。")
        return None
    rec = _new_job(store, s, thread_id, text)
    rest.post(thread_id, f"📝 受け付けました（fix {rec['fix_id']}）。順番に実行します。")
    return rec["fix_id"]


def mention_tick(store: Store, s: WatchSettings, rest: DiscordREST) -> list[str]:
    """Read new messages of the report channel and the followed threads; returns queued fix ids."""
    me, queued = rest.me(), []
    for channel in [s.report_channel_id, *recent_threads(store, utcnow())]:
        cursor = _state_get(store, f"cursor:{channel}")
        try:
            msgs = rest.messages(channel, after=cursor) if cursor else rest.messages(channel, limit=1)
        except Exception as exc:  # noqa: BLE001 -- an archived / deleted thread must not stop the others
            log.warning("watchdog could not read %s: %s", channel, exc)
            continue
        if cursor is None:  # first look at this channel: start after its newest message, never act on history
            _state_put(store, f"cursor:{channel}", msgs[-1]["id"] if msgs else "0")
            continue
        for m in msgs:
            _state_put(store, f"cursor:{channel}", m["id"])
            if m["author"].get("bot") or me not in {str(u["id"]) for u in m.get("mentions", [])}:
                continue
            try:
                if fix_id := handle_message(store, s, rest, channel, m):
                    queued.append(fix_id)
            except Exception as exc:
                log.exception("watchdog could not handle a mention")
                rest.post(channel, f"❌ 指示を処理できませんでした: {str(exc)[:300]}")
    return queued


def run_watch(store: Store, s: WatchSettings, rest: DiscordREST, *, once: bool = False, interval: int = 300,
              mention_interval: int = 30, dry_run: bool = False, triage=None, agent=run_agent,
              sleep: Callable[[float], None] | None = None) -> dict:
    """The resident loop: mentions every ``mention_interval`` s, detection every ``interval`` s, one fix at a time
    in a worker thread (which does no database access). ``once`` runs one round and waits for a started fix."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    from research_autopilot.autopilot.watchdog import detection_tick

    store.init_schema()
    if dry_run:
        return detection_tick(store, s, rest, None, dry_run=True)
    mark_interrupted(store, rest)
    pool, running, last_detect, summary = ThreadPoolExecutor(max_workers=1), None, None, {}
    sleep = sleep or time.sleep
    try:
        while True:
            try:
                summary["queued"] = mention_tick(store, s, rest)
            except Exception:
                log.exception("watchdog mention tick failed")
            if last_detect is None or time.monotonic() - last_detect >= interval:
                try:
                    summary["detection"] = detection_tick(store, s, rest, triage)
                except Exception:
                    log.exception("watchdog detection tick failed")
                last_detect = time.monotonic()
            if running is not None and running[1].done():
                rec, fut = running
                exc = fut.exception()
                rec.update(fut.result() if exc is None else {"state": "failed", "error": str(exc)[:500]})
                rec["finished_at"] = utcnow().isoformat()
                store.put(db.watchdog_fixes, rec["fix_id"], rec)
                summary.setdefault("fixes", []).append({"fix_id": rec["fix_id"], "state": rec["state"]})
                running = None
            if running is None and (rec := next_queued(store)) is not None:
                rec["state"], rec["started_at"] = "running", utcnow().isoformat()
                store.put(db.watchdog_fixes, rec["fix_id"], rec)
                running = (rec, pool.submit(execute_fix, s, job_from(rec), rest, agent))
            if once and running is None:
                return summary
            sleep(1 if once else mention_interval)
    finally:
        pool.shutdown(wait=False)


def next_queued(store: Store) -> dict | None:
    for f in store.find(db.watchdog_fixes):
        if f.get("state") == "queued":
            return f
    return None


def mark_interrupted(store: Store, rest: DiscordREST) -> None:
    """At start: a fix that was running when the watchdog stopped is not resumed silently."""
    for f in store.find(db.watchdog_fixes):
        if f.get("state") == "running":
            f["state"] = "interrupted"
            store.put(db.watchdog_fixes, f["fix_id"], f)
            try:
                rest.post(f["thread_id"], f"⚠️ watchdog の再起動で fix {f['fix_id']} が中断されました。作業ツリー "
                          f"`{f['worktree']}` は残っています。必要ならもう一度指示してください。")
            except Exception:  # noqa: BLE001
                log.warning("watchdog could not report interrupted fix %s", f["fix_id"])


__all__ = [
    "FIX_PROMPT",
    "AgentOutcome",
    "FixJob",
    "apply_fix",
    "discard_fix",
    "execute_fix",
    "handle_message",
    "job_from",
    "mark_interrupted",
    "mention_tick",
    "next_queued",
    "run_agent",
    "run_watch",
]
