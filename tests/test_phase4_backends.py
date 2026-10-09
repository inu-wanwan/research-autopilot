"""Real-process tests of LocalBackend (detached shim), Slurm with a fake executor, and orchestrator resume."""

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from phase4_fakes import RESULT_OK, make_exp
from research_autopilot.experiments.backends import local as local_mod
from research_autopilot.experiments.backends.base import BackendNotConfigured
from research_autopilot.experiments.backends.fake import FakeComputeBackend
from research_autopilot.experiments.backends.local import LocalBackend
from research_autopilot.experiments.backends.slurm import SlurmBackend, SlurmConfig
from research_autopilot.experiments.models import (
    Allocation,
    ExperimentState,
    JobHandle,
    JobSpec,
    JobState,
    ResourceEstimate,
)
from research_autopilot.experiments.orchestrator import run_experiment
from research_autopilot.experiments.resources import detect_host
from research_autopilot.storage.db import Store


def alloc(mem=1.0, wall=60) -> Allocation:
    est = ResourceEstimate(cpu=1, memory_gb=mem, gpu=0, walltime_minutes=wall / 60, basis="default", confidence="low",
                           rationale=[])
    return Allocation(cpu=1, memory_gb=mem, gpu=0, walltime_seconds=wall, threads=1, estimate=est)


def job(tmp_path: Path, code: str, key="run:exp:1", mem=1.0, wall=60, secret_names=()) -> JobSpec:
    run_dir = tmp_path / key.replace(":", "_")
    src = run_dir / "src"
    src.mkdir(parents=True)
    (src / "job.py").write_text(code)
    return JobSpec(job_key=key, experiment_run_id="run", experiment_id="exp", spec_id="s", commit="c" * 40,
                   argv=[sys.executable, "job.py"], working_directory=".", env={"OMP_NUM_THREADS": "1"},
                   secret_env_names=list(secret_names), allocation=alloc(mem, wall), run_dir=str(run_dir),
                   checkout_dir=str(src), outputs=["out/result.json"], heartbeat_seconds=5)


