# ResearchAutopilot — Phase 6B: Parent Task DAG Execution

## 1. Objective

Phase 6A の明示的実行開始機構を拡張し、**Notion の親タスクを1件起動するだけで、既存の子タスクを取得・依存関係に従って実行・統合評価できる機能**を実装してください。

対象ケースは以下です。

Parent:
- INU-274 — Phase A: Active-space optimization benchmark / problem diagnosis

Children:
- INU-275 — A1. Benchmark specification
- INU-276 — A2. CASCI reference landscape
- INU-277 — A3. Generic optimizer benchmark
- INU-278 — A4. Beam search query inefficiency diagnosis

ユーザーは INU-274 の Notion Task で Run Autopilot ボタンを押すだけで構いません。

ResearchAutopilot は既存子タスクと依存関係を認識し、実行準備を検証してから、実行可能なタスクを順次または並列に処理します。

A1〜A4 の結果が揃ったら、Phase A の研究目的に対する Completion Gate を評価します。

今回は、ユーザーが登録した既存子タスクの取り込みを対象とします。

新しい研究上の問いを自律的に分解し、子タスクを創出する機能は後続 Phase に回します。

## 2. Prerequisites

- Phase 6A が実装・検証済みである
- 既存 Phase 1〜5 の実行・承認ポリシーが維持されている
- Phase 6A の ExecutionRequest / ExecutionRun を再利用できる

実装前に、現在のコードと Phase 6A の成果物を調査してください。

既存の Research Proposal → child task registration と親子タスク取得を、必要な範囲で共通化してください。

ただし、既存の提案自動実行を破壊しないでください。

## 3. Target UX

1. ユーザーが Notion 上の Phase A 親タスクで Run Autopilot を押す。
2. Notion が Requested になる。
3. Poller が開始要求を取得する。
4. 子タスク A1〜A4 を取得する。
5. 各タスクの科学仕様と依存関係を検証する。
6. Parent DAG を PostgreSQL に保存する。
7. Parent Execution Status を Accepted にする。
8. Prefect Parent Flow を開始する。
9. 実行可能な子タスクをスケジューリングする。
10. 子タスクごとに Phase 2〜5 を実行する。
11. 完了した成果物を依存先のタスクへ渡す。
12. 全子タスクの検証後、親タスクの Completion Gate を評価する。
13. Notion Research Notes に結果を記録する。
14. Linear の親・子タスク Status を更新する。
15. Discord に重要な結果を通知する。

人間が子タスクを1件ずつ Run する必要がないことを目標とします。

## 4. Parent / child discovery

Notion Research Tasks DB の親子 relation を使用して、既存の子タスクを取得してください。

既存の child relation / parent relation を実際の schema から確認してください。

- Notion parent task → children
- Child Notion task → Linear issue
- Parent Linear issue → parent Notion task

これらの対応関係を検証してください。

子タスクが存在しない場合に、LLM が勝手に科学タスクを生成しないでください。

親タスクだけの単一実行モードは Phase 6A のまま維持してください。

階層は将来の多段ネストにも対応できるモデルを採用してください。ただし初期テストでは1階層で十分です。

## 5. DAG construction

依存関係は次の情報を優先して決定してください。

1. Linear `blocked-by` relation
2. 明示的に構造化された task / artifact dependencies
3. 既存の承認済み specification に記述された dependency

タイトルの A1 / A2 / A3 / A4 の番号だけで依存を推測してはいけません。

依存関係が自然言語でしか記載されていない場合、LLM が候補を提案することは許可しますが、確定 DAG として実行する前に必要な検証・承認を行ってください。

DAG は PostgreSQL に version と hash を付けて保存してください。

必要な検証：

- Cycle detection
- Missing dependency
- Cross-project dependency
- Circular parent-child reference
- Duplicate task
- Invalid repository mapping
- Missing implementation specification
- Missing acceptance criteria
- Scope / approval violations

DAG が不正な場合は、安全に Blocked としてください。

## 6. Child task execution

各 child node は Phase 6A の ExecutionRun と既存 Phase 5 の Task Execution Flow を再利用してください。

- Child run ごとに独立 Run ID を持つ
- Child task の科学仕様を固定する
- 既存の Approval Gate を通す
- Phase 2 Planner / Reviewer を呼び出す
- Phase 3 Implementer / Reviewer を呼び出す
- Phase 4 Executor / Analyst / Scientific Reviewer を呼び出す
- Phase 5 Completion / Notes / Linear を呼び出す

子タスクの実行に成功しても、未検証の acceptance criteria が残る場合は依存先を無条件に開始しないでください。

依存 artifact の存在・hash・schema・validation status を照合してください。

### Parallelism

DAG 上で独立なタスクは、利用可能な CPU / RAM / GPU / I/O と ResearchAutopilot の concurrency budget の範囲で並列実行を許可してください。

既存 Phase 5 の research-task concurrency limit が1の場合はその制限を尊重し、必要であれば設定可能な拡張案を示してください。

同一 Git repo / branch / worktree を複数エージェントが安全性確認なしで同時変更しないようにしてください。

複数子タスクが同じ Repository を扱う場合、worktree / base commit / merge strategy を明示的に制御してください。

## 7. Artifact handoff

子タスク間の引き継ぎを、単なるチャット履歴や自然言語 summary に依存させないでください。

以下を構造化して保存します。

- Producer task / run ID
- Artifact ID / path
- Content hash
- Git commit
- Schema version
- Scientific metadata
- Validation status
- Consumer dependency

例えば Phase A では：

- A1：共通 objective / query accounting interface
- A2：CASCI reference table
- A3：regret–query trajectories
- A4：Beam search diagnostics

を後続工程に渡します。

入力に必要な artifact が不足・不正な場合は、そのタスクを Blocked にしてください。

