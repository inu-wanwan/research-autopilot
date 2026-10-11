"""Planner / Executor integration of the artifact catalog.

Phase 2 resolves a task's ``catalog:`` requirements before any paid planning
call and freezes the resolution in the ExecutionHandoff.  Phase 3 shows the
verified artifacts to the implementer as read-only inputs.  Phase 4 re-resolves
right before a job starts and refuses to run if any input changed since
planning (TOCTOU), then hands the job a manifest via ``RA_CATALOG_INPUTS``.
"""

from __future__ import annotations

import json
from pathlib import Path

from research_autopilot.catalog.models import ArtifactQuery, Resolution
from research_autopilot.catalog.service import CatalogContext, environment_name, parse_requirements, resolve

INPUTS_ENV = "RA_CATALOG_INPUTS"


def context_from_env(store, registry: dict | None = None) -> CatalogContext:
    import os
    if registry is None:
        from research_autopilot.implementation.config import RegistryError, load_phase3_settings, load_registry
        try:
            registry = load_registry(load_phase3_settings().repos_file)
        except RegistryError:
            registry = {}
    auto = (os.environ.get("RA_CATALOG_AUTO_GENERATE", "true").strip().lower() not in ("0", "false", "no"))
    budget = float(os.environ.get("RA_ADAPTER_GENERATION_BUDGET_USD") or 1.0)

    def factory():
        from research_autopilot.catalog.cli import build_generator
        return build_generator(budget)
    return CatalogContext(store=store, registry=registry, environment=environment_name(),
                          generator_factory=factory if auto else None)


def resolve_for_task(ctx: CatalogContext | None, input_artifacts: str | None) -> tuple[Resolution | None, list[str]]:
    """``(None, [])`` when the task declares no catalog requirement; problems otherwise fail closed.

    Before resolving, every repository named by a requirement is brought up to date without human steps:
    an adapter is generated and activated when needed (declarative only) and stale scans are refreshed.
    """
    try:
        queries = parse_requirements(input_artifacts)
    except ValueError as exc:
        return None, [f"invalid catalog requirement: {exc}"]
    if not queries:
        return None, []
    if ctx is None:
        return None, ["the task declares catalog requirements but no artifact catalog is configured"]
    upkeep: list[str] = []
    for repo_id in sorted({q.repository_id for q in queries if q.repository_id}):
        upkeep += ensure_repository(ctx, repo_id)
    res = resolve(ctx, queries, verify=True)
    problems = list(res.problems)
    if problems:
        problems += [f"catalog upkeep: {u}" for u in upkeep]  # why nothing usable was found
    return res, problems


def ensure_repository(ctx: CatalogContext, repository_id: str) -> list[str]:
    """Autonomous upkeep of one repository: generate an adapter if needed, then rescan if stale. Notes only."""
    from research_autopilot.catalog import adapters, generation
    from research_autopilot.catalog.models import ScanRecord
    from research_autopilot.catalog.service import _git_head, scan
    from research_autopilot.models import utcnow
    from research_autopilot.storage import db

    notes: list[str] = []
    if repository_id not in ctx.registry:
        return [f"{repository_id}: not in the research repository registry"]
    root = ctx.root(repository_id)
    if root is None or not root.is_dir():
        return [f"{repository_id}: repository root unavailable on {ctx.environment}"]
    need = generation.detect(ctx, repository_id)
    generated = False
    if need.needed and need.reason in ("unbound", "no_active_adapter", "scan_failed", "no_artifacts"):
        commit = _git_head(root.resolve())
        failed = generation.recently_failed(ctx.store, repository_id, commit, ctx.generation_cooldown_hours)
        if ctx.generator_factory is None:
            notes.append(f"{repository_id}: needs an adapter ({need.reason}) but autonomous generation is disabled")
        elif failed is not None:
            notes.append(f"{repository_id}: adapter generation failed for this commit at {failed.started_at:%Y-%m-%d %H:%M}"
                         f" ({failed.note}); not retried within {ctx.generation_cooldown_hours:g}h")
        else:
            run = generation.generate_adapter(ctx, repository_id, ctx.generator_factory(),
                                              max_attempts=ctx.generation_attempts, allow_python=False,
                                              notifier=ctx.notifier)
            notes.append(f"{repository_id}: adapter generation {run.outcome} ({run.note})")
            generated = run.outcome == "activated"
    scans = [ScanRecord.model_validate(r) for r in ctx.store.find(db.catalog_scans, repository_id=repository_id)]
    last = next((s for s in reversed(scans) if s.environment == ctx.environment), None)
    active = sorted(a.adapter_id for a in adapters.active_for(ctx.store, repository_id))
    head = _git_head(root.resolve())
    stale = (last is None or generated or sorted(last.adapter_ids) != active
             or (head is not None and last.repository_commit != head)
             or (head is None and last.finished_at is not None
                 and (utcnow() - last.finished_at).total_seconds() > ctx.scan_max_age_hours * 3600))
    if active and stale:
        rec = scan(ctx, repository_id)
        notes.append(f"{repository_id}: rescanned ({rec.discovered} artifact(s), {rec.by_status})")
    return notes


