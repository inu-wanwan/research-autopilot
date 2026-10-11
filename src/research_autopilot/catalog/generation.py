"""Phase 7B: autonomous adapter generation, validation and policy-gated activation.

Flow per repository: detect need -> bounded read-only survey -> AI proposal
(declarative preferred) -> register as a new immutable ``generated`` version ->
automatic validation (safety + behaviour + determinism + read-only + no
regression) with feedback-driven retries -> activation policy:

* declarative: activated automatically after every gate passes, but only when
  it does not displace a human-made adapter and verifies everything the
  currently active generated version verified;
* python: validated in the OS sandbox, then waits for an explicit human
  activation (never imported in-process, never auto-activated).

The catalog core is unchanged: generated adapters enter through the Phase 7A
register / validate / activate path, so Artifact reuse still depends only on
each artifact's own checks.
"""

from __future__ import annotations

import ast
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ValidationError

from research_autopilot.catalog import adapters, declarative, sandbox, validators
from research_autopilot.catalog.models import (AdapterKind, AdapterOrigin, AdapterRecord, AdapterState,
                                               AdapterValidation, Artifact, CheckResult, GenerationAttempt,
                                               GenerationRun, ScanRecord, ValidationStatus, artifact_id_for,
                                               content_hash, stable_hash)
from research_autopilot.catalog.service import CatalogContext, _status
from research_autopilot.catalog.survey import survey as survey_repository
from research_autopilot.models import utcnow
from research_autopilot.storage import db

PROMPT_VERSION = "adapter-generator@1"
MAX_DEFINITION_CHARS = 64_000
MAX_SOURCE_CHARS = 40_000
MAX_RULES = 20
MAX_CHECKS_PER_RULE = 60
DISCOVERY_SECONDS = 300
MAX_ARTIFACTS = 50_000  # an artifact is a reusable research unit, not every file of a cache


# ---------------------------------------------------------------- detection


class Need(BaseModel):
    repository_id: str
    needed: bool
    reason: Literal["unbound", "no_active_adapter", "scan_failed", "no_artifacts", "stale_artifacts", "ok",
                    "repository_unavailable"]
    detail: str = ""
    active_adapters: list[str] = []


def detect(ctx: CatalogContext, repository_id: str) -> Need:
    """Does this registered repository need a (new or updated) adapter on this environment?"""
    root = ctx.root(repository_id)
    if root is None or not root.is_dir():
        return Need(repository_id=repository_id, needed=False, reason="repository_unavailable",
                    detail=f"no readable root on {ctx.environment}")
    if not ctx.store.get(db.catalog_bindings, repository_id):
        return Need(repository_id=repository_id, needed=True, reason="unbound", detail="no adapter is bound")
    active = adapters.active_for(ctx.store, repository_id)
    names = [a.adapter_id for a in active]
    if not active:
        return Need(repository_id=repository_id, needed=True, reason="no_active_adapter",
                    detail="bound adapters exist but none is active")
    scans = [ScanRecord.model_validate(r) for r in ctx.store.find(db.catalog_scans, repository_id=repository_id)]
    last = next((s for s in reversed(scans) if s.environment == ctx.environment), None)
    if last is not None:
        if last.errors:
            return Need(repository_id=repository_id, needed=True, reason="scan_failed", detail=last.errors[0][:300],
                        active_adapters=names)
        if last.discovered == 0:
            return Need(repository_id=repository_id, needed=True, reason="no_artifacts",
                        detail="the active adapters discovered nothing", active_adapters=names)
        stale = last.became_stale
        if stale:
            return Need(repository_id=repository_id, needed=True, reason="stale_artifacts",
                        detail=f"{stale} artifact(s) became stale", active_adapters=names)
    return Need(repository_id=repository_id, needed=False, reason="ok", active_adapters=names)


# ---------------------------------------------------------------- generator


class AdapterProposal(BaseModel):
    kind: Literal["declarative", "python"]
    definition_json: str   # declarative: the full definition as JSON text; "" for python
    python_source: str     # python: module with discover(root); "" for declarative
    rationale: str
    assumptions: list[str]
    unresolved: list[str]


