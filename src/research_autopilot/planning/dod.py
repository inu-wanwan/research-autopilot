"""Definition of Done: deterministic checks of a plan's pass/fail items and the system baseline.

The Planner writes task-specific items (docs/phase_specs/phase2.md, Definition of Done); the system adds the
baseline items that its own checks already prove, so neither the implementer nor the reviewer re-negotiates them.
"""

import re
import unicodedata

from research_autopilot.models import DoneCriterion, ExecutionPlan, TaskSpec

MAX_TASK_CRITERIA = 10
# Re-plans per planning run after a Definition-of-Done-only rejection (they do not consume a max_replans
# iteration); the next DoD rejection ends the run.
MAX_DOD_REJECTIONS = 2
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
                                     "listed in experiment.json (Phase 4 installs only those), unless the "
                                     "repository runs in its own container image, which provides them.",
                  source=PHASE4_CONTRACT, check="review", how="imports vs. declared dependency files"),
    DoneCriterion(id="B5", statement="Only files in the allowed file scope changed; protected paths untouched.",
                  source=PHASE4_CONTRACT, check="deterministic", how="Phase 3 deterministic check: scope"),
]

_VAGUE = re.compile(r"\b(appropriate(ly)?|reasonabl[ey]|as needed|if possible|etc\.?)\b|適切に|必要に応じ", re.I)
_MARKUP = re.compile(r"\*\*|__|[`#*_「」『』“”‘’\"']")
_LINE_MARK = re.compile(r"^\s*(?:[-*+・•]|\d+[.)]|[(（]\d+[)）])\s*", re.M)
# Backslashes before ASCII punctuation: markdown escapes (``A^\*``) and LaTeX delimiters (``\(``) drift in copies.
_ESCAPE = re.compile(r"\\+(?=[!-/:-@\[-`{-~])")


def _norm(text: str) -> str:
    """Comparable form of a spec quote: NFKC, no markdown / quote marks / list markers / escaping backslashes,
    one-space whitespace."""
    text = _ESCAPE.sub("", unicodedata.normalize("NFKC", text or ""))
    text = _MARKUP.sub("", _LINE_MARK.sub("", text))
    return re.sub(r"\s+", " ", text).strip().strip("。.").strip()


_PASSAGE_BREAK = re.compile(r"\n\s*\n|…|\.\.\.")


def _missing_passage(source: str, text: str) -> str | None:
    """The first passage of ``source`` that is not verbatim in ``text`` (None = all found).

    A criterion may cite several places of the specification; the passages are separated by a blank line or
    an ellipsis and each must be verbatim on its own."""
    passages = [p for p in _PASSAGE_BREAK.split(source) if p.strip()] or [source]
    compact = text.replace(" ", "")  # spacing between Latin and CJK text drifts in copies ("subset 集合" / "subset集合")
    for passage in passages:
        norm = _norm(passage)
        if len(norm) < 4 or norm.replace(" ", "") not in compact:
            return passage.strip()
    return None


def spec_text(spec: TaskSpec) -> str:
    fields = [spec.title, spec.preamble, spec.purpose, spec.background, spec.scientific_rationale,
              spec.specification, spec.acceptance_criteria, spec.dependencies_note, spec.scientific_invariants,
              spec.artifact_requirements, spec.non_goals, spec.file_scope, spec.input_artifacts, spec.literature,
              spec.research_objective, spec.budget, spec.success_criteria, spec.stop_conditions, spec.autonomy,
              *spec.other_sections.values()]
    return _norm("\n".join(f for f in fields if f))


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
        if c.source.strip() != PHASE4_CONTRACT and (missing := _missing_passage(c.source, text)) is not None:
            out.append(f"{c.id}: source must quote the task specification verbatim (or be '{PHASE4_CONTRACT}'); "
                       f"several passages may be separated by a blank line or '…'; not found: {missing[:160]!r}")
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
