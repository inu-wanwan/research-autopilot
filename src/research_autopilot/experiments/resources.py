"""Resource detection, estimation and reservation (oversubscription / OOM prevention)."""

import os
import re
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from research_autopilot.experiments.config import Phase4Settings
from research_autopilot.experiments.models import Allocation, HostResources, ResourceEstimate
from research_autopilot.implementation.experiment import ExperimentSpec

# CDI device of the host's NVIDIA GPUs (nvidia-ctk cdi generate; WSL's spec has only "all", so a job's own
# GPUs are selected with CUDA_VISIBLE_DEVICES)
CDI_GPU_DEVICE = "nvidia.com/gpu=all"

THREAD_VARS = ["OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
               "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS", "PYSCF_MAX_MEMORY"]


def detect_host() -> HostResources:
    notes: list[str] = []
    cpu_logical = os.cpu_count() or 1
    cpu_limit = float(cpu_logical)
    mem = psutil.virtual_memory()
    mem_limit = mem.total
    container = None
    # cgroup v2 limits (Docker / systemd / WSL distributions)
    cpu_max = Path("/sys/fs/cgroup/cpu.max")
    if cpu_max.exists():
        quota, period = (cpu_max.read_text().split() + ["100000"])[:2]
        if quota != "max":
            cpu_limit = min(cpu_limit, int(quota) / int(period))
            notes.append(f"cgroup cpu.max limits CPU to {cpu_limit:g}")
    mem_max = Path("/sys/fs/cgroup/memory.max")
    if mem_max.exists() and mem_max.read_text().strip() != "max":
        mem_limit = min(mem_limit, int(mem_max.read_text().strip()))
        notes.append("cgroup memory.max limits memory")
    if Path("/.dockerenv").exists():
        container = "docker"
    try:
        if "microsoft" in Path("/proc/version").read_text().lower():
            container = container or "wsl"
            notes.append("WSL detected: Windows host memory/CPU limits come from .wslconfig")
    except OSError:
        pass
    gpus: list[dict] = []
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=10).stdout
            for line in out.strip().splitlines():
                name, total, used = [x.strip() for x in line.split(",")]
                gpus.append({"name": name, "vendor": "nvidia", "kind": "cuda", "memory_gb": round(float(total) / 1024, 2),
                             "memory_used_gb": round(float(used) / 1024, 2)})
        except (OSError, ValueError, subprocess.TimeoutExpired):
            notes.append("nvidia-smi present but query failed")
    elif platform.system() == "Darwin" and platform.machine() == "arm64":
        gpus.append({"name": "Apple GPU", "vendor": "apple", "kind": "metal", "memory_gb": 0.0})
        notes.append("Apple Silicon GPU (Metal, unified memory); not usable as CUDA")
    return HostResources(
        platform=f"{platform.system()} {platform.machine()}", cpu_logical=cpu_logical, cpu_limit=cpu_limit,
        memory_total_gb=round(mem.total / 2**30, 2), memory_limit_gb=round(mem_limit / 2**30, 2),
        memory_available_gb=round(mem.available / 2**30, 2), gpus=gpus, container=container,
        load_avg_1m=round(os.getloadavg()[0], 2), notes=notes,
    )


