# Research Autopilot

研究者が定義・承認した研究タスクを、複数のエージェントが実装・実験・解析まで自律的に進めるシステム。

- **Phase 1（自律実行・監視基盤）**：研究室サーバー（Eagle）上の self-hosted Prefect 3（Server / UI、Process Worker、PostgreSQL）を Docker Compose で動かし、MacBook とスマートフォンから Tailscale 経由で監視する。
- **Phase 5（Autopilot）**：Notion / Linear に登録された承認済みの研究タスクを、Phase 2〜4 を通して自律的に実行する。初回の委任（ResearchAuthorization）の範囲内なら途中の承認なしで進め、範囲外・予算超過・完了の判断が必要なときだけ Linear の Status で承認を求める。結果は Notion Notes と Linear に反映する。運用は [`docs/operations/phase5_autopilot.md`](docs/operations/phase5_autopilot.md)。
- **Phase 4（Experiment Runner / Scientific Analyst）**：固定 commit と ExperimentSpec から実験を再現可能に実行・監視・回収・検証し、失敗は振り分けて再試行するか Phase 3 で修正する。検証済みの結果を LLM Analyst が解釈し、独立した Reviewer と ScopeAssessment が次の実験案を審査して、Phase 5 への handoff を作る（承認はしない）。運用は [`docs/operations/phase4_experiments.md`](docs/operations/phase4_experiments.md)。
- **Phase 3（Implementer / Code Reviewer）**：承認済みタスクを登録済みの研究リポジトリの専用 worktree で Codex（fallback は Claude Code）に実装させ、決定論的な検証と独立した LLM Reviewer で確認したうえで、固定 commit と ExperimentSpec を Phase 4 に渡す。Phase 4 からの FailureReport による修正にも対応する。運用は [`docs/operations/phase3_implementation.md`](docs/operations/phase3_implementation.md)。
- **Phase 2（Task Manager / Orchestrator）**：Notion（仕様の正本）と Linear（実行状態の正本）からタスクを取得し、決定論的なゲートを通過したタスクについて Planner / Reviewer エージェント（OpenAI Agents SDK）で実行計画を作る。CLI は `research-agent`。運用は [`docs/operations/phase2_planning.md`](docs/operations/phase2_planning.md)。

- 開発ルール：[`AGENTS.md`](AGENTS.md)
- Phase 1 仕様：[`docs/phase_specs/phase1.md`](docs/phase_specs/phase1.md) ／ 技術設計：[`docs/phase1_technical_design.md`](docs/phase1_technical_design.md)
- 進捗・引き継ぎ：[`docs/development_handoff.md`](docs/development_handoff.md)

## 構成

```text
src/research_autopilot/
  tasks/                   # Notion / Linear connector、決定論的ゲート、Task Manager（Phase 2）
  planning/                # Planner / Reviewer エージェント、再計画ループ、コスト推定（Phase 2）
  orchestrator/            # plan-task の制御フローと Prefect flow（Phase 2）
  storage/                 # 実行履歴・リース・監査ログ（SQL）と JSON artifact（Phase 2）
  cli.py                   # research-agent CLI（Phase 2）
  flows/smoke_test.py      # 正常系・retry・timeout・承認待ち・長時間の smoke flow
  notifications/           # ntfy 互換 HTTP 通知と Prefect state hook
  idempotency.py           # operation_id による冪等な副作用
infra/
  compose.yaml             # postgres / prefect-server / prefect-worker
  Dockerfile               # worker イメージ（flow コードを焼き込み）
  automations/             # heartbeat 途絶 → Crashed の automation
prefect.yaml               # deployment 定義
.env.example               # 環境変数のひな形（実値は .env、Git 管理外）
docs/operations/           # deployment / monitoring / recovery
tests/                     # pytest
```

## クイックスタート（MacBook ローカル）

```bash
uv sync
uv run pytest
uv run python -m research_autopilot.flows.smoke_test success   # failure / timeout

# Compose で一式を起動（ローカル）
cp .env.example .env && chmod 600 .env    # パスワードと auth string を乱数に書き換える
alias ra-compose='docker compose --env-file .env -f infra/compose.yaml'
ra-compose up -d --build && ra-compose ps
set -a; . ./.env; set +a
export PREFECT_API_URL=http://127.0.0.1:4200/api PREFECT_API_AUTH_STRING="$PREFECT_AUTH_STRING"
GIT_SHA=$(git rev-parse --short HEAD) uv run prefect deploy --all --no-prompt
uv run prefect deployment run smoke-success/manual --watch
# UI: http://127.0.0.1:4200 （ログインに PREFECT_AUTH_STRING を入力）
```

研究室サーバーへの構築と MacBook からの接続は [`docs/operations/deployment.md`](docs/operations/deployment.md)。

## 運用早見表

以下は `ra-compose='docker compose --env-file .env -f infra/compose.yaml'`（リポジトリ直下で実行）を前提とする。

| 目的 | コマンド | 詳細 |
|---|---|---|
| 状態確認 | `ra-compose ps` | monitoring.md §5 |
| ログ確認 | `ra-compose logs -f --tail=200 prefect-worker`（`prefect-server` / `postgres`） | monitoring.md §5 |
| run のログ | UI、または `uv run prefect flow-run logs <id>` | monitoring.md §5 |
| サービス再起動 | `ra-compose restart prefect-server prefect-worker` | recovery.md §2 |
| 停止していた worker の起動 | `ra-compose up -d prefect-worker`（`docker kill/stop` 後は自動で戻らない） | recovery.md §2 |
| コード更新 | `git pull` → `.env` の `GIT_SHA` 更新 → `ra-compose build prefect-worker && ra-compose up -d prefect-worker` → MacBook で `prefect deploy --all` | deployment.md §4 |
| Prefect / PostgreSQL 更新 | バックアップ → 4か所のバージョンを揃えて変更 → 検証 → 再構築 | deployment.md §4 |
| バックアップ | `ra-compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > <file>` | recovery.md §5 |
| リストア | リハーサル → 承認 → in-place 復元 | recovery.md §5 |
| Crashed run への対応 | ログと副作用を確認 → UI で Retry | recovery.md §3 |
| リモートアクセス | Tailscale Serve（tailnet 内 HTTPS のみ） | monitoring.md §2 |

**禁止**：`ra-compose down -v`（履歴が消える）、`ports:` に host IP なしで追加、docker.sock のマウント、`.env` の commit。
