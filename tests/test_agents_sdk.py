"""Error mapping of the Agents SDK wrapper (no network: Runner.run_sync is patched)."""

from types import SimpleNamespace

import httpx
import openai
import pytest
from agents import ModelBehaviorError

from phase2_fakes import make_plan
from research_autopilot.planning import agents as ag
from tests_helpers_spec import spec


def fake_result(output):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, requests=1,
                            input_tokens_details=SimpleNamespace(cached_tokens=2),
                            output_tokens_details=SimpleNamespace(reasoning_tokens=3))
    return SimpleNamespace(final_output=output, context_wrapper=SimpleNamespace(usage=usage))


def patch_run(monkeypatch, outcome):
    def run_sync(agent, prompt, **kw):
        assert kw["run_config"].tracing_disabled is True
        assert agent.output_type is not None
        if isinstance(outcome, Exception):
            raise outcome
        return fake_result(outcome)
    monkeypatch.setattr(ag.Runner, "run_sync", run_sync)


def test_planner_success_and_usage(monkeypatch):
    patch_run(monkeypatch, make_plan())
    run = ag.OpenAIPlanner("gpt-5.6-terra").plan(spec(), None, [])
    assert run.output.steps[0].id == "S1"
    assert (run.usage.input_tokens, run.usage.cached_input_tokens, run.usage.reasoning_tokens) == (10, 2, 3)


def test_dict_output_is_validated(monkeypatch):
    patch_run(monkeypatch, {"summary": "x"})
    with pytest.raises(ag.InvalidAgentOutput):
        ag.OpenAIPlanner("m").plan(spec(), None, [])


def test_model_behavior_error_is_invalid_output(monkeypatch):
    patch_run(monkeypatch, ModelBehaviorError("Invalid JSON when parsing"))
    with pytest.raises(ag.InvalidAgentOutput):
        ag.OpenAIReviewer("m").review(spec(), make_plan())


def _resp(code):
    return httpx.Response(code, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))


@pytest.mark.parametrize("exc,expected", [
    (openai.APITimeoutError(request=httpx.Request("POST", "https://x")), ag.TransientAgentError),
    (openai.InternalServerError("boom", response=_resp(500), body=None), ag.TransientAgentError),
    (openai.RateLimitError("slow down", response=_resp(429), body={"code": "rate_limit_exceeded"}), ag.TransientAgentError),
    (openai.RateLimitError("quota", response=_resp(429), body={"code": "insufficient_quota"}), ag.AgentConfigError),
    (openai.AuthenticationError("bad key", response=_resp(401), body=None), ag.AgentConfigError),
])
def test_openai_errors_are_classified(monkeypatch, exc, expected):
    patch_run(monkeypatch, exc)
    with pytest.raises(expected):
        ag.OpenAIPlanner("m").plan(spec(), None, [])
