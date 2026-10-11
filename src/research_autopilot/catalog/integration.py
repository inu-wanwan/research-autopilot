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
    if registry is None:
        from research_autopilot.implementation.config import RegistryError, load_phase3_settings, load_registry
        try:
            registry = load_registry(load_phase3_settings().repos_file)
        except RegistryError:
            registry = {}
    return CatalogContext(store=store, registry=registry, environment=environment_name())


def resolve_for_task(ctx: CatalogContext | None, input_artifacts: str | None) -> tuple[Resolution | None, list[str]]:
    """``(None, [])`` when the task declares no catalog requirement; problems otherwise fail closed."""
    try:
        queries = parse_requirements(input_artifacts)
    except ValueError as exc:
        return None, [f"invalid catalog requirement: {exc}"]
    if not queries:
        return None, []
    if ctx is None:
        return None, ["the task declares catalog requirements but no artifact catalog is configured"]
    res = resolve(ctx, queries, verify=True)
    return res, list(res.problems)


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
