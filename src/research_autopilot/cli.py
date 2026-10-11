"""``research-agent`` CLI.

    research-agent plan --task INU-123            # dry-run (default): plan + review, no external writes
    research-agent plan --next                    # pick the next eligible task deterministically
    research-agent plan --task INU-123 --check-only   # deterministic gates only, no LLM calls
    research-agent plan --task INU-123 --apply    # also move Linear to In Progress (needs RA_LINEAR_WRITE_ENABLED=true)
    research-agent runs list [--task INU-123]
    research-agent runs show <plan_run_id>
    research-agent db init

By default ``plan`` runs as the Prefect flow ``plan-task`` (history appears in the
Prefect UI of the configured PREFECT_API_URL). ``--local`` skips Prefect.
"""

import asyncio
import json
import os
import sys

import typer

from research_autopilot.config import load_dotenv, load_settings
from research_autopilot.models import utcnow
from research_autopilot.storage.db import Store

load_dotenv()  # secrets stay in the process environment; nothing is printed

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Research Autopilot task planning CLI")
runs_app = typer.Typer(no_args_is_help=True, help="Inspect plan-run history")
db_app = typer.Typer(no_args_is_help=True, help="Database management")
impl_app = typer.Typer(no_args_is_help=True, help="Inspect Phase 3 implementation runs")
app.add_typer(runs_app, name="runs")
app.add_typer(db_app, name="db")
app.add_typer(impl_app, name="impl-runs")
exp_app = typer.Typer(no_args_is_help=True, help="Phase 4 experiments")
app.add_typer(exp_app, name="experiment")
ap_app = typer.Typer(no_args_is_help=True, help="Phase 5 autonomous workflow")
app.add_typer(ap_app, name="autopilot")
discord_app = typer.Typer(no_args_is_help=True, help="Discord notifications and approval UI")
app.add_typer(discord_app, name="discord")

EXIT = {"ready": 0, "blocked": 2, "insufficient_spec": 3, "transient_failure": 4}


@app.command()
def plan(
    task: str | None = typer.Option(None, "--task", help="Linear issue identifier, e.g. INU-123"),
    next_: bool = typer.Option(False, "--next", help="Select the next eligible task"),
    apply: bool = typer.Option(False, "--apply", help="Move the Linear issue to In Progress when ready"),
    check_only: bool = typer.Option(False, "--check-only", help="Run deterministic gates only (no LLM, no writes)"),
    local: bool = typer.Option(False, "--local", help="Run without Prefect"),
    as_json: bool = typer.Option(False, "--json", help="Print the decision as JSON"),
) -> None:
    if (task is None) == (not next_):
        raise typer.BadParameter("specify exactly one of --task or --next")
    if apply and check_only:
        raise typer.BadParameter("--apply and --check-only are mutually exclusive")
    mode = "apply" if apply else "check_only" if check_only else "dry_run"

    if local:
        from research_autopilot.orchestrator.core import plan_task
        from research_autopilot.orchestrator.factory import build_deps

        record = plan_task(build_deps(load_settings()), task_id=task, next_task=next_, mode=mode)
        decision = record.decision.model_dump(mode="json")
        decision["_record"] = record.artifact_path
    else:
        from research_autopilot.orchestrator.flow import plan_task_flow

        decision = plan_task_flow(task_id=task, next_task=next_, mode=mode)

    if as_json:
        typer.echo(json.dumps(decision, ensure_ascii=False, indent=2))
    else:
        _print_decision(decision)
    raise typer.Exit(EXIT.get(decision["status"], 1))


def _print_decision(d: dict) -> None:
    typer.echo(f"task: {d['task_id']}  decision: {d['status'].upper()}")
    typer.echo(f"rationale: {d['rationale']}")
    for r in d["reasons"]:
        typer.echo(f"  - [{r['code']}] {r['message']}")
    if d.get("reviewer_verdict"):
        typer.echo(f"reviewer: {d['reviewer_verdict']}  plan_id: {d['plan_id']}")
    if d.get("linear_update"):
        u = d["linear_update"]
        typer.echo(f"linear: {u['action']} ({u.get('from_state')} -> {u.get('to_state')})")
    typer.echo(f"plan_run_id: {d['plan_run_id']}")


IMPL_EXIT = {"completed": 0, "blocked": 2, "failed": 5, "interrupted": 4}


