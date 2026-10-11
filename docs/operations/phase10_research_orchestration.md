# Phase 10 Autonomous Research Planning & Orchestration

仕様は `docs/phase_specs/phase10.md`（ユーザー提供の指示＋正本 Notion INU-288 の該当節＋ユーザー決定）。

## 実装計画（2026-10-11）

既存 Orchestrator（Phase 5 engine）の拡張として実装する。新しい Orchestrator・承認システム・状態管理は作らない。

| 責務 | 実装 | 再利用する既存機能 |
|---|---|---|
| 起動 | Run Autopilot（6A）で研究目的タスクを受理。研究目的の見出しを持つタスクを research objective として扱う | 6A ExecutionRequest / ExecutionRun、engine.poll |
| Research Planner（10A） | 研究スコープの構造化（各項目に原文引用を要求し決定論的に照合。引用のない値は確定しない）→ Evidence / Artifact Context（Phase 7 Catalog・Phase 8 レポート・Phase 9 Registry）→ 根拠付き計画と Task DAG draft（決定論的検証） | Phase 8 の引用照合方式、Catalog / literature / external の照会 |
| ResearchAuthorization | Phase 5 の `ResearchAuthorization` に version・内容 hash・研究スコープ・計画 ID を追加。旧版は superseded | Phase 5 authorization / policy gate |
| 承認 | `ApprovalKind.RESEARCH_SCOPE`。承認対象 hash＝計画内容 hash（Notion 仕様本文 hash を含む）。古いボタン・版違い・仕様編集後は stale | ApprovalController（Discord 本人確認・期限・二重押下防止）、Linear Awaiting Approval |
| Notion | 計画は研究目的ページ内のサブページ「RA Research Plan vN」（不変、版ごとに新規）。`Approval Status` は承認イベントの projection（失敗時は再試行、表示を根拠に実行しない） | NotionWriter |
| 実行（10B） | 承認後、サイクルごとに子タスク（Notion：Origin=RA・親タスク relation、Linear：blocked-by）を作り、6B の ParentRun / TaskDAG で実行 | 6B dag_runtime（schedule_ready・artifact handoff）、Phase 2〜4 の各タスク実行 |
| Research Evaluator / Replanner（10B） | サイクル完了時に検証済み結果から次の行動（追加実験・補助解析・再試行・完了・停止・スコープ変更案）を根拠付きで決定。決定論的ゲートで承認範囲（条件範囲・件数・サイクル数・予算・不変条件）を確認し、範囲外は変更案（新しい版）として Discord 承認待ち | Phase 4 Analyst の結果、policy / budget |
| 予算 | chain 予算（Phase 2〜4・文献・外部導入）に計画／評価の LLM 費用を加算 | autopilot.budget.chain_usage |
| 停止・再開 | Discord の停止／再開ボタン（承認者のみ）。停止は新規投入停止、実行中ジョブは完了まで | Discord gateway の本人確認 |
| 通知 | 承認依頼・マイルストーン（計画承認・サイクル評価）・成果・異常のみ。研究の子タスクごとの通知は抑止 | notify_once / Discord ルーティング |

テスト：10A（根拠付き計画・不足情報を確定しない・承認前実行拒否・stale 承認拒否）、10B（複数サイクル・範囲外／仕様変更／予算超過の拒否・停止／再開／再試行での整合性）、共通（Notion / Discord / PostgreSQL / Linear の整合性）。E2E：10A は Phase A の研究目的で計画のみ、10A＋10B は INU-292（H₂ RHF–FCI）。
