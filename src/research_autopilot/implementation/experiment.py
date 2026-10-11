"""ExperimentSpec: machine-executable experiment definition handed to Phase 4.

The Implementer writes ``.autopilot/experiment.json`` (an ``ExperimentDefinition``)
in the research repository. Phase 3 validates it deterministically against the
*fixed commit* (files exist at that commit, argv is structured and shell-free,
paths are relative, required conditions are present) and combines it with
identification and validation data that come from the task spec (never from the
LLM) into an ``ExperimentSpec``. Phase 4 can start the experiment from the spec
alone, without further LLM reasoning.
"""

import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from research_autopilot.models import SourceRef, utcnow

EXPERIMENT_DEFINITION_PATH = ".autopilot/experiment.json"
SPEC_SCHEMA_VERSION = "1.0"

ALLOWED_LAUNCHERS = {"python", "python3", "uv"}
SHELL_META = re.compile(r"[;&|`$<>\n]")
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SECRET_PATTERNS = re.compile(r"(sk-[A-Za-z0-9_-]{20,}|ntn_[A-Za-z0-9]{20,}|lin_api_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|"
                             r"-----BEGIN [A-Z ]*PRIVATE KEY-----)")
DEPENDENCY_FILES = {"pyproject.toml", "requirements.txt", "uv.lock", "environment.yml", "environment.yaml",
                    "poetry.lock", "Pipfile", "Pipfile.lock", "setup.cfg", "setup.py"}
PYTHON_SELECTOR = re.compile(
    r"^(?P<prefix>==|>=)?(?:python)?(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+|x|\*))?$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- definition (authored in the repo)


class Estimate(BaseModel):
    value: float = Field(ge=0)
    basis: Literal["required", "estimated"]
    rationale: str = Field(min_length=3)
    confidence: Literal["low", "medium", "high"]


class Resources(BaseModel):
    cpu: Estimate
    memory_gb: Estimate
    gpu: Estimate
    walltime_minutes: Estimate
    parallelizable: bool
    parallel_notes: str = ""
    job_dependencies: list[str] = Field(default_factory=list)  # ids of other experiments that must run first


class Execution(BaseModel):
    argv: list[str] = Field(min_length=1)
    working_directory: str = "."
    setup: list[list[str]] = Field(default_factory=list)  # each step is an argv
    env_var_names: list[str] = Field(default_factory=list)  # names only, never values


class Environment(BaseModel):
    type: Literal["python"]
    python_version: str = Field(min_length=1)
    dependency_files: list[str] = Field(min_length=1)
    install_argv: list[str] | None = None  # e.g. ["uv", "sync", "--frozen"]

    @field_validator("python_version", mode="before")
    @classmethod
    def normalize_python_version(cls, value: object) -> str:
        """Canonicalize common version notation to a uv interpreter selector."""
        raw = str(value).strip()
        match = PYTHON_SELECTOR.fullmatch(raw)
        if match is None:
            raise ValueError("must identify one Python minor/patch version (for example 3.12 or 3.12.4)")
        prefix = match.group("prefix")
        patch = match.group("patch")
        if prefix == ">=" and patch is not None:
            raise ValueError("a minimum Python version may not include a patch component")
        if prefix == "==" and patch not in (None, "x", "*"):
            raise ValueError("use a bare exact patch version such as 3.12.4")
        base = f"{match.group('major')}.{match.group('minor')}"
        return f"{base}.{patch}" if patch and patch not in ("x", "*") else base


class InputFile(BaseModel):
    path: str
    kind: Literal["config", "data", "code"]
    scientific: bool  # True = carries scientific parameters (molecule, basis, ...) -> locked during repairs
    description: str = ""


class OutputFile(BaseModel):
    path: str
    format: Literal["json", "csv", "npy", "npz", "hdf5", "txt", "png", "pdf", "other"]
    required: bool
    units: dict[str, str] = Field(default_factory=dict)  # field -> unit, e.g. {"e_fci": "Hartree"}
    description: str = ""
    validation: str = ""  # how to check integrity (e.g. "JSON with keys e_rhf, e_fci")


class Metric(BaseModel):
    name: str
    definition: str
    output: str  # output path
    json_pointer: str | None = None
    units: str | None = None


class NumericCheck(BaseModel):
    name: str
    output: str
    json_pointer: str
    condition: Literal["lt", "le", "gt", "ge", "eq", "abs_le", "finite"]
    value: float | None = None  # literal right-hand side
    reference_pointer: str | None = None  # or another field in the same output
    tolerance: float | None = None


class ExternalCodeUse(BaseModel):
    """Phase 9: run this experiment inside the approved container of a registered external implementation.

    The repository is not copied into the research repository: it is mounted read-only at /ext/<mount_as>
    (on PYTHONPATH) and the research checkout at /work.  Research modifications are patch files committed in
    this repository and listed in ``patches``.  Execution needs a human-approved grant covering purpose,
    network, file access and resources; Phase 4 checks it before every run.
    """

    name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", description="GitHub owner/name of the Registry entry")
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$",
                               description="fixed commit; omit to use the Registry's current verified commit")
    purpose: Literal["baseline_comparison", "reproduction", "component_reuse", "data_generation", "analysis"]
    purpose_detail: str = Field(default="", max_length=500)
    mount_as: str = Field(default="ext", pattern=r"^[a-z][a-z0-9_]{0,31}$")
    network: Literal["none", "unrestricted"] = "none"
    job_checkout: Literal["rw", "ro"] = "rw"
    patches: list[str] = Field(default_factory=list, description="repository-relative *.patch files applied to a copy")


