# 監視とリモートアクセス（Phase 1）

> 検証状況（2026-10-10）：Prefect UI の配信（`/` が 200、`/ui-settings` で `auth: BASIC`）、API の Basic 認証（認証なし／誤り 401、正しい値 200、`/api/health` は認証不要）、`127.0.0.1:4200` のみの bind、LAN IP からの到達不可は **MacBook 上のローカル Compose で確認済み**。
> **Tailscale、スマートフォン、研究室サーバー上での確認、ブラウザでの UI ログイン操作は未実施。** 以下の Tailscale 手順は公式ドキュメントに基づく手順であり、まだ実行していない。

## 1. アクセス経路

```text
MacBook / スマートフォン ──(tailnet 内 HTTPS)──> https://<TS_FQDN>
                                                    │ tailscale serve (研究室サーバー host)
                                                    ▼
                                         127.0.0.1:4200 (prefect-server)
```

- 公開インターネットからは到達できない（ポートは localhost のみ、Funnel は使わない）。
- 認可の主な境界は Tailscale の grants。Prefect の Basic 認証は追加の防御。

## 2. Tailscale の設定（研究室サーバー）

管理者権限のある人が行う。`<...>` は未確認の実値に置き換える。

1. 公式手順で Tailscale をインストールし、ログインする。machine name に秘密情報を含めない（HTTPS 証明書の公開ログに FQDN が載るため）。
   ```bash
   sudo tailscale up --advertise-tags=tag:research-server   # tag を使う場合（tagOwners の設定が先に必要）
   ```
2. 管理画面で MagicDNS と HTTPS Certificates を有効にする。
3. Serve を設定する（tailnet 内のみ。再起動後も維持される）：
   ```bash
   sudo tailscale serve --bg 4200
   tailscale serve status        # https://<TS_FQDN> → http://127.0.0.1:4200 を確認
   tailscale funnel status       # Funnel が無効であることを確認
   ```
4. `.env` の `PREFECT_SERVER_UI_API_URL=https://<TS_FQDN>/api` と `PREFECT_UI_URL=https://<TS_FQDN>` を設定し、`ra-compose up -d` で再作成する。
5. アクセス制御（grants）の例。広い `"src": ["*"]` は使わない：
   ```json
   {
     "tagOwners": { "tag:research-server": ["<admin-user@example.com>"] },
     "grants": [
       {
         "src": ["<researcher@example.com>"],
         "dst": ["tag:research-server"],
         "ip":  ["tcp:443"]
       }
     ]
   }
   ```
   SSH など他の用途の grants は別途、必要最小限で定義する。ポリシーの変更履歴は管理者が記録する（認証キーや秘密はリポジトリに置かない）。

## 3. 端末の設定と確認

| 端末 | 手順 | 確認方法 |
|---|---|---|
| MacBook | Tailscale にログイン → `deployment.md` §3 の profile 設定 | `uv run prefect work-pool ls`、ブラウザで `https://<TS_FQDN>` |
| スマートフォン | Tailscale アプリでログイン → ブラウザで `https://<TS_FQDN>` | ログイン画面で auth string を入力 → Runs 一覧・各 run のログ・状態が見える |

UI でできること：Runs 一覧（状態・開始時刻・所要時間）、各 run のパラメータ・ログ・例外、deployment とスケジュール、Paused run の Resume（承認）、手動 retry。

### 負の確認（未承認経路から届かないこと）

```bash
# サーバー上：4200 は 127.0.0.1 のみ、5432 は出ていない
ss -ltnp | grep -E ':4200|:5432'
# 同じ LAN の別マシンから（失敗すること）
curl -m 5 http://<server-lan-ip>:4200/api/health
# tailnet に参加していない端末 / grants 対象外のユーザーから https://<TS_FQDN> が開けないこと
# 認証なしの API 呼び出しが 401 になること
curl -s -o /dev/null -w '%{http_code}\n' https://<TS_FQDN>/api/admin/version
```

## 4. 通知（ntfy 互換）

`.env` で設定する（未設定ならログに出るだけ）：

```bash
RA_NOTIFY_URL=https://<ntfy-server>     # 自前の ntfy、または ntfy.sh
RA_NOTIFY_TOPIC=<推測されにくい topic 名>
RA_NOTIFY_TOKEN=<アクセストークン（任意）>
```

`ra-compose up -d prefect-worker` で反映。スマートフォンの ntfy アプリで同じ topic を購読する。

- 送られるイベント：`completed`（正常完了）、`failed`（Failed / TimedOut / Crashed hook）、`approval_required`（approval-demo の一時停止）。
- 本文に入るのは flow 名、run 名、状態、例外の **型名**、UI リンクだけ。例外メッセージ、パラメータ、ログ、トークンは入らない。
- 送信は timeout 5秒、5xx / 通信エラーは最大2回再送、4xx は再送しない。失敗しても warning ログを出すだけで run の状態は変わらない。
- **制約**：heartbeat 途絶 automation が Crashed にした run（worker ごと落ちた場合）は flow プロセスが存在しないため hook 通知が飛ばない。必要なら Prefect automation の通知 action で補う（Phase 1 では未実装）。

ローカル検証（2026-10-10）：Worker からホスト上のモック HTTP 受信器に `completed` / `failed` / `approval_required` が届き、Bearer token が付いていること、本文に例外メッセージがないことを確認済み。実際の ntfy サーバーでは未確認。

## 5. ログとヘルスチェック

```bash
ra-compose ps                                   # 各サービスの (healthy)
ra-compose logs -f --tail=200 prefect-worker    # worker / flow の標準出力
ra-compose logs --tail=200 prefect-server
curl -s http://127.0.0.1:4200/api/health        # サーバー host 上で
uv run prefect flow-run ls                      # MacBook から
uv run prefect flow-run logs <flow-run-id>      # run のログ（DB に保存されたもの）
```

- コンテナのログは json-file で 10MB × 5 世代にローテーションされる。run のログの正本は PostgreSQL（UI / API で参照）。
- healthcheck の意味：server は `/api/health` の応答、worker は poll 活動（`--with-healthcheck` の `/health`）。**どちらも flow の成功を意味しない。**
