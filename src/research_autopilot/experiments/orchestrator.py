"""Experiment Orchestrator (Phase 4).

contract & approval -> resources -> schedule (independent experiments in parallel within caps,
dependent ones in order) -> submit (async, ledger) -> monitor (heartbeat) -> collect -> validate
-> triage (retry / more resources / Phase 3 repair / blocked) -> analyze -> propose -> review
-> scope -> ReviewedExperimentProposal -> Phase5Handoff.

Jobs run detached; this process only polls. If it dies, ``run_experiment(resume_run_id=...)``
reconciles the ledger with reality and continues -- nothing is resubmitted blindly.
Phase 4 never approves proposals and never writes to Notion / Linear.
"""

import hashlib
import json
import subprocess
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from research_autopilot.catalog.integration import INPUTS_ENV, context_from_env, recheck, upstream_artifacts
from research_autopilot.config import Settings
from research_autopilot.experiments import resources as res
from research_autopilot.experiments.analyst import Analyst, LlmBudgetError, ScientificReviewer
from research_autopilot.experiments.backends.base import BackendNotConfigured, ComputeBackend
from research_autopilot.experiments.config import Phase4Settings
from research_autopilot.experiments.environment import (EnvironmentError_, container_argv, container_provenance,
                                                       ensure_venv, export_commit, gpu_override, repository_output_mounts,
                                                       resolve_argv)
from research_autopilot.experiments.models import (
    AnalysisResult,
    ExperimentManifest,
    ExperimentProposal,
    ExperimentState,
    HostResources,
    JobRecord,
    JobSpec,
    JobState,
    JobStatus,
    LlmUsage,
    Phase5Handoff,
    ReviewedExperimentProposal,
    TriageDecision,
)
from research_autopilot.experiments.scope import assess, dedupe_key
from research_autopilot.experiments.triage import classify
from research_autopilot.experiments.validator import validate
from research_autopilot.implementation.experiment import (
    SECRET_PATTERNS,
    ExperimentDefinition,
    ExperimentSpec,
    validate_definition,
)
from research_autopilot.implementation.failure import FailureReport
from research_autopilot.implementation.models import ImplementationManifest, RunState
from research_autopilot.implementation.orchestrator import ImplDeps, check_execution_approval, repair
from research_autopilot.implementation.worktree import matches_any
from research_autopilot.models import ExecutionHandoff
from research_autopilot.storage.artifacts import write_json
from research_autopilot.storage.db import Store
from research_autopilot.tasks.manager import TaskManager

ENVIRONMENT_FAILURE = "environment preparation failed"  # transient (network / index / interpreter download /
# timeout): a retry re-runs Phase 4 only
SETUP_DEFECT = "experiment setup is broken at the commit"  # deterministic: a Phase-4-only rerun fails the same way
_DETERMINISTIC_ENV = re.compile(
    r"unsupported launcher|unsupported dependency files|\bnot found at [0-9a-f]|setup step .* failed \(exit|"
    r"no solution found|unsatisfiable|not found in the package registry|no matching distribution|"
    r"failed to parse|failed to build|no version of|was not found in", re.I)


@dataclass
class ExpDeps:
    settings: Phase4Settings
    phase2: Settings
    store: Store
    manager: TaskManager | None
    backend: ComputeBackend
    registry: dict  # repo id -> RepoConfig (Phase 3 registry)
    analyst: Analyst | None
    reviewer: ScientificReviewer | None
    impl_deps: ImplDeps | None  # for Phase 3 repairs
    detect_host: Callable[[], HostResources] = res.detect_host
    sleep: Callable[[float], None] = time.sleep
    prepare_env: bool = True  # False in unit tests with the FakeBackend
    extra: dict = field(default_factory=dict)
    external: object | None = None  # Phase 9 external.service.ExternalDeps (None: external code cannot run)


class _Block(Exception):
    pass


# ---------------------------------------------------------------- entry point


def run_experiment(deps: ExpDeps, *, phase3_run_ids: list[str] | None = None, resume_run_id: str | None = None,
                   flow_run_id: str | None = None, max_loops: int = 100000) -> ExperimentManifest:
    deps.store.init_schema()
    if resume_run_id:
        m = deps.store.get_experiment(resume_run_id)
        if m is None:
            raise ValueError(f"unknown experiment run {resume_run_id}")
        if m.state in (ExperimentState.COMPLETED, ExperimentState.BLOCKED, ExperimentState.FAILED):
            raise ValueError(f"experiment run {resume_run_id} already finished ({m.state.value})")
        m.event("recovery", "resuming; reconciling job ledger")
        for s in deps.backend.recover():
            m.event("recovery", f"{s.job_key}: {s.state.value}" + (f" ({s.reason})" if s.reason else ""))
    else:
        if not phase3_run_ids:
            raise ValueError("specify phase3_run_ids or resume_run_id")
        first = deps.store.get_implementation(phase3_run_ids[0])
        m = ExperimentManifest(task_id=first.task_id if first else "unknown", phase3_run_id=phase3_run_ids[0])
    m.flow_run_id = flow_run_id or m.flow_run_id
    run_dir = deps.settings.artifact_root / m.task_id / m.experiment_run_id
    try:
        if not resume_run_id:
            for rid in phase3_run_ids:
                _accept_contract(deps, m, rid)
        m.host = deps.detect_host()
        m.state = ExperimentState.RUNNING
        _save(deps, m, run_dir)
        _schedule(deps, m, run_dir, max_loops)
        if m.state is ExperimentState.RUNNING:
            _analyze_and_propose(deps, m, run_dir)
            m.state = ExperimentState.COMPLETED
    except _Block as b:
        m.state = ExperimentState.BLOCKED
        m.block_reason = str(b)
        m.event("blocked", str(b))
    except Exception as exc:
        m.state = ExperimentState.FAILED
        m.block_reason = f"unexpected {type(exc).__name__}: {exc}"
        m.event("error", m.block_reason)
        raise
    finally:
        _finalize(deps, m, run_dir)
    return m