class ExperimentDefinition(BaseModel):
    """Written by the Implementer at ``.autopilot/experiment.json``."""

    experiment_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    execution: Execution
    environment: Environment
    inputs: list[InputFile]
    resources: Resources
    outputs: list[OutputFile] = Field(min_length=1)
    metrics: list[Metric] = Field(default_factory=list)
    numeric_checks: list[NumericCheck] = Field(default_factory=list)
    reproduction: list[str] = Field(min_length=1)  # human-readable steps to reproduce
    external_code: list[ExternalCodeUse] = Field(default_factory=list, max_length=1)

    @field_validator("outputs")
    @classmethod
    def _has_required_output(cls, v: list[OutputFile]) -> list[OutputFile]:
        if not any(o.required for o in v):
            raise ValueError("at least one output must be required")
        return v


# ---------------------------------------------------------------- spec (handed to Phase 4)


class Identification(BaseModel):
    task_id: str
    phase2_plan_run_id: str
    plan_id: str
    phase3_run_id: str
    repository_id: str
    repository_url: str | None  # no local absolute paths in the spec
    branch: str
    commit: str


class Validation(BaseModel):
    acceptance_criteria: list[str]  # verbatim lines from the task spec
    scientific_invariants: list[str]  # verbatim lines from the task spec
    metrics: list[Metric]
    numeric_checks: list[NumericCheck]
    output_integrity: list[str]


class ExperimentSpec(BaseModel):
    schema_version: Literal["1.0"] = SPEC_SCHEMA_VERSION
    spec_id: str
    created_at: datetime = Field(default_factory=utcnow)
    identification: Identification
    experiment_id: str
    execution: Execution
    environment: Environment
    inputs: list[InputFile]
    resources: Resources
    outputs: list[OutputFile]
    validation: Validation
    reproduction: list[str]
    definition_path: str = EXPERIMENT_DEFINITION_PATH
    supersedes_spec_id: str | None = None  # set when produced by a Phase 4 -> 3 repair
    sources: list[SourceRef]
    external_code: list[ExternalCodeUse] = Field(default_factory=list)  # Phase 9

    def locked_scientific_inputs(self) -> list[str]:
        return [i.path for i in self.inputs if i.scientific]


# ---------------------------------------------------------------- validation against a commit


FileExists = Callable[[str], bool]  # path relative to repo root -> exists at the commit (file)
DirExists = Callable[[str], bool]


def _bad_path(p: str) -> str | None:
    if not p or p.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", p):
        return f"absolute path not allowed: {p!r}"
    if ".." in p.replace("\\", "/").split("/"):
        return f"parent-directory reference not allowed: {p!r}"
    if SHELL_META.search(p):
        return f"shell metacharacters in path: {p!r}"
    return None


def _check_argv(argv: list[str], where: str, file_exists: FileExists, workdir: str) -> list[str]:
    errors = []
    if not argv:
        return [f"{where}: empty argv"]
    launcher = argv[0]
    if launcher not in ALLOWED_LAUNCHERS:
        errors.append(f"{where}: launcher {launcher!r} not allowed (use one of {sorted(ALLOWED_LAUNCHERS)})")
    for a in argv:
        if SHELL_META.search(a):
            errors.append(f"{where}: shell metacharacters in argument {a!r}")
        if a.startswith("/") or a.startswith("~"):
            errors.append(f"{where}: absolute path argument {a!r}")
    rest = argv[1:]
    if launcher in ("python", "python3"):
        if "-c" in rest:
            errors.append(f"{where}: 'python -c' (inline code) is not allowed")
        if rest and rest[0] == "-m":
            if len(rest) < 2 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", rest[1]):
                errors.append(f"{where}: invalid module after -m")
        elif rest:
            script = rest[0] if workdir in (".", "") else f"{workdir.rstrip('/')}/{rest[0]}"
            if not file_exists(script):
                errors.append(f"{where}: script {script!r} does not exist at the commit")
        else:
            errors.append(f"{where}: python without a script or -m module")
    if launcher == "uv" and (not rest or rest[0] not in ("run", "sync")):
        errors.append(f"{where}: only 'uv run ...' / 'uv sync ...' are allowed")
    if launcher == "uv" and rest[:1] == ["run"] and any(x in ("sh", "bash", "zsh", "-c") for x in rest):
        errors.append(f"{where}: shells are not allowed under uv run")
    return errors


