"""Task Manager: load a task from Linear (status SSOT) and Notion (spec SSOT),
validate their correspondence, and pick the next candidate deterministically."""

from dataclasses import dataclass, field
from typing import Protocol

from research_autopilot.config import Settings
from research_autopilot.models import BlockedCode, BlockedReason, LinearIssue, NotionTask, TaskSpec
from research_autopilot.tasks import gates
from research_autopilot.tasks.errors import ConnectorResponseError, NotFoundError


class NotionReader(Protocol):
    def get_task(self, page_id: str) -> NotionTask: ...
    def find_page_ids_by_linear_id(self, identifier: str) -> list[str]: ...


class LinearGateway(Protocol):
    def get_issue(self, identifier: str) -> LinearIssue: ...
    def candidate_identifiers(self, label: str, state_type: str) -> list[str]: ...
    def team_state_id(self, team_id: str, name: str) -> tuple[str, str]: ...
    def set_state(self, issue_id: str, state_id: str) -> str: ...


@dataclass
class TaskBundle:
    issue: LinearIssue
    page: NotionTask | None
    spec: TaskSpec | None
    reasons: list[BlockedReason] = field(default_factory=list)


class TaskManager:
    def __init__(self, notion: NotionReader, linear: LinearGateway, settings: Settings) -> None:
        self.notion = notion
        self.linear = linear
        self.settings = settings

    def load(self, task_id: str) -> TaskBundle:
        """Fetch and cross-check one task. Connector errors other than
        not-found propagate (the orchestrator classifies them)."""
        issue = self.linear.get_issue(task_id)
        page, reasons = self._resolve_page(issue)
        spec = TaskSpec.from_sources(issue, page) if page else None
        reasons += gates.status_gates(issue, page, self.settings)
        if spec is not None:
            reasons += gates.spec_gates(spec, self.settings)
        return TaskBundle(issue=issue, page=page, spec=spec, reasons=reasons)

    def next_candidates(self) -> list[str]:
        return self.linear.candidate_identifiers(self.settings.scope_label, self.settings.approved_state_type)

    def _resolve_page(self, issue: LinearIssue) -> tuple[NotionTask | None, list[BlockedReason]]:
        by_property = self.notion.find_page_ids_by_linear_id(issue.identifier)
        linked = issue.notion_page_ids
        if len(by_property) > 1:
            return None, [_inconsistent("multiple Notion pages claim this Linear issue",
                                        notion_pages=by_property)]
        if by_property and linked and by_property[0] not in linked:
            return None, [_inconsistent("Linear links a different Notion page than the one whose "
                                        "'Linear Issue ID' points back", linked=linked, by_property=by_property)]
        page_id = by_property[0] if by_property else (linked[0] if linked else None)
        if page_id is None:
            return None, [BlockedReason(code=BlockedCode.MISSING_SPEC,
                                        message="no Notion spec page is linked to this Linear issue")]
        try:
            page = self.notion.get_task(page_id)
        except (NotFoundError, ConnectorResponseError) as exc:
            return None, [_inconsistent(f"linked Notion page is unusable: {exc}", page_id=page_id)]
        if page.linear_issue_id != issue.identifier:
            return page, [_inconsistent(
                f"Notion 'Linear Issue ID' is {page.linear_issue_id!r}, expected {issue.identifier!r}",
                page_id=page.page_id)]
        return page, []


def _inconsistent(message: str, **details) -> BlockedReason:
    return BlockedReason(code=BlockedCode.DATA_INCONSISTENCY, message=message, details=details)