# ---------------------------------------------------------------- contract


def _accept_contract(deps: ExpDeps, m: ExperimentManifest, phase3_run_id: str) -> None:
    impl = deps.store.get_implementation(phase3_run_id)
    if impl is None:
        raise _Block(f"unknown Phase 3 run {phase3_run_id}")
    if impl.state is not RunState.COMPLETED or impl.final_review is None or impl.final_review.verdict != "pass":
        raise _Block(f"Phase 3 run {phase3_run_id} is not a completed implementation with Code Reviewer pass")
    if impl.task_id != m.task_id:
        raise _Block(f"Phase 3 run {phase3_run_id} belongs to {impl.task_id}, not {m.task_id}")
    spec_path = impl.artifact_paths.get("experiment_spec")
    if not spec_path or not Path(spec_path).exists():
        raise _Block(f"Phase 3 run {phase3_run_id} has no ExperimentSpec")
    spec = ExperimentSpec.model_validate_json(Path(spec_path).read_text())
    if spec.identification.commit != impl.implementation_commit or spec.spec_id != impl.experiment_spec_id:
        raise _Block("ExperimentSpec does not match the Phase 3 implementation commit / spec id")
    if spec.experiment_id in m.specs:
        raise _Block(f"duplicate experiment id {spec.experiment_id} in batch")
    # Human execution approval (same deterministic gate as Phase 3).
    handoff = ExecutionHandoff.model_validate_json(Path(impl.handoff_path).read_text())
    if reason := check_execution_approval(deps.store, deps.manager, deps.phase2, handoff):
        raise _Block(f"execution not approved: {reason}")
    # Re-validate the executable contract against the fixed commit (never trust it blindly).
    repo = _repo(deps, impl)
    errors = _revalidate(spec, repo)
    if errors:
        raise _Block("ExperimentSpec invalid at its commit: " + "; ".join(errors[:5]))
    missing_env = [n for n in spec.execution.env_var_names if n not in deps.extra.get("host_env", __import__("os").environ)
                   and not n.endswith("_NUM_THREADS") and n != INPUTS_ENV]  # the inputs manifest is set per job
    if missing_env:
        raise _Block(f"required environment variables not set on this host: {missing_env}")
    m.specs[spec.experiment_id] = spec
    m.spec_history.append(spec.spec_id)
    m.phase2_plan_run_id, m.plan_id = impl.phase2_plan_run_id, impl.plan_id
    m.event("contract", f"accepted {spec.experiment_id} @ {spec.identification.commit[:12]} (Phase 3 {phase3_run_id[:8]})")


def _catalog_inputs(deps: ExpDeps, m: ExperimentManifest, impl, jdir: Path, experiment_id: str) -> Path | None:
    """Phase 7A catalog inputs re-verified on this host right before the job starts, plus the validated outputs of
    upstream tasks (ArtifactHandoff, bytes re-hashed): one manifest in RA_CATALOG_INPUTS."""
    upstream = upstream_artifacts(deps.store, m.task_id)
    handoff = ExecutionHandoff.model_validate_json(Path(impl.handoff_path).read_text()) \
        if impl.handoff_path and Path(impl.handoff_path).exists() else None
    if not (handoff and handoff.catalog_resolution) and not upstream:
        return None
    if handoff and handoff.catalog_resolution:
        ctx = deps.extra.get("catalog") or context_from_env(deps.store, deps.registry)
        now, problems = recheck(ctx, handoff.catalog_resolution)
        if problems:
            raise _Block(f"{experiment_id}: catalog inputs are not reusable: " + "; ".join(problems[:5]))
        doc = now.model_dump(mode="json")
        where = now.environment
    else:
        doc, where = {"artifacts": []}, "this host"
    doc["artifacts"] = list(doc.get("artifacts") or []) + upstream
    path = jdir / "catalog_inputs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2))
    m.artifact_paths[f"catalog_inputs:{experiment_id}"] = str(path)
    m.event("catalog", f"{experiment_id}: {len(doc['artifacts'])} verified input artifact(s) on {where} "
                       f"({len(upstream)} from upstream tasks)")
    return path


