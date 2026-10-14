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

## 4. 通知（Discord）

`.env` で Bot token、guild、承認者 user ID、approvals / results / errors / logs の4チャンネルを設定する。変数名と用途は `phase5_autopilot.md` §2〜3 および `.env.example` を参照する。

```bash
RA_NOTIFICATION_BACKEND=discord
RA_APPROVAL_MODE=discord
uv run research-agent discord check
uv run research-agent discord check --send-test   # 明示的なテスト投稿を各チャンネルへ1件
```

Compose では `prefect-worker` が PostgreSQL に通知を enqueue し、`discord-bot` が Gateway と REST を担当する。設定変更後は `ra-compose up -d prefect-worker discord-bot` で反映する。

- イベントは種類から4チャンネルへ決定論的に routing し、Discord Embed で表示する。
- Embed の書式（2026-10-14 ユーザー要望、`notifications/layout.py`）：タイトルは「承認依頼：研究計画｜INU-292」「完了｜INU-292」のように短く、本文は `[タスク]` `[確認してほしいこと]` `[状況]`／`[結果]`／`[内容]` `[変更点]` `[理由]` `[予算・費用]` `[期限]` `[次にやること]`（番号付き）`[リンク]`（Notion / Linear / PR）の順の短い見出しブロック。長い一覧は「ほか N 件（詳細は Notion / Linear）」で切る。request id・run id・commit SHA などは短縮してフッターの `参照: ...` 行に回し、その末尾に配信マーカー `ra:<24hex>` を置く（再起動時の取り込みは、フッターがマーカーと一致するか ` · <marker>` で終わるものを同じメッセージとみなす）。
- 通知は DB outbox で重複排除し、成功時に message ID を保存する。失敗は pending/failed のまま指数 backoff で再送し、run の成否は変えない。
- Gateway は自動再接続し、再起動時に pending 通知と承認待ち persistent view を DB から復元する。
- Bot token は coding agent の環境、通知本文、ログ、custom_id、DB record に含めない。
- **制約**：heartbeat 途絶 automation が Crashed にした run（worker ごと落ちた場合）は flow プロセスが存在しないため hook 通知が飛ばない。必要なら Prefect automation の通知 action で補う（Phase 1 では未実装）。

実 Discord 検証（2026-10-10）：Bot の Gateway 接続、guild、4チャンネルの view/send/embed/history 権限を確認し、各チャンネルにテスト Embed を1件投稿した。ボタン操作は fake E2E で検証し、実ユーザーによるクリックは未確認。ntfy adapter は後方互換として残す。

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

## Watchdog（`research-agent autopilot watch`、2026-10-13 / Claude Code、ユーザー依頼）

止まった・進まないタスクを Discord の報告チャンネル（`RA_DISCORD_REPORT_CHANNEL_ID`、現在 #report）に投稿する常駐監視。正常な間は LLM を使わない。

- **検知（LLM なし、DB だけを読む）**：各タスクの最新 workflow run の blocked / dead_letter、active stage のまま `RA_WATCH_STALL_HOURS`（既定 6）時間更新なし、親 DAG の Blocked / Failed（停止した子が既に報告されていれば親は省く）、承認待ちが `RA_WATCH_APPROVAL_HOURS`（既定 24）時間超、outbox の dead、Discord 通知が `RA_WATCH_DISCORD_BACKLOG_MINUTES`（既定 15）分以上送られていない（gateway 停止）、poll の完了が `RA_WATCH_POLL_STALE_MINUTES`（既定 180）分ない（`autopilot poll` が tick 完了ごとに `autopilot_heartbeats` を更新）。
- **投稿**：Discord REST で直接送る（gateway が落ちていても届く）。同じアラートは 1 回だけ（`autopilot_watchdog_alerts`）。送信失敗は次の回に再試行（最大 5 回）。初回は既存の停止を記録するだけで、開始メッセージを 1 件送る。
- **見立て（triage）**：error のアラートだけ、スレッドに読み取り専用の見立てを 1 回投稿する。サブスク（Codex → Claude）のみで従量 API は使わない。1 日 `RA_WATCH_TRIAGE_DAILY_MAX`（既定 10）回まで。入力はアラート文とそのタスクの直近 5 run の停止理由だけで、ツールなし・何も変更しない。実測：Codex gpt-5.6-terra で入力約 1.5 万 / 出力約 300 tokens（大半は CLI 側の固定プロンプト）。コードは読めないので見立ては推測を含む。
- 起動：`uv run research-agent autopilot watch`（既定 300 秒ごと）、cron なら `--once`。確認だけなら `--dry-run`（投稿しない）。見立てなしは `--no-triage`。
- 未実施：研究室サーバー（compose）での常駐。compose のコンテナには codex / claude CLI のログインがないため、見立てを使うならホストで動かすか、CLI を入れる必要がある（未決定）。
- **メンションによる修正**（ユーザー承認 2026-10-13）：`RA_DISCORD_AUTHORIZED_USER_IDS` のユーザーが #report かその下のスレッド（アラート／修正スレッド、直近 7 日・最大 20）で bot をメンションしたときだけ反応する（30 秒ごとに REST で確認、初めて見るチャンネルは過去のメッセージに反応しない）。
  - `@bot <指示>`：作業ブランチの HEAD から `autofix/<日時>-<id>` を切り、`RA_WATCH_FIX_DIR`（既定 `<RA_STATE_DIR>/autofix`）に git worktree を作って、コーディングエージェント（Codex `workspace-write` sandbox → Claude（Phase 3 の implementer と同じツール許可リスト）、サブスクのみ）に直させる。エージェントには秘密情報を除いた環境を渡し、commit / push / merge はさせない。終わったら watchdog がテスト（`RA_WATCH_FIX_TEST_COMMAND`、既定 `uv run pytest -q -x -p no:cacheprovider`）を走らせ、結果に関わらず commit してスレッドに要約・diff stat・テスト結果を投稿する。アラートのスレッドなら、そのアラートと見立てを文脈として渡す。同じスレッドで続けて指示すると同じブランチで続ける。
  - `@bot 適用`：テスト合格のときだけ、作業ツリーが修正の起点ブランチにあり、未 commit の変更がなく、そのブランチが main / master でない場合に限り、fast-forward（できなければ cherry-pick、衝突なら中止）で取り込む。push はしない。poll ループは次の tick から新しいコードを使うが、Discord gateway と watchdog 自身は再起動が必要。研究室サーバーには反映されない。
  - `@bot 破棄`：その修正の worktree とブランチを削除する。`@bot 状態`：現在のアラート一覧。
  - 修正は 1 件ずつ実行する。実行中に watchdog が止まった修正は、次の起動時に「中断」としてスレッドに報告する（自動再開しない）。
  - 検証：一時 git リポジトリと偽のエージェントでのテスト（`tests/test_watchdog_fix.py`）のみ。実際の Codex / Claude による修正はまだ通していない。
