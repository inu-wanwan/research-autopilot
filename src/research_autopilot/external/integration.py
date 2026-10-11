"""Phase 9 hooks into the existing pipeline.

* Phase 8 -> 9: reference implementations found by the Researcher are onboarded (Registry only).
* Phase 2 / 3: the Planner and the Implementer see what is registered and whether execution is granted.
* Phase 4: an experiment that declares ``external_code`` runs only through ``prepare_job`` (gate -> container
  argv); without a covering grant it is blocked with an approval request instead.
* Phase 5: the poller applies verified Discord decisions and resumes runs that waited for them.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from research_autopilot.external import grants, registry, service
from research_autopilot.external.models import (EntryState, ExecutionRequest, FileAccess, GrantStatus, PaperRef,
                                                RegistryEntry)

GITHUB = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")


class NeedsApproval(Exception):
    def __init__(self, request, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.request, self.reasons = request, reasons


class Denied(Exception):
    pass


# ---------------------------------------------------------------- Phase 8 -> 9


def candidates_from_report(report) -> list[tuple[str, PaperRef, str | None]]:
    """(owner/name, paper, paper text) for each reference implementation in a Phase 8 report."""
    from research_autopilot.literature.text import pdf_pages
    papers = [s for s in report.sources if s.source_type.value == "original_paper"]
    paper = papers[0] if papers else None
    ref = PaperRef(title=paper.title, doi=paper.doi, arxiv_id=paper.arxiv_id, authors=paper.authors,
                   literature_report_id=report.report_id, source_id=paper.source_id) if paper else None
    text = None
    if paper and paper.retrieval.status == "ok" and paper.retrieval.local_path:
        try:
            text = "\n".join(pdf_pages(Path(paper.retrieval.local_path), paper.retrieval.sha256))
        except Exception:
            text = None
    out = []
    for s in report.sources:
        if s.source_type.value != "reference_implementation":
            continue
        m = GITHUB.search(s.url or "") or GITHUB.search(f"github.com/{s.title}")
        if m:
            out.append((m.group(1).removesuffix(".git"), ref, text))
    return out


def onboard_from_reports(deps: service.ExternalDeps, reports: list) -> list[RegistryEntry]:
    out = []
    for report in reports:
        for name, paper, text in candidates_from_report(report):
            try:
                out.append(service.onboard(deps, name, paper=paper, paper_text=text))
            except Exception:  # onboarding problems never stop planning; the entry records them when it exists
                continue
    return out


# ---------------------------------------------------------------- Phase 2 / 3 context


def describe(store, entry: RegistryEntry) -> dict:
    v = entry.verification
    active = [g for g in registry.grants(store, name=entry.name, status=GrantStatus.ACTIVE)
              if g.draft.entry_id == entry.entry_id]
    return {"name": entry.name, "commit": entry.commit, "state": entry.state.value,
            "provider": entry.identification.provider.value,
            "paper": entry.identification.paper.title if entry.identification.paper else None,
            "license": v.license.spdx if v else None,
            "python_dirs": v.survey.python_dirs if v else [],
            "requirements": [f"{w.name}=={w.version}" for w in (v.environment.wheels if v and v.environment else [])],
            "tests": [f"{t.name}:{'ok' if t.passed else 'fail'}" for t in (v.tests if v else [])][:8],
            "execution_grant": [{"purposes": g.draft.scope.purposes, "network": g.draft.scope.network,
                                 "max_cpus": g.draft.scope.max_cpus, "max_walltime_minutes": g.draft.scope.max_walltime_minutes}
                                for g in active] or "none (the first run asks the human for approval)"}


def planner_context(store, entries: list[RegistryEntry]) -> str:
    if not entries:
        return ""
    lines = ["EXTERNAL IMPLEMENTATIONS (Phase 9 Registry; registration does not permit execution):"]
    for e in entries:
        d = describe(store, e)
        why = "; ".join((e.verification.failure_reasons if e.verification else [])[:2])
        lines.append(f"- {d['name']}@{d['commit'][:12]} [{d['state']}] provider={d['provider']} license={d['license']} "
                     f"grant={'yes' if isinstance(d['execution_grant'], list) else 'none'}" + (f" ({why[:200]})" if why else ""))
    lines.append("To use a registered implementation, the experiment definition declares it in external_code; "
                 "its first execution waits for a human approval.")
    return "\n".join(lines)


def implementer_view(store, names: list[str]) -> list[dict]:
    out = []
    for n in names:
        e = registry.current_entry(store, n)
        if e is not None:
            out.append(describe(store, e))
    return out


# ---------------------------------------------------------------- Phase 4


def prepare_job(deps: service.ExternalDeps, spec, alloc, *, checkout: Path, env: dict[str, str], job_key: str,
                experiment_run_id: str, onboard_missing: bool = True) -> tuple[list[str], dict]:
    """Container argv for an approved execution plus the provenance to keep with the experiment's results, or
    NeedsApproval / Denied."""
    use = spec.external_code[0]
    req = ExecutionRequest(name=use.name, commit=use.commit, purpose=use.purpose, purpose_detail=use.purpose_detail,
                           research_repository=spec.identification.repository_id, task_id=spec.identification.task_id,
                           network=use.network, file_access=FileAccess(job_checkout=use.job_checkout),
                           cpus=alloc.cpu, memory_gb=alloc.memory_gb,
                           walltime_minutes=max(1, math.ceil(alloc.walltime_seconds / 60)),
                           patches=[str(checkout / p) for p in use.patches])
    gate = grants.authorize(deps.store, req, docker=deps.docker)
    if not gate.allowed and gate.entry is None and onboard_missing:
        service.onboard(deps, use.name, commit=use.commit)
        gate = grants.authorize(deps.store, req, docker=deps.docker)
    if not gate.allowed:
        if gate.draft is not None:
            try:
                checks = service.verify_patched(deps, gate.entry, req.patches)
            except ValueError as exc:  # a patch that does not apply never reaches the human
                raise Denied(str(exc)) from exc
            if any("FAILED" in c for c in checks):
                raise Denied("research-patched external code fails its import checks: " + "; ".join(checks)[:300])
            ar = grants.request_grant(deps.store, gate.entry, gate.draft, notifier=deps.notifier,
                                      diff=gate.reasons + checks)
            if ar.status.value in ("rejected", "revision_requested"):
                raise Denied(f"execution of {use.name} was {ar.status.value} by a human (request {ar.request_id[:8]}); "
                             "a changed request is needed")
            raise NeedsApproval(ar, gate.reasons)
        state = gate.entry.state.value if gate.entry else "missing"
        raise Denied(f"external code {use.name} cannot run ({state}): " + "; ".join(gate.reasons))
    code_dir, patches, code_sha = grants.prepare_code(gate.entry, req.patches)
    v = gate.entry.verification
    container_env = {k: v_ for k, v_ in env.items() if k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                                                              "NUMEXPR_NUM_THREADS", "PYTHONHASHSEED",
                                                              "RA_EXPERIMENT_RUN_ID", "RA_TASK_ID")}
    argv = deps.docker.research_argv(
        image=v.environment.image_id, name=f"ra-job-{re.sub(r'[^a-zA-Z0-9_.-]', '-', job_key)[:60]}",
        code_dir=code_dir, mount_as=use.mount_as, checkout=checkout, checkout_mode=use.job_checkout,
        data_paths=[], network=use.network, cpus=alloc.cpu, memory_gb=alloc.memory_gb,
        workdir=spec.execution.working_directory, env=container_env, python_dirs=v.survey.python_dirs,
        argv=["python", *spec.execution.argv[1:]])
    rec = grants.record_start(deps.store, gate, req, argv=argv, code_sha256=code_sha, patches=patches, job_key=job_key,
                              experiment_run_id=experiment_run_id, experiment_id=spec.experiment_id,
                              catalog=deps.catalog)
    return argv, {"external_execution_id": rec.execution_id, "external_entry": gate.entry.entry_id,
                  "external_url": gate.entry.identification.url, "external_commit": gate.entry.commit,
                  "external_code_sha256": code_sha, "external_image": v.environment.image_id,
                  "external_lock_sha256": v.environment.lock_sha256, "external_grant": gate.grant.grant_id,
                  "external_patches": ",".join(p["sha256"][:12] for p in patches)}


def finish_job(deps: service.ExternalDeps, job_key: str, status) -> None:
    for rec in registry.executions(deps.store, job_key=job_key):
        if rec.outcome != "running":
            continue
        wall = (status.finished_at - status.started_at).total_seconds() if status.started_at and status.finished_at else 0.0
        cpus = float(rec.requested.get("cpus") or 1)
        grants.record_finish(deps.store, rec, exit_code=status.exit_code, wall_seconds=wall, cpus=cpus,
                             detail=status.reason or "", catalog=deps.catalog)


# ---------------------------------------------------------------- Phase 5


def process_approvals(deps: service.ExternalDeps) -> list[dict]:
    return grants.process_decisions(deps.store, catalog=deps.catalog, notifier=deps.notifier,
                                    on_license=lambda e: service.resume_after_license(deps, e))


def refresh(deps: service.ExternalDeps) -> list[str]:
    """Re-verify repositories whose default branch moved (cooldown per repository)."""
    out = []
    for name in sorted({e.name for e in registry.entries(deps.store) if e.state is EntryState.REGISTERED}):
        try:
            new = service.check_update(deps, name)
        except Exception:
            continue
        if new is not None:
            out.append(f"{name}: {new.commit[:12]} {new.state.value}")
    return out