def _repo(deps: ExpDeps, impl: ImplementationManifest):
    repo = deps.registry.get(impl.task.repo_id)
    if repo is None:
        raise _Block(f"repository {impl.task.repo_id} is not registered on this host")
    return repo


def _revalidate(spec: ExperimentSpec, repo) -> list[str]:
    from research_autopilot.implementation.worktree import WorktreeManager

    wt = WorktreeManager(repo, Path("/nonexistent"))
    commit = spec.identification.commit
    d = ExperimentDefinition(experiment_id=spec.experiment_id, execution=spec.execution, environment=spec.environment,
                             inputs=spec.inputs, resources=spec.resources, outputs=spec.outputs,
                             metrics=spec.validation.metrics, numeric_checks=spec.validation.numeric_checks,
                             reproduction=spec.reproduction or ["-"])
    try:
        return validate_definition(d, lambda p: wt.file_at(commit, p), lambda p: wt.dir_at(commit, p))
    except Exception as exc:  # noqa: BLE001 - e.g. repository missing
        return [f"cannot inspect commit: {exc}"]


# ---------------------------------------------------------------- scheduling


def _schedule(deps: ExpDeps, m: ExperimentManifest, run_dir: Path, max_loops: int) -> None:
    s = deps.settings
    container = _container_mode(deps, m)
    if container:
        m.host = res.for_container(m.host, deps.extra.get("docker_memory") or res.docker_memory())
    rm = res.ResourceManager(s, m.host)
    done: dict[str, str] = {}  # experiment_id -> "validated" | "blocked"
    waiting_since: dict[str, float] = {}  # experiment_id -> when it started waiting for free memory
    waited: set[str] = set()
    warned_deps: set[str] = set()  # experiments whose other-task job_dependencies were already reported
    # Rebuild state from the manifest (resume)
    for exp_id in m.specs:
        last = _last_job(m, exp_id)
        if last and last.validation and last.validation.status == "pass":
            done[exp_id] = "validated"
        elif last and last.triage and last.triage.action == "blocked":
            done[exp_id] = "blocked"
    for j in m.jobs:
        if j.status is None or not j.status.state.terminal:
            if j.allocation:
                rm.reserve(j.job_key, j.allocation)
    _note_serialization(m, rm)

    for _ in range(max_loops):
        progressed = False
        # 1. submit ready experiments
        for exp_id, spec in m.specs.items():
            if exp_id in done or _active_job(m, exp_id):
                continue
            ignored = [d for d in spec.resources.job_dependencies if d not in m.specs and _TASK_ID.fullmatch(d)]
            if ignored and exp_id not in warned_deps:
                warned_deps.add(exp_id)
                m.event("warning", f"{exp_id}: job_dependencies {ignored} look like other task ids, not experiments of "
                                   f"this run; ignored (ordering between tasks is guaranteed by the task DAG)")
            job_deps = _run_deps(spec, m.specs)
            unknown = [d for d in job_deps if d not in m.specs]
            if unknown:
                done[exp_id] = "blocked"
                m.event("blocked", f"{exp_id}: unknown dependency {unknown}")
                continue
            if any(done.get(d) == "blocked" for d in job_deps):
                done[exp_id] = "blocked"
                m.event("blocked", f"{exp_id}: dependency failed")
                continue
            if not all(done.get(d) == "validated" for d in job_deps):
                continue
            last = _last_job(m, exp_id)
            scale = 1.5 if last and last.triage and last.triage.action == "retry_more_resources" else 1.0
            try:
                est = res.estimate(spec, deps.store.ledger_history(exp_id), s)
                alloc = rm.plan(est, scale=scale)
            except res.ResourceBusy as exc:
                since = waiting_since.setdefault(exp_id, time.monotonic())
                if time.monotonic() - since <= s.resource_wait_minutes * 60:
                    if exp_id not in waited:
                        waited.add(exp_id)
                        m.event("waiting", f"{exp_id}: {exc}; waiting up to {s.resource_wait_minutes:g} min")
                    continue
                done[exp_id] = "blocked"
                m.event("blocked", f"{exp_id}: {exc} (still after {s.resource_wait_minutes:g} min)")
                continue
            except res.ResourceError as exc:
                done[exp_id] = "blocked"
                m.event("blocked", f"{exp_id}: {exc}")
                continue
            if not rm.fits_now(alloc):
                continue
            alloc = rm.claim_gpus(alloc)
            job = _submit(deps, m, run_dir, spec, alloc)
            rm.reserve(job.job_key, alloc)
            progressed = True
        # 2. poll running jobs
        for j in [j for j in m.jobs if j.handle and (j.status is None or not j.status.state.terminal)]:
            st = deps.backend.status(j.handle)
            j.status = st
            if st.state.terminal:
                rm.release(j.job_key)
                outcome = _finish_job(deps, m, run_dir, j)
                if outcome in ("validated", "blocked"):
                    done[j.experiment_id] = outcome
                progressed = True
        _save(deps, m, run_dir)
        if len(done) == len(m.specs):
            break
        waiting = [e for e in waiting_since if e not in done]
        if waiting and not progressed:
            m.host = deps.detect_host()  # re-sample free memory while a job waits for it
            if container:
                m.host = res.for_container(m.host, deps.extra.get("docker_memory") or res.docker_memory())
            rm.host = m.host
        if not progressed and not waiting and not any(
                j.handle and (j.status is None or not j.status.state.terminal) for j in m.jobs):
            stuck = [e for e in m.specs if e not in done]
            raise _Block(f"cannot schedule {stuck} within resource caps")
        if not progressed:
            deps.sleep(_poll_interval(m, s))
    else:
        raise _Block("scheduler loop limit reached")
    blocked = [e for e, v in done.items() if v == "blocked"]
    if blocked:
        raise _Block(f"experiments blocked: {blocked}" + _triage_summary(m, blocked))


