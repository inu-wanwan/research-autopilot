"""Codex CLI backend (``codex exec``, non-interactive, JSONL events)."""

import json
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
from research_autopilot.implementation.models import BackendStatus, ImplementationResult, Usage, utcnow


class CodexBackend:
    name = "codex"

    def __init__(self, executable: str = "codex", sandbox: str = "workspace-write") -> None:
        self.executable = executable
        self.sandbox = sandbox

    def status(self) -> BackendStatus:
        if not shutil.which(self.executable):
            return BackendStatus(backend="codex", installed=False, detail="codex not on PATH")
        version = _out([self.executable, "--version"])
        login = _out([self.executable, "login", "status"])
        billing = "subscription" if "chatgpt" in login.lower() else "api" if "api key" in login.lower() else "unknown"
        return BackendStatus(backend="codex", installed=True, version=version.strip(),
                             authenticated=login.lower().startswith("logged in"), billing=billing,
                             detail=login.strip()[:120])

    def command(self, request: BackendRequest, sandbox: str | None = None) -> list[str]:
        cmd = [self.executable, "exec", "--json", "--ephemeral", "--ignore-user-config",
               "-C", request.task.worktree_path, "-s", sandbox or self.sandbox, "-m", request.choice.model]
        if request.choice.reasoning_effort:
            cmd += ["-c", f'model_reasoning_effort="{request.choice.reasoning_effort}"']
        return cmd + ["-"]  # prompt from stdin

    def run(self, request: BackendRequest, on_start: Callable[[int], None],
            sandbox: str | None = None, extra_args: list[str] | None = None) -> ImplementationResult:
        started = utcnow()
        cmd = self.command(request, sandbox)
        if extra_args:
            cmd = cmd[:-1] + extra_args + ["-"]
        out = run_process(cmd, Path(request.task.worktree_path), request.prompt, request.timeout_seconds,
                          request.log_path, on_start, env=self.process_env(),
                          on_stdout=(lambda line: _forward_progress(line, request.on_progress)))
        usage, message, errors = parse_events(out.stdout)
        res = dict(backend=self.name, model=request.choice.model, exit_code=out.exit_code, summary=message[:4000],
                   usage=usage, cost_usd=self.cost(request.choice.model, usage), pid=out.pid, started_at=started,
                   finished_at=utcnow(), log_path=str(request.log_path))
        if out.timed_out:
            return ImplementationResult(status="timeout", error=f"timed out after {request.timeout_seconds}s", **res)
        if out.exit_code == 0 and not errors:
            return ImplementationResult(status="completed", **res)
        text = " ".join(errors) + " " + tail(out.stderr, 2000)
        status = classify_error_text(text) or ("failed" if out.exit_code == 0 else "process_error")
        return ImplementationResult(status=status, error=tail(" | ".join(errors) or out.stderr, 500), **res)


    def process_env(self) -> dict[str, str] | None:
        return None  # sanitized environment; the CLI uses its own (subscription) login

    def cost(self, model: str, usage: Usage) -> float | None:
        return None  # subscription: no per-call price


