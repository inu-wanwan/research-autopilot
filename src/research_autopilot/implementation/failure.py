"""FailureReport (Phase 4 -> Phase 3) and the deterministic repair policy.

The error classification alone never authorizes a repair: the policy also
checks the round-trip limit, the requested repair scope (must stay inside the
originally approved scope and away from locked scientific inputs) and whether
the cause is clearly an implementation / environment problem. Anything that
could require changing the science goes to a human.
"""

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from research_autopilot.implementation.worktree import glob_to_regex, matches_any
from research_autopilot.models import utcnow

FAILURE_SCHEMA_VERSION = "1.0"
MAX_ROUNDTRIPS = 2


class ErrorClass(StrEnum):
    INFRASTRUCTURE = "infrastructure_failure"
    ENVIRONMENT = "environment_failure"
    IMPLEMENTATION = "implementation_failure"
    NUMERICAL = "numerical_failure"
    SCIENTIFIC_VALIDATION = "scientific_validation_failure"
    RESOURCE_EXCEEDED = "resource_exceeded"
    UNKNOWN = "unknown"


class FailureReport(BaseModel):
    schema_version: Literal["1.0"] = FAILURE_SCHEMA_VERSION
    report_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    created_at: datetime = Field(default_factory=utcnow)
    task_id: str
    phase3_run_id: str  # implementation run whose commit failed
    phase4_run_id: str
    failed_commit: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    experiment_spec_id: str
    argv: list[str]
    input_config: dict[str, str] = Field(default_factory=dict)  # input path -> sha / description (no secrets)
    exit_code: int | None
    signal: str | None = None
    error_class: ErrorClass
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=3)
    log_excerpt: str = Field(default="", max_length=8000)
    reproduction: list[str] = Field(min_length=1)
    allowed_repair_scope: list[str] = Field(min_length=1)  # globs Phase 4 asks to let Phase 3 modify
    scientific_invariants: list[str]
    roundtrip: int = Field(ge=1)  # 1 = first Phase 4 -> 3 return for this task


class RepairDecision(BaseModel):
    allowed: bool
    code: Literal["ok", "roundtrip_limit", "human_required", "phase4_responsibility", "scope_exceeds_approval",
                  "scope_touches_scientific_inputs", "invariants_mismatch", "commit_mismatch", "duplicate_report",
                  "original_run_invalid"]
    reason: str


AUTONOMOUS = {ErrorClass.IMPLEMENTATION, ErrorClass.ENVIRONMENT}
PHASE4 = {ErrorClass.INFRASTRUCTURE, ErrorClass.RESOURCE_EXCEEDED}
HUMAN = {ErrorClass.NUMERICAL, ErrorClass.SCIENTIFIC_VALIDATION, ErrorClass.UNKNOWN}

# Text hints that a failure may stem from the science rather than the code; an
# implementation/environment report that mentions these is escalated to a human.
SCIENCE_HINTS = re.compile(r"(converge|収束|basis|基底|geometry|分子構造|bond length|結合長|hamiltonian|"
                           r"tolerance|許容|acceptance|invariant|不変条件|objective|metric|評価指標)", re.I)


def decide(report: FailureReport, *, approved_scope: list[str], locked_inputs: list[str],
           original_invariants: list[str], persisted_roundtrips: int) -> RepairDecision:
    roundtrip = max(report.roundtrip, persisted_roundtrips + 1)
    if roundtrip > MAX_ROUNDTRIPS:
        return RepairDecision(allowed=False, code="roundtrip_limit",
                              reason=f"Phase 3<->4 round trip {roundtrip} exceeds the limit of {MAX_ROUNDTRIPS}")
    if report.error_class in PHASE4:
        return RepairDecision(allowed=False, code="phase4_responsibility",
                              reason=f"{report.error_class.value} is handled by Phase 4 (no code change by Phase 3)")
    if report.error_class in HUMAN:
        return RepairDecision(allowed=False, code="human_required",
                              reason=f"{report.error_class.value}: cause may be scientific; human decision required")
    if SCIENCE_HINTS.search(report.summary + " " + report.log_excerpt[:2000]):
        return RepairDecision(allowed=False, code="human_required",
                              reason="failure mentions scientific conditions (convergence/basis/geometry/metrics); "
                                     "cannot rule out a scientific cause -> human decision required")
    if [s.strip() for s in report.scientific_invariants] != [s.strip() for s in original_invariants]:
        return RepairDecision(allowed=False, code="invariants_mismatch",
                              reason="report's scientific invariants differ from the approved task spec")
    outside = [g for g in report.allowed_repair_scope if not _glob_within(g, approved_scope)]
    if outside:
        return RepairDecision(allowed=False, code="scope_exceeds_approval",
                              reason=f"requested repair scope {outside} is outside the approved scope")
    touching = [p for p in locked_inputs if matches_any(p, report.allowed_repair_scope)]
    if touching:
        return RepairDecision(allowed=False, code="scope_touches_scientific_inputs",
                              reason=f"repair scope would allow changing scientific inputs {touching}")
    return RepairDecision(allowed=True, code="ok", reason=f"{report.error_class.value} within approved scope")


def _glob_within(glob: str, approved: list[str]) -> bool:
    """Conservative containment: the requested glob must equal an approved glob, or
    be a literal path / narrower pattern that an approved glob matches."""
    g = glob.strip().removeprefix("./")
    if g in approved:
        return True
    probe = g.replace("**/", "x/").replace("**", "x").replace("*", "x").replace("?", "x")
    return any(glob_to_regex(a).match(probe) for a in approved) and not g.startswith("**")
