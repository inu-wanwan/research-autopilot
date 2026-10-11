"""Phase 10 fixtures: a research-objective page and a scripted Research Planner / Evaluator LLM."""

from research_autopilot.research import evaluator, planner
from research_autopilot.research.models import (Autonomy, BudgetItem, ConditionBound, EvaluationDecision, EvaluationOut,
                                                PlannedTask, PlannedTaskOut, ResearchScope, ScopeItem)

OBJ = ("- Question: how does dE(R)=E_RHF(R)-E_FCI(R) change with the bond length R?\n"
       "- Target: H2 with basis STO-3G, bond length R between 0.5 and 3.0 Angstrom.\n"
       "- Primary metric: dE(R) in Hartree (same orbitals and Hamiltonian).\n"
       "- At most 6 geometries in total.")
RESEARCH_SECTIONS = {
    "purpose": "Characterise how the RHF error grows along the H2 dissociation curve.",
    "research_objective": OBJ,
    "scientific_invariants": "- Same geometry, basis, charge and spin for RHF and FCI.",
    "acceptance_criteria": "- dE(R) obtained for several converged geometries with provenance.",
    "budget": "- Compute budget: 1 CPU-hour in total; API budget 5 USD.",
}


def scope(**kw) -> ResearchScope:
    s = ResearchScope(
        objective=ScopeItem(value="how dE(R)=E_RHF(R)-E_FCI(R) changes with R",
                            quote="how does dE(R)=E_RHF(R)-E_FCI(R) change with the bond length R", status="stated"),
        primary_metric=ScopeItem(value="dE(R) in Hartree", quote="Primary metric: dE(R) in Hartree", status="stated"),
        targets=ScopeItem(value="H2, STO-3G", quote="Target: H2 with basis STO-3G", status="stated"),
        conditions=[ConditionBound(name="bond_length_r", kind="range", unit="Angstrom", minimum=0.5, maximum=3.0,
                                   quote="bond length R between 0.5 and 3.0 Angstrom"),
                    ConditionBound(name="max_geometries", kind="max_count", count=6, applies_to="bond_length_r",
                                   quote="At most 6 geometries in total")],
        invariants=[ScopeItem(value="Same geometry, basis, charge and spin for RHF and FCI",
                              quote="Same geometry, basis, charge and spin for RHF and FCI", status="stated")],
        success_criteria=[ScopeItem(value="dE(R) obtained for several converged geometries",
                                    quote="dE(R) obtained for several converged geometries", status="stated")],
        budget=BudgetItem(cpu_hours=1, api_usd=5, quote="Compute budget: 1 CPU-hour in total; API budget 5 USD",
                          status="stated"),
        autonomy=Autonomy())
    return s.model_copy(update=kw)


def draft(**kw):
    """PlanDraft with internal PlannedTask objects converted to the strict LLM-facing form."""
    kw["tasks"] = [PlannedTaskOut.of(t) if isinstance(t, PlannedTask) else t for t in kw.get("tasks", [])]
    return planner.PlanDraft(**kw)


def task(key="T1", values=("0.5", "1.0", "2.0"), deps=(), kind="experiment", title=None) -> PlannedTask:
    return PlannedTask(key=key, title=title or f"RHF/FCI at R={','.join(values)}", kind=kind,
                       objective="Compute E_RHF and E_FCI", specification="Run h2_energy.py for the listed R values",
                       acceptance_criteria=["results/h2.json has e_rhf and e_fci"], depends_on=list(deps),
                       parameters={"bond_length_r": list(values)} if values else {}, estimate_cpu_hours=0.01,
                       decision_point=True, repository="h2-test", file_scope=["h2_energy.py", "tests/**", "configs/**"])


class ScriptedResearchLlm:
    """Scope / plan / evaluation answers in order; records every call and its payload."""

    model = "gpt-5.6-terra"

    def __init__(self, scope_answer=None, plans=None, decisions=None, cost=0.05):
        self.scope_answer = scope_answer or scope()
        self.plans = list(plans or [draft(rationale="Start with three geometries [E1]",
                                                      rationale_evidence=[], tasks=[task()],
                                                      replanning_rules=["add points where dE changes fastest"])])
        self.decisions = list(decisions or [])
        self.calls, self.spent, self.cost = [], 0.0, cost

    def __call__(self, name, instructions, payload, out_type):
        self.calls.append((name, payload))
        self.spent += self.cost
        if out_type is ResearchScope:
            return self.scope_answer
        if out_type is planner.PlanDraft:
            d = self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
            return d.model_copy(update={"tasks": [PlannedTaskOut.of(t) if isinstance(t, PlannedTask) else t
                                                  for t in d.tasks]})
        if out_type is EvaluationOut:
            d = self.decisions.pop(0) if self.decisions else EvaluationDecision(action="stop", rationale="no script")
            if callable(d):
                d = d(payload)
            return d
        raise AssertionError(out_type)


def complete(payload) -> EvaluationDecision:
    refs = [r["ref"] for r in payload["RESULTS"] if r.get("validated")]
    return EvaluationDecision(action="complete", rationale="dE grows with R", evidence=refs[:2],
                              criteria=[{"criterion": "dE(R) obtained for several converged geometries",
                                         "status": "met", "evidence": refs[:2]}],
                              conclusion="dE(R) increases towards dissociation", limitations=["STO-3G only"])


def more(values=("1.5",), **kw) -> EvaluationDecision:
    return EvaluationDecision(action="continue", rationale="dE changes fastest between 1.0 and 2.0",
                              new_tasks=[task("T2", values)], **kw)


__all__ = ["RESEARCH_SECTIONS", "ScriptedResearchLlm", "complete", "evaluator", "more", "scope", "task"]
