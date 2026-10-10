"""CodingBackend interface and shared non-interactive process handling."""

import os
import re
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from research_autopilot.implementation.models import (
    BackendName,
    BackendStatus,
    ImplementationResult,
    ImplementationTask,
    ModelChoice,
    ResultStatus,
)

# Output patterns (case-insensitive) used to classify failures. They are
# matched against CLI error text only, never used to grant anything.
QUOTA_PATTERNS = [r"usage limit", r"quota", r"insufficient_quota", r"out of credits", r"credit balance",
                  r"you've hit your", r"limit reached", r"plan limit"]
RATE_PATTERNS = [r"rate.?limit", r"\b429\b", r"too many requests", r"overloaded", r"\b529\b"]
TRANSIENT_PATTERNS = [r"\b50[0234]\b", r"timed? ?out", r"connection (reset|refused|error)", r"network error",
                      r"temporarily unavailable", r"stream disconnected"]
AUTH_PATTERNS = [r"not logged in", r"unauthori[sz]ed", r"\b401\b", r"authentication", r"login required",
                 r"invalid api key"]


def classify_error_text(text: str) -> ResultStatus | None:
    t = text.lower()
    for status, pats in (("auth_error", AUTH_PATTERNS), ("quota_exhausted", QUOTA_PATTERNS),
                         ("rate_limited", RATE_PATTERNS), ("transient_error", TRANSIENT_PATTERNS)):
        if any(re.search(p, t) for p in pats):
            return status  # type: ignore[return-value]
    return None


@dataclass
class BackendRequest:
    task: ImplementationTask
    choice: ModelChoice
    prompt: str
    timeout_seconds: int
    log_path: Path
    max_budget_usd: float | None = None


class CodingBackend(Protocol):
    name: BackendName

    def status(self) -> BackendStatus: ...

    def run(self, request: BackendRequest, on_start: Callable[[int], None]) -> ImplementationResult:
        """Run to completion (or timeout). ``on_start(pid)`` is called as soon as
        the process exists so the orchestrator can persist it before waiting."""
        ...


@dataclass
class ProcessOutcome:
    pid: int
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration: float


SECRET_ENV = re.compile(r"(API_KEY|TOKEN|SECRET|PASSWORD|AUTH_STRING|DATABASE_URL|PRIVATE_KEY|CREDENTIAL)", re.I)
SECRET_ENV_NAMES = {"NOTION_API_TOKEN", "LINEAR_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "PREFECT_API_AUTH_STRING",
                    "PREFECT_AUTH_STRING", "POSTGRES_PASSWORD", "RA_DATABASE_URL", "RA_NOTIFY_TOKEN",
                    "RA_DISCORD_BOT_TOKEN", "RA_MODEL_PRICING"}


def sanitized_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for coding agents, reviewers and check commands: Research Autopilot's own credentials
    (Notion / Linear / OpenAI keys, Prefect auth, DB URL, ...) are removed so an LLM-driven process cannot use
    them (e.g. to change a Linear status). CLI logins (Codex / Claude) live in their own config dirs."""
    env = dict(os.environ if base is None else base)
    return {k: v for k, v in env.items() if k not in SECRET_ENV_NAMES and not SECRET_ENV.search(k)}


def run_process(cmd: list[str], cwd: Path, stdin_text: str, timeout: int, log_path: Path,
                on_start: Callable[[int], None], env: dict[str, str] | None = None) -> ProcessOutcome:
    """Run a CLI in its own process group; on timeout the whole group is killed
    and reaped before returning, so no orphan keeps editing the worktree."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True, env=env if env is not None else sanitized_env())
    on_start(proc.pid)
    timed_out = False
    try:
        out, err = proc.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_group(proc.pid)
        out, err = proc.communicate()
    with log_path.open("w", encoding="utf-8") as f:
        f.write(f"$ {' '.join(cmd[:1])} ... (cwd={cwd})\n--- stdout ---\n{out}\n--- stderr ---\n{err}\n")
    return ProcessOutcome(pid=proc.pid, exit_code=proc.returncode, stdout=out or "", stderr=err or "",
                          timed_out=timed_out, duration=time.monotonic() - t0)


def terminate_group(pid: int, grace: float = 5.0) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.waitpid(pid, os.WNOHANG)  # reap if it is our zombie child
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def tail(text: str, n: int = 1500) -> str:
    return text[-n:] if text else ""
