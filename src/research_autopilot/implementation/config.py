"""Phase 3 settings (environment) and the repository registry (TOML)."""

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

from research_autopilot.implementation.models import BackendName, RepoConfig, Tier

# Verified on 2026-10-10 with real CLI calls (docs/phase_specs/phase3.md §2).
DEFAULT_MODELS: dict[BackendName, dict[Tier, str]] = {
    "codex": {"lightweight": "gpt-5.6-luna", "standard": "gpt-5.6-terra", "high_reasoning": "gpt-5.6-sol"},
    "claude": {"lightweight": "claude-haiku-4-5-20251001", "standard": "claude-sonnet-5-5",
               "high_reasoning": "claude-opus-5-5"},
}
DEFAULT_EFFORT: dict[Tier, str] = {"lightweight": "low", "standard": "medium", "high_reasoning": "high"}


class Phase3Settings(BaseModel):
    repos_file: Path = Path("config/research_repos.toml")
    worktrees_dir: Path = Path.home() / ".research-autopilot" / "worktrees"
    primary_backend: BackendName = "codex"
    fallback_backend: BackendName | None = "claude"
    reviewer_backend: BackendName = "codex"
    models: dict[BackendName, dict[Tier, str]] = Field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_MODELS.items()})
    reasoning_effort: dict[Tier, str] = Field(default_factory=lambda: dict(DEFAULT_EFFORT))
    # Reviewer tier by scientific risk (low risk still gets a standard reviewer).
    reviewer_tier_by_risk: dict[str, Tier] = Field(
        default_factory=lambda: {"low": "standard", "medium": "high_reasoning", "high": "high_reasoning"})

    max_revisions: int = Field(default=2, ge=0, le=2)
    max_attempts: int = Field(default=3, ge=1, le=3)
    max_fallbacks: int = Field(default=1, ge=0, le=1)
    max_concurrent_tasks: int = Field(default=1, ge=1, le=1)
    task_wall_time_seconds: int = Field(default=1800, ge=60)
    max_tokens_per_run: int = Field(default=3_000_000, ge=1)
    max_cost_usd_per_run: float = Field(default=5.0, ge=0)  # applies to backends that report cost
    claude_max_budget_usd_per_call: float = Field(default=2.0, gt=0)
    # Escalate after this many consecutive reviews with the same escalatable failure.
    escalate_after_repeats: int = Field(default=2, ge=1)


def load_phase3_settings(env: dict[str, str] | None = None) -> Phase3Settings:
    env = dict(os.environ) if env is None else env
    s = Phase3Settings()
    simple = {
        "RA_REPOS_FILE": ("repos_file", Path), "RA_WORKTREES_DIR": ("worktrees_dir", Path),
        "RA_IMPL_PRIMARY_BACKEND": ("primary_backend", str), "RA_IMPL_REVIEWER_BACKEND": ("reviewer_backend", str),
        "RA_IMPL_MAX_REVISIONS": ("max_revisions", int), "RA_IMPL_MAX_ATTEMPTS": ("max_attempts", int),
        "RA_IMPL_WALL_TIME_SECONDS": ("task_wall_time_seconds", int),
        "RA_IMPL_MAX_TOKENS": ("max_tokens_per_run", int), "RA_IMPL_MAX_COST_USD": ("max_cost_usd_per_run", float),
        "RA_CLAUDE_MAX_BUDGET_USD": ("claude_max_budget_usd_per_call", float),
    }
    data = s.model_dump()
    for var, (field, conv) in simple.items():
        if (raw := env.get(var, "").strip()):
            data[field] = conv(raw)
    data["worktrees_dir"] = Path(data["worktrees_dir"]).expanduser()
    if "RA_IMPL_FALLBACK_BACKEND" in env:
        data["fallback_backend"] = env["RA_IMPL_FALLBACK_BACKEND"].strip() or None
    for backend in ("codex", "claude"):
        for tier, suffix in (("lightweight", "LIGHT"), ("standard", "STANDARD"), ("high_reasoning", "HIGH")):
            if (raw := env.get(f"RA_{backend.upper()}_MODEL_{suffix}", "").strip()):
                data["models"][backend][tier] = raw
    return Phase3Settings(**data)


class RegistryError(Exception):
    pass


def load_registry(path: Path) -> dict[str, RepoConfig]:
    if not path.exists():
        raise RegistryError(f"repository registry not found: {path}")
    with path.open("rb") as f:
        raw = tomllib.load(f)
    repos = {}
    for repo_id, cfg in raw.get("repositories", {}).items():
        repos[repo_id] = RepoConfig(id=repo_id, **cfg)
    return repos
