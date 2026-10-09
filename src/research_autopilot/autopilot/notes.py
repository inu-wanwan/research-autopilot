"""Concise human reports for Notion; full evidence remains in ExperimentManifest / PostgreSQL."""

from __future__ import annotations

import re

from research_autopilot.autopilot.models import CompletionAssessment, ResearchTaskProposal
from research_autopilot.experiments.models import ExperimentManifest
from research_autopilot.tasks.notion_writer import bullet, heading, paragraph, section, table, toggle


_STATUS = {"met": "達成", "unverifiable": "未検証", "not_met": "未達"}
_HYPOTHESIS = {"supported": "支持", "refuted": "不支持", "inconclusive": "判断保留"}
_METRIC_LABELS = {
    "e_rhf": "RHF 全エネルギー", "rhf_energy": "RHF 全エネルギー",
    "e_fci": "FCI 全エネルギー", "fci_energy": "FCI 全エネルギー",
    "e_corr": "相関エネルギー", "correlation_energy": "相関エネルギー",
}


def _latest_jobs(manifest: ExperimentManifest):
    latest = {}
    for job in manifest.jobs:
        if job.validation:
            latest[job.experiment_id] = job
    return list(latest.values())


def _unit(detail: str) -> str | None:
    match = re.search(r"declared unit ([^;]+)", detail)
    return match.group(1).strip() if match else None


def _display_unit(unit: str | None) -> str:
    return "Ha" if unit and unit.casefold() == "hartree" else (unit or "")


def _display_value(value: float) -> str:
    if value == 0:
        return "0"
    if abs(value) >= 1e6 or abs(value) < 1e-6:
        return f"{value:.8e}"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.8f}"


def _label(name: str) -> str:
    return _METRIC_LABELS.get(name.casefold(), name.replace("_", " "))


def _metric_rows(manifest: ExperimentManifest) -> list[list[str]]:
    """Build display metrics deterministically, deduplicated by the underlying JSON field."""
    rows: dict[tuple[str, str], tuple[str, float, str | None]] = {}
    for job in _latest_jobs(manifest):
        validation = job.validation
        spec = manifest.specs.get(job.experiment_id)

        # Unit checks retain values for declared output fields, including fields not separately named as metrics.
        for check in validation.checks:
            if check.kind != "units" or check.status != "pass" or check.value is None or not check.name.startswith("units:"):
                continue
            _, remainder = check.name.split("units:", 1)
            path, field = remainder.rsplit(":", 1)
            key = (path, field.lstrip("/"))
            rows[key] = (_label(field.lstrip("/")), float(check.value), _unit(check.detail))

        for name, value in validation.metrics.items():
            metric = next((m for m in spec.validation.metrics if m.name == name), None) if spec else None
            field = metric.json_pointer.rsplit("/", 1)[-1] if metric and metric.json_pointer else name
            path = metric.output if metric else job.experiment_id
            rows[(path, field)] = (_label(name), float(value), metric.units if metric else None)

    display: dict[str, tuple[float, str | None]] = {}
    for label, value, unit in rows.values():
        if label not in display or unit:
            display[label] = (value, unit)
    return [[label, " ".join(x for x in (_display_value(value), _display_unit(unit)) if x)]
            for label, (value, unit) in display.items()]


def _completion_reason(assessment: CompletionAssessment) -> str:
    if assessment.auto_done:
        return "自動完了可能"
    labels = []
    joined = " ".join(assessment.reasons).casefold()
    if "acceptance criteria" in joined:
        labels.append("Acceptance criteria の一部が未検証または未達")
    if "scientific risk is high" in joined:
        labels.append("人間の確認が必要")
    if "deterministic validation" in joined:
        labels.append("数値検証が未完了または失敗")
    if "completion review" in joined:
        labels.append("完了レビューが未確認")
    return "保留（" + "、".join(labels or ["確認が必要"]) + "）"


def _artifacts(manifest: ExperimentManifest) -> list[str]:
    return list(dict.fromkeys(a.path for job in _latest_jobs(manifest) if job.collect for a in job.collect.artifacts))


def notes_blocks(task_id: str, task_title: str, manifest: ExperimentManifest, assessment: CompletionAssessment,
                 links: dict[str, str], commits: list[str]) -> list[dict]:
    analysis = manifest.analysis
    blocks = [heading("結論")]
    blocks.append(paragraph(analysis.summary if analysis and analysis.summary else "科学的な結論は未確認です。"))
    blocks.append(paragraph(f"研究仮説の評価：{_HYPOTHESIS.get(assessment.hypothesis_outcome or '', '未確認')}。"))
    blocks.append(paragraph(f"タスク完了判定：{_completion_reason(assessment)}。"))

    blocks.append(heading("計算結果"))
    rows = _metric_rows(manifest)
    blocks.append(table(["指標", "値"], rows) if rows else paragraph("表示できる検証済み metric はありません。"))

    validations = [job.validation for job in _latest_jobs(manifest)]
    numeric_ok = bool(validations) and all(v.status == "pass" for v in validations)
    checks = [f"数値検証：{'成功' if numeric_ok else '失敗または未完了'}"]
    checks += [f"{_STATUS[c.status]}：{c.criterion}" for c in assessment.criteria]
    checks.append(f"自動完了：{_completion_reason(assessment)}")
    blocks += [heading("検証状況"), *[bullet(line) for line in checks]]

    cautions: list[str] = []
    if analysis:
        cautions += analysis.limitations
        cautions += [f"不確実性：{x}" for x in analysis.uncertainties]
        cautions += [f"異常：{x}" for x in analysis.anomalies]
    cautions += [f"未検証：{c.criterion}" for c in assessment.criteria if c.status == "unverifiable"]
    blocks += [heading("注意点"), *([bullet(x) for x in dict.fromkeys(cautions)] if cautions else
                                     [paragraph("特記すべき注意点はありません。")])]

    actions: list[str] = []
    if analysis:
        actions += analysis.open_questions
    actions += [f"追加検証：{c.criterion}" for c in assessment.criteria if c.status == "unverifiable"]
    actions += [f"未達原因の確認：{c.criterion}" for c in assessment.criteria if c.status == "not_met"]
    if actions:
        blocks += [heading("次のアクション"), *[bullet(x) for x in dict.fromkeys(actions)]]

    fixed_commits = list(dict.fromkeys([*commits, *[j.commit for j in _latest_jobs(manifest) if j.commit]]))
    references = [
        bullet(f"Task ID: {task_id} ({task_title})"),
        bullet(f"Experiment Run ID: {manifest.experiment_run_id}"),
        bullet("固定 Git commit: " + (", ".join(c[:12] for c in fixed_commits) or "未確認")),
        bullet(f"Linear Issue: {links.get('linear') or '未確認'}"),
        bullet(f"Prefect Run: {manifest.flow_run_id or links.get('prefect') or '未確認'}"),
        *[bullet(f"主要 artifact: {path}") for path in _artifacts(manifest)],
        bullet("詳細監査記録: PostgreSQL / ExperimentManifest"),
    ]
    blocks.append(toggle("参照・再現情報", references))
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
