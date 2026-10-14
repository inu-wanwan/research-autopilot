"""Subscription-first routing of structured LLM calls (fake ``codex`` executable; the API path is patched)."""

import json
import stat
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from phase2_fakes import make_plan
from research_autopilot import llm_subscription as sub
from research_autopilot.config import ModelPrice
from research_autopilot.experiments.analyst import LlmBudgetError, _PricedAgent
from research_autopilot.literature.researcher import PricedLlm
from research_autopilot.models import ExecutionPlan, ReviewResult
from research_autopilot.planning import agents as ag
from tests_helpers_spec import spec

FAKE = """#!/bin/sh
d="$FAKE_CODEX_DIR"
n=$(cat "$d/count" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$d/count"
cat > "$d/stdin.$n"
echo "$@" > "$d/args.$n"
env > "$d/env.$n"
f="$d/out.$n"; [ -f "$f" ] || f="$d/out"
cat "$f"
rc="$d/rc.$n"; [ -f "$rc" ] || rc="$d/rc"
exit $(cat "$rc" 2>/dev/null || echo 0)
"""

PRICES = {"m": ModelPrice(input_per_1m=1.0, output_per_1m=10.0)}


class Out(BaseModel):
    answer: str
    score: int = 3


def events(message: str) -> str:
    return "\n".join(json.dumps(e) for e in [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": message}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20,
                                             "reasoning_output_tokens": 5}},
    ]) + "\n"


USAGE_LIMIT = json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again later."}})


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    d = tmp_path / "fake"
    d.mkdir()
    exe = tmp_path / "codex"
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_CODEX_DIR", str(d))
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_CODEX", str(exe))
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_FIRST", "codex")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    def calls() -> int:
        return int((d / "count").read_text()) if (d / "count").exists() else 0
    return SimpleNamespace(dir=d, calls=calls)


@pytest.fixture
def api(monkeypatch):
    """Patched Agents SDK call: records each API call and returns ``state.output``."""
    state = SimpleNamespace(calls=0, output=None)

    def run_sync(agent, prompt, **kw):
        state.calls += 1
        usage = SimpleNamespace(input_tokens=1_000_000, output_tokens=0, requests=1,
                                input_tokens_details=SimpleNamespace(cached_tokens=0),
                                output_tokens_details=SimpleNamespace(reasoning_tokens=0))
        return SimpleNamespace(final_output=state.output, context_wrapper=SimpleNamespace(usage=usage))
    monkeypatch.setattr(ag.Runner, "run_sync", run_sync)
    return state


def test_strict_schema_has_no_defaults():
    def keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from keys(v)
        elif isinstance(node, list):
            for v in node:
                yield from keys(v)
    for model in (Out, ExecutionPlan, ReviewResult):
        schema = sub.strict_schema(model)
        assert "default" not in set(keys(schema))
        assert schema.get("additionalProperties") is False


def test_subscription_success_no_api_call_zero_cost(fake_codex, api):
    (fake_codex.dir / "out").write_text(events(json.dumps({"answer": "yes", "score": 7})))
    agent = _PricedAgent("m", PRICES, lambda: 1.0)
    run = agent._call("X", "Be brief.", {"q": "hello"}, Out)
    assert run.output == Out(answer="yes", score=7)
    assert (run.cost_usd, run.billing) == (0.0, "subscription")
    assert run.usage.input_tokens == 100 and run.usage.reasoning_tokens == 5
    assert api.calls == 0 and fake_codex.calls() == 1
    args = (fake_codex.dir / "args.1").read_text()
    for part in ("exec --json --ephemeral --ignore-user-config -s read-only --skip-git-repo-check",
                 "-m m", 'model_reasoning_effort="medium"', "--output-schema"):
        assert part in args
    stdin = (fake_codex.dir / "stdin.1").read_text()
    assert "Be brief." in stdin and '"q": "hello"' in stdin
    env = (fake_codex.dir / "env.1").read_text()
    assert "sk-test-not-real" not in env and "OPENAI_API_KEY" not in env


def test_priced_llm_budget_not_consumed_by_subscription(fake_codex, api):
    (fake_codex.dir / "out").write_text(events(json.dumps({"answer": "a"})))
    llm = PricedLlm("m", PRICES, budget_usd=0.5)
    assert llm("X", "i", {}, Out).answer == "a"
    assert llm.spent == 0.0 and api.calls == 0


def test_planner_subscription_and_billing(fake_codex, api):
    (fake_codex.dir / "out").write_text(events(make_plan().model_dump_json()))
    run = ag.OpenAIPlanner("m").plan(spec(), None, [])
    assert run.billing == "subscription" and run.output.steps[0].id == "S1" and api.calls == 0


def test_usage_limit_falls_back_to_api_and_is_cached(fake_codex, api):
    (fake_codex.dir / "out").write_text(USAGE_LIMIT + "\n")
    (fake_codex.dir / "rc").write_text("1")
    api.output = Out(answer="api")
    agent = _PricedAgent("m", PRICES, lambda: 1.0)
    run = agent._call("X", "i", {}, Out)
    assert run.output.answer == "api" and run.billing == "api"
    assert run.cost_usd == pytest.approx(1.0)  # priced API cost is kept
    assert api.calls == 1 and fake_codex.calls() == 1
    assert sub.cooldown_remaining() > 0
    agent._call("X", "i", {}, Out)  # cooldown: straight to the API, no subprocess
    assert api.calls == 2 and fake_codex.calls() == 1