_TASK_ID = re.compile(r"[A-Z][A-Z0-9]*-\d+")  # e.g. INU-358: a task id, never an experiment id of this run


def _run_deps(spec, specs) -> list[str]:
    """job_dependencies that are about experiments of this run. Other tasks' ids are dropped: the order between
    tasks is already guaranteed by the task DAG / Linear blocked-by, and Phase 4 cannot resolve them."""
    return [d for d in spec.resources.job_dependencies if d in specs or not _TASK_ID.fullmatch(d)]


def _note_serialization(m: ExperimentManifest, rm: res.ResourceManager) -> None:
    independent = [e for e, sp in m.specs.items() if not _run_deps(sp, m.specs)]
    if len(independent) > 1:
        cap_cpu, cap_mem = rm.capacity()
        if rm.settings.max_parallel_jobs < len(independent):
            m.serialization_notes.append(
                f"{len(independent)} independent experiments but max_parallel_jobs={rm.settings.max_parallel_jobs}; "
                f"excess runs queued (cap cpu={cap_cpu:g}, mem={cap_mem:g}GB)")


def _poll_interval(m: ExperimentManifest, s: Phase4Settings) -> float:
    beats = [j.allocation.walltime_seconds / 3 for j in m.jobs if j.allocation and j.status and not j.status.state.terminal]
    beats += [s.heartbeat_min_seconds]
    return float(min(max(min(beats), s.heartbeat_min_seconds), s.heartbeat_max_seconds))


def _last_job(m: ExperimentManifest, exp_id: str) -> JobRecord | None:
    jobs = [j for j in m.jobs if j.experiment_id == exp_id]
    return jobs[-1] if jobs else None


def _container_mode(deps: ExpDeps, m: ExperimentManifest) -> bool:
    """Jobs of this run execute in the repository's container image (Docker VM resources, not the host's)."""
    if not deps.prepare_env:
        return bool(deps.extra.get("docker_memory"))
    try:
        for spec in m.specs.values():
            impl = deps.store.get_implementation(spec.identification.phase3_run_id)
            if impl and getattr(_repo(deps, impl), "container", None) and not spec.external_code:
                return True
    except Exception:  # a missing record never changes the resource rule
        return False
    return False


def _active_job(m: ExperimentManifest, exp_id: str) -> bool:
    j = _last_job(m, exp_id)
    return bool(j and (j.status is None or not j.status.state.terminal) and j.triage is None)


