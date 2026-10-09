"""Deterministic Approval Policy Gate.

LLM outputs (Phase 4 ScopeAssessment, Scientific Reviewer verdict) are evidence that can only make
the decision stricter: a proposal runs automatically only when it is mechanically shown to stay inside
the user's ResearchAuthorization (invariants, acceptance criteria, allowed parameter domains, limits,
budget). Anything else needs a human.
"""

import hashlib
import json
import re

from research_autopilot.autopilot.models import PolicyDecision, ResearchAuthorization
from research_autopilot.experiments.models import ReviewedExperimentProposal
from research_autopilot.models import utcnow

KIND_CATEGORY = {
    "new_target": "target_change", "objective": "metric_change", "invariant": "invariant_change",
    "acceptance": "acceptance_change", "method": "undecidable_scope", "other": "undecidable_scope",
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def spec_hash(purpose: str | None, invariants: list[str], acceptance: list[str], specification: str | None,
              target: str | None) -> str:
    """Hash of the scientific content of a task (what the user authorized)."""
    payload = {"purpose": _norm(purpose or ""), "target": _norm(target or ""), "spec": _norm(specification or ""),
               "inv": sorted(_norm(x) for x in invariants), "acc": sorted(_norm(x) for x in acceptance)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


def check_authorization(auth: ResearchAuthorization | None) -> list[str]:
    if auth is None:
        return ["no ResearchAuthorization: explicit onboarding approval required"]
    out = []
    if auth.revoked:
        out.append("authorization revoked")
    if auth.expires_at < utcnow():
        out.append("authorization expired")
    return out


def evaluate_budget(auth: ResearchAuthorization, used_api_usd: float, used_cpu_hours: float,
                    unknown_api_cost_calls: int = 0) -> list[str]:
    out = []
    if used_api_usd >= auth.budget.api_usd:
        out.append(f"API budget exhausted (${used_api_usd:.2f} >= ${auth.budget.api_usd:.2f})")
    if used_cpu_hours >= auth.budget.compute_cpu_hours:
        out.append(f"compute budget exhausted ({used_cpu_hours:.2f} >= {auth.budget.compute_cpu_hours:.2f} CPU-h)")
    if unknown_api_cost_calls:
        out.append(f"{unknown_api_cost_calls} paid API call(s) without a known price; budget cannot be verified")
    return out


def evaluate_proposal(auth: ResearchAuthorization | None, rp: ReviewedExperimentProposal, *, chain_depth: int,
                      proposals_from_experiment: int, pending_proposals: int, used_api_usd: float,
                      used_cpu_hours: float, unknown_api_cost_calls: int = 0) -> PolicyDecision:
    review = rp.review
    if review is None:
        return PolicyDecision(outcome="reject", reasons=[f"no Scientific Reviewer decision ({rp.review_error})"],
                              categories=["reviewer_unavailable"])
    if review.verdict != "accept":
        return PolicyDecision(outcome="reject", reasons=[f"Scientific Reviewer verdict {review.verdict}: {review.summary[:200]}"],
                              categories=["reviewer_not_accepted"])
    reasons, cats = [], []

    def need(cat: str, msg: str) -> None:
        cats.append(cat)
        reasons.append(msg)

    for r in check_authorization(auth):
        need("undecidable_scope", r)
    if auth is not None:
        d = rp.proposal.draft
        if {_norm(x) for x in d.scientific_invariants} != {_norm(x) for x in auth.scientific_invariants}:
            need("invariant_change", "scientific invariants differ from the authorized ones")
        missing = [a for a in auth.acceptance_criteria if _norm(a) not in {_norm(x) for x in d.acceptance_criteria}]
        if missing:
            need("acceptance_change", f"authorized acceptance criteria missing (possible relaxation): {missing}")
        domains = {p.name: p for p in auth.policy.parameter_domains}
        within = 0
        for c in d.parameter_changes:
            if c.kind != "within_spec":
                need(KIND_CATEGORY.get(c.kind, "undecidable_scope"), f"declared {c.kind} change: {c.name} {c.from_value}->{c.to_value}")
                continue
            dom = domains.get(c.name)
            if dom is None:
                need("undecidable_scope", f"parameter {c.name!r} is not in the authorized parameter domains")
            elif dom.kind == "integer_range":
                try:
                    v = float(c.to_value)
                    if (dom.min_value is not None and v < dom.min_value) or (dom.max_value is not None and v > dom.max_value):
                        need("undecidable_scope", f"{c.name}={c.to_value} outside authorized range [{dom.min_value}, {dom.max_value}]")
                    else:
                        within += 1
                except ValueError:
                    need("undecidable_scope", f"{c.name}={c.to_value!r} is not numeric")
            elif dom.kind == "enum" and c.to_value not in dom.allowed_values:
                need("undecidable_scope", f"{c.name}={c.to_value} not in {dom.allowed_values}")
            else:
                within += 1
            if c.from_value and _norm(c.from_value) in " ".join(_norm(x) for x in auth.scientific_invariants):
                need("invariant_change", f"change of {c.name} touches a value fixed by an invariant")
        if within == 0 and not cats:
            need("undecidable_scope", "no declared change within the authorized parameter domains (scope cannot be decided mechanically)")
        pol = auth.policy
        if chain_depth > pol.max_chain_depth:
            need("limit", f"chain depth {chain_depth} > {pol.max_chain_depth}")
        if proposals_from_experiment > pol.max_new_proposals_per_experiment:
            need("limit", f"more than {pol.max_new_proposals_per_experiment} proposals from one experiment")
        if pending_proposals >= pol.max_pending_proposals:
            need("limit", f"{pending_proposals} proposals already pending (limit {pol.max_pending_proposals})")
        for r in evaluate_budget(auth, used_api_usd, used_cpu_hours, unknown_api_cost_calls):
            need("budget_exceeded", r)
    if not rp.scope_assessment.within_scope:  # LLM evidence can only add restrictions
        need("undecidable_scope", "Phase 4 ScopeAssessment: " + rp.scope_assessment.rationale[:200])
    if not cats:
        return PolicyDecision(outcome="auto", reasons=["within authorized scope, limits and budget"], categories=[])
    return PolicyDecision(outcome="human", reasons=reasons, categories=sorted(set(cats)))
