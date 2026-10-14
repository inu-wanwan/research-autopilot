"""Phase 4 GPU slots: one job per GPU, each job sees only its own GPU(s), CPU jobs see none."""

import pytest
from phase4_fakes import host

from research_autopilot.experiments import resources as res
from research_autopilot.experiments.config import Phase4Settings
from research_autopilot.experiments.models import Allocation, ResourceEstimate

CUDA = {"name": "NVIDIA GeForce RTX 4090", "vendor": "nvidia", "kind": "cuda", "memory_gb": 24.0, "used_gb": 0.0}


def _alloc(gpu: int, cpu: int = 2) -> Allocation:
    est = ResourceEstimate(cpu=cpu, memory_gb=1, gpu=gpu, walltime_minutes=10, basis="default", confidence="low",
                           rationale=[])
    return Allocation(cpu=cpu, memory_gb=1, gpu=gpu, walltime_seconds=600, threads=cpu, estimate=est)


def _rm(gpus) -> res.ResourceManager:
    s = Phase4Settings(max_parallel_jobs=3, max_job_cpu_total=24, max_job_memory_gb_total=40)
    return res.ResourceManager(s, host(cpu=32, mem=50, avail=45).model_copy(
        update={"gpus": gpus}))


def test_a_gpu_job_holds_its_gpu_and_a_second_one_waits_while_cpu_jobs_still_run():
    rm = _rm([CUDA])
    first = _alloc(1)
    assert rm.fits_now(first)
    first = rm.claim_gpus(first)
    assert first.gpu_ids == [0]
    rm.reserve("a", first)
    assert not rm.fits_now(_alloc(1))  # the only GPU is taken: no second training on the same card
    cpu_job = rm.claim_gpus(_alloc(0))
    assert rm.fits_now(cpu_job) and cpu_job.gpu_ids == []
    rm.release("a")
    assert rm.fits_now(_alloc(1)) and rm.claim_gpus(_alloc(1)).gpu_ids == [0]


def test_job_environment_exposes_only_the_jobs_own_gpus():
    rm = _rm([CUDA, {**CUDA, "name": "second"}])
    a = rm.claim_gpus(_alloc(1))
    rm.reserve("a", a)
    b = rm.claim_gpus(_alloc(1))
    assert (a.gpu_ids, b.gpu_ids) == ([0], [1])
    assert res.thread_env(b)["CUDA_VISIBLE_DEVICES"] == "1"
    env = res.thread_env(_alloc(0))
    assert env["CUDA_VISIBLE_DEVICES"] == "" and env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"


def test_gpu_job_on_a_host_without_cuda_is_refused():
    with pytest.raises(res.ResourceError):
        _rm([]).plan(ResourceEstimate(cpu=1, memory_gb=1, gpu=1, walltime_minutes=5, basis="default",
                                      confidence="low", rationale=[]))


def test_a_container_job_gets_the_gpu_only_through_its_own_override(tmp_path):
    from research_autopilot.experiments.environment import container_argv, gpu_override
    c = {"compose_project": "p", "service": "research", "workdir": "/workspace"}
    override = gpu_override(c, tmp_path)
    assert override.read_text() == "services:\n  research:\n    devices:\n      - nvidia.com/gpu=all\n"
    argv = container_argv(c, tmp_path / "src", ["python", "train.py"], ".", {"CUDA_VISIBLE_DEVICES": "0"}, [],
                          override)
    i = argv.index(str(override.resolve()))
    assert argv[i - 1] == "-f" and argv[i - 2].endswith("compose.yaml")  # layered on the repository's compose file
    assert "CUDA_VISIBLE_DEVICES=0" in argv
    plain = container_argv(c, tmp_path / "src", ["python", "train.py"], ".", {}, [])
    assert str(override.resolve()) not in plain and plain.count("-f") == 1


def test_the_planner_learns_the_hosts_gpus_and_the_rule_to_use_them():
    from research_autopilot.implementation.facts import environment_facts
    from research_autopilot.models import TaskSpec
    facts = environment_facts({}, gpus=lambda: ["NVIDIA GeForce RTX 4090, 24564 MiB"])(TaskSpec.model_construct(repository=None))
    assert facts["gpus"]["available"] == ["NVIDIA GeForce RTX 4090, 24564 MiB"]
    assert "resources.gpu >= 1" in facts["gpus"]["rule"]


def test_container_user_and_env_reach_every_compose_run_and_cannot_inject_flags(tmp_path):
    """Eagle (rootless Docker): user 0 + RESEARCH_USER=root keeps jobs as the host user, able to write their dirs."""
    from research_autopilot.experiments.environment import EnvironmentError_, container_argv, container_run_args
    c = {"compose_project": "p", "service": "research", "workdir": "/workspace", "user": "0",
         "env": {"RESEARCH_USER": "root"}}
    argv = container_argv(c, tmp_path, ["python", "run.py"], ".", {}, [])
    run = argv.index("run")
    assert argv[run:run + 8] == ["run", "--rm", "-T", "--no-deps", "--user", "0", "-e", "RESEARCH_USER=root"]
    assert argv[-3:] == ["research", "python", "run.py"]
    assert container_run_args({"compose_project": "p", "service": "s"}) == []
    for bad in ({"user": "--privileged"}, {"user": "0 --privileged"}, {"env": {"A B": "x"}}, {"env": "X=1"}):
        with pytest.raises(EnvironmentError_):
            container_run_args(bad)
