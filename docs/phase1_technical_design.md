# Phase 1 技術調査・設計レビュー

- 調査日: 2026-10-10
- 対象: Research Autopilot Phase 1「自律実行・監視基盤」
- 文書の状態: 実装用設計（機能実装・実環境検証は未実施）
- 要件の入力: ユーザーが 2026-10-10 に提示した Claude Code 向け Phase 1 指示の Section 3〜8

## 1. 文書の読み方と前提

本文では、記述を次の3種類に分ける。

- **確認済み事実**: 2026-10-10 時点の公式ドキュメント、またはリポジトリで確認した事実。
- **設計上の推奨**: Phase 1 要件を満たすために本プロジェクトで採用する設計方針。公式機能そのものとは区別する。
- **未確認 / 制約**: 研究室サーバー、tailnet、スマートフォンなど実環境がなければ確定・検証できない事項。

`docs/phase_specs/phase1.md` はリポジトリに存在しない。ユーザーから「添付指示から Phase 1 要件を読み取り、仕様ファイルは追加不要」と指示されたため、本書に実装契約として必要な要件を含める。ただし、`AGENTS.md` が定める長期的な仕様 SSOT が未作成である事実は変わらない。

今回は技術調査と設計レビューのみであり、Docker、Python、Tailscale、研究室サーバーの設定変更は行っていない。

## 2. Phase 1 実装契約

### 2.1 スコープ

Phase 1 は、研究室 Linux サーバー上で常時稼働する Prefect 3 の実行・監視・障害検出基盤を構築する。MacBook から deployment の登録と起動を行い、研究室サーバーの Worker が Flow を実行する。MacBook とスマートフォンは Tailscale 経由で UI / API にアクセスする。

含むもの:

- Self-hosted Prefect Server / UI、Prefect Worker、PostgreSQL の Docker Compose 配置
- 手動実行、スケジュール実行、パラメータ、状態、履歴、ログ、例外の確認
- retry、timeout、失敗通知、正常完了通知、承認待ち相当通知
- PostgreSQL による Prefect メタデータ永続化
- コンテナ再起動、Server / Worker 停止時の検出と運用上の復旧手順
- 副作用付き Task の冪等性サンプルと正常系・異常系 smoke test
- Tailscale の限定公開、バックアップ / リストア、運用文書、pytest

含まないもの:

- Prefect Cloud
- OpenAI Agents SDK、Notion / Linear API、Codex / Claude Code 自動実装
- Experiment Runner / Analyst / Reviewer、TSUBAME / Slurm
- 独自 Web ダッシュボード、研究タスク自動選択、科学的仕様生成

### 2.2 実装時の受け入れ条件

実装担当者は AC-01〜10 を Passed / Failed / Not Tested で報告する。研究室サーバー、Tailscale、スマートフォンを使えない項目は成功扱いにせず Not Tested とする。

| ID | 条件 |
|---|---|
| AC-01 | `uv run pytest` が成功する |
| AC-02 | MacBook 上で正常系・異常系 Flow を実行できる |
| AC-03 | 研究室サーバーで Compose の必要サービスが起動する |
| AC-04 | サーバー上 Worker が登録済み Flow を実行できる |
| AC-05 | MacBook から登録・起動し、サーバーで実行できる |
| AC-06 | スマートフォンから Tailscale 経由で UI の状態・ログを確認できる |
| AC-07 | Server / Worker 再起動後も過去の Run 履歴が残る |
| AC-08 | 異常系 Flow で retry、failure logging、通知が機能する |
| AC-09 | 重複実行時に副作用の重複が防止される |
| AC-10 | README と運用文書から再構築、バックアップ、復旧を再現できる |

## 3. 公式ドキュメントで確認した事実

### 3.1 Self-hosted Server とデータベース

**確認済み事実**

