"""Failure triage: classify, then decide retry / Phase 3 repair / blocked.

Decisions are deterministic and conservative: only clearly infrastructural
failures are retried under identical scientific conditions; only clear
implementation / environment failures go to Phase 3; everything that might be
scientific (numerical, validation, unknown) is blocked for a human.
"""

import re

from research_autopilot.experiments.models import JobState, JobStatus, TriageDecision, ValidationResult
from research_autopilot.implementation.failure import SCIENCE_HINTS, ErrorClass

INFRA = re.compile(r"(No space left on device|Too many open files|Connection (reset|refused)|Temporary failure|"
                   r"Read-only file system|Input/output error|cannot allocate memory for thread|BrokenPipe)", re.I)
ENV = re.compile(r"(ModuleNotFoundError: No module named '(?P<mod>[\w.]+)'|ImportError: (cannot import|DLL load|"
                 r"lib\w+\.so)|GLIBC|symbol not found|version mismatch)", re.I)
NUMERIC = re.compile(r"(not converge|did not converge|convergence fail|nan|overflow|singular matrix|"
                     r"LinAlgError|FloatingPointError|ill-conditioned)", re.I)
TRACEBACK_FILE = re.compile(r'File "(?P<f>[^"]+)", line \d+')


def classify(status: JobStatus, validation: ValidationResult | None, stderr: str, repo_files: set[str]) -> TriageDecision:
    tail = stderr[-6000:]
    ev = [f"state={status.state.value} exit={status.exit_code} signal={status.signal} reason={status.reason}"]
    if status.state is JobState.RESOURCE_EXCEEDED or status.signal == 9 and "memory" in (status.reason or "memory"):
        return TriageDecision(error_class=ErrorClass.RESOURCE_EXCEEDED, action="retry_more_resources",
                              reason=status.reason or "killed (likely out of memory / walltime)", evidence=ev)
    if status.state is JobState.LOST:
        return TriageDecision(error_class=ErrorClass.INFRASTRUCTURE, action="blocked",
                              reason="job state unknown (lost); not resubmitted automatically", evidence=ev)
    if status.state is JobState.SUCCEEDED and status.exit_code == 0:
        if validation is not None and validation.status == "fail":
            missing_out = [c for c in validation.checks if c.kind in ("output_exists", "format") and c.status == "fail"]
            if missing_out:
                return TriageDecision(error_class=ErrorClass.IMPLEMENTATION, action="phase3_repair",
                                      reason="exit 0 but required outputs missing / unparsable",
                                      evidence=ev + [c.detail for c in missing_out])
            nonfinite = [c for c in validation.checks if c.kind == "finite" and c.status == "fail"]
            if nonfinite:
                return TriageDecision(error_class=ErrorClass.NUMERICAL, action="blocked",
                                      reason="non-finite values in outputs; cause may be scientific",
                                      evidence=ev + [c.detail for c in nonfinite])
            if any(c.kind == "input_integrity" and c.status == "fail" for c in validation.checks):
                return TriageDecision(error_class=ErrorClass.IMPLEMENTATION, action="blocked",
                                      reason="the job modified its scientific inputs", evidence=ev)
            return TriageDecision(error_class=ErrorClass.SCIENTIFIC_VALIDATION, action="blocked",
                                  reason="numeric checks / metrics failed: " + (validation.failure_reason or ""),
                                  evidence=ev)
        raise ValueError("classify() called for a successful, validated job")
    if INFRA.search(tail):
        return TriageDecision(error_class=ErrorClass.INFRASTRUCTURE, action="retry", reason="infrastructure error",
                              evidence=ev + [INFRA.search(tail).group(0)])
    m = ENV.search(tail)
    if m:
        mod = (m.groupdict().get("mod") or "").split(".")[0]
        in_repo = mod and any(f.split("/")[0].removesuffix(".py") == mod for f in repo_files)
        if in_repo:
            return TriageDecision(error_class=ErrorClass.IMPLEMENTATION, action="phase3_repair",
                                  reason=f"import of repository module {mod!r} failed", evidence=ev + [m.group(0)])
        return TriageDecision(error_class=ErrorClass.ENVIRONMENT, action="phase3_repair",
                              reason="environment / dependency problem", evidence=ev + [m.group(0)])
    if NUMERIC.search(tail) or SCIENCE_HINTS.search(tail[-2000:]):
        return TriageDecision(error_class=ErrorClass.NUMERICAL, action="blocked",
                              reason="numerical / scientific failure signature; human decision required",
                              evidence=ev + [(NUMERIC.search(tail) or SCIENCE_HINTS.search(tail)).group(0)])
    frames = [f for f in TRACEBACK_FILE.findall(tail)]
    if "Traceback" in tail and any(any(fr.endswith("/" + rf) or fr == rf for rf in repo_files) for fr in frames):
        return TriageDecision(error_class=ErrorClass.IMPLEMENTATION, action="phase3_repair",
                              reason="uncaught exception in repository code", evidence=ev + [tail.strip().splitlines()[-1][:200]])
    return TriageDecision(error_class=ErrorClass.UNKNOWN, action="blocked", reason="unclassified failure", evidence=ev)
