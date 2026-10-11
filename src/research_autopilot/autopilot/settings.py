"""Phase 5 settings from the environment."""

import os

from pydantic import BaseModel, Field

from research_autopilot.autopilot.models import Budget


class Phase5Settings(BaseModel):
    write_enabled: bool = False  # RA_AUTOPILOT_WRITE: allow Notion / Linear writes at all
    test_only: bool = True  # RA_AUTOPILOT_TEST_ONLY: only write to "[TEST]" issues (Mac default)
    notes_data_source_id: str = "31769893-cf8f-80d7-8217-000b83f7f15f"
    budget_api_usd: float = 5.0
    budget_cpu_hours: float = 4.0
    poll_seconds: int = 60
    approval_poll_seconds: int = 60
    max_concurrent_tasks: int = Field(default=1, ge=1, le=16)
    dag_inference_model: str | None = None  # defaults to the Phase 2 reviewer model
    prefect_ui_url: str | None = None
    confirm_deployment: str = "approval-confirm/manual"
    approval_mode: str = "discord"  # Discord interaction is authoritative; legacy modes remain supported.
    approver_linear_user_ids: list[str] = []

    @property
    def budget(self) -> Budget:
        return Budget(api_usd=self.budget_api_usd, compute_cpu_hours=self.budget_cpu_hours)


def _b(v: str) -> bool:
    return v.strip().lower() in {"1", "true", "yes", "on"}


def load_phase5_settings(env: dict[str, str] | None = None) -> Phase5Settings:
    env = dict(os.environ) if env is None else env
    d = Phase5Settings().model_dump()
    conv = {"RA_AUTOPILOT_WRITE": ("write_enabled", _b), "RA_AUTOPILOT_TEST_ONLY": ("test_only", _b),
            "NOTION_NOTES_DATA_SOURCE_ID": ("notes_data_source_id", str),
            "RA_AUTOPILOT_BUDGET_USD": ("budget_api_usd", float), "RA_AUTOPILOT_BUDGET_CPU_HOURS": ("budget_cpu_hours", float),
            "RA_AUTOPILOT_POLL_SECONDS": ("poll_seconds", int), "PREFECT_UI_URL": ("prefect_ui_url", str),
            "RA_AUTOPILOT_MAX_CONCURRENT_TASKS": ("max_concurrent_tasks", int),
            "RA_DAG_INFERENCE_MODEL": ("dag_inference_model", str),
            "RA_APPROVAL_MODE": ("approval_mode", str),
            "RA_APPROVER_LINEAR_USER_IDS": ("approver_linear_user_ids", lambda v: [x.strip() for x in v.split(",") if x.strip()])}
    for var, (field, f) in conv.items():
        if (raw := env.get(var, "").strip()):
            d[field] = f(raw)
    return Phase5Settings(**d)
