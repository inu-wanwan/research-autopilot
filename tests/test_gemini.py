"""Gemini via the Antigravity CLI (agy) on the Google AI Pro subscription as a fallback (user decisions 2026-10-16).

agy is a fake executable; the output shapes are the ones agy 1.4.0 printed on Eagle (one JSON object on stdout,
``status`` SUCCESS / ERROR, ``structured_output`` with ``--json-schema``, ``denied_actions`` for a denied tool)."""

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from phase3_fakes import GOOD_CODE, GOOD_TEST, make_impl_deps
from research_autopilot import llm_subscription as sub
from research_autopilot.config import ModelPrice
from research_autopilot.experiments.analyst import _PricedAgent
from research_autopilot.implementation.backends import gemini as g
from research_autopilot.implementation.config import load_phase3_settings
from research_autopilot.implementation.models import RunState
from research_autopilot.implementation.orchestrator import implement
from test_phase3 import EXP, GOOD

FAKE = """#!/bin/sh
d="$FAKE_AGY_DIR"
n=$(cat "$d/count" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$d/count"
cat > "$d/stdin.$n"
printf '%s\n' "$@" > "$d/args.$n"
env > "$d/env.$n"
o="$d/out.$n"; [ -f "$o" ] || o="$d/out"
[ -f "$o" ] && cat "$o"
[ -f "$d/err" ] && cat "$d/err" >&2
exit $(cat "$d/rc" 2>/dev/null || echo 0)
"""

USAGE = {"input_tokens": 1000, "output_tokens": 70, "thinking_tokens": 20, "cache_read_tokens": 400,
         "total_tokens": 1070}


def ok(response: str = "done", structured=None, denied_actions=None) -> str:
    r = {"conversation_id": "c", "status": "SUCCESS", "response": response, "num_turns": 1, "usage": USAGE}
    if structured is not None:
        r["structured_output"] = structured
    if denied_actions:
        r["denied_actions"] = [{"action": a, "display_name": a} for a in denied_actions]
    return json.dumps(r)


def error(message: str) -> str:
    return json.dumps({"conversation_id": "", "status": "ERROR", "response": "", "error": message, "usage": {}})


class Out(BaseModel):
    answer: str
    score: int = 3


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    d = tmp_path / "fake"
    d.mkdir()
    exe = tmp_path / "agy"
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "home"
    cli = home / ".gemini" / "antigravity-cli"
    (cli / "cache").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FAKE_AGY_DIR", str(d))
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_GEMINI", str(exe))

    def login(consumer: bool = True, token: bool = True) -> None:
        (cli / "cache" / "onboarding.json").write_text(json.dumps(
            {"consumerOnboardingComplete": consumer, "enterpriseOnboardingComplete": not consumer}))
        (cli / "antigravity-oauth-token").unlink(missing_ok=True)
        if token:
            (cli / "antigravity-oauth-token").write_text("x")

    login()
    return SimpleNamespace(dir=d, exe=str(exe), login=login,
                           calls=lambda: int((d / "count").read_text()) if (d / "count").exists() else 0,
                           args=lambda n=1: (d / f"args.{n}").read_text().split("\n"))


