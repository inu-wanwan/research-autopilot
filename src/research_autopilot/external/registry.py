"""External Repository Registry (PostgreSQL / SQLite records) mirrored into the Phase 7 Artifact Catalog.

The database is the source of truth.  Every entry, grant, human decision and execution is also written as an
immutable-per-version JSON record under ``<external>/registry`` which is catalogued as repository
``ra-external`` by the built-in adapter, so provenance, verification results and usage history are discoverable
next to the research artifacts that used them.
"""

from __future__ import annotations

import json
from pathlib import Path

from research_autopilot.catalog import adapters, service
from research_autopilot.catalog.models import AdapterKind, AdapterOrigin, AdapterState, ArtifactQuery
from research_autopilot.external.github import ext_root
from research_autopilot.external.models import (EntryState, ExecutionGrant, ExecutionRecord, GrantDecisionRecord,
                                                GrantStatus, RegistryEntry)
from research_autopilot.implementation.models import RepoConfig
from research_autopilot.models import utcnow
from research_autopilot.storage import db

REPO_ID = "ra-external"
BUILTIN_ACTOR = "ra-builtin"

ADAPTER = {
    "name": "ra-external", "version": "1.0.0",
    "description": "Research Autopilot external repository registry, execution grants and usage (Phase 9).",
    "artifacts": [
        {"rule": "entry", "kind": "external-implementation", "pattern": "entries/{entry_id}.json",
         "fields": {"doc_id": "/entry_id", "name": "/name", "commit": "/commit", "state": "/state",
                    "provider": "/identification/provider", "url": "/identification/url",
                    "license": {"pointer": "/verification/license/spdx", "required": False},
                    "verification": {"pointer": "/verification/status", "required": False},
                    "tree_sha256": {"pointer": "/verification/integrity/tree_sha256", "required": False}},
         "identity": ["entry_id"],
         "metadata": ["name", "commit", "state", "provider", "url", "license", "verification", "tree_sha256"],
         "checks": [{"type": "equals_field", "field": "doc_id", "other": "entry_id"},
                    {"type": "one_of", "field": "state",
                     "value": ["fetched", "verifying", "verified", "registered", "failed", "needs_human", "superseded"]}]},
        {"rule": "grant", "kind": "external-grant", "pattern": "grants/{grant_id}.json",
         "fields": {"doc_id": "/grant_id", "entry_id": "/draft/entry_id", "commit": "/draft/commit",
                    "status": "/status", "approved_by": "/approved_by", "approved_at": "/approved_at",
                    "purpose": "/draft/scope/purpose_detail", "network": "/draft/scope/network"},
         "identity": ["grant_id"],
         "metadata": ["entry_id", "commit", "status", "approved_by", "approved_at", "purpose", "network"],
         "checks": [{"type": "equals_field", "field": "doc_id", "other": "grant_id"}]},
        {"rule": "decision", "kind": "external-decision", "pattern": "decisions/{request_id}.json",
         "fields": {"doc_id": "/request_id", "entry_id": "/entry_id", "decision": "/decision", "actor": "/actor",
                    "decided_at": "/decided_at"},
         "identity": ["request_id"], "metadata": ["entry_id", "decision", "actor", "decided_at"],
         "checks": [{"type": "equals_field", "field": "doc_id", "other": "request_id"}]},
        {"rule": "execution", "kind": "external-execution", "pattern": "executions/{execution_id}.json",
         "fields": {"doc_id": "/execution_id", "entry_id": "/entry_id", "grant_id": "/grant_id", "commit": "/commit",
                    "task_id": {"pointer": "/task_id", "required": False}, "outcome": "/outcome",
                    "experiment_run_id": {"pointer": "/experiment_run_id", "required": False},
                    "research_repository": "/research_repository", "code_sha256": "/code_sha256"},
         "identity": ["execution_id"],
         "metadata": ["entry_id", "grant_id", "commit", "task_id", "outcome", "experiment_run_id",
                      "research_repository", "code_sha256"],
         "checks": [{"type": "equals_field", "field": "doc_id", "other": "execution_id"}]},
    ],
}


def registry_dir() -> Path:
    return ext_root() / "registry"


# ---------------------------------------------------------------- entries


def save_entry(store, entry: RegistryEntry, catalog=None) -> None:
    store.put(db.external_entries, entry.entry_id, entry.model_dump(mode="json"), name=entry.name,
              state=entry.state.value)
    _mirror(catalog, "entries", entry.entry_id, entry.model_dump(mode="json"))


def get_entry(store, entry_id: str) -> RegistryEntry | None:
    raw = store.get(db.external_entries, entry_id)
    return RegistryEntry.model_validate(raw) if raw else None


def entries(store, name: str | None = None) -> list[RegistryEntry]:
    rows = store.find(db.external_entries, name=name) if name else store.find(db.external_entries)
    return [RegistryEntry.model_validate(r) for r in rows]


def current_entry(store, name: str) -> RegistryEntry | None:
    """Latest *registered* commit of a repository; failed or unverified updates never become current."""
    reg = [e for e in entries(store, name) if e.state is EntryState.REGISTERED]
    return max(reg, key=_age) if reg else None


def _age(e: RegistryEntry) -> tuple:
    """Newest commit wins (an older commit verified later never becomes current)."""
    return (e.identification.commit_date or "", e.registered_at or e.retrieved_at)


def find_entry(store, name: str, commit: str | None) -> RegistryEntry | None:
    if not commit:
        return current_entry(store, name)
    hits = [e for e in entries(store, name) if e.commit.startswith(commit) and len(commit) >= 7]
    return hits[-1] if hits else None


