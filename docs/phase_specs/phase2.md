# Phase 2 仕様 — Task Manager / Orchestrator

- 作成：2026-10-10 / Claude Code（ユーザー指示により作成）
- 出典：
  - ユーザーが提示した「ResearchAutopilot Phase 2 実装依頼」（§1〜§15）
  - Notion Phase 2 ページ `3f469893cf8f817f9f7ef768fd99c9d3`（Purpose / Implementation / Acceptance criteria / Artifacts / Dependencies）、Linear `INU-265`
  - 2026-10-10 のユーザー回答（§3 の「確定事項」）
- 外部仕様の正本は Notion の Phase 2 ページ。このファイルはそのローカル写しに、ユーザーとの合意事項を加えたもの。
- 変更はユーザーの指示・承認がある場合のみ（`AGENTS.md` §5）。

---

## 1. 目的

Notion と Linear を正本として研究タスクの仕様と実行状態を取得し、承認済みの範囲内で、次に実行できるタスクと action を決める。そのタスクについて実行計画を生成・レビューし、許可された場合は Linear の進捗状態を更新する。

研究コードの自動生成と本格的な実験実行は Phase 3・4 で行う。

## 2. 責務分離（SSOT）

| システム | 正本となるもの | 取得するもの |
|---|---|---|
| Notion Research Tasks（DB `3b469893-cf8f-80f2-bc05-e8950262e0a0`、data source `3b469893-cf8f-802d-9f86-000bb4bbb21a`） | 研究タスクの詳細仕様 | Purpose, Background, Scientific rationale, Implementation / experiment specification, Acceptance criteria, Dependencies, Scientific invariants, Artifact requirements |
| Linear Research（project UUID `484c0d7b-f027-4a08-b433-6e9984b50436`。依頼文の `P-INU-1` に当たる） | タスクの実行状態 | Issue status, priority, dependencies, 対応する Notion ページ, 実行可能性の判定に必要なメタデータ |

- Linear と Notion の Status 同期（Mirror）は既存の外部ワークフローが担う。**再実装しない。** Notion の Status を実行状態の正本として使わない。
- ResearchAutopilot は必要なときに Linear の Status だけを更新し、Notion への反映は既存の Mirror に任せる。

## 3. 確定事項（2026-10-10 ユーザー回答）

| 論点 | 決定 |
|---|---|
| 人間による着手承認 | **Linear の state が `Todo`（type `unstarted`）で、かつ Notion の `Pending` が false** のときだけ承認済みとみなす。Backlog や Pending=true は blocked（承認待ち） |
| 作業スコープ | **Linear のラベルで指定する。** 指定ラベルが付いた issue だけが対象。ラベル名は設定で変えられる（既定 `autopilot`）。ラベルのない issue は blocked（スコープ外） |
| 依存関係 | **Linear の blocked-by relation を正本とする。** 未完了（state type が `completed` 以外）の blocker があれば blocked。Notion の Dependencies の記述は Planner への参考情報として渡すだけで、判定には使わない |
| 研究室サーバー（Eagle） | Windows + WSL2 上で Docker を動かしている |
| モデル | Planner（タスク計画）は `gpt-5.6-terra`、Reviewer は `gpt-5.6-sol`。設定で変更できる |

## 4. Task Manager

### 4.1 タスク取得

- `task_id`（Linear の識別子 `INU-123`）を指定し、Notion と Linear の情報を取得する。
- 実行できる次のタスク候補を取得する（スコープ内で `Todo` の issue を、priority → 作成日時の順に並べ、決定論的なゲートを最初に通過したもの）。
- Notion と Linear の対応関係を検証する。
  - Linear の説明文や添付にある Notion URL のページ
  - Notion の `Linear Issue ID` プロパティ

  この2つが食い違えばデータ不整合とする。
- 仕様不足、データ不整合、API 障害を区別する。

### 4.2 実行可能性の判定（決定論的。LLM の判断には依存しない）

- Linear の現在の Status（`Todo` 以外は着手しない）
- 未完了の依存タスク（Linear の blocked-by）
- 必要な研究仕様が揃っているか（必須セクション。既定は Purpose / Implementation・experiment specification / Acceptance criteria）
- 承認条件（§3）
- 同じタスクが実行中でないか（リースによる排他）
- 許可された作業スコープ（ラベル。§3）

### 4.3 Linear Status の更新

- 実行が許可され、着手が確定した場合だけ、Linear の Status を `In Progress` に更新する。
- **計画が完成したことだけを理由に `Done` にしない。** 更新先は state type `started` のものに限る。
- 更新は冪等にし（すでに In Progress なら何もしない）、監査ログを記録する。

## 5. マルチエージェント構成（OpenAI Agents SDK）

### 5.1 Planner Agent

研究タスクの仕様から、実装・実験ができる計画を作る。

出力：実行ステップ、依存関係と実行順序、入力と出力、必要な環境・ライブラリ、実験・実装上の制約、検証方法、acceptance criteria との対応、必要な計算資源の概算、不明点・承認が必要な事項。

元の研究目的、scientific invariants、acceptance criteria を無断で変更しない。

