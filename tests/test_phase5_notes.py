import json
from pathlib import Path

from research_autopilot.autopilot.completion import review_payload
from research_autopilot.autopilot.models import CompletionAssessment
from research_autopilot.autopilot.notes import notes_blocks
from research_autopilot.experiments.models import ExperimentManifest


FIXTURE = Path(__file__).parent / "fixtures" / "inu_279_report.json"


def _fixture():
    raw = json.loads(FIXTURE.read_text())
    return ExperimentManifest.model_validate(raw["manifest"]), CompletionAssessment.model_validate(raw["assessment"])


def _text(block) -> str:
    kind = block["type"]
    data = block[kind]
    parts = [x["text"]["content"] for x in data.get("rich_text", [])]
    for row in data.get("children", []):
        if row["type"] == "table_row":
            parts += [x["text"]["content"] for cell in row["table_row"]["cells"] for x in cell]
        else:
            parts.append(_text(row))
    return " ".join(parts)


def test_inu_279_human_report_is_concise_and_traceable():
    manifest, assessment = _fixture()
    blocks = notes_blocks("INU-279", "[TEST] H2", manifest, assessment,
                          {"linear": "https://linear.app/issue/INU-279", "prefect": "https://prefect/runs"},
                          ["18cd7d61234567890abcdef"])
    headings = [_text(b) for b in blocks if b["type"] == "heading_2"]
    assert headings == ["結論", "計算結果", "検証状況", "注意点", "次のアクション"]
    (metrics,) = [b for b in blocks if b["type"] == "table"]
    assert metrics["table"]["has_column_header"] is True
    report = " ".join(_text(b) for b in blocks)
    assert "RHF 全エネルギー -1.11675931 Ha" in report
    assert "FCI 全エネルギー -1.13728383 Ha" in report
    assert "相関エネルギー -0.02052453 Ha" in report
    assert manifest.jobs[0].validation.metrics["correlation_energy"] == -0.020524527092076417
    assert report.count("相関エネルギー -0.02052453 Ha") == 1
    assert "未検証：results/h2.json" in report and "達成：FCI ≤ RHF" in report
    assert "自動完了：保留" in report and "研究仮説の評価：支持" in report
    assert "主要 artifact: results/h2.json" in report and "Experiment Run ID: exp-inu-279" in report
    assert "/private/tmp/" not in report and "sha256" not in report and "aaaaaaaaaaaa" not in report
    assert any(b["type"] == "toggle" and b["toggle"]["children"] for b in blocks)


def test_report_handles_empty_limitations_and_actions_naturally():
    manifest, assessment = _fixture()
    manifest.analysis.limitations = []
    manifest.analysis.open_questions = []
    assessment.criteria = [assessment.criteria[1]]
    blocks = notes_blocks("INU-279", "H2", manifest, assessment, {}, [])
    report = " ".join(_text(b) for b in blocks)
    assert "特記すべき注意点はありません。" in report
    assert "次のアクション" not in [_text(b) for b in blocks if b["type"] == "heading_2"]


def test_completion_payload_lists_exact_allowed_evidence():
    manifest, _ = _fixture()
    payload = review_payload({"task_id": "INU-279"}, manifest, manifest.analysis.summary)
    assert "exists:results/h2.json" in payload["allowed_evidence"]
    assert "units:results/h2.json:e_rhf" in payload["allowed_evidence"]
    assert "check:fci_below_rhf" in payload["allowed_evidence"]
    assert "acceptance:output" not in payload["allowed_evidence"]


def test_not_met_status_is_not_softened_in_human_report():
    manifest, assessment = _fixture()
    assessment.criteria[0].status = "not_met"
    blocks = notes_blocks("INU-279", "H2", manifest, assessment, {}, [])
    assert "未達：results/h2.json" in " ".join(_text(b) for b in blocks)
