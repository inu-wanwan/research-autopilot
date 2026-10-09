"""In-memory ComputeBackend for tests. Each submit consumes a scripted outcome:
dict(state=..., exit_code=..., outputs={path: content}, stderr=..., peak_rss_gb=...)."""

from pathlib import Path

from research_autopilot.experiments.backends.common import collect_outputs
from research_autopilot.experiments.models import CollectResult, JobHandle, JobSpec, JobState, JobStatus
from research_autopilot.models import utcnow


class FakeComputeBackend:
    name = "fake"

    def __init__(self, script: list[dict] | None = None, by_experiment: dict[str, list[dict]] | None = None) -> None:
        self.script = list(script or [])
        self.by_experiment = {k: list(v) for k, v in (by_experiment or {}).items()}
        self.jobs: dict[str, dict] = {}
        self.submits: list[JobSpec] = []
        self.cancelled: list[str] = []
        self.running: set[str] = set()
        self.max_concurrent = 0
        self.order: list[str] = []

    def submit(self, job: JobSpec) -> JobHandle:
        if job.job_key in self.jobs:
            return self.jobs[job.job_key]["handle"]
        queue = self.by_experiment.get(job.experiment_id) or self.script
        outcome = queue.pop(0) if queue else {"state": "succeeded", "exit_code": 0}
        handle = JobHandle(job_key=job.job_key, backend="fake", external_id=f"fake-{len(self.jobs)}", run_dir=job.run_dir)
        self.jobs[job.job_key] = {"handle": handle, "spec": job, "outcome": outcome, "polls": 0}
        self.submits.append(job)
        self.running.add(job.job_key)
        self.order.append(job.experiment_id)
        self.max_concurrent = max(self.max_concurrent, len(self.running))
        return handle

    def status(self, handle: JobHandle) -> JobStatus:
        j = self.jobs[handle.job_key]
        j["polls"] += 1
        o = j["outcome"]
        run_dir = Path(j["spec"].run_dir)
        if j["polls"] < o.get("polls_until_done", 1):
            return JobStatus(job_key=handle.job_key, state=JobState.RUNNING, heartbeat_at=utcnow())
        if handle.job_key in self.running:
            self.running.discard(handle.job_key)
            work = Path(j["spec"].checkout_dir) / j["spec"].working_directory
            for rel, content in o.get("outputs", {}).items():
                (work / rel).parent.mkdir(parents=True, exist_ok=True)
                (work / rel).write_text(content) if isinstance(content, str) else (work / rel).write_bytes(content)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "stdout.log").write_text(o.get("stdout", ""))
            (run_dir / "stderr.log").write_text(o.get("stderr", ""))
        return JobStatus(job_key=handle.job_key, state=JobState(o.get("state", "succeeded")), exit_code=o.get("exit_code"),
                         signal=o.get("signal"), peak_rss_gb=o.get("peak_rss_gb", 0.1), reason=o.get("reason"),
                         started_at=utcnow(), finished_at=utcnow(), stdout_path=str(run_dir / "stdout.log"),
                         stderr_path=str(run_dir / "stderr.log"))

    def cancel(self, handle: JobHandle) -> None:
        self.cancelled.append(handle.job_key)
        self.running.discard(handle.job_key)

    def collect(self, handle: JobHandle, outputs: list[str], workdir: Path | None = None) -> CollectResult:
        spec = self.jobs[handle.job_key]["spec"]
        return collect_outputs(handle.job_key, Path(spec.run_dir), Path(spec.checkout_dir) / spec.working_directory,
                               outputs)

    def recover(self) -> list[JobStatus]:
        return [self.status(j["handle"]) for k, j in self.jobs.items() if k in self.running]
