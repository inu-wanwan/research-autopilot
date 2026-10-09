"""ComputeBackend interface."""

from typing import Protocol

from research_autopilot.experiments.models import CollectResult, JobHandle, JobSpec, JobStatus


class BackendNotConfigured(Exception):
    """Required configuration (e.g. Slurm partition / account / auth) is missing -> blocked."""


class ComputeBackend(Protocol):
    name: str

    def submit(self, job: JobSpec) -> JobHandle:
        """Idempotent per ``job.job_key``: a second submit returns the existing handle."""
        ...

    def status(self, handle: JobHandle) -> JobStatus: ...

    def cancel(self, handle: JobHandle) -> None: ...

    def collect(self, handle: JobHandle, outputs: list[str]) -> CollectResult: ...

    def recover(self) -> list[JobStatus]:
        """Reconcile every non-terminal ledger entry with reality after a restart."""
        ...