def request(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    return SimpleNamespace(task=SimpleNamespace(worktree_path=str(wt)), choice=SimpleNamespace(model="gemini-4-argon-high"),
                           prompt="IMPLEMENT", timeout_seconds=30, log_path=tmp_path / "logs" / "attempt-1-gemini.log",
                           check_commands=[])


# ---------------------------------------------------------------- backend


def test_implementer_run_is_a_known_zero_cost_subscription_call(fake_agy, tmp_path, monkeypatch):
    (fake_agy.dir / "out").write_text(ok())
    monkeypatch.setenv("AGY_LLM_GATEWAY_API_KEY", "not-real")
    r = g.GeminiBackend(fake_agy.exe).run(request(tmp_path), lambda pid: None)
    assert r.status == "completed" and r.summary == "done"
    assert r.cost_usd == 0.0  # known price: never an "unpriced paid call" for the research budget
    assert (r.usage.input_tokens, r.usage.cached_input_tokens, r.usage.output_tokens, r.usage.reasoning_tokens) \
        == (1000, 400, 70, 20)
    args = fake_agy.args()
    assert args[:7] == ["--output-format", "json", "--model", "gemini-4-argon-high", "--mode", "accept-edits",
                        "--disable-slash-commands"]
    assert "--dangerously-skip-permissions" not in args and "-p" not in args
    stdin = (fake_agy.dir / "stdin.1").read_text()
    assert stdin.endswith("IMPLEMENT") and "pre-approved shell commands" in stdin
    assert "AGY_LLM_GATEWAY_API_KEY" not in (fake_agy.dir / "env.1").read_text()


def test_a_denied_tool_is_reported_as_failed_for_the_review(fake_agy, tmp_path):
    (fake_agy.dir / "out").write_text(ok("", denied_actions=["command"]))
    r = g.GeminiBackend(fake_agy.exe).run(request(tmp_path), lambda pid: None)
    assert r.status == "failed" and "command" in r.error


def test_errors_are_classified(fake_agy, tmp_path):
    (fake_agy.dir / "rc").write_text("1")
    for message, status in (("RESOURCE_EXHAUSTED: You have exhausted your quota", "quota_exhausted"),
                            ("429 Too Many Requests", "rate_limited"),
                            ("401 Unauthorized", "auth_error"),
                            ("invalid model selection (--model x)", "process_error")):
        (fake_agy.dir / "out").write_text(error(message))
        assert g.GeminiBackend(fake_agy.exe).run(request(tmp_path), lambda pid: None).status == status, message


def test_only_a_consumer_google_login_is_used(fake_agy, tmp_path):
    backend = g.GeminiBackend(fake_agy.exe)
    assert backend.status().authenticated and backend.status().billing == "subscription"
    for kwargs in ({"consumer": False}, {"token": False}):
        fake_agy.login(**kwargs)
        assert not backend.status().authenticated
        r = backend.run(request(tmp_path), lambda pid: None)
        assert r.status == "auth_error" and fake_agy.calls() == 0


def test_review_runner_is_read_only_with_the_schema_enforced(fake_agy, tmp_path):
    from phase3_fakes import code_review
    from research_autopilot.implementation.backends.review_runners import GeminiReviewRunner
    (fake_agy.dir / "out").write_text(ok("text", structured=code_review("pass").model_dump(mode="json")))
    req = request(tmp_path)
    result, parsed = GeminiReviewRunner(g.GeminiBackend(fake_agy.exe))(req.task, req.choice, "REVIEW", {"type": "object"},
                                                                    tmp_path / "review.log")
    assert result.status == "completed" and parsed is not None and parsed.verdict == "pass"
    args = fake_agy.args()
    assert args[args.index("--mode") + 1] == "plan" and args[args.index("--json-schema") + 1] == '{"type": "object"}'
    assert (fake_agy.dir / "stdin.1").read_text() == "REVIEW"


def test_settings_default_order_puts_gemini_before_the_paid_api():
    s = load_phase3_settings({})
    assert s.fallback_backends == ["claude", "gemini", "codex_api"] and s.max_fallbacks == 3
    assert s.models["gemini"]["high_reasoning"] == "gemini-4-argon-high"
    assert load_phase3_settings({"RA_GEMINI_MODEL_HIGH": "gemini-x"}).models["gemini"]["high_reasoning"] == "gemini-x"


# ---------------------------------------------------------------- orchestrator


def test_both_subscriptions_exhausted_fall_back_to_gemini_before_the_api(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [{"status": "quota_exhausted", "files": {"h2_energy.py": GOOD_CODE}}],
                                        claude=[{"status": "quota_exhausted"}],
                                        gemini=[{"files": {"tests/test_h2.py": GOOD_TEST, **EXP}, "cost": 0.0}],
                                        codex_api=[])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED, m.block_reasons
    assert m.fallbacks_used == ["claude", "gemini"] and deps.backends["codex_api"].calls == []
    assert m.final_review.reviewer.backend == "codex" and deps.reviewers["gemini"].calls == []


def test_gemini_limit_moves_on_to_the_api(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [{"status": "quota_exhausted"}], claude=[{"status": "quota_exhausted"}],
                                        gemini=[{"status": "quota_exhausted"}], codex_api=[{**GOOD, "cost": 0.1}])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED and m.fallbacks_used == ["claude", "gemini", "codex_api"]


def test_gemini_does_not_review_work_it_wrote(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [{"status": "quota_exhausted"}], claude=[{"status": "quota_exhausted"}],
                                        gemini=[GOOD], reviewer=["quota_exhausted"],
                                        claude_reviewer=["quota_exhausted"])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.BLOCKED and deps.reviewers["gemini"].calls == []
    assert "LLM reviewer unavailable" in m.attempts[0].review.reason


def test_gemini_reviews_when_another_backend_wrote_the_work(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [GOOD], reviewer=["quota_exhausted"],
                                        claude_reviewer=["quota_exhausted"], gemini=[])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED and m.final_review.reviewer.backend == "gemini"


def test_gemini_configured_as_the_only_reviewer_blocks_its_own_work(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [{"status": "quota_exhausted"}], claude=[{"status": "quota_exhausted"}],
                                        gemini=[GOOD], reviewer_backend="gemini", fallback_backends=["claude", "gemini"])
    deps.reviewers.pop("claude")
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.BLOCKED and deps.reviewers["gemini"].calls == []


# ---------------------------------------------------------------- structured calls


PRICES = {"m": ModelPrice(input_per_1m=1.0, output_per_1m=10.0)}


@pytest.fixture
def api(monkeypatch):
    from research_autopilot.planning import agents as ag
    state = SimpleNamespace(calls=0, output=None)

    def run_sync(agent, prompt, **kw):
        state.calls += 1
        usage = SimpleNamespace(input_tokens=10, output_tokens=0, requests=1,
                                input_tokens_details=SimpleNamespace(cached_tokens=0),
                                output_tokens_details=SimpleNamespace(reasoning_tokens=0))
        return SimpleNamespace(final_output=state.output, context_wrapper=SimpleNamespace(usage=usage))
    monkeypatch.setattr(ag.Runner, "run_sync", run_sync)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    return state


def test_structured_call_on_gemini_is_a_subscription_call(fake_agy, api, monkeypatch):
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_FIRST", "gemini")
    (fake_agy.dir / "out").write_text(ok("answer=gemini", structured={"answer": "gemini", "score": 5}))
    run = _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out)
    assert run.output.answer == "gemini" and run.billing == "subscription" and api.calls == 0
    args = fake_agy.args()
    assert args[args.index("--mode") + 1] == "plan" and args[args.index("--model") + 1] == "gemini-4-argon-high"
    assert '"answer"' in args[args.index("--json-schema") + 1]
    assert "Do not use any tools" in (fake_agy.dir / "stdin.1").read_text()


