"""Approved publication of a finished task's implementation: push the task branch and open a pull request.

User decisions (2026-10-14): when a task the user submitted is finished (a plain task when Done, a Phase 10 research
objective when the research completes), ask on Discord whether to publish; on Approve, push the result as the
``autopilot/<task>`` branch to GitHub and open a pull request into the base branch.  A human reviews and
merges the PR on GitHub -- nothing here pushes to the base branch, force-pushes or merges.

* Opt-in per repository (``publish = true`` in config/research_repos.toml) and only with ``RA_GITHUB_PUSH_TOKEN``.
* The approval covers exactly (repository, branch, commit, base); the commit is pushed as ``<commit>:<branch>``.
* Push / PR creation run in the poller after the decision and are retried on transient failures; a rejected push
  (the remote branch moved) is never forced.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import httpx

from research_autopilot.autopilot.models import ApprovalKind, ApprovalRequest, ApprovalStatus, WorkflowRun
from research_autopilot.models import utcnow
from research_autopilot.notifications import notify_once
from research_autopilot.notifications.events import EventKind, NotificationEvent
from research_autopilot.storage import db

MAX_ATTEMPTS = 5
BRANCH_PREFIX = "autopilot/"
GITHUB_URL = re.compile(r"github\.com[:/](?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


class PublishError(RuntimeError):
    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class _NoIssue:
    """Publication approvals are decided on Discord, not by a Linear state."""

    state_name = "n/a"


def github_repo(url: str | None) -> tuple[str, str] | None:
    m = GITHUB_URL.search(url or "")
    return (m["owner"], m["name"]) if m else None


def _hash(p: dict) -> str:
    keys = ("repo_id", "owner", "name", "branch", "commit", "base_branch")
    return hashlib.sha256(json.dumps({k: p[k] for k in keys}, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------- request (at completion of a submitted task)


def request(deps, run: WorkflowRun, *, research: bool = False) -> list[ApprovalRequest]:
    """Ask whether to publish what a task the user submitted produced (one PR per submitted task and repository).

    User decision (2026-10-14): the PR unit is the task the user submits.  Children (Phase 10 cycle tasks, Phase 6B
    DAG children) and automatic follow-ups never ask; a Phase 10 research objective asks once when the research
    completes, with the last validated implementation of its cycle tasks (they build on each other)."""
    if deps.publisher is None or run.parent_task_id or run.research_cycle_id or run.chain_depth:
        return []
    chains = _research_chains(deps, run.task_id) if research else _own_chain(deps, run)
    out = []
    for chain in chains.values():
        req = _ask(deps, run, chain)
        if req is not None:
            out.append(req)
    return out


def _own_chain(deps, run: WorkflowRun) -> dict[str, list]:
    impl = deps.store.get_implementation(run.phase3_run_ids[-1]) if run.phase3_run_ids else None
    if impl is None or impl.task is None or not impl.implementation_commit:
        return {}
    return {impl.task.repo_id: [impl]}


def _research_chains(deps, root_task_id: str) -> dict[str, list]:
    """Per repository, the cycle tasks' implementations whose experiment validated, oldest first."""
    from research_autopilot.experiments.models import ExperimentState
    from research_autopilot.implementation.models import RunState
    chains: dict[str, list] = {}
    for r in deps.store.find(db.workflow_runs):
        if r.get("root_task_id") != root_task_id or not r.get("research_cycle_id"):
            continue
        validated = {e.phase3_run_id for eid in r.get("experiment_run_ids", [])
                     if (e := deps.store.get_experiment(eid)) and e.state is ExperimentState.COMPLETED}
        for rid in r.get("phase3_run_ids", []):
            m = deps.store.get_implementation(rid) if rid in validated else None
            if m and m.state is RunState.COMPLETED and m.implementation_commit and m.task:
                chains.setdefault(m.task.repo_id, []).append(m)
    return {k: sorted(v, key=lambda m: m.updated_at) for k, v in chains.items()}


