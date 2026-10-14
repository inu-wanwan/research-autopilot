"""Message templates for states that need a human (docs/phase_specs/ux_policy.md).

Every such message says what happened, why, the numbered steps until the run continues, the Notion / Linear
links and the cost / budget usage (policy 1.1, 1.5, 1.6).  Templates are per approval kind and per block reason
so the same situation always reads the same way.
"""

from __future__ import annotations

from research_autopilot.notifications.events import EventKind, NotificationEvent

_DECIDE = "Approve / Request revision（コメント必須）/ Reject のいずれかを押す"
_NEXT_POLL = "押した後は次のポーリング（最大 5 分）で自動的に続きが始まる（追加の操作は不要）"
_RUN = "Notion のタスクページで Run Autopilot を押す（新しい実行として最初から計画し直す）"

# kind -> (what is asked, steps)
APPROVAL_TEMPLATES: dict[str, tuple[str, list[str]]] = {
    "onboarding": ("Autopilot への委任（計画・実装・実験・解析をこのタスクの範囲で自律実行）の承認", [
        "Notion のタスクページで目的・科学的不変条件・受け入れ基準を確認する",
        "下の「変更」「予算」を確認する（範囲外の追加実験は別途承認が必要）",
        _DECIDE, _NEXT_POLL]),
    "budget": ("予算超過のため実行を止めています。予算の増額（承認で現在の上限の 2 倍）の承認", [
        "下の「予算」で使用量と上限、超過の理由を確認する",
        "続ける場合は Approve（上限が 2 倍になり止まった段階から再開）",
        "続けない場合は Reject（打ち切り）、仕様を直す場合は Request revision（コメント必須）", _NEXT_POLL]),
    "completion_review": ("自動で完了と判定できなかった結果の確認（Done にしてよいか）", [
        "下の「理由」と Notion の結果・受け入れ基準を確認する",
        "Approve = Done にして Notes を作成 / Request revision（コメント必須）= 止めて仕様を直す / Reject = 打ち切り",
        _NEXT_POLL]),
    "proposal": ("自動範囲外の追加実験（フォローアップタスク）の承認", [
        "Notion の提案タスクページで目的・変更点・理由を確認する",
        "Approve = 実行キューに入る / Reject = 取り下げ / Request revision（コメント必須）", _NEXT_POLL]),
    "dag": ("親タスクの子タスク間の依存関係（推論した DAG）の承認", [
        "下の依存関係（producer -> consumer）と根拠を確認する",
        "正しければ Approve（DAG を固定して子タスクを開始）",
        "違う場合は Notion の子タスクの仕様（依存・入力）を直してから親タスクで Run Autopilot を押す"
        "（このメッセージは失効し、推論し直した依頼が届く）", _NEXT_POLL]),
    "research_scope": ("研究スコープ（研究計画）の承認", [
        "Notion の計画ページで目的・条件・タスク・予算を確認する",
        _DECIDE,
        "仕様を直したい場合は Notion の仕様を編集して保存するだけでよい（自動で計画し直し、新しい版が届く）",
        _NEXT_POLL]),
    "plan": ("計画の科学的判断の承認", ["Notion の計画を確認する", _DECIDE, _NEXT_POLL]),
    "external_code": ("外部コードを研究環境で実行してよいか（初回実行・範囲変更）の承認", [
        "リポジトリ・ライセンス・サンドボックス検証の結果・実行範囲（下記）を確認する",
        "Approve = この範囲内でのみ実行を許可 / Reject = 実行しない / Request revision（コメント必須）",
        _NEXT_POLL]),
    "publish": ("完了したタスクの実装を GitHub に push して Pull Request を作ってよいか", [
        "Notion の結果と下の「変更」（push 先のブランチ・PR）を確認する",
        "Approve = タスクのブランチだけを push して PR を作成（main への merge は GitHub で人が行う）/ Reject = push しない",
        _NEXT_POLL]),
    "external_license": ("外部コードのライセンス条項の判断", [
        "検出したライセンス条項（下記）とリポジトリを確認する",
        "Approve = この用途で使ってよい（検証と登録に進む）/ Reject = このリポジトリは使わない", _NEXT_POLL]),
}


