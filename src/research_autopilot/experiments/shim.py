"""Detached job wrapper ("supervisor") for LocalBackend.

Started in its own session by LocalBackend; survives the orchestrator / worker
process. It writes ``launch.started`` + ``shim.pid`` first (crash-safe evidence
that the job started), runs the job argv without a shell, writes heartbeats and
resource usage to ``status.json`` (atomic replace) and enforces the memory /
wall-time limits by killing the job's process group.

    python -m research_autopilot.experiments.shim <run_dir>
"""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _tree_usage(proc: psutil.Process) -> tuple[float, float]:
    rss, cpu = 0, 0.0
    try:
        procs = [proc, *proc.children(recursive=True)]
    except psutil.Error:
        return 0.0, 0.0
    for p in procs:
        try:
            rss += p.memory_info().rss
            t = p.cpu_times()
            cpu += t.user + t.system
        except psutil.Error:
            pass
    return rss / 2**30, cpu


def _kill_group(child: subprocess.Popen) -> None:
    """SIGTERM the job's process group, then SIGKILL whatever is left."""
    for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 0.0)):
        try:
            os.killpg(child.pid, sig)
        except (ProcessLookupError, PermissionError):  # group already gone (macOS reports EPERM for zombies)
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.1)
        if child.poll() is not None:
            return


def main(run_dir: str) -> int:
    rd = Path(run_dir)
    job = json.loads((rd / "job.json").read_text())
    (rd / "shim.pid").write_text(str(os.getpid()))
    (rd / "launch.started").write_text(_now())
    status = {"state": "running", "started_at": _now(), "heartbeat_at": _now(), "shim_pid": os.getpid()}
    cancelled = {"flag": False}

    def on_term(signum, frame):  # cancellation
        cancelled["flag"] = True

    signal.signal(signal.SIGTERM, on_term)
    # The job's environment is the shim's own environment, minus what only the shim needs.
    env = {k: v for k, v in os.environ.items() if k not in ("RA_SHIM_PYTHONPATH",)}
    if os.environ.get("RA_SHIM_PYTHONPATH"):
        env.pop("PYTHONPATH", None)  # do not leak the orchestrator's site-packages into the job
    with open(rd / "stdout.log", "wb") as out, open(rd / "stderr.log", "wb") as err:
        try:
            child = subprocess.Popen(job["argv"], cwd=job["cwd"], env=env, stdout=out, stderr=err,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            _write(rd / "status.json", {**status, "state": "failed", "finished_at": _now(), "exit_code": None,
                                        "reason": f"launch failed: {exc}"})
            return 1
        status["pid"] = child.pid
        proc = psutil.Process(child.pid)
        peak, cpu = 0.0, 0.0
        t0 = time.monotonic()
        reason, state = None, None
        interval = max(0.5, min(job.get("poll_seconds", 2), 10))
        while child.poll() is None:
            rss, cpu = _tree_usage(proc)
            peak = max(peak, rss)
            if cancelled["flag"]:
                state, reason = "cancelled", "cancel requested"
            elif rss > job["memory_limit_gb"]:
                state, reason = "resource_exceeded", f"memory {rss:.2f}GB > limit {job['memory_limit_gb']}GB"
            elif time.monotonic() - t0 > job["walltime_seconds"]:
                state, reason = "resource_exceeded", f"walltime > {job['walltime_seconds']}s"
            if state:
                _kill_group(child)
                child.wait()
                break
            _write(rd / "status.json", {**status, "heartbeat_at": _now(), "peak_rss_gb": round(peak, 4),
                                        "cpu_seconds": round(cpu, 2)})
            time.sleep(interval)
        rc = child.returncode
        final = state or ("succeeded" if rc == 0 else "failed")
        _write(rd / "status.json", {**status, "state": final, "heartbeat_at": _now(), "finished_at": _now(),
                                    "exit_code": rc if rc is not None and rc >= 0 else None,
                                    "signal": -rc if rc is not None and rc < 0 else None,
                                    "peak_rss_gb": round(peak, 4), "cpu_seconds": round(cpu, 2), "reason": reason,
                                    "wall_seconds": round(time.monotonic() - t0, 2)})
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1]))