def _ask(deps, run: WorkflowRun, chain: list) -> ApprovalRequest | None:
    last = chain[-1]
    repo = deps.impl.registry.get(last.task.repo_id)
    if repo is None or not repo.publish:
        return None
    target = github_repo(repo.url)
    branch = f"{BRANCH_PREFIX}{run.task_id.lower()}"
    if target is None or branch == repo.base_branch:
        run.event("publish_skipped", f"repository {repo.id}: url is not a GitHub repository")
        return None
    commit = last.implementation_commit
    missing = [m.task_id for m in chain[:-1] if not _is_ancestor(last.task.repo_path, m.implementation_commit, commit)]
    files = sorted({f for m in chain for f in m.changed_files})
    p = {"run_id": run.run_id, "repo_id": repo.id, "repo_path": last.task.repo_path, "owner": target[0],
         "name": target[1], "branch": branch, "commit": commit, "base_branch": repo.base_branch,
         "tasks": list(dict.fromkeys(m.task_id for m in chain)), "not_included": missing,
         "changed_files": files[:50], "notes_url": _notes_url(deps, run)}
    h = _hash(p)
    req = ApprovalRequest(
        kind=ApprovalKind.PUBLISH, task_id=run.task_id, root_task_id=run.root_task_id, spec_hash=h,
        title=f"{run.task_id} の成果を {p['owner']}/{p['name']} に push して PR を作る",
        what_changes=[f"push {commit[:12]} を {p['owner']}/{p['name']} のブランチ {branch} へ（main には push しない）",
                      f"Pull Request を作成: {branch} → {p['base_branch']}（merge は GitHub で人が行う）",
                      f"含まれるタスク: {', '.join(p['tasks'])}",
                      *([f"含まれない実装（{', '.join(missing)}）: 最後の commit の祖先ではない"] if missing else [])],
        diff_from_original=[f"変更ファイル: {', '.join(files[:8]) or '-'}" + (f" ほか {len(files) - 8} 件" if len(files) > 8 else "")],
        budget_summary="-", payload={**p, "_dedupe": f"publish:{run.task_id}:{h}"})
    # Recorded like Phase 9 requests: the Linear issue stays Done (no Awaiting Approval round trip).
    if not deps.store.insert_new(db.approval_requests, req.request_id, req.model_dump(mode="json"),
                                 task_id=req.task_id, status=req.status.value, dedupe_key=req.payload["_dedupe"]):
        return None  # already asked for this exact commit
    deps.approvals.notify(req, "requested")
    run.publications.append({"request_id": req.request_id, "repo_id": repo.id, "state": "requested", "attempts": 0})
    run.event("publish_requested", f"{req.request_id[:8]}: {branch} @ {commit[:12]}")
    return req


