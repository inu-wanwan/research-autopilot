"""``research-agent literature ...`` (Phase 8)."""

import json
from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, help="Phase 8 evidence-grounded literature research")
zotero_app = typer.Typer(no_args_is_help=True, help="Zotero Connector automation (ported PoC)")
app.add_typer(zotero_app, name="zotero")


def _out(v) -> None:
    typer.echo(json.dumps(v, ensure_ascii=False, indent=2, default=str))


def _deps(note: bool):
    from research_autopilot.autopilot.settings import load_phase5_settings
    from research_autopilot.catalog.integration import context_from_env
    from research_autopilot.config import load_settings
    from research_autopilot.literature.researcher import deps_from_env
    from research_autopilot.storage.db import Store
    s = load_settings()
    store = Store(s.resolved_database_url())
    store.init_schema()
    writer = None
    if note and s.notion_api_token:
        from research_autopilot.tasks.notion_writer import NotionWriter
        writer = NotionWriter(s.notion_api_token.get_secret_value(), s.notion_tasks_data_source_id,
                              load_phase5_settings().notes_data_source_id)
    return deps_from_env(context_from_env(store), writer)


@app.command("research")
def research_cmd(question: str, seed: list[str] = typer.Option(None, "--seed", help="DOI / arXiv id / GitHub owner/name"),
                 code_query: str | None = typer.Option(None, "--code-query"), task: str | None = typer.Option(None, "--task"),
                 note: bool = typer.Option(True, "--note/--no-note", help="Create a Notion Research Note (分類=文献)"),
                 reuse: bool = typer.Option(True, "--reuse/--no-reuse")) -> None:
    """Search, retrieve, analyse and store a literature report (the same path the Planner uses)."""
    from research_autopilot.literature.researcher import research
    r = research(_deps(note), question, task_id=task, seeds=seed or [], code_query=code_query, reuse=reuse)
    _out({"report_id": r.report_id, "sources": [(s.source_id, s.source_type.value, s.title, s.retrieval.method,
                                                 s.retrieval.status) for s in r.sources],
          "claims": len(r.claims), "verified": len(r.verified_claims), "conflicts": len(r.conflicts),
          "failures": [f.model_dump() for f in r.failures], "cost_usd": r.total_cost_usd,
          "stages": [(x.stage, x.detail) for x in r.stages]})


@app.command("show")
def show(report_id: str) -> None:
    from research_autopilot.literature.store import store_dir
    _out(json.loads((store_dir() / "reports" / f"{report_id}.json").read_text()))


@app.command("search")
def search_cmd(query: str, code_query: str | None = typer.Option(None, "--code-query")) -> None:
    """Discovery only (no LLM, no downloads)."""
    from research_autopilot.literature import net, search
    cands, fails = search.discover(net.from_env(), query, code_query=code_query)
    _out({"candidates": [c.model_dump(exclude={"abstract"}) for c in cands], "failures": [f.model_dump() for f in fails]})


@zotero_app.command("check")
def zotero_check() -> None:
    """Preflight: Zotero running, Local API on, expected collection selected, Connector unpacked."""
    from research_autopilot.literature.zotero.session import ZoteroNotReady, ZoteroSession
    try:
        _out({"ok": True, **ZoteroSession().preflight()})
    except ZoteroNotReady as exc:
        _out({"ok": False, "error": str(exc)})
        raise typer.Exit(1)


@zotero_app.command("fetch-connector")
def fetch_connector(dest: Path | None = typer.Option(None, "--dest")) -> None:
    """Download and unpack the Zotero Connector (pin it: the save trigger uses an internal API)."""
    from research_autopilot.literature.zotero.config import Config
    from research_autopilot.literature.zotero.extension import fetch_connector as fetch
    target = dest or Config.load().extension_dir
    _out({"dest": str(target), "version": fetch(target).get("version")})
