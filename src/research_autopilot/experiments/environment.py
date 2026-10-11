"""Reproducible execution environment from the fixed commit.

- checkout: ``git archive <commit>`` extracted into the job's own directory (no git link,
  never shared between jobs, never updated in place);
- Python environment: a cached venv keyed by the dependency files *at the commit* and the
  canonical Python interpreter selector; installed with uv and frozen (``uv pip freeze``)
  for provenance;
- argv: the spec's launcher (``python`` / ``uv run python``) is replaced by the venv interpreter;
  everything else is passed as-is (no shell).
"""

import hashlib
import io
import os
import subprocess
import tarfile
import time
from pathlib import Path

from research_autopilot.implementation.experiment import ExperimentSpec


class EnvironmentError_(Exception):  # noqa: N801 - avoid shadowing builtins.EnvironmentError
    pass


def export_commit(repo: Path, commit: str, dest: Path) -> None:
    if (dest / ".export-complete").exists():
        return
    dest.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit], capture_output=True)
    if p.returncode != 0:
        raise EnvironmentError_(f"git archive {commit[:12]} failed: {p.stderr.decode()[:300]}")
    with tarfile.open(fileobj=io.BytesIO(p.stdout)) as tar:
        tar.extractall(dest, filter="data")
    (dest / ".export-complete").write_text(commit)


def _git_show(repo: Path, commit: str, rel: str) -> bytes:
    p = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{rel}"], capture_output=True)
    if p.returncode != 0:
        raise EnvironmentError_(f"{rel} not found at {commit[:12]}")
    return p.stdout


def env_key(repo: Path, spec: ExperimentSpec) -> str:
    h = hashlib.sha256(spec.environment.python_version.encode())
    for f in sorted(spec.environment.dependency_files):
        h.update(f.encode() + b"\0" + _git_show(repo, spec.identification.commit, f))
    return h.hexdigest()[:20]


def ensure_venv(repo: Path, spec: ExperimentSpec, checkout: Path, cache_dir: Path, timeout: int = 1800) -> dict[str, str]:
    """Create (once) and return provenance of the cached venv for this spec."""
    key = env_key(repo, spec)
    venv = cache_dir / key
    ready = venv / ".ready"
    lock = cache_dir / f"{key}.lock"
    cache_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while not ready.exists():
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() > deadline:
                raise EnvironmentError_(f"timed out waiting for environment {key}") from None
            time.sleep(2)
            continue
        try:
            os.close(fd)
            _install(spec, checkout, venv, timeout)
            ready.write_text(key)
        finally:
            lock.unlink(missing_ok=True)
    freeze = (venv / "freeze.txt").read_text() if (venv / "freeze.txt").exists() else ""
    deps = set(spec.environment.dependency_files)
    return {"env_key": key, "python": str(venv / "bin" / "python"),
            "python_version": _run([str(venv / "bin" / "python"), "--version"], checkout, 60).strip(),
            "locked": str(bool({"uv.lock", "poetry.lock", "Pipfile.lock"} & {d.split("/")[-1] for d in deps})),
            "freeze_sha256": hashlib.sha256(freeze.encode()).hexdigest()}


def _install(spec: ExperimentSpec, checkout: Path, venv: Path, timeout: int) -> None:
    deps = [d for d in spec.environment.dependency_files]
    names = {d.split("/")[-1]: d for d in deps}
    _run(["uv", "venv", "--quiet", "--python", spec.environment.python_version, str(venv)], checkout, timeout)
    py = str(venv / "bin" / "python")
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv), "VIRTUAL_ENV": str(venv)}
    if "uv.lock" in names:
        _run(["uv", "sync", "--frozen", "--no-install-project"], checkout, timeout, env)
    elif "requirements.txt" in names:
        _run(["uv", "pip", "install", "--quiet", "--python", py, "-r", names["requirements.txt"]], checkout, timeout)
    elif "pyproject.toml" in names:
        _run(["uv", "pip", "install", "--quiet", "--python", py, "-r", names["pyproject.toml"]], checkout, timeout)
    else:
        raise EnvironmentError_(f"unsupported dependency files {deps}")
    (venv / "freeze.txt").write_text(_run(["uv", "pip", "freeze", "--python", py], checkout, 300))


def _run(cmd: list[str], cwd: Path, timeout: int, env: dict | None = None) -> str:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EnvironmentError_(f"{cmd[0]} failed: {exc}") from exc
    if p.returncode != 0:
        raise EnvironmentError_(f"{' '.join(cmd[:3])} failed ({p.returncode}): {(p.stderr or p.stdout)[-500:]}")
    return p.stdout


def resolve_argv(argv: list[str], python: str) -> list[str]:
    if argv[0] in ("python", "python3"):
        return [python, *argv[1:]]
    if argv[:3] == ["uv", "run", "python"] or argv[:3] == ["uv", "run", "python3"]:
        return [python, *argv[3:]]
    raise EnvironmentError_(f"unsupported launcher for execution: {argv[:3]}")


# ---------------------------------------------------------------- container mode (RepoConfig.container)


def container_argv(container: dict, checkout: Path, argv: list[str], working_directory: str, env: dict[str, str],
                   mounts: list[Path]) -> list[str]:
    """``docker compose run`` of the repository's service with the job checkout as its project directory (the
    compose file's ``.`` volume therefore mounts this checkout, never the live repository). ``mounts`` are host
    directories the job reads (inputs, the job directory) and are mounted at the same path."""
    if argv[:1] in (["python"], ["python3"]):
        inner = ["python", *argv[1:]]
    elif argv[:3] in (["uv", "run", "python"], ["uv", "run", "python3"]):
        inner = ["python", *argv[3:]]
    else:
        raise EnvironmentError_(f"unsupported launcher for execution: {argv[:3]}")
    workdir = (Path(container.get("workdir", "/workspace")) / working_directory).as_posix().rstrip("/.") or "/"
    cmd = ["docker", "compose", "-f", str(checkout / container.get("compose_file", "compose.yaml")),
           "--project-directory", str(checkout), "-p", container["compose_project"], "run", "--rm", "-T",
           "--no-deps", "-w", workdir]
    for m in sorted({str(Path(x).resolve()) for x in mounts}):
        cmd += ["-v", f"{m}:{m}"]
    for k, v in sorted(env.items()):
        cmd += ["-e", f"{k}={v}"]
    return cmd + [container["service"], *inner]


def container_provenance(container: dict, checkout: Path) -> dict[str, str]:
    """Image identity of the service (the environment is the image, not a dependency file)."""
    base = ["docker", "compose", "-f", str(checkout / container.get("compose_file", "compose.yaml")),
            "--project-directory", str(checkout), "-p", container["compose_project"]]
    try:
        image = _run(base + ["config", "--images"], checkout, 120).split()
        image = image[0] if image else ""
        image_id = _run(["docker", "image", "inspect", "-f", "{{.Id}}", image], checkout, 60).strip() if image else ""
    except EnvironmentError_ as exc:
        raise EnvironmentError_(f"container image not available: {exc}") from exc
    if not image_id:
        raise EnvironmentError_(f"container image for service {container['service']} is not built on this host")
    return {"env_key": f"container:{image_id[:19]}", "container_image": image, "container_image_id": image_id,
            "python": "python (container)", "locked": "image"}
