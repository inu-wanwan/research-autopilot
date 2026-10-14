"""Approved publication of finished tasks: push the task branch and open a PR only after a Discord Approve."""

import subprocess

import httpx
import pytest
from phase5_fakes import approve, build

from research_autopilot.autopilot import publish
from research_autopilot.autopilot.engine import poll
from research_autopilot.autopilot.models import ApprovalKind, DiscordSubmission, WorkflowRun, WorkflowStage
from research_autopilot.autopilot.publish import GitHubPublisher, PublishError
from research_autopilot.storage import db

TOKEN = "github_pat_should_never_leak_0000000000"


class FakePublisher:
    def __init__(self, fail: list[PublishError] = ()):
        self.fail = list(fail)
        self.pushes: list[tuple] = []
        self.prs: list[dict] = []

    def push(self, repo_path, owner, name, commit, branch):
        if self.fail:
            raise self.fail.pop(0)
        self.pushes.append((str(repo_path), owner, name, commit, branch))

    def open_pull_request(self, owner, name, *, head, base, title, body):
        self.prs.append({"owner": owner, "name": name, "head": head, "base": base, "title": title, "body": body})
        return f"https://github.com/{owner}/{name}/pull/7"


def _deps(tmp_path, *, publish_repo=True, publisher=None):
    deps, linear, notion, writer, channel = build(tmp_path, drafts=[])
    repo = deps.impl.registry["h2-test"]
    deps.impl.registry["h2-test"] = repo.model_copy(update={"publish": publish_repo,
                                                            "url": "git@github.com:lab/h2-test.git"})
    deps.publisher = publisher if publisher is not None else FakePublisher()
    return deps, linear


def _done(deps, linear) -> WorkflowRun:
    poll(deps)
    (req,) = deps.approvals.pending()
    approve(deps, linear, req.request_id)
    poll(deps)
    run = _run(deps)
    assert run.stage is WorkflowStage.DONE, (run.blocked_reason, run.events[-3:])
    return run


def _run(deps) -> WorkflowRun:
    (raw,) = deps.store.find(db.workflow_runs, task_id="INU-900")
    return WorkflowRun.model_validate(raw)


def _publish_requests(deps):
    return [r for r in deps.approvals.pending() if r.kind is ApprovalKind.PUBLISH]


def _discord(deps, req, decision="approve"):
    sub = DiscordSubmission(interaction_id=f"i-{req.request_id}-{decision}", request_id=req.request_id,
                            decision=decision, spec_hash=req.spec_hash, user_id="1001", guild_id="1", channel_id="2")
    deps.store.insert_new(db.discord_interactions, sub.interaction_id, sub.model_dump(mode="json"),
                          request_id=req.request_id)


def test_done_task_asks_and_pushes_only_the_task_branch_after_a_discord_approve(tmp_path):
    deps, linear = _deps(tmp_path)
    run = _done(deps, linear)
    (req,) = _publish_requests(deps)
    assert req.payload["branch"] == "autopilot/inu-900" and req.payload["commit"] == run.commits[-1]
    assert req.payload["tasks"] == ["INU-900"] and req.payload["not_included"] == []
    assert run.publications[0]["state"] == "requested" and linear.issues["INU-900"].state_name == "Done"
    poll(deps)
    assert deps.publisher.pushes == []  # nothing is pushed without a decision
    _discord(deps, req)
    poll(deps)
    (push,) = deps.publisher.pushes
    assert push[1:] == ("lab", "h2-test", run.commits[-1], "autopilot/inu-900")
    (pr,) = deps.publisher.prs
    assert pr["head"] == req.payload["branch"] and pr["base"] == "main" and "INU-900" in pr["title"]
    run = _run(deps)
    assert run.publications[0]["state"] == "published" and run.publications[0]["pr_url"].endswith("/pull/7")
    poll(deps)
    assert len(deps.publisher.pushes) == 1  # idempotent: published once


def test_reject_never_pushes(tmp_path):
    deps, linear = _deps(tmp_path)
    _done(deps, linear)
    (req,) = _publish_requests(deps)
    _discord(deps, req, "reject")
    poll(deps)
    poll(deps)
    assert deps.publisher.pushes == [] and _run(deps).publications[0]["state"] == "rejected"


@pytest.mark.parametrize("publish_repo, with_publisher", [(False, True), (True, False)])
def test_no_request_unless_the_repository_opts_in_and_a_token_is_configured(tmp_path, publish_repo, with_publisher):
    deps, linear = _deps(tmp_path, publish_repo=publish_repo)
    if not with_publisher:
        deps.publisher = None
    run = _done(deps, linear)
    assert _publish_requests(deps) == [] and run.publications == []


def test_request_goes_stale_when_the_repository_stops_publishing(tmp_path):
    deps, linear = _deps(tmp_path)
    _done(deps, linear)
    (req,) = _publish_requests(deps)
    repo = deps.impl.registry["h2-test"]
    deps.impl.registry["h2-test"] = repo.model_copy(update={"publish": False})
    _discord(deps, req)
    poll(deps)
    assert deps.publisher.pushes == [] and _run(deps).publications[0]["state"] == "stale"


