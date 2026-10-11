"""Build real dependencies from settings (credentials from the environment)."""

from research_autopilot.config import Settings
from research_autopilot.orchestrator.core import Deps
from research_autopilot.planning.agents import OpenAIPlanner, OpenAIReviewer
from research_autopilot.storage.db import Store
from research_autopilot.tasks.linear import LinearClient
from research_autopilot.tasks.manager import TaskManager
from research_autopilot.tasks.notion import NotionClient


class ConfigurationError(Exception):
    pass


def build_deps(settings: Settings, on_retry=None) -> Deps:
    missing = [name for name, value in [
        ("NOTION_API_TOKEN", settings.notion_api_token),
        ("NOTION_TASKS_DATA_SOURCE_ID", settings.notion_tasks_data_source_id),
        ("LINEAR_API_KEY", settings.linear_api_key),
        ("LINEAR_PROJECT_ID", settings.linear_project_id),
        ("OPENAI_API_KEY", settings.openai_api_key),
    ] if not value]
    if missing:
        raise ConfigurationError("missing settings: " + ", ".join(missing))
    notion = NotionClient(settings.notion_api_token.get_secret_value(), settings.notion_tasks_data_source_id,
                          on_retry=on_retry)
    linear = LinearClient(settings.linear_api_key.get_secret_value(), settings.linear_project_id,
                          on_retry=on_retry)
    # The Agents SDK reads OPENAI_API_KEY from the environment itself.
    from research_autopilot.catalog.integration import context_from_env
    store = Store(settings.resolved_database_url())
    planner = OpenAIPlanner(settings.planner_model, tracing=settings.agents_tracing)
    planner.environment_facts = _environment_facts()
    return Deps(
        manager=TaskManager(notion, linear, settings),
        linear=linear,
        planner=planner,
        reviewer=OpenAIReviewer(settings.reviewer_model, tracing=settings.agents_tracing),
        store=store,
        settings=settings,
        catalog=(catalog := context_from_env(store)),
        literature=_literature(settings, catalog),
        external=_external(store, catalog),
    )


def _external(store, catalog):
    """Phase 9 onboarding of reference implementations (Registry only; execution needs a human grant)."""
    from research_autopilot.external.service import optional_from_env
    try:
        return optional_from_env(store, catalog)
    except Exception:  # e.g. Docker missing: planning continues without onboarding
        return None


def _literature(settings: Settings, catalog):
    """Phase 8 Researcher (needs RA_LITERATURE_CONTACT_EMAIL); notes go to Notion Research Notes when configured."""
    import os
    if not os.environ.get("RA_LITERATURE_CONTACT_EMAIL", "").strip():
        return None
    from research_autopilot.literature.researcher import deps_from_env, research
    from research_autopilot.autopilot.settings import load_phase5_settings
    writer = None
    notes_ds = load_phase5_settings().notes_data_source_id
    if notes_ds and settings.notion_api_token:
        from research_autopilot.tasks.notion_writer import NotionWriter
        writer = NotionWriter(settings.notion_api_token.get_secret_value(), settings.notion_tasks_data_source_id,
                              notes_ds)

    def run(question: str, task_id: str | None, seeds: list[str], purpose: str | None = None):
        return research(deps_from_env(catalog, writer), question, task_id=task_id, seeds=seeds, purpose=purpose)
    return run


def _environment_facts():
    """Repository / test-environment facts for the Planner; None if the Phase 3 registry is unavailable."""
    try:
        from research_autopilot.implementation.config import load_phase3_settings, load_registry
        from research_autopilot.implementation.facts import environment_facts
        return environment_facts(load_registry(load_phase3_settings().repos_file))
    except Exception:
        return None
