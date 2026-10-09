# Development Handoff

Codex / Claude Code が作業を再開するときに最初に読む引き継ぎ記録です。
**進捗の記録であり、仕様の正本ではありません**（仕様は `docs/phase_specs/` とユーザーの指示）。

更新ルール（詳細は `AGENTS.md` §8）：

- 重要な実装単位の完了時、セッション終了・中断前、ブロッカー発生時に必ず更新する。
- 事実のみを書き、未確認のことは「未確認」と書く。
- テスト結果は、実際に実行したものだけを、実行コマンドと結果つきで書く。
- 未解決の Known Issues は解決するまで消さない。

---

## Current Phase

Phase 2（Task Manager / Orchestrator）実装完了。MacBook のローカル Compose と実際の Notion / Linear / OpenAI で統合テスト済み。研究室サーバー（Eagle）への反映はまだ。

## Current Task

Phase 2 の実装・テスト・統合テスト

## Status

完了。AC は下表のとおり。Eagle での反映（worker の作り直しが必要）が残っている。

## Last Updated

2026-10-10 / Claude Code

## Git Branch / Commit

- `phase2/task-manager` を `main` に merge し、push 済み（ユーザー許可あり）。主な commit：
  - `140a1ef` Phase 2 仕様書
  - `a611732` 実装（Task Manager、agents、orchestrator、CLI、テスト）
  - `ae61351` compose / prefect.yaml の配線とドキュメント
  - `1f20b04` プレースホルダーだけの仕様を不足と判定する修正
  - この handoff の commit

## Completed Work

### Phase 2 実装（2026-10-10 / Claude Code）

- 仕様書は `docs/phase_specs/phase2.md`。ユーザーと合意した事項：
  - 承認：Linear Todo かつ Notion Pending=false
  - スコープ：ラベル `autopilot`
  - 依存：Linear の blocked-by
  - モデル：Planner `gpt-5.6-terra`、Reviewer `gpt-5.6-sol`
  - Eagle：Windows + WSL2
- コード（`src/research_autopilot/`）
  - `tasks/`：Notion connector（読み取り専用、API 2025-09-03、見出しを正規の項目に対応づける）、Linear connector（GraphQL、限定的な再試行）、決定論的なゲート、TaskManager（Notion と Linear の対応確認、`--next` の選択）
  - `planning/`：Agents SDK の Planner / Reviewer（Pydantic の strict 出力、エラーを3種類に分類）、再計画ループ（毎回レビュー、最大2回）、コスト推定（単価が分からなければ null）
  - `orchestrator/`：`core.plan_task`（リース、最後のゲート再確認、apply 時だけ Linear を In Progress にする。started 以外の state へは更新しない）、Prefect flow `plan-task`（markdown artifact を作る）
  - `storage/`：SQLAlchemy の `plan_runs` / `task_leases` / `linear_status_audit`、JSON 記録、Phase 3 用の handoff JSON
  - `cli.py`：`research-agent plan|runs|db`
  - `models.py`：TaskSpec、ExecutionPlan、ReviewResult、WorkflowDecision、BlockedReason など
- インフラ：worker に Phase 2 の環境変数と `RA_DATABASE_URL`（同じ PostgreSQL 上の別 DB `research_autopilot`）を追加。`prefect.yaml` に deployment `plan-task/manual` を追加（既定は dry_run）。
- ドキュメント：`docs/operations/phase2_planning.md`、README、AGENTS、docs/README。
- 依存の追加：`openai-agents`、`sqlalchemy`、`psycopg[binary]`。

### Phase 2 事前設定（2026-10-10 / Claude Code + ユーザー）

