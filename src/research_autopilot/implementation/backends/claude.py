"""Claude Code CLI backend (``claude -p --output-format json``)."""

import json
import shlex
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

# Edits are limited to the worktree (cwd) by acceptEdits; shell use is allow-listed.
ALLOWED_TOOLS = ",".join([
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(uv run pytest:*)", "Bash(uv run python:*)", "Bash(python -m pytest:*)", "Bash(python:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(ls:*)",
])
DISALLOWED_TOOLS = ",".join([
    "Bash(git push:*)", "Bash(git commit:*)", "Bash(git checkout:*)", "Bash(git reset:*)", "Bash(git merge:*)",
    "Bash(rm -rf:*)", "Bash(sudo:*)", "Bash(curl:*)", "Bash(wget:*)", "WebFetch", "WebSearch",
])


def allowed_tools(request: BackendRequest) -> str:
    """Defaults plus the repository's own check commands (e.g. ``docker compose ... run research pytest``), in
    the form the reviewer runs them, so the implementer can run the same tests (no other docker use)."""
    from research_autopilot.implementation.reviewer import with_worktree_git
    extra = []
    for cmd in request.check_commands:
        for form in (cmd, with_worktree_git(cmd, Path(request.task.worktree_path))):
            rule = f"Bash({shlex.join(form)}:*)"
            if rule not in extra:
                extra.append(rule)
    return ",".join([ALLOWED_TOOLS, *extra]) if extra else ALLOWED_TOOLS


class ClaudeBackend:
    name = "claude"

    def __init__(self, executable: str = "claude") -> None:
        self.executable = executable
        self._billing: str | None = None

    def billing(self) -> str:
        """``subscription`` / ``api`` / ``unknown`` from ``claude auth status`` (asked once per process)."""
        if self._billing is None:
            self._billing = self.status().billing
        return self._billing

    def status(self) -> BackendStatus:
        if not shutil.which(self.executable):
            return BackendStatus(backend="claude", installed=False, detail="claude not on PATH")
        version = _out([self.executable, "--version"]).strip()
        try:
            auth = json.loads(_out([self.executable, "auth", "status"]))
        except ValueError:
            auth = {}
        method = str(auth.get("authMethod", ""))
        billing = "subscription" if method == "claude.ai" else "api" if method else "unknown"
        return BackendStatus(backend="claude", installed=True, version=version, authenticated=bool(auth.get("loggedIn")),
                             billing=billing, detail=f"authMethod={method or 'unknown'}")

    def command(self, request: BackendRequest, json_schema: str | None = None, read_only: bool = False) -> list[str]:
        cmd = [self.executable, "-p", "--output-format", "json", "--model", request.choice.model,
               "--no-session-persistence", "--strict-mcp-config",
               "--permission-mode", "default" if read_only else "acceptEdits",
               "--allowedTools", "Read,Glob,Grep" if read_only else allowed_tools(request),
               "--disallowedTools", DISALLOWED_TOOLS]
        if request.max_budget_usd is not None:
            cmd += ["--max-budget-usd", f"{request.max_budget_usd:.2f}"]
        if json_schema:
            cmd += ["--json-schema", json_schema]
        return cmd  # prompt via stdin

    def run(self, request: BackendRequest, on_start: Callable[[int], None],
            json_schema: str | None = None, read_only: bool = False) -> ImplementationResult:
        started = utcnow()
        out = run_process(self.command(request, json_schema, read_only), Path(request.task.worktree_path),
                          request.prompt, request.timeout_seconds, request.log_path, on_start)
        try:
            r = json.loads(out.stdout)
        except ValueError:
            r = {}
        u = r.get("usage") or {}
        usage = Usage(input_tokens=(u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                                    + u.get("cache_creation_input_tokens", 0)),
                      cached_input_tokens=u.get("cache_read_input_tokens", 0), output_tokens=u.get("output_tokens", 0))
        # On a claude.ai subscription total_cost_usd is a notional price, not a charge: like the Codex
        # subscription it is not counted against the API budget.
        cost = None if self.billing() == "subscription" else r.get("total_cost_usd")
        summary = r.get("structured_output") if json_schema and r.get("structured_output") is not None else r.get("result", "")
        res = dict(backend="claude", model=request.choice.model, exit_code=out.exit_code,
                   summary=(json.dumps(summary, ensure_ascii=False) if not isinstance(summary, str) else summary)[:20000],
                   usage=usage, cost_usd=float(cost) if isinstance(cost, (int, float)) else None, pid=out.pid,
                   started_at=started, finished_at=utcnow(), log_path=str(request.log_path))
        if out.timed_out:
            return ImplementationResult(status="timeout", error=f"timed out after {request.timeout_seconds}s", **res)
        subtype = str(r.get("subtype", ""))
        if out.exit_code == 0 and r and not r.get("is_error") and subtype == "success":
            return ImplementationResult(status="completed", **res)
        if "budget" in subtype:
            return ImplementationResult(status="budget_exceeded", error=subtype, **res)
        text = f"{r.get('result', '')} {subtype} {r.get('api_error_status', '')} {tail(out.stderr, 2000)}"
        status = classify_error_text(text) or ("failed" if r else "process_error")
        return ImplementationResult(status=status, error=tail(text.strip(), 500), **res)


def _out(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        return p.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return ""