def _submit(deps: ExpDeps, m: ExperimentManifest, run_dir: Path, spec: ExperimentSpec, alloc) -> JobRecord:
    attempt = len([j for j in m.jobs if j.experiment_id == spec.experiment_id]) + 1
    key = f"{m.experiment_run_id}:{spec.experiment_id}:{attempt}"
    jdir = run_dir / spec.experiment_id / f"attempt-{attempt}"
    checkout = jdir / "src"
    argv = spec.execution.argv
    input_hashes: dict[str, str] = {}
    impl = deps.store.get_implementation(spec.identification.phase3_run_id)
    repo_cfg = _repo(deps, impl)
    repo = Path(repo_cfg.path).expanduser()
    if repo_cfg.read_only_output_dirs and not repo_cfg.container:
        raise _Block(f"{spec.experiment_id}: read-only repository outputs require a container")
    try:
        export_commit(repo, spec.identification.commit, checkout)  # immutable per-job checkout of the fixed commit
        output_mounts = repository_output_mounts(repo, repo_cfg.read_only_output_dirs, repo_cfg.container) \
            if repo_cfg.container else []
        if deps.prepare_env and not spec.external_code and repo_cfg.container:
            # the repository's own image is the environment (e.g. conda + compiled solvers): no venv is built
            m.environment[spec.experiment_id] = container_provenance(repo_cfg.container, checkout)
        elif deps.prepare_env and not spec.external_code:
            prov = ensure_venv(repo, spec, checkout, deps.settings.env_cache_dir)
            argv = resolve_argv(argv, prov["python"])
            for step in spec.execution.setup:
                import subprocess

                try:
                    p = subprocess.run(resolve_argv(step, prov["python"]),
                                       cwd=checkout / spec.execution.working_directory,
                                       capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL)
                except subprocess.TimeoutExpired as exc:
                    raise EnvironmentError_(f"setup step {step[:3]} timed out") from exc
                if p.returncode != 0:
                    raise EnvironmentError_(f"setup step {step[:3]} failed (exit {p.returncode}): "
                                            f"{(p.stderr or p.stdout)[-300:]}")
            m.environment[spec.experiment_id] = prov
    except EnvironmentError_ as exc:
        kind = SETUP_DEFECT if _DETERMINISTIC_ENV.search(str(exc)) else ENVIRONMENT_FAILURE
        raise _Block(f"{spec.experiment_id}: {kind}: {exc}") from exc
    for inp in spec.inputs:
        f = checkout / inp.path
        input_hashes[inp.path] = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else ""
    (jdir / "input_hashes.json").parent.mkdir(parents=True, exist_ok=True)
    (jdir / "input_hashes.json").write_text(json.dumps(input_hashes))
    env = {**res.thread_env(alloc), "HOME": str(jdir / "home"), "TMPDIR": str(jdir / "tmp"),
           "PYTHONHASHSEED": "0", "RA_EXPERIMENT_RUN_ID": m.experiment_run_id, "RA_TASK_ID": m.task_id}
    if (manifest_path := _catalog_inputs(deps, m, impl, jdir, spec.experiment_id)) is not None:
        env[INPUTS_ENV] = str(Path(manifest_path).resolve())  # the job runs in its checkout: never a relative path
    (jdir / "home").mkdir(parents=True, exist_ok=True)
    (jdir / "tmp").mkdir(parents=True, exist_ok=True)
    set_by_runner = set(env)  # e.g. RA_CATALOG_INPUTS: never required from the host environment
    if spec.external_code:  # Phase 9: only inside the approved container, after the grant check
        argv = _external_argv(deps, m, spec, alloc, checkout, env, key)
    elif deps.prepare_env and repo_cfg.container:
        mounts = [jdir.resolve()]
        if manifest_path is not None:
            mounts += [Path(f).resolve().parent for a in json.loads(Path(manifest_path).read_text()).get("artifacts", [])
                       for f in a.get("files", [])]
        setup_env = dict(env)
        for step in spec.execution.setup:
            p = subprocess.run(container_argv(repo_cfg.container, checkout.resolve(), step,
                                              spec.execution.working_directory, setup_env, mounts,
                                              read_only_mounts=output_mounts),
                               cwd=checkout, capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL)
            if p.returncode != 0:
                raise _Block(f"{spec.experiment_id}: {SETUP_DEFECT}: setup step {step[:3]} failed (exit "
                             f"{p.returncode}) in the container: {(p.stderr or p.stdout)[-300:]}")
        override = gpu_override(repo_cfg.container, jdir) if alloc.gpu_ids else None  # only a GPU job sees GPUs
        argv = container_argv(repo_cfg.container, checkout.resolve(), argv, spec.execution.working_directory, env,
                              mounts, override, output_mounts)
        env = {}  # passed into the container with -e; the docker CLI itself keeps the host HOME / context
    job = JobSpec(job_key=key, experiment_run_id=m.experiment_run_id, experiment_id=spec.experiment_id, spec_id=spec.spec_id,
                  commit=spec.identification.commit, argv=argv, working_directory=spec.execution.working_directory,
                  env=env, secret_env_names=[n for n in spec.execution.env_var_names if n not in set_by_runner],
                  allocation=alloc, run_dir=str(jdir), checkout_dir=str(checkout), outputs=[o.path for o in spec.outputs],
                  heartbeat_seconds=res.heartbeat_seconds(alloc, deps.settings))
    rec = JobRecord(experiment_id=spec.experiment_id, spec_id=spec.spec_id, commit=spec.identification.commit,
                    attempt=attempt, job_key=key, allocation=alloc)
    m.jobs.append(rec)
    _save(deps, m, run_dir)  # record intent before submitting
    try:
        rec.handle = deps.backend.submit(job)
    except BackendNotConfigured as exc:
        raise _Block(f"compute backend not configured: {exc}") from exc
    _ledger_tag(deps, key, spec.experiment_id)
    m.event("submit", f"{key} cpu={alloc.cpu} mem={alloc.memory_gb}GB wall={alloc.walltime_seconds}s "
                      f"({alloc.estimate.basis}/{alloc.estimate.confidence})")
    return rec


def _external_argv(deps: ExpDeps, m: ExperimentManifest, spec: ExperimentSpec, alloc, checkout: Path,
                   env: dict, key: str) -> list[str]:
    from research_autopilot.external.integration import Denied, NeedsApproval, prepare_job
    if deps.external is None:
        raise _Block(f"{spec.experiment_id}: uses external code {spec.external_code[0].name} but Phase 9 is not "
                     "configured; it is never run outside the approved container")
    try:
        argv, provenance = prepare_job(deps.external, spec, alloc, checkout=checkout, env=env, job_key=key,
                                       experiment_run_id=m.experiment_run_id)
    except NeedsApproval as exc:
        m.external_approvals.append(exc.request.request_id)
        raise _Block(f"{spec.experiment_id}: external code {spec.external_code[0].name} needs a human execution "
                     f"approval ({exc.request.request_id[:8]}): {'; '.join(exc.reasons)[:300]}") from exc
    except Denied as exc:
        raise _Block(f"{spec.experiment_id}: {exc}") from exc
    m.environment[spec.experiment_id] = provenance  # which external code version produced these results
    m.event("external_code", f"{key}: {spec.external_code[0].name} under execution record "
                             f"{provenance['external_execution_id']}")
    return argv


