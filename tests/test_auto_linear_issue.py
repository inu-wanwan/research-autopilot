"""Run Autopilot on a Notion task without a Linear issue creates the issue first (user decision 2026-10-14)."""

from phase5_fakes import build

from research_autopilot.autopilot.engine import poll
from research_autopilot.autopilot.models import ApprovalKind


def _unlink(notion, title, linear):
    """A Notion task that no Linear issue links yet (the fixture's INU-900 is unlinked from it)."""
    pid = next(iter(notion.pages))
    p = notion.pages[pid]
    notion.pages[pid] = p.model_copy(update={"linear_issue_id": None, "title": title})
    linear.issues["INU-900"] = linear.issues["INU-900"].model_copy(update={"notion_page_ids": [], "description": ""})
    return pid


def test_a_new_issue_is_created_linked_and_the_task_starts(tmp_path):
    deps, linear, notion, writer, channel = build(tmp_path, approval_mode="discord")
    pid = _unlink(notion, "[TEST] New task without a Linear issue", linear)
    s = poll(deps)
    (ident,) = linear.created
    issue = linear.issues[ident]
    assert issue.title == "[TEST] New task without a Linear issue" and "autopilot" in issue.labels
    assert issue.state_name == "Awaiting Approval" or issue.state_type in ("unstarted", "started")
    assert writer.page_url(pid) in issue.description and notion.pages[pid].linear_issue_id == ident
    assert s["linear_created"] == [{"page": pid, "issue": ident, "reused": False}]
    assert s["discovered"] == [ident]  # the usual intake follows in the same poll
    assert [r.kind for r in deps.approvals.pending()] == [ApprovalKind.ONBOARDING]


def test_an_issue_that_already_links_the_page_is_reused(tmp_path):
    deps, linear, notion, writer, channel = build(tmp_path, approval_mode="discord")
    pid = _unlink(notion, "[TEST] Retried after a half-finished attempt", linear)
    team = linear.project_team_id()
    _, existing, _ = linear.create_issue(team, "[TEST] Retried after a half-finished attempt",
                                         f"詳細仕様（SSOT）: {writer.page_url(pid)}", "state-Todo", ["label-autopilot"])
    s = poll(deps)
    assert linear.created == [existing] and notion.pages[pid].linear_issue_id == existing  # no duplicate
    assert s["linear_created"][0]["reused"] is True


def test_test_only_policy_refuses_to_create_issues_for_non_test_titles(tmp_path):
    deps, linear, notion, writer, channel = build(tmp_path, approval_mode="discord")
    pid = _unlink(notion, "Production task", linear)
    s = poll(deps)
    assert linear.created == [] and notion.pages[pid].linear_issue_id is None
    page = notion.pages[pid]
    assert page.execution_status == "Blocked" and "Linear Issue ID" in page.execution_error and not page.run_requested
    assert "refused" in s["linear_created"][0]
