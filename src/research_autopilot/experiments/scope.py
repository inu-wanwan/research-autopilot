"""ScopeAssessment: deterministic diff of a proposal against the original task, combined
with the Reviewer's (advisory) opinion. Ambiguity -> requires_human_decision=True.
Phase 4 never turns this into an approval."""

import hashlib
import json
import re

from research_autopilot.experiments.models import ProposalDraft, ScientificReviewResult, ScopeAssessment

OUT_OF_SCOPE_KINDS = {"new_target", "invariant", "acceptance", "objective"}
AMBIGUOUS_KINDS = {"method", "other"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def assess(draft: ProposalDraft, *, original_invariants: list[str], original_acceptance: list[str],
           original_purpose: str | None, review: ScientificReviewResult | None) -> ScopeAssessment:
    findings: list[str] = []
    changes: list[str] = []
    ambiguous: list[str] = []

    prop_inv = {_norm(x) for x in draft.scientific_invariants}
    for inv in original_invariants:
        if _norm(inv) not in prop_inv:
            findings.append(f"original scientific invariant not preserved verbatim: {inv!r}")
    prop_acc = {_norm(x) for x in draft.acceptance_criteria}
    for acc in original_acceptance:
        if _norm(acc) not in prop_acc:
            findings.append(f"original acceptance criterion missing (possible relaxation): {acc!r}")

    inv_text = " ".join(_norm(x) for x in original_invariants)
    for c in draft.parameter_changes:
        desc = f"{c.name}: {c.from_value} -> {c.to_value} ({c.kind})"
        changes.append(desc)
        if c.kind in OUT_OF_SCOPE_KINDS:
            findings.append(f"declared out-of-scope change: {desc}")
        elif c.kind in AMBIGUOUS_KINDS:
            ambiguous.append(f"change of kind {c.kind!r} needs a human judgement: {desc}")
        elif c.from_value and _norm(c.from_value) in inv_text:
            findings.append(f"'within_spec' change touches a value fixed by an invariant: {desc}")
    if not draft.purpose.strip() or not draft.hypothesis.strip():
        ambiguous.append("purpose or hypothesis is empty")

    reviewer_ok: bool | None = None
    if review is None:
        ambiguous.append("no Scientific Reviewer opinion available")
    else:
        reviewer_ok = (review.scope_within_original and review.invariants_preserved and review.acceptance_not_relaxed
                       and not review.new_scientific_judgement_required)
        if not review.scope_within_original:
            findings.append("Reviewer: outside the original research scope")
        if not review.invariants_preserved:
            findings.append("Reviewer: scientific invariants not preserved")
        if not review.acceptance_not_relaxed:
            findings.append("Reviewer: acceptance criteria relaxed")
        if review.new_scientific_judgement_required:
            ambiguous.append("Reviewer: a new scientific judgement is required")
        if review.verdict != "accept":
            ambiguous.append(f"Reviewer verdict is {review.verdict!r}")

    within = not findings and not ambiguous and reviewer_ok is True
    rationale = ("within the originally approved purpose, invariants and acceptance criteria; reviewer agrees"
                 if within else "; ".join(findings + ambiguous))
    return ScopeAssessment(within_scope=within, rationale=rationale, proposed_changes=changes,
                           requires_human_decision=not within, deterministic_findings=findings + ambiguous,
                           reviewer_opinion=reviewer_ok)


def dedupe_key(task_id: str, draft: ProposalDraft) -> str:
    content = {"task": task_id, "q": _norm(draft.research_question), "h": _norm(draft.hypothesis),
               "impl": _norm(draft.implementation), "acc": sorted(_norm(a) for a in draft.acceptance_criteria),
               "changes": sorted(f"{_norm(c.name)}|{_norm(c.to_value)}|{c.kind}" for c in draft.parameter_changes)}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:40]
