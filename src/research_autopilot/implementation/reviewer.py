"""Code reviewer: deterministic checks + independent LLM review (read-only CLI session)."""

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from agents.agent_output import AgentOutputSchema
from pydantic import ValidationError

from research_autopilot.implementation.models import (
    CheckResult,
    CodeReviewResult,
    DangerFinding,
    DeterministicReport,
    ImplementationResult,
    ImplementationTask,
    ModelChoice,
    RepoConfig,
    ReviewOutcome,
    Usage,
)
from research_autopilot.implementation.prompts import reviewer_prompt
from research_autopilot.implementation.worktree import WorktreeManager, matches_any

DANGER_PATTERNS: list[tuple[str, str, str]] = [
    # (name, regex on added lines, severity)
    ("git push", r"\bgit\s+push\b", "block"),
    ("rm -rf on root/home", r"rm\s+-rf\s+(/|~|\$HOME)(\s|$)", "block"),
    ("sudo", r"(^|[\s;'\"`])sudo\s", "block"),
    ("pipe to shell", r"(curl|wget)[^|\n]*\|\s*(ba|z)?sh", "block"),
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "block"),
    ("api token", r"\b(sk-[A-Za-z0-9_-]{20,}|ntn_[A-Za-z0-9]{20,}|lin_api_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,})", "block"),
    ("os.system", r"\bos\.system\(", "warn"),
    ("shell=True", r"shell\s*=\s*True", "warn"),
    ("chmod 777", r"chmod\s+(-R\s+)?777", "warn"),
    ("eval/exec", r"\b(eval|exec)\(", "warn"),
]


class ReviewerRunner(Protocol):
    """Runs one read-only review session and returns (result, parsed review or None)."""

    def __call__(self, task: ImplementationTask, choice: ModelChoice, prompt: str, schema: dict,
                 log_path: Path) -> tuple[ImplementationResult, CodeReviewResult | None]: ...


def review_schema() -> dict:
    return AgentOutputSchema(CodeReviewResult).json_schema()


def parse_review(text: str) -> CodeReviewResult | None:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return CodeReviewResult.model_validate(json.loads(m.group(0)))
    except (ValueError, ValidationError):
        return None


def scan_diff(diff: str) -> list[DangerFinding]:
    findings, current = [], ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+") and not line.startswith("+++"):
            for name, pat, sev in DANGER_PATTERNS:
                if re.search(pat, line[1:]):
                    findings.append(DangerFinding(file=current, pattern=name, line=line[1:200], severity=sev))
    return findings


def run_check(name: str, cmd: list[str] | None, cwd: Path, timeout: int) -> CheckResult:
    if not cmd:
        return CheckResult(name=name, command=None, ok=True, skipped=True, output_tail="not configured")
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except FileNotFoundError as exc:
        return CheckResult(name=name, command=cmd, ok=False, exit_code=None, output_tail=f"command not found: {exc}")
    except subprocess.TimeoutExpired:
        return CheckResult(name=name, command=cmd, ok=False, exit_code=None, output_tail=f"timed out after {timeout}s")
    return CheckResult(name=name, command=cmd, ok=p.returncode == 0, exit_code=p.returncode,
                       output_tail=((p.stdout or "") + (p.stderr or ""))[-3000:])


def deterministic_report(task: ImplementationTask, repo: RepoConfig, wt: WorktreeManager) -> tuple[DeterministicReport, str]:
    path = Path(task.worktree_path)
    changed = wt.changed_files(path, task.base_commit)
    diff = wt.diff(path, task.base_commit)
    out_of_scope = [f for f in changed if not matches_any(f, task.allowed_file_scope)]
    protected = [f for f in changed if matches_any(f, task.protected_paths)]
    checks = [run_check(n, c, path, repo.check_timeout_seconds) for n, c in (
        ("tests", repo.test_command), ("lint", repo.lint_command), ("typecheck", repo.typecheck_command))]
    syntax = [f for f in changed if f.endswith(".py") and (path / f).exists()]
    if syntax:
        checks.append(run_check("syntax", [sys.executable, "-m", "py_compile", *syntax], path, 120))
    return DeterministicReport(changed_files=changed, out_of_scope_files=out_of_scope, protected_violations=protected,
                               danger_findings=scan_diff(diff), checks=checks,
                               diff_stat=wt.diff_stat(path, task.base_commit)[-4000:]), diff