- 詳細と結果は `docs/operations/phase2_prerequisites.md`。
- `.env.example` に `NOTION_API_TOKEN` / `NOTION_TASKS_DATA_SOURCE_ID` / `LINEAR_API_KEY` / `LINEAR_PROJECT_ID` / `OPENAI_API_KEY` を追加した。秘密でない ID 2つは確認済みの値を記入。
- ユーザーが Mac の `.env` に秘密の値を設定した。`LINEAR_PROJECT_ID` はユーザーの承認を得て、正式な UUID に置き換えた。
- 接続確認：Notion PASS、Linear 読み取り PASS、Linear 書き込み PASS（テスト用 issue INU-270 だけを変更し、元に戻した）、OpenAI PASS（クレジット追加後）。
- Phase 2 の仕様（Notion ページ、INU-265）を API で読めることを確認した。`docs/phase_specs/phase2.md` はまだ作っていない（Phase 2 の実装依頼時に作る）。
- 新しい機能、依存、Compose の変更はしていない。worker も再起動していない。

### Phase 1 実装（2026-10-10 / Claude Code）

- `docs/phase_specs/phase1.md` を作成した（ユーザー指示「phase1 の要件は claude 側で作成」による）。ユーザー提示の Phase 1 指示 §1, §3〜§8, §10 を整理したもので、要件の追加や変更はしていない。
  - なお `docs/phase1_technical_design.md` §1 の「phase1.md は存在しない」という記述は Codex の文書なので書き換えていない。現在はこのファイルが存在する。
- Python（`src/research_autopilot/`）
  - `flows/smoke_test.py`：次の5つの flow。各 flow に completed / failed / crashed の通知 hook を付けた。
    - `smoke-success`：冪等な副作用付き
    - `smoke-failure`：`TransientError` に限り最大2回 retry（指数 backoff + jitter）
    - `smoke-timeout`：task timeout 2秒
    - `approval-demo`：通知してから `pause_flow_run`
    - `smoke-sleep`：復旧訓練用
  - `idempotency.py`：`apply_once`。tmp ファイルを書いてから `os.link` でアトミックに作成し、payload が食い違えば検出する。
  - `notifications/`：型付きの `NotificationEvent`（例外メッセージは入れず型名だけ）、ntfy 互換の `HttpNotifier`（timeout 5秒、5xx と通信エラーのみ最大2回 retry）、`LoggingNotifier`（未設定時）、`safe_send`（例外を外に出さない）、Prefect hook。
  - 依存に `httpx>=0.28.1` を追加（直接 import しているため）。
- インフラ
  - `infra/compose.yaml`
    - `postgres:16.15`：named volume、host にポートを出さない
    - `prefecthq/prefect:3.8.8-python3.12` の単一プロセス server：`127.0.0.1:4200` のみ、Basic auth、CSRF 有効
    - process worker：`research-local` pool、`--limit 1`、`--with-healthcheck`、非 root、docker.sock なし
    - 共通：healthcheck、`depends_on: service_healthy`、`restart: unless-stopped`、ログのローテーション
  - `infra/Dockerfile`：uv 0.12.18 を使い、`uv sync --frozen --no-dev` でコードを `/opt/research-autopilot` に焼き込む。uid 10001 で実行する。
  - `infra/automations/crash-zombie-flow-runs.yaml`：heartbeat が 540 秒途絶えた run を Crashed にする。
  - `prefect.yaml`：5つの deployment。version は `GIT_SHA`。スケジュールは inactive で登録する。
  - `.env.example`、`.dockerignore` を追加。
- テスト（`tests/`、25件）：冪等性（逐次、16スレッド同時、payload の食い違い、不正な ID）、通知（payload、token の秘匿、retry、4xx を再送しない、失敗の隔離、設定の validation）、flow（成功、重複実行、retry 後の失敗、retry での回復、timeout、通知失敗が state に影響しないこと、sleep）。
- ドキュメント：`README.md`、`docs/operations/{deployment,monitoring,recovery}.md`。`docs/README.md` と `AGENTS.md` の構成図を更新した。

### Phase 1 技術調査・設計（2026-10-10 / Codex）