class CodexApiBackend(CodexBackend):
    """Codex CLI billed to the OpenAI API key (pay-as-you-go fallback after the subscriptions).

    The key is handed to ``codex exec`` only, as ``CODEX_API_KEY``, with a dedicated ``CODEX_HOME`` so the
    ChatGPT login is never used or modified. Codex's default shell-environment policy drops ``*KEY*`` variables
    from the commands the agent runs. A run is refused unless the model has a price in ``RA_MODEL_PRICING``, so
    every call is accounted against the cost limits.
    """

    name = "codex_api"

    def __init__(self, api_key: str | None, pricing: dict, home: Path, executable: str = "codex",
                 sandbox: str = "workspace-write") -> None:
        super().__init__(executable, sandbox)
        self._api_key, self.pricing, self.home = api_key, pricing, Path(home)

    def status(self) -> BackendStatus:
        if not shutil.which(self.executable):
            return BackendStatus(backend="codex_api", installed=False, detail="codex not on PATH")
        ok = bool(self._api_key)
        return BackendStatus(backend="codex_api", installed=True, version=_out([self.executable, "--version"]).strip(),
                             authenticated=ok, billing="api",
                             detail="OPENAI_API_KEY (pay-as-you-go)" if ok else "OPENAI_API_KEY is not set")

    def process_env(self) -> dict[str, str]:
        self.home.mkdir(parents=True, exist_ok=True)
        return {**sanitized_env(), "CODEX_HOME": str(self.home), "CODEX_API_KEY": self._api_key or ""}

    def cost(self, model: str, usage: Usage) -> float | None:
        from research_autopilot.planning.cost import estimate_cost
        return estimate_cost(usage, self.pricing.get(model))

    def run(self, request: BackendRequest, on_start: Callable[[int], None],
            sandbox: str | None = None, extra_args: list[str] | None = None) -> ImplementationResult:
        if self.pricing.get(request.choice.model) is None:  # fail closed: an unpriced call could not be budgeted
            return ImplementationResult(backend="codex_api", model=request.choice.model, status="process_error",
                                        exit_code=None, error=f"no RA_MODEL_PRICING entry for {request.choice.model}; "
                                                              "API-billed run refused")
        return super().run(request, on_start, sandbox, extra_args)


def parse_events(stdout: str) -> tuple[Usage, str, list[str]]:
    usage, message, errors = Usage(), "", []
    for line in stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = e.get("type")
        if t == "item.completed" and (e.get("item") or {}).get("type") == "agent_message":
            message = e["item"].get("text", "") or message
        elif t == "turn.completed":
            u = e.get("usage") or {}
            usage = usage.add(Usage(input_tokens=u.get("input_tokens", 0), cached_input_tokens=u.get("cached_input_tokens", 0),
                                    output_tokens=u.get("output_tokens", 0),
                                    reasoning_tokens=u.get("reasoning_output_tokens", 0)))
        elif t in ("turn.failed", "error"):
            err = e.get("error") or {}
            errors.append(str(err.get("message") if isinstance(err, dict) else err or e.get("message", ""))[:500])
    return usage, message, errors


def progress_summary(line: str) -> str | None:
    """Return a secret-safe operational summary for one Codex JSONL event.

    Raw commands, command output and agent messages are deliberately omitted.
    """
    try:
        event = json.loads(line)
    except ValueError:
        return None
    event_type = event.get("type")
    item = event.get("item") or {}
    item_type = item.get("type")
    if item_type == "command_execution" and event_type in {"item.started", "item.completed"}:
        command = str(item.get("command", "")).lower()
        if "pytest" in command or "unittest" in command:
            activity = "tests"
        elif any(token in command for token in ("git status", "git diff", "git log", "git show")):
            activity = "Git inspection"
        elif any(token in command for token in ("rg ", "find ", "sed ", "awk ", "jq ")):
            activity = "repository inspection"
        elif any(token in command for token in ("uv sync", "uv add", "pip install")):
            activity = "dependency setup"
        else:
            activity = "workspace command"
        if event_type == "item.started":
            return f"{activity} started"
        status = str(item.get("status") or "completed")
        exit_code = item.get("exit_code")
        suffix = f", exit {exit_code}" if exit_code is not None else ""
        return f"{activity} {status}{suffix}"
    if item_type == "file_change" and event_type == "item.completed":
        changes = item.get("changes") or []
        names = [Path(str(change.get("path", ""))).name for change in changes[:5] if change.get("path")]
        files = f": {', '.join(names)}" if names else ""
        return f"file changes completed ({len(changes)} files){files}"
    return None


def _forward_progress(line: str, callback: Callable[[str], None] | None) -> None:
    if callback is not None and (summary := progress_summary(line)) is not None:
        callback(summary)


def _out(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        return (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        return ""
