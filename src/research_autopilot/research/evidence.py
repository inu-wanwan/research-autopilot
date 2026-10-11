"""Evidence / Artifact Context for the Research Planner and Evaluator.

Sources: verified Phase 7 Catalog artifacts, citable Phase 8 literature claims, Phase 9 registered implementations
and this research's own validated results.  Each item says whether it is verified; external content is data only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from research_autopilot.research.models import EvidenceItem

STOP = {"the", "and", "for", "with", "from", "that", "this", "into", "between", "using", "under", "when", "what",
        "how", "does", "which", "are", "is", "of", "to", "in", "on", "by", "as", "an", "a"}
MAX_PER_SOURCE = 8


def keywords(text: str) -> set[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9+_-]{2,}|[0-9]+[A-Za-z]+[A-Za-z0-9-]*", text or "")
    return {w.lower() for w in words if w.lower() not in STOP}


def _overlap(a: set[str], text: str) -> int:
    return len(a & keywords(text))


def collect(*, spec_text: str, store=None, catalog=None, literature_reports: list | None = None,
            external_entries: list | None = None, prior_results: list[dict] | None = None,
            registry: dict | None = None) -> list[EvidenceItem]:
    kw = keywords(spec_text)
    items: list[EvidenceItem] = []

    def add(**kw_):
        items.append(EvidenceItem(evidence_id=f"E{len(items) + 1}", **kw_))

    for r in (prior_results or [])[:20]:  # this research's own validated results (10B)
        add(kind="prior_result", ref=r.get("ref", ""), title=r.get("title", "result"),
            statement=json.dumps(r.get("data", {}), ensure_ascii=False, default=str)[:900],
            status="verified" if r.get("validated") else "unverified", locator=r.get("locator", ""))
    if catalog is not None and store is not None:
        try:
            from research_autopilot.catalog.models import ArtifactQuery
            from research_autopilot.catalog.service import query
            arts = [a for a in query(store, ArtifactQuery()) if a.status.value == "verified"]
            scored = sorted(((_overlap(kw, json.dumps([a.kind, a.identity, a.metadata], default=str)), a) for a in arts),
                            key=lambda x: -x[0])
            for score, a in scored[:MAX_PER_SOURCE]:
                if score < 2:
                    break
                add(kind="artifact", ref=a.artifact_id, title=f"{a.kind} {a.identity}"[:200],
                    statement=json.dumps(a.metadata, ensure_ascii=False, default=str)[:600], status="verified",
                    locator=a.provenance.repository_id if hasattr(a.provenance, "repository_id") else "")
        except Exception:
            pass
    for rep in literature_reports or []:
        if _overlap(kw, rep.question) < 2 and not rep.task_id:
            continue
        for c in rep.claims[:MAX_PER_SOURCE]:
            add(kind="literature", ref=f"{rep.report_id}:{c.claim_id}", title=f"{c.source_id} {c.kind.value}",
                statement=c.statement[:500], status="verified" if c.status == "verified" else "unverified",
                locator=c.locator)
    for e in external_entries or []:
        if _overlap(kw, f"{e.name} {e.identification.description} {(e.identification.paper.title if e.identification.paper else '')}") < 1:
            continue
        add(kind="implementation", ref=e.entry_id, title=f"{e.name}@{e.commit[:12]} ({e.state.value})",
            statement=(f"registered external implementation; license {e.verification.license.spdx if e.verification else '?'}; "
                       "execution needs a separate human grant"),
            status="verified" if e.state.value in ("registered", "superseded") else "unverified")
    for rid, cfg in (registry or {}).items():
        add(kind="spec", ref=f"repository:{rid}", title=f"registered research repository {rid}",
            statement=f"path {Path(cfg.path).name}; test command configured: {bool(getattr(cfg, 'test_command', None))}",
            status="verified")
    return items


def literature_reports(task_id: str | None = None, text: str = "", limit: int = 5) -> list:
    """Stored Phase 8 reports for this task or with overlapping questions (no new research, no cost)."""
    from research_autopilot.literature.models import LiteratureReport
    from research_autopilot.literature.store import store_dir
    d = store_dir() / "reports"
    if not d.is_dir():
        return []
    kw = keywords(text)
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            rep = LiteratureReport.model_validate_json(p.read_text())
        except Exception:
            continue
        if (task_id and rep.task_id == task_id) or _overlap(kw, rep.question) >= 2:
            out.append(rep)
    return out[-limit:]
