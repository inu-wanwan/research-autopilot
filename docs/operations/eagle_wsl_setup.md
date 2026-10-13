# Eagle（Windows + WSL2）セットアップ手順

研究室サーバー Eagle に、Docker Compose のスタック（PostgreSQL / Prefect Server / Prefect Worker / Discord Bot）を WSL2 から構築する手順。
共通の内容（バージョン固定、MacBook の profile、更新手順）は `deployment.md` にある。この文書では **Eagle 固有の差分**と、実行する順番をまとめる。

> 検証状況（2026-10-13 / Claude Code）：下記「Eagle の現状」の各項目は Eagle 上のコマンドで確認した。**この文書の手順そのものは、まだ Eagle で通しで実行していない。**
> 対象は Phase 1〜2（compose で動く範囲）。Phase 3 以降を Eagle で動かす構成は未決定（§9）。

## 0. Eagle の現状（2026-10-13 確認）

| 項目 | 状態 | 確認方法 |
|---|---|---|
| WSL ディストリビューション | Ubuntu 24.04.1 LTS、x86_64、32 CPU、メモリ 31 GiB（WSL から見た値） | `/etc/os-release`、`nproc`、`free -g` |
| systemd | 無効（PID 1 は `init(Ubuntu)`） | `ps -p 1 -o comm=` |
| `.wslconfig` | なし（WSL の既定の上限を使っている） | `ls /mnt/c/Users/<user>/.wslconfig` |
| Docker | **Docker Desktop**（WSL integration、Engine 29.7.2、Compose v5.4.0） | `docker info --format '{{.OperatingSystem}}'` |
| Tailscale | **Windows 側**で稼働（machine `eagle`）。WSL 内の `tailscaled` は動いていない | `tailscale.exe status` |
| Tailscale Serve | 設定済み：`https://eagle.tail5877f8.ts.net` → `http://127.0.0.1:4200`（tailnet のみ） | `tailscale.exe serve status` |
| 既存の volume | `research-autopilot_pgdata`、`research-autopilot_worker-state`（2026-10-09 作成） | `docker volume ls` |
| 既存のイメージ | `research-autopilot-worker:5f6ad8b`、`:e623fcb` | `docker images` |
| 稼働中のコンテナ | なし | `docker compose ls` |
| uv（WSL 内） | 未インストール | `which uv` |
| 現在のチェックアウト | `/mnt/c/Users/rixyt/workspace_eagle/dev/ResearchAutopilot`（Windows 側のファイルシステム、CRLF） | `file infra/compose.yaml` |

## 1. 前提（Windows 側）

1. Docker Desktop
   - Settings > Resources > WSL integration で、Ubuntu の integration を有効にする（済み）。
   - Settings > General の「Start Docker Desktop when you sign in」を有効にする。Docker Desktop が起動すれば、`restart: unless-stopped` のコンテナは自動で再開する。**Windows にログインしていない間は Docker Desktop が起動しない**ので、再起動後の自動ログインまたは手動ログインの運用を決めておく（未決定）。
2. Windows の電源設定で、スリープ・休止状態に入らないようにする。
3. Tailscale：Windows 版を使う（今の構成）。WSL 内には入れなくてよい。Serve は Windows 側の `127.0.0.1:4200` を見ている。Docker Desktop が publish したポートは Windows の localhost に出るので、このままつながる想定（**未確認**。§7 で確認する）。

## 2. リポジトリを WSL のファイルシステムに置き直す

今のチェックアウトは `/mnt/c`（Windows 側）にある。ここでは次の 2 つの問題がある。

- `chmod 600` が効かず、`.env` が誰でも読める権限（777）のままになる。
- Windows の Git で checkout したため、全ファイルが CRLF になっている（`git status` の 200 件以上の変更は改行コードだけの差分）。Dockerfile やシェルスクリプトが壊れる原因になる。

WSL 側（ext4）に clone し直す：

```bash
mkdir -p ~/workspace && cd ~/workspace
git clone https://github.com/inu-wanwan/research-autopilot.git ResearchAutopilot   # GitHub の認証が必要（gh auth login か SSH 鍵）
cd ResearchAutopilot
git switch main && git log --oneline -1

# 改行コードの確認：CRLF が出ないこと
file infra/compose.yaml infra/Dockerfile .env.example    # "ASCII text" のみ（"with CRLF" が付かない）
```