- Prefect の self-hosted server は API、UI、データベースを持つ。データベースには Flow / Task Run の状態、履歴、ログ、deployments、work pool 状態、events、automations 等が保存される。
- PostgreSQL は本番・高可用性・複数 server 向けで、Prefect は PostgreSQL 14.9 以上と `pg_trgm` を要求する。Server 起動時に必要な Alembic migration が自動実行される。
- PostgreSQL 接続には async driver の `postgresql+asyncpg://...` を使用する。現行設定名は `PREFECT_SERVER_DATABASE_CONNECTION_URL`。公式 Compose 例の旧 alias `PREFECT_API_DATABASE_CONNECTION_URL` も現行コードでは受理されるが、本設計では現行名に統一する。
- Prefect は client と server を同じバージョンに揃えることを推奨している。新しい client と古い server の組み合わせは非互換になり得る。

根拠:

- https://docs.prefect.io/v3/concepts/server
- https://docs.prefect.io/v3/how-to-guides/self-hosted/server-cli
- https://reference.prefect.io/prefect/settings/models/server/database/

### 3.2 公式 Docker Compose 例と Redis

**確認済み事実**

- 現行の公式 Compose ガイドは `postgres`、`redis`、`prefect-server`、`prefect-services`、`prefect-worker` の5サービス例を示している。
- 同ガイドは Redis を「strictly required ではないが、server を scale する場合に推奨」としている。
- 複数 API server、複数 server worker process、または複数 background-service process では Redis が必要である。単一 process では messaging と Docket の in-memory backend を利用できる。
- `prefect server start` は単一 process の基本構成で API / UI と background services を起動できる。`--no-services` は API と background services を分離するときに使う。

根拠:

- https://docs.prefect.io/v3/how-to-guides/self-hosted/docker-compose
- https://docs.prefect.io/v3/how-to-guides/self-hosted/server-cli
- https://docs.prefect.io/v3/advanced/self-hosted

### 3.3 Deployments、schedule、Worker

**確認済み事実**

- Deployment は Flow の server-side metadata であり、いつ・どこで・どのように実行するかを保持するが、Flow コード本体は保持しない。
- Deployment は手動、schedule、event で起動できる。schedule は Cron、Interval、RRule をサポートし、1 deployment に複数設定できる。
- Process Worker は Flow Run を subprocess で実行する。Worker には `PREFECT_API_URL` が必要で、work pool を poll する。
- Worker の `--with-healthcheck` は既定で `0.0.0.0:8080/health` を提供する。正常時 200、規定時間 poll がない場合 503 を返す。ただし、これは poll 活動を確認するもので Flow の成功を保証しない。
- Deployment-based Run の手動 retry は同じ Run ID と parameters を維持し、`run_count` を増やして `Scheduled` に戻し、Worker が再取得する。

根拠:

- https://docs.prefect.io/v3/concepts/deployments
- https://docs.prefect.io/v3/concepts/schedules
- https://docs.prefect.io/v3/concepts/workers
- https://docs.prefect.io/v3/advanced/worker-healthchecks
- https://docs.prefect.io/v3/how-to-guides/workflows/retry-flow-runs

### 3.4 ログ、retry、結果永続化、障害検出

**確認済み事実**

- `get_run_logger()` のログは Flow / Task Run に関連付けられて backend に送られ、UI で参照できる。`log_prints=True` で `print` も backend log にできる。
- Flow / Task の `retries`、`retry_delay_seconds`、Task の exponential backoff、jitter、条件付き retry が利用できる。
- PostgreSQL に保存される Run state / history / log と、Flow / Task の返り値である result は別物である。result は既定では永続化されない。
- result 永続化を有効にしても、Docker の既定ローカル保存先はコンテナをまたいで残らない。再実行・別コンテナで再利用するには shared volume または remote storage が必要である。
- 突然の machine crash や container eviction では Run が `Running` に残ることがある。Self-hosted では heartbeat event を用いた Automation を自分で作り、応答のない Run を `Crashed` にする必要がある。現行 client の heartbeat 既定値は180秒で、公式例は3回分の540秒を detection window の目安としている。
- Prefect transaction / cache だけで任意の外部副作用が exactly-once になるわけではない。公式文書も concurrent transaction が両方開始し得ること、rollback hook が外部操作を自動では取り消さないことを明記している。

