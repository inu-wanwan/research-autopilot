"""``research-agent catalog ...``: adapter lifecycle, scans and resolution (Phase 7A)."""

import collections
import json
from pathlib import Path

import typer

from research_autopilot.catalog import adapters, service
from research_autopilot.catalog.models import AdapterKind, AdapterOrigin, Artifact

app = typer.Typer(no_args_is_help=True, help="Phase 7A research artifact catalog")
adapter_app = typer.Typer(no_args_is_help=True, help="Adapter lifecycle: register -> validate -> activate")
app.add_typer(adapter_app, name="adapter")


def _ctx() -> service.CatalogContext:
    from research_autopilot.catalog.integration import context_from_env
    from research_autopilot.config import load_settings
    from research_autopilot.storage.db import Store

    store = Store(load_settings().resolved_database_url())
    store.init_schema()
    return context_from_env(store)


def _out(value) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _fail(exc: Exception) -> None:
    typer.echo(f"error: {exc}", err=True)
    raise typer.Exit(1)


@adapter_app.command("register")
def adapter_register(definition: Path | None = typer.Argument(None, help="Declarative adapter JSON"),
                     actor: str = typer.Option(..., "--actor", help="Who registers it (recorded)"),
                     entrypoint: str | None = typer.Option(None, "--python", help="module:factory (trusted package)"),
                     name: str | None = typer.Option(None, "--name"), version: str | None = typer.Option(None, "--version")
                     ) -> None:
    """Store a new immutable adapter version (state: registered; not usable yet)."""
    ctx = _ctx()
    try:
        if entrypoint:
            rec = adapters.register(ctx.store, kind=AdapterKind.PYTHON, origin=AdapterOrigin.MANUAL, actor=actor,
                                    entrypoint=entrypoint, name=name, version=version)
        else:
            if definition is None:
                raise adapters.AdapterError("give a declarative JSON file or --python module:factory")
            rec = adapters.register(ctx.store, kind=AdapterKind.DECLARATIVE, origin=AdapterOrigin.MANUAL, actor=actor,
                                    definition=json.loads(definition.read_text()))
    except (adapters.AdapterError, ValueError) as exc:
        _fail(exc)
    _out({"adapter_id": rec.adapter_id, "state": rec.state.value, "definition_hash": rec.definition_hash})


@adapter_app.command("validate")
def adapter_validate(adapter_id: str, repository: str = typer.Option(..., "--repository"),
                     actor: str = typer.Option(..., "--actor")) -> None:
    """Static checks + read-only dry discovery against a registered repository."""
    ctx = _ctx()
    root = ctx.root(repository)
    if root is None:
        _fail(adapters.AdapterError(f"repository {repository!r} has no root on {ctx.environment}"))
    try:
        rec = adapters.validate(ctx.store, adapter_id, repository_id=repository, root=root, actor=actor)
    except adapters.AdapterError as exc:
        _fail(exc)
    _out({"adapter_id": rec.adapter_id, "state": rec.state.value,
          "checks": [c.model_dump() for c in rec.validation.checks]})


@adapter_app.command("activate")
def adapter_activate(adapter_id: str, actor: str = typer.Option(..., "--actor", help="Human approver name")) -> None:
    """Make a validated adapter usable (explicit human step; retires the previously active version)."""
    try:
        rec = adapters.activate(_ctx().store, adapter_id, actor=actor)
    except adapters.AdapterError as exc:
        _fail(exc)
    _out({"adapter_id": rec.adapter_id, "state": rec.state.value, "activated_by": rec.activated_by})


@adapter_app.command("retire")
def adapter_retire(adapter_id: str, actor: str = typer.Option(..., "--actor"),
                   reason: str = typer.Option(..., "--reason")) -> None:
    rec = adapters.retire(_ctx().store, adapter_id, actor=actor, reason=reason)
    _out({"adapter_id": rec.adapter_id, "state": rec.state.value})


@adapter_app.command("list")
def adapter_list() -> None:
    from research_autopilot.storage import db
    _out([{k: r[k] for k in ("adapter_id", "kind", "origin", "state", "definition_hash", "activated_by")}
          for r in _ctx().store.find(db.catalog_adapters)])


@app.command("bind")
def bind(repository: str, adapter_names: list[str] = typer.Argument(..., help="Adapter names (not versions)"),
         actor: str = typer.Option(..., "--actor")) -> None:
    """Associate a registered repository with adapter names; the active version of each is used."""
    ctx = _ctx()
    try:
        b = adapters.bind(ctx.store, repository, adapter_names, actor=actor, registry=ctx.registry)
    except adapters.AdapterError as exc:
        _fail(exc)
    _out(b.model_dump(mode="json"))


