"""CodingBackend interface and shared non-interactive process handling."""

import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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
# Claude Code's OAuth refresh race (several claude processes refreshing at once; Eagle 2026-10-15): transient, retried
# after a short wait instead of moving to the next backend.  Checked before the auth patterns.
REFRESH_PATTERNS = [r"refresh(ing)? (the )?oauth token", r"another claude code process"]
TRANSIENT_RETRY_WAIT_SECONDS = 20.0
AUTH_PATTERNS = [r"not logged in", r"unauthori[sz]ed", r"\b401\b", r"authentication", r"login required",
                 r"invalid api key"]


def classify_error_text(text: str) -> ResultStatus | None:
    t = text.lower()
    if any(re.search(p, t) for p in REFRESH_PATTERNS):
        return "transient_error"
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
    # the repository's configured check commands (tests / lint / typecheck): the implementer may run exactly these
    check_commands: list[list[str]] = field(default_factory=list)
    # Safe, already-redacted progress summaries for operational logs. Raw CLI
    # output remains in log_path and must never be forwarded by this callback.
    on_progress: Callable[[str], None] | None = None


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
                on_start: Callable[[int], None], env: dict[str, str] | None = None,
                on_stdout: Callable[[str], None] | None = None) -> ProcessOutcome:
    """Run a CLI in its own process group; on timeout the whole group is killed
    and reaped before returning, so no orphan keeps editing the worktree.

    Stdout is persisted line-by-line while the process runs. ``on_stdout`` may
    inspect each line, but callback failures are isolated from the coding job.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    with log_path.open("w", encoding="utf-8", buffering=1) as log:
        log.write(f"$ {' '.join(cmd[:1])} ... (cwd={cwd})\n--- stdout ---\n")
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, start_new_session=True,
                                env=env if env is not None else sanitized_env())
        on_start(proc.pid)

        def drain_stdout() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                stdout_parts.append(line)
                log.write(line)
                if on_stdout is not None:
                    try:
                        on_stdout(line)
                    except Exception:  # noqa: BLE001 - progress must not fail the coding job
                        pass

        def drain_stderr() -> None:
            assert proc.stderr is not None
            stderr_parts.extend(proc.stderr)

        stdout_thread = threading.Thread(target=drain_stdout, name=f"backend-stdout-{proc.pid}", daemon=True)
        stderr_thread = threading.Thread(target=drain_stderr, name=f"backend-stderr-{proc.pid}", daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        if proc.stdin is not None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            except BrokenPipeError:
                pass
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_group(proc.pid)
            proc.wait()
        stdout_thread.join()
        stderr_thread.join()
        err = "".join(stderr_parts)
        log.write(f"\n--- stderr ---\n{err}\n")
    out = "".join(stdout_parts)
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
