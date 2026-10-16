"""Gemini CLI backend (``gemini -o json``, headless), on the Google AI Pro "Login with Google" subscription.

User decision (2026-10-16): Gemini is a subscription fallback after Codex and Claude, before the pay-as-you-go
OpenAI API. Facts below were read from the @google/gemini-cli 0.64.0 package (not from a live call):

- Headless output: one JSON object on stdout (``response``, ``stats.models.<model>.tokens``). On failure the error
  JSON ``{"error": {"type", "message", "code"}}`` goes to **stderr** and the exit code may be an HTTP status.
- Tools are controlled by a Policy Engine TOML passed with ``--policy`` (user tier, overrides the built-in rules).
  In headless mode anything that would ask for confirmation is denied, never waits. Build files (pyproject.toml,
  lockfiles, Dockerfile, ...) are always protected from headless edits.
- The CLI keeps its own login in ``~/.gemini`` (or ``$GEMINI_CLI_HOME/.gemini``). Only ``oauth-personal``
  (Login with Google) is the subscription; an API key / Vertex login would be pay-as-you-go and is refused.
"""

import json
import os
import shlex
import shutil
import subprocess
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

SUBSCRIPTION_AUTH = "oauth-personal"
# Variables that could switch the CLI to API-key / Vertex billing or another endpoint (keys are already removed by
# sanitized_env). The configured settings.json auth type wins over these, but they are dropped anyway.
BILLING_ENV = ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA", "GOOGLE_GEMINI_BASE_URL", "GOOGLE_CLOUD_PROJECT",
               "GOOGLE_CLOUD_LOCATION", "GEMINI_CLI_USE_COMPUTE_ADC", "GEMINI_MODEL")
EXIT_AUTH, EXIT_UNTRUSTED = 41, 55
READ_TOOLS = ["read_file", "read_many_files", "list_directory", "glob", "grep_search"]
EDIT_TOOLS = ["write_file", "replace"]
# Same shell allowance as the Claude backend (ALLOWED_TOOLS) plus the repository's check commands.
SHELL_PREFIXES = ["uv run pytest", "uv run python", "python -m pytest", "python", "git status", "git diff", "ls"]
# Quota error classes of the CLI (TerminalQuotaError: daily / capacity limit; RetryableQuotaError: per-minute).
ERROR_TYPES: dict[str, ResultStatus] = {
    "TerminalQuotaError": "quota_exhausted", "RetryableQuotaError": "rate_limited",
    "FatalAuthenticationError": "auth_error", "ValidationRequiredError": "auth_error",
}
NO_TOOLS = "no-tools"


def gemini_home() -> Path:
    return Path(os.environ.get("GEMINI_CLI_HOME", "").strip() or Path.home()) / ".gemini"


