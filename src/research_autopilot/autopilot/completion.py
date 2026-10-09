"""Done decision. Done requires ALL of:
- Phase 4 run completed and every validation passed (artifact integrity, finite values, metrics, numeric checks);
- every acceptance criterion judged "met" by the completion reviewer AND each cited piece of evidence
  is an existing *passing* check / metric (fabricated evidence downgrades to "unverifiable");
- scientific risk is not "high" (high-risk tasks get a human review);
- no pending approvals for the task (checked by the caller), Notes written and Linear comment posted.
A refuted hypothesis is a valid outcome: success means the predefined criteria were evaluated and met.
"""

import json
from typing import Protocol

from research_autopilot.autopilot.models import CompletionAssessment, CompletionReview, CriterionResult
from research_autopilot.experiments.models import ExperimentManifest, ExperimentState


class CompletionReviewer(Protocol):
    model: str

    def review(self, payload: dict): ...  # -> LlmRun with CompletionReview output


def assess(manifest: ExperimentManifest, acceptance: list[str], scientific_risk: str | None,
           review: CompletionReview | None) -> CompletionAssessment:
    reasons: list[str] = []
    validations = [j.validation for j in manifest.jobs if j.validation]
    latest = {}
    for j in manifest.jobs:
        if j.validation:
            latest[j.experiment_id] = j.validation
    det_ok = manifest.state is ExperimentState.COMPLETED and bool(latest) and all(v.status == "pass" for v in latest.values())
    if not det_ok:
        reasons.append("deterministic validation incomplete or failed")
    passing = {c.name for v in latest.values() for c in v.checks if c.status == "pass"}
    passing |= {f"metric:{k}" for v in latest.values() for k in v.metrics} | {k for v in latest.values() for k in v.metrics}
    criteria: list[CriterionResult] = []
    if review is None:
        reasons.append("no completion review available")
    else:
        by_text = {c.criterion.strip(): c for c in review.criteria}
        for crit in acceptance:
            r = by_text.get(crit.strip())
            if r is None:
                criteria.append(CriterionResult(criterion=crit, status="unverifiable", evidence=[], note="not assessed"))
                continue
            if r.status == "met":
                bad = [e for e in r.evidence if e not in passing]
                if not r.evidence or bad:
                    r = CriterionResult(criterion=crit, status="unverifiable", evidence=r.evidence,
                                        note=f"evidence not backed by passing checks: {bad or 'none cited'}")
            criteria.append(r)
        not_met = [c.criterion for c in criteria if c.status != "met"]
        if not_met:
            reasons.append(f"acceptance criteria not established: {not_met}")
    if scientific_risk == "high":
        reasons.append("scientific risk is high: human review required before Done")
    del validations
    return CompletionAssessment(auto_done=not reasons, reasons=reasons or ["all completion conditions met"],
                                deterministic_ok=det_ok, criteria=criteria,
                                hypothesis_outcome=review.hypothesis_outcome if review else None,
                                scientific_risk=scientific_risk)


COMPLETION_INSTRUCTIONS = """\
You are the Completion Reviewer of Research Autopilot. Decide, for each predefined acceptance criterion of the
task (quoted verbatim), whether the validated results show it is met. Cite as evidence ONLY names of validation
checks with status "pass" or metric names from the provided results. If a criterion cannot be established from
the validated results, use "unverifiable" (or "not_met" if the results contradict it). Also state whether the
hypothesis was supported, refuted or inconclusive -- a refuted hypothesis is a valid result. Respond in the
language of the task.
"""


def review_payload(task: dict, manifest: ExperimentManifest, analysis_summary: str | None) -> dict:
    return {
        "task": task,
        "validated_results": [{"experiment": j.experiment_id, "status": j.validation.status, "metrics": j.validation.metrics,
                               "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in j.validation.checks]}
                              for j in manifest.jobs if j.validation],
        "analysis_summary": analysis_summary,
    }


class OpenAICompletionReviewer:
    def __init__(self, model: str, prices: dict, tracing: bool = False) -> None:
        from research_autopilot.experiments.analyst import _PricedAgent

        self._agent = _PricedAgent(model, prices, lambda: 1.0, tracing)
        self.model = model

    def review(self, payload: dict):
        return self._agent._call("CompletionReviewer", COMPLETION_INSTRUCTIONS, payload, CompletionReview)


def to_json(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)
