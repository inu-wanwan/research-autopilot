"""Structured LLM calls on the ChatGPT/Codex subscription (``codex exec``), with API fallback routing.

User decision (2026-10-12, until production): use the subscription as much as possible and fall back to the
pay-as-you-go OpenAI API only when it is impossible. ``RA_LLM_SUBSCRIPTION_FIRST=codex`` turns this on for every
structured call that goes through ``planning.agents._AgentsSdkBase`` (planner / reviewer, analyst, scientific
reviewer, completion reviewer, dependency inferer, adapter generator, literature / research / external LLM).

The CLI runs read-only in an empty temporary directory with the sanitized environment (no API key is passed), so
it uses its own subscription login (default ``CODEX_HOME``). A subscription call costs 0 USD for the budgets.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

ENV_FLAG = "RA_LLM_SUBSCRIPTION_FIRST"
COOLDOWN_SECONDS = 15 * 60  # after a usage limit / auth problem the subscription is skipped for this long
DEFAULT_TIMEOUT = 900
DEFAULT_EFFORT = "medium"

# Failures after which the next calls go straight to the API for COOLDOWN_SECONDS (they will not heal per call).
_STICKY = {"quota_exhausted", "auth_error", "not_installed"}

_lock = threading.Lock()
_skip_until = 0.0
_skip_reason = ""


class SubscriptionCallError(Exception):
    """A subscription call that did not produce a valid result; ``status`` says why."""

    def __init__(self, status: str, detail: str = "") -> None:
        super().__init__(f"{status}: {detail}" if detail else status)
        self.status, self.detail = status, detail


def subscription_mode() -> str | None:
    """``"codex"`` when structured calls should try the subscription first, else ``None`` (default: off)."""
    return "codex" if subscription_providers() else None


def subscription_providers() -> list[str]:
    """Subscriptions to try, in the written order, before the API: ``codex`` (default for truthy values),
    ``claude`` and/or ``gemini`` (``codex,claude,gemini``: Claude when Codex is at its limit, then Gemini)."""
    v = os.environ.get(ENV_FLAG, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return ["codex"]
    return [p for p in (x.strip() for x in v.split(",")) if p in ("codex", "claude", "gemini")]


def strict_schema(output_type: type[BaseModel]) -> dict:
    """Strict structured-output schema for ``codex --output-schema`` (which rejects 'default' keywords)."""
    from agents.agent_output import AgentOutputSchema

    from research_autopilot.implementation.reviewer import _strip_defaults
    return _strip_defaults(AgentOutputSchema(output_type).json_schema())


def cooldown_remaining() -> float:
    with _lock:
        return max(0.0, _skip_until - time.monotonic())


def mark_unavailable(reason: str, seconds: float = COOLDOWN_SECONDS) -> None:
    global _skip_until, _skip_reason
    with _lock:
        _skip_until, _skip_reason = time.monotonic() + seconds, reason


def reset() -> None:
    """Forget a cached unavailability (tests, or after re-login)."""
    global _skip_until, _skip_reason
    with _lock:
        _skip_until, _skip_reason = 0.0, ""
    _claude.update(skip_until=0.0, billing=None)
    _gemini.update(skip_until=0.0)


def _executable() -> str:
    return os.environ.get("RA_LLM_SUBSCRIPTION_CODEX", "").strip() or "codex"


def codex_structured(model: str, instructions: str, prompt: str, output_type: type[BaseModel],
                     timeout: int = DEFAULT_TIMEOUT, reasoning_effort: str | None = None,
                     executable: str | None = None) -> tuple[BaseModel, dict]:
    """One ``codex exec`` structured call on the subscription login. Returns (parsed output, usage dict).

    Raises ``SubscriptionCallError`` with status quota_exhausted / rate_limited / auth_error / transient_error /
    process_error / timeout / not_installed / invalid_output.
    """
    from research_autopilot.implementation.backends.base import classify_error_text, run_process, sanitized_env, tail
    from research_autopilot.implementation.backends.codex import parse_events

    exe = executable or _executable()
    if not shutil.which(exe):
        raise SubscriptionCallError("not_installed", f"{exe} not on PATH")
    with tempfile.TemporaryDirectory(prefix="ra-llm-sub-") as tmp:
        root = Path(tmp)
        work = root / "work"
        work.mkdir()
        schema = root / "schema.json"
        schema.write_text(json.dumps(strict_schema(output_type)), encoding="utf-8")
        cmd = [exe, "exec", "--json", "--ephemeral", "--ignore-user-config", "-s", "read-only",
               "--skip-git-repo-check", "-C", str(work), "-m", model,
               "-c", f'model_reasoning_effort="{reasoning_effort or DEFAULT_EFFORT}"',
               "--output-schema", str(schema), "-"]
        stdin = f"{instructions.strip()}\n\n---\nINPUT:\n{prompt}\n"
        out = run_process(cmd, work, stdin, timeout, root / "codex.log", lambda pid: None, env=sanitized_env())
    usage_obj, message, errors = parse_events(out.stdout)
    usage = usage_obj.model_dump()
    if out.timed_out:
        raise SubscriptionCallError("timeout", f"timed out after {timeout}s")
    if out.exit_code != 0 or errors:
        text = " ".join(errors) + " " + tail(out.stderr, 2000)
        status = classify_error_text(text) or "process_error"
        raise SubscriptionCallError(status, tail(" | ".join(errors) or out.stderr, 300))
    try:
        return output_type.model_validate(json.loads(message)), usage
    except (ValueError, ValidationError) as exc:
        raise SubscriptionCallError("invalid_output", str(exc)[:300]) from exc


CLAUDE_MODEL_ENV = "RA_LLM_SUBSCRIPTION_CLAUDE_MODEL"
_claude = {"skip_until": 0.0, "billing": None}


def claude_structured(instructions: str, prompt: str, output_type: type[BaseModel],
                      timeout: int = DEFAULT_TIMEOUT, executable: str | None = None,
                      model: str | None = None) -> tuple[BaseModel, dict]:
    """One ``claude -p --json-schema`` call on the claude.ai subscription (no tools, empty directory, sanitized
    environment). Refuses (auth_error) when the login is API-billed, so it never adds pay-as-you-go cost."""
    from research_autopilot.implementation.backends.base import classify_error_text, run_process, sanitized_env, tail
    from research_autopilot.implementation.backends.claude import ClaudeBackend

    exe = executable or os.environ.get("RA_LLM_SUBSCRIPTION_CLAUDE", "").strip() or "claude"
    if not shutil.which(exe):
        raise SubscriptionCallError("not_installed", f"{exe} not on PATH")
    if _claude["billing"] is None:
        _claude["billing"] = ClaudeBackend(exe).status().billing
    if _claude["billing"] != "subscription":
        raise SubscriptionCallError("auth_error", f"claude login is not a subscription ({_claude['billing']})")
    model = model or os.environ.get(CLAUDE_MODEL_ENV, "").strip() or "claude-opus-5-5"
    with tempfile.TemporaryDirectory(prefix="ra-llm-sub-claude-") as tmp:
        root = Path(tmp)
        cmd = [exe, "-p", "--output-format", "json", "--model", model, "--no-session-persistence",
               "--strict-mcp-config", "--permission-mode", "default", "--tools", "",
               "--json-schema", json.dumps(strict_schema(output_type))]
        stdin = f"{instructions.strip()}\n\n---\nINPUT:\n{prompt}\n"
        out = run_process(cmd, root, stdin, timeout, root / "claude.log", lambda pid: None, env=sanitized_env())
    if out.timed_out:
        raise SubscriptionCallError("timeout", f"timed out after {timeout}s")
    try:
        r = json.loads(out.stdout)
    except ValueError:
        r = {}
    u = r.get("usage") or {}
    usage = {"input_tokens": int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0))
             + int(u.get("cache_creation_input_tokens", 0)),
             "cached_input_tokens": int(u.get("cache_read_input_tokens", 0)),
             "output_tokens": int(u.get("output_tokens", 0)), "reasoning_tokens": 0}
    if out.exit_code != 0 or not r or r.get("is_error"):
        text = f"{r.get('result', '')} {r.get('subtype', '')} {tail(out.stderr, 2000)}"
        raise SubscriptionCallError(classify_error_text(text) or "process_error", tail(text.strip(), 300))
    data = r.get("structured_output")
    try:
        if data is None:
            data = json.loads(r.get("result") or "")
        return output_type.model_validate(data), usage
    except (ValueError, ValidationError) as exc:
        raise SubscriptionCallError("invalid_output", str(exc)[:300]) from exc


GEMINI_MODEL_ENV = "RA_LLM_SUBSCRIPTION_GEMINI_MODEL"
GEMINI_DEFAULT_MODEL = "gemini-4-argon-high"  # `agy models` on Eagle, 2026-10-16
_gemini = {"skip_until": 0.0}


def gemini_structured(instructions: str, prompt: str, output_type: type[BaseModel],
                      timeout: int = DEFAULT_TIMEOUT, executable: str | None = None,
                      model: str | None = None) -> tuple[BaseModel, dict]:
    """One headless Antigravity CLI call (``agy --mode plan --json-schema``) on the Google AI Pro login, in an
    empty directory with the sanitized environment. Refuses (auth_error) unless agy is logged in with a consumer
    Google account, so it never adds pay-as-you-go cost."""
    from research_autopilot.implementation.backends import gemini as g
    from research_autopilot.implementation.backends.base import run_process, sanitized_env, tail

    exe = executable or os.environ.get("RA_LLM_SUBSCRIPTION_GEMINI", "").strip() or "agy"
    if not shutil.which(exe):
        raise SubscriptionCallError("not_installed", f"{exe} not on PATH")
    ok, detail = g.login_state()
    if not ok:
        raise SubscriptionCallError("auth_error", f"agy is not logged in with a Google AI Pro account ({detail})")
    model = model or os.environ.get(GEMINI_MODEL_ENV, "").strip() or GEMINI_DEFAULT_MODEL
    with tempfile.TemporaryDirectory(prefix="ra-llm-sub-gemini-") as tmp:
        root = Path(tmp)
        work = root / "work"
        work.mkdir()
        stdin = f"{instructions.strip()}\n\n{g.NO_TOOLS_NOTE}\n\n---\nINPUT:\n{prompt}\n"
        out = run_process(g.command(exe, model, "plan", json.dumps(strict_schema(output_type))), work, stdin,
                          timeout, root / "agy.log", lambda pid: None, env=sanitized_env())
    if out.timed_out:
        raise SubscriptionCallError("timeout", f"timed out after {timeout}s")
    result = g.parse_output(out.stdout)
    u = g.usage_of(result)
    usage = {"input_tokens": u.input_tokens, "cached_input_tokens": u.cached_input_tokens,
             "output_tokens": u.output_tokens, "reasoning_tokens": u.reasoning_tokens}
    if out.exit_code != 0 or result.get("status") != "SUCCESS":
        raise SubscriptionCallError(g.classify(result, out.stderr), tail(str(result.get("error") or out.stderr), 300))
    try:
        return output_type.model_validate(json.loads(g.answer(result))), usage
    except (ValueError, ValidationError) as exc:
        raise SubscriptionCallError("invalid_output", str(exc)[:300]) from exc


def _try_gemini(instructions: str, prompt: str, output_type: type[BaseModel], timeout: int):
    if _gemini["skip_until"] > time.monotonic():
        return None
    for attempt in (1, 2):
        try:
            return gemini_structured(instructions, prompt, output_type, timeout)
        except SubscriptionCallError as exc:
            if exc.status in ("invalid_output", "transient_error") and attempt == 1:
                continue
            if exc.status in _STICKY:
                _gemini["skip_until"] = time.monotonic() + COOLDOWN_SECONDS
            log.warning("gemini subscription call failed (%s: %s)", exc.status, exc.detail[:200])
            return None
        except OSError as exc:
            log.warning("gemini subscription call could not start (%s)", exc)
            return None
    return None


def _try_claude(instructions: str, prompt: str, output_type: type[BaseModel], timeout: int):
    if _claude["skip_until"] > time.monotonic():
        return None
    for attempt in (1, 2):
        try:
            return claude_structured(instructions, prompt, output_type, timeout)
        except SubscriptionCallError as exc:
            if exc.status == "invalid_output" and attempt == 1:
                continue
            if exc.status == "transient_error" and attempt == 1:  # e.g. the OAuth refresh race
                from research_autopilot.implementation.backends.base import TRANSIENT_RETRY_WAIT_SECONDS
                time.sleep(TRANSIENT_RETRY_WAIT_SECONDS)
                continue
            if exc.status in _STICKY:
                _claude["skip_until"] = time.monotonic() + COOLDOWN_SECONDS
            log.warning("claude subscription call failed (%s: %s)", exc.status, exc.detail[:200])
            return None
        except OSError as exc:
            log.warning("claude subscription call could not start (%s)", exc)
            return None
    return None


def try_structured(model: str, instructions: str, prompt: str, output_type: type[BaseModel],
                   timeout: int = DEFAULT_TIMEOUT, reasoning_effort: str | None = None
                   ) -> tuple[BaseModel, dict] | None:
    """Subscription attempt for one structured call; ``None`` means "use the API path".

    Invalid output is retried once; usage limits / auth problems are cached for COOLDOWN_SECONDS so later calls
    skip the subprocess. Never raises for a subscription failure.
    """
    for provider in subscription_providers():  # in the configured order (e.g. claude,codex: Claude first)
        got = (_try_codex(model, instructions, prompt, output_type, timeout, reasoning_effort) if provider == "codex"
               else _try_claude(instructions, prompt, output_type, timeout) if provider == "claude"
               else _try_gemini(instructions, prompt, output_type, timeout))
        if got is not None:
            return got
    return None


def _try_codex(model, instructions, prompt, output_type, timeout, reasoning_effort):
    if (left := cooldown_remaining()) > 0:
        log.info("codex subscription skipped for %ds more (%s)", int(left), _skip_reason)
        return None
    for attempt in (1, 2):
        try:
            return codex_structured(model, instructions, prompt, output_type, timeout, reasoning_effort)
        except SubscriptionCallError as exc:
            if exc.status == "invalid_output" and attempt == 1:
                log.warning("subscription output invalid (%s); retrying once", exc.detail[:120])
                continue
            if exc.status in _STICKY:
                mark_unavailable(exc.status)
            log.warning("subscription call failed (%s: %s); falling back", exc.status, exc.detail[:200])
            return None
        except OSError as exc:  # could not start the CLI at all
            log.warning("subscription call could not start (%s); falling back", exc)
            return None
    return None
