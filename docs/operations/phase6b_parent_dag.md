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
| child が Failed/Blocked だが、独立 sibling が進行中または ready | Running（残りが approval 待ちだけなら Awaiting Approval） |
| child に unrecoverable failure があり、他に進行可能な node がない | Failed |
| child/dependency/artifact が進行不能で、他に進行可能な node がない | Blocked |
| 全 mandatory child Completed + parent Completion Gate | Completed |
| verified parent completion rejection | Canceled |

失敗隔離：Failed/Blocked child の依存先（推移的な依存先を含む）は開始しない。依存関係のない sibling は既存の slot / repository 制約のもとで実行を続け、親は進行可能な node がなくなった時点で終端化する。終端時の親 `Execution Error` には各 Failed/Blocked/Canceled child の task ID と `error_summary` を記録する。Awaiting Approval の親で child が失敗した場合は Running を経由して Failed に遷移する（Phase 6A の状態遷移表は変更しない）。

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

## Phase A 準備状況（2026-10-11 / Claude Code、ユーザー許可済み）

- 書き直された Phase A を read-only dry-run：親 Acceptance Criteria、子の `autopilot` label / Specification は充足。残りは Repository 見出し（子は `Repository / execution environment` という非 canonical 見出しのみ）、Registry 未登録、親 `autopilot` label 欠落だった。
- ユーザー許可のもと反映：INU-275〜278 の Notion ページ末尾に `## Repository` / `active-space-research` を追記。INU-274（Linear）に `autopilot` label を追加。`config/research_repos.toml`（Git 管理外）に `active-space-research` を登録（path `~/workspace_callduck/sekijima_lab_callduck/active_space_research`、base `main`、protected `data/**` `outputs/**` `notebooks/**`、test は研究 repo の AGENTS.md に従い `docker compose -p active-space-ra run --rm -T research pytest -q -p no:cacheprovider`、timeout 900s）。test command は研究 repo main `b53f807` で 280 passed を確認。`ruff check .` は既存 72 errors のため lint_command は未登録。
- 再 dry-run（registry 検証込み）：DAG `valid`、hash `30722d5c…3364`、4 node とも repo `active-space-research`、blocked-by 0 件のため Dependency Planner 対象。その後 intake に Allowed files 必須チェックを追加したため、Allowed files 設定までは `missing_allowed_files` で Blocked になる。
- 未設定（人間の判断待ち）：各子の `Allowed files`（ページ上も「未確定・未承認」）と、A1〜A4 の Linear blocked-by（任意。未設定なら LLM 提案 + Discord 承認）。本番投入は全 Phase 完了後にユーザーが行う方針。

## Phase A 相当の E2E（INU-324、2026-10-12 / Claude Code、ユーザー委任）

Notion / Linear に `[E2E]` コピー（親 INU-324、A1 INU-325 / A2 INU-326 / A3 INU-327 / A4 INU-328）を作り、承認は dev 限定 CLI（`research-agent research approve`、`cli-dev:claude-code`）で代行した。E2E コピーの仕様に足りない情報はユーザー許可のうえ補った（`Allowed files`、`Input artifacts`、評価規則・整合確認・tie 規則・optimum 定義。各節に「E2E 補完」と明記。本番 Phase A の仕様ではない）。

結果（20:1x JST）：A1・A2 は完了（A1：81 条件で oracle が Phase 4 cache を再生、A2：81 条件の reference table）。A3 は科学的な理由で停止（承認済み cache に Greedy/Beam の途中評価と Entropy/NOON の参照がなく、新規 CASCI か仕様変更が必要。研究者判断待ち）。A4 は A3 待ち。

結果（2026-10-13 03:25 JST、最終）：**親 INU-324 は Done**（A1〜A4 すべて Completed、親 Notes 作成、Linear Done）。A3・A4 はユーザー許可のもと新規 CASCI を使える条件で実行（実際の新規 CASCI は 0 件）、Entropy/NOON Top-k は Mac の Docker 上限 8 GB に参照計算が載らないためユーザー判断で保留。A3 の入力整合チェックは軌道エネルギーを完全一致で比べていて 1e-14 Eh の浮動小数点差で止まったため、ユーザー承認のうえ 1e-10 Eh 許容に変更（E2E コピーの仕様に追記）。A3：81 条件×8 手法、Greedy は Phase 4 と 81/81 一致、最適到達は Beam-8 74・BOCS 68・Beam-4 65・Greedy 52（query 予算が揃っていないので効率比較は未結論）。A4：数値チェック 14 件合格、beam 幅 2/4/8 の query 削減上限 0.146/0.307/0.443。科学的な結論は人が検証していない。親の Completion Gate は「Gate の判断材料を Notes に記録」を決定論的に確立できず COMPLETION_REVIEW に回り、CLI で承認した（親 Notes は子の状態と Gate 理由だけで、判断材料は各子の Notes にある）。