@app.command("scan")
def scan(repository: str) -> None:
    """Read-only discovery + validation of one repository on this environment (RA_CATALOG_ENVIRONMENT)."""
    rec = service.scan(_ctx(), repository)
    _out(rec.model_dump(mode="json"))
    if rec.errors:
        raise typer.Exit(1)


@app.command("list")
def list_artifacts(kind: str | None = typer.Option(None, "--kind"), repository: str | None = typer.Option(None, "--repository"),
                   status: str | None = typer.Option(None, "--status"), summary: bool = typer.Option(False, "--summary")
                   ) -> None:
    from research_autopilot.storage import db
    where = {k: v for k, v in (("kind", kind), ("repository_id", repository), ("status", status)) if v}
    arts = [Artifact.model_validate(r) for r in _ctx().store.find(db.catalog_artifacts, **where)]
    if summary:
        counts = collections.Counter((a.kind, a.provenance.repository_id, a.status.value,
                                      json.dumps(a.provenance.source, sort_keys=True)[:80]) for a in arts)
        _out([{"kind": k, "repository": r, "status": s, "provenance": p, "count": n}
              for (k, r, s, p), n in sorted(counts.items())])
        return
    _out([{"artifact_id": a.artifact_id, "kind": a.kind, "identity": a.identity, "status": a.status.value,
           "environments": [loc.environment for loc in a.locations]} for a in arts])


@app.command("show")
def show(artifact_id: str) -> None:
    art = service.get_artifact(_ctx().store, artifact_id)
    if art is None:
        _fail(KeyError(artifact_id))
    data = art.model_dump(mode="json")
    data["members_digest"] = f"{len(art.members_digest)} member(s)"
    _out(data)


@app.command("resolve")
def resolve(requirement: list[str] = typer.Argument(..., help='e.g. "catalog: kind=... k=2..10 expect=81"'),
            verify: bool = typer.Option(True, "--verify/--no-verify", help="Re-hash members on this environment")
            ) -> None:
    """What a task would receive: verified, unambiguous artifacts on this environment, or the blocking problems."""
    try:
        queries = service.parse_requirements("\n".join(requirement))
    except ValueError as exc:
        _fail(exc)
    res = service.resolve(_ctx(), queries, verify=verify)
    _out({"environment": res.environment, "resolved": len(res.artifacts), "problems": res.problems,
          "artifacts": [{"artifact_id": a.artifact_id, "identity": a.identity, "path": a.path}
                        for a in res.artifacts]})
    if res.problems:
        raise typer.Exit(1)


# ---------------------------------------------------------------- Phase 7B


@app.command("detect")
def detect(repository: list[str] = typer.Argument(None, help="Repository ids (default: every registered one)")) -> None:
    """Which registered repositories need a (new or updated) adapter on this environment."""
    from research_autopilot.catalog import generation
    ctx = _ctx()
    _out([generation.detect(ctx, r).model_dump() for r in (repository or sorted(ctx.registry))])


def build_generator(budget_usd: float):
    """Paid generator with the shared pricing table; refuses models without a configured price."""
    import os

    from research_autopilot.catalog.generation import OpenAIAdapterGenerator
    from research_autopilot.config import load_settings

    s = load_settings()
    model = os.environ.get("RA_ADAPTER_GENERATOR_MODEL", "").strip() or s.reviewer_model
    spent = {"usd": 0.0}
    gen = OpenAIAdapterGenerator(model, s.model_pricing, lambda: budget_usd - spent["usd"], s.agents_tracing)
    original = gen.generate

    def tracked(payload):
        run = original(payload)
        spent["usd"] += run.cost_usd or 0.0
        return run
    gen.generate = tracked
    return gen


@app.command("generate")
def generate(repository: str,
             focus: str | None = typer.Option(None, "--focus", help="What artifacts matter (ranks the survey)"),
             attempts: int = typer.Option(3, "--attempts", min=1, max=5),
             budget_usd: float = typer.Option(None, "--budget-usd", help="Default RA_ADAPTER_GENERATION_BUDGET_USD or 1.0"),
             force: bool = typer.Option(False, "--force", help="Generate even when detection says it is not needed")
             ) -> None:
    """AI-generate an adapter, validate it, and activate it when policy allows (declarative only)."""
    import os

    from research_autopilot.catalog import generation
    budget = budget_usd if budget_usd is not None else float(os.environ.get("RA_ADAPTER_GENERATION_BUDGET_USD") or 1.0)
    run = generation.generate_adapter(_ctx(), repository, build_generator(budget), focus=focus,
                                      max_attempts=attempts, force=force)
    _out(run.model_dump(mode="json"))
    if run.outcome in ("rejected", "failed"):
        raise typer.Exit(1)


@app.command("generations")
def generations(repository: str) -> None:
    """Generation history (attempts, validation problems, outcome, cost) for a repository."""
    from research_autopilot.catalog import generation
    _out([g.model_dump(mode="json") for g in generation.history(_ctx().store, repository)])
