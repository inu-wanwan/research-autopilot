"""Definition of Done: deterministic checks of a plan's pass/fail items and the system baseline.

The Planner writes task-specific items (docs/phase_specs/phase2.md, Definition of Done); the system adds the
baseline items that its own checks already prove, so neither the implementer nor the reviewer re-negotiates them.
"""

import re

from research_autopilot.models import DoneCriterion, ExecutionPlan, TaskSpec

MAX_TASK_CRITERIA = 10
PHASE4_CONTRACT = "phase4_contract"

# Proven by Phase 3's deterministic checks / Phase 4's validator; the reviewer does not re-judge them.
BASELINE: list[DoneCriterion] = [
    DoneCriterion(id="B1", statement="The repository's test command passes.", source=PHASE4_CONTRACT,
                  check="deterministic", how="Phase 3 deterministic check: tests"),
    DoneCriterion(id="B2", statement="`.autopilot/experiment.json` exists and validates against the experiment "
                                     "definition schema.", source=PHASE4_CONTRACT, check="deterministic",
                  how="Phase 3 deterministic check: experiment_spec"),
    DoneCriterion(id="B3", statement="Every declared output names the units of its real JSON fields.",
                  source=PHASE4_CONTRACT, check="deterministic", how="experiment definition validation"),
    DoneCriterion(id="B4", statement="Every runtime dependency the code imports is declared in a dependency file "
                                     "listed in experiment.json (Phase 4 installs only those).",
                  source=PHASE4_CONTRACT, check="review", how="imports vs. declared dependency files"),
    DoneCriterion(id="B5", statement="Only files in the allowed file scope changed; protected paths untouched.",
                  source=PHASE4_CONTRACT, check="deterministic", how="Phase 3 deterministic check: scope"),
]

_VAGUE = re.compile(r"\b(appropriate(ly)?|reasonabl[ey]|as needed|if possible|etc\.?)\b|適切に|必要に応じ", re.I)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^[\s\-*・\d.)]+", "", text or "")).strip().strip("。.")


def spec_text(spec: TaskSpec) -> str:
    fields = (spec.purpose, spec.specification, spec.acceptance_criteria, spec.scientific_invariants,
              spec.artifact_requirements, spec.non_goals)
    return _norm(" ".join(f for f in fields if f))


def problems(spec: TaskSpec, plan: ExecutionPlan) -> list[str]:
    """Why the plan's Definition of Done is not acceptable (empty = acceptable)."""
    out: list[str] = []
    dod = plan.definition_of_done
    if not dod:
        return ["definition_of_done is empty: write pass/fail items that prove every acceptance criterion"]
    if len(dod) > MAX_TASK_CRITERIA:
        out.append(f"definition_of_done has {len(dod)} items; keep at most {MAX_TASK_CRITERIA} (drop items the "
                   "specification does not require)")
    text = spec_text(spec)
    for c in dod:
        if not re.fullmatch(r"D\d+", c.id):
            out.append(f"{c.id}: ids are D1, D2, ... (B ids are reserved for the system baseline)")
        source = _norm(c.source)
        if source != PHASE4_CONTRACT and (len(source) < 4 or source not in text):
            out.append(f"{c.id}: source must quote the task specification verbatim (or be '{PHASE4_CONTRACT}'); "
                       f"not found: {c.source[:120]!r}")
        if _VAGUE.search(c.statement):
            out.append(f"{c.id}: statement is not pass/fail checkable (vague wording): {c.statement[:120]!r}")
    for m in plan.acceptance_mapping:
        if not m.done_criteria:
            out.append(f"acceptance criterion {m.criterion[:80]!r} is not covered by any definition_of_done item")
    return out


def full(plan: ExecutionPlan | None) -> list[DoneCriterion]:
    """Baseline + the plan's items (empty when the plan predates the Definition of Done)."""
    items = getattr(plan, "definition_of_done", None)
    return BASELINE + list(items) if items else []
