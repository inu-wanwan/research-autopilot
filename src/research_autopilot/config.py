"""Phase 2 settings, read from environment variables (see .env.example).

Secrets are held as ``SecretStr`` so they never appear in reprs, logs or
persisted records.
"""

import json
import os
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, field_validator


class ReviewPolicy(StrEnum):
    # Phase 2 runs the Reviewer on every plan. Other policies (e.g. conditional
    # review) can be added later without changing callers.
    ALWAYS = "always"


class ModelPrice(BaseModel):
    """USD per 1M tokens."""

    input_per_1m: float
    output_per_1m: float
    cached_input_per_1m: float | None = None


class Settings(BaseModel):
    notion_api_token: SecretStr | None = None
    notion_tasks_data_source_id: str | None = None
    linear_api_key: SecretStr | None = None
    linear_project_id: str | None = None
    openai_api_key: SecretStr | None = None

    # Scope / approval / status (docs/phase_specs/phase2.md §3)
    scope_label: str = "autopilot"
    approved_state_type: str = "unstarted"
    in_progress_state_name: str = "In Progress"
    required_sections: list[str] = Field(
        default_factory=lambda: ["purpose", "specification", "acceptance_criteria"]
    )
    linear_write_enabled: bool = False

    planner_model: str = "gpt-5.6-terra"
    reviewer_model: str = "gpt-5.6-sol"
    review_policy: ReviewPolicy = ReviewPolicy.ALWAYS
    max_replans: int = Field(default=2, ge=0, le=2)
    llm_invalid_output_retries: int = Field(default=1, ge=0, le=3)
    llm_transient_retries: int = Field(default=2, ge=0, le=5)
    agents_tracing: bool = False
    model_pricing: dict[str, ModelPrice] = Field(default_factory=dict)

    lease_ttl_seconds: int = Field(default=3600, ge=60)
    state_dir: Path = Path(".prefect/research-autopilot-state")
    database_url: str | None = None

    @field_validator("required_sections")
    @classmethod
    def _known_sections(cls, v: list[str]) -> list[str]:
        from research_autopilot.tasks.sections import CANONICAL_SECTIONS

        unknown = set(v) - set(CANONICAL_SECTIONS)
        if unknown:
            raise ValueError(f"unknown required sections: {sorted(unknown)}")
        return v

    def resolved_database_url(self) -> str:
        return self.database_url or f"sqlite:///{self.state_dir / 'research_autopilot.db'}"


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ) if env is None else env

    def get(name: str) -> str | None:
        value = env.get(name, "").strip()
        return value or None

    data: dict[str, object] = {
        "notion_api_token": get("NOTION_API_TOKEN"),
        "notion_tasks_data_source_id": get("NOTION_TASKS_DATA_SOURCE_ID"),
        "linear_api_key": get("LINEAR_API_KEY"),
        "linear_project_id": get("LINEAR_PROJECT_ID"),
        "openai_api_key": get("OPENAI_API_KEY"),
        "database_url": get("RA_DATABASE_URL"),
    }
    optional = {
        "RA_SCOPE_LABEL": ("scope_label", str),
        "RA_IN_PROGRESS_STATE": ("in_progress_state_name", str),
        "RA_PLANNER_MODEL": ("planner_model", str),
        "RA_REVIEWER_MODEL": ("reviewer_model", str),
        "RA_REVIEW_POLICY": ("review_policy", str),
        "RA_MAX_REPLANS": ("max_replans", int),
        "RA_LEASE_TTL_SECONDS": ("lease_ttl_seconds", int),
        "RA_STATE_DIR": ("state_dir", Path),
        "RA_LINEAR_WRITE_ENABLED": ("linear_write_enabled", _bool),
        "RA_AGENTS_TRACING": ("agents_tracing", _bool),
        "RA_REQUIRED_SECTIONS": ("required_sections", lambda s: [x.strip() for x in s.split(",") if x.strip()]),
        "RA_MODEL_PRICING": ("model_pricing", json.loads),
    }
    for var, (field, conv) in optional.items():
        if (raw := get(var)) is not None:
            data[field] = conv(raw)
    return Settings(**data)
