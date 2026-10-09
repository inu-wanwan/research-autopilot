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

import json
import sys

import typer

from research_autopilot.config import load_settings
from research_autopilot.storage.db import Store

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Research Autopilot task planning CLI")
runs_app = typer.Typer(no_args_is_help=True, help="Inspect plan-run history")
db_app = typer.Typer(no_args_is_help=True, help="Database management")
impl_app = typer.Typer(no_args_is_help=True, help="Inspect Phase 3 implementation runs")
app.add_typer(runs_app, name="runs")
app.add_typer(db_app, name="db")
app.add_typer(impl_app, name="impl-runs")

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
