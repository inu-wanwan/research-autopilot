"""Build real Phase 4 dependencies from the environment."""

from research_autopilot.config import load_settings
from research_autopilot.experiments.analyst import OpenAIAnalyst, OpenAIScientificReviewer
from research_autopilot.experiments.backends.base import BackendNotConfigured
from research_autopilot.experiments.backends.local import LocalBackend
from research_autopilot.experiments.backends.slurm import SlurmBackend, SlurmConfig
from research_autopilot.experiments.config import load_phase4_settings
from research_autopilot.experiments.orchestrator import ExpDeps
from research_autopilot.implementation.factory import build_impl_deps
from research_autopilot.storage.db import Store


def build_exp_deps() -> ExpDeps:
    phase2 = load_settings()
    settings = load_phase4_settings()
    impl = build_impl_deps(phase2)
    store = Store(phase2.resolved_database_url())
    if settings.backend == "local":
        backend = LocalBackend(store)
    elif settings.backend == "slurm":
        backend = SlurmBackend(SlurmConfig(), store)  # unconfigured on purpose: submit -> BackendNotConfigured
    else:
        raise BackendNotConfigured(f"unknown backend {settings.backend}")
    def remaining() -> float:
        # Per-run spend is enforced by the orchestrator from the manifest's recorded costs;
        # the agents themselves only refuse models without a configured price.
        return settings.max_llm_cost_usd_per_run

    return ExpDeps(
        settings=settings, phase2=phase2, store=store, manager=impl.manager, backend=backend, registry=impl.registry,
        analyst=OpenAIAnalyst(settings.analyst_model, phase2.model_pricing, remaining, phase2.agents_tracing),
        reviewer=OpenAIScientificReviewer(settings.reviewer_model, phase2.model_pricing, remaining, phase2.agents_tracing),
        impl_deps=impl,
    )
