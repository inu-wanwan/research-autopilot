"""Build real Phase 3 dependencies from the environment."""

from research_autopilot.config import Settings, load_settings
from research_autopilot.implementation.backends.claude import ClaudeBackend
from research_autopilot.implementation.backends.codex import CodexBackend
from research_autopilot.implementation.backends.review_runners import ClaudeReviewRunner, CodexReviewRunner
from research_autopilot.implementation.config import Phase3Settings, load_phase3_settings, load_registry
from research_autopilot.implementation.orchestrator import ImplDeps
from research_autopilot.storage.db import Store
from research_autopilot.tasks.linear import LinearClient
from research_autopilot.tasks.manager import TaskManager
from research_autopilot.tasks.notion import NotionClient


def build_impl_deps(phase2: Settings | None = None, settings: Phase3Settings | None = None) -> ImplDeps:
    phase2 = phase2 or load_settings()
    settings = settings or load_phase3_settings()
    manager = None
    if phase2.notion_api_token and phase2.linear_api_key:
        manager = TaskManager(
            NotionClient(phase2.notion_api_token.get_secret_value(), phase2.notion_tasks_data_source_id),
            LinearClient(phase2.linear_api_key.get_secret_value(), phase2.linear_project_id), phase2)
    else:
        raise RuntimeError("Phase 3 requires NOTION_API_TOKEN and LINEAR_API_KEY for the approval gate")
    return ImplDeps(
        settings=settings, phase2=phase2, registry=load_registry(settings.repos_file),
        store=Store(phase2.resolved_database_url()), manager=manager,
        backends={"codex": CodexBackend(), "claude": ClaudeBackend()},
        reviewers={"codex": CodexReviewRunner(),
                   "claude": ClaudeReviewRunner(max_budget_usd=settings.claude_max_budget_usd_per_call)},
    )
