"""Notion Notes / Tasks page content (findings, provenance, links -- not raw logs)."""

from research_autopilot.autopilot.models import CompletionAssessment, ResearchTaskProposal
from research_autopilot.experiments.models import ExperimentManifest
from research_autopilot.tasks.notion_writer import bullet, heading, paragraph, section


def notes_blocks(task_id: str, task_title: str, manifest: ExperimentManifest, assessment: CompletionAssessment,
                 links: dict[str, str], commits: list[str]) -> list[dict]:
    a = manifest.analysis
    blocks = section("Summary", a.summary if a else "No analysis available.")
    blocks += section("Outcome", [f"Hypothesis: {assessment.hypothesis_outcome or 'n/a'}",
                                  f"Auto-completion: {'yes' if assessment.auto_done else 'no'} — " + "; ".join(assessment.reasons)])
    blocks += section("Acceptance criteria", [f"[{c.status}] {c.criterion} — evidence: {', '.join(c.evidence) or '-'}"
                                              for c in assessment.criteria])
    if a:
        blocks += section("Findings (observed facts)", [f"{o.statement} [{', '.join(o.evidence)}]" for o in a.observed_facts])
        blocks += section("Inferences", [o.statement for o in a.inferences])
        blocks += section("Limitations", a.limitations)
        blocks += section("Unresolved questions", a.open_questions)
    metrics = []
    for j in manifest.jobs:
        if j.validation and j.validation.status == "pass":
            metrics += [f"{j.experiment_id}: {k} = {v}" for k, v in j.validation.metrics.items()]
    blocks += section("Validated metrics", metrics)
    arts = [f"{x.path} sha256={x.sha256[:16]} ({x.stored_path})" for j in manifest.jobs if j.collect for x in j.collect.artifacts]
    blocks += section("Artifacts", arts)
    blocks += section("Provenance", [f"task: {task_id} ({task_title})", f"experiment run: {manifest.experiment_run_id}",
                                     f"commits: {', '.join(c[:12] for c in commits)}",
                                     *[f"{k}: {v}" for k, v in links.items()]])
    return blocks


def proposal_blocks(p: ResearchTaskProposal) -> list[dict]:
    blocks = section("Purpose", p.purpose)
    blocks += section("Implementation", p.implementation)
    blocks += section("Acceptance criteria", p.acceptance_criteria)
    blocks += section("Scientific invariants", p.scientific_invariants)
    if p.repository:
        blocks += section("Repository", p.repository)
    if p.file_scope:
        blocks += [heading("Allowed files"), *[bullet(x.strip("- ").strip()) for x in p.file_scope.splitlines() if x.strip()]]
    blocks += section("Hypothesis", p.hypothesis)
    blocks += section("Rationale (Autopilot proposal)", p.rationale)
    blocks += section("Scientific review", p.review_summary)
    blocks += section("Estimated resources", p.resources)
    blocks += section("Evidence artifacts", p.evidence_artifacts[:20])
    blocks += [heading("Autopilot metadata"), paragraph(f"proposal_id={p.proposal_id} parent={p.parent_task_id} "
                                                       f"root={p.root_task_id} depth={p.chain_depth} policy={p.policy}: "
                                                       + "; ".join(p.policy_reasons))]
    return blocks