根拠:

- https://docs.prefect.io/v3/how-to-guides/workflows/add-logging
- https://docs.prefect.io/v3/how-to-guides/workflows/retries
- https://docs.prefect.io/v3/advanced/results
- https://docs.prefect.io/v3/advanced/detect-zombie-flows
- https://docs.prefect.io/v3/advanced/transactions

### 3.5 Tailscale と HTTPS

**確認済み事実**

- Tailscale Serve は host の localhost service を tailnet 内だけに reverse proxy できる。Funnel は公開インターネット向けであり別機能である。
- Serve は HTTPS certificate を自動利用でき、MagicDNS の FQDN（例: `https://lab-server.example.ts.net`）を提供する。`--bg` で設定すると reboot / Tailscale restart 後も再開する。
- Tailscale の access-control grants は source user/group/device、destination、port を限定できる。Serve にも access policy が適用される。
- HTTPS certificate transparency log に完全修飾 machine name が公開されるため、machine name に秘密情報を含めてはいけない。

根拠:

- https://tailscale.com/docs/features/tailscale-serve
- https://tailscale.com/docs/reference/tailscale-cli/serve
- https://tailscale.com/docs/features/magicdns
- https://tailscale.com/docs/how-to/set-up-https-certificates
- https://tailscale.com/docs/features/access-control/grants
- https://tailscale.com/docs/reference/best-practices/security

### 3.6 Docker Compose の起動制御と永続化

**確認済み事実**

- `depends_on.condition: service_healthy` により依存先 healthcheck 成功後に依存 service を開始できる。ただし、これは起動順序であり、実行中の依存先障害を自動で意味論的に復旧する機能ではない。
- `restart: unless-stopped` は明示停止されない限り終了理由にかかわらず再起動する。`on-failure` は非0終了時だけ再起動する。
- named volume の内容は container lifecycle の外に存在し、container 削除後も保持される。ただし volume 自体の破損・削除に備える backup は別途必要である。
- host IP を省略した port publish は全 interface (`0.0.0.0`) に bind される。localhost 限定には `127.0.0.1:HOST_PORT:CONTAINER_PORT` を明示する。

根拠:

- https://docs.docker.com/compose/how-tos/startup-order/
- https://docs.docker.com/reference/compose-file/services/
- https://docs.docker.com/engine/containers/start-containers-automatically/
- https://docs.docker.com/engine/storage/volumes/

## 4. 採用する最小構成

### 4.1 結論

**設計上の推奨**

Phase 1 は次の3サービスで開始する。

1. `postgres`: Prefect metadata の永続ストア
2. `prefect-server`: 単一 process の Prefect API / UI / background services
3. `prefect-worker`: `process` work pool を poll し、同一 container 内の subprocess で Flow を実行

Redis は Phase 1 の最小構成には入れない。Phase 1 は単一 server process / 単一 background-services process であり、公式文書上 Redis は必須ではないためである。次のいずれかが必要になった時点で Redis と `prefect-services` 分離を設計変更として追加する。

- `prefect server start --workers N` を使う
- API server / background services を複数 replica にする
- 高可用性または load balancing を導入する
- 複数 background-service process を並行稼働する

この境界を越えて Redis なしで scale すると、duplicate schedule / automation などの correctness 問題が起こり得る。

### 4.2 構成図

