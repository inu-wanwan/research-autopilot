"""Fakes and fixtures for Phase 3 tests: isolated git repo, fake backends / reviewers,
and a real Phase 2 apply-mode run (with Phase 2 fakes) that produces the handoff."""

import json
import subprocess
import sys
from pathlib import Path

from phase2_fakes import FULL_SECTIONS, FakeLinear, FakeNotion, make_deps, make_issue, make_page, make_plan, review
from research_autopilot.implementation.config import Phase3Settings
from research_autopilot.implementation.models import (
    BackendStatus,
    CodeFinding,
    CodeReviewResult,
    ImplementationResult,
    RepoConfig,
    Usage,
)
from research_autopilot.implementation.orchestrator import ImplDeps
from research_autopilot.orchestrator.core import plan_task
from research_autopilot.tasks.manager import TaskManager

SECTIONS = {
    **FULL_SECTIONS,
    "repository": "h2-test",
    "file_scope": "- h2_energy.py\n- tests/**\n- configs/**",
    "non_goals": "No production calculations.",
}

GOOD_CODE = "def energy():\n    return -1.137\n"
CONFIG = json.dumps({"basis": "sto-3g", "bond_length_angstrom": 0.74}) + "\n"


def experiment_def(**overrides) -> str:
    d = {
        "experiment_id": "h2-sto3g",
        "execution": {"argv": ["python", "h2_energy.py", "--config", "configs/h2.json"], "working_directory": ".",
                      "setup": [], "env_var_names": ["OMP_NUM_THREADS"]},
        "environment": {"type": "python", "python_version": "3.12", "dependency_files": ["pyproject.toml"],
                        "install_argv": ["uv", "sync", "--frozen"]},
        "inputs": [{"path": "configs/h2.json", "kind": "config", "scientific": True, "description": "geometry/basis"}],
        "resources": {
            "cpu": {"value": 1, "basis": "estimated", "rationale": "single small SCF", "confidence": "high"},
            "memory_gb": {"value": 1, "basis": "estimated", "rationale": "STO-3G H2 is tiny", "confidence": "high"},
            "gpu": {"value": 0, "basis": "required", "rationale": "CPU-only code", "confidence": "high"},
            "walltime_minutes": {"value": 2, "basis": "estimated", "rationale": "seconds for FCI", "confidence": "medium"},
            "parallelizable": False, "parallel_notes": "", "job_dependencies": []},
        "outputs": [{"path": "results/h2.json", "format": "json", "required": True,
                     "units": {"e_rhf": "Hartree", "e_fci": "Hartree"}, "validation": "keys e_rhf, e_fci"}],
        "metrics": [{"name": "correlation_energy", "definition": "e_fci - e_rhf", "output": "results/h2.json",
                     "json_pointer": "/e_corr", "units": "Hartree"}],
        "numeric_checks": [{"name": "fci_below_rhf", "output": "results/h2.json", "json_pointer": "/e_fci",
                            "condition": "le", "reference_pointer": "/e_rhf"}],
        "reproduction": ["uv sync --frozen", "python h2_energy.py --config configs/h2.json"],
    }
    d.update(overrides)
    return json.dumps(d, indent=2)
