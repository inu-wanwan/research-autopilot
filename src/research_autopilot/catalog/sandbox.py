"""Isolated execution of generated Python adapters (Phase 7B).

A generated adapter never runs inside Research Autopilot.  It runs in a
separate interpreter (``-I -S``: no site-packages, no user paths) under an OS
sandbox that denies network, process creation, every write except a fresh
work directory, and every read except the repository, the work directory,
the interpreter installation and OS libraries.
Without an available sandbox the adapter is not run at all (fail closed).
The parent never trusts the adapter's file hashes: members are re-hashed here.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from research_autopilot.catalog.declarative import DiscoveredGroup
from research_autopilot.catalog.models import CheckResult

MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_GROUPS = 200_000

# Trusted harness: imports the adapter from a file path, calls discover(root), writes JSON.
_RUNNER = r'''
import importlib.util, json, sys
adapter_path, root, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
spec = importlib.util.spec_from_file_location("generated_adapter", adapter_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
groups = mod.discover(root)
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump({"groups": groups}, fh)
'''


class SandboxUnavailable(Exception):
    pass


def _python() -> tuple[str, str]:
    exe = os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)
    return exe, os.path.realpath(sys.base_prefix)


def _sbpl(python: str, base_prefix: str, root: str, workdir: str) -> str:
    """Deny-by-default reads: only the repository, the work dir, the interpreter and OS libraries."""
    q = json.dumps  # SBPL string literals are double-quoted with JSON-compatible escapes
    system = ["/usr/lib", "/usr/share", "/System", "/Library/Apple", "/private/var/db/timezone"]
    return "\n".join([
        "(version 1)", "(allow default)", "(deny network*)", "(deny process-fork)",
        "(deny process-exec)", f"(allow process-exec (literal {q(python)}))",
        "(deny file-write*)", f"(allow file-write* (subpath {q(workdir)}) (literal \"/dev/null\"))",
        "(deny file-read-data)",
        "(allow file-read-data (literal \"/\") (literal \"/dev/null\") (literal \"/dev/urandom\")"
        " (literal \"/dev/random\") " + " ".join(f"(subpath {q(p)})" for p in system) + ")",
        f"(allow file-read-data (subpath {q(root)}) (subpath {q(workdir)}) (subpath {q(base_prefix)}))",
    ])


def sandbox_command(python: str, base_prefix: str, root: str, workdir: str, argv: list[str]) -> list[str]:
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return ["sandbox-exec", "-p", _sbpl(python, base_prefix, root, workdir), python, *argv]
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        cmd = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
               "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
        for p in ("/usr", "/lib", "/lib64", "/bin", "/etc", base_prefix, os.path.dirname(python)):
            cmd += ["--ro-bind-try", p, p]
        cmd += ["--ro-bind", root, root, "--bind", workdir, workdir, python, *argv]
        return cmd
    raise SandboxUnavailable("no OS sandbox (macOS sandbox-exec or Linux bwrap) is available on this host")


def available() -> bool:
    return (sys.platform == "darwin" and shutil.which("sandbox-exec") is not None) or \
        (sys.platform.startswith("linux") and shutil.which("bwrap") is not None)


def _limits(cpu_seconds: int):
    def apply() -> None:
        import resource
        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    return apply


def run_python_adapter(source: str, root: Path, *, timeout: int = 300) -> tuple[list[dict] | None, str | None]:
    """Run ``discover(root)`` of untrusted source in the sandbox. Returns (raw groups, error)."""
    root_s = os.path.realpath(str(root.expanduser()))
    python, base = _python()
    workdir = os.path.realpath(tempfile.mkdtemp(prefix="ra-adapter-"))
    try:
        Path(workdir, "adapter.py").write_text(source, encoding="utf-8")
        Path(workdir, "runner.py").write_text(_RUNNER, encoding="utf-8")
        out = Path(workdir, "out.json")
        cmd = sandbox_command(python, base, root_s, workdir,
                              ["-I", "-S", f"{workdir}/runner.py", f"{workdir}/adapter.py", root_s, str(out)])
        env = {"PATH": "/usr/bin:/bin", "HOME": workdir, "LANG": "C.UTF-8", "PYTHONHASHSEED": "0",
               "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.Popen(cmd, cwd=workdir, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, preexec_fn=_limits(timeout))
        try:
            _, err = proc.communicate(timeout=timeout + 5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            return None, f"adapter exceeded {timeout}s"
        if proc.returncode != 0:
            return None, f"adapter exited {proc.returncode}: {err.strip()[-500:]}"
        if not out.is_file() or out.stat().st_size > MAX_OUTPUT_BYTES:
            return None, "adapter produced no output or output too large"
        data = json.loads(out.read_text(encoding="utf-8"))
        groups = data.get("groups") if isinstance(data, dict) else None
        if not isinstance(groups, list) or len(groups) > MAX_GROUPS:
            return None, "adapter output must be {'groups': [...]} with a bounded number of groups"
        return groups, None
    except SandboxUnavailable as exc:
        return None, str(exc)
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"[:500]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def to_groups(raw: list[dict], root: Path) -> tuple[list[DiscoveredGroup], list[str]]:
    """Validate untrusted output; members must be real files inside root, re-hashed here."""
    root = root.expanduser().resolve()
    groups: list[DiscoveredGroup] = []
    errors: list[str] = []
    for i, g in enumerate(raw):
        try:
            members = g["members"]
            if not isinstance(members, list) or not members:
                raise ValueError("members must be a non-empty list of repository-relative paths")
            digests: dict[str, str] = {}
            for rel in members:
                path = (root / str(rel)).resolve()
                if Path(str(rel)).is_absolute() or not path.is_relative_to(root) or not path.is_file() \
                        or (root / str(rel)).is_symlink():
                    raise ValueError(f"member {rel!r} is not a regular file inside the repository")
                digests[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            checks = [CheckResult(name=str(c["name"]), passed=bool(c["passed"]), mandatory=bool(c.get("mandatory", True)),
                                  detail=str(c.get("detail", ""))[:500], validator="python-adapter")
                      for c in g.get("checks", [])]
            checks.insert(0, CheckResult(name="required_fields", passed=True, validator="sandbox@1"))
            groups.append(DiscoveredGroup(rule=str(g["rule"]), kind=str(g["kind"]), identity=dict(g["identity"]),
                                          metadata=dict(g.get("metadata", {})), provenance=dict(g.get("provenance", {})),
                                          relative_dir=str(g.get("relative_dir", "")), members=digests, checks=checks))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"group {i}: {exc}"[:300])
    return groups, errors


class SandboxedPythonRuntime:
    """AdapterRuntime for generated python adapters: discovery only ever happens in the sandbox."""

    def __init__(self, source: str, timeout: int = 300) -> None:
        self.source, self.timeout = source, timeout

    def discover(self, root: Path) -> tuple[list[DiscoveredGroup], list[str]]:
        raw, err = run_python_adapter(self.source, root, timeout=self.timeout)
        if err is not None:
            return [], [err]
        return to_groups(raw, root)


def describe_contract() -> dict[str, Any]:
    return {
        "entry": "def discover(root: str) -> list[dict]",
        "group": {"rule": "str", "kind": "str", "identity": "dict", "metadata": "dict", "provenance": "dict",
                  "relative_dir": "repository-relative str", "members": "list of repository-relative file paths",
                  "checks": "list of {name, passed, mandatory, detail}"},
        "allowed_imports": sorted(ALLOWED_IMPORTS),
        "environment": "isolated interpreter, read-only repository, no network, no subprocess, no writes",
    }


ALLOWED_IMPORTS = {"json", "csv", "re", "math", "os", "os.path", "pathlib", "hashlib", "itertools", "collections",
                   "functools", "typing", "datetime", "fnmatch", "statistics", "dataclasses", "string", "decimal",
                   "fractions", "glob"}