def _is_ancestor(repo_path: str, ancestor: str, commit: str) -> bool:
    try:
        return subprocess.run(["git", "-C", repo_path, "merge-base", "--is-ancestor", ancestor, commit],
                              capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _notes_url(deps, run: WorkflowRun) -> str | None:
    try:
        return deps.notion_writer.page_url(run.notes_page_id) if run.notes_page_id else None
    except Exception:
        return None


def current_hash(deps, req: ApprovalRequest) -> str:
    """Stale when the repository stopped publishing or no longer has that GitHub URL."""
    p = req.payload
    repo = deps.impl.registry.get(p.get("repo_id"))
    if repo is None or not repo.publish or github_repo(repo.url) != (p.get("owner"), p.get("name")):
        return "unavailable"
    return _hash(p)


# ---------------------------------------------------------------- poller: decisions, then execution


def tick(deps, runs: list[WorkflowRun], save) -> list[dict]:
    """Apply Discord decisions on publish requests and run approved publications (idempotent, retried)."""
    from research_autopilot.autopilot.approvals import ApprovalController
    out: list[dict] = []
    ctl = ApprovalController(deps.store, None, None, mode="discord")  # decided on Discord whatever the task mode
    by_request = {pub["request_id"]: (r, pub) for r in runs for pub in r.publications}
    for req in ctl.pending():
        if req.kind is not ApprovalKind.PUBLISH:
            continue
        res = ctl.check(req, _NoIssue(), [], current_hash(deps, req))
        out.append({"request_id": req.request_id, "status": res.status.value, "note": res.note})
        if req.request_id not in by_request or res.status is ApprovalStatus.PENDING:
            continue
        run, pub = by_request[req.request_id]
        if res.status is ApprovalStatus.APPROVED:
            pub.update(state="approved", approved_by=res.decision.actor if res.decision else None)
            run.event("publish_approved", f"{req.request_id[:8]} by {pub['approved_by']}")
        else:
            pub.update(state=res.status.value)
            run.event("publish_" + res.status.value, f"{req.request_id[:8]}: {res.note}")
        save(run)
    if deps.publisher is None:
        return out
    for run, pub in by_request.values():
        if pub.get("state") in ("approved", "retrying"):
            out.append({"request_id": pub["request_id"], **_execute(deps, run, pub, _request(deps, pub["request_id"]))})
            save(run)
    return out


def _request(deps, request_id: str) -> ApprovalRequest:
    return ApprovalRequest.model_validate(deps.store.get(db.approval_requests, request_id))


def _execute(deps, run: WorkflowRun, pub: dict, req: ApprovalRequest) -> dict:
    p = req.payload
    pub["attempts"] = pub.get("attempts", 0) + 1
    try:
        deps.publisher.push(Path(p["repo_path"]), p["owner"], p["name"], p["commit"], p["branch"])
        pr_url = deps.publisher.open_pull_request(p["owner"], p["name"], head=p["branch"], base=p["base_branch"],
                                                  title=f"[Autopilot] {run.task_id}", body=_pr_body(run, p))
    except PublishError as exc:
        pub["last_error"] = str(exc)[:300]
        dead = exc.permanent or pub["attempts"] >= MAX_ATTEMPTS
        pub["state"] = "failed" if dead else "retrying"
        run.event("publish_failed" if dead else "publish_retry", pub["last_error"])
        if dead:
            _notify(deps, run, EventKind.UNRECOVERABLE_ERROR, f"publish-failed:{req.request_id}", [
                f"{p['owner']}/{p['name']} への push / PR 作成に失敗しました（{pub['attempts']} 回）",
                f"理由: {pub['last_error']}",
                "対応: 原因（token の権限・期限、リモートのブランチの状態）を直してから、"
                f"research-agent autopilot publish-retry {run.task_id} を実行する",
                f"手動で行う場合: git push <remote> {p['commit']}:refs/heads/{p['branch']} のあと GitHub で PR を作る"])
        return {"state": pub["state"], "error": pub["last_error"]}
    pub.update(state="published", pr_url=pr_url, published_at=utcnow().isoformat())
    run.event("published", f"{p['branch']} @ {p['commit'][:12]}: {pr_url}")
    _notify(deps, run, EventKind.TASK_COMPLETED, f"published:{req.request_id}",
            ["Pull Request を作成しました（merge は GitHub で確認してから人が行う）",
             f"ブランチ: {p['branch']} → {p['base_branch']}", f"Pull Request: {pr_url}", f"Commits: {p['commit'][:12]}"])
    return {"state": "published", "pr_url": pr_url}


def retry(deps, run: WorkflowRun) -> bool:
    """CLI: after fixing the cause, try the failed publications again (the human approval still stands)."""
    failed = [pub for pub in run.publications if pub.get("state") == "failed"]
    for pub in failed:
        pub.update(state="retrying", attempts=0)
    if failed:
        run.event("publish_retry", "requested by a human")
    return bool(failed)


def _pr_body(run: WorkflowRun, p: dict) -> str:
    lines = [f"Research Autopilot task **{run.task_id}** (approved on Discord before this push).", "",
             f"- Commit: `{p['commit']}`", f"- Base: `{p['base_branch']}`"]
    if len(p.get("tasks", [])) > 1 or p.get("tasks", [run.task_id]) != [run.task_id]:
        lines.append(f"- Tasks: {', '.join(p['tasks'])}")
    if p.get("not_included"):
        lines.append(f"- Not included (not ancestors of this commit): {', '.join(p['not_included'])}")
    if p.get("notes_url"):
        lines.append(f"- Results (Notion): {p['notes_url']}")
    if p.get("changed_files"):
        lines += ["", "Changed files:", *[f"- `{f}`" for f in p["changed_files"][:30]]]
    lines += ["", "Merging is a human decision; Autopilot never merges or pushes to the base branch."]
    return "\n".join(lines)


def _notify(deps, run: WorkflowRun, kind: EventKind, dedupe: str, details: list[str]) -> None:
    notify_once(deps.store, NotificationEvent(kind=kind, flow_name="autopilot", flow_run_name=run.task_id,
                                              state_name="publish", details=details, dedupe_key=dedupe),
                deps.notifier)


# ---------------------------------------------------------------- GitHub


class GitHubPublisher:
    """Pushes with a fine-grained token (Contents: write, Pull requests: write on the research repositories).

    The token reaches git only through ``GIT_CONFIG_*`` environment variables of the push process (never argv,
    remote URLs, .git/config or logs)."""

    api = "https://api.github.com"

    def __init__(self, token: str, *, run=subprocess.run, client: httpx.Client | None = None) -> None:
        self._token = token
        self._run = run
        self._client = client or httpx.Client(timeout=30)

    def push(self, repo_path: Path, owner: str, name: str, commit: str, branch: str) -> None:
        if not branch.startswith(BRANCH_PREFIX):
            raise PublishError(f"refusing to push non-autopilot branch {branch!r}", permanent=True)
        basic = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_")}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}")
        try:
            p = self._run(["git", "-C", str(repo_path), "push", "--porcelain",
                           f"https://github.com/{owner}/{name}.git", f"{commit}:refs/heads/{branch}"],
                          capture_output=True, text=True, timeout=300, env=env, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PublishError(f"git push could not run: {type(exc).__name__}") from exc
        if p.returncode != 0:
            err = (p.stderr or p.stdout or "").replace(self._token, "***").replace(basic, "***")[-300:]
            permanent = any(s in err for s in ("non-fast-forward", "[rejected]", "fetch first", "not found",
                                               "Permission", "403", "protected branch"))
            raise PublishError(f"git push failed ({p.returncode}): {err.strip()}", permanent=permanent)

    def open_pull_request(self, owner: str, name: str, *, head: str, base: str, title: str, body: str) -> str:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        url = f"{self.api}/repos/{owner}/{name}/pulls"
        try:
            r = self._client.post(url, headers=headers, json={"title": title, "head": head, "base": base, "body": body})
            if r.status_code == 201:
                return r.json()["html_url"]
            if r.status_code == 422 and "already exists" in r.text:
                existing = self._client.get(url, headers=headers, params={"head": f"{owner}:{head}", "state": "all"})
                if existing.status_code == 200 and existing.json():
                    return existing.json()[0]["html_url"]
        except httpx.HTTPError as exc:
            raise PublishError(f"GitHub API unreachable: {type(exc).__name__}") from exc
        raise PublishError(f"pull request creation failed: HTTP {r.status_code}", permanent=r.status_code < 500)