```text
MacBook Prefect CLI                         Smartphone browser
PREFECT_API_URL=https://<TS_FQDN>/api       https://<TS_FQDN>
             \                              /
              +-------- Tailscale HTTPS ----+
                              |
                     tailscale serve --bg 4200
                              |
                    host 127.0.0.1:4200
                              |
              +---------------v----------------+
              | Docker Compose private network |
              |                                |
              | prefect-server:4200            |
              |   API + UI + scheduler/services|
              |       |                        |
              |       +--> postgres:5432       |
              |                                |
              | prefect-worker                 |
              |   PREFECT_API_URL=             |
              |   http://prefect-server:4200/api
              |   process pool -> subprocess   |
              +--------------------------------+
```

Tailscale は host にインストールし、Compose sidecar にはしない。これにより `/dev/net/tun`、`NET_ADMIN`、host network 等の特権を application Compose に持ち込まずに済む。Funnel は使用しない。

### 4.3 バージョン方針

**リポジトリで確認済み**: Python は `>=3.12`、`uv.lock` の Prefect は 3.8.8、`pyproject.toml` は `prefect>=3.8.8` である。

**設計上の推奨**:

- 実装時に Python minor、Prefect client / server / worker、PostgreSQL image を明示的に固定する。
- Prefect server / worker image は同じ exact Prefect version と Python versionに揃える。初期候補は `prefecthq/prefect:3.8.8-python3.12` だが、実装時に公式 registry で tag と target CPU architecture を確認し、可能なら digest も記録する。
- `3-latest`、`latest`、実行時の package auto-install は本番に使わない。
- PostgreSQL は Prefect が要求する14.9以上を満たす固定版を選ぶ。研究室サーバーの CPU architecture は未確認なので platform を推測で固定しない。

## 5. API URL、UI URL、ネットワーク境界

ここは実装時に最も混同しやすいため、値を用途別に分ける。

| 利用者 / 設定 | 値 | 理由 |
|---|---|---|
| Server listen address | container 内 `0.0.0.0:4200` | 他 container と host port publish から到達させるため。これは host の公開範囲とは別 |
| Server DB URL | `postgresql+asyncpg://<user>:<password>@postgres:5432/<db>` | `postgres` は Compose DNS 名。host の `localhost` ではない |
| Worker `PREFECT_API_URL` | `http://prefect-server:4200/api` | Worker は Compose network 内から service DNS を使う |
| Server `PREFECT_SERVER_UI_API_URL` | `https://<TS_FQDN>/api` | UI JavaScript はスマートフォン / MacBook の browser 上で動くため、browser から到達可能な外部 API URL が必要 |
| UI URL | `https://<TS_FQDN>` | UI の入口。`/api` を付けない |
| MacBook CLI `PREFECT_API_URL` | `https://<TS_FQDN>/api` | MacBook は Compose DNS を解決できないため Tailscale URL を使う |
| Server host-local API | `http://127.0.0.1:4200/api` | SSH 等で host 内から診断するときだけ使う |
| Compose port mapping | `127.0.0.1:4200:4200` | LAN / public interface への直接公開を防ぎ、Tailscale Serve の localhost upstream に限定 |

`PREFECT_API_URL` は client（Worker、Flow、CLI）が API に接続する URL である。`PREFECT_SERVER_UI_API_URL` は配信された UI が browser から API に接続する URL であり、同じ server でも実行場所によって値が異なる。`http://prefect-server:4200/api` を UI 設定に入れると、スマートフォン browser は Compose DNS を解決できず失敗する。

PostgreSQL 5432、Worker health 8080、Docker daemon socket は host に publish / mount しない。必要な container 間通信だけを Compose private network に置く。

## 6. Compose 実装仕様

以下は次の実装担当者が `infra/compose.yaml` に反映する契約であり、今回はファイルを作成していない。

### 6.1 `postgres`

- 固定 version の公式 PostgreSQL image
- `POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_DB` は Git 管理外 `.env` から注入
- named volume を `/var/lib/postgresql/data` に mount
- `pg_isready -U ... -d ...` healthcheck
- `restart: unless-stopped`
- host port publish なし

### 6.2 `prefect-server`

