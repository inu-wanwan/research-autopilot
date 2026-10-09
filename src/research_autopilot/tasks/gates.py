"""Deterministic pre-planning gates (docs/phase_specs/phase2.md §3, §4.2).

No LLM is involved: every reason here is derived from Linear / Notion data
and settings, so the same inputs always give the same decision.
"""

from research_autopilot.config import Settings
from research_autopilot.models import (
    BlockedCode,
    BlockedReason,
    DecisionStatus,
    HumanApproval,
    LinearIssue,
    NotionTask,
    TaskSpec,
)

_SECTION_LABELS = {
    "purpose": "Purpose",
    "background": "Background",
    "scientific_rationale": "Scientific rationale",
    "specification": "Implementation / experiment specification",
    "acceptance_criteria": "Acceptance criteria",
    "dependencies": "Dependencies",
    "scientific_invariants": "Scientific invariants",
    "artifact_requirements": "Artifact requirements",
}


def human_approval(issue: LinearIssue, page: NotionTask | None, settings: Settings) -> HumanApproval:
    pending = page.pending if page else None
    return HumanApproval(
        linear_state=issue.state_name,
        linear_state_type=issue.state_type,
        notion_pending=pending,
        approved=issue.state_type == settings.approved_state_type and pending is False,
    )


def status_gates(issue: LinearIssue, page: NotionTask | None, settings: Settings) -> list[BlockedReason]:
    """Scope, approval, status and dependency gates. Re-run right before any
    Linear write to catch changes made while planning."""
    reasons: list[BlockedReason] = []
    if issue.project_id != settings.linear_project_id:
        reasons.append(BlockedReason(code=BlockedCode.OUT_OF_SCOPE,
                                     message="issue is not in the configured Linear project",
                                     details={"project_id": issue.project_id}))
    if settings.scope_label not in issue.labels:
        reasons.append(BlockedReason(code=BlockedCode.OUT_OF_SCOPE,
                                     message=f"issue lacks the scope label {settings.scope_label!r}",
                                     details={"labels": issue.labels}))

    st = issue.state_type
    if st in ("backlog", "triage"):
        reasons.append(BlockedReason(code=BlockedCode.NOT_APPROVED,
                                     message=f"Linear state is {issue.state_name} (not approved for start)",
                                     details={"state": issue.state_name}))
    elif st == "started":
        reasons.append(BlockedReason(code=BlockedCode.INVALID_STATUS,
                                     message=f"task is already {issue.state_name}",
                                     details={"state": issue.state_name}))
    elif st in ("completed", "canceled"):
        reasons.append(BlockedReason(code=BlockedCode.INVALID_STATUS,
                                     message=f"task is {issue.state_name}",
                                     details={"state": issue.state_name}))
    elif st != settings.approved_state_type:
        reasons.append(BlockedReason(code=BlockedCode.INVALID_STATUS,
                                     message=f"unexpected Linear state type {st}",
                                     details={"state": issue.state_name}))

    if page is not None and page.pending:
        reasons.append(BlockedReason(code=BlockedCode.NOT_APPROVED,
                                     message="Notion Pending is checked (awaiting human approval)"))

    open_blockers = [b for b in issue.blocked_by if b.state_type != "completed"]
    if open_blockers:
        reasons.append(BlockedReason(
            code=BlockedCode.DEPENDENCY_INCOMPLETE,
            message="blocked by unfinished Linear issues: " + ", ".join(b.identifier for b in open_blockers),
            details={"blocked_by": [b.model_dump() for b in open_blockers]},
        ))
    return reasons


def spec_gates(spec: TaskSpec, settings: Settings) -> list[BlockedReason]:
    missing = [s for s in settings.required_sections if spec.section(s) is None]
    if not missing:
        return []
    return [BlockedReason(
        code=BlockedCode.MISSING_SPEC,
        message="Notion spec is missing required sections: " + ", ".join(_SECTION_LABELS[m] for m in missing),
        details={"missing_sections": missing},
    )]


def classify(reasons: list[BlockedReason]) -> DecisionStatus | None:
    """None means all gates passed."""
    if not reasons:
        return None
    if any(r.code == BlockedCode.TRANSIENT_ERROR for r in reasons):
        return DecisionStatus.TRANSIENT_FAILURE
    if all(r.code == BlockedCode.MISSING_SPEC for r in reasons):
        return DecisionStatus.INSUFFICIENT_SPEC
    return DecisionStatus.BLOCKED