CRLF になっていたら、WSL 内の Git に `git config --global core.autocrlf input` を設定してから clone し直す。

`.env` を移して権限を絞る（中身は表示しない）：

```bash
install -m 600 /mnt/c/Users/rixyt/workspace_eagle/dev/ResearchAutopilot/.env .env
sed -i 's/\r$//' .env          # Windows で編集した場合に備えて CRLF を除く
ls -l .env                     # -rw------- であること
```

移したあと、`/mnt/c` 側の `.env` は削除するかどうかを決める（秘密情報が Windows 側にも残るため）。古いチェックアウトは、`git status` の差分が改行コードだけであることを確認してから片付ける（`git diff --ignore-cr-at-eol --stat` が空なら改行コードだけ）。

> compose のプロジェクト名は `infra/compose.yaml` の `name: research-autopilot` で固定されている。置き場所を変えても、既存の volume（`research-autopilot_pgdata` など）がそのまま使われる。

## 3. uv を入れる（WSL 内）

automation の登録と `prefect deploy` に使う。イメージ内と同じバージョンにそろえる：

```bash
curl -LsSf https://astral.sh/uv/0.12.18/install.sh | sh
exec $SHELL -l
uv --version            # 0.12.18
uv sync                 # リポジトリ直下で。.venv を作る
```

（MacBook から Tailscale 経由で行う場合は不要。`deployment.md` §3。）

## 4. `.env` の確認

値の入れ方は `.env.example` と `deployment.md` §2。Eagle では次の点を確認する。

| 変数 | Eagle での値 |
|---|---|
| `GIT_SHA` | `$(git rev-parse --short HEAD)`。**手で古い値を残さない**（下のコマンドで更新） |
| `PREFECT_SERVER_UI_API_URL` | `https://eagle.tail5877f8.ts.net/api` |
| `PREFECT_UI_URL` | `https://eagle.tail5877f8.ts.net` |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | **既存の volume を作ったときと同じ値**（下の注意） |
| `RA_CATALOG_ENVIRONMENT` | `eagle`（`config/research_repos.toml` の `locations` と名前を合わせる） |
| `RA_EXP_*` | Mac 用の安全な値のままでよい（Phase 4 は §9 が決まるまで動かさない） |
| `RA_AUTOPILOT_WRITE` / `RA_AUTOPILOT_TEST_ONLY` | `false` / `true` のまま |
| `RA_LINEAR_WRITE_ENABLED` | `false` のまま（Phase 2 の apply を試すときだけ `true`） |

```bash
sed -i "s/^GIT_SHA=.*/GIT_SHA=$(git rev-parse --short HEAD)/" .env
grep '^GIT_SHA=' .env
```

**既存の PostgreSQL volume についての注意**：PostgreSQL は、volume が空のときだけ `POSTGRES_PASSWORD` を使ってユーザーを作る。`research-autopilot_pgdata` は 2026-10-09 に作られているので、そのときと違うパスワードを `.env` に入れると、Prefect Server が DB に接続できなくなる（認証エラー）。

- 同じ値を使う → そのまま進む。
- 値を変えた、または分からない → どちらにするかを決める。
  - (a) 中のデータ（Phase 1 の試験 run の履歴）が要らない：`recovery.md` §5 でバックアップを取ってから volume を作り直す。
  - (b) 要る：古いパスワードで起動し、`ALTER USER` でパスワードを変える。
  - **volume の削除は取り消せないので、実行する前に必ず確認する。**

## 5. ビルドと起動

```bash
alias ra-compose='docker compose --env-file .env -f infra/compose.yaml'
ra-compose config --quiet && echo OK     # 必須の変数が足りなければここでエラーになる
ra-compose build
ra-compose up -d
ra-compose ps                            # postgres / prefect-server / prefect-worker / discord-bot が healthy（discord-bot は healthcheck なし、Up であること）
ra-compose logs --tail=50 prefect-server
```

## 6. DB の初期化（初回のみ）

```bash
ra-compose exec postgres sh -c 'createdb -U "$POSTGRES_USER" research_autopilot'   # 既にあればエラーになるだけ
ra-compose exec prefect-worker research-agent db init
ra-compose exec prefect-worker research-agent discord check
ra-compose exec prefect-worker research-agent discord check --send-test   # 各チャンネルにテスト投稿を 1 件ずつ送る
```

## 7. ネットワークの確認

Docker Desktop のポートは Windows 側に出るので、WSL の `ss` では見えない。Windows 側で確認する：