- Worker と同一 exact Prefect version の image
- command は単一 process の `prefect server start --host 0.0.0.0 --port 4200`。`--no-services` は付けない
- `PREFECT_SERVER_DATABASE_CONNECTION_URL` は `postgres` service DNS を使う
- `PREFECT_SERVER_UI_API_URL=https://<TS_FQDN>/api`
- `PREFECT_SERVER_API_AUTH_STRING` を Git 管理外 secret から設定し、Tailscale access control に加えて Basic Auth を defense-in-depth として有効化する
- `PREFECT_SERVER_API_CSRF_PROTECTION_ENABLED=true`
- 必要な FQDN のみに CORS origin を限定する。UI と API が同一 origin の場合、不要な wildcard は設定しない
- `127.0.0.1:4200:4200` のみ publish
- `/api/health` healthcheck。DB migration を考慮して十分な `start_period` を取る
- `postgres: condition: service_healthy` の後に開始
- `restart: unless-stopped`

Prefect の Basic Auth は強力な multi-user RBAC の代替ではない。Phase 1 は Tailscale grants を主要な認可境界、Basic Auth を追加防御とする。認証文字列は `.env.example` には変数名だけを載せ、実値を commit / log / notification に出さない。

### 6.3 `prefect-worker`

- Repository と lockfile から作る固定 image に Flow code と依存を bake する。研究室サーバーで実行する code revision と deployment version を対応付ける
- `prefect worker start --pool research-local --type process --name lab-worker --with-healthcheck`
- `PREFECT_API_URL=http://prefect-server:4200/api`
- `PREFECT_API_AUTH_STRING` は server と同じ secret
- `--limit` で Phase 1 の安全な同時実行数を明示する。初期値は1を推奨し、性能要件なしに増やさない
- localhost result / idempotency sample 用 named volume を固定 path に mount
- `/health` healthcheck
- `prefect-server: condition: service_healthy` の後に開始
- `restart: unless-stopped`
- Docker socket、host root filesystem、SSH key を mount しない。privileged mode を使わない

Worker container 内の Process Worker は subprocess を作れるため、Phase 1 で Docker Worker を採用する必要はない。Docker Worker を採用すると Docker socket access が必要になり、LLM agent に host 権限を渡さない要件と衝突する。

### 6.4 healthcheck と restart の意味

- healthcheck は「その process が期待した endpoint / DB readiness に応答する」ことを確認する。
- `depends_on` は初回起動順序を改善するが、後から DB が停止した場合に Server の処理を保証しない。
- restart policy は container process を再起動するだけで、実行中 Flow の checkpoint resume や外部副作用 rollback を保証しない。
- Worker healthcheck は poll の生存性だけを確認し、Flow の成功や正しい work pool 設定までは保証しない。

## 7. Deployment と scheduling

**設計上の推奨**

- Work pool は `research-local`、type は `process` とする。
- Flow code は Worker image の `/opt/research-autopilot` に固定配置し、deployment の `path` / working directory と entrypoint をその配置に一致させる。MacBook の絶対パスを deployment metadata に保存しない。
- `prefect.yaml` を deployment 定義の Git 管理 SSOT とし、deployment 名は用途を表す（例: `smoke-success/manual`, `smoke-failure/retry-check`）。
- MacBook は Tailscale API URL と auth secret を環境変数で設定して `prefect deploy` / `prefect deployment run` を実行する。
- Schedule は timezone を明示する。研究運用上の既定候補は `Asia/Tokyo` だが、具体的時刻・頻度は科学的運用判断なので、実装時は smoke-test schedule を inactive で登録し、ユーザー承認後に有効化する。
- Deployment version は Git commit SHA と対応させ、Worker image tag も同じ revision に結び付ける。metadata だけ先行更新して古い image で実行しない。

## 8. ログと永続データ

### 8.1 データ分類