def estimate(spec: ExperimentSpec, history: list[dict], settings: Phase4Settings) -> ResourceEstimate:
    """Spec estimate, corrected by history of the same experiment_id when available."""
    r = spec.resources
    rationale = [f"spec cpu={r.cpu.value:g} ({r.cpu.basis}, {r.cpu.confidence}): {r.cpu.rationale}",
                 f"spec memory={r.memory_gb.value:g}GB ({r.memory_gb.basis}): {r.memory_gb.rationale}",
                 f"spec walltime={r.walltime_minutes.value:g}min ({r.walltime_minutes.basis}): {r.walltime_minutes.rationale}"]
    cpu = max(1, int(round(r.cpu.value)))
    mem = max(0.25, r.memory_gb.value)
    wall = max(0.5, r.walltime_minutes.value)
    basis = "spec_required" if r.cpu.basis == r.memory_gb.basis == "required" else "spec_estimate"
    conf = min((r.cpu.confidence, r.memory_gb.confidence, r.walltime_minutes.confidence),
               key=["low", "medium", "high"].index)
    ok = [h for h in history if h.get("peak_rss_gb") is not None and h.get("wall_seconds")]
    if ok:
        peak = max(h["peak_rss_gb"] for h in ok)
        wall_hist = max(h["wall_seconds"] for h in ok) / 60
        mem, wall = max(mem, peak), max(wall, wall_hist)
        basis, conf = "history", "high" if len(ok) >= 2 else "medium"
        rationale.append(f"history of {len(ok)} successful run(s): peak RSS {peak:.2f}GB, wall {wall_hist:.1f}min")
    return ResourceEstimate(cpu=cpu, memory_gb=round(mem, 3), gpu=int(round(r.gpu.value)),
                            walltime_minutes=round(wall, 2), basis=basis, confidence=conf, rationale=rationale)


def docker_memory() -> tuple[float, float] | None:
    """(total, free) GB of the Docker VM, where container-mode jobs run; None when docker is unavailable.

    On macOS the VM's memory is reserved apart from the host's, so the host's free memory says nothing about
    what a container job can use."""
    try:
        total = int(subprocess.run(["docker", "info", "--format", "{{.MemTotal}}"], capture_output=True, text=True,
                                   timeout=30).stdout.strip() or 0)
        used = 0.0
        out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}"], capture_output=True,
                             text=True, timeout=60).stdout
        units = {"b": 1, "kib": 2**10, "mib": 2**20, "gib": 2**30, "kb": 1e3, "mb": 1e6, "gb": 1e9}
        for line in out.splitlines():
            m = re.match(r"\s*([\d.]+)\s*([KMG]i?B|B)", line.split("/")[0].strip(), re.I)
            if m:
                used += float(m.group(1)) * units.get(m.group(2).lower(), 1)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if total <= 0:
        return None
    return round(total / 2**30, 2), round(max(total - used, 0) / 2**30, 2)


def for_container(host: HostResources, memory: tuple[float, float] | None) -> HostResources:
    """The host as a container-mode job sees it: memory limit / free memory of the Docker VM."""
    if memory is None:
        return host
    total, free = memory
    return host.model_copy(update={"memory_limit_gb": total, "memory_available_gb": free, "docker_vm": True,
                                   "notes": [*host.notes, f"container mode: Docker VM memory {total:g}GB"]})


class ResourceError(Exception):
    """The job cannot fit within the configured caps / host capacity."""


class ResourceBusy(ResourceError):
    """The job fits the configured caps but not the memory free right now (other workloads): wait, do not block."""