def test_gemini_is_tried_after_claude_and_its_quota_is_cached(fake_agy, api, monkeypatch, tmp_path):
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_FIRST", "codex,claude,gemini")
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_CODEX", str(tmp_path / "no-codex"))
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_CLAUDE", str(tmp_path / "no-claude"))
    assert sub.subscription_providers() == ["codex", "claude", "gemini"]
    (fake_agy.dir / "out").write_text(ok(structured={"answer": "gemini"}))
    assert _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out).output.answer == "gemini"
    (fake_agy.dir / "out").write_text(error("RESOURCE_EXHAUSTED"))
    (fake_agy.dir / "rc").write_text("1")
    api.output = Out(answer="api")
    assert _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out).billing == "api"
    calls = fake_agy.calls()
    assert _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out).billing == "api"
    assert fake_agy.calls() == calls  # skipped during the cooldown


def test_invalid_gemini_answer_is_retried_once_then_the_api(fake_agy, api, monkeypatch):
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_FIRST", "gemini")
    (fake_agy.dir / "out").write_text(ok("I cannot answer.", denied_actions=["read_url"]))
    api.output = Out(answer="api")
    assert _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out).billing == "api"
    assert fake_agy.calls() == 2


def test_non_subscription_agy_login_is_never_used_for_structured_calls(fake_agy, api, monkeypatch):
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_FIRST", "gemini")
    fake_agy.login(consumer=False)
    api.output = Out(answer="api")
    assert _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out).billing == "api"
    assert fake_agy.calls() == 0
