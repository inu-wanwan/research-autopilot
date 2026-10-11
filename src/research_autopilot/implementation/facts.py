"""Measured environment facts for the Planner (docs/phase_specs/phase2.md, Definition of Done).

A plan whose Definition of Done needs a library the test environment lacks, or an environment Phase 4 does not
build, can only fail in review. The Planner therefore gets: how Phase 4 builds the run environment, the
repository's dependency files at its base branch, and the packages installed in the test environment (when the
test command runs a local Python interpreter; a containerized test command is reported as such).
"""

import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from research_autopilot.implementation.models import RepoConfig
from research_autopilot.models import TaskSpec

PHASE4_ENVIRONMENT_CONTRACT = (
    "Phase 4 exports the implementation commit (`git archive`, no .git), creates `uv venv --python <experiment.json "
    "environment.python_version>` and installs only the dependencies of environment.dependency_files from the "
    "package index (uv.lock -> `uv sync --frozen --no-install-project`; else `uv pip install -r requirements.txt`; "
    "else `uv pip install -r pyproject.toml`), recording the installed versions with `uv pip freeze`. The "
    "repository itself is NOT installed (no `pip install .` / `-e .`) and install_argv is not executed; no lock "
    "file, offline install or container image is needed. execution.setup steps and execution.argv then run with "
    "the venv's Python from <checkout>/<execution.working_directory>, so the code must import from the checkout "
    "as it is: run scripts by path, use `python -m` only for modules importable from the working directory, and "
    "with a src/ layout put src/ on sys.path explicitly (or set the working directory to src/). The repository's "
    "tests (Phase 3) run with the test command below, in the test environment."
)
def container_contract(repo: RepoConfig) -> str:
    c = repo.container or {}
    return (f"This repository runs in its own container image: Phase 4 exports the commit (`git archive`) and runs "
            f"experiment.json execution.setup steps and execution.argv with `docker compose run {c.get('service')}` "
            f"(project {c.get('compose_project')}), the exported commit mounted at {c.get('workdir', '/workspace')} "
            "and used as the working directory root. The image (built from the repository's Dockerfile / "
            "environment.yml) is the environment: do not add dependency declarations; environment.python_version "
            "must match the image's Python; launcher `python`. Input manifests (RA_CATALOG_INPUTS) and their files "
            "are mounted read-only at their host paths. Repository tests (Phase 3) run with the test command below.")


DEPENDENCY_FILES = ("pyproject.toml", "requirements.txt", "uv.lock")
CACHE_TTL_SECONDS = 300  # the test environment can change (packages installed) while the service runs
_cache: dict[str, tuple[float, list[str] | str]] = {}


def repo_id(repository: str | None) -> str | None:
    return next((ln.strip().strip("-*` ").split()[0] for ln in (repository or "").splitlines()
                 if ln.strip().strip("-*` ")), None)


def environment_facts(registry: dict[str, RepoConfig]) -> Callable[[TaskSpec], dict | None]:
    def facts(spec: TaskSpec) -> dict | None:
        repo = registry.get(repo_id(spec.repository) or "")
        out: dict = {"phase4_environment": PHASE4_ENVIRONMENT_CONTRACT}
        if repo is None:
            return out
        if repo.container:
            out["phase4_environment"] = container_contract(repo)
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
    if repo.container:  # the repository's image is the environment of tests and experiments alike
        c = repo.container
        key = f"container:{repo.id}"
        if (hit := _cache.get(key)) and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        try:
            p = subprocess.run(["docker", "compose", "-p", c["compose_project"], "run", "--rm", "-T", "--no-deps",
                                c["service"], "python", "-m", "pip", "list", "--format=freeze"],
                               cwd=Path(repo.path).expanduser(), capture_output=True, text=True, timeout=600,
                               stdin=subprocess.DEVNULL)
            result: list[str] | str = sorted(ln.strip() for ln in p.stdout.splitlines() if "==" in ln) \
                if p.returncode == 0 else "unknown (package listing in the container failed)"
        except (OSError, subprocess.TimeoutExpired):
            result = "unknown (package listing in the container failed)"
        _cache[key] = (time.monotonic(), result)
        return result
    if not cmd or not Path(cmd[0]).name.startswith("python"):
        return "unknown (the test command does not run a local Python interpreter: " + " ".join(cmd[:3]) + ")"
    if (hit := _cache.get(cmd[0])) and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
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
    _cache[cmd[0]] = (time.monotonic(), result)
    return result