@app.command()
def implement(
    task: str | None = typer.Option(None, "--task", help="Use the latest Phase 2 handoff of this task"),
    handoff: str | None = typer.Option(None, "--handoff", help="Path to a Phase 2 handoff JSON"),
    resume: str | None = typer.Option(None, "--resume", help="Resume an interrupted Phase 3 run by id"),
    local: bool = typer.Option(False, "--local", help="Run without Prefect"),
) -> None:
    """Phase 3: implement an approved task (never merges or pushes)."""
    if sum(x is not None for x in (task, handoff, resume)) != 1:
        raise typer.BadParameter("specify exactly one of --task, --handoff, --resume")
    if local:
        from pathlib import Path

        from research_autopilot.implementation.factory import build_impl_deps
        from research_autopilot.implementation.flow import summary_markdown
        from research_autopilot.implementation.orchestrator import implement as run_implement

        m = run_implement(build_impl_deps(), task_id=task, handoff_path=Path(handoff) if handoff else None,
                          resume_run_id=resume)
        typer.echo(summary_markdown(m))
        raise typer.Exit(IMPL_EXIT.get(m.state.value, 1))
    from research_autopilot.implementation.flow import implement_task_flow

    out = implement_task_flow(task_id=task, handoff_path=handoff, resume_run_id=resume)
    typer.echo(json.dumps(out, ensure_ascii=False, indent=2))
    raise typer.Exit(IMPL_EXIT.get(out["state"], 1))


@app.command("repair")
def repair_cmd(report: str = typer.Argument(..., help="Path to a Phase 4 FailureReport JSON"),
               local: bool = typer.Option(False, "--local")) -> None:
    """Phase 4 -> 3: repair a failed experiment commit within the approved scope."""
    if local:
        from pathlib import Path

        from research_autopilot.implementation.factory import build_impl_deps
        from research_autopilot.implementation.flow import summary_markdown
        from research_autopilot.implementation.orchestrator import repair as run_repair

        m = run_repair(build_impl_deps(), Path(report))
        typer.echo(summary_markdown(m))
        raise typer.Exit(IMPL_EXIT.get(m.state.value, 1))
    from research_autopilot.implementation.flow import repair_task_flow

    out = repair_task_flow(report_path=report)
    typer.echo(json.dumps(out, ensure_ascii=False, indent=2))
    raise typer.Exit(IMPL_EXIT.get(out["state"], 1))


@impl_app.command("list")
def impl_list(task: str | None = typer.Option(None, "--task"), limit: int = typer.Option(20, "--limit")) -> None:
    store = Store(load_settings().resolved_database_url())
    store.init_schema()
    for r in store.list_implementations(task, limit):
        typer.echo(f"{r['created_at']:%Y-%m-%d %H:%M:%S} {r['task_id']:<10} {r['state']:<12} "
                   f"{(r['implementation_commit'] or '-')[:12]:<12} parent={(r['parent_run_id'] or '-')[:8]} "
                   f"{r['phase3_run_id']}")


@impl_app.command("show")
def impl_show(run_id: str) -> None:
    store = Store(load_settings().resolved_database_url())
    m = store.get_implementation(run_id)
    if m is None:
        typer.echo("not found", err=True)
        raise typer.Exit(1)
    typer.echo(m.model_dump_json(indent=2))


EXP_EXIT = {"completed": 0, "blocked": 2, "failed": 5}


@exp_app.command("run")
def experiment_run(phase3_run: list[str] = typer.Option(..., "--phase3-run", help="Completed Phase 3 run id (repeatable)"),
                   local: bool = typer.Option(False, "--local")) -> None:
    """Run the ExperimentSpecs of completed Phase 3 runs (never approves follow-up proposals)."""
    _exp(phase3_run_ids=phase3_run, resume_run_id=None, local=local)


@exp_app.command("resume")
def experiment_resume(run_id: str, local: bool = typer.Option(False, "--local")) -> None:
    """Reconcile the job ledger after a restart and continue an experiment run."""
    _exp(phase3_run_ids=None, resume_run_id=run_id, local=local)


def _exp(phase3_run_ids, resume_run_id, local: bool) -> None:
    if local:
        from research_autopilot.experiments.factory import build_exp_deps
        from research_autopilot.experiments.flow import summary_markdown
        from research_autopilot.experiments.orchestrator import run_experiment

        m = run_experiment(build_exp_deps(), phase3_run_ids=phase3_run_ids, resume_run_id=resume_run_id)
        typer.echo(summary_markdown(m))
        raise typer.Exit(EXP_EXIT.get(m.state.value, 1))
    from research_autopilot.experiments.flow import run_experiment_flow

    out = run_experiment_flow(phase3_run_ids=phase3_run_ids, resume_run_id=resume_run_id)
    typer.echo(json.dumps(out, ensure_ascii=False, indent=2))
    raise typer.Exit(EXP_EXIT.get(out["state"], 1))