class GeneratorRun(BaseModel):
    output: AdapterProposal
    model: str
    cost_usd: float | None = None


class AdapterGenerator(Protocol):
    model: str

    def generate(self, payload: dict) -> GeneratorRun: ...


INSTRUCTIONS = """\
You write a data adapter that lets Research Autopilot's artifact catalog discover and validate research
artifacts (datasets, caches, results, manifests) in ONE research repository. You only see a bounded survey of the
repository (generalized path shapes with counts, truncated JSON structure samples, README/config excerpts).

Prefer kind="declarative": put a JSON object matching DECLARATIVE_SCHEMA into definition_json. It is data only.
Use kind="python" only if the structure truly cannot be expressed declaratively; then python_source must define
`discover(root: str) -> list[dict]` following PYTHON_CONTRACT (only ALLOWED_IMPORTS; it runs read-only in a
sandbox without network or subprocesses and must never write files) and needs human approval anyway.

Rules for a good adapter:
- One artifact = one research condition / dataset unit. `identity` holds the scientific condition fields;
  `provenance` holds where it came from (run, commit, config hash, software version). Never put provenance into
  identity, and never pick a "canonical" run yourself: different runs must stay separate artifacts.
- Declare deterministic mandatory checks that establish integrity: field/path agreement (equals_field), expected
  constants (equals / one_of), finiteness of numbers, counts, list-element checks, and cross_reference against
  related files when the repository stores the same values twice (e.g. results vs. cache). Every rule needs at
  least one mandatory check; do not add checks you cannot justify from the survey. Never weaken a check to make
  data pass: inconsistent data must stay inconsistent.
- Patterns are repository-relative, one path segment per `{capture}` / `*`; no absolute paths, no "..".
- Use only check types and validator names listed in the schema. Keep it minimal and focused on FOCUS when given.
- If PREVIOUS_ATTEMPT_PROBLEMS is present, fix exactly those problems.
Explain assumptions and anything unresolved; a human may read them.
"""


class OpenAIAdapterGenerator:
    """Agents SDK generator with the same pricing / budget refusal as the other paid agents."""

    def __init__(self, model: str, prices: dict, remaining_budget, tracing: bool = False) -> None:
        from research_autopilot.experiments.analyst import _PricedAgent

        class _Agent(_PricedAgent):
            pass

        self.model = model
        self._agent = _Agent(model, prices, remaining_budget, tracing)

    def generate(self, payload: dict) -> GeneratorRun:
        run = self._agent._call("AdapterGenerator", INSTRUCTIONS, payload, AdapterProposal)
        return GeneratorRun(output=run.output, model=run.model, cost_usd=run.cost_usd)


def generator_payload(repository_id: str, survey: dict, focus: str | None, feedback: list[str],
                      current: AdapterRecord | None, previous: dict | None = None) -> dict:
    return {
        "PROMPT_VERSION": PROMPT_VERSION,
        "REPOSITORY_ID": repository_id,
        "FOCUS": focus,
        "SURVEY": survey,
        "DECLARATIVE_SCHEMA": declarative.DeclarativeDefinition.model_json_schema(),
        "CHECK_VOCABULARY": declarative.CHECK_VOCABULARY,
        "NAMED_VALIDATORS": validators.names(),
        "PYTHON_CONTRACT": sandbox.describe_contract(),
        "CURRENT_ACTIVE_ADAPTER": (current.definition if current and current.kind is AdapterKind.DECLARATIVE
                                   else None),
        "PREVIOUS_ATTEMPT_PROBLEMS": feedback,
        "PREVIOUS_ATTEMPT_PROPOSAL": previous,
    }


# ---------------------------------------------------------------- automatic validation


@dataclass
class AutoValidation:
    passed: bool
    checks: list[CheckResult]
    verified_ids: set[str]
    discovered: int


def _check(name: str, passed: bool, detail: str = "") -> CheckResult:
    return CheckResult(name=name, passed=passed, detail=detail[:800], validator=PROMPT_VERSION)