def review(task: ImplementationTask, repo: RepoConfig, wt: WorktreeManager, choice: ModelChoice,
           runner: ReviewerRunner, log_dir: Path, fallback: tuple[ModelChoice, ReviewerRunner] | None = None,
           on_event: Callable[[str], None] = lambda m: None) -> ReviewOutcome:
    det, diff = deterministic_report(task, repo, wt)

    if not det.scope_ok:
        cats = []
        if det.out_of_scope_files or det.protected_violations:
            cats.append("scope_violation")
        if any(f.severity == "block" for f in det.danger_findings):
            cats.append("dangerous_change")
        reasons = []
        if det.out_of_scope_files:
            reasons.append(f"files outside allowed scope: {det.out_of_scope_files}")
        if det.protected_violations:
            reasons.append(f"protected paths modified: {det.protected_violations}")
        reasons += [f"dangerous change in {f.file}: {f.pattern}" for f in det.danger_findings if f.severity == "block"]
        return ReviewOutcome(verdict="blocked", reason="unapproved change: " + "; ".join(reasons), categories=cats,
                             deterministic=det, llm=None, reviewer=None, revision_requests=[])
    if not det.changed_files:
        return ReviewOutcome(verdict="revise", reason="implementer produced no changes", categories=["no_changes"],
                             deterministic=det, llm=None, reviewer=None,
                             revision_requests=["No files were changed. Implement the task within the allowed scope."])
    failed = [c for c in det.checks if not c.ok and not c.skipped]
    if failed:
        # Deterministic failures alone decide; no LLM approval can override them.
        cats = [c.name if c.name in ("tests", "lint", "typecheck") else "tests" for c in failed]
        return ReviewOutcome(verdict="revise", reason="automated checks failed: " + ", ".join(c.name for c in failed),
                             categories=sorted(set(cats)), deterministic=det, llm=None, reviewer=None,
                             revision_requests=[f"`{' '.join(c.command or [])}` failed:\n{c.output_tail[-1500:]}"
                                                for c in failed])

    prompt = reviewer_prompt(task, diff, [c.model_dump(mode="json") for c in det.checks], det.changed_files)
    usage, cost = Usage(), 0.0
    cost_known = True
    parsed, used = None, None
    candidates = [(choice, runner)] + ([fallback] if fallback else [])
    for i, (ch, run) in enumerate(candidates):
        for attempt in range(2):  # one retry for invalid JSON
            result, parsed = run(task, ch, prompt, review_schema(), log_dir / f"review-{ch.backend}-{i}-{attempt}.log")
            usage = usage.add(result.usage)
            if result.cost_usd is None:
                cost_known = False
            else:
                cost += result.cost_usd
            if result.status != "completed":
                on_event(f"reviewer {ch.backend}/{ch.model} {result.status}: {result.error}")
                break
            if parsed is not None:
                used = ch
                break
            on_event(f"reviewer {ch.backend}/{ch.model} returned invalid review JSON (attempt {attempt + 1})")
        if parsed is not None:
            break
    common = dict(deterministic=det, reviewer=used, reviewer_usage=usage, reviewer_cost_usd=cost if cost_known else None)
    if parsed is None:
        return ReviewOutcome(verdict="blocked", reason="LLM reviewer unavailable or returned invalid output",
                             categories=[], llm=None, revision_requests=[], **common)
    requests = [f"[{f.severity}/{f.aspect}] {f.file}: {f.issue} -> {f.requested_change}" for f in parsed.findings
                if f.severity != "minor" or parsed.verdict == "revise"]
    if parsed.verdict == "blocked":
        return ReviewOutcome(verdict="blocked", reason="reviewer blocked: " + parsed.summary, categories=[],
                             llm=parsed, revision_requests=requests, **common)
    if parsed.invariant_violations:
        return ReviewOutcome(verdict="revise", reason="scientific invariants violated", categories=["invariant_violation"],
                             llm=parsed, revision_requests=[f"Scientific invariant violated: {v}" for v in parsed.invariant_violations] + requests,
                             **common)
    if parsed.verdict == "revise":
        return ReviewOutcome(verdict="revise", reason="reviewer requested changes: " + parsed.summary,
                             categories=["review_findings"], llm=parsed, revision_requests=requests, **common)
    return ReviewOutcome(verdict="pass", reason="deterministic checks passed and reviewer approved", categories=[],
                         llm=parsed, revision_requests=[], **common)