- ユーザーが提供した Claude Code 向け指示の Section 3〜8 から Phase 1 要件（FR-01〜05、NFR、AC-01〜10）を読み取った。
- ユーザー指示により `docs/phase_specs/phase1.md` は追加していない。実装に必要な要件は `docs/phase1_technical_design.md` 内の「Phase 1 実装契約」に収録した。
- Prefect、Tailscale、Docker、PostgreSQL の現行公式ドキュメントを調査し、公式 URL とともに `docs/phase1_technical_design.md` に記録した。
- 確認済み事実、設計上の推奨、未確認 / 制約を区別した。
- Phase 1 の最小構成を `postgres` + 単一 process `prefect-server` + Process `prefect-worker` の3サービスとし、Redis 追加条件を明示した。
- container 内 Worker API URL、browser 用 UI API URL、MacBook 用 API URL、host-local URL を分離した。
- Tailscale Serve + localhost port bind + grants + Prefect Basic Auth を推奨し、Funnel と public port publish を使わない設計とした。
- retry と exactly-once の違い、PostgreSQL metadata と result storage の違い、container restart と in-flight Run recovery の違いを明記した。
- Compose 実装契約、障害モード、冪等性、通知分離、backup / restore、テストマトリクス、要件トレーサビリティを設計した。

### 開発基盤（2026-10-09 / Claude Code）

- リポジトリの現状を確認。
- `AGENTS.md`（共通開発ルール）、`CLAUDE.md`（`@AGENTS.md` のみ）、`docs/README.md`、`docs/development_handoff.md` を作成。
- パッケージ名を `research_autopilot` に統一（ユーザー指示）：
  - `src/researchautopilot/__init__.py` を `src/research_autopilot/__init__.py` に移動し、`src/researchautopilot/` を削除。
  - `pyproject.toml`：プロジェクト名 `researchautopilot` → `research-autopilot`、script を `research-autopilot = "research_autopilot:main"` に変更（uv_build が `research_autopilot` モジュールを自動解決するため）。
  - `uv sync` により `uv.lock` 内のプロジェクト名も更新。依存パッケージの追加・削除はなし。
- `.gitignore` を追加（Python キャッシュ、build 生成物、`.venv/`、テスト/lint キャッシュ、`.env` / `.env.*`（`.env.example` は除く）、`.prefect/`、ログ、OS/エディタ設定）。
- 初回 commit（ユーザー許可済み）。

### リポジトリ情報（2026-10-09 時点）

- `pyproject.toml`：name `research-autopilot`、Python `>=3.12`、build backend `uv_build`
  - dependencies：`prefect>=3.8.8`, `pydantic>=2.14.0`, `typer>=0.27.3`
  - dev：`pytest>=9.1.1`
  - script：`research-autopilot = "research_autopilot:main"`
- `.python-version`：3.12
- `src/research_autopilot/__init__.py`：雛形の `main()`（Hello 出力のみ）
- `src/research_autopilot/flows/__init__.py`：空
- `tests/`, `data/`, `infra/`：空ディレクトリ（Git は空ディレクトリを追跡しないため commit には含まれない）
- `README.md`：空
- OpenAI Agents SDK、Docker Compose、PostgreSQL 関連のファイル・依存：なし

## Work In Progress

なし。

## Remaining Work

- **Eagle への反映**（`phase2_planning.md` §5）：
  - サーバーの `.env` に Phase 2 の変数を設定する
  - `createdb research_autopilot` を実行する
  - worker イメージを作り直し、worker を再作成する（**実行中の run がないことを確認し、ユーザーの承認を得てから**）
  - `research-agent db init`、MacBook から `prefect deploy --all`
  - Eagle の worker で plan-task の check-only を確認する
- テストデータの後片付け（ユーザー判断）：INU-273 は apply テストで In Progress になっている（Notion のテストページも Mirror で同じになる）。INU-271 / 272 / 273 と Notion のテストページ、ラベル `autopilot` を残すかどうか。
- `RA_MODEL_PRICING` に gpt-5.6-terra / sol の単価を設定する（未設定のあいだ、推定料金は n/a になる）。
- Phase 1 の残り：本番値での zombie 検出の実測、ntfy、バックアップの保存先と定期実行。
- 研究タスクを Autopilot の対象にするには、Linear に `autopilot` ラベルを付け、Notion の本文に Purpose / Implementation / Acceptance criteria を書く必要がある（既存タスクは見出しがばらばらで、多くが insufficient_spec になる）。

## Changed Files