@exp_app.command("list")
def experiment_list(task: str | None = typer.Option(None, "--task")) -> None:
    store = Store(load_settings().resolved_database_url())
    store.init_schema()
    for r in store.list_experiments(task):
        typer.echo(f"{r['created_at']:%Y-%m-%d %H:%M:%S} {r['task_id']:<10} {r['state']:<10} {r['experiment_run_id']}")


@exp_app.command("show")
def experiment_show(run_id: str) -> None:
    m = Store(load_settings().resolved_database_url()).get_experiment(run_id)
    if m is None:
        typer.echo("not found", err=True)
        raise typer.Exit(1)
    typer.echo(m.model_dump_json(indent=2))


@exp_app.command("proposals")
def experiment_proposals(task: str | None = typer.Option(None, "--task")) -> None:
    store = Store(load_settings().resolved_database_url())
    store.init_schema()
    for p in store.list_proposals(task):
        sa_ = p["scope_assessment"]
        typer.echo(f"{p['proposal']['proposal_id']} {p['proposal']['draft']['title'][:50]!r} "
                   f"review={(p['review'] or {}).get('verdict')} within_scope={sa_['within_scope']} "
                   f"human={sa_['requires_human_decision']} status={p['approval_status']}")


@ap_app.command("poll")
def autopilot_poll(task: str | None = typer.Option(None, "--task", help="Only this Linear issue"),
                   check_only: bool = typer.Option(False, "--check-only", help="Discover / evaluate only; no writes, no LLM"),
                   local: bool = typer.Option(False, "--local", help="Run without Prefect")) -> None:
    """One scheduler tick: outbox retries, approval checks, advance <=1 research task.
    Writes require RA_AUTOPILOT_WRITE=true; RA_AUTOPILOT_TEST_ONLY=true (default) limits them to [TEST] issues."""
    if local:
        from research_autopilot.autopilot.engine import poll
        from research_autopilot.autopilot.factory import build_autopilot_deps

        typer.echo(json.dumps(poll(build_autopilot_deps(dry_run=check_only), only_task=task), ensure_ascii=False, indent=2,
                              default=str))
        return
    from research_autopilot.autopilot.flows import autopilot_poll_flow

    typer.echo(json.dumps(autopilot_poll_flow(task_id=task, dry_run=check_only), ensure_ascii=False, indent=2, default=str))


@ap_app.command("approve")
def autopilot_approve(request_id: str, decision: str = typer.Option(..., "--decision", help="approve | reject | revise"),
                      approver: str = typer.Option(..., "--approver"), comment: str = typer.Option("", "--comment")) -> None:
    """Submit the approval form of a pending request through the authenticated Prefect API
    (same as pressing Resume in the Prefect UI; requires PREFECT_API_AUTH_STRING). The poller then
    verifies it against the Linear status, spec hash and expiry like any form submission."""
    if decision not in ("approve", "reject", "revise"):
        raise typer.BadParameter("decision must be approve, reject or revise")
    from prefect.flow_runs import resume_flow_run

    from research_autopilot.storage import db as dbm

    store = Store(load_settings().resolved_database_url())
    req = store.get(dbm.approval_requests, request_id)
    if req is None or not req.get("confirmation_ref"):
        typer.echo("unknown request or no confirmation form", err=True)
        raise typer.Exit(1)
    resume_flow_run(req["confirmation_ref"], run_input={"decision": decision, "approver": approver, "comment": comment,
                                                        "request_id": request_id, "spec_hash": req["spec_hash"]})
    typer.echo(f"submitted {decision} for {request_id[:8]} (form run {req['confirmation_ref']}); now set the Linear status")


@ap_app.command("status")
def autopilot_status() -> None:
    """Workflow runs, pending approvals and outbox state."""
    from research_autopilot.autopilot.engine import status_report
    from research_autopilot.autopilot.factory import build_autopilot_deps

    typer.echo(json.dumps(status_report(build_autopilot_deps(dry_run=True)), ensure_ascii=False, indent=2, default=str))


@discord_app.command("run")
def discord_run() -> None:
    """Run the reconnecting Discord Gateway consumer and durable notification sender."""
    from research_autopilot.notifications.discord import load_discord_settings, run_gateway

    settings = load_discord_settings()
    store = Store(load_settings().resolved_database_url())
    store.init_schema()
    run_gateway(store, settings)


@discord_app.command("check")
def discord_check(send_test: bool = typer.Option(False, "--send-test", help="Post a labelled test embed to all channels")) -> None:
    """Validate token, guild and channel access without starting the Gateway loop."""
    from research_autopilot.notifications.discord import connection_check, load_discord_settings

    checked = asyncio.run(connection_check(load_discord_settings(), send_test=send_test))
    typer.echo("Discord configuration OK: " + ", ".join(checked))


