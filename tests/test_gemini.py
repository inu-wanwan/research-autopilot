"""Gemini CLI on the Google AI Pro subscription as a fallback (user decision 2026-10-16).

The CLI is a fake executable; the output shapes follow the @google/gemini-cli 0.64.0 package (JSON on stdout,
error JSON on stderr, exit 41 = authentication)."""

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
from test_phase3 import EXP, GOOD, codes

FAKE = """#!/bin/sh
d="$FAKE_GEMINI_DIR"
n=$(cat "$d/count" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$d/count"
cat > "$d/stdin.$n"
echo "$@" > "$d/args.$n"
env > "$d/env.$n"
for a in "$@"; do case "$prev" in --policy) cp "$a" "$d/policy.$n";; esac; prev="$a"; done
[ -f "$d/out" ] && cat "$d/out"
[ -f "$d/err" ] && cat "$d/err" >&2
exit $(cat "$d/rc" 2>/dev/null || echo 0)
"""


def ok_output(response: str) -> str:
    return json.dumps({"session_id": "s", "response": response, "stats": {"models": {
        "gemini-3.1-pro-preview": {"tokens": {"prompt": 1000, "cached": 400, "candidates": 50, "thoughts": 20}},
        "gemini-3.5-flash": {"tokens": {"prompt": 100, "cached": 0, "candidates": 5, "thoughts": 0}}}}}, indent=2)


QUOTA = json.dumps({"session_id": "s", "error": {"type": "TerminalQuotaError", "code": 429,
                                                  "message": "You have exhausted your daily quota on this model."}})


class Out(BaseModel):
    answer: str
    score: int = 3


@pytest.fixture
def fake_gemini(tmp_path, monkeypatch):
    d = tmp_path / "fake"
    d.mkdir()
    exe = tmp_path / "gemini"
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "ghome"
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "oauth_creds.json").write_text("{}")
    monkeypatch.setenv("GEMINI_CLI_HOME", str(home))
    monkeypatch.setenv("FAKE_GEMINI_DIR", str(d))
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_GEMINI", str(exe))

    def login(kind: str = "oauth-personal") -> None:
        (home / ".gemini" / "settings.json").write_text(json.dumps({"security": {"auth": {"selectedType": kind}}}))

    login()
    return SimpleNamespace(dir=d, exe=str(exe), login=login,
                           calls=lambda: int((d / "count").read_text()) if (d / "count").exists() else 0)


def request(tmp_path, check_commands=None):
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    return SimpleNamespace(task=SimpleNamespace(worktree_path=str(wt)), choice=SimpleNamespace(model="gemini-3.1-pro-preview"),
                           prompt="IMPLEMENT", timeout_seconds=30, log_path=tmp_path / "logs" / "attempt-1-gemini.log",
                           check_commands=check_commands or [])


# ---------------------------------------------------------------- backend


def test_implementer_run_is_a_known_zero_cost_subscription_call(fake_gemini, tmp_path, monkeypatch):
    (fake_gemini.dir / "out").write_text(ok_output("done"))
    monkeypatch.setenv("GEMINI_API_KEY", "not-real")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    r = g.GeminiBackend(fake_gemini.exe).run(request(tmp_path, [["uv", "run", "pytest", "-q"]]), lambda pid: None)
    assert r.status == "completed" and r.summary == "done"
    assert r.cost_usd == 0.0  # known price: never an "unpriced paid call" for the research budget
    assert (r.usage.input_tokens, r.usage.cached_input_tokens, r.usage.output_tokens) == (1100, 400, 75)
    args = (fake_gemini.dir / "args.1").read_text()
    for flag in ("-o json", "-m gemini-3.1-pro-preview", "--approval-mode auto_edit", "--skip-trust", "-e none",
                 "--allowed-mcp-server-names __none__"):
        assert flag in args
    assert (fake_gemini.dir / "stdin.1").read_text() == "IMPLEMENT"
    env = (fake_gemini.dir / "env.1").read_text()
    assert "GEMINI_API_KEY" not in env and "GOOGLE_GENAI_USE_VERTEXAI" not in env
    policy = (fake_gemini.dir / "policy.1").read_text()
    assert '"write_file"' in policy and '"uv run pytest -q"' in policy and 'toolName = "*"\ndecision = "deny"' in policy