| データ | System of Record | 永続化方針 |
|---|---|---|
| Run state / history / parameters / Prefect logs / exceptions / deployments | PostgreSQL | PostgreSQL named volume + `pg_dump` backup |
| Flow / Task return result | Prefect result storage | 必要な Task のみ `persist_result=True`。Phase 1 単一 host では Worker named volume。将来分散時は object storage へ変更 |
| 研究成果物 / 大規模計算データ | Phase 1 未定義 | Prefect result と混同しない。Phase 4 仕様で決定 |
| Container stdout/stderr | Docker logging driver | `docker compose logs` で診断。rotation / retention を Compose で制限し、DB log の代替にしない |
| PostgreSQL backup | host の権限制限された backup directory | repository / named volume の中だけに置かず、別媒体へ複製。復元テストを行う |

Flow / Task 内の業務ログは `get_run_logger()` を使う。秘密値、HTTP Authorization header、DB URL、環境変数 dump をログに出さない。

### 8.2 PostgreSQL backup / restore 設計

- custom format の `pg_dump -Fc` を使用し、`pg_restore` で復元する。
- DB password は command line に直接書かず、Compose environment / `.pgpass` 相当の安全な注入を使う。
- backup は timestamp と Prefect / PostgreSQL version、Git revision を別 metadata に記録する。
- restore は停止した検証環境の空 DB に対して先に rehearsal し、`pg_restore --exit-on-error` を使用する。本番 DB への破壊的 restore は明示承認を必須とする。
- named volume の存在は backup の代替ではない。少なくとも定期 backup、保持世代、別媒体コピー、定期 restore test を運用文書に含める。

根拠:

- https://www.postgresql.org/docs/current/backup.html
- https://www.postgresql.org/docs/current/app-pgdump.html
- https://www.postgresql.org/docs/current/app-pgrestore.html

## 9. Retry、冪等性、通知

### 9.1 Retry と timeout

- 自動 retry はネットワーク瞬断など一時障害が想定される最小 Task に設定する。validation error、認証 error、科学的条件不成立を無条件 retry しない。
- exponential backoff + jitter、上限回数を明示し、無限 retry を禁止する。
- Flow timeout と外部 HTTP request timeout を別々に設定する。
- 自動 retry は同じ実行試行の制御、手動 retry は運用者の判断による Run 再投入として、ログと通知で区別する。

### 9.2 副作用の冪等性

Prefect retry を exactly-once と見なさない。各副作用 Adapter は stable operation key を必須入力とし、可能なら相手 API の idempotency key、または外部 DB の unique constraint / atomic upsert を使う。

Phase 1 のサンプルは、`operation_id` から決まる1ファイルを persistent volume 上に atomic create する Task とする。同じ `operation_id` の再実行では既存内容を検証して `already_applied` を返し、追記や二重作成をしない。副作用そのものを一意ファイル作成にすることで、「effect 完了後・ledger 記録前」の二重書き込み window を作らない。concurrent duplicate も atomic create で競合させる。

実サービスへの一般化では、外部 service が idempotency key / transaction を提供しない限り crash 境界をまたぐ exactly-once は保証しない。その制約を Adapter の docstring と運用文書に残す。

### 9.3 通知

- HTTP notification adapter は endpoint、token、topic 等を環境変数から読む。
- 完了、失敗、承認待ち相当を型付き event として受け、secret と機密ログを除いた最小 payload を送る。
- 通知送信は短い timeout と限定 retry を持つが、送信失敗を研究 Job 本体の state に伝播させない。失敗は warning log と notification failure metric / artifact に記録する。
- 通知 hook 自身が例外を再送出して成功 Run を Failed に変えないことを unit test する。
- Webhook token を URL や message body、Prefect UI log に表示しない。

## 10. 障害モードと復旧限界

