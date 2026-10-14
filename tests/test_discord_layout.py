"""Short, Japanese-first Discord layout (user request 2026-10-14)."""

from datetime import datetime, timezone

from research_autopilot.autopilot.models import ApprovalRequest
from research_autopilot.notifications.discord import _delivery_marker, _has_marker, embed_payload
from research_autopilot.notifications.events import EventKind, NotificationEvent
from research_autopilot.notifications.guidance import approval_event, block_steps, human_event

REQ_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
SHA = "0123456789abcdef0123456789abcdef01234567"
UUID = "3f2b8c1e-9a7d-4e21-b6f0-1c2d3e4f5a6b"


def _headings(desc: str) -> list[str]:
    return [line.split("]**")[0].removeprefix("**[") for line in desc.splitlines() if line.startswith("**[")]


def _req(**kw) -> ApprovalRequest:
    base = dict(kind="research_scope", task_id="INU-292", root_task_id="INU-292", spec_hash="h",
                title="H₂ 結合解離曲線の研究", what_changes=["R の範囲を 0.5〜4.0 Å に拡大", "予算 50 CPU 時間"],
                diff_from_original=[], budget_summary="API $2.00 / 計算 50 CPU-h",
                expires_at=datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc))
    base.update(kw)
    req = ApprovalRequest(**base)
    return req.model_copy(update={"request_id": REQ_ID})


def test_approval_reads_top_down_in_japanese_with_ids_in_the_footer():
    event = approval_event(_req(), links=["Notion: https://www.notion.so/plan", "Linear: https://linear.app/x/issue/INU-292"])
    marker = _delivery_marker(event.stable_dedupe_key())
    p = embed_payload(event, marker)
    assert p["title"] == "承認依頼：研究計画｜INU-292"
    assert _headings(p["description"]) == ["タスク", "確認してほしいこと", "変更点", "予算・費用", "期限", "次にやること",
                                           "リンク"]
    assert "**[タスク]** INU-292 H₂ 結合解離曲線の研究" in p["description"]
    assert "**[確認してほしいこと]** 研究計画を承認してよいか" in p["description"]
    assert "・R の範囲を 0.5〜4.0 Å に拡大" in p["description"]
    assert "**[期限]** 10/21 09:00 UTC まで" in p["description"]
    assert "1. " in p["description"]  # numbered steps until the run continues (UX policy 1.5)
    assert REQ_ID[:8] not in p["description"]  # identifiers stay out of the body
    assert p["footer"].startswith(f"参照: req {REQ_ID[:8]}") and REQ_ID not in p["footer"]
    assert p["footer"].endswith(" · " + marker) and _has_marker(p["footer"], marker)


def test_result_message_has_result_and_links_last():
    event = NotificationEvent(kind=EventKind.TASK_COMPLETED, flow_name="research", flow_run_name="INU-292",
                              details=["研究が完了しました（3 サイクル）", "Notes: https://www.notion.so/notes",
                                       "結果: 解離曲線を 12 点で取得、FCI との差 < 1 mHa"])
    p = embed_payload(event)
    assert p["title"] == "完了｜INU-292"
    assert _headings(p["description"]) == ["タスク", "結果", "リンク"]
    assert "FCI との差 < 1 mHa" in p["description"] and "[Notion 結果](https://www.notion.so/notes)" in p["description"]


def test_human_block_message_keeps_reason_cost_steps_and_links():
    event = human_event(flow_name="autopilot", task_id="INU-300", state="blocked", headline="実行を止めました",
                        reason=f"Phase 4 blocked: commit {SHA} run {UUID}", steps=block_steps("Phase 4 blocked"),
                        links=["Notion: https://www.notion.so/t"], cost="費用: API $1.20 / 予算 $5.00", dedupe_key="k")
    p = embed_payload(event)
    assert p["title"] == "対応が必要｜INU-300"
    assert _headings(p["description"]) == ["タスク", "状況", "理由", "予算・費用", "次にやること", "リンク"]
    assert SHA not in p["description"] and SHA[:12] in p["description"]
    assert UUID not in p["description"] and UUID[:8] in p["description"]
    assert "上の［理由］" in p["description"]  # the reason is shown above the steps


def test_long_lists_are_cut_and_point_to_notion_linear():
    event = approval_event(_req(what_changes=[f"変更 {i}" for i in range(20)]))
    desc = embed_payload(event)["description"]
    assert "変更 4" in desc and "変更 5" not in desc and "ほか" in desc and "Notion / Linear" in desc


def test_discord_limits_hold_and_steps_survive_a_huge_body():
    event = NotificationEvent(kind=EventKind.UNRECOVERABLE_ERROR, flow_name="autopilot", flow_run_name="INU-1",
                              needs_human=True, details=["x" * 5000, *[f"理由: {'y' * 900}" for _ in range(10)],
                                                         "Linear: https://linear.app/x/issue/INU-1"],
                              steps=[f"手順 {i} " + "z" * 300 for i in range(12)])
    p = embed_payload(event, _delivery_marker("k"))
    assert len(p["description"]) <= 4096 and len(p["title"]) <= 256 and len(p["footer"]) <= 2048
    assert len(p["title"]) + len(p["description"]) + len(p["footer"]) <= 6000
    assert "[次にやること]" in p["description"] and "[リンク]" in p["description"]
    assert "ほか 4 件" in p["description"]  # 12 steps -> 8 shown


def test_urls_are_never_shortened_and_empty_links_are_dropped():
    url = f"https://example.org/{SHA}"
    event = NotificationEvent(kind=EventKind.TASK_COMPLETED, flow_name="autopilot", flow_run_name="INU-1",
                              details=[f"Pull Request: {url}", "Notes: None", "Task: "])
    desc = embed_payload(event)["description"]
    assert f"[Pull Request]({url})" in desc and "None" not in desc


def test_ids_from_reference_lines_go_to_the_footer():
    event = NotificationEvent(kind=EventKind.EXPERIMENT_COMPLETED, flow_name="autopilot", flow_run_name="INU-1",
                              details=["実験が完了しました", f"Experiment run: {UUID}"])
    p = embed_payload(event)
    assert UUID[:8] not in p["description"] and f"実験 {UUID[:8]}" in p["footer"]


def test_closed_approval_is_announced_first_with_a_link_to_the_replacement():
    event = approval_event(_req())
    closed = event.model_copy(update={"details": [*event.details,
                                                  "置き換え: 仕様が変わりました。新しい依頼へ: https://discord.com/channels/1/2/3"],
                                      "steps": []})
    desc = embed_payload(closed)["description"]
    assert _headings(desc)[0] == "この依頼は無効です"
    assert "[新しい依頼へ](https://discord.com/channels/1/2/3)" in desc and "[次にやること]" not in desc


def test_marker_matching_accepts_old_and_new_footers():
    m = _delivery_marker("k")
    assert _has_marker(m, m) and _has_marker(f"参照: req 1 · {m}", m)
    assert not _has_marker(None, m) and not _has_marker(f"{m}x", m) and not _has_marker("ra:other", m)
