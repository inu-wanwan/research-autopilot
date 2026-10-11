# Phase 6B Parent Task DAG

## 実行契約

親の `Run Autopilot` は Phase 6A と同じ checkbox request を作る。Poller は `サブタスク` relation が空なら単一タスクとして扱い、1件以上なら Parent DAG intake を行う。子タスクを生成することはない。

DAG membership は Notion `サブタスク` / `親タスク`、identity は `Linear Issue ID` と Linear の Notion link、実行 edge は Linear `blocked-by` を正本とする。タイトルの A1/A2 等から順序を推測しない。自然言語の dependency は executable edge に昇格せず `ambiguous_dependency` で Blocked にする。

intake は cycle、DAG 外 dependency、project、identity、scope label、canonical Specification / Acceptance Criteria / Repository を検証する。不正 DAG も hash 付き監査 record として保存するが、ExecutionRun や子 Workflow は開始しない。

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

## Phase A read-only dry-run（2026-10-11）

外部サービスへ書き込まず、INU-274 と INU-275〜278 を実 connector と実 validator で取得した。

- Notion schema: `親タスク` / `サブタスク` は同じ Research Tasks data source への relation。INU-274 から4子、各子から INU-274 への逆 relation を確認。
- Identity / project: 5件とも Notion page は1件、Linear Project ID は一致。
- Linear dependency: A1〜A4 の `blocked-by` は全て空。したがって executable edge は0件であり、A番号から補完しなかった。
- DAG snapshot: 4 node、親と全子の spec hash を含む hash `bbfd39b08cf64bb85b11de11e56ed9b3038f2dc34519d1e95ed7d7c54152237a`、status `blocked`。
- Block reasons: 親の canonical Acceptance Criteria がない。各子に `autopilot` label、canonical Specification、Repository がない。既存 A1〜A4 は目的や個別評価条件を含むが、実装可能な canonical section としては不足している。

従って現状の Phase A button を押しても安全に Blocked となる。本番 A1〜A4 は実行していない。実行可能にするには、人間が科学仕様を変更・承認したうえで、不足 section と Linear `blocked-by` を設定する必要がある。これは本実装では自動補完しない。

## 検証

Fake integration は親 request、DAG intake、4 child の既存 Phase 2〜5、順次 dependency release、artifact handoff、parent Completion Gate、Research Notes、Linear Done、Notion Completed を通す。cycle、missing dependency、identity/project/spec、duplicate parent start、restart recovery、stale artifact を fail-closed で検証する。

Eagle / TSUBAME deploy と実 Phase A execution は未実施。