DAG の edge に関する教訓：Linear に依存が 1 本でもあると推論しないため、edge を足すときは全部を Linear に明示する。DAG 承認前に edge を必ず確認する（v16 で推論から A3→A4 が落ちたのを見落として承認し、A4 が先に走った）。

見つけて直した仕組みの不具合（すべて commit・テスト済み）：

| 症状 | 修正 |
|---|---|
| DAG 推定の根拠がラベル付き引用・JSON エスケープで照合失敗 | `6fc6cc8`：生の文字列で照合、引用部分を検証、言い換えは除外 |
| 推定失敗を「Notion を直せ」と案内 | `e1aeb09` |
| 兄弟が動いている間に止まった子が approvals に出ない | `9fc8acd` |
| catalog の members digest を `sha256=` と表示し実装者が誤解 | `2853508`：`file_sha256` を manifest に追加 |
| 止まった親の再実行で子が In Progress のまま intake 拒否 | `98b48ce` |
| container 内でワークツリーの .git が見えない／Claude がテストを実行できない | `55e558b` |
| Claude サブスクの名目費用を API 予算に計上・1 回 $2 で停止 | `e363a16` / `039d0fa`（ユーザー確認済み） |
| 一時的な空きメモリ不足で即停止 | `8740124` / `dbddbb6`：最大 30 分待つ |
| container の provenance で相対パスが二重化 | `d267f02` |
| 親の再実行で止まった子を最初からやり直す | `75367fb` / `9a40d2d` / `1df9241`：Phase 3 の続き、Phase 4 だけ 1 回、completing から再開 |
| DoD の引用（複数箇所・和欧間の空白） | `db946af` |
| Codex 上限時に構造化呼び出しが API へ | `73041b1`：Codex → Claude サブスク → API |
| container モードで RA_CATALOG_INPUTS をホストに要求 | `cded2a1` |
| 出力内の units 辞書を値として読む | `42dbea4` |
| container ジョブなのにホストの空きメモリで判定 | `ff9b4a6`：Docker VM のメモリで判定 |
| 推定 edge の説明文を artifact id として照合（2 か所） | `afc3655` / `9458d92` |
| 親の再実行で完了済みの子も再実行（手順の「再利用」と矛盾） | `a037b68`：仕様が同じなら引き継ぐ |
| Notes の表が 100 行超で Notion 400、101 ブロック目以降が黙って欠落 | `1df9241` |
| 仕様が実装に委ねた事項を Plan Reviewer が計画で要求 | `ccb37cb` |
| トークン上限に cache read を含めて数える | `72e05f7`（ユーザー決定：cache read を除く、上限 3M は維持） |
| 無視済みの noise パスの除外で `git add` が exit 1 | `d555d61` |
| container モードで host の memory reserve を Docker VM に二重に課す／安全係数だけで上限超過して block | `5969ae8`：初回は推定が上限内なら上限を割り当てる |
| 親の再実行で adopt した完了 child の成果物が schedule 判定より後に capture され「missing or stale」 | `18f4f3d`：schedule 前に capture、upstream 入力に `file_sha256` |
| units チェックが `definitions` の説明文（文字列の辞書）を値として読む | `a8a4f0f` |

運用上の注意：Discord gateway（`discord run`）は起動時のコードで動くので、コード更新後に再起動する。Codex サブスクは構造化呼び出しもサブスクに回すと 1〜2 時間で上限に達する（回復まで約 5 時間）。この間は Claude サブスクで進む。

## 検証

Fake integration は親 request、DAG intake、4 child の既存 Phase 2〜5、順次 dependency release、artifact handoff、parent Completion Gate、Research Notes、Linear Done、Notion Completed を通す。追加単体テストでは inference proposal、Discord 承認前の実行禁止、承認後の開始、source spec 変更による stale rejection、unknown task、cycle、根拠なし edge を検証する。

Eagle / TSUBAME deploy と実 Phase A execution は未実施。
