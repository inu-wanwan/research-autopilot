"""Researcher Agent: question -> sources -> originals -> verified claims -> Research Artifact (+ Notion note).

Every stage is recorded with timing and cost; failures are kept and the run continues with what is reachable.
The report only informs planning: it never edits a task's scientific specification.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from research_autopilot.literature import acquire, analysis, search
from research_autopilot.literature import store as lit_store
from research_autopilot.literature.models import (Candidate, Failure, LiteratureReport, Retrieval, Source,
                                                  SourceType, StageRecord)
from research_autopilot.literature.text import pdf_pages, repo_files
from research_autopilot.models import utcnow


class BudgetExceeded(Exception):
    pass


class PricedLlm:
    """Callable used by ``analysis``: one priced Agents SDK call per invocation, with a run budget."""

    def __init__(self, model: str, prices: dict, budget_usd: float, tracing: bool = False) -> None:
        from research_autopilot.experiments.analyst import _PricedAgent
        self.model, self.budget_usd, self.spent = model, budget_usd, 0.0
        self._agent = _PricedAgent(model, prices, lambda: self.budget_usd - self.spent, tracing)

    def __call__(self, name: str, instructions: str, payload: dict, out_type):
        if self.spent >= self.budget_usd:
            raise BudgetExceeded(f"literature budget ${self.budget_usd:.2f} exhausted")
        run = self._agent._call(name, instructions, payload, out_type)
        self.spent += run.cost_usd or 0.0
        return run.output


@dataclass
class ResearcherDeps:
    net: Any
    llm: Any                      # PricedLlm-like callable with .spent and .model
    catalog: Any                  # catalog.service.CatalogContext
    zotero_factory: Any = None    # () -> ZoteroSession; None disables Zotero (OA download still works)
    notes_writer: Any = None      # NotionWriter-like with create_note(title, blocks, category=...)
    max_sources: int = 4
    max_retrievals: int = acquire.MAX_RETRIEVALS  # automatic downloads per run, capped at acquire.MAX_RETRIEVALS
    reuse_days: float = 30.0
    extra: dict = field(default_factory=dict)


def research(deps: ResearcherDeps, question: str, *, task_id: str | None = None, seeds: list[str] = (),
             code_query: str | None = None, reuse: bool = True, write_note: bool = True) -> LiteratureReport:
    key = lit_store.topic_key(question, list(seeds))
    if reuse:
        old = lit_store.reusable_report(deps.catalog, key, deps.reuse_days)
        if old is not None:
            old.reused = True
            return old
    report = LiteratureReport(question=question, task_id=task_id, topic_key=key, models=[deps.llm.model])

    def stage(name: str, detail: str = "") -> StageRecord:
        rec = StageRecord(stage=name, detail=detail)
        report.stages.append(rec)
        return rec

    def close(rec: StageRecord, detail: str, before: float) -> None:
        rec.finished_at, rec.detail, rec.cost_usd = utcnow(), detail, round(deps.llm.spent - before, 6)

    repos: dict[str, str] = {}
    zotero = None
    limit = acquire.RetrievalLimit(deps.max_retrievals)
    try:
        # 1. discovery
        rec, c0 = stage("search"), deps.llm.spent
        cands, fails = search.discover(deps.net, question, seeds=list(seeds), code_query=code_query)
        report.failures += fails
        close(rec, f"{len(cands)} candidate(s) from {sorted({s for c in cands for s in c.found_by})}", c0)
        if not cands:
            report.unresolved.append("no source could be discovered for the question")
            return _finish(deps, report, repos, write_note)

        # 2. selection (seeds always kept)
        rec, c0 = stage("selection"), deps.llm.spent
        chosen = _select(deps, question, cands, list(seeds), report)
        close(rec, f"{len(chosen)} selected", c0)

        # 3-6. acquisition, text, extraction, verification per source
        for n, (cand, sel) in enumerate(chosen, 1):
            src = _source(cand, sel, n)
            report.sources.append(src)
            rec, c0 = stage("acquisition", src.source_id), deps.llm.spent
            if cand.repo_full_name:
                repos[src.source_id] = cand.repo_full_name
                if not _reuse_source(deps, src, repo=cand.repo_full_name):
                    report.failures += acquire.snapshot_repository(deps.net, src, cand.repo_full_name, limit)
            elif not _reuse_source(deps, src, doi=cand.doi, arxiv_id=cand.arxiv_id):
                if zotero is None and deps.zotero_factory is not None:
                    zotero = deps.zotero_factory()
                report.failures += acquire.acquire_paper(deps.net, src, cand, zotero=zotero, limit=limit)
            close(rec, f"{src.source_id}: {src.retrieval.method} {src.retrieval.status}", c0)
            if src.retrieval.status != "ok":
                report.unresolved.append(f"{src.title}: original not retrieved ({src.retrieval.detail[:160]})")
                continue
            try:
                pages = files = None
                if cand.repo_full_name:
                    files = repo_files(Path(src.retrieval.local_path))
                else:
                    pages = pdf_pages(Path(src.retrieval.local_path), src.retrieval.sha256)
                    src.retrieval.pages = len(pages)
                    if not any(p.strip() for p in pages):
                        raise ValueError("PDF has no extractable text (scanned?)")
            except Exception as exc:
                report.failures.append(Failure(stage="text_extraction", target=src.source_id, detail=str(exc)[:300]))
                continue
            rec, c0 = stage("analysis", src.source_id), deps.llm.spent
            try:
                text = analysis.source_text(src, pages, files)
                extracted = analysis.extract(deps.llm, question, src, text)
                claims = analysis.verify(src, extracted, pages, files)
                report.claims += claims
                report.unresolved += [f"{src.title}: does not address {x}" for x in extracted.not_found[:5]]
                close(rec, f"{len(claims)} claim(s), {sum(c.status == 'verified' for c in claims)} verified", c0)
            except BudgetExceeded:
                raise
            except Exception as exc:
                report.failures.append(Failure(stage="analysis", target=src.source_id,
                                               detail=f"{type(exc).__name__}: {exc}"[:300]))
                close(rec, "failed", c0)
        # 7. synthesis
        if report.claims:
            rec, c0 = stage("synthesis"), deps.llm.spent
            report.summary, report.conflicts, unresolved = analysis.synthesize(deps.llm, question, report.sources,
                                                                              report.claims)
            report.unresolved += unresolved
            close(rec, f"{len(report.conflicts)} conflict(s)", c0)
        else:
            report.unresolved.append("no claim could be extracted from retrieved originals")
    except BudgetExceeded as exc:
        report.failures.append(Failure(stage="analysis", target="budget", detail=str(exc), needs_human=True))
    finally:
        if zotero is not None:
            zotero.__exit__(None, None, None)  # close the automated browser even on errors
    return _finish(deps, report, repos, write_note)


def _select(deps, question: str, cands: list[Candidate], seeds: list[str], report) -> list[tuple[Candidate, Any]]:
    by_id = {c.candidate_id: c for c in cands}
    seed_ids = [c.candidate_id for c in cands if "seed" in c.found_by]
    try:
        out = analysis.select(deps.llm, question, cands, deps.max_sources, seed_ids)
        chosen = [(by_id[s.candidate_id], s) for s in out.selected if s.candidate_id in by_id]
        report.unresolved += [f"selection note: {out.notes[:300]}"] if out.notes else []
    except BudgetExceeded:
        raise
    except Exception as exc:  # deterministic fallback: seeds, then most-corroborated candidates
        report.failures.append(Failure(stage="selection", target="llm", detail=f"{type(exc).__name__}: {exc}"[:300]))
        ranked = sorted(cands, key=lambda c: (-len(c.found_by), -(c.stars or 0)))
        chosen = [(c, None) for c in ranked[:deps.max_sources]]
    have = {c.candidate_id for c, _ in chosen}
    chosen += [(by_id[i], None) for i in seed_ids if i not in have]
    return chosen[: max(deps.max_sources, len(seed_ids))]


def _source(cand: Candidate, sel, n: int) -> Source:
    stype = SourceType(sel.source_type) if sel else (cand.kind_hint or SourceType.ORIGINAL_PAPER)
    return Source(source_id=f"S{n}", source_type=stype, title=cand.title, authors=cand.authors, year=cand.year,
                  doi=cand.doi, arxiv_id=cand.arxiv_id, version=cand.arxiv_version, url=cand.url, venue=cand.venue,
                  selection_reason=(sel.reason if sel else "seed / fallback ranking")[:500],
                  other_versions=cand.related_ids)


def _reuse_source(deps, src: Source, **ident) -> bool:
    doc = lit_store.known_source(deps.catalog, **ident)
    if not doc:
        return False
    src.retrieval = Retrieval.model_validate(doc["retrieval"])
    src.retrieval.detail = f"reused catalogued source {doc['source_key']}; " + src.retrieval.detail
    if doc.get("version"):
        src.version = doc["version"]
    return True


def _finish(deps, report: LiteratureReport, repos: dict[str, str], write_note: bool) -> LiteratureReport:
    report.total_cost_usd = round(deps.llm.spent, 6)
    try:
        lit_store.save(deps.catalog, report, repos)
    except Exception as exc:
        report.failures.append(Failure(stage="store", target=report.report_id, detail=f"{type(exc).__name__}: {exc}"[:300]))
    if write_note and deps.notes_writer is not None:
        from research_autopilot.literature.notes import write_note as _write
        try:
            _write(deps.notes_writer, report)
        except Exception as exc:
            report.failures.append(Failure(stage="notes", target=report.report_id, detail=f"{type(exc).__name__}"))
    return report


def deps_from_env(catalog, notes_writer=None) -> ResearcherDeps:
    from research_autopilot.config import load_settings
    from research_autopilot.literature.net import from_env
    s = load_settings()
    model = os.environ.get("RA_LITERATURE_MODEL", "").strip() or s.reviewer_model
    budget = float(os.environ.get("RA_LITERATURE_BUDGET_USD") or 2.0)

    def zotero_factory():
        from research_autopilot.literature.zotero.session import ZoteroSession
        return ZoteroSession()
    use_zotero = (os.environ.get("RA_ZOTERO_ENABLED", "true").strip().lower() not in ("0", "false", "no"))
    return ResearcherDeps(net=from_env(), llm=PricedLlm(model, s.model_pricing, budget, s.agents_tracing),
                          catalog=lit_store.with_literature(catalog), notes_writer=notes_writer,
                          zotero_factory=zotero_factory if use_zotero else None,
                          max_sources=int(os.environ.get("RA_LITERATURE_MAX_SOURCES") or 4),
                          max_retrievals=int(os.environ.get("RA_LITERATURE_MAX_RETRIEVALS") or acquire.MAX_RETRIEVALS))