def approval_event(req, *, flow_name: str = "autopilot", links: list[str] = (), form_url: str | None = None,
                   dedupe_key: str | None = None, changes_limit: int = 8) -> NotificationEvent:
    """The single approvals message of one request (buttons are attached by the Discord gateway)."""
    kind = req.kind.value
    ask, steps = APPROVAL_TEMPLATES.get(kind, (f"{kind} の承認", ["下の内容を確認する", _DECIDE, _NEXT_POLL]))
    p = req.payload or {}
    details = [f"依頼: {ask}", f"対象: {req.title}"]
    if p.get("_supersedes"):
        details.append(f"版 v{p.get('_version', 2)}: 以前の依頼 {p['_supersedes'][:8]} を置き換えます")
    details += [x for x in links if x]
    if form_url:
        details.append(f"Approval form: {form_url}")
    details += [f"変更: {x}" for x in req.what_changes[:changes_limit]]
    details += [f"理由: {x}" for x in req.diff_from_original[:5]]
    details.append(f"予算: {req.budget_summary}")
    details.append(f"期限: {req.expires_at:%Y-%m-%d %H:%M} UTC（期限後は無効になり、必要なら新しい依頼が届く）")
    return NotificationEvent(kind=EventKind.APPROVAL_REQUIRED, flow_name=flow_name, flow_run_name=req.task_id,
                             state_name=f"{kind}:{req.status.value}", details=details,
                             dedupe_key=dedupe_key or f"approval:{req.request_id}", approval_request_id=req.request_id,
                             needs_human=True, steps=list(steps))


# (substring of the block reason, steps). Checked in order; the first match wins.
_BLOCK_RULES: list[tuple[tuple[str, ...], list[str]]] = [
    (("dead-lettered",), [
        "errors チャンネルの調査用メッセージで失敗の詳細を確認する",
        "原因（外部サービス・環境・設定）を直す"]),
    (("write refused",), [
        "Linear のタスクの状態とラベルを確認する（test-only 運用では [TEST] タスクのみ書き込める）",
        "状態を Todo に戻すか設定を直す"]),
    (("no usable Notion spec", "missing scientific specification"), [
        "Notion のタスクページで不足している節（目的・科学的不変条件・受け入れ基準・仕様）を埋める",
        "Linear Issue ID と Notion ページの対応を確認する"]),
    (("follow-up task spec deviates",), [
        "このフォローアップタスクの不変条件・受け入れ基準を、元タスクの承認範囲に合わせて Notion で直す",
        "範囲を広げたい場合は元タスクの仕様を直して改めて承認を受ける"]),
    (("Definition of Done",), [
        "Notion の受け入れ基準（Definition of Done）を確認し、実現できない項目を書き直すか根拠を確認する"]),
    (("revision requested",), [
        "承認メッセージに書かれた修正内容（コメント）を確認する",
        "Notion の仕様・計画をコメントどおりに直す"]),
    (("Phase 2",), [
        "下の理由（計画できなかった点）を確認する",
        "Notion の仕様（目的・実装方針・受け入れ基準・ファイル範囲）を、理由に書かれた点が解消するよう直す"]),
    (("Phase 3",), [
        "下の理由（実装・レビューが止まった点）を確認する",
        "Notion の仕様（実装方針・対象ファイル・受け入れ基準）を直す。環境・権限の問題ならそれを直す"]),
    (("Phase 4",), [
        "下の理由（実験が止まった点）を確認する",
        "計算条件・資源・データの指定を Notion の仕様で直す。計算資源の問題ならそれを直す"]),
    (("external code",), [
        "外部コードの判断（Reject / Request revision / 失効）を確認する",
        "Notion の仕様で外部コードを使わない方針にするか、範囲を変えて改めて依頼する"]),
    (("cannot complete",), [
        "下の理由（決定的なチェックが通らなかった点）と実験結果を確認する",
        "受け入れ基準・実験条件を Notion で直す"]),
    (("research objective found but",), [
        "Phase 10 の研究計画の設定（LLM の設定）を有効にする"]),
]


def block_steps(reason: str, *, resume_button: bool = False) -> list[str]:
    """Numbered steps for a blocked run, derived from the block reason."""
    steps = next((list(s) for keys, s in _BLOCK_RULES if any(k in reason for k in keys)),
                 ["下の理由を確認する", "理由に書かれた点を Notion の仕様または環境で直す"])
    steps.append("直したら Resume を押す（打ち切る場合は Cancel）" if resume_button else _RUN)
    return steps


def human_event(*, kind: EventKind = EventKind.UNRECOVERABLE_ERROR, flow_name: str, task_id: str,
                state: str | None, headline: str, reason: str, steps: list[str], links: list[str] = (),
                cost: str | None = None, dedupe_key: str, control_task_id: str | None = None,
                control_actions: list[str] = ()) -> NotificationEvent:
    """An approvals message for a state only a human can resolve (no approval request behind it)."""
    details = [headline, f"理由: {reason[:900]}", *[x for x in links if x]]
    if cost:
        details.append(cost)
    return NotificationEvent(kind=kind, flow_name=flow_name, flow_run_name=task_id, state_name=state,
                             details=details, dedupe_key=dedupe_key, needs_human=True, steps=list(steps),
                             control_task_id=control_task_id if control_actions else None,
                             control_actions=list(control_actions))


__all__ = ["APPROVAL_TEMPLATES", "approval_event", "block_steps", "human_event"]
