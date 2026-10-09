import pytest
from pydantic import ValidationError

from phase2_fakes import make_plan, review
from research_autopilot.config import ModelPrice, load_settings
from research_autopilot.models import ExecutionPlan, ReviewResult
from research_autopilot.planning.agents import Usage
from research_autopilot.planning.cost import estimate_cost
from research_autopilot.tasks.sections import canonical_section, split_sections


@pytest.mark.parametrize("heading,expected", [
    ("Purpose", "purpose"), ("目的", "purpose"), ("1. Purpose:", "purpose"),
    ("Implementation", "specification"), ("Work", "specification"), ("実験仕様", "specification"),
    ("Acceptance criteria", "acceptance_criteria"), ("完了条件", "acceptance_criteria"),
    ("Artifacts", "artifact_requirements"), ("Dependencies", "dependencies"),
    ("Scientific invariants", "scientific_invariants"), ("Inputs", "input_artifacts"), ("Repository", "repository"),
    ("Allowed files", "file_scope"), ("Non-goals", "non_goals"), ("Notes", None), ("まとめる内容", None),
])
def test_heading_mapping(heading, expected):
    assert canonical_section(heading) == expected


def test_split_sections_nesting_and_preamble():
    canonical, other, preamble = split_sections([
        (0, "intro"), (2, "Purpose"), (0, "p"), (3, "Sub"), (0, "s"), (2, "Notes"), (0, "n"), (3, "Deeper"), (0, "d"),
    ])
    assert preamble == "intro"
    assert canonical == {"purpose": "p\n### Sub\ns"}
    assert other == {"Notes": "n", "Deeper": "d"}


def test_plan_validator_rejects_inconsistent_plans():
    good = make_plan(2).model_dump()
    bad_dep = {**good, "steps": [{**good["steps"][0], "depends_on": ["S9"]}, good["steps"][1]]}
    bad_order = {**good, "execution_order": ["S2", "S1"]}
    missing = {**good, "execution_order": ["S1"]}
    for data in (bad_dep, bad_order, missing, {**good, "steps": []}):
        with pytest.raises(ValidationError):
            ExecutionPlan.model_validate(data)
    ExecutionPlan.model_validate(good)


def test_review_validator():
    base = review("approved").model_dump()
    with pytest.raises(ValidationError):
        ReviewResult.model_validate({**base, "verdict": "revision_required", "revision_requests": []})
    with pytest.raises(ValidationError):
        ReviewResult.model_validate({**base, "verdict": "blocked", "blocking_reasons": []})
    with pytest.raises(ValidationError):
        ReviewResult.model_validate({**base, "verdict": "unknown"})


def test_cost_estimate_never_guesses():
    u = Usage(input_tokens=1_000_000, output_tokens=100_000, cached_input_tokens=200_000)
    assert estimate_cost(u, None) is None
    assert estimate_cost(u, ModelPrice(input_per_1m=1.0, output_per_1m=10.0, cached_input_per_1m=0.1)) \
        == pytest.approx(0.8 + 0.02 + 1.0)


def test_settings_from_env():
    s = load_settings({"OPENAI_API_KEY": "sk-x", "RA_MAX_REPLANS": "1", "RA_LINEAR_WRITE_ENABLED": "true",
                       "RA_MODEL_PRICING": '{"m": {"input_per_1m": 1, "output_per_1m": 2}}',
                       "RA_REQUIRED_SECTIONS": "purpose,acceptance_criteria"})
    assert s.max_replans == 1 and s.linear_write_enabled and s.model_pricing["m"].output_per_1m == 2
    assert "sk-x" not in repr(s)
    with pytest.raises(ValidationError):
        load_settings({"RA_MAX_REPLANS": "3"})  # capped at 2 by the spec
    with pytest.raises(ValidationError):
        load_settings({"RA_REQUIRED_SECTIONS": "purpose,nonsense"})
