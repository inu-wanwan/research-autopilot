# Phase 3 仕様 — Autonomous Implementer & Code Reviewer

- 作成：2026-10-10 / Claude Code（ユーザー指示により作成）
- 出典：
  - ユーザーが提示した「ResearchAutopilot Phase 3 — Autonomous Implementer & Code Reviewer」（§1〜§17）
  - Notion Phase 3 ページ `3f469893cf8f81c89c65d16434f6b6e5`、Linear `INU-266`
  - 2026-10-10 のユーザー回答（§2）
- 外部仕様の正本は Notion。このファイルはそのローカル写しに、合意事項を加えたもの。変更はユーザーの承認がある場合のみ。

## 1. 目的

承認済みの研究タスクを Coding Agent に実装させ、独立した Reviewer に検証させたうえで、再現可能な実装成果（固定 commit と manifest）として確定する。Phase 2 の handoff JSON を入力とし、Phase 4 の Experiment Runner に渡す構造化成果物を作る。

## 2. 確定事項（2026-10-10 ユーザー回答・調査結果）

| 論点 | 決定 |
|---|---|
| 人間による実行承認 | **Phase 2 の apply モードで作られた handoff**（Linear を In Progress にした実行の handoff）があり、かつ **Linear が今も In Progress** のタスクだけを実装する。dry-run の handoff、承認情報のない handoff は blocked。LLM が承認を出すことはない |
| リポジトリと変更範囲 | 許可するリポジトリは**ローカルの登録ファイル**（TOML：パス、base branch、保護パス、test / lint / type check のコマンド）で管理する。各タスクの **Notion 本文の見出し「Repository」「Allowed files」**で、対象リポジトリと変更してよいファイル（glob）を人間が指定する。どちらかが欠けていれば blocked |
| 費用 | サブスクリプション認証の CLI（Codex の ChatGPT ログインなど）は、試行回数・時間・トークン数の上限で制御し、費用は unknown と記録する。従量課金の API 呼び出しは、単価と予算上限が設定されていなければ実行しない。Claude Code CLI は自分で費用（`total_cost_usd`）を報告するので、`--max-budget-usd` と実行全体の費用上限で制御する |
| Code Reviewer の LLM | Implementer とは別の非対話 CLI セッション（既定は Codex CLI の read-only sandbox と `--output-schema`）で実行する。従量課金の API を使わないため、費用の方針と整合する |
| Claude fallback の実動作テスト | Mac で小規模に行ってよい |
| 研究コードの置き場所 | ResearchAutopilot 本体とは別の Git リポジトリ |

### 動作確認できたモデル（2026-10-10、Mac、実際の CLI 呼び出しで応答を確認）

| tier | Codex CLI 0.162.0（ChatGPT ログイン） | Claude Code 2.1.295 |
|---|---|---|
| lightweight | `gpt-5.6-luna` | `claude-haiku-4-5-20251001` |
| standard | `gpt-5.6-terra` | `claude-sonnet-5-5` |
| high-reasoning | `gpt-5.6-sol` | `claude-opus-5-5` |

モデル ID は環境変数で変更できる。ここにないモデルは動作未確認として扱う。

## 3. 構成要素

- **Implementation Orchestrator**：handoff の受け取り、実行承認の検証、難易度判定、Backend とモデルの選択、修正・再試行・昇格・Fallback の制御、状態の遷移と保存、Phase 4 への引き継ぎ。
- **CodingBackend**：`CodexBackend`（primary、`codex exec`）と `ClaudeBackend`（fallback、`claude -p`）。非対話プロセスの管理と、結果・エラーの正規化を担う。
- **WorktreeManager**：タスクごとの branch と worktree、排他、差分と commit、異常終了後の復旧、Git 操作の監査。
- **CodeReviewer**：決定論的な検証と独立した LLM レビュー。修正要求を構造化し、科学的制約を確認する。
- 実行は Phase 1 / 2 の Prefect 基盤（flow `implement-task`）に統合する。独立したワークフローエンジンは作らない。

## 4. 難易度と科学的リスク、モデルの選択

- `implementation_complexity` と `scientific_risk`（それぞれ low / medium / high）を、設定で変えられる決定論的なルールで別々に評価し、`classification_reason` に根拠を残す。判定しにくい場合は medium 以上にする。分類のためだけの LLM は使わない。
- Implementer の tier：low → lightweight、medium → standard、high → high-reasoning。
- Reviewer のモデルは scientific_risk で選ぶ（low → standard、medium 以上 → high-reasoning）。
- 修正が必要になったら、通常は同じモデルで直させる。同じ種類の失敗が続いたときだけ上位の tier に昇格する。昇格しても試行回数は増えない。科学的制約違反、未承認の操作、仕様不足、予算超過のときは昇格しない。

## 5. 上限（初期値。設定で変更できる）

修正は最大2回、Implementer の試行は最大3回、同時に実装するタスクは1件、1タスクの実行時間は30分。main への自動 merge は無効、Eagle へのデプロイは無効。トークンと費用の上限も設定する。

## 6. Fallback（Codex → Claude）

- **許可する条件**：利用枠の到達、レート制限、復旧可能なプロセス障害、再試行しても解消しない一時的な障害。
- **禁止する条件**：未承認の操作、scientific invariants の違反、仕様不足、予算超過、通常のテスト失敗。
- **切り替えの手順**：
  - 切り替える前に、旧プロセスが終了したことを確認する。
  - Git の状態（branch / commit / 未 commit の diff）と manifest を保存する。
  - Claude は Git と manifest から作業を再開する。Codex の会話は引き継がない。
  - 同じ worktree を2つの Backend が同時に操作することは、ロックで禁止する。

## 7. 安全性

- main / master を直接変更しない。未 commit の変更を破棄しない。保護パス（validated artifacts）を上書きしない。
- 許可されていないファイルの変更を検出する。merge と push は行わない。Git 操作はすべて監査ログに残す。
- Phase 3 を通過しただけで Linear を Done にしない（Phase 3 は Linear を更新しない）。

## 7.1 Phase 2 の handoff 拡張（後方互換）

Phase 2 の見出し対応表に Repository / Allowed files / Non-goals / Inputs を追加し、`TaskSpec` と `ExecutionHandoff` にそれぞれ省略可能なフィールドとして加える。あわせて handoff に `phase2_mode` を加える。既存の handoff（これらのフィールドがないもの）も読めるが、`phase2_mode` がないので Phase 3 の承認ゲートで blocked になる。

## 8. Acceptance Criteria

1. Phase 2 の handoff JSON から Phase 3 を実行できる。
2. タスク単位の独立した Git worktree を作成できる。
3. 難易度と科学的リスクを別々に評価できる。
4. Implementer と Reviewer のモデルを適切に選べる。
5. Codex CLI で実際にコードを生成できる。
6. 静的検証と独立した LLM Reviewer が動作する。
7. Reviewer が問題を検出し、修正フローに戻せる。
8. 修正は最大2回に制限される。
9. Codex から Claude への fallback が安全に実行される。
10. main を無断で変更しない。
11. 予算超過や権限不足のときは安全に停止する。
12. 中断した実装を manifest から復旧できる。
13. Prefect UI で実行履歴を確認できる。
14. Phase 4 向けの固定 commit と artifact を生成できる。
15. 既存のテストがすべて通る。

## 9. Non-goals

Phase 4 の Experiment Runner、TSUBAME / Slurm での実行、長時間の研究計算、実験結果の科学的解釈、Phase 5 の常時自律ループ、Linear–Notion Mirror の再実装、未承認の main への merge / push、Eagle へのデプロイ。