def planner_context(res: Resolution) -> str:
    """Compact, deterministic text appended to Input artifacts for the planner (paths stay out of the LLM)."""
    lines = [f"Resolved catalog artifacts ({len(res.artifacts)}, environment {res.environment}, all verified, "
             f"read-only; never modify or regenerate them):"]
    for a in res.artifacts[:200]:
        lines.append(f"- {a.kind} {json.dumps(a.identity, sort_keys=True)} id={a.artifact_id} "
                     f"sha256={a.content_hash[:12]}")
    if len(res.artifacts) > 200:
        lines.append(f"- ... {len(res.artifacts) - 200} more")
    return "\n".join(lines)


def implementer_view(resolution: dict | None) -> list[dict]:
    if not resolution:
        return []
    res = Resolution.model_validate(resolution)
    return [{"artifact_id": a.artifact_id, "kind": a.kind, "identity": a.identity, "content_hash": a.content_hash,
             "files": a.files, "metadata": a.metadata, "provenance": a.provenance} for a in res.artifacts]


def recheck(ctx: CatalogContext, frozen: dict) -> tuple[Resolution, list[str]]:
    """Re-resolve the planned requirements now; the artifact set and bytes must be unchanged."""
    planned = Resolution.model_validate(frozen)
    queries = [ArtifactQuery.model_validate(q) for q in planned.queries]
    now = resolve(ctx, queries, verify=True)
    problems = list(now.problems)
    before = {a.artifact_id: a.content_hash for a in planned.artifacts}
    after = {a.artifact_id: a.content_hash for a in now.artifacts}
    if not problems and before != after:
        problems.append("catalog inputs changed since planning; re-plan the task")
    return now, problems


def write_inputs_manifest(res: Resolution, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(res.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return path


def upstream_artifacts(store, task_id: str) -> list[dict]:
    """Validated outputs of the tasks this task depends on (Phase 6B / research ArtifactHandoff rows whose bytes
    still match), in the catalog manifest's artifact shape so one RA_CATALOG_INPUTS manifest carries both."""
    from research_autopilot.storage import db
    from research_autopilot.autopilot.dag import sha256_file
    rows = list(store.find(db.artifact_handoffs, consumer_task_id=task_id))
    # a research task also sees what earlier cycles of the same research produced (cycles are separate DAGs)
    run = next(iter(store.find(db.workflow_runs, task_id=task_id)), None)
    if run and run.get("research_cycle_id") and run.get("root_task_id"):
        parents = {p["parent_run_id"] for p in store.find(db.parent_runs, parent_task_id=run["root_task_id"])}
        rows += [r for r in store.find(db.artifact_handoffs) if r.get("parent_run_id") in parents]
    latest: dict[tuple[str, str], dict] = {}
    for row in rows:
        if row["producer_task_id"] == task_id:
            continue
        key = (row["producer_task_id"], row["artifact_id"])
        if row.get("validation_status") == "validated" and str(row.get("created_at", "")) >= str(
                latest.get(key, {}).get("created_at", "")):
            latest[key] = row
    out = []
    for (producer, name), row in sorted(latest.items()):
        path = Path(row["path"])
        if not path.is_file() or sha256_file(path) != row["content_hash"]:
            continue
        out.append({"artifact_id": f"{producer}:{name}", "kind": "upstream", "identity": {"producer_task_id": producer,
                    "name": name}, "content_hash": row["content_hash"], "files": [str(path.resolve())],
                    "metadata": row.get("scientific_metadata") or {},
                    "provenance": {"git_commit": row.get("git_commit"), "handoff_id": row.get("handoff_id")}})
    return out
