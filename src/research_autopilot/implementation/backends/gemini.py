"""Gemini through the Antigravity CLI (``agy``, headless print mode) on the Google AI Pro subscription.

User decisions (2026-10-16): Gemini is a subscription fallback after Codex and Claude, before the pay-as-you-go
OpenAI API. The Gemini CLI no longer accepts the Google-account login, so the Antigravity CLI is used instead.
Facts below were checked on Eagle with agy 1.4.0 (account ``ra``):

- The prompt is read from stdin; ``--output-format json`` prints one object on stdout:
  ``{"status": "SUCCESS"|"ERROR", "response", "structured_output"?, "error"?, "usage": {input_tokens (incl.
  cache reads), output_tokens (incl. thinking), thinking_tokens, cache_read_tokens, total_tokens},
  "denied_actions"?: [{"action", "display_name"}]}``. An error also exits 1.
- ``--json-schema`` returns ``structured_output``. ``--mode accept-edits`` lets the agent edit files;
  ``--mode plan`` does not.
- Headless mode cannot ask for a permission: a tool without an allow-rule (shell command, URL fetch, ...) is
  denied and the turn ends. Allow-rules exist only in the user settings
  (``~/.gemini/antigravity-cli/settings.json`` ``permissions.allow``, e.g. ``command(uv run pytest)``), which are
  set up once on the host (docs/operations/phase3_implementation.md), never by Research Autopilot.
"""

import json
import shutil
from collections.abc import Callable
from pathlib import Path

from research_autopilot.implementation.backends.base import (
    BackendRequest,
    classify_error_text,
    run_process,
    sanitized_env,
    tail,
)
from research_autopilot.implementation.models import BackendStatus, ImplementationResult, ResultStatus, Usage, utcnow

QUOTA_TEXT = ("resource_exhausted", "exhausted your", "quota exceeded", "out of quota")
NO_TOOLS_NOTE = "Do not use any tools: answer from the input alone."
IMPLEMENTER_NOTE = (
    "Environment note: you run headless. Only pre-approved shell commands run (typically the repository's test "
    "command); any other shell command, URL fetch, web search or MCP tool is denied and ends your session. "
    "Read and edit files directly instead.\n\n")


def cli_home() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli"


def login_state(home: Path | None = None) -> tuple[bool, str]:
    """(logged in with a consumer Google account, detail). The OAuth token file exists after ``agy`` login;
    consumer onboarding = a personal Google account (Google AI Pro), not an enterprise / pay-as-you-go setup."""
    home = home or cli_home()
    token = (home / "antigravity-oauth-token").exists()
    try:
        onboarding = json.loads((home / "cache" / "onboarding.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        onboarding = {}
    consumer = bool(onboarding.get("consumerOnboardingComplete")) and not onboarding.get("enterpriseOnboardingComplete")
    return token and consumer, f"token={'yes' if token else 'no'}, consumer={'yes' if consumer else 'no'}"


def command(executable: str, model: str, mode: str, json_schema: str | None = None) -> list[str]:
    cmd = [executable, "--output-format", "json", "--model", model, "--mode", mode, "--disable-slash-commands"]
    return cmd + (["--json-schema", json_schema] if json_schema else [])  # prompt via stdin


def parse_output(stdout: str) -> dict:
    text = (stdout or "").strip()
    for candidate in (text, text[text.find("{"):text.rfind("}") + 1] if "{" in text else ""):
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return {}


def usage_of(result: dict) -> Usage:
    u = result.get("usage") or {}
    return Usage(input_tokens=int(u.get("input_tokens", 0) or 0), cached_input_tokens=int(u.get("cache_read_tokens", 0) or 0),
                 output_tokens=int(u.get("output_tokens", 0) or 0), reasoning_tokens=int(u.get("thinking_tokens", 0) or 0))


def denied(result: dict) -> list[str]:
    return [str(d.get("action") or d.get("display_name") or "?") for d in result.get("denied_actions") or []
            if isinstance(d, dict)]


def classify(result: dict, stderr: str) -> ResultStatus:
    text = f"{result.get('error', '')} {tail(stderr, 2000)}"
    if any(q in text.lower() for q in QUOTA_TEXT):
        return "quota_exhausted"
    return classify_error_text(text) or "process_error"


def answer(result: dict) -> str:
    """The structured output (as JSON text) when a schema was given, else the response text."""
    if result.get("structured_output") is not None:
        return json.dumps(result["structured_output"], ensure_ascii=False)
    return str(result.get("response") or "")


class GeminiBackend:
    name = "gemini"

    def __init__(self, executable: str = "agy") -> None:
        self.executable = executable

    def status(self) -> BackendStatus:
        if not shutil.which(self.executable):
            return BackendStatus(backend="gemini", installed=False, detail=f"{self.executable} not on PATH")
        ok, detail = login_state()
        # only the consumer (Google AI Pro) login is usable: anything else could not be budgeted
        return BackendStatus(backend="gemini", installed=True, authenticated=ok,
                             billing="subscription" if ok else "unknown", detail=detail)

    def run(self, request: BackendRequest, on_start: Callable[[int], None], json_schema: str | None = None,
            read_only: bool = False) -> ImplementationResult:
        started = utcnow()
        res = dict(backend="gemini", model=request.choice.model, started_at=started, log_path=str(request.log_path))
        ok, detail = login_state()
        if not ok:  # fail closed: never run on another login
            return ImplementationResult(status="auth_error", exit_code=None, finished_at=utcnow(),
                                        error=f"agy is not logged in with a Google AI Pro account ({detail})", **res)
        prompt = request.prompt if read_only else IMPLEMENTER_NOTE + request.prompt
        out = run_process(command(self.executable, request.choice.model, "plan" if read_only else "accept-edits",
                                  json_schema),
                          Path(request.task.worktree_path), prompt, request.timeout_seconds, request.log_path, on_start,
                          env=sanitized_env())
        result = parse_output(out.stdout)
        # Subscription: a known price of 0 USD (an unknown price would stop a research chain's budget check).
        res.update(pid=out.pid, exit_code=out.exit_code, summary=answer(result)[:20000], usage=usage_of(result),
                   cost_usd=0.0, finished_at=utcnow())
        if out.timed_out:
            return ImplementationResult(status="timeout", error=f"timed out after {request.timeout_seconds}s", **res)
        if out.exit_code == 0 and result.get("status") == "SUCCESS":
            if (actions := denied(result)):  # the turn ended at a denied tool: the review judges what was done
                return ImplementationResult(status="failed", error=f"denied without an allow-rule: {actions}", **res)
            return ImplementationResult(status="completed", **res)
        return ImplementationResult(status=classify(result, out.stderr),
                                    error=tail(str(result.get("error") or out.stderr), 500), **res)
