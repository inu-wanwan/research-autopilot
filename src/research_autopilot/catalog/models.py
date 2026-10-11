"""Phase 7A typed records for the Research Artifact Catalog.

Everything here is repository-agnostic: repository-specific layout, naming and
schema live only in adapter definitions (``catalog.adapters``).  Records are
persisted as JSON in PostgreSQL (``storage.db``) so later phases (7B generated
adapters) can extend them without a migration of the indexed columns.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from research_autopilot.models import new_id, utcnow

SCHEMA = "1.0"


class AdapterKind(StrEnum):
    DECLARATIVE = "declarative"  # data-only definition interpreted by the catalog; no code execution
    PYTHON = "python"            # trusted module; activation always needs a human


class AdapterOrigin(StrEnum):
    MANUAL = "manual"        # written and registered by a human (Phase 7A)
    GENERATED = "generated"  # produced by an AI generator (Phase 7B)


class AdapterState(StrEnum):
    REGISTERED = "registered"  # stored, never used for discovery
    VALIDATED = "validated"    # passed automatic validation; still unusable until activated
    ACTIVE = "active"          # usable for discovery / resolution
    REJECTED = "rejected"      # failed validation (kept for audit)
    RETIRED = "retired"        # superseded or disabled (kept for audit)


class AdapterValidation(BaseModel):
    passed: bool
    checks: list[CheckResult] = Field(default_factory=list)
    repository_id: str | None = None  # repository the dry discovery ran against
    discovered: int = 0
    validated_at: datetime = Field(default_factory=utcnow)


class AdapterEvent(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    kind: str
    actor: str
    message: str


class AdapterRecord(BaseModel):
    """One immutable adapter version. A new definition is a new record, never an in-place edit."""

    schema_version: Literal["1.0"] = SCHEMA
    adapter_id: str  # f"{name}@{version}"
    name: str
    version: str
    kind: AdapterKind
    origin: AdapterOrigin
    definition: dict[str, Any] | None = None  # declarative body
    entrypoint: str | None = None             # python (manual, trusted package): "package.module:factory"
    python_source: str | None = None          # python (generated): module source, only ever run in a sandbox
    definition_hash: str
    state: AdapterState = AdapterState.REGISTERED
    generator: dict[str, Any] = Field(default_factory=dict)  # 7B: model / prompt hash / source scan
    validation: AdapterValidation | None = None
    activated_by: str | None = None
    endorsed_by: str | None = None  # human who reviewed a generated adapter's checks (scientific trust)
    history: list[AdapterEvent] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def event(self, kind: str, actor: str, message: str) -> None:
        self.history.append(AdapterEvent(kind=kind, actor=actor, message=message))
        self.updated_at = utcnow()


class RepositoryBinding(BaseModel):
    """Which adapter versions interpret a registered repository (several repos may share one)."""

    schema_version: Literal["1.0"] = SCHEMA
    repository_id: str
    adapter_ids: list[str] = Field(default_factory=list)
    bound_by: str
    updated_at: datetime = Field(default_factory=utcnow)


class ValidationStatus(StrEnum):
    UNVERIFIED = "unverified"      # discovered, checks not (yet) able to establish integrity
    VERIFIED = "verified"          # every mandatory check passed on this content hash
    INCONSISTENT = "inconsistent"  # a mandatory check failed
    INACCESSIBLE = "inaccessible"  # no readable location on the requesting environment
    AMBIGUOUS = "ambiguous"        # another artifact claims the same identity with different content
    STALE = "stale"                # content changed since it was verified


REUSABLE = {ValidationStatus.VERIFIED}


class CheckResult(BaseModel):
    name: str
    passed: bool
    mandatory: bool = True
    detail: str = ""
    validator: str = ""  # validator id / version, so results stay reproducible


class ArtifactLocation(BaseModel):
    environment: str           # e.g. "mac", "eagle"
    repository_root: str       # absolute root on that environment
    relative_path: str         # repository-relative directory (or file) of the artifact
    verified_content_hash: str | None = None  # last content hash observed on this environment
    verified_at: datetime | None = None


class Provenance(BaseModel):
    repository_id: str
    repository_url: str | None = None
    repository_commit: str | None = None  # HEAD of the repository at scan time (if git)
    adapter_id: str
    adapter_definition_hash: str
    rule: str                 # adapter rule that produced the artifact
    source: dict[str, Any] = Field(default_factory=dict)  # adapter-declared provenance (run id, config hash, ...)
    scan_id: str


class Artifact(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    artifact_id: str
    kind: str
    identity: dict[str, Any]   # the research condition this artifact represents
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance
    content_hash: str          # sha256 over sorted (member path, member sha256)
    member_count: int
    members_digest: dict[str, str] = Field(default_factory=dict)  # relative member path -> sha256
    locations: list[ArtifactLocation] = Field(default_factory=list)
    status: ValidationStatus = ValidationStatus.UNVERIFIED
    checks: list[CheckResult] = Field(default_factory=list)
    status_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def location_for(self, environment: str) -> ArtifactLocation | None:
        return next((loc for loc in self.locations if loc.environment == environment), None)


class GenerationAttempt(BaseModel):
    attempt: int
    adapter_id: str | None = None
    kind: str | None = None
    passed: bool = False
    problems: list[str] = Field(default_factory=list)
    cost_usd: float | None = None
    rationale: str = ""
    assumptions: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


class GenerationRun(BaseModel):
    """Phase 7B audit record of one autonomous adapter generation for a repository."""

    schema_version: Literal["1.0"] = SCHEMA
    generation_id: str = Field(default_factory=new_id)
    repository_id: str
    environment: str
    reason: str                     # why generation was needed (detection result)
    model: str | None = None
    survey_hash: str | None = None
    attempts: list[GenerationAttempt] = Field(default_factory=list)
    outcome: Literal["activated", "awaiting_human", "rejected", "skipped", "failed"] = "failed"
    adapter_id: str | None = None   # final adapter (activated or awaiting human)
    note: str = ""
    total_cost_usd: float = 0.0
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class ScanRecord(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    scan_id: str = Field(default_factory=new_id)
    repository_id: str
    environment: str
    adapter_ids: list[str]
    repository_commit: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    discovered: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    became_stale: int = 0  # previously catalogued artifacts no longer observed by this scan
    errors: list[str] = Field(default_factory=list)


class ArtifactQuery(BaseModel):
    """Requirement from a research task: what is needed, not where it is."""

    kind: str
    repository_id: str | None = None
    filters: dict[str, list[Any]] = Field(default_factory=dict)  # identity field -> accepted values
    expect_count: int | None = None  # every combination must resolve to exactly this many artifacts
    # "human": only artifacts verified by human-made / human-reviewed adapters may be reused (default).
    # "any": the requirement explicitly accepts checks written by an unreviewed generated adapter.
    trust: Literal["human", "any"] = "human"
    raw: str = ""


class ResolvedArtifact(BaseModel):
    artifact_id: str
    kind: str
    identity: dict[str, Any]
    content_hash: str
    path: str  # absolute directory of the artifact on the requesting environment
    files: list[str] = Field(default_factory=list)  # absolute member paths (read-only inputs)
    repository_id: str
    adapter_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)


class Resolution(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    environment: str
    queries: list[ArtifactQuery]
    artifacts: list[ResolvedArtifact] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    resolved_at: datetime = Field(default_factory=utcnow)

    @property
    def ok(self) -> bool:
        return not self.problems


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def content_hash(members: dict[str, str]) -> str:
    """Order-independent digest of an artifact's member files."""
    return stable_hash(sorted(members.items()))


def artifact_id_for(repository_id: str, kind: str, identity: dict[str, Any], content: str) -> str:
    # The content hash is part of the id: different bytes are never the same artifact.
    return "art_" + stable_hash({"repo": repository_id, "kind": kind, "identity": identity, "content": content})[:32]


AdapterValidation.model_rebuild()