- 新規：`docs/phase_specs/phase1.md`, `src/research_autopilot/idempotency.py`, `src/research_autopilot/flows/smoke_test.py`, `src/research_autopilot/notifications/{__init__,events,http,hooks}.py`, `tests/{conftest,test_idempotency,test_notifications,test_smoke_flows}.py`, `infra/{compose.yaml,Dockerfile}`, `infra/automations/crash-zombie-flow-runs.yaml`, `prefect.yaml`, `.env.example`, `.dockerignore`, `docs/operations/{deployment,monitoring,recovery}.md`
- 変更：`pyproject.toml`, `uv.lock`（httpx を追加）、`README.md`、`AGENTS.md`（構成図）、`docs/README.md`（構成図・役割表）、本ファイル

## Tests and Verification

### Phase 2（2026-10-10、MacBook）

| 確認 | 結果 |
|---|---|
| `uv run pytest` | **116 passed**（Phase 1 の 25 件、Phase 2 の 91 件） |
| 実データでの読み取り（INU-265 / 261 / 239 / 243） | ゲートが期待どおりに判定した（スコープ外、In Progress、Backlog と Pending、見出しの不足） |
| ローカル Compose の worker：DB `research_autopilot` 作成、`db init`、deploy、`plan-task` の check-only（INU-265） | blocked。PostgreSQL に記録された |
| 統合テスト INU-271（仕様不足） | insufficient_spec（プレースホルダーを検出、LLM 呼び出しなし） |
| 統合テスト INU-272（Pending） | blocked / not_approved（LLM 呼び出しなし） |
| 統合テスト INU-273 の dry-run（worker 上） | ready。Planner 21秒 / Reviewer 7秒で approved、入力 4,639 / 出力 2,625 トークン、handoff JSON を作成、Linear は変更なし |
| `--next` の check-only | 271 と 272 を飛ばして INU-273 を選んだ |
| INU-273 の apply（ユーザー承認あり、Mac の CLI → Prefect） | ready、Linear が Todo → In Progress（updated）、監査ログ1行 |
| INU-273 の apply 2回目 | blocked / invalid_status。書き込みなし（監査ログは1行のまま） |

まだ確認していないこと：Eagle での動作、ntfy への通知、単価を設定したうえでの料金推定（単体テストでは確認済み）、Reviewer の revision_required → 再計画の実 LLM での発生（単体テストでは確認済み）。

### Phase 2 Acceptance Criteria

| # | 条件 | 判定 |
|---|---|---|
| 1 | 実際の Notion / Linear から取得できる | Passed |
| 2 | 着手できるタスクを決定論的に選べる | Passed（`--next`） |
| 3 | Planner が構造化された計画を作る | Passed |
| 4 | Reviewer がすべての計画を検証する | Passed（実行時と単体テスト） |
| 5 | 最大2回まで再計画できる | Passed（単体テスト。実 LLM では再計画が起きなかった） |
| 6 | 4状態を区別できる | Passed（ready / blocked / insufficient_spec は実データ、transient_failure は単体テスト） |
| 7 | 許可済みのタスクを In Progress にできる | Passed（INU-273） |
| 8 | 計画の完了だけで Done にしない | Passed（state type が started 以外への更新を拒否。テストあり） |
| 9 | 重複実行を防げる | Passed（リース、apply の2回目が blocked） |
| 10 | 履歴、根拠、トークン数を確認できる | Passed（`runs list/show`、PostgreSQL、JSON） |
| 11 | Prefect UI で履歴を確認できる | Passed（ローカル Compose の UI / API に flow run と artifact がある。Eagle の UI では未確認） |
| 12 | 単体テストと安全な統合テストが成功する | Passed |
| 13 | Phase 3 用の構造化 artifact が作られる | Passed（`handoffs/<task>/<plan_id>.json`、schema_version 1） |

### 研究室サーバーでの確認（2026-10-10、ユーザー実施・報告）

ユーザーが `main`（`e623fcb`）を研究室サーバーに pull し、運用ドキュメントの手順で次を確認したと報告した。手順とドキュメントの間に食い違いはなかったとのこと。エージェントはサーバーに直接アクセスしていないので、個々のコマンド出力は見ていない。