def test_quota_error_on_stderr_with_an_http_exit_code_is_quota_exhausted(fake_gemini, tmp_path):
    (fake_gemini.dir / "err").write_text("[ERROR] something\n" + QUOTA + "\n")
    (fake_gemini.dir / "rc").write_text("173")  # 429 as seen by the OS
    r = g.GeminiBackend(fake_gemini.exe).run(request(tmp_path), lambda pid: None)
    assert r.status == "quota_exhausted" and "daily quota" in r.error


def test_auth_exit_code_is_auth_error(fake_gemini, tmp_path):
    (fake_gemini.dir / "err").write_text("Please set an Auth method in your settings.json\n")
    (fake_gemini.dir / "rc").write_text("41")
    assert g.GeminiBackend(fake_gemini.exe).run(request(tmp_path), lambda pid: None).status == "auth_error"


def test_api_billed_login_is_refused_without_starting_the_cli(fake_gemini, tmp_path):
    fake_gemini.login("gemini-api-key")
    backend = g.GeminiBackend(fake_gemini.exe)
    assert not backend.status().authenticated and backend.status().billing == "api"
    before = fake_gemini.calls()  # status() runs --version
    r = backend.run(request(tmp_path), lambda pid: None)
    assert r.status == "auth_error" and fake_gemini.calls() == before
    fake_gemini.login()
    assert backend.status().authenticated and backend.status().billing == "subscription"


def test_policies_by_mode():
    read, none = g.policy_toml("read"), g.policy_toml(g.NO_TOOLS)
    assert '"read_file"' in read and "write_file" not in read and "run_shell_command" not in read
    assert none.count("[[rule]]") == 1 and 'decision = "deny"' in none


def test_settings_default_order_puts_gemini_before_the_paid_api():
    s = load_phase3_settings({})
    assert s.fallback_backends == ["claude", "gemini", "codex_api"] and s.max_fallbacks == 3
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


def test_structured_call_on_gemini_is_a_subscription_call(fake_gemini, api, monkeypatch):
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_FIRST", "gemini")
    (fake_gemini.dir / "out").write_text(ok_output('```json\n{"answer": "gemini", "score": 5}\n```'))
    run = _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out)
    assert run.output.answer == "gemini" and run.billing == "subscription" and api.calls == 0
    stdin = (fake_gemini.dir / "stdin.1").read_text()
    assert "JSON Schema" in stdin and '"answer"' in stdin
    assert (fake_gemini.dir / "policy.1").read_text().count("[[rule]]") == 1  # no tools at all
    assert "--approval-mode default" in (fake_gemini.dir / "args.1").read_text()


def test_gemini_is_tried_after_claude_and_its_quota_is_cached(fake_gemini, api, monkeypatch, tmp_path):
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_FIRST", "codex,claude,gemini")
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_CODEX", str(tmp_path / "no-codex"))
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_CLAUDE", str(tmp_path / "no-claude"))
    assert sub.subscription_providers() == ["codex", "claude", "gemini"]
    (fake_gemini.dir / "out").write_text(ok_output('{"answer": "gemini"}'))
    assert _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out).output.answer == "gemini"
    (fake_gemini.dir / "out").unlink()
    (fake_gemini.dir / "err").write_text(QUOTA)
    (fake_gemini.dir / "rc").write_text("173")
    api.output = Out(answer="api")
    assert _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out).billing == "api"
    calls = fake_gemini.calls()
    assert _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out).billing == "api"
    assert fake_gemini.calls() == calls  # skipped during the cooldown


def test_invalid_gemini_answer_is_retried_once_then_the_api(fake_gemini, api, monkeypatch):
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_FIRST", "gemini")
    (fake_gemini.dir / "out").write_text(ok_output("I cannot answer in JSON."))
    api.output = Out(answer="api")
    assert _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out).billing == "api"
    assert fake_gemini.calls() == 2


def test_api_billed_gemini_login_is_never_used_for_structured_calls(fake_gemini, api, monkeypatch):
    monkeypatch.setenv("RA_LLM_SUBSCRIPTION_FIRST", "gemini")
    fake_gemini.login("gemini-api-key")
    api.output = Out(answer="api")
    assert _PricedAgent("m", PRICES, lambda: 1.0)._call("X", "i", {}, Out).billing == "api"
    assert fake_gemini.calls() == 0