def validate_definition(d: ExperimentDefinition, file_exists: FileExists, dir_exists: DirExists) -> list[str]:
    errors: list[str] = []
    wd = d.execution.working_directory
    if (e := _bad_path(wd)) is not None:
        errors.append(f"working_directory: {e}")
    elif wd not in (".", "") and not dir_exists(wd):
        errors.append(f"working_directory {wd!r} does not exist at the commit")
    errors += _check_argv(d.execution.argv, "execution.argv", file_exists, wd)
    for i, step in enumerate(d.execution.setup):
        errors += _check_argv(step, f"execution.setup[{i}]", file_exists, wd)
    if d.environment.install_argv:
        errors += [x for x in _check_argv(d.environment.install_argv, "environment.install_argv", file_exists, ".")
                   if "script" not in x]
    for name in d.execution.env_var_names:
        if not ENV_NAME.match(name):
            errors.append(f"env_var_names: invalid name {name!r} (names only, no values)")
    for f in d.environment.dependency_files:
        if (e := _bad_path(f)) is not None:
            errors.append(f"environment.dependency_files: {e}")
        elif f.split("/")[-1] not in DEPENDENCY_FILES:
            errors.append(f"environment.dependency_files: {f!r} is not a recognized dependency file")
        elif not file_exists(f):
            errors.append(f"environment.dependency_files: {f!r} does not exist at the commit")
    for inp in d.inputs:
        if (e := _bad_path(inp.path)) is not None:
            errors.append(f"inputs: {e}")
        elif not file_exists(inp.path):
            errors.append(f"inputs: {inp.path!r} does not exist at the commit")
    out_paths = set()
    for o in d.outputs:
        if (e := _bad_path(o.path)) is not None:
            errors.append(f"outputs: {e}")
        out_paths.add(o.path)
    for m in d.metrics:
        if m.output not in out_paths:
            errors.append(f"metrics: {m.name!r} refers to unknown output {m.output!r}")
    for c in d.numeric_checks:
        if c.output not in out_paths:
            errors.append(f"numeric_checks: {c.name!r} refers to unknown output {c.output!r}")
        if c.condition != "finite" and c.value is None and c.reference_pointer is None:
            errors.append(f"numeric_checks: {c.name!r} needs a value or reference_pointer")
    for dep in d.resources.job_dependencies:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", dep):
            errors.append(f"resources.job_dependencies: invalid id {dep!r}")
    for use in d.external_code:
        if d.execution.argv[0] not in ("python", "python3"):
            errors.append("external_code: execution.argv must use the container's python launcher")
        if d.execution.env_var_names:
            errors.append("external_code: environment variables (credentials) are never passed to external code")
        if d.execution.setup:
            errors.append("external_code: setup steps are not supported inside the external-code container")
        for p in use.patches:
            if (e := _bad_path(p)) is not None:
                errors.append(f"external_code.patches: {e}")
            elif not p.endswith(".patch") or not file_exists(p):
                errors.append(f"external_code.patches: {p!r} must be an existing *.patch file at the commit")
    if SECRET_PATTERNS.search(d.model_dump_json()):
        errors.append("definition contains what looks like a secret value")
    return errors


def parse_definition(text: str | None) -> tuple[ExperimentDefinition | None, list[str]]:
    if text is None:
        return None, [f"{EXPERIMENT_DEFINITION_PATH} is missing"]
    try:
        return ExperimentDefinition.model_validate(json.loads(text)), []
    except ValueError as exc:
        errs = exc.errors() if isinstance(exc, ValidationError) else [{"loc": (), "msg": str(exc)}]
        return None, [f"{EXPERIMENT_DEFINITION_PATH}: {'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errs][:20]


def spec_lines(text: str | None) -> list[str]:
    """Bullet / line list from a spec section, verbatim (markers stripped)."""
    out = []
    for line in (text or "").splitlines():
        item = re.sub(r"^\s*(?:[-*・]|\d+[.)])\s*", "", line).strip()
        if item:
            out.append(item)
    return out


def definition_schema() -> dict:
    return ExperimentDefinition.model_json_schema()