| 障害 | 期待動作 | 自動で保証しないこと | 運用 |
|---|---|---|---|
| PostgreSQL container restart | named volume から DB 再開、Server は再接続後に履歴参照 | volume 破損・削除からの復旧 | health と log を確認、必要なら backup restore |
| Prefect Server restart | Compose が process を再起動、DB 上の過去状態は保持 | 実行中 subprocess の完全な継続・欠落 state の即時整合 | `/api/health`、Worker 再接続、Run state を確認 |
| Worker restart / kill | Compose が Worker を再起動し poll 再開 | kill された実行中 Flow の checkpoint resume | heartbeat Automation が stale Run を `Crashed` 化後、外部副作用を確認して手動 retry |
| Host reboot | Docker daemon 起動後に `unless-stopped` service 再開 | reboot 中の実行を exactly-once で再開 | Compose / Tailscale Serve / Run state / backup を確認 |
| API / tailnet 一時断 | retry / reconnect、Worker health が長期断を検出 | disconnected 中の UI 可視性、状態 report 成功 | connectivity と `/api/health` を確認 |
| named volume loss | なし | 履歴復旧 | 検証済み `pg_dump` backup から restore |
| Flow process crash | heartbeat 欠落 Automation で検出可能 | 自動的な安全再実行 | `Crashed`、ログ、外部副作用を確認して手動判断 |

Self-hosted 用に、Flow heartbeat を既定180秒のまま使い、540秒間 event がない deployment-based Run を `Crashed` にする Automation を初期値として設計する。閾値は研究室ネットワークの実測後に調整する。Automation 作成自体と kill test は実装・検証フェーズで行う。

## 11. Tailscale セキュリティ手順の設計

1. 研究室 host に公式 Tailscale client をインストールし、秘密を含まない固定 machine name を付ける。
2. MagicDNS と HTTPS を有効化する。certificate transparency への FQDN 公開を管理者が理解したうえで行う。
3. host 上で `tailscale serve --bg 4200` を設定し、出力された `https://<TS_FQDN>` を確認する。`tailscale funnel` は設定しない。
4. grants で、承認済み user / group / device だけが研究室 server の `tcp:443` に接続できるようにする。広い `src: ["*"]` / `ip: ["*"]` を使わない。
5. MacBook とスマートフォンを tailnet に登録し、各端末で HTTPS UI と API health を確認する。
6. LAN / public interface から host port 4200 と5432へ到達できないことを別経路から negative test する。
7. Prefect Basic Auth を有効化し、MacBook CLI / Worker は `PREFECT_API_AUTH_STRING`、Server は `PREFECT_SERVER_API_AUTH_STRING` を secret として受け取る。
8. access policy と Serve 設定を version 管理可能な安全な運用先に記録し、変更レビューを行う。secret / auth key は repository に置かない。

**未確認**: tailnet 名、研究室 server machine name、組織の user/group、Tailscale 管理権限、スマートフォン OS、研究室 firewall、Docker host OS / CPU architecture。これらは実装前に取得し、placeholder を実値にする。

## 12. 実装ファイルとテスト計画

### 12.1 実装対象

次の担当者は、既存構成を保ちながら最低限以下を作る。

- `.env.example`: 変数名と非秘密 placeholder のみ
- `infra/compose.yaml`, `infra/Dockerfile`: 3サービス構成
- `prefect.yaml`: process work pool deployment、inactive schedule
- `src/research_autopilot/flows/smoke_test.py`: 成功、retry 後失敗、timeout、冪等副作用
- `src/research_autopilot/notifications/`: HTTP adapter と型付き event
- `tests/`: Flow / Task / notification / idempotency unit tests
- `docs/operations/deployment.md`: build、deploy、upgrade、version pin
- `docs/operations/monitoring.md`: UI、logs、health、Tailscale grants / Serve
- `docs/operations/recovery.md`: restart、zombie detection、backup / restore rehearsal
- `README.md`: 開発と本番の入口、運用文書への導線

### 12.2 検証マトリクス

