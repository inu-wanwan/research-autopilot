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

Phase 2 の事前設定（完了）。Phase 2 本体（Task Manager / Orchestrator）は未着手。Phase 1 は完了（AC-01〜10 すべて Passed）。

## Current Task

Phase 2 の事前設定：Notion / Linear / OpenAI API の認証情報・権限・接続確認

## Status

完了。接続確認はすべて PASS（Notion、Linear の読み取りと書き込み、OpenAI）。Phase 2 の実装を始められる状態。本体の実装は別の指示を待つ。

## Last Updated

2026-10-10 / Claude Code

## Git Branch / Commit

- Branch: `phase2/prerequisites`（`main` @ `b5eb9be` から分岐。未 merge・未 push）
  - `0e3d0e6` phase2: add placeholders for Notion / Linear / OpenAI settings in .env.example
  - この記録を含む docs の commit（hash は `git log` を参照）
- `main` は `origin/main` より1 commit 先行（`b5eb9be`、サーバー検証の記録。未 push）

## Completed Work

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

- Phase 2 本体の実装（別の指示を待つ）。着手時に `docs/phase_specs/phase2.md` を作る。
- 研究室サーバーの `.env` に Phase 2 の5変数を設定し、`infra/compose.yaml` の `prefect-worker.environment` に渡す設定を追加する（Phase 2 のコードが使い始める時点で。worker の再作成はユーザーの承認が必要）。
- Phase 1 の残り：本番値での zombie 検出の実測、ntfy の設定、バックアップの保存先と定期実行、サーバーの OS / CPU の記録、smoke-success のスケジュールを有効にするかの判断。
- Phase 2 で決めること：使うモデル、統合テスト用の Notion / Linear sandbox、OpenAI の予算上限、INU-270 の扱い。

## Changed Files

- 新規：`docs/phase_specs/phase1.md`, `src/research_autopilot/idempotency.py`, `src/research_autopilot/flows/smoke_test.py`, `src/research_autopilot/notifications/{__init__,events,http,hooks}.py`, `tests/{conftest,test_idempotency,test_notifications,test_smoke_flows}.py`, `infra/{compose.yaml,Dockerfile}`, `infra/automations/crash-zombie-flow-runs.yaml`, `prefect.yaml`, `.env.example`, `.dockerignore`, `docs/operations/{deployment,monitoring,recovery}.md`
- 変更：`pyproject.toml`, `uv.lock`（httpx を追加）、`README.md`、`AGENTS.md`（構成図）、`docs/README.md`（構成図・役割表）、本ファイル

## Tests and Verification

すべて 2026-10-10 に MacBook（arm64、Docker 29.8.1）で実行した。Compose は project 名 `ra-verify` でローカルに起動し、`.env` はローカル用の乱数で作った（Git 管理外）。

| コマンド / 操作 | 結果 |
|---|---|
| `uv run pytest` | 25 passed |
| `uv run python -m research_autopilot.flows.smoke_test success/failure/timeout`（一時的な SQLite サーバー） | Completed / Failed（3回試行）/ TimedOut |
| `docker compose ... config -q`、`build`、`up -d` | 3サービスとも healthy |
| `lsof -iTCP:4200` / `:5432`、LAN IP からの curl | 4200 は 127.0.0.1 のみ、5432 は host に出ていない、LAN IP からは到達不可 |
| `GET /api/admin/version`（認証なし / 誤り / 正しい値）、`/api/health` | 401 / 401 / 200、health は 200 |
| Mac から `prefect deploy --all --no-prompt` → `deployment run --watch` | 登録成功。worker が実行し、success は Completed、retry-check は Failed（ログに Retry 1/2・2/2・exhausted）、timeout-check は TimedOut |
| `deployment inspect` | version=GIT_SHA、schedule は cron `0 9 * * *` Asia/Tokyo で `active: False`、pull step は `/opt/research-autopilot` |
| 通知（host 上のモック HTTP 受信器） | completed / failed（error: TransientError）/ approval_required が届き、Bearer token が一致、本文に例外メッセージなし |
| approval-demo | Paused → `resume_flow_run` → Completed（completed 通知あり） |
| `restart prefect-server prefect-worker`、`restart postgres`、`down` → `up -d` | いずれも run 数・状態の内訳が一致（6 runs） |
| worker を `docker kill`（heartbeat 30秒、一時的な 90 秒 automation） | 約100秒後に run が Running → Crashed。コンテナは exited のまま（restart policy の対象外） |
| コンテナ内で worker プロセスを SIGKILL | 自動で再起動（RestartCount=1）、healthy に戻り、新しい run も Completed |
| `pg_dump -Fc` → 使い捨てコンテナへ `pg_restore --exit-on-error` → 稼働スタックへ in-place 復元 | どちらも 8 runs / 115 logs / 5 deployments で一致。復元後の新しい run も Completed |
| `git diff --check`、秘密情報らしき文字列の grep | 問題なし |

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

1. ユーザー：`phase2/prerequisites` と `main`（未 push の `b5eb9be`）の merge と push を判断する。
2. ユーザー：OpenAI の Limits で予算とアラートを設定する（推奨）。Notion integration の capabilities が Read content だけになっているかを確認する（推奨）。
3. ユーザー：Phase 2 本体の実装指示を出す。
4. エージェント：Phase 2 に着手するときは、まず Notion の Phase 2 ページ（`3f469893cf8f817f9f7ef768fd99c9d3`）と提示された要件から `docs/phase_specs/phase2.md` を作り、`AGENTS.md` §4 の手順で進める。接続情報は `docs/operations/phase2_prerequisites.md` を参照する。
