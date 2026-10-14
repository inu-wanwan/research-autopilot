"""Prefect flows ``implement-task`` and ``repair-task`` (Phase 1 work pool)."""

import re
from pathlib import Path

from prefect import flow, get_run_logger
from prefect.artifacts import create_markdown_artifact
from prefect.runtime import flow_run as runtime_flow_run

from research_autopilot.implementation.factory import build_impl_deps
from research_autopilot.implementation.models import ImplementationManifest, RunState
from research_autopilot.implementation.orchestrator import implement, repair
from research_autopilot.notifications import safe_send
from research_autopilot.notifications.events import EventKind, NotificationEvent
from research_autopilot.notifications.hooks import notify_completed, notify_failed


def _name() -> str:
    p = runtime_flow_run.parameters
    return f"implement-{p.get('task_id') or (p.get('resume_run_id') or '')[:8] or 'handoff'}"


@flow(name="implement-task", flow_run_name=_name, on_completion=[notify_completed], on_failure=[notify_failed],
      on_crashed=[notify_failed])
def implement_task_flow(task_id: str | None = None, handoff_path: str | None = None,
                        resume_run_id: str | None = None) -> dict:
    deps = build_impl_deps()
    m = implement(deps, task_id=task_id, handoff_path=Path(handoff_path) if handoff_path else None,
                  resume_run_id=resume_run_id, flow_run_id=_flow_run_id())
    return _publish(m)


@flow(name="repair-task", flow_run_name=lambda: f"repair-{Path(runtime_flow_run.parameters['report_path']).stem}",
      on_completion=[notify_completed], on_failure=[notify_failed], on_crashed=[notify_failed])
def repair_task_flow(report_path: str) -> dict:
    deps = build_impl_deps()
    m = repair(deps, Path(report_path), flow_run_id=_flow_run_id())
    if m.state is RunState.COMPLETED:
        safe_send(NotificationEvent(kind=EventKind.REPAIR_SUCCEEDED, flow_name="repair-task",
                                    flow_run_name=m.task_id, flow_run_id=m.flow_run_id,
                                    state_name=m.state.value, details=[f"Phase 3 run: {m.phase3_run_id}"]))
    return _publish(m)


def _flow_run_id() -> str | None:
    return str(runtime_flow_run.id) if runtime_flow_run.id else None


def _publish(m: ImplementationManifest) -> dict:
    logger = get_run_logger()
    for e in m.events:
        logger.info("[%s] %s", e.kind, e.message)
    if m.state is RunState.BLOCKED:
        # Human attention needed (approval, scope, budget, revision limit, ...). No approval request exists
        # behind this message, so it carries the reasons and steps instead of approval buttons.
        safe_send(NotificationEvent(
            kind=EventKind.UNRECOVERABLE_ERROR, flow_name="implement-task", flow_run_name=m.task_id,
            flow_run_id=m.flow_run_id, state_name=f"blocked:{m.block_reasons[-1].code if m.block_reasons else ''}",
            details=[*[f"理由: {b.code}: {b.message[:300]}" for b in m.block_reasons[-5:]],
                     f"Phase 3 run: {m.phase3_run_id}",
                     f"費用: ${m.cost_usd_known:.4f}（費用不明の呼び出し {m.cost_unknown_calls} 件）"],
            needs_human=True,
            steps=["上の［理由］を確認する（詳細は Prefect の run の artifact）",
                   "理由に応じて Notion の仕様（実装方針・対象ファイル・受け入れ基準）、承認、環境を直す",
                   f"implement-task を resume_run_id={m.phase3_run_id} で実行し直す"]))
    create_markdown_artifact(key="impl-" + re.sub(r"[^a-z0-9-]", "-", m.task_id.lower()),
                             markdown=summary_markdown(m), description=f"Phase 3 run for {m.task_id}")
    return {"phase3_run_id": m.phase3_run_id, "state": m.state.value, "task_id": m.task_id,
            "implementation_commit": m.implementation_commit, "experiment_spec_id": m.experiment_spec_id,
            "block_reasons": [b.model_dump() for b in m.block_reasons], "artifacts": m.artifact_paths}


def summary_markdown(m: ImplementationManifest) -> str:
    a = m.assessment
    lines = [
        f"# Phase 3 {m.task_id} — **{m.state.value}**",
        f"- run: `{m.phase3_run_id}`" + (f" (repair round {m.repair_round} of `{m.parent_run_id}`)" if m.parent_run_id else ""),
        f"- complexity / risk: {a.implementation_complexity if a else '-'} / {a.scientific_risk if a else '-'}",
        f"- branch: `{m.task.branch if m.task else '-'}` commit: `{m.implementation_commit or '-'}` (not merged)",
        f"- attempts: {len(m.attempts)} (revisions {m.revisions_used}), backend invocations: {m.backend_invocations}, "
        f"fallback: {m.fallback_used}",
        f"- tokens: {m.usage.total} (+{m.inherited_usage.total} inherited); known cost ${m.cost_usd_known:.4f}, "
        f"{m.cost_unknown_calls} call(s) with unknown cost",
        f"- experiment spec: `{m.experiment_spec_id or '-'}`",
    ]
    for at in m.attempts:
        lines.append(f"  - attempt {at.number} {at.choice.backend}/{at.choice.model}: "
                     f"{at.result.status if at.result else '-'} -> {at.review.verdict if at.review else '-'}"
                     + (f" (escalation: {at.escalation_reason})" if at.escalation_reason else "")
                     + (f" (fallback: {at.fallback_reason})" if at.fallback_reason else ""))
    for b in m.block_reasons:
        lines.append(f"- blocked `{b.code}`: {b.message}")
    return "\n".join(lines)
