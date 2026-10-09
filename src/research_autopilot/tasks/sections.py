"""Map free-form Notion headings to canonical spec sections.

Task pages use mixed English / Japanese headings. A heading maps to a
canonical section when its normalized text matches one of the synonyms
below; unmatched headings are kept verbatim under ``other_sections`` so the
Planner still sees them. Matching is exact (after normalization) or by the
listed prefixes, so it is deterministic.
"""

import re

CANONICAL_SECTIONS: dict[str, tuple[set[str], tuple[str, ...]]] = {
    # name: (exact synonyms, prefixes)
    "purpose": ({"purpose", "objective", "objectives", "goal", "goals", "目的", "ゴール"}, ()),
    "background": ({"background", "context", "背景"}, ()),
    "scientific_rationale": (
        {"scientific rationale", "rationale", "motivation", "科学的根拠", "根拠", "動機"},
        (),
    ),
    "specification": (
        {
            "implementation", "experiment", "experiments", "specification", "spec", "method",
            "methods", "work", "procedure", "implementation / experiment specification",
            "implementation specification", "experiment specification", "実装", "実験", "手順",
            "作業", "仕様", "実装仕様", "実験仕様", "方法",
        },
        ("implementation", "experiment", "実装", "実験"),
    ),
    "acceptance_criteria": (
        {
            "acceptance criteria", "acceptance", "done criteria", "definition of done",
            "完了条件", "受け入れ条件", "受入条件", "合格条件",
        },
        (),
    ),
    "dependencies": ({"dependencies", "dependency", "依存", "依存関係", "前提"}, ()),
    "scientific_invariants": (
        {"scientific invariants", "invariants", "不変条件", "科学的不変条件"},
        (),
    ),
    "artifact_requirements": (
        {"artifacts", "artifact", "artifact requirements", "deliverables", "成果物"},
        (),
    ),
}

_NUMBERING = re.compile(r"^\s*(?:\d+(?:\.\d+)*[.)]?|[-*・])\s*")


def normalize_heading(text: str) -> str:
    text = _NUMBERING.sub("", text.strip())
    return re.sub(r"\s+", " ", text).strip(" :：").lower()


def canonical_section(heading: str) -> str | None:
    norm = normalize_heading(heading)
    for name, (exact, prefixes) in CANONICAL_SECTIONS.items():
        if norm in exact or any(norm.startswith(p) for p in prefixes):
            return name
    return None


def split_sections(blocks: list[tuple[int, str]]) -> tuple[dict[str, str], dict[str, str], str]:
    """Split ``(heading_level, text)`` lines into sections.

    ``heading_level`` is 1-3 for headings and 0 for body lines. Returns
    ``(canonical_sections, other_sections, preamble)``. A non-canonical
    heading nested deeper than the current canonical heading stays inside
    that section.
    """
    canonical: dict[str, list[str]] = {}
    other: dict[str, list[str]] = {}
    preamble: list[str] = []
    target: list[str] = preamble
    current_level = 0

    for level, text in blocks:
        if level == 0:
            target.append(text)
            continue
        name = canonical_section(text)
        if name is not None:
            target = canonical.setdefault(name, [])
            current_level = level
        elif current_level and level > current_level:
            target.append("#" * level + " " + text)
        else:
            key = text.strip()
            target = other.setdefault(key, [])
            current_level = 0

    def join(d: dict[str, list[str]]) -> dict[str, str]:
        return {k: "\n".join(v).strip() for k, v in d.items()}

    return join(canonical), join(other), "\n".join(preamble).strip()
