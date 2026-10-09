"""Model routing: implementer by complexity, reviewer by scientific risk, and escalation."""

from research_autopilot.implementation.config import Phase3Settings
from research_autopilot.implementation.models import (
    ESCALATABLE,
    TIER_ORDER,
    BackendName,
    ComplexityAssessment,
    ModelChoice,
    ReviewOutcome,
    Tier,
)

COMPLEXITY_TIER: dict[str, Tier] = {"low": "lightweight", "medium": "standard", "high": "high_reasoning"}


def choose(settings: Phase3Settings, backend: BackendName, tier: Tier) -> ModelChoice:
    return ModelChoice(backend=backend, tier=tier, model=settings.models[backend][tier],
                       reasoning_effort=settings.reasoning_effort.get(tier))


def implementer_choice(settings: Phase3Settings, a: ComplexityAssessment, backend: BackendName | None = None) -> ModelChoice:
    return choose(settings, backend or settings.primary_backend, COMPLEXITY_TIER[a.implementation_complexity])


def reviewer_choice(settings: Phase3Settings, a: ComplexityAssessment) -> ModelChoice:
    return choose(settings, settings.reviewer_backend, settings.reviewer_tier_by_risk[a.scientific_risk])


def escalation(settings: Phase3Settings, current: ModelChoice, reviews: list[ReviewOutcome]) -> tuple[ModelChoice, str | None]:
    """Return (choice for next attempt, reason or None).

    Escalate one tier only when the last ``escalate_after_repeats`` reviews
    share an escalatable failure category. Invariant / scope / dangerous
    failures never escalate (a stronger model must not be used to get
    around a constraint).
    """
    n = settings.escalate_after_repeats
    if len(reviews) < n:
        return current, None
    recent = reviews[-n:]
    if any(set(r.categories) - ESCALATABLE for r in recent):
        return current, None
    common = set(recent[0].categories)
    for r in recent[1:]:
        common &= set(r.categories)
    if not common:
        return current, None
    idx = TIER_ORDER.index(current.tier)
    if idx + 1 >= len(TIER_ORDER):
        return current, None
    nxt = choose(settings, current.backend, TIER_ORDER[idx + 1])
    return nxt, f"same failure repeated in {n} reviews ({sorted(common)}): {current.tier} -> {nxt.tier}"
