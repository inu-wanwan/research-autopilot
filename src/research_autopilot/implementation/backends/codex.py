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
                          request.log_path, on_start)
        usage, message, errors = parse_events(out.stdout)
        res = dict(backend="codex", model=request.choice.model, exit_code=out.exit_code, summary=message[:4000],
                   usage=usage, cost_usd=None, pid=out.pid, started_at=started, finished_at=utcnow(),
                   log_path=str(request.log_path))
        if out.timed_out:
            return ImplementationResult(status="timeout", error=f"timed out after {request.timeout_seconds}s", **res)
        if out.exit_code == 0 and not errors:
            return ImplementationResult(status="completed", **res)
        text = " ".join(errors) + " " + tail(out.stderr, 2000)
        status = classify_error_text(text) or ("failed" if out.exit_code == 0 else "process_error")
        return ImplementationResult(status=status, error=tail(" | ".join(errors) or out.stderr, 500), **res)


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


def _out(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        return (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        return ""
