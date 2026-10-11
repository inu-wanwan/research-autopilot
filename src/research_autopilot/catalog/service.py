"""Catalog operations: scan registered repositories, query, and resolve verified artifacts.

Nothing here knows any repository layout or scientific field: adapters supply
discovered groups and checks; the catalog only applies identity, provenance,
integrity and reuse rules.  Repositories are read, never written.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from research_autopilot.catalog import adapters
from research_autopilot.catalog.models import (REUSABLE, Artifact, ArtifactLocation, ArtifactQuery, Provenance,
                                               Resolution, ResolvedArtifact, ScanRecord, ValidationStatus,
                                               artifact_id_for, content_hash, stable_hash)
from research_autopilot.models import utcnow
from research_autopilot.storage import db

DEFAULT_ENVIRONMENT = "local"


@dataclass
class CatalogContext:
    """What the catalog needs from the host: storage, the repository registry and this environment's name."""

    store: Any
    registry: dict  # repo id -> RepoConfig
    environment: str = DEFAULT_ENVIRONMENT
    env_roots: dict[str, dict[str, str]] = field(default_factory=dict)  # test override: env -> repo -> root

    def root(self, repository_id: str, environment: str | None = None) -> Path | None:
        env = environment or self.environment
        if env in self.env_roots and repository_id in self.env_roots[env]:
            return Path(self.env_roots[env][repository_id]).expanduser()
        repo = self.registry.get(repository_id)
        if repo is None:
            return None
        locations = getattr(repo, "locations", {}) or {}
        if env in locations:
            return Path(locations[env]).expanduser()
        # The registry ``path`` is, by definition, where the repository lives on this host.
        return Path(repo.path).expanduser() if env == self.environment else None


