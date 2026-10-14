"""Phase 9 typed records (docs/phase_specs/phase9.md).

Three things are kept strictly apart:

* ``RegistryEntry``   -- one external repository at one fixed commit, with its verification (the code is *known*);
* ``ExecutionGrant``  -- a human's permission to run exactly that entry within a scope (the code may *run*);
* ``ExecutionRecord`` -- one actual research execution under a grant (what *did* run).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from research_autopilot.models import new_id, utcnow

SCHEMA = "1.0"


def stable_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


# ---------------------------------------------------------------- identification


class Provider(StrEnum):
    OFFICIAL = "official"        # linked from the paper itself or published by the paper's organisation
    AUTHOR = "author"            # owned by one of the paper's authors
    THIRD_PARTY = "third_party"  # somebody else's implementation
    UNKNOWN = "unknown"          # no paper relation could be established


class PaperRef(BaseModel):
    title: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    authors: list[str] = Field(default_factory=list)
    literature_report_id: str | None = None  # Phase 8 report that proposed the repository
    source_id: str | None = None


class Identification(BaseModel):
    full_name: str                   # GitHub owner/name
    url: str
    owner: str
    owner_type: str | None = None    # User / Organization
    owner_name: str | None = None    # display name of the owner, when public
    description: str | None = None
    default_branch: str | None = None
    commit: str                      # the fixed commit everything below refers to
    commit_date: str | None = None
    fork_of: str | None = None
    archived: bool = False
    github_license: str | None = None  # SPDX id reported by the GitHub API (evidence only)
    paper: PaperRef | None = None
    provider: Provider = Provider.UNKNOWN
    provider_evidence: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- license


class LicenseAssessment(BaseModel):
    spdx: str | None = None                   # effective identifier (None = unknown)
    detected: dict[str, str] = Field(default_factory=dict)  # where -> identifier (github_api, LICENSE.md, headers)
    category: Literal["permissive", "weak_copyleft", "strong_copyleft", "network_copyleft", "public_domain",
                      "none", "unknown", "conflicting"] = "unknown"
    conditions: dict[str, str] = Field(default_factory=dict)  # use / modify / redistribute / attribution / ...
    intended_use: Literal["internal_research", "vendor_into_research_repo", "redistribute"] = "internal_research"
    decision: Literal["auto_ok", "needs_human", "incompatible"] = "needs_human"
    reasons: list[str] = Field(default_factory=list)
    human_decision: dict | None = None        # {"decision", "actor", "at", "comment"} when a human decided


# ---------------------------------------------------------------- dependencies / environment


class DependencySurvey(BaseModel):
    languages: dict[str, int] = Field(default_factory=dict)        # file extension -> count
    dependency_files: list[str] = Field(default_factory=list)
    declared: list[str] = Field(default_factory=list)              # requirement lines found in files
    imports: list[str] = Field(default_factory=list)               # third-party top-level imports in Python code
    local_modules: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)          # what will be installed (PyPI names/specs)
    python_version: str = "3.12"
    python_dirs: list[str] = Field(default_factory=list)           # directories put on PYTHONPATH
    install_steps: list[str] = Field(default_factory=list)         # e.g. setup.py found (never executed on the host)
    tests: list[str] = Field(default_factory=list)                 # discovered test files
    entrypoints: list[str] = Field(default_factory=list)           # example / main scripts
    unsupported: list[str] = Field(default_factory=list)           # runtimes RA cannot provide (MATLAB, CUDA, ...)
    notes: list[str] = Field(default_factory=list)


class Wheel(BaseModel):
    name: str
    version: str
    filename: str
    sha256: str


class LockedEnvironment(BaseModel):
    base_image: str                       # image reference with digest
    python_version: str
    platform: str                         # docker architecture (arm64 / amd64)
    wheels: list[Wheel] = Field(default_factory=list)
    lock_sha256: str = ""                 # hash of the exact wheel set
    image_id: str = ""                    # local image id (sha256:...) the code runs in
    image_tag: str = ""


class SafetyFinding(BaseModel):
    severity: Literal["critical", "warning", "info"]
    rule: str
    path: str
    line: int | None = None
    excerpt: str = ""


class SandboxLimits(BaseModel):
    cpus: float = 2.0
    memory_gb: float = 4.0
    timeout_s: int = 1200
    disk_gb: float = 2.0       # tmpfs size for writable scratch space
    pids: int = 256
    network: Literal["none"] = "none"


class TestRun(BaseModel):
    name: str
    kind: Literal["suite", "import", "smoke"]
    argv: list[str]
    exit_code: int | None = None
    duration_s: float = 0.0
    timed_out: bool = False
    passed: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""
    rationale: str = ""


class Integrity(BaseModel):
    files: int = 0
    verified_blobs: int = 0              # files whose git blob SHA-1 matches the commit's tree
    mismatches: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    tree_sha256: str = ""                # sha256 manifest of the fetched source (what later runs must match)
    ok: bool = False


class Verification(BaseModel):
    verification_id: str = Field(default_factory=lambda: "ver_" + new_id()[:12])
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    limits: SandboxLimits = Field(default_factory=SandboxLimits)
    integrity: Integrity = Field(default_factory=Integrity)
    license: LicenseAssessment = Field(default_factory=LicenseAssessment)
    survey: DependencySurvey = Field(default_factory=DependencySurvey)
    environment: LockedEnvironment | None = None
    findings: list[SafetyFinding] = Field(default_factory=list)
    tests: list[TestRun] = Field(default_factory=list)
    status: Literal["passed", "failed", "needs_human"] = "failed"
    failure_reasons: list[str] = Field(default_factory=list)
    unverified: list[str] = Field(default_factory=list)   # what the automated checks could not establish
    cost_usd: float = 0.0
    models: list[str] = Field(default_factory=list)
    attempts: list[dict] = Field(default_factory=list)       # every plan / environment / result (audit trail)
    smoke_code: dict[str, str] = Field(default_factory=dict)  # wrapper scripts RA wrote for the smoke runs


# ---------------------------------------------------------------- registry


class EntryState(StrEnum):
    FETCHED = "fetched"
    VERIFYING = "verifying"
    VERIFIED = "verified"           # passed every automatic check (transient: registration follows)
    REGISTERED = "registered"       # in the Registry; still NOT allowed to run in the research environment
    FAILED = "failed"               # never trusted
    NEEDS_HUMAN = "needs_human"     # license / provenance needs a decision
    SUPERSEDED = "superseded"       # an newer verified commit replaced it as the current entry


class EntryEvent(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    actor: str                       # "autopilot" or a human id
    action: str
    detail: str = ""


class RegistryEntry(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    entry_id: str                    # ext-<owner>-<name>@<commit12>
    name: str                        # owner/name
    commit: str
    state: EntryState = EntryState.FETCHED
    identification: Identification
    source_dir: str                  # private, read-only copy of the fixed commit
    retrieved_at: datetime = Field(default_factory=utcnow)
    origin_url: str = ""
    verification: Verification | None = None
    registered_at: datetime | None = None
    previous_entry_id: str | None = None   # entry this one would replace (update of the same repository)
    history: list[EntryEvent] = Field(default_factory=list)

    def event(self, actor: str, action: str, detail: str = "") -> None:
        self.history.append(EntryEvent(actor=actor, action=action, detail=detail[:500]))


# ---------------------------------------------------------------- execution grants


class FileAccess(BaseModel):
    """What the external code may see when it runs for research.

    ``job_checkout`` is RA's private per-job export of the research commit (never the live repository);
    ``data_paths`` are extra host directories mounted read-only and must be granted explicitly.
    """

    job_checkout: Literal["rw", "ro"] = "rw"
    data_paths: list[str] = Field(default_factory=list)


Purpose = Literal["baseline_comparison", "reproduction", "component_reuse", "data_generation", "analysis"]


class ExecutionScope(BaseModel):
    purposes: list[Purpose]                # approved uses (categories are compared, not free text)
    purpose_detail: str = ""               # what the human was told the code is for
    research_repositories: list[str] = Field(default_factory=list)  # empty = any registered research repository
    file_access: FileAccess = Field(default_factory=FileAccess)
    network: Literal["none", "unrestricted"] = "none"
    max_cpus: float = 4.0
    max_memory_gb: float = 8.0
    max_walltime_minutes: int = 120
    max_total_cpu_hours: float = 20.0
    max_gpus: int = 0                      # CUDA GPUs per run; 0 = no GPU is passed into the container
    # sha256 of the exact research modifications (patch files) the human approved; any other patch changes the
    # executed code and needs re-verification and re-approval
    approved_patches: list[str] = Field(default_factory=list)


class GrantDraft(BaseModel):
    """What a human is asked to approve: the exact code + environment + scope."""

    entry_id: str
    name: str
    commit: str
    tree_sha256: str
    lock_sha256: str
    image_id: str
    base_image: str
    python_version: str
    requirements: list[str]
    license: str | None
    provider: str
    scope: ExecutionScope
    verification_id: str
    requested_by_task: str | None = None
    reason: str = ""

    def approval_hash(self) -> str:
        d = self.model_dump(mode="json", exclude={"requested_by_task", "reason"})
        if not d["scope"].get("max_gpus"):
            d["scope"].pop("max_gpus", None)  # grants approved before GPUs existed keep their hash
        return stable_hash(d)


class GrantStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ExecutionGrant(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    grant_id: str = Field(default_factory=lambda: "grant_" + new_id()[:12])
    draft: GrantDraft
    approval_hash: str
    approved_by: str
    approved_at: datetime = Field(default_factory=utcnow)
    approval_request_id: str
    channel: str
    status: GrantStatus = GrantStatus.ACTIVE
    expires_at: datetime = Field(default_factory=lambda: utcnow() + timedelta(days=180))
    revoked_reason: str | None = None


class GrantDecisionRecord(BaseModel):
    """Audit row for every human decision on an execution grant (approve / reject / revise)."""

    request_id: str
    entry_id: str
    approval_hash: str
    decision: str
    actor: str
    channel: str
    comment: str = ""
    decided_at: datetime = Field(default_factory=utcnow)
    grant_id: str | None = None


class ExecutionRequest(BaseModel):
    """What one research execution asks for; checked against a grant before anything runs."""

    name: str
    commit: str | None = None
    purpose: Purpose
    purpose_detail: str = ""
    research_repository: str
    task_id: str | None = None
    network: Literal["none", "unrestricted"] = "none"
    file_access: FileAccess = Field(default_factory=FileAccess)
    cpus: float = 1.0
    memory_gb: float = 2.0
    walltime_minutes: int = 30
    gpus: int = 0
    patches: list[str] = Field(default_factory=list)   # absolute paths of patch files (research modifications)


class ExecutionRecord(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA
    execution_id: str = Field(default_factory=lambda: "exec_" + new_id()[:12])
    grant_id: str
    entry_id: str
    name: str
    commit: str
    image_id: str
    task_id: str | None = None
    research_repository: str
    experiment_run_id: str | None = None
    experiment_id: str | None = None
    job_key: str | None = None
    argv: list[str] = Field(default_factory=list)
    patches: list[dict] = Field(default_factory=list)   # {"path", "sha256"} research modifications applied
    code_sha256: str = ""                               # tree hash of the code actually mounted (after patches)
    requested: dict = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    exit_code: int | None = None
    cpu_hours: float = 0.0
    outcome: Literal["running", "succeeded", "failed", "denied"] = "running"
    detail: str = ""


class GateResult(BaseModel):
    allowed: bool
    grant: ExecutionGrant | None = None
    entry: RegistryEntry | None = None
    reasons: list[str] = Field(default_factory=list)
    draft: GrantDraft | None = None   # set when a (new) human approval would allow the request