- Compose の3サービスの起動、worker による deployment の実行（AC-03 / 04）
- MacBook から Tailscale 経由での登録・起動（AC-05）
- スマートフォンから Tailscale 経由で UI の状態・ログを確認（AC-06）
- 再起動後の履歴保持、バックアップと復元（AC-07 / 10）

まだ確認されていないこと：本番値（180 秒 / 540 秒）での zombie 検出の実測、実際の ntfy サーバーでの通知、未承認経路からの負の確認の個別結果、サーバーの OS / CPU。

### Acceptance Criteria

| ID | 判定 | 根拠 |
|---|---|---|
| AC-01 | **Passed** | `uv run pytest` 25 passed（MacBook、merge 後の main でも再実行） |
| AC-02 | **Passed** | MacBook で正常系・異常系の flow を実行（ローカルの一時サーバーとローカル Compose の両方） |
| AC-03 | **Passed** | 研究室サーバーで確認（ユーザー報告）。MacBook の Compose でも healthy を確認済み |
| AC-04 | **Passed** | 研究室サーバーで確認（ユーザー報告） |
| AC-05 | **Passed** | MacBook → Tailscale → 研究室サーバーで確認（ユーザー報告） |
| AC-06 | **Passed** | スマートフォンから Tailscale 経由で確認（ユーザー報告） |
| AC-07 | **Passed** | 研究室サーバーで確認（ユーザー報告）。ローカルでも server / worker / postgres の再起動と down → up で確認済み |
| AC-08 | **Passed** | ローカル Compose で retry・失敗ログ・failed 通知（モック受信器）を確認。実際の ntfy では未確認 |
| AC-09 | **Passed** | `test_idempotency.py`（16スレッド同時）、`test_duplicate_runs_apply_side_effect_once` |
| AC-10 | **Passed** | 研究室サーバーで確認（ユーザー報告）。ローカルでも構築・バックアップ・リハーサル復元・in-place 復元を再現済み |

## Known Issues / Blockers

1. **Notion / Linear へのアクセス不可**：エージェントは外部仕様を取得できない。Phase 1 の仕様はローカルの `docs/phase_specs/phase1.md` を使う。
2. **実環境の情報が記録されていない**：研究室サーバーの OS / CPU、Docker / Compose のバージョン、バックアップの保存先、通知エンドポイント。サーバーでの動作はユーザーが確認済み。
3. **実行中の run は自動で復旧できない**：heartbeat automation で Crashed を検出し、副作用を確認してから手動で再実行する設計（recovery.md）。
4. **`docker kill` / `docker stop` した worker は自動で再起動しない**（Docker の仕様。ローカルで確認済み）。運用者が `up -d` で起動する。
5. **worker ごと落ちた run は hook 通知が飛ばない**：Crashed への変更は automation が行うため。必要なら Prefect automation の通知 action を追加する（Phase 1 では未実装）。
6. `data/` の扱いは未決定。Phase 1 では研究成果物の storage を追加していない。
7. `docs/phase1_technical_design.md` §1 と旧 handoff にある「phase1.md は存在しない」は古い記述（現在は存在する）。設計書は Codex の文書なので変更していない。
8. ローカル検証用のスタック `ra-verify`（コンテナ、ボリューム `ra-verify_pgdata` / `ra-verify_worker-state`）とローカルの `.env` が MacBook に残っている。不要になったら `docker compose -p ra-verify --env-file .env -f infra/compose.yaml down -v` で削除する（ローカル検証データのみ）。

## Exact Next Actions

1. ユーザー：Eagle への反映を承認する。承認後、`docs/operations/phase2_planning.md` §5 の手順で反映し、check-only で動作を確認する。
2. ユーザー：テスト用の Issue と Notion ページの扱い、モデル単価（`RA_MODEL_PRICING`）を決める。
3. 次の Phase：Phase 3 の要件定義を受け取り、`docs/phase_specs/phase3.md` を作ってから着手する。Phase 3 の入力は `handoffs/<task>/<plan_id>.json`（`ExecutionHandoff`）。
