# Phase 5 仕様 — Approval-Gated Autonomous Research Workflow

- 作成：2026-10-10 / Claude Code（ユーザー指示により作成）
- 出典：ユーザーが提示した Phase 5 の実装指示（Objective、§1〜§10）と、2026-10-10 のユーザー回答（§0）。Notion は参照しない。
- 変更はユーザーの承認がある場合のみ。

## 0. 確定事項と調査結果（2026-10-10）

| 論点 | 決定 / 事実 |
|---|---|
| Linear で操作者を判別できるか | Personal API key による更新は**ユーザー本人の操作として記録される**（`actor` がユーザー、`botActor` は null。INU-273 の履歴で確認）。Linear だけでは Autopilot の更新と人間の操作を区別できない |
| 承認者の確定方法 | **Linear の Status で意思を示し、認証済みの Prefect の Human-in-the-loop フォームで最終確定する**（ユーザー回答）。Autopilot は承認用の Status（Approved / Revision Requested / Canceled）に自分で遷移させることはない。フォームは suspend した flow で受け付けるので、待っている間は実行資源を占有しない。Prefect OSS の Basic 認証は共有の資格情報なので、「認証済み」とは auth string の保持者という意味になる（制約として記録） |
| Linear の workflow state | 今あるのは Backlog / Todo / In Progress / Done / Canceled / Duplicate。**Awaiting Approval、Approved、Revision Requested をユーザーが追加する**（ユーザー回答）。却下には既存の Canceled を使う。state が存在しない間は、承認待ちの処理は blocked（作成の依頼を通知する） |
| Notion への書き込み | integration に Insert / Update content の権限をユーザーが付与する。実際に書き込むテストは **[TEST] タスクと、そこから生成された提案・Notes に限る**（ユーザー回答）。Notion の Status は書き換えない（mirror が担当）。Pending（checkbox）と Linear Issue ID は書いてよい |
| Notes DB | `31769893-cf8f-807a-97d8-fc78171790c8`、data source `31769893-cf8f-80d7-8217-000b83f7f15f`。プロパティは 名前（title）、ステータス（status）、作成日（date）、分類（multi_select）、最終更新日 |
| 初期予算 | 研究タスク1件あたり、追加実験を含めた累計で **API $5、計算 4 CPU 時間**（ユーザー回答）。ResearchAuthorization ごとに変更できる |
| デプロイ | Eagle と TSUBAME は Phase 5 の完了後にまとめて行う（2026-10-10 ユーザー決定）。今回は Mac の隔離環境だけで検証する |

## 1. 目的

ユーザーが ChatGPT と相談して Notion Research Tasks と Linear Issue を作った後、Autopilot が計画の精緻化 → 実装 → レビュー → 非同期計算 → 検証 → 科学的な解釈 → 追加実験までを自律的に行う。途中での承認は通常0回を目標とし、重要な科学仕様の変更や予算超過のときだけ承認を求める。結果は Notion Notes と Linear で根拠つきで確認できるようにする。

## 2. 委任と Approval Policy Gate

- `ResearchAuthorization` と `ApprovalPolicy`（schema 1.0）：元の TaskSpec と計画の hash、承認者、目的、研究対象、仮説、invariants、acceptance criteria、許可するパラメータの範囲、環境、累計の予算、提案の件数、連鎖の深さ、有効期限を記録する。
- **`autopilot` ラベルを無条件の包括承認とは扱わない。** ResearchAuthorization がないタスクでは、最初に明示的な onboarding の承認を求める（この初回の委任は「途中の承認」には数えない）。
- 自動で許可するもの：範囲内での計画の具体化、承認済みの条件での実装・レビュー・修正、同じ仕様での再試行、検証に必要な点 / seed / replicate の追加、Reviewer が採択した範囲内の追加実験（件数・深さ・予算の範囲内）。
- 人間の承認が必要なもの：目的や研究対象の拡張、invariants・指標・acceptance の変更や緩和、許可されていないデータの送信、危険なコマンド、main への merge、予算の超過、範囲を機械的に判定できない場合。
- Phase 4 の LLM による評価（within_scope、reviewer accept）は判断材料にとどめる。元の仕様の hash と提案の構造化された差分を決定論的に比較して判断する。

## 3〜8

ユーザーの指示（§3 Orchestration、§5 Linear / ntfy / Prefect、§6 提案のライフサイクル、§7 完了と Notes、§8 運用）に従う。要点：

- 研究タスクの同時実行は1件。polling は5分ごと（承認の確認は1分ごとにできる）。
- Postgres の outbox、冪等キー、照合、dead-letter で重複を防ぐ。
- Done になるのは複数の条件をすべて満たした場合だけ。仮説が否定されても、検証として成立していれば成功とする。
- Mac での検証では本番の研究 Issue に書き込まない。

## 9. Acceptance

ユーザーの指示 §9 に従う。Fake を使った unit / integration / E2E テストと、実際の CLI / LLM / LocalBackend / Prefect を使った統合テストは区別して報告する。Eagle は未デプロイ、TSUBAME は未接続であり、これらを PASS とは扱わない。
