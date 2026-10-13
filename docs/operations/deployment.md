# デプロイ手順（Phase 1）

研究室サーバーに Prefect Server / Worker / PostgreSQL を構築し、MacBook から deployment を登録・起動する手順。設計の根拠は `docs/phase1_technical_design.md`。

> Eagle（Windows + WSL2）では worker / Discord bot / watchdog を WSL ホストの専用ユーザー `ra` で動かす構成にした（2026-10-13 ユーザー決定）。Eagle の手順は `eagle_wsl_setup.md`。この文書のコンテナ worker 構成は Mac のローカル検証用。
>
> 検証状況（2026-10-10）：本手順は **MacBook (arm64, Docker 29.8) 上でローカル模擬として実行・確認済み**。研究室サーバー上では未実施。サーバーの OS / CPU architecture は未確認（使用イメージは linux/amd64 と linux/arm64 の両方を提供していることを確認済み）。

## 1. 構成とバージョン

| 要素 | 値 | 固定箇所 |
|---|---|---|
| Python | 3.12 | `.python-version`, `infra/Dockerfile` のベースタグ |
| Prefect (client / server / worker) | 3.8.8 | `uv.lock`, `infra/Dockerfile`, `infra/compose.yaml` (server image), `prefect.yaml` |
| PostgreSQL | 16.15 | `infra/compose.yaml` |
| uv (イメージ内) | 0.12.18 | `infra/Dockerfile` |

2026-10-10 時点の digest（参考。タグは不変ではないため、更新時に再確認する）：

- `prefecthq/prefect:3.8.8-python3.12` — `sha256:57e02b3f63d663e02b06d4127362ea48d92d4b361fbd96c8ba8f01e5702755f4`
- `ghcr.io/astral-sh/uv:0.12.18` — `sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc`
- `postgres:16.15`：digest 未記録（`docker buildx imagetools inspect postgres:16.15` で確認）

**Prefect のバージョンは4か所すべてを同時に変更する**（client と server の不一致は非互換の原因になる）。

サービス：

| Service | 役割 | 公開 |
|---|---|---|
| `postgres` | Prefect メタデータ（run 状態・履歴・ログ・deployment） | なし（compose 内部のみ） |
| `prefect-server` | API / UI / スケジューラ等（単一プロセス） | `127.0.0.1:4200` のみ |
| `prefect-worker` | `research-local`（process 型）pool を poll し、subprocess で flow を実行。同時実行 1 | なし |
| `discord-bot` | Gateway 常駐、通知 outbox の送信、承認 Interaction の保存 | なし |

Redis は使わない（単一サーバープロセス構成のため不要。スケールする場合は設計変更が必要。技術設計 §4.1）。

## 2. 研究室サーバーの初回構築

前提：Docker Engine と Compose v2 がインストール済み、リポジトリを clone できること。

```bash
git clone <repo-url> ResearchAutopilot && cd ResearchAutopilot

# 1. シークレットを作成（Git 管理外）
cp .env.example .env
chmod 600 .env
# .env を編集：
#   POSTGRES_PASSWORD   = $(openssl rand -hex 24)    # URL-safe な文字のみ
#   PREFECT_AUTH_STRING = admin:$(openssl rand -hex 24)
#   PREFECT_SERVER_UI_API_URL = https://<TS_FQDN>/api   (monitoring.md で Tailscale 設定後に確定)
#   PREFECT_UI_URL            = https://<TS_FQDN>
#   GIT_SHA = $(git rev-parse --short HEAD)

# 2. ビルドと起動
alias ra-compose='docker compose --env-file .env -f infra/compose.yaml'
ra-compose build
ra-compose up -d
ra-compose ps            # postgres / prefect-server / prefect-worker / discord-bot を確認

# 3. ポートの確認：4200 が 127.0.0.1 のみ、5432 は host に出ていない
ss -ltnp | grep -E ':4200|:5432'
```

続けて `monitoring.md` の Tailscale 設定を行い、`PREFECT_SERVER_UI_API_URL` / `PREFECT_UI_URL` を実際の FQDN にしたら `ra-compose up -d` で再作成する。

### zombie run 検出 automation の登録（初回のみ）

```bash
# サーバー上（host 内から localhost API を使う場合）
set -a; . ./.env; set +a
export PREFECT_API_URL=http://127.0.0.1:4200/api PREFECT_API_AUTH_STRING="$PREFECT_AUTH_STRING"
uv run prefect automation create --from-file infra/automations/crash-zombie-flow-runs.yaml
```

（サーバーに uv が無い場合は MacBook から Tailscale 経由で同じコマンドを実行してよい。）

## 3. MacBook の設定（deployment 登録・起動）

MacBook は Compose の DNS 名を解決できないので、Tailscale の URL を使う。

