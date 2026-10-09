"""SlurmBackend for TSUBAME (interface only in Phase 4 -- never connected here).

All site-specific values (host, partition / resource type, account / group, QoS,
transfer method, policy acknowledgement) must be set by a human; anything missing
raises BackendNotConfigured (-> blocked). Remote commands go through an injected
``executor`` so tests can use a fake Slurm.
"""

import re
import shlex
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from research_autopilot.experiments.backends.base import BackendNotConfigured
from research_autopilot.experiments.models import CollectResult, JobHandle, JobSpec, JobState, JobStatus
from research_autopilot.storage.db import Store

SACCT_STATE = {
    "PENDING": JobState.QUEUED, "CONFIGURING": JobState.QUEUED, "RUNNING": JobState.RUNNING,
    "COMPLETING": JobState.RUNNING, "COMPLETED": JobState.SUCCEEDED, "FAILED": JobState.FAILED,
    "CANCELLED": JobState.CANCELLED, "TIMEOUT": JobState.RESOURCE_EXCEEDED, "OUT_OF_MEMORY": JobState.RESOURCE_EXCEEDED,
    "NODE_FAIL": JobState.FAILED, "PREEMPTED": JobState.FAILED, "BOOT_FAIL": JobState.FAILED,
}


class SlurmConfig(BaseModel):
    host: str | None = None  # SSH host alias (keys managed by ~/.ssh/config, never stored here)
    partition: str | None = None
    account: str | None = None
    qos: str | None = None
    remote_root: str | None = None  # remote working root
    transfer: str | None = None  # "rsync" | "scp" (to be decided by a human)
    max_walltime_minutes: int | None = None
    policy_acknowledged: bool = False  # human confirmed site rules / quotas

    def missing(self) -> list[str]:
        req = {"host": self.host, "partition": self.partition, "account": self.account,
               "remote_root": self.remote_root, "transfer": self.transfer,
               "max_walltime_minutes": self.max_walltime_minutes}
        out = [k for k, v in req.items() if not v]
        if not self.policy_acknowledged:
            out.append("policy_acknowledged")
        return out


Executor = Callable[[list[str]], tuple[int, str, str]]  # argv -> (rc, stdout, stderr)


