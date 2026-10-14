"""Docker isolation for untrusted code (verification) and for granted research executions.

* Dependencies: ``pip download --only-binary=:all:`` from PyPI inside a throw-away container -- only pip runs
  (no package build scripts, no repository code); then the image is built with ``--network none`` from exactly
  those hash-locked wheels.
* Verification runs: no network, no host environment variables, no secrets, no home or research directories;
  the code is mounted read-only and copied into a size-limited tmpfs; CPU / memory / PIDs / time are limited;
  all capabilities dropped, no-new-privileges, read-only root filesystem, unprivileged user.
* Research runs (``research_argv``) mount only what the grant allows.
* Rootless Docker (e.g. the dedicated ``ra`` user on Eagle): containers that write to bind mounts run as the
  container's root, which is the calling host user there -- the host uid would map to an unprivileged subuid
  that cannot write the host directories.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from research_autopilot.experiments.resources import CDI_GPU_DEVICE as GPU_DEVICE  # never in verification runs
from research_autopilot.external.models import SandboxLimits, Wheel

NOBODY = "65534:65534"
WHEEL = re.compile(r"^(?P<name>[A-Za-z0-9_.]+)-(?P<version>[^-]+)-.*\.whl$")


class DockerError(RuntimeError):
    pass


class Docker:
    def __init__(self, run=subprocess.run, binary: str | None = None, rootless: bool | None = None) -> None:
        self._run = run
        self.binary = binary or shutil.which("docker") or "docker"
        self._rootless = rootless

    def _call(self, args: list[str], *, timeout: float = 600, check: bool = True):
        p = self._run([self.binary, *args], capture_output=True, text=True, timeout=timeout,
                      stdin=subprocess.DEVNULL, env=_clean_env())
        if check and p.returncode != 0:
            raise DockerError(f"docker {args[0]} failed ({p.returncode}): {(p.stderr or p.stdout)[-400:]}")
        return p

    def rootless(self) -> bool:
        """Whether the daemon runs rootless (asked once; an unreachable daemon counts as rootful)."""
        if self._rootless is None:
            try:
                p = self._call(["info", "--format", "{{json .SecurityOptions}}"], timeout=30, check=False)
                self._rootless = p.returncode == 0 and "name=rootless" in p.stdout
            except (OSError, subprocess.SubprocessError):
                self._rootless = False
        return self._rootless

    def _writer_user(self) -> str:
        """User for containers that write to bind mounts: the host user's own files stay its own."""
        return "0:0" if self.rootless() else _host_user()

    def architecture(self) -> str:
        return self._call(["info", "--format", "{{.Architecture}}"], timeout=30).stdout.strip()

    def base_image(self, python_version: str) -> str:
        ref = f"python:{python_version}-slim"
        self._call(["pull", "-q", ref], timeout=900)
        digests = self._call(["image", "inspect", ref, "--format", "{{json .RepoDigests}}"]).stdout
        digest = next((d for d in json.loads(digests or "[]") if "@sha256:" in d), None)
        if not digest:
            raise DockerError(f"no registry digest for {ref}")
        return digest

    # ---------------------------------------------------------------- dependencies

    def download_wheels(self, base: str, requirements: list[str], dest: Path,
                        limits: SandboxLimits) -> tuple[list[Wheel], list[str]]:
        """Binary wheels only (no build scripts). Requirements that cannot be satisfied are reported, not fatal."""
        dest.mkdir(parents=True, exist_ok=True)
        failed: list[str] = []
        if requirements and not self._pip_download(base, requirements, dest, limits):
            for req in requirements:  # find the culprits, keep the rest
                if not self._pip_download(base, [req], dest, limits):
                    failed.append(req)
        wheels = []
        for f in sorted(dest.glob("*.whl")):
            m = WHEEL.match(f.name)
            if m:
                wheels.append(Wheel(name=m["name"], version=m["version"], filename=f.name,
                                    sha256=hashlib.sha256(f.read_bytes()).hexdigest()))
        return wheels, failed

    def _pip_download(self, base: str, reqs: list[str], dest: Path, limits: SandboxLimits) -> bool:
        p = self._call(["run", "--rm", "--network", "bridge", *self._limits(limits), "--user", self._writer_user(),
                        "-e", "HOME=/tmp", "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1", "-v", f"{dest}:/wheels:rw",
                        base, "python", "-m", "pip", "download", "--only-binary=:all:", "--no-cache-dir",
                        "--index-url", "https://pypi.org/simple", "-d", "/wheels", *reqs],
                       timeout=limits.timeout_s, check=False)
        return p.returncode == 0

    def build(self, base: str, wheels: list[Wheel], wheel_dir: Path, tag: str) -> str:
        lock = "\n".join(f"{w.name}=={w.version} --hash=sha256:{w.sha256}" for w in wheels) + "\n"
        with tempfile.TemporaryDirectory() as ctx:
            c = Path(ctx)
            (c / "wheels").mkdir()
            for w in wheels:
                shutil.copy2(wheel_dir / w.filename, c / "wheels" / w.filename)
            (c / "wheels" / "lock.txt").write_text(lock)
            install = ("RUN pip install --no-cache-dir --no-index --find-links /wheels --require-hashes "
                       "-r /wheels/lock.txt && rm -rf /wheels\n") if wheels else "RUN rm -rf /wheels\n"
            (c / "Dockerfile").write_text(f"FROM {base}\nCOPY wheels /wheels\n{install}"
                                          "ENV PYTHONDONTWRITEBYTECODE=1 MPLBACKEND=Agg\n")
            self._call(["build", "--network", "none", "-q", "-t", tag, str(c)], timeout=1800)
        return self._call(["image", "inspect", tag, "--format", "{{.Id}}"]).stdout.strip()

    def image_exists(self, image_id: str) -> bool:
        return self._call(["image", "inspect", image_id, "--format", "{{.Id}}"], check=False).returncode == 0

    # ---------------------------------------------------------------- isolated verification run

    def run_isolated(self, image: str, code_dir: Path, argv: list[str], *, workdir: str = ".",
                     python_dirs: list[str] = (), limits: SandboxLimits) -> dict:
        name = f"ra-verify-{os.getpid()}-{int(time.time() * 1000) % 10**9}"
        pythonpath = ":".join(f"/work/{d}".rstrip("/.") or "/work" for d in python_dirs or ["."])
        args = ["run", "--rm", "--name", name, "--network", "none", *self._limits(limits), *self._hardening(),
                "--tmpfs", f"/work:rw,exec,size={int(limits.disk_gb * 1024)}m,mode=1777",
                "--user", NOBODY, "-e", "HOME=/tmp", "-e", f"PYTHONPATH={pythonpath}",
                "-v", f"{code_dir}:/ext/code:ro", "-w", "/work", image,
                "sh", "-c", 'cp -R /ext/code/. /work/ && cd "/work/$0" && exec "$@"', workdir, *argv]
        t0 = time.monotonic()
        try:
            p = self._call(args, timeout=limits.timeout_s, check=False)
            return {"exit_code": p.returncode, "stdout": p.stdout[-4000:], "stderr": p.stderr[-4000:],
                    "timed_out": False, "duration_s": round(time.monotonic() - t0, 2)}
        except subprocess.TimeoutExpired:
            self._call(["kill", name], check=False, timeout=60)
            return {"exit_code": None, "stdout": "", "stderr": f"timed out after {limits.timeout_s}s",
                    "timed_out": True, "duration_s": round(time.monotonic() - t0, 2)}

    def _limits(self, limits: SandboxLimits) -> list[str]:
        return ["--cpus", f"{limits.cpus:g}", "--memory", f"{int(limits.memory_gb * 1024)}m",
                "--memory-swap", f"{int(limits.memory_gb * 1024)}m", "--pids-limit", str(limits.pids)]

    @staticmethod
    def _hardening() -> list[str]:
        return ["--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
                "--tmpfs", "/tmp:rw,exec,size=512m,mode=1777"]

    # ---------------------------------------------------------------- granted research run

    def research_argv(self, *, image: str, name: str, code_dir: Path, mount_as: str, checkout: Path,
                      checkout_mode: str, data_paths: list[str], network: str, cpus: float, memory_gb: float,
                      workdir: str, env: dict[str, str], python_dirs: list[str], argv: list[str],
                      gpu_ids: list[int] = ()) -> list[str]:
        """``docker run`` argv for one approved research execution (run by the Phase 4 job shim).

        GPUs (only within an approved ``max_gpus``) come in through CDI.  WSL's CDI spec has only
        ``nvidia.com/gpu=all``, so the job's own devices are selected with ``CUDA_VISIBLE_DEVICES``."""
        if network not in ("none", "unrestricted"):
            raise ValueError(f"unknown network scope {network}")
        ext = f"/ext/{mount_as}"
        pythonpath = ":".join(f"{ext}/{d}".rstrip("/.") or ext for d in python_dirs or ["."])
        out = [self.binary, "run", "--rm", "--init", "--name", name,
               "--network", "none" if network == "none" else "bridge",
               "--cpus", f"{cpus:g}", "--memory", f"{int(memory_gb * 1024)}m",
               "--memory-swap", f"{int(memory_gb * 1024)}m", "--pids-limit", "512", *self._hardening(),
               "--user", self._writer_user(), "-e", "HOME=/tmp", "-e", f"PYTHONPATH={pythonpath}",
               "-v", f"{code_dir}:{ext}:ro", "-v", f"{checkout}:/work:{checkout_mode}"]
        for i, d in enumerate(data_paths):
            out += ["-v", f"{d}:/data/{i}:ro"]
        if gpu_ids:
            out += ["--device", GPU_DEVICE, "-e", "CUDA_DEVICE_ORDER=PCI_BUS_ID",
                    "-e", "CUDA_VISIBLE_DEVICES=" + ",".join(str(i) for i in gpu_ids)]
        for k, v in sorted(env.items()):
            out += ["-e", f"{k}={v}"]
        return out + ["-w", f"/work/{workdir}".rstrip("/.") or "/work", image, *argv]


def _host_user() -> str:
    return f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else NOBODY


def _clean_env() -> dict[str, str]:
    """The docker CLI gets PATH / HOME / Docker settings only -- never API keys or tokens."""
    keep = ("PATH", "HOME", "LANG", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")
    return {k: os.environ[k] for k in keep if k in os.environ}
