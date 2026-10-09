"""LocalBackend: asynchronous submit via the detached shim + persistent job ledger.

Crash safety: the ledger row (state=submitting) is written *before* the process is
spawned. On recovery, a submitting row is resolved from on-disk evidence:
``launch.started``/``status.json`` present -> the job really started (adopt it);
no evidence and the shim is not alive after a grace period -> ``never_started``
(safe to resubmit with the same key); anything else -> LOST (never resubmitted
automatically).
"""

import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from research_autopilot.experiments.backends.common import collect_outputs
from research_autopilot.experiments.models import CollectResult, JobHandle, JobSpec, JobState, JobStatus
from research_autopilot.storage.db import Store

GRACE_SECONDS = 30


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ts(v: str | None):
    return datetime.fromisoformat(v) if v else None


class LocalBackend:
    name = "local"

    def __init__(self, store: Store, base_env: dict[str, str] | None = None) -> None:
        self.store = store
        self.base_env = base_env if base_env is not None else {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR")
                                                               if k in os.environ}

    # ---------------------------------------------------------------- submit

    def submit(self, job: JobSpec) -> JobHandle:
        run_dir = Path(job.run_dir)
        if not self.store.ledger_insert(job.job_key, job.experiment_run_id, self.name, job.run_dir):
            row = self.store.ledger_get(job.job_key)
            return JobHandle(job_key=job.job_key, backend="local", external_id=row["external_id"], run_dir=row["run_dir"])
        run_dir.mkdir(parents=True, exist_ok=True)
        missing = [n for n in job.secret_env_names if n not in os.environ]
        if missing:
            self.store.ledger_update(job.job_key, state="failed", record={"reason": f"missing env vars {missing}"})
            raise RuntimeError(f"required environment variables not set: {missing}")
        # The job environment reaches the shim only through its process environment; secret
        # values are never written to disk, manifests or logs.
        env = {**self.base_env, **job.env, **{n: os.environ[n] for n in job.secret_env_names}}
        (run_dir / "job.json").write_text(json.dumps({
            "argv": job.argv, "cwd": str(Path(job.checkout_dir) / job.working_directory),
            "memory_limit_gb": job.allocation.memory_gb, "walltime_seconds": job.allocation.walltime_seconds,
            "poll_seconds": min(max(job.heartbeat_seconds // 5, 1), 10), "job_key": job.job_key,
            "experiment_id": job.experiment_id}))
        with open(run_dir / "shim.log", "ab") as log:
            proc = subprocess.Popen([sys.executable, "-m", "research_autopilot.experiments.shim", str(run_dir)],
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                                    env={**env, "RA_SHIM_PYTHONPATH": "1", "PYTHONPATH": os.pathsep.join(sys.path)})
        self.store.ledger_update(job.job_key, state="queued", external_id=str(proc.pid))
        return JobHandle(job_key=job.job_key, backend="local", external_id=str(proc.pid), run_dir=str(run_dir))

    # ---------------------------------------------------------------- status

    def status(self, handle: JobHandle) -> JobStatus:
        rd = Path(handle.run_dir)
        shim_pid = int(handle.external_id) if handle.external_id else _read_int(rd / "shim.pid")
        st = _read_json(rd / "status.json")
        common = dict(job_key=handle.job_key, external_id=str(shim_pid) if shim_pid else None,
                      stdout_path=str(rd / "stdout.log"), stderr_path=str(rd / "stderr.log"))
        if st:
            state = JobState(st["state"])
            if not state.terminal and not _pid_alive(shim_pid):
                state, reason = JobState.LOST, "shim died without a final status"
            else:
                reason = st.get("reason")
            status = JobStatus(state=state, started_at=_ts(st.get("started_at")), heartbeat_at=_ts(st.get("heartbeat_at")),
                               finished_at=_ts(st.get("finished_at")), exit_code=st.get("exit_code"), signal=st.get("signal"),
                               peak_rss_gb=st.get("peak_rss_gb"), cpu_seconds=st.get("cpu_seconds"), reason=reason, **common)
        elif (rd / "launch.started").exists():
            status = JobStatus(state=JobState.RUNNING if _pid_alive(shim_pid) else JobState.LOST,
                               reason=None if _pid_alive(shim_pid) else "started but no status written", **common)
        elif _pid_alive(shim_pid):
            status = JobStatus(state=JobState.QUEUED, **common)
        else:
            status = JobStatus(state=JobState.LOST, reason="process not running and no start evidence", **common)
        self._record(status, st)
        return status

    def _record(self, status: JobStatus, raw: dict | None) -> None:
        row = self.store.ledger_get(status.job_key)
        if row is None:
            return
        rec = dict(row.get("record") or {})
        rec.update({"state": status.state.value, "peak_rss_gb": status.peak_rss_gb,
                    "wall_seconds": (raw or {}).get("wall_seconds"), "experiment_id": rec.get("experiment_id")})
        self.store.ledger_update(status.job_key, state=status.state.value, record=rec)

    # ---------------------------------------------------------------- cancel / collect / recover

    def cancel(self, handle: JobHandle) -> None:
        pid = int(handle.external_id) if handle.external_id else _read_int(Path(handle.run_dir) / "shim.pid")
        if pid and _pid_alive(pid):
            os.kill(pid, signal.SIGTERM)  # shim kills the job's process group and records "cancelled"

    def collect(self, handle: JobHandle, outputs: list[str], workdir: Path | None = None) -> CollectResult:
        job = _read_json(Path(handle.run_dir) / "job.json") or {}
        return collect_outputs(handle.job_key, Path(handle.run_dir), Path(workdir or job.get("cwd", handle.run_dir)), outputs)

    def recover(self) -> list[JobStatus]:
        out = []
        for row in self.store.ledger_open(self.name):
            rd = Path(row["run_dir"])
            handle = JobHandle(job_key=row["job_key"], backend="local", external_id=row["external_id"], run_dir=row["run_dir"])
            if row["state"] == "submitting" and not row["external_id"]:
                shim_pid = _read_int(rd / "shim.pid")
                if (rd / "launch.started").exists() or shim_pid:
                    handle.external_id = str(shim_pid) if shim_pid else None
                    self.store.ledger_update(row["job_key"], external_id=handle.external_id)
                else:
                    age = (datetime.now(UTC) - _aware(row["created_at"])).total_seconds()
                    if age < GRACE_SECONDS:
                        continue
                    # No evidence the job ever started: safe to resubmit under the same key.
                    self.store.ledger_update(row["job_key"], state="never_started")
                    out.append(JobStatus(job_key=row["job_key"], state=JobState.LOST, reason="never_started"))
                    continue
            out.append(self.status(handle))
        return out

    def forget_never_started(self, job_key: str) -> bool:
        """Allow resubmission of a job proven never to have started."""
        row = self.store.ledger_get(job_key)
        if row and row["state"] == "never_started":
            from research_autopilot.storage.db import experiment_jobs
            with self.store.engine.begin() as conn:
                conn.execute(experiment_jobs.delete().where(experiment_jobs.c.job_key == job_key))
            return True
        return False


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _read_int(p: Path) -> int | None:
    try:
        return int(p.read_text().strip())
    except (OSError, ValueError):
        return None