```bash
# Prefect profile を作る（auth string は ~/.prefect/profiles.toml に平文保存される。ファイル権限に注意）
uv run prefect profile create lab
uv run prefect --profile lab config set \
  PREFECT_API_URL=https://<TS_FQDN>/api \
  PREFECT_API_AUTH_STRING='<.env の PREFECT_AUTH_STRING と同じ値>'
uv run prefect profile use lab
uv run prefect work-pool ls          # research-local が見えれば接続 OK
```

profile に保存したくない場合は、同じ2変数を環境変数として毎回 export してもよい。

### deployment の登録

**サーバー側の worker イメージが同じ commit でビルド済みであること**を先に確認する（deployment はコードを持たず、worker イメージ内の `/opt/research-autopilot` を参照するため）。

```bash
GIT_SHA=$(git rev-parse --short HEAD) uv run prefect deploy --all --no-prompt
```

登録される deployment（`prefect.yaml`）：

| Deployment | 内容 |
|---|---|
| `smoke-success/manual` | 正常系＋冪等な副作用。cron `0 9 * * *` Asia/Tokyo を **inactive** で登録 |
| `smoke-failure/retry-check` | 一時障害を想定した retry（最大2回、指数 backoff + jitter）後に Failed |
| `smoke-timeout/timeout-check` | task timeout（2秒）超過で TimedOut |
| `approval-demo/manual` | 承認待ち通知を送り、UI から Resume されるまで Paused |
| `smoke-sleep/recovery-drill` | 長時間 Running（復旧訓練用） |

### 起動

```bash
uv run prefect deployment run smoke-success/manual --watch
uv run prefect deployment run smoke-failure/retry-check --watch
uv run prefect deployment run smoke-success/manual --param message=hi --param operation_id=my-op-1
```

スケジュールの有効化（時刻・頻度を運用者が承認した後のみ）：UI の deployment 画面で schedule を有効化するか、`prefect.yaml` の `active: true` に変更して再 deploy する。

## 4. コード・依存の更新

順序が重要：**先にイメージ、後に deployment metadata**（新しい metadata が古いイメージで実行されるのを防ぐ）。

```bash
# サーバー
git pull
sed -i "s/^GIT_SHA=.*/GIT_SHA=$(git rev-parse --short HEAD)/" .env
ra-compose build prefect-worker
ra-compose up -d prefect-worker discord-bot  # 同じ image。実行中 run があれば終わるのを待つ

# MacBook（同じ commit で）
GIT_SHA=$(git rev-parse --short HEAD) uv run prefect deploy --all --no-prompt
```

### Prefect / PostgreSQL のバージョン更新

1. `recovery.md` の手順でバックアップを取る（Server 起動時に DB migration が自動実行されるため）。
2. `pyproject.toml` / `uv.lock`（`uv lock --upgrade-package prefect`）、`infra/Dockerfile`、`infra/compose.yaml`、`prefect.yaml` の Prefect バージョンを揃えて変更する。
3. ローカルで `uv run pytest` と Compose 起動を確認してから、サーバーで `ra-compose build && ra-compose up -d`。
4. PostgreSQL のメジャーバージョン変更は data directory の互換性がないため、`pg_dump` → 新バージョンの空ボリュームへ `pg_restore` で移行する。

## 5. 開発用と本番用の違い

Compose ファイルは共通。違いは `.env` の値だけ。

| 項目 | ローカル開発（MacBook） | 本番（研究室サーバー） |
|---|---|---|
| `PREFECT_SERVER_UI_API_URL` | `http://127.0.0.1:4200/api` | `https://<TS_FQDN>/api` |
| `PREFECT_UI_URL` | `http://127.0.0.1:4200` | `https://<TS_FQDN>` |
| MacBook CLI の `PREFECT_API_URL` | `http://127.0.0.1:4200/api` | `https://<TS_FQDN>/api` |
| シークレット | ローカル用の乱数 | 本番専用の乱数（ローカルと共有しない） |
| 通知 | Discord のテスト guild / 4 channel | Discord の運用 guild / 4 channel（token は環境別） |

Compose を使わない最小のローカル実行（一時的な SQLite サーバーを自動起動）：

```bash
uv run python -m research_autopilot.flows.smoke_test success   # failure / timeout も可
uv run pytest
```

## 6. セキュリティ上の約束事

- `.env` は `chmod 600`、Git に入れない（`.gitignore` 済み）。
- Docker socket・host のディレクトリは一切マウントしない。privileged も使わない。Worker は非 root（uid 10001）で動く。
- ポートは `127.0.0.1:4200` だけを publish する。`ports:` に host IP を書かずに追加すると全 interface に公開されるので禁止。
- 将来の LLM エージェントにはこのホストの Docker 操作権限や root を渡さない。