def _ledger_tag(deps: ExpDeps, key: str, exp_id: str) -> None:
    row = deps.store.ledger_get(key)
    if row is not None:
        rec = dict(row.get("record") or {})
        rec["experiment_id"] = exp_id
        deps.store.ledger_update(key, record=rec)


# ---------------------------------------------------------------- completion / triage


def _finish_job(deps: ExpDeps, m: ExperimentManifest, run_dir: Path, j: JobRecord) -> str:
    spec = m.specs[j.experiment_id]
    if spec.external_code and deps.external is not None:
        from research_autopilot.external.integration import finish_job
        finish_job(deps.external, j.job_key, j.status)
    workdir = Path(j.handle.run_dir) / "src" / spec.execution.working_directory
    if j.status.state is JobState.SUCCEEDED:
        j.collect = deps.backend.collect(j.handle, [o.path for o in spec.outputs])
        hashes = json.loads((Path(j.handle.run_dir) / "input_hashes.json").read_text())
        j.validation = validate(spec, j.status, j.collect, workdir, hashes)
        if j.validation.status == "pass":
            m.event("validated", f"{j.job_key}: pass (fully_verified={j.validation.fully_verified})")
            return "validated"
    stderr = _read_tail(j.status.stderr_path or str(Path(j.handle.run_dir) / "stderr.log"))
    impl = deps.store.get_implementation(spec.identification.phase3_run_id)
    repo_files = set(impl.changed_files) | {i.path for i in spec.inputs}
    j.triage = classify(j.status, j.validation, stderr, repo_files)
    m.event("triage", f"{j.job_key}: {j.triage.error_class.value} -> {j.triage.action} ({j.triage.reason[:150]})")
    return _act_on_triage(deps, m, run_dir, j, stderr)


def _act_on_triage(deps: ExpDeps, m: ExperimentManifest, run_dir: Path, j: JobRecord, stderr: str) -> str:
    s, t = deps.settings, j.triage
    if t.action in ("retry", "retry_more_resources"):
        if m.retries >= s.max_infra_retries:
            j.triage = TriageDecision(error_class=t.error_class, action="blocked", reason=f"retry limit reached: {t.reason}",
                                      evidence=t.evidence)
            return "blocked"
        m.retries += 1
        deps.sleep(s.retry_backoff_seconds * m.retries)
        m.event("retry", f"{j.experiment_id}: retry {m.retries}/{s.max_infra_retries} (same scientific conditions)")
        return "retry"
    if t.action == "phase3_repair":
        return _repair(deps, m, run_dir, j, stderr)
    return "blocked"


def _repair(deps: ExpDeps, m: ExperimentManifest, run_dir: Path, j: JobRecord, stderr: str) -> str:
    s = deps.settings
    spec = m.specs[j.experiment_id]
    if m.roundtrips >= s.max_roundtrips:
        j.triage = TriageDecision(error_class=j.triage.error_class, action="blocked",
                                  reason=f"Phase 3<->4 round-trip limit ({s.max_roundtrips}) reached", evidence=j.triage.evidence)
        return "blocked"
    if deps.impl_deps is None:
        j.triage = TriageDecision(error_class=j.triage.error_class, action="blocked",
                                  reason="Phase 3 repair not available on this host", evidence=j.triage.evidence)
        return "blocked"
    impl = deps.store.get_implementation(spec.identification.phase3_run_id)
    locked = spec.locked_scientific_inputs()
    scope = [g for g in impl.task.allowed_file_scope
             if g != ".autopilot/experiment.json" and not any(matches_any(p, [g]) for p in locked)]
    if not scope:
        j.triage = TriageDecision(error_class=j.triage.error_class, action="blocked",
                                  reason="no repair scope left outside scientific inputs", evidence=j.triage.evidence)
        return "blocked"
    n = m.roundtrips + 1
    report = FailureReport(
        report_id=re.sub(r"[^A-Za-z0-9._-]", "-", f"{m.experiment_run_id[:8]}-{spec.experiment_id}-rt{n}")[:64],
        task_id=m.task_id, phase3_run_id=spec.identification.phase3_run_id, phase4_run_id=m.experiment_run_id,
        failed_commit=spec.identification.commit, experiment_spec_id=spec.spec_id, argv=spec.execution.argv,
        input_config={i.path: "unchanged from commit" for i in spec.inputs}, exit_code=j.status.exit_code,
        signal=str(j.status.signal) if j.status.signal else None, error_class=j.triage.error_class,
        stdout_ref=j.status.stdout_path, stderr_ref=j.status.stderr_path,
        artifacts=[a.stored_path for a in (j.collect.artifacts if j.collect else [])],
        summary=j.triage.reason[:1000], log_excerpt=SECRET_PATTERNS.sub("[REDACTED]", stderr[-6000:]),
        reproduction=[f"git checkout {spec.identification.commit}", " ".join(spec.execution.argv)],
        allowed_repair_scope=scope, scientific_invariants=spec.validation.scientific_invariants, roundtrip=n,
    )
    path = write_json(Path(j.handle.run_dir) / "failure_report.json", report)
    m.roundtrips = n
    m.state = ExperimentState.REPAIRING
    m.event("repair", f"{j.experiment_id}: FailureReport {report.report_id} -> Phase 3 (round trip {n})")
    _save(deps, m, run_dir)
    fixed = repair(deps.impl_deps, path)
    m.repairs.append({"report_id": report.report_id, "phase3_run_id": fixed.phase3_run_id, "state": fixed.state.value,
                      "reason": fixed.block_reasons[-1].message if fixed.block_reasons else ""})
    m.state = ExperimentState.RUNNING
    if fixed.state is not RunState.COMPLETED:
        j.triage = TriageDecision(error_class=j.triage.error_class, action="blocked",
                                  reason=f"Phase 3 repair did not complete: {m.repairs[-1]['reason']}", evidence=j.triage.evidence)
        return "blocked"
    new_spec = ExperimentSpec.model_validate_json(Path(fixed.artifact_paths["experiment_spec"]).read_text())
    m.specs[j.experiment_id] = new_spec
    m.spec_history.append(new_spec.spec_id)
    m.event("repair", f"{j.experiment_id}: new commit {new_spec.identification.commit[:12]} spec {new_spec.spec_id[:8]}")
    return "repaired"