def auth_type(home: Path | None = None) -> str:
    """``security.auth.selectedType`` of the CLI's user settings ('' when not logged in / unreadable)."""
    try:
        settings = json.loads(((home or gemini_home()) / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    auth = ((settings.get("security") or {}).get("auth") or {}) if isinstance(settings, dict) else {}
    return str(auth.get("selectedType") or settings.get("selectedAuthType") or "")


def process_env() -> dict[str, str]:
    return {k: v for k, v in sanitized_env().items() if k not in BILLING_ENV}


def policy_toml(mode: str, shell_prefixes: list[str] | None = None) -> str:
    """Policy for one call. ``mode``: ``edit`` (implementer), ``read`` (reviewer) or ``no-tools`` (structured
    answer). Everything not allowed here is denied (web fetch / search, MCP, other shell commands, ...)."""
    def rule(tools: list[str] | str, extra: str = "") -> str:
        return f"[[rule]]\ntoolName = {json.dumps(tools)}\n{extra}decision = \"allow\"\npriority = 500\n\n"
    out = ""
    if mode in ("edit", "read"):
        out += rule(READ_TOOLS)
    if mode == "edit":
        out += rule(EDIT_TOOLS)
        if shell_prefixes:
            out += rule("run_shell_command", f"commandPrefix = {json.dumps(shell_prefixes, ensure_ascii=False)}\n")
    return out + ('[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 100\n'
                  'denyMessage = "Not permitted by Research Autopilot."\n')


def shell_prefixes(request: BackendRequest) -> list[str]:
    from research_autopilot.implementation.reviewer import with_worktree_git
    out = list(SHELL_PREFIXES)
    for cmd in request.check_commands:
        for form in (cmd, with_worktree_git(cmd, Path(request.task.worktree_path))):
            if (p := shlex.join(form)) not in out:
                out.append(p)
    return out


def command(executable: str, model: str, policy: Path, approval_mode: str) -> list[str]:
    # Prompt via stdin; -p only forces headless mode and is appended after it. An explicit model keeps the CLI
    # from silently routing to another model. No extensions and no MCP server (an empty allow-list means "all").
    return [executable, "-p", "Follow the instructions above.", "-o", "json", "-m", model,
            "--approval-mode", approval_mode, "--policy", str(policy), "--skip-trust",
            "-e", "none", "--allowed-mcp-server-names", "__none__"]


def parse_output(stdout: str, stderr: str) -> tuple[dict, dict]:
    """(result object from stdout, error object from stderr or stdout)."""
    result = _json_object(stdout)
    err = result.get("error") if isinstance(result.get("error"), dict) else {}
    if not err:
        err = (_json_object(stderr).get("error") or {}) if stderr else {}
    return result, err if isinstance(err, dict) else {}


def usage_of(result: dict) -> Usage:
    """Token counts summed over every model the CLI used (``prompt`` includes the cached part)."""
    u = Usage()
    for m in (((result.get("stats") or {}).get("models")) or {}).values():
        t = (m or {}).get("tokens") or {}
        u = u.add(Usage(input_tokens=int(t.get("prompt", 0) or 0), cached_input_tokens=int(t.get("cached", 0) or 0),
                        output_tokens=int(t.get("candidates", 0) or 0) + int(t.get("thoughts", 0) or 0),
                        reasoning_tokens=int(t.get("thoughts", 0) or 0)))
    return u


def classify(exit_code: int | None, err: dict, stderr: str) -> ResultStatus:
    if (status := ERROR_TYPES.get(str(err.get("type", "")))) is not None:
        return status
    if exit_code == EXIT_AUTH:
        return "auth_error"
    if exit_code == EXIT_UNTRUSTED:
        return "process_error"
    text = f"{err.get('type', '')} {err.get('message', '')} {err.get('code', '')} {tail(stderr, 2000)}"
    if "resource_exhausted" in text.lower() or "exhausted your" in text.lower():
        return "quota_exhausted"
    return classify_error_text(text) or "process_error"


def _json_object(text: str) -> dict:
    """The JSON object in a CLI stream (whole text first, then the outermost ``{...}``; log lines may surround it)."""
    text = (text or "").strip()
    for candidate in (text, text[text.find("{"):text.rfind("}") + 1] if "{" in text else ""):
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return {}


class GeminiBackend:
    name = "gemini"

    def __init__(self, executable: str = "gemini") -> None:
        self.executable = executable

    def status(self) -> BackendStatus:
        if not shutil.which(self.executable):
            return BackendStatus(backend="gemini", installed=False, detail="gemini not on PATH")
        home = gemini_home()
        method = auth_type(home)
        creds = (home / "oauth_creds.json").exists()
        subscription = method == SUBSCRIPTION_AUTH
        return BackendStatus(backend="gemini", installed=True, version=_out([self.executable, "--version"]).strip(),
                             # only the subscription login is usable: an API-billed login could not be budgeted
                             authenticated=subscription and creds,
                             billing="subscription" if subscription else "api" if method else "unknown",
                             detail=f"selectedType={method or 'unset'}, oauth_creds={'yes' if creds else 'no'}")

    def run(self, request: BackendRequest, on_start: Callable[[int], None], mode: str = "edit") -> ImplementationResult:
        started = utcnow()
        res = dict(backend="gemini", model=request.choice.model, pid=None, started_at=started,
                   log_path=str(request.log_path))
        if auth_type() != SUBSCRIPTION_AUTH:  # fail closed: never run on a pay-as-you-go login
            return ImplementationResult(status="auth_error", exit_code=None, finished_at=utcnow(),
                                        error=f"gemini login is not '{SUBSCRIPTION_AUTH}' (Login with Google)", **res)
        policy = request.log_path.with_suffix(".policy.toml")
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(policy_toml(mode, shell_prefixes(request) if mode == "edit" else None), encoding="utf-8")
        cmd = command(self.executable, request.choice.model, policy, "auto_edit" if mode == "edit" else "default")
        out = run_process(cmd, Path(request.task.worktree_path), request.prompt, request.timeout_seconds,
                          request.log_path, on_start, env=process_env())
        result, err = parse_output(out.stdout, out.stderr)
        # Subscription: a known price of 0 USD (an unknown price would stop a research chain's budget check).
        res.update(pid=out.pid, exit_code=out.exit_code, summary=str(result.get("response") or "")[:20000],
                   usage=usage_of(result), cost_usd=0.0, finished_at=utcnow())
        if out.timed_out:
            return ImplementationResult(status="timeout", error=f"timed out after {request.timeout_seconds}s", **res)
        if out.exit_code == 0 and result and not err:
            return ImplementationResult(status="completed", **res)
        status = classify(out.exit_code, err, out.stderr)
        detail = f"{err.get('type', '')}: {err.get('message', '')}".strip(": ") or tail(out.stderr, 500)
        return ImplementationResult(status=status, error=tail(detail, 500), **res)


def _out(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        return p.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return ""