def python_source_problems(source: str) -> list[str]:
    """Static gate (defence in depth; the sandbox is the real boundary)."""
    problems = []
    if len(source) > MAX_SOURCE_CHARS:
        problems.append(f"source longer than {MAX_SOURCE_CHARS} characters")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: {exc}"]
    banned_calls = {"eval", "exec", "compile", "__import__", "open_code", "breakpoint", "input"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in sandbox.ALLOWED_IMPORTS:
                    problems.append(f"import {a.name} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "") not in sandbox.ALLOWED_IMPORTS or node.level:
                problems.append(f"from {node.module} import is not allowed")
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if name in banned_calls:
                problems.append(f"call to {name}() is not allowed")
            if name == "open" and any(isinstance(a, ast.Constant) and isinstance(a.value, str) and
                                      set(a.value) & set("wax+") for a in [*node.args[1:2],
                                                                           *[k.value for k in node.keywords
                                                                             if k.arg == "mode"]]):
                problems.append("open() in a write mode is not allowed")
            if isinstance(fn, ast.Attribute) and fn.attr in {"system", "popen", "remove", "unlink", "rmdir",
                                                            "rename", "replace", "write_text", "write_bytes",
                                                            "mkdir", "makedirs", "chmod", "symlink_to", "touch"}:
                problems.append(f"call to .{fn.attr}() is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr not in {"__name__"}:
            problems.append(f"dunder attribute access {node.attr} is not allowed")
    if not any(isinstance(n, ast.FunctionDef) and n.name == "discover" for n in tree.body):
        problems.append("module must define discover(root)")
    return sorted(set(problems))


def _repo_fingerprint(root: Path) -> str | None:
    """Git HEAD + porcelain status; None when the root is not a git checkout."""
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=30, stdin=subprocess.DEVNULL)
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
                                capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if head.returncode != 0 or status.returncode != 0:
        return None
    return stable_hash([head.stdout.strip(), status.stdout])


def _listing(root: Path) -> str:
    """Fallback read-only witness for non-git roots: path/size/mtime of every file."""
    rows = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            st = p.stat()
            rows.append((p.relative_to(root).as_posix(), st.st_size, st.st_mtime_ns))
    return stable_hash(rows)


def autovalidate(ctx: CatalogContext, rec: AdapterRecord, repository_id: str, root: Path) -> AutoValidation:
    """Every gate a generated adapter must pass before it can be used (or offered to a human)."""
    checks: list[CheckResult] = []
    groups_runs: list[list] = []
    try:
        if rec.kind is AdapterKind.DECLARATIVE:
            raw = json.dumps(rec.definition or {})
            parsed = declarative.parse(rec.definition or {})  # confinement, vocabulary, validator names
            limits = []
            if len(raw) > MAX_DEFINITION_CHARS:
                limits.append(f"definition longer than {MAX_DEFINITION_CHARS}")
            if len(parsed.artifacts) > MAX_RULES:
                limits.append(f"more than {MAX_RULES} rules")
            limits += [f"rule {r.rule} has more than {MAX_CHECKS_PER_RULE} checks" for r in parsed.artifacts
                       if len(r.checks) > MAX_CHECKS_PER_RULE]
            limits += [f"rule {r.rule} declares no mandatory check" for r in parsed.artifacts
                       if not any(c.mandatory for c in r.checks)]
            checks.append(_check("definition_safety", not limits, "; ".join(limits) or
                                 "data-only definition; repository-confined patterns; known check vocabulary"))
        else:
            problems = python_source_problems(rec.python_source or "")
            checks.append(_check("python_static_safety", not problems, "; ".join(problems)))
            if not sandbox.available():
                checks.append(_check("sandbox_available", False, "no OS sandbox on this host; not executed"))
    except (ValidationError, ValueError) as exc:
        checks.append(_check("definition_safety", False, str(exc)))
    if not all(c.passed for c in checks):
        return AutoValidation(False, checks, set(), 0)

    root = root.expanduser().resolve()
    before = _repo_fingerprint(root) or _listing(root)
    runtime = adapters.runtime(rec)
    elapsed = []
    for _ in range(2):  # determinism: two independent discoveries must agree exactly
        t0 = time.monotonic()
        try:
            groups, errors = runtime.discover(root)
        except Exception as exc:  # generated code / definitions must never crash the caller
            groups, errors = [], [f"{type(exc).__name__}: {exc}"]
        elapsed.append(time.monotonic() - t0)
        groups_runs.append((groups, errors))
    after = _repo_fingerprint(root) or _listing(root)
    (groups, errors), (groups2, _) = groups_runs
    ids = [_artifact_id(repository_id, g) for g in groups]
    ids2 = [_artifact_id(repository_id, g) for g in groups2]
    checks.append(_check("discovery_errors", not errors, "; ".join(errors[:5])))
    checks.append(_check("discovers_artifacts", bool(groups), f"{len(groups)} artifact(s)"))
    checks.append(_check("deterministic", sorted(ids) == sorted(ids2), "two discoveries returned different artifacts"
                         if sorted(ids) != sorted(ids2) else ""))
    checks.append(_check("repository_unchanged", before == after, "" if before == after
                         else "repository content changed during validation"))
    checks.append(_check("time_budget", max(elapsed) <= DISCOVERY_SECONDS, f"{max(elapsed):.1f}s"))
    checks.append(_check("artifact_granularity", len(groups) <= MAX_ARTIFACTS,
                         f"{len(groups)} artifacts" + ("" if len(groups) <= MAX_ARTIFACTS else
                                                       f" (> {MAX_ARTIFACTS}); group member files into research "
                                                       "conditions (identity) instead of one artifact per file")))
    unique = len(set(ids)) == len(ids)
    checks.append(_check("unique_identities", unique, "" if unique else "two groups produced the same artifact"))
    statuses = [_status(g.checks)[0] for g in groups]
    verified = {i for i, st in zip(ids, statuses) if st is ValidationStatus.VERIFIED}
    if rec.kind is AdapterKind.DECLARATIVE and (not groups or errors or not verified):
        checks.append(_check("diagnostics", True, " | ".join(
            declarative.diagnose(declarative.parse(rec.definition or {}), root))))
    checks.append(_check("verifies_some_artifacts", bool(verified),
                         f"{len(verified)}/{len(groups)} verified; " + _failure_summary(groups)))
    return AutoValidation(all(c.passed for c in checks), checks, verified, len(groups))


def _artifact_id(repository_id: str, g) -> str:
    return artifact_id_for(repository_id, g.kind, g.identity, content_hash(g.members))


def _failure_summary(groups) -> str:
    """Failing mandatory checks with one concrete example each (what the generator needs to fix them)."""
    counts: dict[str, int] = {}
    example: dict[str, str] = {}
    for g in groups:
        for c in g.checks:
            if c.mandatory and not c.passed:
                counts[c.name] = counts.get(c.name, 0) + 1
                if c.name not in example:
                    where = next(iter(g.members), "")
                    example[c.name] = f"{c.detail[:200]} in {where.rsplit('/', 1)[-1]}" if c.detail else where
    return "; ".join(f"{k} failed on {v} (e.g. {example[k]})"
                     for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:8])


def _previously_verified(ctx: CatalogContext, repository_id: str, adapter_ids: set[str]) -> set[str]:
    out = set()
    for raw in ctx.store.find(db.catalog_artifacts, repository_id=repository_id, status=ValidationStatus.VERIFIED.value):
        art = Artifact.model_validate(raw)
        if art.provenance.adapter_id in adapter_ids:
            out.add(art.artifact_id)
    return out


# ---------------------------------------------------------------- orchestration


def generated_name(repository_id: str) -> str:
    return f"{repository_id}.generated"


def _next_version(ctx: CatalogContext, name: str) -> str:
    n = len(ctx.store.find(db.catalog_adapters, name=name))
    return f"0.{n + 1}.0"


def generate_adapter(ctx: CatalogContext, repository_id: str, generator: AdapterGenerator, *,
                     focus: str | None = None, max_attempts: int = 3, force: bool = False,
                     notifier=None) -> GenerationRun:
    """Generate, validate and (policy permitting) activate an adapter for one registered repository."""
    store = ctx.store
    store.init_schema()
    need = detect(ctx, repository_id)
    run = GenerationRun(repository_id=repository_id, environment=ctx.environment,
                        reason=f"{need.reason}: {need.detail}".strip(": "), model=generator.model)
    if not need.needed and not force:
        run.outcome, run.note = "skipped", f"no generation needed ({need.reason})"
        return _finish(ctx, run, notifier)
    root = ctx.root(repository_id)
    if root is None or not root.is_dir():
        run.outcome, run.note = "failed", "repository root unavailable"
        return _finish(ctx, run, notifier)
    active = adapters.active_for(store, repository_id)
    human_active = [a for a in active if a.origin is not AdapterOrigin.GENERATED]
    current_generated = next((a for a in active if a.origin is AdapterOrigin.GENERATED), None)
    survey = survey_repository(root, focus)
    run.survey_hash = stable_hash(survey)
    name = generated_name(repository_id)
    feedback: list[str] = []
    previous: dict | None = None
    for attempt in range(1, max_attempts + 1):
        att = GenerationAttempt(attempt=attempt)
        run.attempts.append(att)
        try:
            out = generator.generate(generator_payload(repository_id, survey, focus, feedback,
                                                       current_generated or (human_active[0] if human_active else None),
                                                       previous))
        except Exception as exc:  # budget refusal, transport, invalid structured output
            att.problems = [f"generator failed: {type(exc).__name__}: {exc}"[:500]]
            run.outcome, run.note = "failed", att.problems[0]
            break
        att.cost_usd = out.cost_usd
        run.total_cost_usd += out.cost_usd or 0.0
        p = out.output
        previous = {"kind": p.kind, "definition_json": p.definition_json[:MAX_DEFINITION_CHARS],
                    "python_source": p.python_source[:MAX_SOURCE_CHARS]}
        att.kind, att.rationale = p.kind, p.rationale[:2000]
        att.assumptions, att.unresolved = p.assumptions[:20], p.unresolved[:20]
        meta = {"model": out.model, "prompt_version": PROMPT_VERSION, "survey_hash": run.survey_hash,
                "generation_id": run.generation_id, "attempt": attempt, "focus": focus,
                "rationale": att.rationale, "assumptions": att.assumptions, "unresolved": att.unresolved}
        try:
            rec = _register(ctx, name, p, meta)
        except (adapters.AdapterError, ValueError) as exc:
            att.problems = [f"proposal rejected before validation: {exc}"[:800]]
            feedback = _accumulate(feedback, att.problems)
            continue
        att.adapter_id = rec.adapter_id
        result = autovalidate(ctx, rec, repository_id, root)
        _record_validation(ctx, rec, result, repository_id)
        att.passed = result.passed
        att.problems = [f"{c.name}: {c.detail}" for c in result.checks
                        if not c.passed or (c.name == "diagnostics" and not result.passed)]
        if not result.passed:
            feedback = _accumulate(feedback, att.problems)
            continue
        run.adapter_id = rec.adapter_id
        _decide_activation(ctx, run, rec, result, repository_id, human_active, current_generated)
        break
    else:
        run.outcome = "rejected"
        run.note = f"no proposal passed validation in {max_attempts} attempt(s); active adapters unchanged"
    return _finish(ctx, run, notifier)


def _accumulate(feedback: list[str], new: list[str]) -> list[str]:
    """Latest problems first, earlier ones kept so a fixed mistake is not reintroduced."""
    out = [f"(latest attempt) {p}" for p in new]
    out += [p if p.startswith("(earlier") else f"(earlier attempt) {p.removeprefix('(latest attempt) ')}"
            for p in feedback]
    seen, dedup = set(), []
    for p in out:
        key = p.split(") ", 1)[-1]
        if key not in seen:
            seen.add(key)
            dedup.append(p)
    return dedup[:30]


def _register(ctx: CatalogContext, name: str, p: AdapterProposal, meta: dict) -> AdapterRecord:
    version = _next_version(ctx, name)
    if p.kind == "declarative":
        definition = json.loads(p.definition_json)
        if not isinstance(definition, dict):
            raise ValueError("definition_json must be a JSON object")
        definition = {**definition, "name": name, "version": version}  # identity is assigned, not proposed
        return adapters.register(ctx.store, kind=AdapterKind.DECLARATIVE, origin=AdapterOrigin.GENERATED,
                                 actor=f"generator:{meta['model']}", definition=definition, generator=meta)
    return adapters.register(ctx.store, kind=AdapterKind.PYTHON, origin=AdapterOrigin.GENERATED,
                             actor=f"generator:{meta['model']}", python_source=p.python_source, name=name,
                             version=version, generator=meta)


def _record_validation(ctx: CatalogContext, rec: AdapterRecord, result: AutoValidation, repository_id: str) -> None:
    rec = adapters.get(ctx.store, rec.adapter_id)
    rec.validation = AdapterValidation(passed=result.passed, checks=result.checks, repository_id=repository_id,
                                       discovered=result.discovered)
    rec.state = AdapterState.VALIDATED if result.passed else AdapterState.REJECTED
    rec.event("validated" if result.passed else "rejected", "autovalidate",
              f"{result.discovered} artifact(s), {len(result.verified_ids)} verified")
    adapters._save(ctx.store, rec)


def _decide_activation(ctx, run: GenerationRun, rec: AdapterRecord, result: AutoValidation, repository_id: str,
                       human_active: list[AdapterRecord], current_generated: AdapterRecord | None) -> None:
    if adapters.requires_human(rec):
        run.outcome = "awaiting_human"
        run.note = "python adapter validated in the sandbox; explicit human activation required"
        return
    if human_active:
        run.outcome = "awaiting_human"
        run.note = (f"a human-made adapter is active ({', '.join(a.adapter_id for a in human_active)}); "
                    "a generated one never replaces it automatically")
        return
    if current_generated is not None:
        lost = _previously_verified(ctx, repository_id, {current_generated.adapter_id}) - result.verified_ids
        if lost:
            run.outcome = "awaiting_human"
            run.note = (f"would stop verifying {len(lost)} artifact(s) verified by {current_generated.adapter_id}; "
                        "regression needs human review")
            return
    adapters.activate(ctx.store, rec.adapter_id, actor=adapters.AUTOPILOT_ACTOR)
    raw = ctx.store.get(db.catalog_bindings, repository_id)
    names = set(raw.get("adapter_ids", [])) if raw else set()
    adapters.bind(ctx.store, repository_id, sorted(names | {rec.name}), actor=adapters.AUTOPILOT_ACTOR,
                  registry=ctx.registry)
    run.outcome = "activated"
    run.note = f"declarative adapter auto-activated after validation ({len(result.verified_ids)} verified)"


def _finish(ctx: CatalogContext, run: GenerationRun, notifier) -> GenerationRun:
    run.finished_at = utcnow()
    ctx.store.put(db.catalog_generations, run.generation_id, run.model_dump(mode="json"),
                  repository_id=run.repository_id)
    if run.outcome in ("activated", "awaiting_human", "rejected", "failed"):
        _notify(ctx, run, notifier)
    return run


def _notify(ctx: CatalogContext, run: GenerationRun, notifier) -> None:
    from research_autopilot.notifications import notify_once
    from research_autopilot.notifications.events import EventKind, NotificationEvent

    kind = {"activated": EventKind.PROPOSAL_GENERATED, "awaiting_human": EventKind.APPROVAL_REQUIRED}.get(
        run.outcome, EventKind.UNRECOVERABLE_ERROR)
    details = [f"Repository: {run.repository_id} ({run.environment})", f"Outcome: {run.outcome}", run.note[:300],
               f"Cost: ${run.total_cost_usd:.4f}"]
    if run.outcome == "awaiting_human" and run.adapter_id:
        details.append(f"Review, then: research-agent catalog adapter activate {run.adapter_id} --actor <you>")
    try:
        notify_once(ctx.store, NotificationEvent(kind=kind, flow_name="catalog-adapter-generation",
                                                 flow_run_name=run.repository_id, details=details,
                                                 dedupe_key=f"adapter-generation:{run.generation_id}"),
                    notifier=notifier)
    except Exception:  # notification failure never changes the outcome
        pass


def history(store, repository_id: str) -> list[GenerationRun]:
    return [GenerationRun.model_validate(r) for r in store.find(db.catalog_generations, repository_id=repository_id)]