def test_invalid_json_retried_once_then_api(fake_codex, api):
    (fake_codex.dir / "out").write_text(events("not json"))
    api.output = Out(answer="api")
    run = _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out)
    assert run.billing == "api" and api.calls == 1 and fake_codex.calls() == 2
    assert sub.cooldown_remaining() == 0  # an invalid answer is not a sticky failure


def test_invalid_then_valid_on_retry(fake_codex, api):
    (fake_codex.dir / "out.1").write_text(events(json.dumps({"wrong": 1})))
    (fake_codex.dir / "out").write_text(events(json.dumps({"answer": "ok"})))
    run = _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out)
    assert run.billing == "subscription" and api.calls == 0 and fake_codex.calls() == 2


def test_flag_off_uses_api_only(fake_codex, api, monkeypatch):
    monkeypatch.delenv("RA_LLM_SUBSCRIPTION_FIRST")
    (fake_codex.dir / "out").write_text(events(json.dumps({"answer": "sub"})))
    api.output = Out(answer="api")
    run = _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out)
    assert run.output.answer == "api" and run.billing == "api"
    assert fake_codex.calls() == 0


def test_api_errors_keep_their_classes_after_fallback(fake_codex, monkeypatch):
    (fake_codex.dir / "out").write_text("")
    (fake_codex.dir / "rc").write_text("2")

    def run_sync(agent, prompt, **kw):
        raise ag.ModelBehaviorError("Invalid JSON")
    monkeypatch.setattr(ag.Runner, "run_sync", run_sync)
    with pytest.raises(ag.InvalidAgentOutput):
        ag.OpenAIReviewer("m").review(spec(), make_plan())
    assert fake_codex.calls() == 1


def test_unpriced_model_runs_on_subscription_but_api_is_refused(fake_codex, api):
    (fake_codex.dir / "out").write_text(events(json.dumps({"answer": "free"})))
    run = _PricedAgent("unpriced", PRICES, lambda: 1.0)._call("X", "i", {}, Out)
    assert run.billing == "subscription" and run.cost_usd == 0.0
    (fake_codex.dir / "out").write_text(USAGE_LIMIT + "\n")
    (fake_codex.dir / "rc").write_text("1")
    with pytest.raises(LlmBudgetError):
        _PricedAgent("unpriced", PRICES, lambda: 1.0)._call("X", "i", {}, Out)
    assert api.calls == 0


def test_missing_executable_falls_back(monkeypatch, api, tmp_path):
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_FIRST", "codex")
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_CODEX", str(tmp_path / "nope"))
    api.output = Out(answer="api")
    assert _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out).billing == "api"
    assert sub.cooldown_remaining() > 0


def test_claude_subscription_is_tried_after_codex_before_the_api(fake_codex, api, tmp_path, monkeypatch):
    (fake_codex.dir / "out").write_text(USAGE_LIMIT + "\n")
    (fake_codex.dir / "rc").write_text("1")
    cdir = tmp_path / "fakeclaude"
    cdir.mkdir()
    exe = tmp_path / "claude"
    exe.write_text(FAKE.replace("FAKE_CODEX_DIR", "FAKE_CLAUDE_DIR"))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    (cdir / "out").write_text(json.dumps({"subtype": "success", "is_error": False, "result": "",
                                          "structured_output": {"answer": "claude", "score": 4},
                                          "usage": {"input_tokens": 10, "output_tokens": 2}}))
    monkeypatch.setenv("FAKE_CLAUDE_DIR", str(cdir))
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_CLAUDE", str(exe))
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_FIRST", "codex,claude")
    sub._claude["billing"] = "subscription"
    run = _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out)
    assert run.output.answer == "claude" and run.billing == "subscription" and api.calls == 0
    assert "--json-schema" in (cdir / "args.1").read_text()
    sub._claude["billing"] = "api"  # an API-billed claude login is never used
    api.output = Out(answer="api")
    run = _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out)
    assert run.billing == "api" and api.calls == 1


def test_provider_list_parsing(monkeypatch):
    for v, want in (("codex", ["codex"]), ("true", ["codex"]), ("codex,claude", ["codex", "claude"]),
                    ("", []), ("claude", ["claude"])):
        monkeypatch.setenv("RA_LLM_SUBSCRIPTION_FIRST", v)
        assert sub.subscription_providers() == want


def test_claude_first_order_never_starts_codex_when_claude_answers(fake_codex, api, tmp_path, monkeypatch):
    """RA_LLM_SUBSCRIPTION_FIRST=claude,codex (planned for production, 2026-10-14): the written order is the try order."""
    cdir = tmp_path / "fakeclaude"
    cdir.mkdir()
    exe = tmp_path / "claude"
    exe.write_text(FAKE.replace("FAKE_CODEX_DIR", "FAKE_CLAUDE_DIR"))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    (cdir / "out").write_text(json.dumps({"subtype": "success", "is_error": False, "result": "",
                                          "structured_output": {"answer": "claude", "score": 4},
                                          "usage": {"input_tokens": 10, "output_tokens": 2}}))
    monkeypatch.setenv("FAKE_CLAUDE_DIR", str(cdir))
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_CLAUDE", str(exe))
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_FIRST", "claude,codex")
    sub._claude["billing"] = "subscription"
    assert sub.subscription_providers() == ["claude", "codex"]
    run = _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out)
    assert run.output.answer == "claude" and run.billing == "subscription" and api.calls == 0
    assert not (fake_codex.dir / "args.1").exists()  # codex was never started