```bash
curl -s http://127.0.0.1:4200/api/health; echo                      # WSL から
powershell.exe -NoProfile -Command "Get-NetTCPConnection -LocalPort 4200,5432 -State Listen -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort"
```

- 4200 は `127.0.0.1` だけ、5432 は出ていないこと。
- `0.0.0.0` や LAN の IP で 4200 が見えたら、作業を止めて報告する（全 interface に公開されている）。
- Windows Defender Firewall の受信規則は変更しない。

tailnet 経由の確認（MacBook かスマートフォンから）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://eagle.tail5877f8.ts.net/api/health          # 200
curl -s -o /dev/null -w '%{http_code}\n' https://eagle.tail5877f8.ts.net/api/admin/version   # 401（認証なし）
```

ブラウザで `https://eagle.tail5877f8.ts.net` を開き、Basic 認証（`PREFECT_AUTH_STRING`）でログインできることを確認する。`tailscale.exe funnel status` で Funnel が無効であることも確認する。

## 8. automation と deployment の登録

```bash
set -a; . ./.env; set +a
export PREFECT_API_URL=http://127.0.0.1:4200/api PREFECT_API_AUTH_STRING="$PREFECT_AUTH_STRING"
uv run prefect work-pool ls                         # research-local が見えること
uv run prefect automation create --from-file infra/automations/crash-zombie-flow-runs.yaml   # 初回のみ
GIT_SHA=$(git rev-parse --short HEAD) uv run prefect deploy --all --no-prompt
uv run prefect deployment run smoke-success/manual --watch
```

- `.env` を shell に読み込むと、そのシェルに秘密情報が残る。作業が終わったら、そのシェルを閉じる。
- `--all` では Phase 3 以降の deployment（`implement-task`、`run-experiment`、`autopilot-poll` など）も登録される。ただし §9 が決まるまで、worker コンテナの中では正しく動かない。**実行しないこと。** `autopilot-poll` のスケジュールは `prefect.yaml` で inactive になっているので、そのままにする。
- Phase 2 の確認は `phase2_planning.md` §5（`plan-task/manual` の dry-run）。

## 9. Phase 3 以降（未決定・未実施）

今の compose では、次のものが足りない。このため Phase 3〜10 と watchdog は Eagle ではまだ動かない。

- worker に渡している環境変数が Phase 2 までしかない。次の変数は `.env` に書いてもコンテナに届かない：
  - `RA_REPOS_FILE`、`RA_EXP_*`、`RA_IMPL_*`
  - `RA_AUTOPILOT_*`、`NOTION_NOTES_DATA_SOURCE_ID`、`RA_APPROVAL_MODE`
  - `RA_CATALOG_*`、`RA_LITERATURE_*`、`RA_EXTERNAL_*`
  - `RA_DISCORD_REPORT_CHANNEL_ID`
- worker イメージに Codex CLI と Claude Code CLI が入っておらず、ログイン方法も Eagle では検証していない（`phase3_implementation.md` §8）。
- 研究リポジトリの clone、`config/research_repos.toml`、`RA_WORKTREES_DIR`、成果物置き場の volume がない。
- Phase 9 は Docker を使うが、worker は docker.sock を持たない設計になっている。
- Phase 8 の Zotero Connector は Windows / Linux で未検証。
- watchdog（`research-agent autopilot watch`）を常駐させる方法が決まっていない（`monitoring.md`）。

構成の候補：

- (a) worker コンテナを拡張する：CLI、volume、環境変数を足す。
- (b) Phase 3〜5 と watchdog を WSL ホスト上のプロセスで動かす：CLI のログイン、研究リポジトリ、Docker をそのまま使える。長時間の計算向けに `phase4_experiments.md` §7 でも挙げている案。WSL では systemd が無効なので、常駐の方法も決める必要がある。

**どちらにするかは人間が決める。** 決まるまでは、次の設定にしておくのが安全：`RA_EXTERNAL_ENABLED=false`、`RA_ZOTERO_ENABLED=false`、`RA_AUTOPILOT_WRITE=false`。

## 10. 再起動・更新

- Windows を再起動したとき：ログインする → Docker Desktop が起動する → コンテナが自動で再開する。WSL の中で `ra-compose ps` を実行して確認する。
- コードを更新するとき：`deployment.md` §4 と同じ手順。リポジトリは §2 の WSL 側の場所で `git pull` する。
