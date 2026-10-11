"""Research Artifacts of literature research, catalogued through the Phase 7 Artifact Catalog.

Reports and per-source records are immutable JSON files in RA's private literature store, which is registered
in the catalog as repository ``ra-literature`` with a built-in declarative adapter.  PDFs themselves stay in
Zotero / Drive (or the private OA cache); only paths and hashes are recorded, never shared bodies.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

from research_autopilot.catalog import adapters, service
from research_autopilot.catalog.models import AdapterKind, AdapterOrigin, AdapterState, ArtifactQuery, ValidationStatus
from research_autopilot.implementation.models import RepoConfig
from research_autopilot.literature import acquire
from research_autopilot.literature.acquire import sha256_file
from research_autopilot.literature.models import LiteratureReport, Source
from research_autopilot.models import utcnow

REPO_ID = "ra-literature"
BUILTIN_ACTOR = "ra-builtin"  # RA's own reviewed adapter, shipped with the code (not AI-generated)


def store_dir() -> Path:
    return acquire.CACHE / "store"

ADAPTER = {
    "name": "ra-literature", "version": "1.0.0",
    "description": "Research Autopilot literature reports and source records (Phase 8).",
    "artifacts": [
        {"rule": "report", "kind": "literature-report", "pattern": "reports/{report_id}.json",
         "fields": {"doc_report_id": "/report_id", "schema": "/schema_version", "topic_key": "/topic_key",
                    "question": "/question", "task_id": {"pointer": "/task_id", "required": False},
                    "created_at": "/created_at", "claims": "/claims",
                    "source_ids": {"pointer": "/sources", "item": "/source_id"}},
         "identity": ["report_id"], "metadata": ["topic_key", "question", "task_id", "created_at"],
         "checks": [{"type": "equals", "field": "schema", "value": "1.0"},
                    {"type": "equals_field", "field": "doc_report_id", "other": "report_id"},
                    {"type": "items_subset", "field": "claims", "item": "/source_id", "other": "source_ids"}]},
        {"rule": "source", "kind": "literature-source", "pattern": "sources/{source_key}.json",
         "fields": {"doc_key": "/source_key", "doi": {"pointer": "/doi", "required": False},
                    "arxiv_id": {"pointer": "/arxiv_id", "required": False},
                    "repo": {"pointer": "/repo", "required": False}, "version": {"pointer": "/version", "required": False},
                    "title": "/title", "source_type": "/source_type", "retrieval_status": "/retrieval/status",
                    "sha256": {"pointer": "/retrieval/sha256", "required": False}},
         "identity": ["source_key"],
         "metadata": ["doi", "arxiv_id", "repo", "version", "title", "source_type", "retrieval_status", "sha256"],
         "checks": [{"type": "equals_field", "field": "doc_key", "other": "source_key"},
                    {"type": "one_of", "field": "retrieval_status",
                     "value": ["ok", "failed", "requires_human", "not_attempted"]}]},
    ],
}


def repo_config() -> RepoConfig:
    store_dir().mkdir(parents=True, exist_ok=True)
    return RepoConfig(id=REPO_ID, path=str(store_dir()))


def with_literature(ctx: service.CatalogContext) -> service.CatalogContext:
    ctx.registry.setdefault(REPO_ID, repo_config())
    return ctx


def topic_key(question: str, seeds: list[str]) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", question.lower()).split()
    return "-".join(words)[:120] + ("|" + ",".join(sorted(s.lower() for s in seeds)) if seeds else "")


def source_key(src: Source, repo: str | None = None) -> str:
    if repo:
        return "repo-" + re.sub(r"[^a-z0-9]+", "-", f"{repo}@{(src.version or '')[:12]}".lower())
    if src.doi:
        return "doi-" + re.sub(r"[^a-z0-9]+", "-", src.doi.lower())
    if src.arxiv_id:
        return "arxiv-" + src.arxiv_id.replace(".", "-")
    return "src-" + re.sub(r"[^a-z0-9]+", "-", src.title.lower())[:80]


def save(ctx: service.CatalogContext, report: LiteratureReport, repos: dict[str, str]) -> list[str]:
    """Write the report and source records, catalogue them, return catalog artifact ids of the report."""
    with_literature(ctx)
    (store_dir() / "reports").mkdir(parents=True, exist_ok=True)
    (store_dir() / "sources").mkdir(parents=True, exist_ok=True)
    for src in report.sources:
        key = source_key(src, repos.get(src.source_id))
        doc = {"source_key": key, "repo": repos.get(src.source_id), **src.model_dump(mode="json")}
        path = store_dir() / "sources" / f"{key}.json"
        if not path.exists() or json.loads(path.read_text()).get("retrieval", {}).get("status") != "ok":
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    path = store_dir() / "reports" / f"{report.report_id}.json"
    path.write_text(report.model_dump_json(indent=1))
    ensure_adapter(ctx)
    service.scan(ctx, REPO_ID, actor=BUILTIN_ACTOR)
    arts = service.query(ctx.store, ArtifactQuery(kind="literature-report", repository_id=REPO_ID,
                                                  filters={"report_id": [report.report_id]}))
    return [a.artifact_id for a in arts]


def ensure_adapter(ctx: service.CatalogContext) -> None:
    store = ctx.store
    store.init_schema()
    rec = adapters.register(store, kind=AdapterKind.DECLARATIVE, origin=AdapterOrigin.MANUAL, actor=BUILTIN_ACTOR,
                            definition=ADAPTER)
    if rec.state is not AdapterState.ACTIVE:
        rec = adapters.validate(store, rec.adapter_id, repository_id=REPO_ID, root=store_dir(), actor=BUILTIN_ACTOR)
        if rec.state is AdapterState.VALIDATED:
            adapters.activate(store, rec.adapter_id, actor=BUILTIN_ACTOR)
    adapters.bind(store, REPO_ID, [ADAPTER["name"]], actor=BUILTIN_ACTOR, registry=ctx.registry)


def reusable_report(ctx: service.CatalogContext, key: str, max_age_days: float) -> LiteratureReport | None:
    with_literature(ctx)
    arts = service.query(ctx.store, ArtifactQuery(kind="literature-report", repository_id=REPO_ID,
                                                  filters={"topic_key": [key]}))
    fresh = [a for a in arts if a.status is ValidationStatus.VERIFIED
             and a.created_at >= utcnow() - timedelta(days=max_age_days)]
    for art in sorted(fresh, key=lambda a: a.created_at, reverse=True):
        path = store_dir() / "reports" / f"{art.identity['report_id']}.json"
        if path.is_file() and sha256_file(path) == next(iter(art.members_digest.values())):
            return LiteratureReport.model_validate_json(path.read_text())
    return None


def known_source(ctx: service.CatalogContext, *, doi: str | None = None, arxiv_id: str | None = None,
                 repo: str | None = None) -> dict | None:
    """A previously retrieved source whose bytes are still on disk with the recorded hash."""
    with_literature(ctx)
    filters = {"doi": [doi]} if doi else {"arxiv_id": [arxiv_id]} if arxiv_id else {"repo": [repo]} if repo else None
    if not filters:
        return None
    for art in service.query(ctx.store, ArtifactQuery(kind="literature-source", repository_id=REPO_ID, filters=filters)):
        doc = json.loads((store_dir() / "sources" / f"{art.identity['source_key']}.json").read_text())
        ret = doc.get("retrieval") or {}
        local = Path(ret.get("local_path") or "")
        if ret.get("status") == "ok" and local.exists():
            if local.is_file() and sha256_file(local) != ret.get("sha256"):
                continue
            return doc
    return None
