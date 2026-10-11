"""Measured environment facts for the Planner (docs/phase_specs/phase2.md, Definition of Done).

A plan whose Definition of Done needs a library the test environment lacks, or an environment Phase 4 does not
build, can only fail in review. The Planner therefore gets: how Phase 4 builds the run environment, the
repository's dependency files at its base branch, and the packages installed in the test environment (when the
test command runs a local Python interpreter; a containerized test command is reported as such).
"""

import subprocess
from collections.abc import Callable
from pathlib import Path

from research_autopilot.implementation.models import RepoConfig
from research_autopilot.models import TaskSpec

PHASE4_ENVIRONMENT_CONTRACT = (
    "Phase 4 checks out the implementation commit, creates `uv venv --python <experiment.json "
    "environment.python_version>`, installs environment.dependency_files from the package index (uv.lock -> "
    "`uv sync --frozen`; else requirements.txt; else the pyproject.toml dependencies) and records the installed "
    "versions with `uv pip freeze`. install_argv is not executed; no lock file, offline install or container "
    "image is needed. The repository's tests (Phase 3) run with the test command below, in the test environment."
)
DEPENDENCY_FILES = ("pyproject.toml", "requirements.txt", "uv.lock")
_cache: dict[str, list[str] | str] = {}


def repo_id(repository: str | None) -> str | None:
    return next((ln.strip().strip("-*` ").split()[0] for ln in (repository or "").splitlines()
                 if ln.strip().strip("-*` ")), None)


def environment_facts(registry: dict[str, RepoConfig]) -> Callable[[TaskSpec], dict | None]:
    def facts(spec: TaskSpec) -> dict | None:
        repo = registry.get(repo_id(spec.repository) or "")
        out: dict = {"phase4_environment": PHASE4_ENVIRONMENT_CONTRACT}
        if repo is None:
            return out
        path = Path(repo.path).expanduser()
        out["repository"] = repo.id
        out["test_command"] = " ".join(repo.test_command or [])
        out["dependency_files"] = {name: _git_show(path, repo.base_branch, name)[:3000]
                                   for name in DEPENDENCY_FILES if _git_show(path, repo.base_branch, name)}
        out["test_environment_packages"] = _test_packages(repo)
        return out
    return facts


def _git_show(path: Path, branch: str, name: str) -> str:
    try:
        p = subprocess.run(["git", "-C", str(path), "show", f"{branch}:{name}"], capture_output=True, text=True,
                           timeout=30, stdin=subprocess.DEVNULL)
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _test_packages(repo: RepoConfig) -> list[str] | str:
    cmd = list(repo.test_command or [])
    if not cmd or not Path(cmd[0]).name.startswith("python"):
        return "unknown (the test command does not run a local Python interpreter: " + " ".join(cmd[:3]) + ")"
    if cmd[0] in _cache:
        return _cache[cmd[0]]
    try:
        p = subprocess.run([cmd[0], "-m", "pip", "list", "--format=freeze", "--disable-pip-version-check"],
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        if p.returncode != 0:
            p = subprocess.run(["uv", "pip", "list", "--format=freeze", "--python", cmd[0]],
                               capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        result: list[str] | str = sorted(ln.strip() for ln in p.stdout.splitlines() if "==" in ln) \
            if p.returncode == 0 else "unknown (package listing failed)"
    except (OSError, subprocess.TimeoutExpired):
        result = "unknown (package listing failed)"
    _cache[cmd[0]] = result
    return result
