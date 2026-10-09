# 障害復旧・バックアップ（Phase 1）

> 検証状況（2026-10-10）：本書の「ローカル検証済み」の記載は **MacBook 上のローカル Compose（project 名 `ra-verify`）で実行した結果**。研究室サーバー上では未実施。

## 1. 用語の区別

| 区別 | 意味 |
|---|---|
| **プロセスの復旧** | コンテナ（server / worker / postgres）が再起動して応答に戻ること。Compose の restart policy が担う |
| **実行中ジョブの復旧** | 落ちた時点で Running だった flow run を続きから再開すること。**Phase 1 では行わない（できない）**。検出して Crashed にし、人が判断して再実行する |
| **自動 retry** | 同じ run の中で、task / flow に設定した retries が自動で再試行すること（例：`smoke-failure` の `flaky_step` は最大2回、指数 backoff + jitter、`TransientError` のみ） |
| **手動再実行** | 運用者が UI の Retry（同じ run ID・同じパラメータで再スケジュール）や `prefect deployment run`（新しい run）で再投入すること |

## 2. 障害ごとの挙動

| 障害 | 何が起きるか | 自動で保証しないこと | 運用者の対応 |
|---|---|---|---|
| PostgreSQL 再起動 | 名前付きボリュームから再開。履歴は残る（ローカル検証済み） | ボリューム破損・削除 | `ra-compose ps`、必要ならバックアップから復元 |
| Server 再起動 | 再起動後も過去の run 履歴・ログ・deployment は残る（ローカル検証済み） | 停止中の状態報告の即時整合 | `/api/health`、worker の再接続、run 状態を確認 |
| Worker プロセスの異常終了 | `restart: unless-stopped` で自動再起動（ローカル検証済み：コンテナ内でプロセスを SIGKILL → restarts=1 で復帰） | 実行中だった run の再開 | 下記 §3 |
| `docker kill` / `docker stop` による停止 | **自動再起動されない**（Docker は手動停止を restart policy の対象外とする。ローカル検証済み） | — | `ra-compose up -d` で起動 |
| host 再起動 | Docker daemon 起動後に `unless-stopped` のサービスが再開。Tailscale Serve も `--bg` 設定なら再開 | 実行中だった run の再開 | §3、`tailscale serve status` |
| `ra-compose down` → `up -d` | ボリュームは残るので履歴は残る（ローカル検証済み）。`down -v` はボリュームを消すので **使わない** | — | — |
| ボリューム消失 | 履歴は失われる | — | §5 で復元 |

## 3. 実行中に worker が落ちた run（zombie run）

flow run は既定で 180 秒ごとに heartbeat を送る（`PREFECT_FLOWS_HEARTBEAT_FREQUENCY`）。`infra/automations/crash-zombie-flow-runs.yaml` は 540 秒（3回分）heartbeat が来ない Running run を **Crashed** にする。登録手順は `deployment.md` §2。

**ローカル検証済み（短縮版）**：heartbeat 30 秒、検出窓 90 秒の一時 automation で、`smoke-sleep/recovery-drill` の実行中に worker を kill → 約100秒後に run が Running → Crashed になった。本番値（180 秒 / 540 秒）での実測は未実施。heartbeat 間隔を変える場合は automation の `within` も同じ比率で変える。

Crashed になった run への対応：

1. UI でその run のログを見て、どこまで進んだかを確認する。
2. 外部副作用（ファイル、通知、外部 API 呼び出し）が実行済みかを確認する。冪等キー付きの副作用（§4）は再実行しても重複しない。
3. 問題なければ UI の Retry、または `prefect deployment run` で再投入する。

### 復旧訓練（研究室サーバーで実施する手順）

```bash
uv run prefect deployment run smoke-sleep/recovery-drill      # MacBook から
# サーバー：run が Running になってから
docker compose --env-file .env -f infra/compose.yaml kill prefect-worker
# 540 秒＋α 待ち、UI で run が Crashed になることを確認
docker compose --env-file .env -f infra/compose.yaml up -d prefect-worker
```

## 4. 冪等性（重複実行で副作用を重複させない）

