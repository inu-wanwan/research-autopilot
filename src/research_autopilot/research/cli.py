"""``research-agent research ...`` (Phase 10). Normal operation is Run Autopilot + Discord; these commands inspect
the state and offer a terminal fallback for Stop / Resume."""

import json

import typer

app = typer.Typer(no_args_is_help=True, help="Phase 10 research planning / orchestration")


def _out(v) -> None:
    typer.echo(json.dumps(v, ensure_ascii=False, indent=2, default=str))


def _store():
    from research_autopilot.config import load_dotenv, load_settings
    from research_autopilot.storage.db import Store
    load_dotenv()
    s = Store(load_settings().resolved_database_url())
    s.init_schema()
    return s


@app.command("status")
def status(task_id: str) -> None:
    """Plans, authorization, cycles and controls of a research objective."""
    from research_autopilot.research import orchestration, workflow
    store = _store()
    auth = workflow.research_authorization(store, task_id)
    _out({"plans": [{"version": p.version, "kind": p.kind, "status": p.status.value, "hash": p.content_hash[:12],
                     "tasks": len(p.tasks), "open_questions": [q.model_dump() for q in p.scope.open_questions],
                     "url": p.plan_page_url, "cost_usd": p.cost_usd} for p in workflow.plans(store, task_id)],
          "authorization": {"id": auth.authorization_id, "version": auth.version, "hash": (auth.content_hash or "")[:12],
                            "approved_by": auth.approved_by} if auth else None,
          "cycles": [{"number": c.number, "status": c.status.value, "tasks": c.child_task_ids,
                      "decision": c.decision.action if c.decision else None,
                      "rationale": c.decision.rationale[:300] if c.decision else None,
                      "budget_after": c.budget_after} for c in orchestration.cycles(store, task_id)],
          "paused": orchestration.paused(store, task_id),
          "controls": [c.model_dump() for c in orchestration.controls(store, task_id)]})


@app.command("stop")
def stop(task_id: str, actor: str = typer.Option(..., "--actor")) -> None:
    """Stop starting new work (running jobs finish). Same effect as the Discord Stop button."""
    from research_autopilot.research import orchestration
    _out(orchestration.request_control(_store(), task_id, "stop", f"cli:{actor}", "cli").model_dump())


@app.command("resume")
def resume(task_id: str, actor: str = typer.Option(..., "--actor")) -> None:
    from research_autopilot.research import orchestration
    _out(orchestration.request_control(_store(), task_id, "resume", f"cli:{actor}", "cli").model_dump())


@app.command("plan")
def show_plan(task_id: str, version: int | None = typer.Option(None, "--version")) -> None:
    from research_autopilot.research import workflow
    ps = workflow.plans(_store(), task_id)
    p = next((x for x in ps if x.version == version), ps[-1] if ps else None)
    _out(p.model_dump(mode="json") if p else None)
