"""Prefect flow ``plan-task`` (runs on the Phase 1 ``research-local`` work pool).

The decision is the flow's return value; a blocked / insufficient_spec
decision is a normal (Completed) outcome, not a flow failure. Each run
publishes a markdown artifact so history is visible in the Prefect UI.
"""

import re

from prefect import flow, get_run_logger
from prefect.artifacts import create_markdown_artifact
from prefect.runtime import flow_run as runtime_flow_run

from research_autopilot.config import load_settings
from research_autopilot.models import PlanRunRecord
from research_autopilot.notifications.hooks import notify_completed, notify_failed
from research_autopilot.orchestrator.core import Mode, plan_task
from research_autopilot.orchestrator.factory import build_deps


def _run_name() -> str:
    p = runtime_flow_run.parameters
    return f"plan-{p.get('task_id') or 'next'}-{p.get('mode', 'dry_run')}"


@flow(name="plan-task", flow_run_name=_run_name, on_completion=[notify_completed],
      on_failure=[notify_failed], on_crashed=[notify_failed])
def plan_task_flow(task_id: str | None = None, next_task: bool = False, mode: Mode = "dry_run") -> dict:
    logger = get_run_logger()
    settings = load_settings()
    deps = build_deps(settings, on_retry=logger.warning)
    record = plan_task(deps, task_id=task_id, next_task=next_task, mode=mode,
                       flow_run_id=str(runtime_flow_run.id) if runtime_flow_run.id else None)
    for ev in record.events:
        logger.info("[%s] %s", ev.kind, ev.message)
    d = record.decision
    logger.info("decision: %s for %s (%s)", d.status.value, d.task_id, d.rationale)
    for r in d.reasons:
        logger.info("  reason %s: %s", r.code.value, r.message)
    create_markdown_artifact(key=_artifact_key(record), markdown=summary_markdown(record),
                             description=f"plan-task decision for {d.task_id}")
    return d.model_dump(mode="json")


def _artifact_key(record: PlanRunRecord) -> str:
    return "plan-" + re.sub(r"[^a-z0-9-]", "-", (record.task_id or "none").lower())


def summary_markdown(record: PlanRunRecord) -> str:
    d, c = record.decision, record.cost
    lines = [
        f"# plan-task: {d.task_id} — **{d.status.value}**",
        f"- mode: `{record.mode}` / plan_run_id: `{record.plan_run_id}`",
        f"- rationale: {d.rationale}",
        f"- reviewer verdict: {d.reviewer_verdict or '-'} / plan_id: `{d.plan_id or '-'}`",
        f"- human approval: {d.human_approval.approved if d.human_approval else '-'}",
        f"- Linear update: {d.linear_update.action if d.linear_update else '-'}",
        f"- models: planner `{record.planner_model}`, reviewer `{record.reviewer_model}`",
        f"- tokens: in {c.input_tokens} / out {c.output_tokens}; est. cost: "
        + (f"${c.estimated_cost_usd:.4f}" if c.estimated_cost_usd is not None else "not estimable (price not configured)"),
        f"- record: `{record.artifact_path}`" + (f" / handoff: `{record.handoff_path}`" if record.handoff_path else ""),
    ]
    if d.reasons:
        lines += ["", "## Reasons", *[f"- `{r.code.value}` {r.message}" for r in d.reasons]]
    for s in d.sources:
        lines.append(f"- source {s.system}: {s.identifier or s.id} (version {s.version})")
    return "\n".join(lines)