@dataclass
class ResourceManager:
    settings: Phase4Settings
    host: HostResources
    reserved: dict[str, Allocation] = field(default_factory=dict)  # job_key -> allocation

    def static_capacity(self) -> tuple[float, float]:
        """The configured caps (host limit minus reserve, capped by the per-job totals).

        The memory reserve keeps memory for the host OS; the Docker VM's memory is already set apart from the
        host's, so a container-mode job is not charged the host reserve a second time."""
        cpu = max(self.host.cpu_limit - self.settings.reserve_cpu, 0)
        reserve_mem = 0.0 if self.host.docker_vm else self.settings.reserve_memory_gb
        mem = max(self.host.memory_limit_gb - reserve_mem, 0)
        if self.settings.max_job_cpu_total is not None:
            cpu = min(cpu, self.settings.max_job_cpu_total)
        if self.settings.max_job_memory_gb_total is not None:
            mem = min(mem, self.settings.max_job_memory_gb_total)
        return cpu, mem

    def capacity(self) -> tuple[float, float]:
        cpu, mem = self.static_capacity()
        # Never plan beyond what is currently free (other workloads keep running).
        mem = min(mem, max(self.host.memory_available_gb - 0.5, 0))
        return cpu, mem

    def plan(self, est: ResourceEstimate, scale: float = 1.0) -> Allocation:
        cap_cpu, cap_mem = self.capacity()
        if est.gpu > 0 and not any(g.get("kind") == "cuda" for g in self.host.gpus):
            raise ResourceError(f"job needs {est.gpu} GPU(s); no CUDA GPU on this host")
        cpu = min(est.cpu, int(cap_cpu)) or 0
        if cpu < 1:
            raise ResourceError(f"no CPU capacity left for jobs (cap {cap_cpu:g})")
        mem = round(est.memory_gb * self.settings.memory_safety_factor * scale, 3)
        static_mem = self.static_capacity()[1]
        if scale == 1.0 and est.memory_gb <= static_mem < mem:
            # First attempt: the estimate itself fits the cap, only the safety margin does not. Give the job the
            # cap rather than refusing it; an actual overrun is an OOM, whose scaled-up retry is then refused.
            mem = round(static_mem, 3)
        if mem > cap_mem:
            if mem <= static_mem:
                raise ResourceBusy(f"job needs ~{mem:g}GB (incl. safety factor); only {cap_mem:g}GB is free now "
                                   f"(cap {static_mem:g}GB)")
            raise ResourceError(f"job needs ~{mem:g}GB (incl. safety factor) but the job memory cap is {cap_mem:g}GB")
        wall_min = min(est.walltime_minutes * self.settings.walltime_safety_factor * scale,
                       self.settings.max_walltime_minutes)
        return Allocation(cpu=cpu, memory_gb=mem, gpu=est.gpu, walltime_seconds=int(wall_min * 60), threads=cpu,
                          estimate=est)

    def free_gpus(self) -> list[int]:
        """CUDA device indices not held by a running job (one job per GPU: a shared GPU runs out of memory)."""
        held = {i for a in self.reserved.values() for i in a.gpu_ids}
        return [i for i, g in enumerate(g for g in self.host.gpus if g.get("kind") == "cuda") if i not in held]

    def fits_now(self, alloc: Allocation) -> bool:
        cap_cpu, cap_mem = self.capacity()
        used_cpu = sum(a.cpu for a in self.reserved.values())
        used_mem = sum(a.memory_gb for a in self.reserved.values())
        return (len(self.reserved) < self.settings.max_parallel_jobs and used_cpu + alloc.cpu <= cap_cpu
                and used_mem + alloc.memory_gb <= cap_mem and alloc.gpu <= len(self.free_gpus()))

    def claim_gpus(self, alloc: Allocation) -> Allocation:
        """Pick the job's GPUs (call after ``fits_now``, before the job environment is built)."""
        return alloc.model_copy(update={"gpu_ids": self.free_gpus()[:alloc.gpu]}) if alloc.gpu else alloc

    def reserve(self, job_key: str, alloc: Allocation) -> None:
        self.reserved[job_key] = alloc

    def release(self, job_key: str) -> None:
        self.reserved.pop(job_key, None)


def thread_env(alloc: Allocation) -> dict[str, str]:
    env = {v: str(alloc.threads) for v in THREAD_VARS if v != "PYSCF_MAX_MEMORY"}
    # GPUs are exclusive: a job sees only its own (indices as nvidia-smi lists them); a CPU job sees none
    env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    env["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in alloc.gpu_ids)
    env["PYSCF_MAX_MEMORY"] = str(int(alloc.memory_gb * 1024 * 0.8))  # MB, leave headroom
    return env


def heartbeat_seconds(alloc: Allocation, settings: Phase4Settings) -> int:
    return int(min(max(alloc.walltime_seconds / 3, settings.heartbeat_min_seconds), settings.heartbeat_max_seconds))
