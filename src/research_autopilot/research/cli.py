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


@app.command("approve")
def approve(task_id: str, decision: str = typer.Option("approve", "--decision", help="approve | reject | revise"),
            actor: str = typer.Option(..., "--actor", help="who decides and on whose behalf"),
            comment: str = typer.Option("", "--comment")) -> None:
    """Development only (RA_ENVIRONMENT=dev): decide the task's pending approval request from the terminal,
    recorded as cli-dev:<actor>. In production approvals are made only in Discord."""
    from research_autopilot.autopilot.approvals import submit_cli_decision
    from research_autopilot.autopilot.models import ApprovalRequest
    from research_autopilot.storage import db
    if decision not in ("approve", "reject", "revise"):
        raise typer.BadParameter("decision must be approve, reject or revise")
    store = _store()
    pending = sorted((ApprovalRequest.model_validate(r) for r in store.find(db.approval_requests, task_id=task_id)
                      if r.get("status") == "pending"), key=lambda r: r.created_at)
    if not pending:
        typer.echo(f"no pending approval request for {task_id}", err=True)
        raise typer.Exit(1)
    req = pending[-1]
    try:
        sub = submit_cli_decision(store, req, decision, actor, comment)
    except (PermissionError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    _out({"request_id": req.request_id, "kind": req.kind.value, "title": req.title, "decision": decision,
          "actor": f"cli-dev:{sub.user_id}", "note": "applied on the next poll (same spec-hash / expiry checks)"})


@app.command("plan")
def show_plan(task_id: str, version: int | None = typer.Option(None, "--version")) -> None:
    from research_autopilot.research import workflow
    ps = workflow.plans(_store(), task_id)
    p = next((x for x in ps if x.version == version), ps[-1] if ps else None)
    _out(p.model_dump(mode="json") if p else None)


@app.command("plan-dry-run")
def plan_dry_run(task_id: str, as_objective: bool = typer.Option(
        False, "--as-objective", help="treat the whole page as a research objective even without the heading")) -> None:
    """10A without side effects: scope structuring, open questions and the plan draft (nothing saved, no approval)."""
    from research_autopilot.autopilot.factory import build_autopilot_deps
    from research_autopilot.research import evidence as evidence_mod
    from research_autopilot.research import planner as planner_mod
    from research_autopilot.research import scope as scope_mod
    from research_autopilot.research.workflow import llm_factory_from_env
    deps = build_autopilot_deps(dry_run=True)
    bundle = deps.plan.manager.load(task_id)
    spec = bundle.spec
    if spec is None:
        _out({"error": [r.message for r in bundle.reasons]})
        raise typer.Exit(1)
    if as_objective and not scope_mod.is_research_objective(spec):
        spec = spec.model_copy(update={"research_objective": spec.purpose or spec.title})
    llm = llm_factory_from_env(deps.plan.settings)()
    text = scope_mod.spec_text(spec)
    ev = evidence_mod.collect(spec_text=text, store=deps.store, catalog=getattr(deps.plan, "catalog", None),
                              literature_reports=evidence_mod.literature_reports(task_id, text),
                              registry=deps.impl.registry)
    plan = planner_mod.build_plan(llm, spec, task_id=task_id, notion_page_id=None, evidence=ev,
                                  registry=deps.impl.registry, version=0)
    _out({"status": plan.status.value, "cost_usd": plan.cost_usd,
          "blocking": [q.model_dump() for q in plan.scope.blocking()],
          "open_questions": [q.model_dump() for q in plan.scope.open_questions],
          "scope": plan.scope.model_dump(mode="json"), "tasks": [t.model_dump(mode="json") for t in plan.tasks],
          "validation_problems": plan.validation_problems, "evidence": [e.model_dump() for e in plan.evidence]})


@app.command("revoke")
def revoke(task_id: str, reason: str = typer.Option(..., "--reason"), actor: str = typer.Option(..., "--actor")) -> None:
    """Revoke the current research-scope authorization (e.g. it contains a value that was not verified).
    The next Resume / Run Autopilot drafts a new plan version that must be approved again."""
    from research_autopilot.autopilot.models import ResearchAuthorization
    from research_autopilot.research import workflow
    from research_autopilot.storage import db
    store = _store()
    auth = workflow.research_authorization(store, task_id)
    if auth is None:
        _out({"revoked": None})
        return
    auth = ResearchAuthorization.model_validate({**auth.model_dump(mode="json"), "revoked": True})
    rec = auth.model_dump(mode="json")
    rec["revocation"] = {"actor": actor, "reason": reason}
    store.put(db.authorizations, auth.authorization_id, rec, root_task_id=auth.root_task_id)
    _out({"revoked": auth.authorization_id, "version": auth.version, "reason": reason, "actor": actor})