| レベル | 検証 |
|---|---|
| Unit | notification redaction / failure isolation、idempotency sequential + concurrent duplicate、retry condition、設定 validation |
| Local | `uv run pytest`、正常 / 異常 Flow、`docker compose config`、image build、service health、UI log |
| Recovery | Server restart、Worker kill、PostgreSQL restart、host相当の Compose down/up、履歴保持、zombie detection、manual retry |
| Backup | `pg_dump -Fc`、空の検証 DB への `pg_restore --exit-on-error`、履歴件数 / deployment / log の spot check |
| Network | MacBook CLI、MacBook UI、スマートフォン UI、tailnet unauthorized source deny、LAN / public 4200 deny |

テストは destructive な本番 DB reset を行わず、専用 test database / Compose project を使う。`prefect server database reset -y` を本番運用手順に含めない。

## 13. 要件トレーサビリティ

| 要件 | 設計箇所 | 主な検証 |
|---|---|---|
| FR-01 Workflow execution | §§4, 5, 7 | AC-02, 04, 05 |
| FR-02 Persistence / recovery / idempotency | §§8〜10 | AC-07, 08, 09 |
| FR-03 Monitoring / Tailscale | §§5, 8, 11 | AC-06 |
| FR-04 Notifications | §9.3 | AC-08 |
| FR-05 Deployment / operations | §§6, 8, 10〜12 | AC-03, 10 |
| Maintainability / out of scope | §§2, 4, 12 | review |
| Reliability | §§6.4, 9, 10 | recovery tests |
| Security | §§5, 6, 11 | negative network tests、secret scan |

## 14. 仕様との矛盾・判断事項

### 14.1 確認した矛盾

Phase 1 の提示要件そのものと、公式ドキュメントの間に実装不能な矛盾はない。

公式 Compose 例は Redis を含む5サービスだが、提示要件は「必要な場合のみ Redis」としている。公式文書は Redis が単一 process 構成では必須でないと明記しているため、本書の3サービス構成は要件と整合する。

公式 Compose 例は host port を `4200:4200` として全 interface に公開し、認証なしである。これは Phase 1 の「公開インターネットへ直接露出しない」とそのままでは整合しない。本書では localhost bind + Tailscale Serve + grants + Prefect Basic Auth に変更する。これは仕様変更ではなくセキュリティ要件を満たすための具体化である。

公式 Compose ページは DB 設定に旧 alias `PREFECT_API_DATABASE_CONNECTION_URL` を載せる一方、現行 server / settings 文書は `PREFECT_SERVER_DATABASE_CONNECTION_URL` を使用する。本書は現行名を採用する。

### 14.2 実装前に確定が必要な環境情報

- 研究室 Linux distribution、CPU architecture、Docker Engine / Compose version
- Tailscale tailnet 管理者、FQDN、machine tag、許可 user/group
- 固定 image tag / digest の registry availability
- backup の保存先、保持期間、暗号化、restore test の実施先
- ntfy 等の通知 endpoint と認証方式
- schedule の実時刻 / timezone のユーザー承認

これらは placeholder のまま安全に実装を進められる部分と、実環境でなければ検証できない部分を分ける。科学的条件や本番 access policy を推測して確定しない。

## 15. 実装順序

1. 本書と Git 状態を確認し、作業 branch を作る。
2. exact versions、環境変数 schema、3-service Compose を作る。
3. 成功 / 失敗 / retry / timeout / idempotency smoke Flow と unit test を作る。
4. process work pool と `prefect.yaml` deployment を作る。
5. HTTP notification adapter と failure-isolation test を作る。
6. Compose health、履歴保持、Worker kill / Server restart をローカル検証する。
7. deployment / monitoring / recovery と backup / restore rehearsal を文書化する。
8. Tailscale は研究室 host で明示承認を得て設定し、MacBook / smartphone / unauthorized path を実機検証する。
9. AC-01〜10 を Passed / Failed / Not Tested で記録し、`docs/development_handoff.md` を更新する。