def register(store, entry: RegistryEntry, catalog=None) -> RegistryEntry:
    """Automatic registration of a verified entry; the previous current entry becomes superseded."""
    if entry.verification is None or entry.verification.status != "passed":
        raise ValueError("only a verified entry can be registered")
    if entry.verification.license.decision != "auto_ok" and \
            (entry.verification.license.human_decision or {}).get("decision") != "approve":
        raise ValueError("license not cleared")
    prev = current_entry(store, entry.name)
    entry.state = EntryState.REGISTERED
    entry.registered_at = utcnow()
    entry.event("autopilot", "registered", "verified automatically; execution in the research environment still "
                                           "requires a human-approved grant")
    if prev is not None and prev.entry_id != entry.entry_id and _age(prev) > _age(entry):
        entry.state = EntryState.SUPERSEDED  # an older commit: registered for pinned use, never current
        entry.event("autopilot", "registered_as_older", f"current commit stays {prev.commit[:12]}")
    elif prev is not None and prev.entry_id != entry.entry_id:
        entry.previous_entry_id = prev.entry_id
        prev.state = EntryState.SUPERSEDED
        prev.event("autopilot", "superseded", f"newer verified commit {entry.commit[:12]} registered; existing grants "
                                              "stay bound to this commit")
        save_entry(store, prev, catalog)
    save_entry(store, entry, catalog)
    return entry


# ---------------------------------------------------------------- grants / decisions / executions


def save_grant(store, grant: ExecutionGrant, catalog=None) -> None:
    store.put(db.external_grants, grant.grant_id, grant.model_dump(mode="json"), entry_id=grant.draft.entry_id,
              name=grant.draft.name, status=grant.status.value)
    _mirror(catalog, "grants", grant.grant_id, grant.model_dump(mode="json"))


def grants(store, name: str | None = None, status: GrantStatus | None = None) -> list[ExecutionGrant]:
    where = {k: v for k, v in (("name", name), ("status", status.value if status else None)) if v}
    return [ExecutionGrant.model_validate(r) for r in store.find(db.external_grants, **where)]


def save_decision(store, rec: GrantDecisionRecord, catalog=None) -> None:
    store.put(db.external_decisions, rec.request_id, rec.model_dump(mode="json"), entry_id=rec.entry_id)
    _mirror(catalog, "decisions", rec.request_id, rec.model_dump(mode="json"))


def decisions(store, entry_id: str | None = None) -> list[GrantDecisionRecord]:
    rows = store.find(db.external_decisions, entry_id=entry_id) if entry_id else store.find(db.external_decisions)
    return [GrantDecisionRecord.model_validate(r) for r in rows]


def save_execution(store, rec: ExecutionRecord, catalog=None) -> None:
    store.put(db.external_executions, rec.execution_id, rec.model_dump(mode="json"), grant_id=rec.grant_id,
              entry_id=rec.entry_id, job_key=rec.job_key or rec.execution_id)
    _mirror(catalog, "executions", rec.execution_id, rec.model_dump(mode="json"))


def executions(store, *, grant_id: str | None = None, entry_id: str | None = None,
               job_key: str | None = None) -> list[ExecutionRecord]:
    where = {k: v for k, v in (("grant_id", grant_id), ("entry_id", entry_id), ("job_key", job_key)) if v}
    return [ExecutionRecord.model_validate(r) for r in store.find(db.external_executions, **where)]


# ---------------------------------------------------------------- catalog mirror


def repo_config() -> RepoConfig:
    registry_dir().mkdir(parents=True, exist_ok=True)
    return RepoConfig(id=REPO_ID, path=str(registry_dir()))


def with_external(ctx: service.CatalogContext) -> service.CatalogContext:
    ctx.registry.setdefault(REPO_ID, repo_config())
    return ctx


def _mirror(catalog, kind_dir: str, key: str, doc: dict) -> None:
    d = registry_dir() / kind_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=str))
    if catalog is not None:
        try:
            sync_catalog(catalog)
        except Exception:  # the DB record is authoritative; a catalog failure is retried on the next write
            pass


def ensure_adapter(ctx: service.CatalogContext) -> None:
    store = ctx.store
    store.init_schema()
    rec = adapters.register(store, kind=AdapterKind.DECLARATIVE, origin=AdapterOrigin.MANUAL, actor=BUILTIN_ACTOR,
                            definition=ADAPTER)
    if rec.state is not AdapterState.ACTIVE:
        rec = adapters.validate(store, rec.adapter_id, repository_id=REPO_ID, root=registry_dir(), actor=BUILTIN_ACTOR)
        if rec.state is AdapterState.VALIDATED:
            adapters.activate(store, rec.adapter_id, actor=BUILTIN_ACTOR)
    adapters.bind(store, REPO_ID, [ADAPTER["name"]], actor=BUILTIN_ACTOR, registry=ctx.registry)


def sync_catalog(ctx: service.CatalogContext) -> None:
    with_external(ctx)
    ensure_adapter(ctx)
    service.scan(ctx, REPO_ID, actor=BUILTIN_ACTOR)


def usage(ctx: service.CatalogContext, entry_id: str) -> list:
    """Catalogued executions of one entry (the 'used by' history)."""
    with_external(ctx)
    arts = service.query(ctx.store, ArtifactQuery(kind="external-execution", repository_id=REPO_ID,
                                                  filters={"entry_id": [entry_id]}))
    return [a for a in arts if a.status.value == "verified"]  # earlier versions of a record are stale
