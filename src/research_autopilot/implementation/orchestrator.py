"""Implementation Orchestrator (Phase 3).

Deterministic control flow:
  approval gate -> repository / scope -> classification & routing -> worktree + lock
  -> attempts (implement -> commit -> review), revisions <= 2, escalation, Codex->Claude fallback
  -> fixed commit + ImplementationManifest + Phase 4 handoff.

The manifest is saved after every state change so a crashed run can be resumed
with ``implement(..., resume_run_id=...)`` from Git + manifest. An interrupted
process is recorded as ``interrupted`` -- never as success or failure.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from research_autopilot.catalog.integration import implementer_view
from research_autopilot.config import Settings
from research_autopilot.implementation import routing
from research_autopilot.implementation.backends.base import BackendRequest, CodingBackend, pid_alive, terminate_group
from research_autopilot.implementation.classify import Rules, classify
from research_autopilot.implementation.config import Phase3Settings
from research_autopilot.implementation.experiment import (
    EXPERIMENT_DEFINITION_PATH,
    ExperimentSpec,
    Identification,
    Validation,
    parse_definition,
    spec_lines,
    validate_definition,
)
from research_autopilot.implementation.failure import FailureReport, RepairDecision, decide
from research_autopilot.implementation.models import (
    AttemptRecord,
    CheckResult,
    BlockReason,
    ImplementationManifest,
    ImplementationResult,
    ImplementationTask,
    ModelChoice,
    Phase4Handoff,
    RepoConfig,
    ReviewOutcome,
    RunState,
    Usage,
    utcnow,
)
from research_autopilot.implementation.prompts import implementer_prompt
from research_autopilot.implementation.reviewer import ReviewerRunner, review
from research_autopilot.implementation.worktree import GitError, LockHeld, WorktreeManager, parse_scope
from research_autopilot.models import ExecutionHandoff, TaskSpec, new_id
from research_autopilot.storage.artifacts import write_json
from research_autopilot.storage.db import Store
from research_autopilot.tasks.errors import ConnectorError, TransientConnectorError
from research_autopilot.tasks.manager import TaskManager

FALLBACK_STATUSES = {"quota_exhausted", "rate_limited", "process_error", "transient_error"}
GLOBAL_LEASE = "phase3:__global__"

SPEC_FIELDS = ("purpose", "specification", "scientific_invariants", "acceptance_criteria", "repository",
               "file_scope", "non_goals", "input_artifacts")


@dataclass
class ImplDeps:
    settings: Phase3Settings
    phase2: Settings
    registry: dict[str, RepoConfig]
    store: Store
    manager: TaskManager | None  # for fresh Linear / Notion checks; None only in tests that skip them
    backends: dict[str, CodingBackend]
    reviewers: dict[str, ReviewerRunner]
    rules: Rules = field(default_factory=Rules)


@dataclass
class RepairContext:
    report: FailureReport
    original: ImplementationManifest
    original_spec: ExperimentSpec
    decision: RepairDecision

    def prompt_block(self) -> dict:
        r = self.report
        return {"report_id": r.report_id, "error_class": r.error_class.value, "failed_commit": r.failed_commit,
                "argv": r.argv, "exit_code": r.exit_code, "signal": r.signal, "summary": r.summary,
                "log_excerpt": r.log_excerpt[-6000:], "reproduction": r.reproduction,
                "locked_scientific_inputs": self.original_spec.locked_scientific_inputs()}


class _Stop(Exception):
    def __init__(self, state: RunState, code: str, message: str, classification: str | None = None) -> None:
        super().__init__(message)
        self.state, self.code, self.message, self.classification = state, code, message, classification


# ---------------------------------------------------------------- entry point


def implement(deps: ImplDeps, *, handoff_path: Path | None = None, task_id: str | None = None,
              resume_run_id: str | None = None, flow_run_id: str | None = None) -> ImplementationManifest:
    deps.store.init_schema()
    rctx = None
    if resume_run_id:
        manifest = deps.store.get_implementation(resume_run_id)
        if manifest is None:
            raise ValueError(f"unknown phase3 run {resume_run_id}")
        if manifest.state in (RunState.COMPLETED, RunState.BLOCKED, RunState.FAILED):
            raise ValueError(f"run {resume_run_id} already finished ({manifest.state.value}); start a new run")
        manifest.recovery_state = "resumed_after_interrupt"
        manifest.event("recovery", f"resuming from state {manifest.state.value}")
        handoff_path = Path(manifest.handoff_path) if manifest.handoff_path else None
        if manifest.failure_report_id:
            rctx = _load_repair_context(deps, manifest)
    else:
        if handoff_path is None:
            if task_id is None:
                raise ValueError("specify handoff_path, task_id or resume_run_id")
            handoff_path = latest_handoff(deps.phase2.state_dir, task_id)
        manifest = ImplementationManifest(task_id=task_id or "unknown",
                                          handoff_path=str(handoff_path) if handoff_path else None)
    return _execute(deps, manifest, handoff_path, resume=bool(resume_run_id), flow_run_id=flow_run_id, rctx=rctx)


def repair(deps: ImplDeps, report_path: Path, *, flow_run_id: str | None = None) -> ImplementationManifest:
    """Phase 4 -> Phase 3 repair entry point (docs/phase_specs/phase3.md §10).

    Idempotent per FailureReport: a report that was already accepted returns the
    existing repair run instead of starting another one."""
    deps.store.init_schema()
    report = FailureReport.model_validate_json(Path(report_path).read_text())
    existing = deps.store.find_implementation_by_failure_report(report.report_id)
    if existing is not None:
        existing.event("duplicate", f"failure report {report.report_id} already handled by this run; not re-run")
        return existing

    original = deps.store.get_implementation(report.phase3_run_id)
    manifest = ImplementationManifest(
        task_id=report.task_id, parent_run_id=report.phase3_run_id, failure_report_id=report.report_id,
        handoff_path=original.handoff_path if original else None,
        repair_round=max(report.roundtrip, (original.repair_round if original else 0) + 1),
    )
    run_dir = deps.phase2.state_dir / "implementation" / manifest.phase3_run_id
    manifest.artifact_paths["failure_report"] = str(_copy_report(report, run_dir))
    if original is not None:
        manifest.inherited_usage = original.inherited_usage.add(original.usage)
        manifest.inherited_cost_usd = round(original.inherited_cost_usd + original.cost_usd_known, 6)
    try:
        _save(deps, manifest, run_dir)  # unique failure_report_id: a concurrent duplicate fails here
    except Exception:
        existing = deps.store.find_implementation_by_failure_report(report.report_id)
        if existing is not None:
            return existing
        raise

    problem = _check_original(deps, report, original)
    if problem:
        _block(manifest, _Stop(RunState.BLOCKED, "repair_not_allowed", problem, "original_run_invalid"))
        _save(deps, manifest, run_dir)
        return manifest
    orig_spec = ExperimentSpec.model_validate_json(Path(original.artifact_paths["experiment_spec"]).read_text())
    decision = decide(report, approved_scope=[g for g in original.task.allowed_file_scope],
                      locked_inputs=orig_spec.locked_scientific_inputs(),
                      original_invariants=spec_lines(original.task.scientific_invariants),
                      persisted_roundtrips=original.repair_round)
    manifest.event("repair", f"decision {decision.code}: {decision.reason}")
    if not decision.allowed:
        _block(manifest, _Stop(RunState.BLOCKED, "repair_not_allowed", f"{decision.code}: {decision.reason}",
                               decision.code))
        _save(deps, manifest, run_dir)
        return manifest
    rctx = RepairContext(report=report, original=original, original_spec=orig_spec, decision=decision)
    return _execute(deps, manifest, Path(original.handoff_path), resume=False, flow_run_id=flow_run_id, rctx=rctx)


def _check_original(deps: ImplDeps, report: FailureReport, original: ImplementationManifest | None) -> str | None:
    if original is None:
        return f"unknown Phase 3 run {report.phase3_run_id}"
    if original.state is not RunState.COMPLETED or not original.implementation_commit:
        return f"Phase 3 run {original.phase3_run_id} is not a completed implementation"
    if original.task_id != report.task_id:
        return f"task mismatch: report {report.task_id}, run {original.task_id}"
    if not original.implementation_commit.startswith(report.failed_commit):
        return f"failed commit {report.failed_commit} is not the run's implementation commit"
    if original.experiment_spec_id != report.experiment_spec_id or "experiment_spec" not in original.artifact_paths:
        return f"experiment spec {report.experiment_spec_id} does not belong to run {original.phase3_run_id}"
    return None


def _copy_report(report: FailureReport, run_dir: Path) -> Path:
    return write_json(run_dir / "failure_report.json", report)


def _load_repair_context(deps: ImplDeps, manifest: ImplementationManifest) -> RepairContext:
    report = FailureReport.model_validate_json(Path(manifest.artifact_paths["failure_report"]).read_text())
    original = deps.store.get_implementation(manifest.parent_run_id)
    orig_spec = ExperimentSpec.model_validate_json(Path(original.artifact_paths["experiment_spec"]).read_text())
    decision = decide(report, approved_scope=original.task.allowed_file_scope,
                      locked_inputs=orig_spec.locked_scientific_inputs(),
                      original_invariants=spec_lines(original.task.scientific_invariants),
                      persisted_roundtrips=original.repair_round)
    return RepairContext(report=report, original=original, original_spec=orig_spec, decision=decision)


def _execute(deps: ImplDeps, manifest: ImplementationManifest, handoff_path: Path | None, *, resume: bool,
             flow_run_id: str | None, rctx: RepairContext | None) -> ImplementationManifest:
    resume_run_id = manifest.phase3_run_id if resume else None
    manifest.flow_run_id = flow_run_id or manifest.flow_run_id
    run_dir = deps.phase2.state_dir / "implementation" / manifest.phase3_run_id
    leases: list[str] = []
    lock = None
    if resume_run_id and manifest.task is not None:
        # Refuse (without touching the manifest or leases) if the original run is still alive.
        wt0 = WorktreeManager(deps.registry[manifest.task.repo_id], deps.settings.worktrees_dir)
        lock = wt0.acquire_lock(Path(manifest.task.worktree_path), {"run_id": manifest.phase3_run_id, "pid": os.getpid()})
    try:
        handoff = _load_handoff(handoff_path, manifest)
        manifest.task_id = handoff.task_id
        _approval_gate(deps, handoff, manifest)
        repo, scope = _resolve_repository(deps, handoff)
        ttl = deps.settings.task_wall_time_seconds + 600
        for key in (GLOBAL_LEASE, f"phase3:{handoff.task_id}"):
            if not deps.store.reclaim_or_acquire_lease(key, manifest.phase3_run_id, ttl):
                raise _Stop(RunState.BLOCKED, "concurrent_run", f"another Phase 3 run holds {key}")
            leases.append(key)

        wt = WorktreeManager(repo, deps.settings.worktrees_dir, audit=manifest.git_audit.append)
        task = _prepare_task(deps, handoff, manifest, repo, scope, wt, rctx)
        if lock is None:
            lock = wt.acquire_lock(Path(task.worktree_path), {"run_id": manifest.phase3_run_id, "pid": os.getpid()})
        _recover_interrupted(manifest, wt, task)
        _save(deps, manifest, run_dir)
        _attempt_loop(deps, manifest, task, repo, wt, run_dir, rctx)
    except _Stop as stop:
        _block(manifest, stop)
    except LockHeld as exc:
        _block(manifest, _Stop(RunState.BLOCKED, "concurrent_run", str(exc)))
    except GitError as exc:
        _block(manifest, _Stop(RunState.BLOCKED, "repository_invalid", str(exc)))
    except Exception as exc:
        manifest.state = RunState.FAILED
        manifest.failure_classification = f"unexpected:{type(exc).__name__}"
        manifest.event("error", f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if lock is not None:
            WorktreeManager.release_lock(lock)
        for key in leases:
            deps.store.release_lease(key, manifest.phase3_run_id)
        _save(deps, manifest, run_dir)
    return manifest


def latest_handoff(state_dir: Path, task_id: str) -> Path | None:
    files = sorted((state_dir / "handoffs" / task_id).glob("*.json"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


# ---------------------------------------------------------------- gates


def _load_handoff(path: Path | None, manifest: ImplementationManifest) -> ExecutionHandoff:
    if path is None or not path.exists():
        raise _Stop(RunState.BLOCKED, "handoff_invalid", f"no Phase 2 handoff found ({path})")
    try:
        handoff = ExecutionHandoff.model_validate_json(path.read_text())
    except ValueError as exc:
        raise _Stop(RunState.BLOCKED, "handoff_invalid", f"handoff does not validate: {str(exc)[:300]}") from exc
    manifest.phase2_plan_run_id, manifest.plan_id = handoff.plan_run_id, handoff.plan_id
    return handoff


def _approval_gate(deps: ImplDeps, h: ExecutionHandoff, manifest: ImplementationManifest) -> None:
    """Human execution approval = Phase 2 apply-mode handoff + Linear still In Progress
    (docs/phase_specs/phase3.md §2). Reviewer approval of the plan is necessary but not sufficient."""
    if h.phase2_mode != "apply":
        raise _Stop(RunState.BLOCKED, "not_approved",
                    f"handoff was produced in {h.phase2_mode or 'unknown'} mode; Phase 3 requires an apply-mode handoff")
    if not h.human_approval.approved or h.review.verdict != "approved":
        raise _Stop(RunState.BLOCKED, "not_approved", "handoff lacks human approval or an approved plan review")
    record = deps.store.get_run(h.plan_run_id)
    if record is None or record.decision is None or record.decision.status.value != "ready" \
            or record.decision.plan_id != h.plan_id:
        raise _Stop(RunState.BLOCKED, "not_approved", "no matching ready Phase 2 run record for this handoff")
    if record.decision.linear_update is None or record.decision.linear_update.action not in ("updated", "noop"):
        raise _Stop(RunState.BLOCKED, "not_approved", "Phase 2 did not move the Linear issue to In Progress")
    if deps.manager is None:
        manifest.event("gate", "fresh Linear/Notion check skipped (no connectors configured)")
        return
    try:
        bundle_issue = deps.manager.linear.get_issue(h.task_id)
        if bundle_issue.state_name != deps.phase2.in_progress_state_name:
            raise _Stop(RunState.BLOCKED, "not_approved",
                        f"Linear state is {bundle_issue.state_name}; Phase 3 requires {deps.phase2.in_progress_state_name}")
        pages = deps.manager.notion.find_page_ids_by_linear_id(h.task_id)
        if len(pages) != 1:
            raise _Stop(RunState.BLOCKED, "spec_changed", f"expected one Notion page for {h.task_id}, found {len(pages)}")
        fresh = TaskSpec.from_sources(bundle_issue, deps.manager.notion.get_task(pages[0]))
    except TransientConnectorError as exc:
        raise _Stop(RunState.BLOCKED, "transient_error", str(exc), "transient") from exc
    except ConnectorError as exc:
        raise _Stop(RunState.BLOCKED, "connector_error", str(exc)) from exc
    changed = [f for f in SPEC_FIELDS if (getattr(fresh, f) or None) != (getattr(h, f) or None)]
    if changed:
        raise _Stop(RunState.BLOCKED, "spec_changed",
                    f"Notion spec changed since planning ({changed}); re-run Phase 2 before implementing")
    manifest.event("gate", "approval gate passed (apply handoff, Linear In Progress, spec unchanged)")


def _resolve_repository(deps: ImplDeps, h: ExecutionHandoff) -> tuple[RepoConfig, list[str]]:
    repo_id = next((ln.strip().strip("-*` ").split()[0] for ln in (h.repository or "").splitlines()
                    if ln.strip().strip("-*` ")), None)
    if not repo_id:
        raise _Stop(RunState.BLOCKED, "repository_invalid", "task spec has no 'Repository' section")
    if repo_id not in deps.registry:
        raise _Stop(RunState.BLOCKED, "repository_invalid", f"repository {repo_id!r} is not in the registry")
    scope = parse_scope(h.file_scope)
    if not scope:
        raise _Stop(RunState.BLOCKED, "scope_missing", "task spec has no 'Allowed files' section")
    return deps.registry[repo_id], scope


def _prepare_task(deps: ImplDeps, h: ExecutionHandoff, manifest: ImplementationManifest, repo: RepoConfig,
                  scope: list[str], wt: WorktreeManager, rctx: RepairContext | None = None) -> ImplementationTask:
    if manifest.task is not None:  # resume: same branch / worktree / base commit
        wt.ensure_worktree(manifest.task.branch, Path(manifest.task.worktree_path), manifest.task.base_commit)
        return manifest.task
    info = wt.verify_repository()
    protected = list(repo.protected_paths)
    if rctx is not None:
        # Repair: start from the failed commit (kept as-is on its own branch), restricted to the
        # validated repair scope; scientific inputs become protected.
        base = rctx.original.implementation_commit
        if wt.git("cat-file", "-t", base, check=False).strip() != "commit":
            raise _Stop(RunState.BLOCKED, "repository_invalid", f"failed commit {base} not found in repository")
        info["base_commit"] = base
        scope = list(rctx.report.allowed_repair_scope)
        protected += rctx.original_spec.locked_scientific_inputs()
    # The experiment definition file is system-defined metadata and always in scope.
    scope = scope + ([EXPERIMENT_DEFINITION_PATH] if EXPERIMENT_DEFINITION_PATH not in scope else [])
    run8 = manifest.phase3_run_id[:8]
    branch = f"autopilot/{h.task_id.lower()}-{run8}"
    path = deps.settings.worktrees_dir / repo.id / f"{h.task_id}-{run8}"
    wt.ensure_worktree(branch, path, info["base_commit"])
    manifest.event("worktree", f"{branch} at {path} from {info['base_commit'][:12]}"
                   + (" (main checkout has uncommitted changes; left untouched)" if info["main_checkout_dirty"] else ""))
    task = ImplementationTask(
        task_id=h.task_id, phase2_plan_run_id=h.plan_run_id, plan_id=h.plan_id, phase3_run_id=manifest.phase3_run_id,
        repo_id=repo.id, repo_path=str(wt.repo_path), repo_url=repo.url, base_branch=repo.base_branch,
        base_commit=info["base_commit"], branch=branch, worktree_path=str(path.expanduser().resolve()),
        allowed_file_scope=scope, protected_paths=protected, purpose=h.purpose,
        specification=h.specification, scientific_invariants=h.scientific_invariants,
        acceptance_criteria=h.acceptance_criteria, non_goals=h.non_goals, input_artifacts=h.input_artifacts,
        artifact_requirements=h.artifact_requirements, plan=h.plan, sources=h.sources,
        catalog_artifacts=implementer_view(h.catalog_resolution),
        external_implementations=h.external_implementations,
    )
    manifest.task = task
    if rctx is not None:
        manifest.assessment = rctx.original.assessment
        manifest.implementer_choice = rctx.original.implementer_choice
        manifest.event("repair", f"repair round {manifest.repair_round} of {rctx.original.phase3_run_id} "
                       f"from {info['base_commit'][:12]}; scope {scope}")
    else:
        manifest.assessment = classify(
            specification=h.specification, plan_text=h.plan.model_dump_json(), invariants=h.scientific_invariants,
            acceptance=h.acceptance_criteria, scope_globs=scope, n_steps=len(h.plan.steps), rules=deps.rules)
        manifest.implementer_choice = routing.implementer_choice(deps.settings, manifest.assessment)
    manifest.event("routing", f"complexity={manifest.assessment.implementation_complexity} "
                   f"risk={manifest.assessment.scientific_risk} -> implementer {manifest.implementer_choice.model}, "
                   f"reviewer {routing.reviewer_choice(deps.settings, manifest.assessment).model}")
    return task


def _recover_interrupted(manifest: ImplementationManifest, wt: WorktreeManager, task: ImplementationTask) -> None:
    """A previous process died mid-attempt: stop any orphaned backend, keep its
    work (commit it on the task branch) and mark the attempt as interrupted."""
    if manifest.active_backend_pid and pid_alive(manifest.active_backend_pid):
        manifest.event("recovery", f"terminating orphaned {manifest.active_backend} process {manifest.active_backend_pid}")
        terminate_group(manifest.active_backend_pid)
    manifest.active_backend_pid, manifest.active_backend = None, None
    last = manifest.attempts[-1] if manifest.attempts else None
    if last is not None and (last.result is None or last.review is None):
        if last.result is None:
            last.result = ImplementationResult(backend=last.choice.backend, model=last.choice.model,
                                               status="process_error", exit_code=None, error="interrupted (process died)")
        last.recovered = True
        if last.commit is None:
            last.commit = wt.commit_all(Path(task.worktree_path),
                                        f"autopilot: {task.task_id} attempt {last.number} (recovered after interrupt)")
        manifest.event("recovery", f"attempt {last.number} interrupted; work preserved at {last.commit or 'no changes'}")


# ---------------------------------------------------------------- attempts


def _attempt_loop(deps: ImplDeps, m: ImplementationManifest, task: ImplementationTask, repo: RepoConfig,
                  wt: WorktreeManager, run_dir: Path, rctx: RepairContext | None = None) -> None:
    s = deps.settings
    started = m.created_at if m.recovery_state is None else utcnow()
    choice = m.attempts[-1].choice if m.attempts else m.implementer_choice
    last_reviewed = next((a.review for a in reversed(m.attempts) if a.review is not None), None)
    requests: list[str] = last_reviewed.revision_requests if last_reviewed else []

    # A recovered attempt has work but no review yet: review it before deciding anything.
    if m.attempts and m.attempts[-1].review is None:
        outcome = _review(deps, m, task, repo, wt, run_dir, m.attempts[-1], rctx)
        if _finish_on_review(deps, m, task, outcome, run_dir, wt, rctx):
            return
        requests = outcome.revision_requests

    while True:
        number = len(m.attempts) + 1
        if number > s.max_attempts or (number > 1 and m.revisions_used >= s.max_revisions):
            raise _Stop(RunState.BLOCKED, "revision_limit",
                        f"no approved implementation after {len(m.attempts)} attempts "
                        f"({m.revisions_used} revisions); human review required", "revision_limit")
        _check_budget(s, m, started)

        escalation_reason = None
        if number > 1:
            reviews = [a.review for a in m.attempts if a.review is not None]
            choice, escalation_reason = routing.escalation(s, choice, reviews)
        attempt = AttemptRecord(number=number, kind="initial" if number == 1 else "revision", choice=choice,
                                escalation_reason=escalation_reason)
        m.attempts.append(attempt)
        m.state = RunState.IMPLEMENTING
        if escalation_reason:
            m.event("escalation", escalation_reason)
        _save(deps, m, run_dir)

        attempt.result = _run_with_fallback(deps, m, task, wt, attempt, requests, run_dir, started, rctx)
        choice = attempt.choice  # may have switched backend
        attempt.diff_path = str(_save_diff(wt, task, run_dir, number))
        attempt.commit = wt.commit_all(Path(task.worktree_path),
                                       f"autopilot: {task.task_id} attempt {number} ({choice.backend}/{choice.model})")
        _save(deps, m, run_dir)

        outcome = _review(deps, m, task, repo, wt, run_dir, attempt, rctx)
        if _finish_on_review(deps, m, task, outcome, run_dir, wt, rctx):
            return
        requests = outcome.revision_requests


def _run_with_fallback(deps: ImplDeps, m: ImplementationManifest, task: ImplementationTask, wt: WorktreeManager,
                       attempt: AttemptRecord, requests: list[str], run_dir: Path, started,
                       rctx: RepairContext | None = None) -> ImplementationResult:
    s = deps.settings
    transient_retry_used = False
    while True:
        _check_budget(s, m, started)
        choice = attempt.choice
        backend = deps.backends[choice.backend]
        resume_context = _resume_context(m, task, wt) if (attempt.fallback_reason or attempt.number > 1) else None
        remaining = s.task_wall_time_seconds - int((utcnow() - started).total_seconds())
        req = BackendRequest(
            task=task, choice=choice,
            prompt=implementer_prompt(task, attempt.number, requests, resume_context,
                                      rctx.prompt_block() if rctx else None),
            timeout_seconds=max(remaining, 30), log_path=run_dir / f"attempt-{attempt.number}-{choice.backend}.log",
            max_budget_usd=min(s.claude_max_budget_usd_per_call,
                               max(s.max_cost_usd_per_run - m.cost_usd_known - m.inherited_cost_usd, 0.01))
            if choice.backend == "claude" else None,
        )

        def on_start(pid: int) -> None:
            m.active_backend_pid, m.active_backend = pid, choice.backend
            _save(deps, m, run_dir)

        m.backend_invocations += 1
        result = backend.run(req, on_start)
        m.active_backend_pid, m.active_backend = None, None
        _account(m, result.usage, result.cost_usd)
        m.event("backend", f"attempt {attempt.number} {choice.backend}/{choice.model}: {result.status}"
                + (f" ({result.error})" if result.error else ""))
        _save(deps, m, run_dir)

        if result.status in ("completed", "failed"):
            return result  # "failed" = agent ran but reported an error; the review decides
        if result.status == "timeout":
            raise _Stop(RunState.BLOCKED, "time_exceeded", f"task wall time {s.task_wall_time_seconds}s exceeded", "timeout")
        if result.status == "auth_error":
            raise _Stop(RunState.BLOCKED, "auth_error", f"{choice.backend} authentication failed: {result.error}", "auth")
        if result.status == "budget_exceeded":
            raise _Stop(RunState.BLOCKED, "budget_exceeded", f"{choice.backend} hit its budget cap", "budget")
        if result.status == "transient_error" and not transient_retry_used:
            transient_retry_used = True
            m.event("retry", f"{choice.backend} transient error; retrying once")
            continue
        if result.status in FALLBACK_STATUSES:
            _switch_to_fallback(deps, m, attempt, result, wt, task, run_dir)
            continue
        raise _Stop(RunState.BLOCKED, "backend_unavailable", f"{choice.backend}: {result.status}", result.status)


def _switch_to_fallback(deps: ImplDeps, m: ImplementationManifest, attempt: AttemptRecord, result: ImplementationResult,
                        wt: WorktreeManager, task: ImplementationTask, run_dir: Path) -> None:
    s = deps.settings
    fb = s.fallback_backend
    if fb is None or fb == attempt.choice.backend or m.fallback_used or s.max_fallbacks < 1:
        raise _Stop(RunState.BLOCKED, "fallback_unavailable",
                    f"{attempt.choice.backend} {result.status} and no fallback left", result.status)
    status = deps.backends[fb].status()
    if not (status.installed and status.authenticated):
        raise _Stop(RunState.BLOCKED, "fallback_unavailable", f"fallback {fb} not usable: {status.detail}", result.status)
    # Old process must be gone before another backend touches the worktree.
    if result.pid and pid_alive(result.pid):
        terminate_group(result.pid)
    if result.pid and pid_alive(result.pid):
        raise _Stop(RunState.BLOCKED, "fallback_unavailable", f"previous backend process {result.pid} still running")
    _save_diff(wt, task, run_dir, attempt.number, suffix=f"before-fallback-{attempt.choice.backend}")
    m.fallback_used = True
    attempt.fallback_reason = f"{attempt.choice.backend} {result.status}: {result.error or ''}".strip()
    attempt.choice = routing.choose(s, fb, attempt.choice.tier)
    m.event("fallback", f"switching to {fb}/{attempt.choice.model} ({attempt.fallback_reason})")
    _save(deps, m, run_dir)


def _review(deps: ImplDeps, m: ImplementationManifest, task: ImplementationTask, repo: RepoConfig,
            wt: WorktreeManager, run_dir: Path, attempt: AttemptRecord,
            rctx: RepairContext | None = None) -> ReviewOutcome:
    s = deps.settings
    m.state = RunState.REVIEWING
    _save(deps, m, run_dir)
    choice = routing.reviewer_choice(s, m.assessment)
    other = s.fallback_backend if s.fallback_backend and s.fallback_backend != choice.backend else None
    fallback = (routing.choose(s, other, choice.tier), deps.reviewers[other]) if other and other in deps.reviewers else None
    outcome = review(task, repo, wt, choice, deps.reviewers[choice.backend], run_dir, fallback=fallback,
                     on_event=lambda msg: m.event("review", msg),
                     extra_checks=lambda: [_experiment_check(task, wt)],
                     science_guard=(lambda: _science_guard(rctx, task, wt)) if rctx else None,
                     extra_instructions=_repair_review_note(rctx) if rctx else "")
    _account(m, outcome.reviewer_usage, outcome.reviewer_cost_usd if outcome.llm or outcome.reviewer_usage.total else 0.0)
    attempt.review = outcome
    m.changed_files = outcome.deterministic.changed_files
    m.event("review", f"attempt {attempt.number}: {outcome.verdict} ({outcome.reason[:200]})")
    _save(deps, m, run_dir)
    return outcome


def _finish_on_review(deps: ImplDeps, m: ImplementationManifest, task: ImplementationTask, outcome: ReviewOutcome,
                      run_dir: Path, wt: WorktreeManager, rctx: RepairContext | None = None) -> bool:
    if outcome.verdict == "pass":
        _complete(deps, m, task, outcome, run_dir, wt, rctx)
        return True
    if outcome.verdict == "blocked":
        code = "unapproved_change" if {"scope_violation", "dangerous_change", "science_change"} & set(
            outcome.categories) else "review_blocked"
        m.final_review = outcome
        raise _Stop(RunState.BLOCKED, code, outcome.reason, code)
    return False


def _complete(deps: ImplDeps, m: ImplementationManifest, task: ImplementationTask, outcome: ReviewOutcome,
              run_dir: Path, wt: WorktreeManager, rctx: RepairContext | None = None) -> None:
    commit = wt.head(Path(task.worktree_path))
    spec = _build_spec(task, wt, commit, rctx)
    m.implementation_commit = commit
    m.final_review = outcome
    spec_path = write_json(deps.phase2.state_dir / "experiment_specs" / task.task_id / f"{spec.spec_id}.json", spec)
    m.experiment_spec_id = spec.spec_id
    m.artifact_paths["experiment_spec"] = str(spec_path)
    m.state = RunState.COMPLETED
    manifest_path = run_dir / "manifest.json"
    handoff = Phase4Handoff(
        task_id=task.task_id, phase2_plan_run_id=task.phase2_plan_run_id, plan_id=task.plan_id,
        phase3_run_id=m.phase3_run_id, repository={"id": task.repo_id, "path": task.repo_path, "url": task.repo_url},
        branch=task.branch, base_commit=task.base_commit, implementation_commit=m.implementation_commit,
        changed_files=outcome.deterministic.changed_files, checks=outcome.deterministic.checks, reviewer_verdict="pass",
        unverified_items=outcome.llm.unverified_items if outcome.llm else [],
        scientific_invariants=task.scientific_invariants, acceptance_criteria=task.acceptance_criteria,
        plan=task.plan, manifest_path=str(manifest_path), sources=task.sources,
        experiment_spec_id=spec.spec_id, experiment_spec_path=str(spec_path),
        parent_run_id=m.parent_run_id, failure_report_id=m.failure_report_id,
    )
    path = deps.phase2.state_dir / "phase4_handoffs" / task.task_id / f"{m.phase3_run_id}.json"
    m.artifact_paths["phase4_handoff"] = str(write_json(path, handoff))
    m.event("complete", f"implementation commit {m.implementation_commit[:12]} on {task.branch} (not merged)")


# ---------------------------------------------------------------- helpers


def _check_budget(s: Phase3Settings, m: ImplementationManifest, started) -> None:
    tokens = m.usage.total + m.inherited_usage.total  # repairs never reset the budget
    cost = m.cost_usd_known + m.inherited_cost_usd
    if tokens >= s.max_tokens_per_run:
        raise _Stop(RunState.BLOCKED, "budget_exceeded",
                    f"token budget exhausted ({tokens} >= {s.max_tokens_per_run}, incl. earlier runs of this chain)",
                    "budget")
    if cost >= s.max_cost_usd_per_run:
        raise _Stop(RunState.BLOCKED, "budget_exceeded",
                    f"cost budget exhausted (${cost:.2f} >= ${s.max_cost_usd_per_run:.2f}, incl. earlier runs)", "budget")
    if (utcnow() - started).total_seconds() >= s.task_wall_time_seconds:
        raise _Stop(RunState.BLOCKED, "time_exceeded", f"task wall time {s.task_wall_time_seconds}s exceeded", "timeout")


def _account(m: ImplementationManifest, usage: Usage, cost: float | None) -> None:
    m.usage = m.usage.add(usage)
    if cost is None:
        m.cost_unknown_calls += 1
    else:
        m.cost_usd_known = round(m.cost_usd_known + cost, 6)


def _resume_context(m: ImplementationManifest, task: ImplementationTask, wt: WorktreeManager) -> dict:
    last_review = next((a.review for a in reversed(m.attempts) if a.review), None)
    return {
        "branch": task.branch,
        "head_commit": wt.head(Path(task.worktree_path)),
        "changed_files": wt.changed_files(Path(task.worktree_path), task.base_commit),
        "uncommitted_changes": wt.has_uncommitted(Path(task.worktree_path)),
        "previous_attempts": [{"number": a.number, "backend": a.choice.backend,
                               "status": a.result.status if a.result else None,
                               "summary": (a.result.summary if a.result else "")[:800]} for a in m.attempts[:-1]],
        "last_review": {"verdict": last_review.verdict, "reason": last_review.reason,
                        "checks": [c.model_dump(mode="json") for c in last_review.deterministic.checks]}
        if last_review else None,
        "plan_steps": [s.id + ": " + s.title for s in task.plan.steps],
    }


def _save_diff(wt: WorktreeManager, task: ImplementationTask, run_dir: Path, number: int, suffix: str = "") -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"attempt-{number}{'-' + suffix if suffix else ''}.diff"
    path.write_text(wt.diff(Path(task.worktree_path), task.base_commit))
    return path


def _block(m: ImplementationManifest, stop: _Stop) -> None:
    m.state = stop.state
    m.failure_classification = stop.classification or stop.code
    m.block_reasons.append(BlockReason(code=stop.code, message=stop.message))  # type: ignore[arg-type]
    m.event("blocked", f"{stop.code}: {stop.message}")


def _save(deps: ImplDeps, m: ImplementationManifest, run_dir: Path) -> None:
    m.updated_at = utcnow()
    path = run_dir / "manifest.json"
    m.artifact_paths["manifest"] = str(path)
    write_json(path, m)
    deps.store.save_implementation(m)


# ---------------------------------------------------------------- experiment spec


def _definition_errors(wt: WorktreeManager, commit: str):
    d, errors = parse_definition(wt.read_at(commit, EXPERIMENT_DEFINITION_PATH))
    if d is not None:
        errors = validate_definition(d, lambda p: wt.file_at(commit, p), lambda p: wt.dir_at(commit, p))
    return d, errors


def _experiment_check(task: ImplementationTask, wt: WorktreeManager) -> CheckResult:
    """Validate .autopilot/experiment.json against the committed tree (the commit Phase 4 would run)."""
    head = wt.head(Path(task.worktree_path))
    _, errors = _definition_errors(wt, head)
    return CheckResult(name="experiment_spec", command=None, ok=not errors,
                       output_tail="\n".join(errors) if errors else f"valid at {head[:12]}")


def _science_guard(rctx: RepairContext, task: ImplementationTask, wt: WorktreeManager) -> list[str]:
    """Repairs must not change the science: locked inputs, metrics, numeric checks, required outputs."""
    head = wt.head(Path(task.worktree_path))
    base = rctx.original.implementation_commit
    orig = rctx.original_spec
    locked = orig.locked_scientific_inputs()
    violations = [f"scientific input modified: {p}" for p in wt.changed_between(base, head) if p in locked]
    d, _ = parse_definition(wt.read_at(head, EXPERIMENT_DEFINITION_PATH))
    if d is not None:
        if [c.model_dump() for c in d.numeric_checks] != [c.model_dump() for c in orig.validation.numeric_checks]:
            violations.append("numeric checks in the experiment definition changed")
        if [x.model_dump() for x in d.metrics] != [x.model_dump() for x in orig.validation.metrics]:
            violations.append("evaluation metrics in the experiment definition changed")
        if {o.path for o in d.outputs if o.required} != {o.path for o in orig.outputs if o.required}:
            violations.append("required outputs changed")
        if sorted(i.path for i in d.inputs if i.scientific) != sorted(locked):
            violations.append("scientific input declarations changed")
    return violations


def _repair_review_note(rctx: RepairContext) -> str:
    return ("This change is a REPAIR of a previously approved implementation after an experiment failure "
            f"({rctx.report.error_class.value}: {rctx.report.summary[:500]}). Any change to the molecule, geometry, "
            "basis set, computed target, scientifically meaningful convergence criteria, metrics, acceptance "
            "criteria or scientific invariants must be reported in invariant_violations or as verdict blocked.")


def _build_spec(task: ImplementationTask, wt: WorktreeManager, commit: str,
                rctx: RepairContext | None) -> ExperimentSpec:
    d, errors = _definition_errors(wt, commit)
    if errors or d is None:
        raise _Stop(RunState.BLOCKED, "experiment_spec_invalid", "; ".join(errors[:5]) or "missing definition")
    return ExperimentSpec(
        spec_id=new_id(),
        identification=Identification(
            task_id=task.task_id, phase2_plan_run_id=task.phase2_plan_run_id, plan_id=task.plan_id,
            phase3_run_id=task.phase3_run_id, repository_id=task.repo_id, repository_url=task.repo_url,
            branch=task.branch, commit=commit),
        experiment_id=d.experiment_id, execution=d.execution, environment=d.environment, inputs=d.inputs,
        resources=d.resources, outputs=d.outputs, external_code=d.external_code,
        validation=Validation(
            acceptance_criteria=spec_lines(task.acceptance_criteria),
            scientific_invariants=spec_lines(task.scientific_invariants),
            metrics=d.metrics, numeric_checks=d.numeric_checks,
            output_integrity=[f"{o.path}: {o.validation}" for o in d.outputs if o.validation]),
        reproduction=[f"git checkout {commit}  # in repository {task.repo_id}", *d.reproduction],
        supersedes_spec_id=rctx.original_spec.spec_id if rctx else None,
        sources=task.sources,
    )


def check_execution_approval(store, manager, phase2, handoff: ExecutionHandoff) -> str | None:
    """Public form of the Phase 3 approval gate for reuse by Phase 4. Returns a block
    reason, or None when execution is approved."""
    from types import SimpleNamespace

    probe = ImplementationManifest(task_id=handoff.task_id)
    try:
        _approval_gate(SimpleNamespace(store=store, manager=manager, phase2=phase2), handoff, probe)
    except _Stop as stop:
        return f"{stop.code}: {stop.message}"
    return None