- Prefect の retry は exactly-once を保証しない。副作用は安定した `operation_id` をキーにして冪等にする。
- サンプル実装：`research_autopilot.idempotency.apply_once`。副作用そのものを「`<operation_id>.json` をアトミックに作成する」ことにしているため、「副作用は済んだが記録前に落ちた」という隙間がない。同時に重複実行しても1つしか成功しない（テスト：`tests/test_idempotency.py` で16スレッド同時実行）。
- `smoke_success` は `operation_id` を省略すると flow run ID を使う。同じ run の手動 Retry では副作用は繰り返されず、新しい run では新しい副作用になる。
- 外部サービスへの一般化では、相手側の idempotency key や一意制約を使うこと。それが無い限り、クラッシュをまたいだ exactly-once は保証できない。
- 保存先：worker の名前付きボリューム `worker-state`（`/var/lib/research-autopilot/state`）。

## 5. PostgreSQL のバックアップとリストア

**ローカル検証済み**：下記の手順で `pg_dump -Fc` を取得し、(a) 使い捨てコンテナへのリハーサル復元、(b) 稼働スタックへの in-place 復元の両方を行った。どちらも復元後の件数が一致した（8 runs / 115 logs / 5 deployments）。復元後に新しい run が実行できることも確認した。

### バックアップ

```bash
cd ResearchAutopilot
alias ra-compose='docker compose --env-file .env -f infra/compose.yaml'
BACKUP_DIR=/srv/backups/research-autopilot      # 例。リポジトリやボリュームの外に置く（保存先は未決定）
sudo install -d -m 700 -o "$USER" "$BACKUP_DIR"
umask 077
ra-compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$BACKUP_DIR/prefect-$(date +%Y%m%dT%H%M%S).dump"
# 付随情報（バージョンと commit）
echo "git=$(git rev-parse --short HEAD) prefect=3.8.8 postgres=16.15" > "$BACKUP_DIR/prefect-$(date +%Y%m%dT%H%M%S).meta"
```

- パスワードはコマンドラインに出ない（コンテナ内の環境変数とローカルソケット認証を使う）。
- 定期実行（cron 等）、保持世代、別媒体へのコピー、暗号化は **未決定**（保存先が決まり次第ここに追記）。名前付きボリュームがあることはバックアップの代わりにならない。

### リストアのリハーサル（本番 DB に触れない）

```bash
F=/srv/backups/research-autopilot/prefect-<timestamp>.dump
docker run -d --name ra-restore-test -e POSTGRES_PASSWORD=rehearsal postgres:16.15
until docker exec ra-restore-test pg_isready -U postgres; do sleep 2; done
docker exec ra-restore-test createdb -U postgres prefect
docker exec -i ra-restore-test pg_restore -U postgres -d prefect --exit-on-error --no-owner < "$F"
docker exec ra-restore-test psql -U postgres -d prefect -tAc \
  "select count(*) from flow_run; select count(*) from log; select count(*) from deployment;"
docker rm -f ra-restore-test
```

### 本番へのリストア（破壊的。実施前に明示的な承認を得る）

```bash
ra-compose stop prefect-worker prefect-server
ra-compose exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" --force "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
ra-compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exit-on-error --no-owner' < "$F"
ra-compose up -d && ra-compose ps
```

ボリュームごと失った場合は、`ra-compose up -d postgres` で空の DB を作ってから上の手順で復元する。

**しないこと**：`prefect server database reset`、`ra-compose down -v`、本番 DB でのリハーサル。

## 6. 復旧できないケース（制約）

- 実行中だった flow run の途中からの再開（checkpoint）は Phase 1 にはない。Crashed として検出し、再実行する。
- 冪等キーを持たない外部副作用は、クラッシュのタイミングによって重複・欠落があり得る。
- 最後のバックアップ以降の履歴は、ボリュームを失うと戻らない。
- worker ごと落ちた run には hook 通知が飛ばない（`monitoring.md` §4）。
- 単一ホスト構成なので、ホスト自体が止まっている間は実行も監視もできない。