GOOD_TEST = "from h2_energy import energy\n\n\ndef test_energy():\n    assert energy() < 0\n"
BAD_TEST = "from h2_energy import energy\n\n\ndef test_energy():\n    assert energy() > 0\n"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "research-repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "README.md").write_text("research repo\n")
    (repo / "data").mkdir()
    (repo / "data" / "validated.json").write_text("{}\n")
    (repo / "tests" / "__init__.py").write_text("")
    (repo / "conftest.py").write_text("")
    (repo / "pyproject.toml").write_text("[project]\nname = \"h2\"\nversion = \"0\"\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return repo


def repo_config(repo: Path) -> RepoConfig:
    return RepoConfig(id="h2-test", path=str(repo), base_branch="main", protected_paths=["data/**"],
                      test_command=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                      lint_command=None, typecheck_command=None, check_timeout_seconds=120)


class FakeBackend:
    """Each script item: dict(status=..., files={path: content}, delete=[...], tokens=int, cost=float|None, pid=int)."""

    def __init__(self, name: str, script: list[dict], authenticated: bool = True) -> None:
        self.name = name
        self.script = list(script)
        self.calls = []
        self.authenticated = authenticated

    def status(self) -> BackendStatus:
        return BackendStatus(backend=self.name, installed=True, authenticated=self.authenticated, billing="subscription")

    def run(self, request, on_start) -> ImplementationResult:
        self.calls.append(request)
        item = self.script.pop(0)
        on_start(item.get("pid", 999999))
        for message in item.get("progress", []):
            if request.on_progress is not None:
                request.on_progress(message)
        wt = Path(request.task.worktree_path)
        for rel, content in item.get("files", {}).items():
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(content)
        if "raise" in item:
            raise item["raise"]
        return ImplementationResult(
            backend=self.name, model=request.choice.model, status=item.get("status", "completed"), exit_code=0,
            summary="done", usage=Usage(input_tokens=item.get("tokens", 1000), output_tokens=100),
            cost_usd=item.get("cost"), pid=item.get("pid"), error=item.get("error"),
        )


def code_review(verdict: str = "pass", invariant: bool = False) -> CodeReviewResult:
    findings = [] if verdict == "pass" else [CodeFinding(aspect="scientific_correctness", severity="major",
                                                         file="h2_energy.py", issue="wrong sign", requested_change="fix")]
    return CodeReviewResult(verdict=verdict, summary=verdict, findings=findings,
                            invariant_violations=["basis must be STO-3G"] if invariant else [],
                            unverified_items=["energy value not checked against reference"])


class FakeReviewer:
    def __init__(self, backend: str, script: list) -> None:
        self.backend = backend
        self.script = list(script)
        self.calls = []

    def __call__(self, task, choice, prompt, schema, log_path):
        self.calls.append((choice, prompt))
        item = self.script.pop(0) if self.script else code_review("pass")
        status = "completed"
        if isinstance(item, str):  # a failure status
            status, item = item, None
        result = ImplementationResult(backend=self.backend, model=choice.model, status=status, exit_code=0,
                                      usage=Usage(input_tokens=500, output_tokens=50))
        return result, item


def phase2_apply(tmp_path: Path, sections: dict | None = None, mode: str = "apply"):
    """Run the real Phase 2 orchestrator (with Phase 2 fakes) to produce a handoff + run record."""
    linear = FakeLinear(make_issue())
    notion = FakeNotion(make_page(sections=SECTIONS if sections is None else sections))
    deps2 = make_deps(tmp_path, notion, linear, [make_plan()], [review("approved")], linear_write_enabled=True)
    rec = plan_task(deps2, task_id="INU-900", mode=mode)
    return deps2, notion, linear, rec


def make_impl_deps(tmp_path: Path, codex: list[dict], claude: list[dict] | None = None, reviewer: list | None = None,
                   claude_reviewer: list | None = None, sections: dict | None = None, mode: str = "apply",
                   **settings_kw):
    repo = make_repo(tmp_path)
    deps2, notion, linear, rec = phase2_apply(tmp_path, sections, mode)
    settings = Phase3Settings(worktrees_dir=tmp_path / "worktrees", **settings_kw)
    deps = ImplDeps(
        settings=settings, phase2=deps2.settings, registry={"h2-test": repo_config(repo)}, store=deps2.store,
        manager=TaskManager(notion, linear, deps2.settings),
        backends={"codex": FakeBackend("codex", codex), "claude": FakeBackend("claude", claude or [])},
        reviewers={"codex": FakeReviewer("codex", reviewer or []), "claude": FakeReviewer("claude", claude_reviewer or [])},
    )
    return deps, repo, rec, notion, linear
