"""``research-agent external ...`` (Phase 9).  Normal operation needs none of these commands: onboarding runs
from planning / experiments and decisions are applied by the autopilot poller.  They exist for inspection,
manual onboarding and standalone execution through the same gate."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, help="Phase 9 external repository onboarding, grants and executions")


def _out(v) -> None:
    typer.echo(json.dumps(v, ensure_ascii=False, indent=2, default=str))


def _deps():
    from research_autopilot.config import load_dotenv, load_settings
    from research_autopilot.external.service import optional_from_env
    from research_autopilot.storage.db import Store
    load_dotenv()
    store = Store(load_settings().resolved_database_url())
    store.init_schema()
    deps = optional_from_env(store)
    if deps is None:
        typer.echo("Phase 9 disabled (RA_EXTERNAL_ENABLED=false or RA_LITERATURE_CONTACT_EMAIL unset)", err=True)
        raise typer.Exit(2)
    return deps


def _entry_summary(e) -> dict:
    v = e.verification
    return {"entry_id": e.entry_id, "state": e.state.value, "url": e.identification.url, "commit": e.commit,
            "provider": e.identification.provider.value, "evidence": e.identification.provider_evidence,
            "uncertainties": e.identification.uncertainties,
            "license": v.license.model_dump(include={"spdx", "category", "decision", "reasons", "detected"}) if v else None,
            "integrity": v.integrity.model_dump(exclude={"mismatches"}) if v else None,
            "requirements": v.survey.requirements if v else None,
            "environment": v.environment.model_dump(exclude={"wheels"}) if v and v.environment else None,
            "findings": [f"{f.severity} {f.rule} {f.path}:{f.line}" for f in (v.findings if v else [])][:20],
            "tests": [{"name": t.name, "kind": t.kind, "passed": t.passed, "exit": t.exit_code,
                       "duration_s": t.duration_s, "argv": t.argv} for t in (v.tests if v else [])],
            "status": v.status if v else None, "failure_reasons": v.failure_reasons if v else None,
            "unverified": v.unverified if v else None, "cost_usd": v.cost_usd if v else None}


@app.command("onboard")
def onboard(name: str, commit: str | None = typer.Option(None, "--commit"),
            report: str | None = typer.Option(None, "--report", help="Phase 8 report id that names the paper"),
            retry_failed: bool = typer.Option(False, "--retry-failed")) -> None:
    """Identify, fetch (fixed commit), assess license, verify in the sandbox and register."""
    from research_autopilot.external import integration, service
    deps = _deps()
    paper = text = None
    if report:
        from research_autopilot.literature.models import LiteratureReport
        from research_autopilot.literature.store import store_dir
        rep = LiteratureReport.model_validate_json((store_dir() / "reports" / f"{report}.json").read_text())
        for n, p, t in integration.candidates_from_report(rep):
            paper, text = p, t
            if n.lower() == name.lower():
                break
    e = service.onboard(deps, name, commit=commit, paper=paper, paper_text=text, actor="cli",
                        retry_failed=retry_failed)
    _out(_entry_summary(e))


@app.command("list")
def list_entries() -> None:
    from research_autopilot.external import registry
    deps = _deps()
    _out([{"entry_id": e.entry_id, "state": e.state.value, "license": e.verification.license.spdx if e.verification else None,
           "registered_at": e.registered_at} for e in registry.entries(deps.store)])


@app.command("show")
def show(entry_id: str) -> None:
    from research_autopilot.external import registry
    deps = _deps()
    e = registry.get_entry(deps.store, entry_id)
    if e is None:
        raise typer.Exit(1)
    _out({**_entry_summary(e), "history": [h.model_dump() for h in e.history]})


@app.command("history")
def history(name: str) -> None:
    """Registry, decisions, grants and executions of one repository (audit trail)."""
    from research_autopilot.external import registry
    deps = _deps()
    ents = registry.entries(deps.store, name)
    _out({"entries": [{"entry_id": e.entry_id, "state": e.state.value, "events": [h.model_dump() for h in e.history]}
                      for e in ents],
          "decisions": [d.model_dump() for e in ents for d in registry.decisions(deps.store, e.entry_id)],
          "grants": [{"grant_id": g.grant_id, "entry_id": g.draft.entry_id, "status": g.status.value,
                      "approved_by": g.approved_by, "approved_at": g.approved_at, "scope": g.draft.scope.model_dump()}
                     for g in registry.grants(deps.store, name=name)],
          "executions": [x.model_dump(exclude={"argv"}) for e in ents for x in registry.executions(deps.store, entry_id=e.entry_id)]})


@app.command("request-grant")
def request_grant(entry_id: str, purpose: str = typer.Option(..., "--purpose"), detail: str = typer.Option("", "--detail"),
                  repo: list[str] = typer.Option(None, "--repo", help="research repository ids (default: any)"),
                  network: str = typer.Option("none", "--network"), checkout: str = typer.Option("rw", "--checkout"),
                  cpus: float = typer.Option(2, "--cpus"), memory: float = typer.Option(4, "--memory-gb"),
                  walltime: int = typer.Option(30, "--walltime-min"),
                  total: float = typer.Option(5, "--total-cpu-hours"),
                  task: str | None = typer.Option(None, "--task")) -> None:
    """Ask the human (Discord) for an execution grant of a registered entry."""
    from research_autopilot.external import grants, registry
    from research_autopilot.external.models import ExecutionScope, FileAccess
    deps = _deps()
    e = registry.get_entry(deps.store, entry_id)
    if e is None or e.state.value not in ("registered", "superseded"):
        typer.echo(f"{entry_id} is not a registered entry", err=True)
        raise typer.Exit(1)
    scope = ExecutionScope(purposes=[purpose], purpose_detail=detail, research_repositories=repo or [],
                           file_access=FileAccess(job_checkout=checkout), network=network, max_cpus=cpus,
                           max_memory_gb=memory, max_walltime_minutes=walltime, max_total_cpu_hours=total)
    req = grants.request_grant(deps.store, e, grants.draft_for(e, scope, task_id=task, reason="requested via CLI"),
                               notifier=deps.notifier)
    _out({"request_id": req.request_id, "status": req.status.value, "approval_hash": req.spec_hash})


@app.command("decisions")
def decisions() -> None:
    """Apply verified Discord decisions (the autopilot poller does this automatically)."""
    from research_autopilot.external.integration import process_approvals
    _out(process_approvals(_deps()))


@app.command("revoke")
def revoke(grant_id: str, reason: str = typer.Option(..., "--reason"), actor: str = typer.Option(..., "--actor")) -> None:
    from research_autopilot.external import grants
    deps = _deps()
    _out(grants.revoke(deps.store, grant_id, actor, reason, deps.catalog).model_dump(include={"grant_id", "status", "revoked_reason"}))


@app.command("run")
def run(name: str, work: Path = typer.Option(..., "--work", help="research directory to copy into the job (never mounted live)"),
        repo: str = typer.Option(..., "--repo", help="research repository id"),
        purpose: str = typer.Option(..., "--purpose"), detail: str = typer.Option("", "--detail"),
        commit: str | None = typer.Option(None, "--commit"), mount_as: str = typer.Option("ext", "--mount-as"),
        network: str = typer.Option("none", "--network"), cpus: float = typer.Option(1, "--cpus"),
        memory: float = typer.Option(2, "--memory-gb"), walltime: int = typer.Option(10, "--walltime-min"),
        patch: list[Path] = typer.Option(None, "--patch"), task: str | None = typer.Option(None, "--task"),
        argv: list[str] = typer.Argument(..., help="python <script relative to --work> ...")) -> None:
    """Run external code for research through the gate (same checks as Phase 4); records the execution."""
    from research_autopilot.external import grants
    from research_autopilot.external.github import ext_root
    from research_autopilot.external.models import ExecutionRequest, FileAccess
    deps = _deps()
    req = ExecutionRequest(name=name, commit=commit, purpose=purpose, purpose_detail=detail, research_repository=repo,
                           task_id=task, network=network, file_access=FileAccess(job_checkout="rw"), cpus=cpus,
                           memory_gb=memory, walltime_minutes=walltime, patches=[str(p.resolve()) for p in patch or []])
    gate = grants.authorize(deps.store, req, docker=deps.docker)
    if not gate.allowed:
        out = {"allowed": False, "reasons": gate.reasons}
        if gate.draft is not None:
            from research_autopilot.external.service import verify_patched
            checks = verify_patched(deps, gate.entry, req.patches)
            out["patched_code_checks"] = checks
            ar = grants.request_grant(deps.store, gate.entry, gate.draft, notifier=deps.notifier,
                                      diff=gate.reasons + checks)
            out["approval_request"] = {"request_id": ar.request_id, "status": ar.status.value}
        _out(out)
        raise typer.Exit(3)
    code_dir, patches, code_sha = grants.prepare_code(gate.entry, req.patches)
    job = ext_root() / "jobs" / f"{int(time.time())}-{gate.grant.grant_id}"
    shutil.copytree(work, job / "work")
    v = gate.entry.verification
    cmd = deps.docker.research_argv(image=v.environment.image_id, name=f"ra-run-{job.name}"[:60], code_dir=code_dir,
                                    mount_as=mount_as, checkout=job / "work", checkout_mode="rw", data_paths=[],
                                    network=network, cpus=cpus, memory_gb=memory, workdir=".",
                                    env={"PYTHONHASHSEED": "0"}, python_dirs=v.survey.python_dirs, argv=list(argv))
    rec = grants.record_start(deps.store, gate, req, argv=cmd, code_sha256=code_sha, patches=patches,
                              catalog=deps.catalog)
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=walltime * 60, stdin=subprocess.DEVNULL)
        code, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        subprocess.run([deps.docker.binary, "kill", f"ra-run-{job.name}"[:60]], capture_output=True)
        code, out, err = None, "", "walltime exceeded"
    rec = grants.record_finish(deps.store, rec, exit_code=code, wall_seconds=time.monotonic() - t0, cpus=cpus,
                               detail=err[-300:], catalog=deps.catalog)
    _out({"allowed": True, "grant_id": gate.grant.grant_id, "execution_id": rec.execution_id, "exit_code": code,
          "job_dir": str(job), "stdout_tail": out[-1500:], "stderr_tail": err[-1500:]})
