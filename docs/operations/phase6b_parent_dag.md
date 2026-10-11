# Phase 6B Parent Task DAG

## 実行契約

親の `Run Autopilot` は Phase 6A と同じ checkbox request を作る。Poller は `サブタスク` relation が空なら単一タスクとして扱い、1件以上なら Parent DAG intake を行う。子タスクを生成することはない。

DAG membership は Notion `サブタスク` / `親タスク`、identity は `Linear Issue ID` と Linear の Notion link を正本とする。実行 edge は Linear `blocked-by`、構造化 dependency の順で優先する。edge がない複数子タスクでは Dependency Planner が scientific specification、input/artifact requirements、acceptance criteria、他の承認済み section から最小 DAG を提案する。タイトルの A1/A2 等の番号だけから順序を推測しない。

推論結果は直ちに実行しない。task ID、self/duplicate edge、cycle、source specification に実在する evidence、required artifact を決定論的に検証し、DAG version/hash、edgeごとの rationale/evidence/artifact contract を PostgreSQL に固定する。その exact hash を Discord の `dag` approval に提示し、承認された場合だけ Parent Flow を開始する。待機中に親子 relation または任意の specification hash が変われば承認を stale として Blocked にする。Linear `blocked-by` への書き戻しは行わず、PostgreSQL の承認済み DAG snapshot を Execution SSOT とする。

intake は cycle、DAG 外 dependency、project、identity、scope label、canonical Specification / Acceptance Criteria / Repository を検証する。不正 DAG も hash 付き監査 record として保存するが、ExecutionRun や子 Workflow は開始しない。Dependency Planner の不在、料金未設定、invalid output、根拠のない edge も fail-closed で Blocked にする。

## 永続化と回復

- `autopilot_task_dags`: versioned DAG snapshot、spec hash、DAG hash、validation problems。
- `autopilot_parent_runs`: parent ExecutionRun、固定 DAG、child ExecutionRun / WorkflowRun の対応。
- `autopilot_artifact_handoffs`: producer/consumer、path、SHA-256、commit、schema、scientific metadata、validation status。
- 既存 `autopilot_execution_requests` / `autopilot_execution_runs`: 親・子の双方に使用。子 request source は `parent_dag`。

Poll ごとに非終端 ParentRun を先に復元する。Completed child は再作成せず、DAG hash と child request key により重複を拒否する。依存 artifact は scheduling 時と Phase 2 開始直前の両方で path / SHA-256 / validation status を検証し、変更・削除されていれば child を Blocked にする。

親 status は次のように導出する。

| 条件 | Parent Execution Status |
|---|---|
| DAG accepted、parent poll flow 起動済み | Running |
| 独立 child が一部 approval 待ち、他が進行可能 | Running |
| 残りが approval 待ちだけ、または parent completion review 待ち | Awaiting Approval |
| child に unrecoverable failure | Failed |
| child/dependency/artifact が進行不能 | Blocked |
| 全 mandatory child Completed + parent Completion Gate | Completed |
| verified parent completion rejection | Canceled |

親 Completion Reviewer が deterministic artifact evidence から全 criterion を確立できない場合、既存 `COMPLETION_REVIEW` を Discord approval に送る。Linear status 変更だけでは承認にならない。

## 並列性と repository isolation

`RA_AUTOPILOT_MAX_CONCURRENT_TASKS` は単一タスクと DAG child が共有する global slot 数で、既定値は `1`。同じ Repository の node は slot が複数でも同時 schedule しない。Phase 3 自身も現在は repository task concurrency 1 を強制するため、Eagle で値を増やす前に worker、worktree、base commit、merge strategy、CPU/RAM/GPU/I/O budget を検証する。

Dependency Planner は `RA_DAG_INFERENCE_MODEL` を使用し、未設定時は `RA_REVIEWER_MODEL` を使用する。従量課金呼び出しは `RA_MODEL_PRICING` に単価がないモデルでは拒否される。

## Phase A read-only dry-run（2026-10-11）

外部サービスへ書き込まず、INU-274 と INU-275〜278 を実 connector と実 validator で取得した。

- Notion schema: `親タスク` / `サブタスク` は同じ Research Tasks data source への relation。INU-274 から4子、各子から INU-274 への逆 relation を確認。
- Identity / project: 5件とも Notion page は1件、Linear Project ID は一致。
- Linear dependency: A1〜A4 の `blocked-by` は全て空。現行 source data は inference 前の必須 specification gate を満たさないため、LLM による番号順補完や本番実行は行わなかった。
- DAG snapshot: 4 node、親と全子の spec hash を含む hash `bbfd39b08cf64bb85b11de11e56ed9b3038f2dc34519d1e95ed7d7c54152237a`、status `blocked`。
- Block reasons: 親の canonical Acceptance Criteria がない。各子に `autopilot` label、canonical Specification、Repository がない。既存 A1〜A4 は目的や個別評価条件を含むが、実装可能な canonical section としては不足している。

従って現状の Phase A button を押しても安全に Blocked となる。本番 A1〜A4 は実行していない。実行可能にするには、人間が科学仕様を変更・承認したうえで、不足 section と Linear `blocked-by` を設定する必要がある。これは本実装では自動補完しない。

## 検証

Fake integration は親 request、DAG intake、4 child の既存 Phase 2〜5、順次 dependency release、artifact handoff、parent Completion Gate、Research Notes、Linear Done、Notion Completed を通す。追加単体テストでは inference proposal、Discord 承認前の実行禁止、承認後の開始、source spec 変更による stale rejection、unknown task、cycle、根拠なし edge を検証する。

Eagle / TSUBAME deploy と実 Phase A execution は未実施。