class SlurmBackend:
    name = "slurm"

    def __init__(self, config: SlurmConfig, store: Store, executor: Executor | None = None) -> None:
        self.config = config
        self.store = store
        self.executor = executor

    def _require(self) -> None:
        if missing := self.config.missing():
            raise BackendNotConfigured(f"Slurm/TSUBAME settings missing: {missing}")
        if self.executor is None:
            raise BackendNotConfigured("no SSH executor configured (TSUBAME access not set up)")

    def _ssh(self, remote: list[str]) -> tuple[int, str, str]:
        return self.executor(["ssh", "-o", "BatchMode=yes", self.config.host, shlex.join(remote)])

    def sbatch_script(self, job: JobSpec) -> str:
        c, a = self.config, job.allocation
        minutes = min(a.walltime_seconds // 60 + 1, c.max_walltime_minutes or 10**6)
        remote_dir = f"{c.remote_root.rstrip('/')}/{job.job_key}"
        lines = ["#!/bin/bash", f"#SBATCH --job-name={_safe(job.job_key)[:60]}", f"#SBATCH --partition={c.partition}",
                 f"#SBATCH --account={c.account}", f"#SBATCH --cpus-per-task={a.cpu}", f"#SBATCH --mem={int(a.memory_gb * 1024)}M",
                 f"#SBATCH --time={minutes // 60:02d}:{minutes % 60:02d}:00", f"#SBATCH --output={remote_dir}/stdout.log",
                 f"#SBATCH --error={remote_dir}/stderr.log"]
        if c.qos:
            lines.append(f"#SBATCH --qos={c.qos}")
        if a.gpu:
            lines.append(f"#SBATCH --gres=gpu:{a.gpu}")
        lines += ["set -euo pipefail", *[f"export {k}={shlex.quote(v)}" for k, v in job.env.items()],
                  f"cd {shlex.quote(remote_dir + '/src/' + job.working_directory)}",
                  "exec " + shlex.join(job.argv)]  # argv quoted, no shell interpolation of job args
        return "\n".join(lines) + "\n"

    def submit(self, job: JobSpec) -> JobHandle:
        self._require()
        if not self.store.ledger_insert(job.job_key, job.experiment_run_id, self.name, job.run_dir):
            row = self.store.ledger_get(job.job_key)
            if row["external_id"]:
                return JobHandle(job_key=job.job_key, backend="slurm", external_id=row["external_id"], run_dir=row["run_dir"])
            # Unknown whether sbatch ran (crash before the id was saved): look it up by job name.
            found = self._find_by_name(job.job_key)
            if found:
                self.store.ledger_update(job.job_key, external_id=found, state="queued")
                return JobHandle(job_key=job.job_key, backend="slurm", external_id=found, run_dir=row["run_dir"])
            raise RuntimeError(f"job {job.job_key} state unknown; refusing to resubmit automatically")
        script = Path(job.run_dir) / "job.sbatch"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(self.sbatch_script(job))
        rc, out, err = self._ssh(["sbatch", "--parsable", f"{self.config.remote_root.rstrip('/')}/{job.job_key}/job.sbatch"])
        if rc != 0:
            self.store.ledger_update(job.job_key, state="failed", record={"reason": err[-300:]})
            raise RuntimeError(f"sbatch failed: {err[-300:]}")
        job_id = out.strip().split(";")[0]
        self.store.ledger_update(job.job_key, external_id=job_id, state="queued")
        return JobHandle(job_key=job.job_key, backend="slurm", external_id=job_id, run_dir=job.run_dir)

    def _find_by_name(self, job_key: str) -> str | None:
        rc, out, _ = self._ssh(["squeue", "--noheader", "--format=%i", f"--name={_safe(job_key)[:60]}"])
        ids = out.split() if rc == 0 else []
        return ids[0] if len(ids) == 1 else None

    def status(self, handle: JobHandle) -> JobStatus:
        self._require()
        rc, out, _ = self._ssh(["sacct", "-j", handle.external_id, "--noheader", "--parsable2",
                                "--format=State,ExitCode,MaxRSS,Elapsed"])
        line = out.strip().splitlines()[0] if rc == 0 and out.strip() else ""
        if not line:
            rc, out, _ = self._ssh(["squeue", "--noheader", "--format=%T", "-j", handle.external_id])
            st = SACCT_STATE.get(out.strip(), JobState.LOST) if rc == 0 and out.strip() else JobState.LOST
            return JobStatus(job_key=handle.job_key, state=st, external_id=handle.external_id)
        state_raw, exit_code, maxrss, _ = (line.split("|") + ["", "", "", ""])[:4]
        state = SACCT_STATE.get(state_raw.split()[0], JobState.LOST)
        code, sig = (exit_code.split(":") + ["0"])[:2] if exit_code else (None, None)
        st = JobStatus(job_key=handle.job_key, state=state, external_id=handle.external_id,
                       exit_code=int(code) if code not in (None, "") else None, signal=int(sig) if sig else None,
                       peak_rss_gb=_rss_gb(maxrss), reason=state_raw)
        self.store.ledger_update(handle.job_key, state=state.value)
        return st

    def cancel(self, handle: JobHandle) -> None:
        self._require()
        self._ssh(["scancel", handle.external_id])

    def collect(self, handle: JobHandle, outputs: list[str]) -> CollectResult:
        self._require()
        raise BackendNotConfigured("artifact transfer from TSUBAME is not configured (decided later by a human)")

    def recover(self) -> list[JobStatus]:
        self._require()
        return [self.status(JobHandle(job_key=r["job_key"], backend="slurm", external_id=r["external_id"],
                                      run_dir=r["run_dir"])) for r in self.store.ledger_open(self.name) if r["external_id"]]


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def _rss_gb(v: str) -> float | None:
    m = re.match(r"([\d.]+)([KMG]?)", v or "")
    if not m:
        return None
    return float(m.group(1)) / {"K": 2**20, "M": 2**10, "G": 1, "": 2**30}[m.group(2)]
