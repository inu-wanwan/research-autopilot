"""Planner integration: literature questions from the task spec or the Planner, evidence back to the Planner."""

from __future__ import annotations

import re

from research_autopilot.literature.models import LiteratureReport

SEED = re.compile(r"(10\.\d{4,9}/[^\s,;)>\]]+|arxiv:\s*\d{4}\.\d{4,5}|\b\d{4}\.\d{4,5}(?:v\d+)?\b|"
                  r"github\.com/[\w.-]+/[\w.-]+)", re.I)
MAX_CONTEXT_CLAIMS = 40


def seeds_from(text: str) -> list[str]:
    out = []
    for m in SEED.findall(text or ""):
        s = m.rstrip(".")
        s = re.sub(r"(?i)^arxiv:\s*", "", s)
        s = re.sub(r"(?i)^github\.com/", "", s)
        if s not in out:
            out.append(s)
    return out


def planner_context(reports: list[LiteratureReport]) -> str:
    lines = ["LITERATURE EVIDENCE (automated review; read-only context, not part of the fixed specification):"]
    for r in reports:
        srcs = {s.source_id: s for s in r.sources}
        lines.append(f"== report {r.report_id}: {r.question[:200]}")
        if r.summary:
            lines.append(f"summary: {r.summary[:1500]}")
        for s in r.sources:
            ident = s.doi or (f"arXiv:{s.arxiv_id}{s.version or ''}" if s.arxiv_id else s.url or "")
            if s.source_type.value == "reference_implementation":
                ident = f"{s.url} @ {(s.version or '')[:12]}"
            lines.append(f"[{s.source_id}] {s.source_type.value}: {s.title} ({s.year or '?'}; {ident}; "
                         f"original {s.retrieval.status})")
        for c in r.claims[:MAX_CONTEXT_CLAIMS]:
            tag = "VERIFIED" if c.status == "verified" else "UNVERIFIED"
            lines.append(f"- {tag} [{c.kind.value}/{c.origin}] {c.statement} ({c.source_id} {c.locator})")
        for x in r.conflicts:
            lines.append(f"! conflict {x.topic}: {x.description} ({', '.join(x.claim_ids)})")
        problems = r.unresolved[:15] + [f"{f.stage} failed for {f.target}: {f.detail[:160]}" for f in r.failures[:10]]
        if problems:
            lines.append("open issues: " + " | ".join(problems))
        if not r.verified_claims:
            lines.append("NOTE: no verified evidence for this question (insufficient grounding)")
    return "\n".join(lines)