def test_transient_failure_is_retried_and_a_rejected_push_is_never_forced(tmp_path):
    deps, linear = _deps(tmp_path, publisher=FakePublisher(fail=[PublishError("network down")]))
    _done(deps, linear)
    (req,) = _publish_requests(deps)
    _discord(deps, req)
    poll(deps)
    assert _run(deps).publications[0]["state"] == "retrying"
    poll(deps)
    assert _run(deps).publications[0]["state"] == "published"

    deps2, linear2 = _deps(tmp_path / "b", publisher=FakePublisher(
        fail=[PublishError("non-fast-forward", permanent=True), PublishError("x")]))
    _done(deps2, linear2)
    (req2,) = _publish_requests(deps2)
    _discord(deps2, req2)
    poll(deps2)
    poll(deps2)
    run = _run(deps2)
    assert run.publications[0]["state"] == "failed" and deps2.publisher.pushes == []
    assert len(deps2.publisher.fail) == 1  # not attempted again after the permanent failure
    assert publish.retry(deps2, run) and run.publications[0]["state"] == "retrying"


def test_children_and_follow_ups_never_ask_only_the_submitted_task_does(tmp_path):
    deps, linear = _deps(tmp_path)
    run = _done(deps, linear)
    for update in ({"research_cycle_id": "c1"}, {"parent_task_id": "INU-1"}, {"chain_depth": 1}):
        child = run.model_copy(update={**update, "publications": []})
        assert publish.request(deps, child) == [] and child.publications == []


def test_a_research_objective_asks_once_for_its_last_validated_implementation(tmp_path, monkeypatch):
    """Phase 10: one PR per research objective (its cycle tasks build on each other), none per cycle task."""
    monkeypatch.setenv("RA_RESEARCH_DIR", str(tmp_path / "research"))
    from research_fakes import RESEARCH_SECTIONS, ScriptedResearchLlm, complete, more

    from research_autopilot.research.workflow import ResearchDeps
    deps, linear, notion, writer, channel = build(tmp_path, approval_mode="discord", drafts=[],
                                                  sections=dict(RESEARCH_SECTIONS))
    llm = ScriptedResearchLlm(decisions=[more(), complete])
    deps.research = ResearchDeps(llm_factory=lambda: llm)
    repo = deps.impl.registry["h2-test"]
    deps.impl.registry["h2-test"] = repo.model_copy(update={"publish": True, "url": "https://github.com/lab/h2-test"})
    deps.publisher = FakePublisher()
    poll(deps)
    (scope_req,) = [r for r in deps.approvals.pending() if r.kind.value == "research_scope"]
    _discord(deps, scope_req)
    for _ in range(20):
        poll(deps)
        if _run(deps).stage is WorkflowStage.DONE:
            break
    run = _run(deps)
    assert run.stage is WorkflowStage.DONE, (run.blocked_reason, run.events[-5:])
    (req,) = _publish_requests(deps)  # one, for the objective; none for the two cycle tasks
    children = [WorkflowRun.model_validate(r) for r in deps.store.find(db.workflow_runs) if r.get("research_cycle_id")]
    assert len({c.task_id for c in children}) == 2 and all(c.publications == [] for c in children)
    last = max((m for c in children for rid in c.phase3_run_ids if (m := deps.store.get_implementation(rid))
                and m.implementation_commit), key=lambda m: m.updated_at)
    assert req.task_id == "INU-900" and req.payload["branch"] == "autopilot/inu-900"
    assert req.payload["commit"] == last.implementation_commit and req.payload["not_included"] == []
    assert sorted(req.payload["tasks"]) == sorted({c.task_id for c in children})
    _discord(deps, req)
    poll(deps)
    (push,) = deps.publisher.pushes
    assert push[3:] == (last.implementation_commit, "autopilot/inu-900")
    assert "Tasks:" in deps.publisher.prs[0]["body"]


# ---------------------------------------------------------------- GitHubPublisher


def test_token_never_reaches_argv_and_only_autopilot_branches_are_pushed(tmp_path):
    seen = {}

    def run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw["env"]
        return subprocess.CompletedProcess(argv, 0, "", "")
    pub = GitHubPublisher(TOKEN, run=run)
    pub.push(tmp_path, "lab", "h2-test", "a" * 40, "autopilot/inu-900-abcd1234")
    assert TOKEN not in " ".join(seen["argv"]) and seen["argv"][-1] == f"{'a' * 40}:refs/heads/autopilot/inu-900-abcd1234"
    assert seen["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    with pytest.raises(PublishError) as e:
        pub.push(tmp_path, "lab", "h2-test", "a" * 40, "main")
    assert e.value.permanent


def test_push_errors_are_redacted_and_rejections_are_permanent(tmp_path):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", f" ! [rejected] (non-fast-forward) token={TOKEN}")
    with pytest.raises(PublishError) as e:
        GitHubPublisher(TOKEN, run=run).push(tmp_path, "lab", "r", "a" * 40, "autopilot/x")
    assert e.value.permanent and TOKEN not in str(e.value)


def test_existing_pull_request_is_reused(tmp_path):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == f"Bearer {TOKEN}"
        if req.method == "POST":
            return httpx.Response(422, json={"message": "Validation Failed",
                                             "errors": [{"message": "A pull request already exists for lab:autopilot/x."}]})
        assert req.url.params["head"] == "lab:autopilot/x"
        return httpx.Response(200, json=[{"html_url": "https://github.com/lab/r/pull/3"}])
    pub = GitHubPublisher(TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert pub.open_pull_request("lab", "r", head="autopilot/x", base="main", title="t", body="b").endswith("/pull/3")
