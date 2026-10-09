"""Prefect flow ``run-experiment`` (Phase 4)."""

import re

from prefect import flow, get_run_logger
from prefect.artifacts import create_markdown_artifact
from prefect.runtime import flow_run as runtime_flow_run

from research_autopilot.experiments.factory import build_exp_deps
from research_autopilot.experiments.models import ExperimentManifest, ExperimentState
from research_autopilot.experiments.orchestrator import run_experiment
from research_autopilot.notifications import safe_send
from research_autopilot.notifications.events import EventKind, NotificationEvent
from research_autopilot.notifications.hooks import notify_completed, notify_failed


@flow(name="run-experiment", flow_run_name=lambda: "experiment-" + (
    (runtime_flow_run.parameters.get("resume_run_id") or "")[:8] or ",".join(
        x[:8] for x in runtime_flow_run.parameters.get("phase3_run_ids") or [])),
      on_completion=[notify_completed], on_failure=[notify_failed], on_crashed=[notify_failed])
def run_experiment_flow(phase3_run_ids: list[str] | None = None, resume_run_id: str | None = None) -> dict:
    deps = build_exp_deps()
    m = run_experiment(deps, phase3_run_ids=phase3_run_ids, resume_run_id=resume_run_id,
                       flow_run_id=str(runtime_flow_run.id) if runtime_flow_run.id else None)
    logger = get_run_logger()
    for e in m.events:
        logger.info("[%s] %s", e.kind, e.message)
    if m.state is ExperimentState.BLOCKED:
        safe_send(NotificationEvent(kind=EventKind.APPROVAL_REQUIRED, flow_name="run-experiment", flow_run_name=m.task_id,
                                    flow_run_id=m.flow_run_id, state_name="blocked"))
    create_markdown_artifact(key="exp-" + re.sub(r"[^a-z0-9-]", "-", m.task_id.lower()), markdown=summary_markdown(m),
                             description=f"Phase 4 run for {m.task_id}")
    return {"experiment_run_id": m.experiment_run_id, "state": m.state.value, "block_reason": m.block_reason,
            "phase5_handoff": m.artifact_paths.get("phase5_handoff"), "proposals": m.reviewed_proposals}


def summary_markdown(m: ExperimentManifest) -> str:
    lines = [f"# Phase 4 {m.task_id} — **{m.state.value}**", f"- run: `{m.experiment_run_id}`",
             f"- retries: {m.retries}, Phase 3 round trips: {m.roundtrips}, LLM cost (known): ${m.llm_cost_known:.4f}"]
    if m.block_reason:
        lines.append(f"- blocked: {m.block_reason}")
    for j in m.jobs:
        lines.append(f"  - `{j.job_key}` {j.status.state.value if j.status else '-'}"
                     f" validation={j.validation.status if j.validation else '-'}"
                     f" triage={j.triage.action if j.triage else '-'} metrics={j.validation.metrics if j.validation else {}}")
    for n in m.serialization_notes:
        lines.append(f"- serialization: {n}")
    lines.append(f"- proposals (pending Phase 5 policy): {len(m.reviewed_proposals)}")
    return "\n".join(lines)