def wait(backend, handle, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = backend.status(handle)
        if st.state.terminal:
            return st
        time.sleep(0.3)
    raise AssertionError("job did not finish")


@pytest.fixture
def store(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 'ledger.db'}")
    s.init_schema()
    return s


WRITE_OK = ("import json, os, pathlib\npathlib.Path('out').mkdir(exist_ok=True)\n"
            "json.dump({'threads': os.environ['OMP_NUM_THREADS'], 'secret_seen': os.environ.get('MY_TOKEN') == 'tok123'},"
            " open('out/result.json', 'w'))\nprint('done')\n")


def test_local_submit_status_collect_and_secret_not_on_disk(tmp_path, store, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "tok123")
    b = LocalBackend(store)
    js = job(tmp_path, WRITE_OK, secret_names=["MY_TOKEN"])
    h = b.submit(js)
    st = wait(b, h)
    assert st.state is JobState.SUCCEEDED and st.exit_code == 0 and st.peak_rss_gb is not None
    res = b.collect(h, js.outputs)
    assert not res.missing and '"secret_seen": true' in Path(res.artifacts[0].stored_path).read_text()
    assert oct(os.stat(res.artifacts[0].stored_path).st_mode)[-3:] == "444"  # immutable copy
    assert (Path(js.run_dir) / "stdout.log").read_text().strip() == "done"
    for f in Path(js.run_dir).rglob("*"):  # the secret value is never written to disk by the backend
        if f.is_file() and "artifacts" not in f.parts and "src" not in f.parts:
            assert "tok123" not in f.read_text(errors="ignore"), f
    assert store.ledger_get(js.job_key)["state"] == "succeeded"


def test_duplicate_submit_is_idempotent(tmp_path, store):
    b = LocalBackend(store)
    js = job(tmp_path, "import time; time.sleep(1)")
    h1, h2 = b.submit(js), b.submit(js)
    assert h1.external_id == h2.external_id
    wait(b, h1)


def test_failure_exit_code_and_stderr_separate(tmp_path, store):
    b = LocalBackend(store)
    js = job(tmp_path, "import sys; print('out'); sys.stderr.write('boom\\n'); sys.exit(3)")
    st = wait(b, b.submit(js))
    assert st.state is JobState.FAILED and st.exit_code == 3
    assert (Path(js.run_dir) / "stderr.log").read_text().strip() == "boom"


def test_cancel(tmp_path, store):
    b = LocalBackend(store)
    js = job(tmp_path, "import time; time.sleep(60)")
    h = b.submit(js)
    time.sleep(1.0)
    b.cancel(h)
    assert wait(b, h).state is JobState.CANCELLED


def test_walltime_and_memory_limits_enforced(tmp_path, store):
    b = LocalBackend(store)
    st = wait(b, b.submit(job(tmp_path, "import time; time.sleep(60)", key="r:wall:1", wall=2)))
    assert st.state is JobState.RESOURCE_EXCEEDED and "walltime" in st.reason
    hog = "import time\nx = bytearray(400 * 1024 * 1024)\nfor i in range(0, len(x), 4096): x[i] = 1\ntime.sleep(30)\n"
    st = wait(b, b.submit(job(tmp_path, hog, key="r:mem:1", mem=0.2)))
    assert st.state is JobState.RESOURCE_EXCEEDED and "memory" in st.reason


def test_missing_secret_env_fails_submit(tmp_path, store, monkeypatch):
    monkeypatch.delenv("ABSENT_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        LocalBackend(store).submit(job(tmp_path, "pass", secret_names=["ABSENT_TOKEN"]))


def test_crash_before_spawn_is_detected_as_never_started_and_resubmittable(tmp_path, store, monkeypatch):
    monkeypatch.setattr(local_mod, "GRACE_SECONDS", 0)
    b = LocalBackend(store)
    js = job(tmp_path, WRITE_OK)
    assert store.ledger_insert(js.job_key, "run", "local", js.run_dir)  # orchestrator died right after this
    rec = b.recover()
    assert rec[0].reason == "never_started" and store.ledger_get(js.job_key)["state"] == "never_started"
    assert b.forget_never_started(js.job_key)
    assert wait(b, b.submit(js)).state is JobState.SUCCEEDED


def test_crash_after_spawn_before_pid_saved_is_adopted(tmp_path, store):
    b = LocalBackend(store)
    js = job(tmp_path, "import time; time.sleep(2)")
    h = b.submit(js)
    time.sleep(1.0)  # the shim has started and written shim.pid
    store.ledger_update(js.job_key, state="submitting", external_id=None)  # pid never recorded
    statuses = b.recover()
    assert statuses and statuses[0].state in (JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED)
    assert store.ledger_get(js.job_key)["external_id"] == h.external_id  # adopted from shim.pid
    assert wait(b, h).state is JobState.SUCCEEDED


def test_killed_supervisor_is_reported_lost_not_success(tmp_path, store):
    b = LocalBackend(store)
    js = job(tmp_path, "import time; time.sleep(30)")
    h = b.submit(js)
    time.sleep(1.5)
    os.kill(int(h.external_id), signal.SIGKILL)
    time.sleep(0.5)
    st = b.status(h)
    assert st.state is JobState.LOST
    # clean up the orphaned job process group
    import json
    child = json.loads((Path(js.run_dir) / "status.json").read_text()).get("pid")
    if child:
        try:
            os.killpg(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_detect_host_reports_real_resources():
    h = detect_host()
    assert h.cpu_logical >= 1 and h.memory_total_gb > 0 and h.memory_available_gb > 0


# ---------------------------------------------------------------- Slurm (fake executor; never connects)


CFG = SlurmConfig(host="tsubame", partition="node_q", account="tgx-test", remote_root="/gs/fs/x/ra", transfer="rsync",
                  max_walltime_minutes=60, policy_acknowledged=True)


class FakeSlurm:
    def __init__(self):
        self.calls = []
        self.jobs = {}

    def __call__(self, argv):
        self.calls.append(argv)
        remote = argv[-1]
        if remote.startswith("sbatch"):
            jid = str(1000 + len(self.jobs))
            self.jobs[jid] = "PENDING"
            return 0, f"{jid};cluster\n", ""
        if remote.startswith("sacct"):
            jid = remote.split()[2]
            st = self.jobs.get(jid)
            return 0, (f"{st}|0:0|512M|00:01:00\n" if st else ""), ""
        if remote.startswith("squeue"):
            return 0, "", ""
        if remote.startswith("scancel"):
            self.jobs[remote.split()[1]] = "CANCELLED"
            return 0, "", ""
        return 1, "", "unknown"


def test_slurm_requires_human_configuration(tmp_path, store):
    b = SlurmBackend(SlurmConfig(host="tsubame"), store, executor=FakeSlurm())
    with pytest.raises(BackendNotConfigured) as exc:
        b.submit(job(tmp_path, "pass"))
    assert "partition" in str(exc.value) and "policy_acknowledged" in str(exc.value)
    with pytest.raises(BackendNotConfigured):
        SlurmBackend(CFG, store, executor=None).submit(job(tmp_path, "pass", key="r:x:1"))


def test_slurm_script_submit_status_cancel(tmp_path, store):
    fake = FakeSlurm()
    b = SlurmBackend(CFG, store, executor=fake)
    js = job(tmp_path, "pass")
    script = b.sbatch_script(js)
    assert "#SBATCH --partition=node_q" in script and "#SBATCH --account=tgx-test" in script
    assert "#SBATCH --mem=1024M" in script and "export OMP_NUM_THREADS=1" in script
    assert "exec " in script and ";" not in script.split("exec ")[1]
    h = b.submit(js)
    assert h.external_id == "1000" and fake.calls[0][:3] == ["ssh", "-o", "BatchMode=yes"]
    assert b.submit(js).external_id == "1000"  # idempotent
    assert b.status(h).state is JobState.QUEUED
    fake.jobs["1000"] = "COMPLETED"
    assert b.status(h).state is JobState.SUCCEEDED
    fake.jobs["1000"] = "OUT_OF_MEMORY"
    assert b.status(h).state is JobState.RESOURCE_EXCEEDED
    b.cancel(h)
    assert fake.jobs["1000"] == "CANCELLED"
    with pytest.raises(BackendNotConfigured):
        b.collect(h, js.outputs)  # transfer method not decided yet


def test_slurm_unknown_submit_state_is_not_resubmitted(tmp_path, store):
    b = SlurmBackend(CFG, store, executor=FakeSlurm())
    js = job(tmp_path, "pass")
    store.ledger_insert(js.job_key, "run", "slurm", js.run_dir)  # crash before sbatch id was saved
    with pytest.raises(RuntimeError, match="refusing to resubmit"):
        b.submit(js)


# ---------------------------------------------------------------- orchestrator resume


class Died(BaseException):
    pass


def test_orchestrator_crash_and_resume(tmp_path):
    compute = FakeComputeBackend([{"state": "succeeded", "exit_code": 0, "polls_until_done": 3,
                                   "outputs": {"results/h2.json": RESULT_OK}}])
    deps, runs, *_ = make_exp(tmp_path, compute=compute)
    calls = {"n": 0}

    def dying_sleep(_):
        calls["n"] += 1
        raise Died()

    deps.sleep = dying_sleep
    with pytest.raises(Died):
        run_experiment(deps, phase3_run_ids=runs)
    crashed = deps.store.list_experiments("INU-900")[0]
    m0 = deps.store.get_experiment(crashed["experiment_run_id"])
    assert m0.state is ExperimentState.RUNNING and m0.jobs[0].handle is not None
    deps.sleep = lambda s: None
    m = run_experiment(deps, resume_run_id=m0.experiment_run_id)
    assert m.state is ExperimentState.COMPLETED, m.block_reason
    assert len(compute.submits) == 1  # the running job was adopted, not resubmitted
    assert any(e.kind == "recovery" for e in m.events)
