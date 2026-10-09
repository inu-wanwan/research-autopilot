"""Deterministic complexity / scientific-risk classification.

Rules are plain data (``Rules``) so thresholds and keywords can be changed
without code changes (``load_rules`` reads an optional TOML override).
When signals are weak or contradictory the result is conservative
(at least ``medium``). No LLM is used.
"""

import re
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

from research_autopilot.implementation.models import ComplexityAssessment, Level

_ORDER: list[Level] = ["low", "medium", "high"]


class Rules(BaseModel):
    # implementation complexity
    steps_medium: int = 4
    steps_high: int = 8
    # scope weight = number of globs + 1 per recursive glob ("**")
    scope_globs_medium: int = 4
    scope_globs_high: int = 10
    complexity_high_keywords: list[str] = Field(default_factory=lambda: [
        "new algorithm", "novel", "optimiz", "parallel", "distributed", "gpu", "neural network", "training",
        "refactor", "architecture", "concurren", "新規アルゴリズム", "アルゴリズム", "最適化", "並列", "学習", "設計変更",
    ])
    complexity_medium_keywords: list[str] = Field(default_factory=lambda: [
        "pipeline", "integration", "database", "cli", "api", "parser", "パイプライン", "統合",
    ])
    # scientific risk
    risk_high_keywords: list[str] = Field(default_factory=lambda: [
        "hamiltonian", "integral", "nevpt2", "casscf", "casci", "dmrg", "active space", "active-space", "orbital",
        "evaluation metric", "loss function", "gradient", "extrapolation", "reference energy", "teacher",
        "ハミルトニアン", "積分", "活性空間", "軌道", "評価指標", "損失", "外挿", "教師",
    ])
    risk_medium_keywords: list[str] = Field(default_factory=lambda: [
        "energy", "basis", "fci", "rhf", "scf", "convergence", "tolerance", "numerical", "metric", "pyscf",
        "エネルギー", "基底", "収束", "数値", "精度",
    ])


def load_rules(path: Path | None) -> Rules:
    if path is None or not path.exists():
        return Rules()
    with path.open("rb") as f:
        return Rules(**tomllib.load(f))


def _hits(text: str, words: list[str]) -> list[str]:
    """ASCII keywords match on word starts (so 'cli' does not hit 'client'); others as substrings."""
    t = text.lower()
    found = set()
    for w in words:
        wl = w.lower()
        if wl.isascii():
            if re.search(r"(?<![a-z0-9])" + re.escape(wl), t):
                found.add(w)
        elif wl in t:
            found.add(w)
    return sorted(found)


def _max(a: Level, b: Level) -> Level:
    return _ORDER[max(_ORDER.index(a), _ORDER.index(b))]


def classify(*, specification: str | None, plan_text: str, invariants: str | None, acceptance: str | None,
             scope_globs: list[str], n_steps: int, rules: Rules | None = None) -> ComplexityAssessment:
    r = rules or Rules()
    reasons: list[str] = []
    corpus = "\n".join(x for x in (specification, plan_text, acceptance) if x)

    # ---- implementation complexity
    impl: Level = "low"
    if n_steps >= r.steps_high:
        impl = _max(impl, "high"); reasons.append(f"impl: {n_steps} plan steps >= {r.steps_high}")
    elif n_steps >= r.steps_medium:
        impl = _max(impl, "medium"); reasons.append(f"impl: {n_steps} plan steps >= {r.steps_medium}")
    wide = [g for g in scope_globs if "**" in g or g.strip() in ("*", ".")]
    n_scope = len(scope_globs) + len(wide)
    if n_scope >= r.scope_globs_high:
        impl = _max(impl, "high"); reasons.append(f"impl: wide file scope ({len(scope_globs)} globs, {len(wide)} recursive)")
    elif n_scope >= r.scope_globs_medium:
        impl = _max(impl, "medium"); reasons.append(f"impl: file scope ({len(scope_globs)} globs, {len(wide)} recursive)")
    hk, mk = _hits(corpus, r.complexity_high_keywords), _hits(corpus, r.complexity_medium_keywords)
    if hk:
        impl = _max(impl, "high"); reasons.append(f"impl: keywords {hk}")
    elif mk:
        impl = _max(impl, "medium"); reasons.append(f"impl: keywords {mk}")
    if not specification or len(specification.strip()) < 40 or n_steps == 0:
        impl = _max(impl, "medium"); reasons.append("impl: specification too thin to judge -> conservative medium")

    # ---- scientific risk
    risk: Level = "low"
    rh, rm = _hits(corpus + "\n" + (invariants or ""), r.risk_high_keywords), _hits(corpus, r.risk_medium_keywords)
    if rh:
        risk = "high"; reasons.append(f"risk: keywords {rh}")
    elif rm:
        risk = "medium"; reasons.append(f"risk: keywords {rm}")
    inv_lines = [ln for ln in (invariants or "").splitlines() if ln.strip()]
    if inv_lines:
        risk = _max(risk, "medium"); reasons.append(f"risk: {len(inv_lines)} scientific invariant line(s) to preserve")
    if re.search(r"\b(1e-\d+|10\^-\d+|tolerance|許容誤差)\b", corpus, re.I):
        risk = _max(risk, "medium"); reasons.append("risk: numerical tolerance in acceptance/spec")
    if not corpus.strip():
        impl, risk = _max(impl, "medium"), _max(risk, "medium")
        reasons.append("no specification or plan text -> conservative medium/medium")
    if impl == "low":
        reasons.append(f"impl: {n_steps} plan steps, {len(scope_globs)} narrow scope glob(s), no complexity keywords -> low")
    if risk == "low":
        reasons.append("risk: no scientific keywords, invariants or numerical tolerances -> low")

    return ComplexityAssessment(
        implementation_complexity=impl, scientific_risk=risk, classification_reason=reasons,
        signals={"plan_steps": n_steps, "scope_globs": len(scope_globs), "recursive_globs": len(wide),
                 "complexity_keywords": hk + mk, "risk_keywords": rh + rm, "invariant_lines": len(inv_lines)},
    )
