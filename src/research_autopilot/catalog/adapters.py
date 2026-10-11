"""Adapter lifecycle: register -> validate -> activate (separate steps), versioned and audited.

The same path accepts hand-written adapters (Phase 7A) and AI-generated ones
(Phase 7B): only ``origin`` / ``generator`` differ, and the activation policy
decides whether a human is required.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from research_autopilot.catalog import declarative
from research_autopilot.catalog.declarative import DiscoveredGroup
from research_autopilot.catalog.models import (AdapterKind, AdapterOrigin, AdapterRecord, AdapterState,
                                               AdapterValidation, CheckResult, RepositoryBinding, stable_hash)
from research_autopilot.models import utcnow
from research_autopilot.storage import db

# Python adapters are imported only from RA's own reviewed package.  A generated
# Python adapter is never imported in-process (Phase 7B adds isolated validation).
TRUSTED_PYTHON_PREFIXES: tuple[str, ...] = ("research_autopilot.catalog.builtin.",)
AUTOPILOT_ACTOR = "autopilot"


class AdapterError(Exception):
    pass


class AdapterRuntime(Protocol):
    def discover(self, root: Path) -> tuple[list[DiscoveredGroup], list[str]]: ...


class _Declarative:
    def __init__(self, definition: dict[str, Any]) -> None:
        self.definition = declarative.parse(definition)

    def discover(self, root: Path) -> tuple[list[DiscoveredGroup], list[str]]:
        return declarative.discover(self.definition, root)


def register(store, *, kind: AdapterKind, origin: AdapterOrigin, actor: str,
             definition: dict[str, Any] | None = None, entrypoint: str | None = None,
             python_source: str | None = None, name: str | None = None, version: str | None = None,
             generator: dict[str, Any] | None = None) -> AdapterRecord:
    """Store a new immutable adapter version in state REGISTERED (idempotent for identical content)."""
    if kind is AdapterKind.DECLARATIVE:
        if definition is None or entrypoint is not None:
            raise AdapterError("a declarative adapter needs a definition and no entrypoint")
        name, version = definition.get("name"), definition.get("version")
        body: Any = definition
    elif origin is AdapterOrigin.GENERATED:
        if not python_source or entrypoint is not None or definition is not None or not name or not version:
            raise AdapterError("a generated python adapter needs name, version and source (no entrypoint)")
        body = {"python_source": python_source}
    else:
        if entrypoint is None or definition is not None or python_source is not None or not name or not version:
            raise AdapterError("a python adapter needs name, version and an entrypoint 'module:factory'")
        body = {"entrypoint": entrypoint}
    if not name or not version:
        raise AdapterError("adapter name and version are required")
    adapter_id = f"{name}@{version}"
    digest = stable_hash({"kind": kind.value, "body": body})
    existing = store.get(db.catalog_adapters, adapter_id)
    if existing:
        rec = AdapterRecord.model_validate(existing)
        if rec.definition_hash != digest:
            raise AdapterError(f"{adapter_id} already exists with different content; bump the version")
        return rec
    rec = AdapterRecord(adapter_id=adapter_id, name=name, version=version, kind=kind, origin=origin,
                        definition=definition, entrypoint=entrypoint, python_source=python_source,
                        definition_hash=digest,
                        generator=generator or {})
    rec.event("registered", actor, f"{kind.value} adapter from {origin.value}")
    _save(store, rec)
    return rec


def runtime(rec: AdapterRecord) -> AdapterRuntime:
    if rec.kind is AdapterKind.DECLARATIVE:
        return _Declarative(rec.definition or {})
    if rec.origin is AdapterOrigin.GENERATED:
        # Never imported in-process: discovery always runs in the OS sandbox (validation and after approval).
        from research_autopilot.catalog.sandbox import SandboxedPythonRuntime
        if not rec.python_source:
            raise AdapterError("generated python adapter has no source")
        return SandboxedPythonRuntime(rec.python_source)
    module, _, attr = (rec.entrypoint or "").partition(":")
    if not attr or not module.startswith(TRUSTED_PYTHON_PREFIXES):
        raise AdapterError(f"python adapter entrypoint {rec.entrypoint!r} is outside the trusted package")
    return getattr(importlib.import_module(module), attr)()


def validate(store, adapter_id: str, *, repository_id: str, root: Path, actor: str) -> AdapterRecord:
    """Static checks plus a read-only dry discovery against one registered repository."""
    rec = get(store, adapter_id)
    if rec.state not in (AdapterState.REGISTERED, AdapterState.REJECTED, AdapterState.VALIDATED):
        raise AdapterError(f"{adapter_id} is {rec.state.value}; only registered adapters are validated")
    checks: list[CheckResult] = []
    groups: list[DiscoveredGroup] = []
    try:
        rt = runtime(rec)
        checks.append(CheckResult(name="definition_schema", passed=True, validator="adapters@1"))
    except (AdapterError, ValidationError, ImportError, AttributeError, ValueError) as exc:
        checks.append(CheckResult(name="definition_schema", passed=False, detail=str(exc)[:500], validator="adapters@1"))
        rt = None
    if rt is not None:
        try:
            groups, errors = rt.discover(root)
            checks.append(CheckResult(name="dry_discovery", passed=bool(groups) and not errors,
                                      detail=(f"{len(groups)} artifact(s); errors: {errors[:3]}" if errors
                                              else f"{len(groups)} artifact(s)"), validator="adapters@1"))
        except Exception as exc:  # an adapter bug must never activate
            checks.append(CheckResult(name="dry_discovery", passed=False, detail=f"{type(exc).__name__}: {exc}"[:500],
                                      validator="adapters@1"))
    passed = all(c.passed for c in checks)
    rec.validation = AdapterValidation(passed=passed, checks=checks, repository_id=repository_id,
                                       discovered=len(groups))
    rec.state = AdapterState.VALIDATED if passed else AdapterState.REJECTED
    rec.event("validated" if passed else "rejected", actor, f"against {repository_id}: {len(groups)} artifact(s)")
    _save(store, rec)
    return rec


def requires_human(rec: AdapterRecord) -> bool:
    """Phase 7B policy hook: only generated *declarative* adapters may activate without a human."""
    return not (rec.kind is AdapterKind.DECLARATIVE and rec.origin is AdapterOrigin.GENERATED)


def activate(store, adapter_id: str, *, actor: str) -> AdapterRecord:
    rec = get(store, adapter_id)
    if rec.state is AdapterState.ACTIVE:
        return rec
    if rec.state is not AdapterState.VALIDATED or not (rec.validation and rec.validation.passed):
        raise AdapterError(f"{adapter_id} must pass validation before activation (state {rec.state.value})")
    if requires_human(rec) and (not actor or actor == AUTOPILOT_ACTOR):
        raise AdapterError(f"{adapter_id} requires explicit human activation")
    for other in store.find(db.catalog_adapters, name=rec.name, state=AdapterState.ACTIVE.value):
        old = AdapterRecord.model_validate(other)
        old.state = AdapterState.RETIRED
        old.event("retired", actor, f"superseded by {adapter_id}")
        _save(store, old)
    rec.state, rec.activated_by = AdapterState.ACTIVE, actor
    rec.event("activated", actor, "usable for discovery")
    _save(store, rec)
    return rec


def retire(store, adapter_id: str, *, actor: str, reason: str) -> AdapterRecord:
    rec = get(store, adapter_id)
    rec.state = AdapterState.RETIRED
    rec.event("retired", actor, reason)
    _save(store, rec)
    return rec


def bind(store, repository_id: str, adapter_names: list[str], *, actor: str, registry: dict) -> RepositoryBinding:
    if repository_id not in registry:
        raise AdapterError(f"repository {repository_id!r} is not in the research repository registry")
    binding = RepositoryBinding(repository_id=repository_id, adapter_ids=sorted(set(adapter_names)), bound_by=actor,
                                updated_at=utcnow())
    store.put(db.catalog_bindings, repository_id, binding.model_dump(mode="json"))
    return binding


def active_for(store, repository_id: str) -> list[AdapterRecord]:
    """Active adapter versions bound (by name) to a repository; unbound or inactive ones are never used."""
    raw = store.get(db.catalog_bindings, repository_id)
    if not raw:
        return []
    out = []
    for name in RepositoryBinding.model_validate(raw).adapter_ids:
        rows = store.find(db.catalog_adapters, name=name, state=AdapterState.ACTIVE.value)
        out.extend(AdapterRecord.model_validate(r) for r in rows)
    return out


def get(store, adapter_id: str) -> AdapterRecord:
    raw = store.get(db.catalog_adapters, adapter_id)
    if not raw:
        raise AdapterError(f"unknown adapter {adapter_id}")
    return AdapterRecord.model_validate(raw)


def _save(store, rec: AdapterRecord) -> None:
    store.put(db.catalog_adapters, rec.adapter_id, rec.model_dump(mode="json"), name=rec.name, state=rec.state.value)
