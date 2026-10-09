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
app.add_typer(runs_app, name="runs")
app.add_typer(db_app, name="db")

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
