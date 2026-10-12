"""Phase 4 settings. Defaults are deliberately small (safe on a laptop); a server
such as Eagle raises the job caps explicitly via environment variables."""

import os
from pathlib import Path

from pydantic import BaseModel, Field


class Phase4Settings(BaseModel):
    backend: str = "local"  # local | slurm
    artifact_root: Path = Path(".prefect/research-autopilot-state/experiments")
    env_cache_dir: Path = Path.home() / ".research-autopilot" / "envs"
    # Resource management: capacity kept for OS / Prefect / PostgreSQL / monitoring.
    reserve_cpu: float = Field(default=2.0, ge=0)
    reserve_memory_gb: float = Field(default=4.0, ge=0)
    # Hard caps for all research jobs together (None = host limit minus reserve).
    max_job_cpu_total: float | None = 2.0
    max_job_memory_gb_total: float | None = 4.0
    max_parallel_jobs: int = Field(default=2, ge=1)
    default_walltime_minutes: float = 30.0
    max_walltime_minutes: float = 120.0
    walltime_safety_factor: float = Field(default=2.0, ge=1.0)
    memory_safety_factor: float = Field(default=1.5, ge=1.0)
    # a job that fits the configured caps but not the memory free right now waits this long before blocking
    resource_wait_minutes: float = Field(default=30.0, ge=0)
    heartbeat_min_seconds: int = 5
    heartbeat_max_seconds: int = 600
    # Failure handling
    max_infra_retries: int = Field(default=2, ge=0)
    retry_backoff_seconds: float = 5.0
    max_roundtrips: int = Field(default=2, ge=0, le=2)
    # LLM analyst / reviewer (OpenAI Agents SDK; paid -> require configured prices)
    analyst_model: str = "gpt-5.6-terra"
    reviewer_model: str = "gpt-5.6-sol"
    max_llm_cost_usd_per_run: float = Field(default=1.0, ge=0)
    max_proposals_per_run: int = Field(default=3, ge=0)
    max_chain_depth: int = Field(default=3, ge=1)


def load_phase4_settings(env: dict[str, str] | None = None) -> Phase4Settings:
    env = dict(os.environ) if env is None else env
    conv = {
        "RA_EXP_BACKEND": ("backend", str), "RA_EXP_ARTIFACT_ROOT": ("artifact_root", Path),
        "RA_EXP_ENV_CACHE": ("env_cache_dir", Path),
        "RA_EXP_RESERVE_CPU": ("reserve_cpu", float), "RA_EXP_RESERVE_MEMORY_GB": ("reserve_memory_gb", float),
        "RA_EXP_MAX_JOB_CPU": ("max_job_cpu_total", float), "RA_EXP_MAX_JOB_MEMORY_GB": ("max_job_memory_gb_total", float),
        "RA_EXP_MAX_PARALLEL": ("max_parallel_jobs", int), "RA_EXP_MAX_WALLTIME_MIN": ("max_walltime_minutes", float),
        "RA_EXP_MAX_RETRIES": ("max_infra_retries", int), "RA_EXP_MAX_ROUNDTRIPS": ("max_roundtrips", int),
        "RA_ANALYST_MODEL": ("analyst_model", str), "RA_SCI_REVIEWER_MODEL": ("reviewer_model", str),
        "RA_EXP_MAX_LLM_COST_USD": ("max_llm_cost_usd_per_run", float),
        "RA_EXP_MAX_PROPOSALS": ("max_proposals_per_run", int),
    }
    data = Phase4Settings().model_dump()
    for var, (field, f) in conv.items():
        raw = env.get(var, "").strip()
        if raw:
            data[field] = None if raw.lower() == "host" and field.startswith("max_job") else f(raw)
    data["env_cache_dir"] = Path(data["env_cache_dir"]).expanduser()
    return Phase4Settings(**data)
