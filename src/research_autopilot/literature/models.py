"""Phase 8 typed records for evidence-grounded literature research.

A claim is ``verified`` only when its quotation was found, by deterministic
text matching, in the stored text of the cited source; everything else stays
``unverified`` (spec: never treat unconfirmed statements as facts).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from research_autopilot.models import new_id, utcnow

SCHEMA = "1.0"


class SourceType(StrEnum):
    ORIGINAL_PAPER = "original_paper"
    REVIEW = "review"
    OFFICIAL_DOC = "official_doc"
    REFERENCE_IMPLEMENTATION = "reference_implementation"
    OTHER = "other"


class Candidate(BaseModel):
    """A search hit before selection (bibliographic data only)."""

    candidate_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None      # without version, e.g. "1806.08838"
    arxiv_version: str | None = None  # e.g. "v2"
    url: str | None = None
    venue: str | None = None
    abstract: str | None = None
    found_by: list[str] = Field(default_factory=list)  # services that returned it
    oa_pdf_url: str | None = None
    is_oa: bool | None = None
    repo_full_name: str | None = None  # GitHub "owner/name" for implementations
    doc_url: str | None = None         # official documentation page (HTML or PDF) on an allowlisted host
    stars: int | None = None
    kind_hint: SourceType | None = None
    related_ids: list[str] = Field(default_factory=list)  # other versions (preprint <-> published)


class Retrieval(BaseModel):
    method: Literal["zotero_existing", "zotero_connector", "open_access_download", "repository_snapshot",
                    "document_snapshot", "none"] = "none"
    status: Literal["ok", "failed", "not_attempted", "requires_human"] = "not_attempted"
    detail: str = ""
    retrieved_at: datetime | None = None
    sha256: str | None = None
    local_path: str | None = None   # where RA reads the bytes (private; never published)
    origin_url: str | None = None   # where the bytes came from
    zotero_item_key: str | None = None
    pages: int | None = None
    access: Literal["open_access", "subscription", "unknown"] = "unknown"


class Source(BaseModel):
    source_id: str
    source_type: SourceType
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    version: str | None = None       # arXiv vN, publisher version, or repository commit
    url: str | None = None
    venue: str | None = None
    selection_reason: str = ""
    retrieval: Retrieval = Field(default_factory=Retrieval)
    other_versions: list[str] = Field(default_factory=list)


class ClaimKind(StrEnum):
    METHOD = "method"
    EQUATION = "equation"
    ALGORITHM = "algorithm"
    EXPERIMENTAL_CONDITION = "experimental_condition"
    METRIC = "metric"
    CONSTRAINT = "constraint"
    RESULT = "result"
    IMPLEMENTATION_CHOICE = "implementation_choice"


class Claim(BaseModel):
    claim_id: str = Field(default_factory=lambda: "clm_" + new_id()[:12])
    kind: ClaimKind
    statement: str
    source_id: str
    quote: str                       # verbatim excerpt the statement rests on
    locator: str = ""                # page / section / file path:line
    origin: Literal["paper", "implementation", "documentation"]
    status: Literal["verified", "unverified"] = "unverified"
    verification: str = ""           # how the quote was (not) found
    page: int | None = None


class Conflict(BaseModel):
    topic: str
    claim_ids: list[str]
    description: str


class Failure(BaseModel):
    stage: Literal["search", "selection", "acquisition", "text_extraction", "analysis", "store", "notes"]
    target: str
    detail: str
    needs_human: bool = False


class StageRecord(BaseModel):
    stage: str
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    detail: str = ""
    cost_usd: float = 0.0


class LiteratureReport(BaseModel):
    """The reusable Research Artifact of one literature investigation."""

    schema_version: Literal["1.0"] = SCHEMA
    report_id: str = Field(default_factory=lambda: "lit_" + new_id()[:16])
    question: str
    task_id: str | None = None
    topic_key: str                   # normalized question + seeds, for reuse lookups
    created_at: datetime = Field(default_factory=utcnow)
    candidates: list[dict] = Field(default_factory=list)  # what the search returned (audit of discovery/selection)
    sources: list[Source] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    failures: list[Failure] = Field(default_factory=list)
    stages: list[StageRecord] = Field(default_factory=list)
    summary: str = ""
    models: list[str] = Field(default_factory=list)
    total_cost_usd: float = 0.0
    reused: bool = Field(default=False, exclude=True)  # set when returned from the store instead of researched

    @property
    def verified_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.status == "verified"]