def _triage_summary(m: ExperimentManifest, blocked: list[str]) -> str:
    parts = []
    for e in blocked:
        j = _last_job(m, e)
        if j and j.triage:
            parts.append(f"{e}: {j.triage.error_class.value}: {j.triage.reason[:120]}")
    return (" -- " + "; ".join(parts)) if parts else ""


def _read_tail(path: str, n: int = 20000) -> str:
    try:
        return Path(path).read_text(errors="replace")[-n:]
    except OSError:
        return ""


# ---------------------------------------------------------------- analysis / proposals


def _analyze_and_propose(deps: ExpDeps, m: ExperimentManifest, run_dir: Path) -> None:
    s = deps.settings
    validated = [j for j in m.jobs if j.validation and j.validation.status == "pass"]
    if not validated or deps.analyst is None:
        m.event("analysis", "skipped (no validated results or no analyst configured)")
        return
    m.state = ExperimentState.ANALYZING
    impl = deps.store.get_implementation(m.phase3_run_id)
    handoff = json.loads(Path(impl.handoff_path).read_text())
    existing = deps.store.list_proposals(m.task_id)
    payload = {
        "task": {k: handoff.get(k) for k in ("task_id", "purpose", "specification", "scientific_invariants",
                                             "acceptance_criteria", "non_goals")},
        "plan": handoff.get("plan"),
        "experiment_specs": {k: v.model_dump(mode="json", include={"experiment_id", "identification", "outputs",
                                                                   "validation", "resources"}) for k, v in m.specs.items()},
        "validated_results": [{"job": j.job_key, "commit": j.commit, "metrics": j.validation.metrics,
                               "checks": [c.model_dump(mode="json") for c in j.validation.checks],
                               "artifacts": [a.model_dump(mode="json") for a in (j.collect.artifacts if j.collect else [])]}
                              for j in validated],
        "failed_or_blocked": [{"job": j.job_key, "triage": j.triage.model_dump(mode="json")} for j in m.jobs if j.triage],
        "existing_proposals": [{"title": p["proposal"]["draft"]["title"], "question": p["proposal"]["draft"]["research_question"]}
                               for p in existing],
        "max_proposals": s.max_proposals_per_run,
    }
    if m.llm_cost_known >= s.max_llm_cost_usd_per_run:
        raise _Block(f"LLM budget exhausted before analysis (${m.llm_cost_known:.4f})")
    try:
        run = deps.analyst.analyze(payload)
    except LlmBudgetError as exc:
        m.llm_usage.append(LlmUsage(agent="analyst", model=deps.analyst.model, error=str(exc)))
        raise _Block(f"analysis not run: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - normalized agent errors
        m.llm_usage.append(LlmUsage(agent="analyst", model=deps.analyst.model, error=f"{type(exc).__name__}: {exc}"))
        raise _Block(f"analysis failed: {type(exc).__name__}: {exc}") from exc
    m.llm_usage.append(LlmUsage(agent="analyst", model=run.model, input_tokens=run.usage.input_tokens,
                                output_tokens=run.usage.output_tokens, cost_usd=run.cost_usd,
                                billing=getattr(run, "billing", None)))
    analysis: AnalysisResult = run.output
    m.analysis = analysis
    m.artifact_paths["analysis"] = str(write_json(run_dir / "analysis.json", analysis))
    _review_proposals(deps, m, run_dir, analysis, handoff, existing, validated)


def _review_proposals(deps: ExpDeps, m: ExperimentManifest, run_dir: Path, analysis: AnalysisResult, handoff: dict,
                      existing: list[dict], validated: list[JobRecord]) -> None:
    s = deps.settings
    inv = [ln for ln in _lines(handoff.get("scientific_invariants"))]
    acc = [ln for ln in _lines(handoff.get("acceptance_criteria"))]
    parent_depth = int(deps.extra.get("chain_depth", 0))
    for draft in analysis.proposals[: s.max_proposals_per_run]:
        depth = parent_depth + 1
        if depth > s.max_chain_depth:
            m.event("proposal", f"skipped {draft.title!r}: chain depth {depth} > {s.max_chain_depth}")
            continue
        key = dedupe_key(m.task_id, draft)
        if any(p["proposal"]["dedupe_key"] == key for p in existing):
            m.event("proposal", f"skipped duplicate {draft.title!r}")
            continue
        proposal = ExperimentProposal(dedupe_key=key, parent_task_id=m.task_id, source_experiment_run_id=m.experiment_run_id,
                                      source_artifacts=[a.stored_path for j in validated for a in (j.collect.artifacts if j.collect else [])],
                                      source_commit=validated[-1].commit, chain_depth=depth, draft=draft)
        review, err = None, None
        if deps.reviewer is not None and m.llm_cost_known < s.max_llm_cost_usd_per_run:
            try:
                rr = deps.reviewer.review({"task": handoff, "proposal": draft.model_dump(mode="json"),
                                           "validated_metrics": [j.validation.metrics for j in validated],
                                           "existing_proposals": [p["proposal"]["draft"]["title"] for p in existing]})
                review = rr.output
                m.llm_usage.append(LlmUsage(agent="reviewer", model=rr.model, input_tokens=rr.usage.input_tokens,
                                            output_tokens=rr.usage.output_tokens, cost_usd=rr.cost_usd,
                                            billing=getattr(rr, "billing", None)))
            except Exception as exc:  # noqa: BLE001 - recorded; scope then requires a human
                err = f"{type(exc).__name__}: {exc}"
                m.llm_usage.append(LlmUsage(agent="reviewer", model=deps.reviewer.model, error=err))
        else:
            err = "reviewer unavailable or LLM budget exhausted"
        scope = assess(draft, original_invariants=inv, original_acceptance=acc, original_purpose=handoff.get("purpose"),
                       review=review)
        reviewed = ReviewedExperimentProposal(
            proposal=proposal, review=review, review_error=err, scope_assessment=scope,
            task_spec_ref={"task_id": m.task_id, "plan_id": m.plan_id, "phase2_plan_run_id": m.phase2_plan_run_id,
                           "sources": json.dumps(handoff.get("sources"), ensure_ascii=False)},
            execution_plan_ref={"plan_id": m.plan_id, "phase3_run_id": m.phase3_run_id},
            spec_diff=scope.proposed_changes + [f for f in scope.deterministic_findings if "missing" in f or "not preserved" in f],
            limits_metadata={"chain_depth": depth, "max_chain_depth": s.max_chain_depth,
                             "proposals_in_run": len(analysis.proposals), "max_proposals_per_run": s.max_proposals_per_run,
                             "llm_cost_usd_known": m.llm_cost_known, "max_llm_cost_usd_per_run": s.max_llm_cost_usd_per_run,
                             "estimated_resources": draft.estimated_resources, "estimated_cost": draft.estimated_cost},
        )
        if not deps.store.save_proposal(reviewed):
            m.event("proposal", f"duplicate (store) {draft.title!r}")
            continue
        path = write_json(run_dir / "proposals" / f"{proposal.proposal_id}.json", reviewed)
        m.reviewed_proposals.append(proposal.proposal_id)
        m.artifact_paths[f"proposal:{proposal.proposal_id}"] = str(path)
        m.event("proposal", f"{draft.title!r}: review={review.verdict if review else 'n/a'} "
                            f"within_scope={scope.within_scope} human={scope.requires_human_decision}")


def _lines(text: str | None) -> list[str]:
    from research_autopilot.implementation.experiment import spec_lines

    return spec_lines(text)


# ---------------------------------------------------------------- persistence


def _finalize(deps: ExpDeps, m: ExperimentManifest, run_dir: Path) -> None:
    final_commit = next((sp.identification.commit for sp in m.specs.values()), None)
    handoff = Phase5Handoff(
        experiment_run_id=m.experiment_run_id, task_id=m.task_id, phase3_run_id=m.phase3_run_id, final_commit=final_commit,
        state=m.state, validations=[j.validation for j in m.jobs if j.validation],
        analysis_path=m.artifact_paths.get("analysis"),
        reviewed_proposal_paths=[v for k, v in m.artifact_paths.items() if k.startswith("proposal:")],
        manifest_path=str(run_dir / "manifest.json"))
    m.artifact_paths["phase5_handoff"] = str(write_json(run_dir / "phase5_handoff.json", handoff))
    _save(deps, m, run_dir)


def _save(deps: ExpDeps, m: ExperimentManifest, run_dir: Path) -> None:
    from research_autopilot.models import utcnow

    m.updated_at = utcnow()
    m.artifact_paths["manifest"] = str(run_dir / "manifest.json")
    write_json(run_dir / "manifest.json", m)
    deps.store.save_experiment(m)


__all__ = ["ExpDeps", "run_experiment", "JobStatus"]
