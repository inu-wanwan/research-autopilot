"""Prefect flows for Phase 5.

- ``autopilot-poll``: scheduler tick (deployment schedule every 5 minutes, created inactive) / manual trigger.
- ``approval-confirm``: authenticated Human-in-the-loop form. It *suspends* (releasing the worker) until a
  human submits the form in the Prefect UI; on resume it records the submission, which the poller verifies
  against the Linear status, spec hash and request.
"""

from typing import Literal

from prefect import flow, get_run_logger
from prefect.runtime import flow_run
from prefect.artifacts import create_markdown_artifact
from prefect.flow_runs import suspend_flow_run
from prefect.input import RunInput

from research_autopilot.autopilot.approvals import record_submission
from research_autopilot.autopilot.models import FormSubmission


class ApprovalForm(RunInput):
    decision: Literal["approve", "reject", "revise"]
    approver: str
    comment: str = ""
    request_id: str  # prefilled; must match the request
    spec_hash: str  # prefilled; must match the current spec


@flow(name="approval-confirm", flow_run_name=lambda: "approval-" + __import__("prefect").runtime.flow_run.parameters["request_id"][:8])
def approval_confirm_flow(request_id: str, spec_hash: str, title: str, details_markdown: str, timeout_seconds: int = 7 * 86400) -> dict:
    form = ApprovalForm.with_initial_data(
        description=f"## {title}\n\n{details_markdown}\n\nAlso set the Linear issue status to **Approved** / "
                    "**Revision Requested** / **Canceled** to match your decision. Do not edit request_id / spec_hash.",
        request_id=request_id, spec_hash=spec_hash, decision="approve", approver="")
    result = suspend_flow_run(wait_for_input=form, timeout=timeout_seconds)
    from research_autopilot.autopilot.factory import store_from_env

    store = store_from_env()
    sub = FormSubmission(request_id=result.request_id, decision=result.decision, spec_hash=result.spec_hash,
                         approver=result.approver or "(prefect basic-auth user)", comment=result.comment)
    record_submission(store, sub)
    get_run_logger().info("recorded %s for %s by %s", sub.decision, sub.request_id, sub.approver)
    return sub.model_dump(mode="json")


@flow(name="autopilot-poll")
def autopilot_poll_flow(task_id: str | None = None, dry_run: bool = False) -> dict:
    from research_autopilot.autopilot.engine import poll, status_report
    from research_autopilot.autopilot.factory import build_autopilot_deps

    deps = build_autopilot_deps(dry_run=dry_run)
    summary = poll(deps, only_task=task_id, prefect_flow_run_id=str(flow_run.id))
    report = status_report(deps)
    lines = ["# Autopilot status", *[f"- `{r['task']}` **{r['stage']}** {r['blocked'] or ''} {r['waiting_for'] or ''}"
                                     for r in report["runs"]],
             "## Pending approvals", *[f"- {a['task']} {a['kind']} `{a['id'][:8]}`" for a in report["pending_approvals"]],
             "## Outbox", *[f"- {o['key']} {o['state']} {o['error'] or ''}" for o in report["outbox"]]]
    create_markdown_artifact(key="autopilot-status", markdown="\n".join(lines), description="Autopilot workflow state")
    get_run_logger().info("poll summary: %s", summary)
    return {"summary": summary, "report": report}


class PrefectConfirmationChannel:
    """Opens the approval form as a deployment run (needs the 'approval-confirm/manual' deployment)."""

    def __init__(self, deployment: str, ui_url: str | None) -> None:
        self.deployment = deployment
        self.ui_url = ui_url

    def open(self, request) -> str | None:
        from prefect.deployments import run_deployment

        details = "\n".join([*(f"- change: {x}" for x in request.what_changes), *(f"- diff: {x}" for x in request.diff_from_original),
                             f"- budget: {request.budget_summary}", f"- task: {request.task_id}"])
        fr = run_deployment(self.deployment, parameters={"request_id": request.request_id, "spec_hash": request.spec_hash,
                                                         "title": request.title, "details_markdown": details}, timeout=0)
        return str(fr.id)

    def url(self, ref: str | None) -> str | None:
        return f"{self.ui_url}/runs/flow-run/{ref}" if ref and self.ui_url else None