将来の Research Artifact Catalog に接続可能な interface にしますが、今回フル機能の Catalog は実装しません。

## 8. Parent Execution Status

親タスクも Phase 6A と同じ Execution Status を使用してください。

- Requested
- Accepted
- Running
- Awaiting Approval
- Blocked
- Completed
- Failed
- Canceled

親の状態を子の状態から導出するルールを明示してください。

基本方針：

- Parent Accepted：DAG が受理されたが Flow は未開始
- Parent Running：少なくとも Parent Flow が起動し、タスクを管理中
- Parent Awaiting Approval：親全体が人間承認により停止している場合
- Parent Blocked：依存条件不足などで進行できない場合
- Parent Completed：全必須子タスクと親 Completion Gate が通過した場合
- Parent Failed：自動復旧不能な orchestration failure
- Parent Canceled：親の実行が明示的に中止された場合

単一子タスクが承認待ちになっただけで、独立な他の子タスクまで停止する必要はありません。

子タスクごとの Notion Execution Status もそれぞれ同期してください。

## 9. Linear integration

Linear は引き続き Task Status の SSOT です。

親・子の Issue Status を個別に管理します。

例：

| Task | Linear | Execution |
|---|---|---|
| Phase A | In Progress | Running |
| A1 | Done | Completed |
| A2 | In Progress | Running |
| A3 | Todo | Idle |
| A4 | Todo | Idle |

親の Done は、全必須子タスクの Completion Gate と親の Acceptance / Completion Gate を通過した場合のみ設定してください。

親タスクが Running だからという理由で、未開始の全子タスクを In Progress にしないでください。

既存 Linear → Notion Status Mirror は変更しないでください。

## 10. Scientific completion gate

Phase A の目的は、単に A1〜A4 のコードを動かすことではありません。

以下を確認することが必要です。

- Benchmark problem definition が確定している
- 共通 query-accounting interface が成立している
- Reference landscape が検証されている
- Generic optimizer の比較結果が再現可能である
- Beam search の query inefficiency が説明されている
- Phase B への判断材料が得られている

科学的な結果の解釈は既存 Scientific Analyst / Scientific Reviewer を再利用してください。

決定論的 Acceptance checks と LLM による解釈は区別します。

科学仮説が支持されなかったこと自体は Execution Failure ではありません。

Phase B への実際の科学仕様変更や新規研究方針の確定は、既存の Human Approval Policy を維持してください。

## 11. Recovery and idempotency

次を保証してください。

- Parent Run ID と Child Run ID の関連付け
- DAG version / hash の固定
- 再起動後の未完了 DAG 復元
- Completed child の不要な再実行を防止
- Failed / Blocked child の原因保存
- 同じ親 Start Request の二重起動防止
- 既存 Artifact を上書きしない
- 古い child callback が新しい parent run を更新しない
- Notion 同期失敗時の reconciliation
- Discord 通知の重複防止

## 12. Tests

### Unit tests

- Parent-child discovery
- Notion / Linear identity matching
- DAG construction
- Cycle detection
- Missing / duplicate child
- Ambiguous dependency
- Scope validation
- Artifact dependency resolution
- Child run creation
- Parent status aggregation
- Parent completion gate
- Retry / resume
- Duplicate parent start
- Shared repository isolation

### Integration tests

Fake Notion / Linear / PostgreSQL / Prefect / Discord を使用してください。

正常系：

1. Parent Start Requested
2. Parent accepted
3. Existing A1〜A4 discovered
4. DAG validated
5. Child tasks scheduled
6. Child Phase 2〜5 executed
7. Artifacts passed to consumers
8. All mandatory children completed
9. Parent completion gate passed
10. Parent Completed / Linear Done / Notion synchronized

異常系：

- Child missing
- Invalid dependency
- Child blocked by scientific invariant
- Child run failed
- Approval required
- Parent process restart
- Partial DAG recovery
- Duplicate Start Request
- Stale artifact
- Shared repository conflict

Phase A の実タスクについては、Notion / Linear を読み取り専用で取得して DAG 構築を検証してください。

本番の A1〜A4 を実行しないでください。

## 13. Out of scope

今回実装しないもの：

- 自然言語からの新規研究タスク自動分解
- Web / 論文検索
- PDF 読解
- 外部 Git repository の clone
- Trusted Repository Onboarding
- Research Artifact Catalog の全機能
- Eagle / TSUBAME 本番デプロイ
- Phase B の研究実装
- 科学仕様・primary metric・objective の無承認変更

## 14. Deliverables

- Parent Task Intake
- Child discovery
- Validated TaskDAG model
- DAG scheduling / recovery
- Structured artifact handoff
- Parent completion gate
- Parent / Child Execution Status sync
- Linear integration
- Discord event integration
- Unit / Integration test results
- Phase A dry-run validation report
- Updated documentation / development handoff

## 15. Acceptance criteria

Phase 6B の最終完了条件：

1. 親タスク1件の開始要求から既存子タスクを自動取得する
2. 依存関係を検証して DAG を構築する
3. 親と各子タスクの Run を独立して管理する
4. 各子タスクを既存 Phase 2〜5 へ引き渡せる
5. Artifact handoff が再現可能である
6. Parent / Child の Execution Status を Notion に同期する
7. Linear の親・子タスク Status を適切に更新する
8. 子タスクの並列実行と失敗隔離が動作する
9. 再起動・二重投入でも DAG を安全に復元できる
10. Parent Completion Gate の成立を確認して終了する
11. Phase A の実 Task から DAG を dry-run で構築できる
12. 既存 Phase 1〜5 および Phase 6A のテストに regression がない

実装・テスト・文書更新まで完了したら停止してください。

本番研究タスクの起動、Eagle デプロイ、main merge / push は行わないでください。