### 5.2 Reviewer Agent

Planner の計画を独立に検証する。

観点：元のタスク仕様との整合性、科学的・数学的な妥当性、実装できるか、再現性、評価指標と検証方法の妥当性、計算資源と実行コスト、研究上のリスク（データリーク、不適切な比較など）。

結論は `approved` / `revision_required` / `blocked` のいずれか。計画を直接書き換えず、構造化した修正要求を返す。

### 5.3 再計画

- Reviewer が修正を求めたら、指摘を Planner に渡して再計画する。
- **再計画は最大2回。** 2回直しても承認されなければ `blocked` とし、判断の根拠を保存する。

### 5.4 Reviewer を実行する条件

- 初期実装では**すべての計画に Reviewer を実行する。**
- 将来は条件付きレビューに切り替えられるよう、設定項目にしておく。

## 6. Orchestrator（Python + Prefect）

LLM は計画作成とレビューの判断に使う。依存の解決、認可、リトライ、実行状態の管理は決定論的な処理で行う。

1. Notion と Linear から研究タスクを取得する
2. 仕様・依存関係・承認ゲートを検証する
3. Planner が計画を作る
4. Reviewer がレビューする
5. 必要なら再計画する（最大2回）
6. 最後にもう一度、実行できるかを判定する
7. 許可されていれば Linear を `In Progress` にする
8. 計画・判断・実行履歴を保存する
9. Phase 3 で使える実行仕様（handoff）を作る

- Phase 1 の Prefect（server、work pool `research-local`、deployment の仕組み）を使い回す。独立したワークフローエンジンは作らない。
- 同じタスクが重複して実行されないようにする。

## 7. データモデル（Pydantic）

`TaskSpec`、`ExecutionPlan`、`ReviewResult`、`WorkflowDecision`、`BlockedReason`。

`WorkflowDecision` は `ready` / `blocked` / `insufficient_spec` / `transient_failure` を区別する。各判断には、根拠、元の Notion / Linear の参照、取得時刻、計画 ID、実行 ID を紐づける。

## 8. ログ・永続化・コスト管理

計画生成とレビューの実行履歴を、JSON artifact と PostgreSQL に保存する。最低限、次を記録する。

- Task ID と Prefect の flow run ID
- Planner / Reviewer のモデル名
- 開始・終了時刻
- 入力ソースとその版・スナップショット
- 計画とレビューの結果、判断理由
- 入力・出力トークン数
- 推定 API 料金：設定した単価から計算する。単価が分からないときは「推定できない」とし、料金を作り上げない
- エラーとリトライの履歴

## 9. CLI

```bash
research-agent plan --task <TASK_ID>
research-agent plan --next
```

- 既定は read-only / dry-run。外部への書き込みは明示的な許可があるときだけ行う。
- Linear の Status を実際に更新するモードを用意する。
- dry-run では Status の更新、コメントの投稿、研究タスクの変更をしない。

## 10. 安全性と承認

- 研究タスクの仕様を無許可で書き換えない。承認されていない研究実験を始めない。
- 人間の承認と Reviewer の承認を区別する。
- 認証情報や秘密の値をログに保存しない。
- Linear を更新するときは、権限と実行モードを検証する。
- LLM の出力は Pydantic スキーマで検証する。LLM の判断を、根拠なしに実行許可として扱わない。
- 外部 API の一時障害では安全に再試行できるようにする。

## 11. テスト

Fake Connector と Fake Agent を使った単体テストで、次を検証する。

- connector の正常系・異常系
- 仕様不足、依存の未完了、承認不足、Notion と Linear のデータ不整合
- 計画の生成、Reviewer の承認・修正要求・却下、最大2回の再計画
- LLM の不正な構造化出力、API の一時障害
- Linear Status の更新の成功・失敗
- 重複実行の防止、dry-run で外部への書き込みが起きないこと
- 入力ソース・トークン・判断履歴の保存

実際の Notion / Linear を使う統合テストは、安全なテスト用タスクで行う。既存の研究タスクを無断で変更しない。

## 12. Acceptance Criteria

1. 実際の Notion / Linear からタスクの仕様と状態を取得できる。
2. 着手できるタスクを決定論的に選べる。
3. Planner が構造化された実行計画を作る。
4. Reviewer がすべての計画を検証する。
5. 最大2回まで再計画できる。
6. `WorkflowDecision` の4状態を正しく区別できる。
7. 許可されたタスクを Linear の `In Progress` に更新できる。
8. 計画の完了だけを理由に研究タスクを `Done` にしない。
9. 同じタスクの重複実行を防げる。
10. 実行履歴、判断根拠、トークン消費を確認できる。
11. Prefect UI でワークフローの履歴を確認できる。
12. 単体テストと安全な統合テストが成功する。
13. Phase 3 の Implementer に渡せる構造化 artifact が作られる。

## 13. Non-goals

Codex / Claude Code による研究コードの自動実装、研究計算の自律実行、実験結果の自律解析、Linear と Notion の Status Mirror の再実装、独自のワークフローエンジン、研究タスクの無断変更、独立した大規模 Web UI。