def environment_name(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return (env.get("RA_CATALOG_ENVIRONMENT") or DEFAULT_ENVIRONMENT).strip()


# ---------------------------------------------------------------- scan


def identity_key(repository_id: str, kind: str, identity: dict[str, Any]) -> str:
    return stable_hash({"repo": repository_id, "kind": kind, "identity": identity})


def scan(ctx: CatalogContext, repository_id: str, *, actor: str = adapters.AUTOPILOT_ACTOR) -> ScanRecord:
    """Discover artifacts of one repository on this environment with its active, bound adapters."""
    store = ctx.store
    store.init_schema()
    active = adapters.active_for(store, repository_id)
    record = ScanRecord(repository_id=repository_id, environment=ctx.environment,
                        adapter_ids=[a.adapter_id for a in active])
    root = ctx.root(repository_id)
    if root is None or not root.is_dir():
        record.errors.append(f"repository root unavailable on {ctx.environment}: {root}")
        return _finish_scan(store, record)
    if not active:
        record.errors.append("no active adapter is bound to this repository")
        return _finish_scan(store, record)
    root = root.resolve()
    record.repository_commit = _git_head(root)
    repo = ctx.registry.get(repository_id)
    seen: set[str] = set()
    for rec in active:
        try:
            groups, errors = adapters.runtime(rec).discover(root)
        except Exception as exc:  # one broken adapter must not stop the others
            record.errors.append(f"{rec.adapter_id}: {type(exc).__name__}: {exc}"[:500])
            continue
        record.errors.extend(f"{rec.adapter_id}: {e}" for e in errors[:50])
        for g in groups:
            art = _upsert(ctx, repo, rec, g, root, record)
            seen.add(art.artifact_id)
    _mark_unobserved(ctx, repository_id, {a.adapter_id for a in active}, seen, record)
    return _finish_scan(store, record)


def _upsert(ctx: CatalogContext, repo, rec, g, root: Path, scan_record: ScanRecord) -> Artifact:
    digest = content_hash(g.members)
    aid = artifact_id_for(repo.id, g.kind, g.identity, digest)
    status, reason = _status(g.checks)
    location = ArtifactLocation(environment=ctx.environment, repository_root=str(root), relative_path=g.relative_dir,
                                verified_content_hash=digest, verified_at=utcnow())
    raw = ctx.store.get(db.catalog_artifacts, aid)
    if raw:
        art = Artifact.model_validate(raw)
        art.locations = [loc for loc in art.locations if loc.environment != ctx.environment] + [location]
        art.checks, art.status, art.status_reason = g.checks, status, reason
        art.updated_at = utcnow()
    else:
        prov = Provenance(repository_id=repo.id, repository_url=getattr(repo, "url", None),
                          repository_commit=scan_record.repository_commit, adapter_id=rec.adapter_id,
                          adapter_definition_hash=rec.definition_hash, rule=g.rule, source=g.provenance,
                          scan_id=scan_record.scan_id)
        art = Artifact(artifact_id=aid, kind=g.kind, identity=g.identity, metadata=g.metadata, provenance=prov,
                       content_hash=digest, member_count=len(g.members), members_digest=g.members,
                       locations=[location], status=status, checks=g.checks, status_reason=reason)
    _save_artifact(ctx.store, art)
    scan_record.discovered += 1
    scan_record.by_status[art.status.value] = scan_record.by_status.get(art.status.value, 0) + 1
    return art


def _status(checks) -> tuple[ValidationStatus, str | None]:
    """Verified only when the adapter declared mandatory checks and every one of them passed."""
    failed = [c for c in checks if c.mandatory and not c.passed]
    if failed:
        return ValidationStatus.INCONSISTENT, "; ".join(f"{c.name}: {c.detail}" for c in failed)[:1000]
    declared = [c for c in checks if c.mandatory and c.name != "required_fields"]
    if not declared:
        return ValidationStatus.UNVERIFIED, "adapter declares no mandatory integrity check for this artifact"
    return ValidationStatus.VERIFIED, None


def _mark_unobserved(ctx: CatalogContext, repository_id: str, adapter_ids: set[str], seen: set[str],
                     record: ScanRecord) -> None:
    """Artifacts previously found here but absent now lose this environment's location."""
    for raw in ctx.store.find(db.catalog_artifacts, repository_id=repository_id):
        art = Artifact.model_validate(raw)
        if art.artifact_id in seen or art.provenance.adapter_id not in adapter_ids:
            continue
        if art.location_for(ctx.environment) is None:
            continue
        art.locations = [loc for loc in art.locations if loc.environment != ctx.environment]
        if not art.locations:
            art.status = ValidationStatus.STALE
            art.status_reason = f"not observed by scan {record.scan_id} on {ctx.environment} (changed or removed)"
        art.updated_at = utcnow()
        _save_artifact(ctx.store, art)


def _finish_scan(store, record: ScanRecord) -> ScanRecord:
    record.finished_at = utcnow()
    store.put(db.catalog_scans, record.scan_id, record.model_dump(mode="json"), repository_id=record.repository_id)
    return record


def _git_head(root: Path) -> str | None:
    try:
        p = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                           timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def _save_artifact(store, art: Artifact) -> None:
    store.put(db.catalog_artifacts, art.artifact_id, art.model_dump(mode="json"), kind=art.kind,
              repository_id=art.provenance.repository_id,
              identity_key=identity_key(art.provenance.repository_id, art.kind, art.identity),
              status=art.status.value)


# ---------------------------------------------------------------- query / resolve


_RANGE = re.compile(r"^(-?\d+)\.\.(-?\d+)$")


def parse_requirements(text: str | None) -> list[ArtifactQuery]:
    """Structured lines in a task's Input artifacts section::

        catalog: kind=<kind> repository=<repo> <field>=<v1>,<v2> k=2..10 expect=81

    Prose lines are ignored here (they stay human context for the planner).
    """
    out: list[ArtifactQuery] = []
    for line in (text or "").splitlines():
        body = line.strip().lstrip("-*").strip().strip("`")
        if not body.lower().startswith("catalog:"):
            continue
        q: dict[str, Any] = {"filters": {}, "raw": body}
        for token in body.split(":", 1)[1].split():
            if "=" not in token:
                raise ValueError(f"catalog requirement token without '=': {token!r}")
            key, value = token.split("=", 1)
            if key == "kind":
                q["kind"] = value
            elif key == "repository":
                q["repository_id"] = value
            elif key == "expect":
                q["expect_count"] = int(value)
            elif m := _RANGE.match(value):
                lo, hi = int(m.group(1)), int(m.group(2))
                q["filters"][key] = list(range(lo, hi + 1))
            else:
                q["filters"][key] = [_scalar(v) for v in value.split(",") if v]
        if "kind" not in q:
            raise ValueError(f"catalog requirement needs kind=: {body!r}")
        out.append(ArtifactQuery(**q))
    return out


def _scalar(v: str) -> Any:
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def _lookup(art: Artifact, key: str) -> tuple[bool, Any]:
    for source in (art.identity, art.metadata, art.provenance.source):
        if key in source:
            return True, source[key]
    return False, None


def _matches(value: Any, accepted: list[Any]) -> bool:
    if isinstance(value, str):
        # Allow an unambiguous prefix for long hashes / commit ids (>= 7 characters).
        return any(value == str(a) or (len(str(a)) >= 7 and value.startswith(str(a))) for a in accepted)
    return any(value == a or str(value) == str(a) for a in accepted)


def query(store, q: ArtifactQuery) -> list[Artifact]:
    where: dict[str, Any] = {"kind": q.kind}
    if q.repository_id:
        where["repository_id"] = q.repository_id
    out = []
    for raw in store.find(db.catalog_artifacts, **where):
        art = Artifact.model_validate(raw)
        ok = True
        for key, accepted in q.filters.items():
            found, value = _lookup(art, key)
            if not found or not _matches(value, accepted):
                ok = False
                break
        if ok:
            out.append(art)
    return out


def resolve(ctx: CatalogContext, queries: list[ArtifactQuery], *, verify: bool = True) -> Resolution:
    """Map requirements to verified, unambiguous, readable artifacts on this environment (fail closed)."""
    res = Resolution(environment=ctx.environment, queries=queries)
    for q in queries:
        found = query(ctx.store, q)
        by_identity: dict[str, list[Artifact]] = {}
        for art in found:
            by_identity.setdefault(identity_key(art.provenance.repository_id, art.kind, art.identity), []).append(art)
        label = q.raw or q.kind
        if not found:
            res.problems.append(f"[{label}] no catalogued artifact matches")
            continue
        _missing_combinations(q, found, res, label)
        for group in by_identity.values():
            ident = group[0].identity
            if len(group) > 1:
                prov = sorted({stable_hash(a.provenance.source)[:8] for a in group})
                res.problems.append(f"[{label}] ambiguous: {len(group)} artifacts claim {ident} "
                                    f"(provenance variants {prov}); pin a provenance field to choose")
                continue
            art = group[0]
            problem = _usable(ctx, art, verify)
            if problem:
                res.problems.append(f"[{label}] {ident}: {problem}")
                continue
            loc = art.location_for(ctx.environment)
            root = ctx.root(art.provenance.repository_id) or Path(loc.repository_root)
            res.artifacts.append(ResolvedArtifact(
                artifact_id=art.artifact_id, kind=art.kind, identity=art.identity, content_hash=art.content_hash,
                path=str((root / loc.relative_path).resolve()),
                files=[str((root / rel).resolve()) for rel in sorted(art.members_digest)],
                repository_id=art.provenance.repository_id, adapter_id=art.provenance.adapter_id,
                metadata=art.metadata, provenance=art.provenance.source))
        if q.expect_count is not None and len(by_identity) != q.expect_count:
            res.problems.append(f"[{label}] expected {q.expect_count} distinct artifacts, found {len(by_identity)}")
    return res


def _missing_combinations(q: ArtifactQuery, found: list[Artifact], res: Resolution, label: str) -> None:
    """When every identity field is constrained, each requested combination must exist."""
    fields = list(found[0].identity)
    # Every requested value of a constrained identity field must be covered by some artifact.
    for f in (f for f in fields if f in q.filters):
        for wanted in q.filters[f]:
            if not any(_matches(str(a.identity.get(f)), [wanted]) for a in found):
                res.problems.append(f"[{label}] missing {f}={wanted}")
    if not fields or any(f not in q.filters for f in fields):
        return
    present = {tuple(str(a.identity[f]) for f in fields) for a in found}
    for combo in itertools.product(*(q.filters[f] for f in fields)):
        if not any(all(_matches(p, [c]) for p, c in zip(row, combo)) for row in present):
            res.problems.append(f"[{label}] missing {dict(zip(fields, combo))}")


def _usable(ctx: CatalogContext, art: Artifact, verify: bool) -> str | None:
    if art.status not in REUSABLE:
        return f"status {art.status.value}" + (f" ({art.status_reason})" if art.status_reason else "")
    loc = art.location_for(ctx.environment)
    if loc is None:
        return f"no location on environment {ctx.environment!r}"
    if loc.verified_content_hash != art.content_hash:
        return f"location on {ctx.environment!r} was not verified for this content"
    root = ctx.root(art.provenance.repository_id)
    if root is None or not root.is_dir():
        return f"repository root not accessible on {ctx.environment!r}"
    if verify:
        changed = _rehash(root.resolve(), art.members_digest)
        if changed:
            art.status = ValidationStatus.STALE
            art.status_reason = f"content changed on {ctx.environment} ({changed[0]})"
            art.updated_at = utcnow()
            _save_artifact(ctx.store, art)
            return f"status stale ({art.status_reason})"
    return None


def _rehash(root: Path, members: dict[str, str]) -> list[str]:
    changed = []
    for rel, digest in members.items():
        path = (root / rel).resolve()
        if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
            changed.append(f"{rel} missing")
            continue
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != digest:
            changed.append(f"{rel} modified")
    return changed


def get_artifact(store, artifact_id: str) -> Artifact | None:
    raw = store.get(db.catalog_artifacts, artifact_id)
    return Artifact.model_validate(raw) if raw else None