@ap_app.command("cancel")
def autopilot_cancel(task: str) -> None:
    """Stop Autopilot work on a task (Linear status is left to the human)."""
    _set_run_stage(task, "cancelled", "cancelled by operator")


@ap_app.command("resume")
def autopilot_resume(task: str, stage: str = typer.Option(..., "--stage", help="Stage to resume (e.g. planning)")) -> None:
    """Move a BLOCKED / DEAD_LETTER run back to a stage after a human fixed the cause."""
    _set_run_stage(task, stage, f"resumed by operator at {stage}")


def _set_run_stage(task: str, stage: str, message: str) -> None:
    from research_autopilot.autopilot.models import WorkflowRun, WorkflowStage
    from research_autopilot.storage import db as dbm

    store = Store(load_settings().resolved_database_url())
    store.init_schema()
    runs = [WorkflowRun.model_validate(r) for r in store.find(dbm.workflow_runs, task_id=task)]
    if not runs:
        typer.echo("no workflow run for this task", err=True)
        raise typer.Exit(1)
    run = runs[-1]
    run.stage = WorkflowStage(stage)
    run.attempts = 0
    run.blocked_reason = None
    run.event("operator", message)
    store.put(dbm.workflow_runs, run.run_id, run.model_dump(mode="json"), task_id=run.task_id, stage=run.stage.value)
    if run.execution_run_id:
        from research_autopilot.autopilot.execution import ExecutionRun, ExecutionStatus, transition
        from research_autopilot.autopilot.factory import build_autopilot_deps
        rec = store.get(dbm.execution_runs, run.execution_run_id)
        if rec:
            execution = ExecutionRun.model_validate(rec)
            # A canceled execution is immutable history. ``resume`` changes the
            # legacy WorkflowRun only; a fresh Notion request creates a new run.
            if run.stage is not WorkflowStage.CANCELLED:
                typer.echo(f"{task}: {run.stage.value}")
                return
            target = ExecutionStatus.CANCELED
            execution.status = transition(execution.status, target)
            execution.error_summary = message if target is ExecutionStatus.CANCELED else None
            execution.finished_at = utcnow() if target is ExecutionStatus.CANCELED else None
            store.put(dbm.execution_runs, execution.execution_run_id, execution.model_dump(mode="json"),
                      request_id=execution.request_id, task_id=execution.task_id, status=execution.status.value)
            # Offline CLI use still records the authoritative DB transition;
            # projection is retried by a write-enabled poller.
            if os.getenv("RA_AUTOPILOT_WRITE", "").lower() in {"1", "true", "yes", "on"}:
                deps = build_autopilot_deps(dry_run=False)
                deps.notion_writer.project_execution(execution.notion_task_id, status=execution.status.value,
                    error=execution.error_summary, run_url=(f"{deps.settings.prefect_ui_url.rstrip('/')}/runs/flow-run/{execution.prefect_flow_run_id}"
                    if execution.prefect_flow_run_id and deps.settings.prefect_ui_url else None))
    typer.echo(f"{task}: {run.stage.value}")


@runs_app.command("list")
def runs_list(task: str | None = typer.Option(None, "--task"), limit: int = typer.Option(20, "--limit")) -> None:
    store = Store(load_settings().resolved_database_url())
    store.init_schema()
    rows = store.list_runs(task, limit)
    if not rows:
        typer.echo("no runs")
        return
    typer.echo(f"{'started_at':<20} {'task':<10} {'mode':<10} {'status':<18} {'tokens in/out':<15} {'cost':<10} plan_run_id")
    for r in rows:
        cost = f"${r['estimated_cost_usd']:.4f}" if r["estimated_cost_usd"] is not None else "n/a"
        typer.echo(f"{r['started_at']:%Y-%m-%d %H:%M:%S} {r['task_id'] or '-':<10} {r['mode']:<10} "
                   f"{r['status'] or '-':<18} {r['input_tokens'] or 0}/{r['output_tokens'] or 0:<9} {cost:<10} {r['plan_run_id']}")


@runs_app.command("show")
def runs_show(plan_run_id: str) -> None:
    store = Store(load_settings().resolved_database_url())
    record = store.get_run(plan_run_id)
    if record is None:
        typer.echo("not found", err=True)
        raise typer.Exit(1)
    typer.echo(record.model_dump_json(indent=2))


@db_app.command("init")
def db_init() -> None:
    url = load_settings().resolved_database_url()
    Store(url).init_schema()
    typer.echo(f"schema ready ({url.split('://')[0]})")


def main() -> None:  # pragma: no cover
    sys.exit(app())
