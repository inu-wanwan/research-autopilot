# Eagle（Windows + WSL2）セットアップ手順

研究室サーバー Eagle に Research Autopilot を構築する手順。**人間が行う操作**（sudo、Windows の設定、専用ユーザーでの作業、ログイン、判断）を中心に書く。
エージェント（Claude Code / Codex、WSL の通常ユーザー `rixyt` で動く）ができる作業は §3 に短くまとめる。

> 検証状況（2026-10-13 / Claude Code）
> - Eagle 上で確認したこと（§0）：Docker Desktop、Windows 側の Tailscale Serve、cgroup v2、WSL から Docker Desktop の `127.0.0.1` の publish ポートに届くこと。
> - `infra/compose.host.yaml` は `docker compose config` で、起動対象が postgres / prefect-server だけになることを確認した。
> - systemd unit は `systemd-analyze verify` を通した。`infra/host/with_dotenv.py` は手元で動作を確認した。
> - rootless Docker 用のユーザー対応（`external/sandbox.py`）は pytest で確認した。
> - **この手順の通し実行、`ra` ユーザーでの rootless Docker、Phase 9 の実 E2E は Eagle で未実施。**

## 構成

```text
Windows：Docker Desktop（rixyt が操作）              Tailscale（Windows 版）
  └ compose: postgres (127.0.0.1:5432)                 └ Serve: https://eagle.tail5877f8.ts.net → 127.0.0.1:4200
             prefect-server (127.0.0.1:4200)
WSL2 Ubuntu（systemd 有効）
  └ ユーザー ra（docker / sudo グループに入れない）
       ├ rootless Docker（ra 専用。Phase 9 のサンドボックス）
       ├ systemd --user：ra-prefect-worker / ra-discord-bot / ra-watchdog / ra-zotero-bridge（H7）
       ├ /opt/research-autopilot（コードと .env。deployment がここで実行される）
       └ Codex / Claude CLI（ra でログイン）、研究リポジトリ、worktree
```

構成をこう分けた理由：

- `rixyt` は `docker` グループに入っている（root 相当）。LLM が書いたコード（Phase 3 のテスト、Phase 4 のジョブ）を `rixyt` で動かすと、Phase 1 仕様 §3「Docker socket や host root を LLM エージェントに公開しない」に反する。
- 専用ユーザー `ra` の rootless Docker は、中で root になってもホスト上では `ra` の権限しかない。

## 0. Eagle の現状（2026-10-13 確認）

| 項目 | 状態 |
|---|---|
| WSL | Ubuntu 24.04.1、x86_64、32 CPU。H2 で systemd を有効化済み。2026-10-14 に `.wslconfig` で `memory=52GB` / `processors=32` を設定した（それまでは既定の 31 GiB）。実機は i9-14900KF（24 コア / 32 スレッド）、64 GB、RTX 4090 |
| cgroup | v2（cpu / memory / pids などが使える）。`rixyt` の subuid は設定済み |
| Docker | Docker Desktop（WSL integration）。WSL から `127.0.0.1` の publish ポートに届く |
| Tailscale | Windows 側。Serve `https://eagle.tail5877f8.ts.net` → `127.0.0.1:4200` は設定済み |
| 既存の volume | `research-autopilot_pgdata` / `_worker-state`（2026-10-09 作成）。`.env` のパスワードは作成時と同じ（ユーザー回答） |
| GitHub | `inu-wanwan/research-autopilot` は public（認証なしで clone できる） |

---

## 1. 人間が行う操作

順番に行う。各ステップの「確認」が通ってから次へ進む。

### H1. Windows の設定

1. Docker Desktop：Settings > General の「Start Docker Desktop when you sign in」を ON にする。
2. 電源設定：スリープと休止状態を「なし」にする。
3. Windows にログインしていない間は Docker Desktop（PostgreSQL / Prefect Server）が動かない。再起動後の自動ログインをどうするか決める（**判断待ち**）。

### H2. WSL の systemd を有効にする

WSL（rixyt）で `/etc/wsl.conf` に追記する（既存の `[automount]` は残す）：

```bash
sudo tee -a /etc/wsl.conf >/dev/null <<'EOF'

[boot]
systemd=true
EOF
```

Windows の PowerShell で `wsl --shutdown` を実行し、WSL を開き直す。**WSL 上のプロセスはすべて止まる**ので、作業中のものがないときに行う。

`wsl --shutdown` を実行すると、Docker Desktop のエンジン（WSL の `docker-desktop` ディストリ）も止まり、自動では戻らない（2026-10-13 に Eagle で発生）。そのため、続けて Docker Desktop をトレイの Quit Docker Desktop で終了し、起動し直す。

**`wsl --shutdown` をするたびに（`.wslconfig` の変更を反映するときも）、この再起動が必要。** 2026-10-14 に発生した例では、コンテナは healthy のまま、Docker Desktop のポート中継が古いエンジンにつながったままになっていた。症状は次のとおり。
- WSL / Windows から `127.0.0.1:4200` に応答がない（tailnet は 502）
- `127.0.0.1:5432` への接続が `server closed the connection unexpectedly` で切れる

確認：WSL で `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:4200/api/health` が 200 になること。

確認：
- `ps -p 1 -o comm=` が `systemd` になる。
- PowerShell の `wsl -l -v` で `docker-desktop` が Running になる。
- WSL で `docker ps` が使える。

### H3. 専用ユーザー `ra` を作る（sudo）

```bash
sudo apt-get update && sudo apt-get install -y uidmap dbus-user-session systemd-container
sudo adduser --disabled-password --gecos "" ra
id ra                                  # groups に docker / sudo が「ない」こと
grep '^ra:' /etc/subuid /etc/subgid    # 両方に 1 行ずつあること（adduser が自動で追加する）
sudo loginctl enable-linger ra         # ログアウト中も ra のサービスを動かす

# rootless Docker で --cpus を使うため、ユーザーサービスに cpu / cpuset / io も委譲する
sudo mkdir -p /etc/systemd/system/user@.service.d
sudo tee /etc/systemd/system/user@.service.d/delegate.conf >/dev/null <<'EOF'
[Service]
Delegate=cpu cpuset io memory pids
EOF
sudo systemctl daemon-reload

sudo install -d -o ra -g ra /opt/research-autopilot
```

### H4. `ra` で rootless Docker を入れる

`ra` には `machinectl` でログインする。`sudo -u ra` や `su` では systemd のユーザーセッション（`XDG_RUNTIME_DIR`）ができず、rootless Docker のインストールが止まる。

```bash
sudo machinectl shell ra@
# ---- ここから ra ----
curl -fsSL https://get.docker.com/rootless | sh        # ~/bin に入る。2026-10-13 時点の版は 29.9.0
systemctl --user enable --now docker
export PATH=$HOME/bin:$PATH DOCKER_HOST=unix://$XDG_RUNTIME_DIR/docker.sock

# 確認
docker info --format '{{json .SecurityOptions}}'       # "name=rootless" を含むこと
cat /sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.controllers   # cpu memory pids を含むこと
docker run --rm --network none --cpus 1 --memory 512m --memory-swap 512m --pids-limit 64 \
  --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp:rw,size=64m \
  python:3.12-slim python -c 'print("sandbox ok")'      # Phase 9 と同じ制限で動くこと
```

最後のコマンドが失敗したら、出力をそのままエージェントに渡す（Phase 9 の制限が Eagle で使えない可能性があるため）。

### H5. `ra` でコード・CLI・設定を用意する

続けて `ra` のシェルで行う。

```bash
# uv（イメージと同じ版）
curl -LsSf https://astral.sh/uv/0.12.18/install.sh | sh
exec $SHELL -l

# コード（deployment は /opt/research-autopilot で実行される）
git clone https://github.com/inu-wanwan/research-autopilot.git /opt/research-autopilot
cd /opt/research-autopilot && uv sync --frozen --no-dev

# Codex CLI（Node が必要）と Claude Code CLI
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
exec $SHELL -l
nvm install --lts
ln -sfn "$(dirname "$(nvm which default)")" ~/.local/node-bin   # systemd unit の PATH に入っている場所
npm install -g @openai/codex
curl -fsSL https://claude.ai/install.sh | bash
codex login            # ChatGPT のサブスクリプションでログイン（ブラウザが開けなければ codex login --device-auth）
claude                 # 起動して /login。claude.ai のサブスクリプションでログインする（API 課金のログインにしない）
npm install -g @google/gemini-cli
gemini                 # 起動して「Login with Google」を選び、Google AI Pro のアカウントでログインする（API キーにしない）
```

**`.env`**：rixyt 側の `.env` をコピーする。この操作は rixyt のシェルで行う（sudo が必要）。

```bash
sudo install -o ra -g ra -m 600 /home/rixyt/workspace/ResearchAutopilot/.env /opt/research-autopilot/.env
```

`ra` のシェルに戻り、`/opt/research-autopilot/.env` を編集する。

- 追記するもの（`.env.example` の「Eagle host processes」の節）：
  - `PREFECT_API_URL=http://127.0.0.1:4200/api`
  - `PREFECT_API_AUTH_STRING`（`PREFECT_AUTH_STRING` と同じ値）
  - `RA_DATABASE_URL=postgresql+psycopg://prefect:<POSTGRES_PASSWORD>@127.0.0.1:5432/research_autopilot`
  - `RA_STATE_DIR=/home/ra/.local/state/research-autopilot`
- 変更するもの：
  - `RA_CATALOG_ENVIRONMENT=eagle`
  - `RA_WORKTREES_DIR=/home/ra/worktrees`
- `RA_EXP_*`：WSL 52 GB の前提で、2026-10-14 にユーザーと合意した値。
  - `RA_EXP_RESERVE_CPU=4`
  - `RA_EXP_RESERVE_MEMORY_GB=8`
  - `RA_EXP_MAX_JOB_CPU=8`
  - `RA_EXP_MAX_JOB_MEMORY_GB=14`
  - `RA_EXP_MAX_PARALLEL=3`
  - `RA_EXP_MAX_WALLTIME_MIN=480`
- `RA_AUTOPILOT_BUDGET_CPU_HOURS`：既定の 4 だと、上の設定では約 10 分で予算承認を求められる。タスクの規模に合わせて人間が決める。

**研究リポジトリ**（例：private の `inu-wanwan/active_space_research`）。どのリポジトリを入れるかは人間が決める。

1. **clone 用の読み取り専用 deploy key**（ra）。
   - GitHub の repository の Settings > Deploy keys に登録する。Allow write access は OFF。
   - push は下の token で行うので、この鍵には書き込み権限を付けない。
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/active_space_research -N "" -C "ra@eagle"
   cat ~/.ssh/active_space_research.pub          # これを Deploy keys に登録
   printf '%s\n' 'Host github-asr' '  HostName github.com' '  User git' \
     '  IdentityFile ~/.ssh/active_space_research' '  IdentitiesOnly yes' >> ~/.ssh/config
   mkdir -p ~/research-repos
   git clone github-asr:inu-wanwan/active_space_research.git ~/research-repos/active_space_research
   ```
2. **docker compose プラグイン**（ra）。
   - repository の compose イメージで Phase 4 を実行する場合（`container = {...}`）に必要。rootless の静的版には含まれない。
   ```bash
   mkdir -p ~/.docker/cli-plugins
   curl -fsSL -o ~/.docker/cli-plugins/docker-compose \
     https://github.com/docker/compose/releases/download/v5.4.0/docker-compose-linux-x86_64
   chmod +x ~/.docker/cli-plugins/docker-compose && docker compose version
   ```
3. **研究イメージのビルド**（ra）。Phase 4 は、このホストにイメージが無いと止まる。
   ```bash
   cd ~/research-repos/active_space_research && docker compose -p active-space-ra build research
   ```
   - rootless Docker では、コンテナ内で root 以外のユーザー（`active_space_research` では `researcher`、uid 1001）として動くと、`ra` が持っているファイルに書けない（2026-10-14 に Eagle で `Permission denied` を確認）。
     - `active_space_research` の起動処理は root で起動すると `gosu "${RESEARCH_USER}"` でユーザーを切り替えるので、`--user 0 -e RESEARCH_USER=root` で起動すれば root（＝ホスト上の `ra`）のまま動く。書き込みも GPU も確認済み（2026-10-14）。
     - RA には `research_repos.toml` の `[repositories.<id>.container]` に `user = "0"` と `env = { RESEARCH_USER = "root" }` を書く。`test_command` にも `"--user", "0", "-e", "RESEARCH_USER=root"` を付ける。
     - この方式で起動すると `codex-data` volume の所有者が root になる。手で `researcher` として入るときは、先に `docker compose -p active-space-ra run --rm codex-init` を実行する。
4. **`/opt/research-autopilot/config/research_repos.toml`**。
   - `config/research_repos.example.toml` をもとに作る（path は `/home/ra/research-repos/...`）。
   - `test_command` などは Mac の同じファイルの値を使う。
   - 完了後の push と PR を使う repository には `publish = true` と GitHub の `url` を書く（`phase5_autopilot.md` §4b）。
5. **push / PR 用の token**（人間が GitHub で作る。使う場合だけ）。
   - Fine-grained personal access token を作る。
     - Repository access：対象の repository だけ。
     - Permissions：Contents を Read and write、Pull requests を Read and write。
   - `/opt/research-autopilot/.env` の `RA_GITHUB_PUSH_TOKEN` に書く。
   - token の期限が切れると push は失敗して errors に通知される。期限は人間が管理する。

既存の Git 管理外の `outputs/` を Phase 4 のコンテナジョブで参照する場合、対象リポジトリの設定に `read_only_output_dirs = ["phase4_orbital_diagnostics"]` のように直下のディレクトリ名を列挙する。ジョブの固定 commit checkout に同名の `outputs/<name>/` が読み取り専用でマウントされる。設定したディレクトリがホストに存在しなければジョブは停止する。新規実験の出力先は、このマウント先以外にする。Catalog の検証済み判定は別途必要であり、この設定だけで未検証の成果物を検証済みとはみなさない。

**常駐サービスの有効化**：§3 の A1（エージェントの作業）が終わってから行う。

```bash
mkdir -p ~/.config/systemd/user
cp /opt/research-autopilot/infra/host/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ra-prefect-worker ra-discord-bot ra-watchdog
systemctl --user status ra-prefect-worker ra-discord-bot ra-watchdog --no-pager
journalctl --user -u ra-prefect-worker -n 50 --no-pager
```

**⑦ GPU（ML の学習を投げる場合）**

uv で動かすジョブは、WSL の GPU ドライバをそのまま使う。

- `/dev/dxg` は誰でも読み書きできる。CUDA の初期化（`cuInit`）が通ることは rixyt で確認済み（2026-10-14）。
- `ra` では次で確認する：
  ```bash
  python3 -c "import ctypes; l=ctypes.CDLL('libcuda.so.1'); print('cuInit', l.cuInit(0))"   # 0 なら OK
  ```

外部コード（Phase 9）は必ずコンテナで動くので、`ra` の rootless Docker で GPU を使える設定が必要。

1. rixyt（sudo）：nvidia-container-toolkit を入れ、WSL 用の CDI 設定を作る。
   ```bash
   curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
     | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
   curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
     | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
     | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
   sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
   sudo nvidia-ctk cdi generate --mode=wsl --output=/etc/cdi/nvidia.yaml
   nvidia-ctk cdi list
   ```
   `nvidia-ctk runtime configure` は実行しない。Docker Desktop の設定まで書き換えるため。
2. ra：次の 2 つを確認する。**必ず `ra`（`sudo machinectl shell ra@`）で実行する。**
   - rixyt の `docker` は Docker Desktop につながっていて、Ubuntu 側の `/etc/cdi` を見ない。rixyt で実行すると `nvidia.com/gpu=all` が見つからずに失敗する（2026-10-14 に発生）。
   ```bash
   docker info --format '{{json .CDISpecDirs}}'
   docker run --rm --device nvidia.com/gpu=all ubuntu:24.04 nvidia-smi
   ```
   - 1 つ目の出力に `/etc/cdi` が無い場合：`~/.config/docker/daemon.json` に `{"cdi-spec-dirs": ["/etc/cdi"]}` と書き、`systemctl --user restart docker` してから確認し直す。
   - 2 つ目で RTX 4090 が表示されれば完了。2026-10-14 に Eagle で確認済み（RTX 4090、ドライバ 610.88）。
   - `ra` のシェルで `nvidia-smi` が見つからない場合は、`/usr/lib/wsl/lib/nvidia-smi` を使う。表示される `apt install nvidia-utils-…` は WSL では入れない。
- WSL のドライバを更新したら、CDI 設定を作り直す（`nvidia-ctk cdi generate` をもう一度実行する）。
3. ra：研究リポジトリのコンテナで GPU が見えるかを確認する（研究イメージのビルド後）。RA が GPU ジョブで使うのと同じ方式で確認する。
   ```bash
   cd ~/research-repos/active_space_research
   printf 'services:\n  research:\n    devices:\n      - nvidia.com/gpu=all\n' > /tmp/ra-gpu-check.yaml
   docker compose -f compose.yaml -f /tmp/ra-gpu-check.yaml -p active-space-ra run --rm -T --no-deps research nvidia-smi
   ```

### H6. 判断すること（運用開始時）

| 項目 | 既定 | 変えるとき |
|---|---|---|
| `autopilot-poll/scheduled` のスケジュール | inactive | Prefect UI で active にすると自律ポーリングが始まる（`phase5_autopilot.md` §7） |
| `RA_AUTOPILOT_WRITE` / `RA_AUTOPILOT_TEST_ONLY` | `false` / `true` | まず [TEST] タスクで動作を確認してから変える |
| `RA_LINEAR_WRITE_ENABLED` | `false` | Phase 2 の apply を使うとき |
| `RA_ZOTERO_ENABLED` | `true` | Windows の Zotero に中継してつなぐ（H7）。H7 が済むまでは Zotero に届かず、調査は Zotero を使わずに進む |

`/opt/research-autopilot/.env` を変えたら、`systemctl --user restart ra-prefect-worker ra-discord-bot ra-watchdog` で反映する。

### H7. Zotero（Windows の Zotero に中継してつなぐ）

Windows で使っている Zotero（9.0.4）を、そのまま RA の保存先にする。`ra` 用の Zotero アカウントは作らない。

```text
ra の Chromium + Zotero Connector ─→ WSL の 127.0.0.1:23119
  ─→ 中継（ra-zotero-bridge、ra の systemd --user）
  ─→ Windows <WSL から見た既定ゲートウェイ>:23120（portproxy）
  ─→ Windows の 127.0.0.1:23119（Zotero）
```

- WSL（NAT モード）からは Windows の `127.0.0.1` に届かない。そのため中継を置く。
- ゲートウェイの IP（現在 `172.29.96.1`）は再起動で変わる。中継は接続のたびに `/proc/net/route` から調べ直す。ファイアウォールは IP ではなく WSL のインターフェースで絞る。
- WSL の mirrored ネットワークは使わない（Docker Desktop の publish ポートが壊れる報告があるため）。

順番に行う。

**① Windows の Zotero（人間、GUI）**

1. Zotero → 編集 → 設定 → 詳細 → 一般 → 「各種設定」（Miscellaneous）を開く。
2. 「このコンピュータ上の他のアプリケーションが Zotero と通信できるようにする」にチェックを入れる。
   - 9.0.x の日本語版では英語のまま「Allow other applications on this computer to communicate with Zotero」と表示されることがある。
   - 設定名は `extensions.zotero.httpServer.localAPI.enabled`。RA はこの Local API で Zotero を読む。
3. コレクション `ResearchAutopilot` を作り、**選択したままにする**。
   - Connector は、Zotero で選択中のコレクションに保存する。
   - RA は、`ResearchAutopilot` が選択されていなければ保存しない（`zotero check` も失敗する）。
   - 選択中のコレクションは、人間が自分のブラウザの Connector で保存するときの保存先にもなる。手で保存するときは、保存後にコレクションを `ResearchAutopilot` に戻す。
4. Windows のログオン時に Zotero を起動する。
   - `Win + R` で `shell:startup` を開き、Zotero のショートカットを置く。
   - Zotero が起動していないと、RA は Zotero を使わずに OA 取得などへ進む（調査は止まらない）。

**② Windows の portproxy とファイアウォール（人間、管理者の PowerShell）**

WSL が起動している状態で（Ubuntu の端末を開いておく）、管理者として PowerShell を開いて実行する。

```powershell
Set-ExecutionPolicy -Scope Process Bypass
& '\\wsl$\Ubuntu\home\rixyt\workspace\ResearchAutopilot\infra\host\windows\zotero-bridge.ps1'
```

- `\\wsl$` から実行できない場合は、ファイルをデスクトップなどにコピーしてから実行する。
- 何度実行しても同じ設定になる。
- 行うこと：
  - `netsh interface portproxy`：`0.0.0.0:23120` → `127.0.0.1:23119`。
  - ファイアウォール規則「Research Autopilot Zotero bridge (WSL only)」：TCP 23120 を、WSL のインターフェース `vEthernet (WSL (Hyper-V firewall))` からだけ許可する。
  - portproxy に必要な IP Helper サービス（`iphlpsvc`）を自動起動にする。
- 最後に portproxy の一覧と規則名が表示される。

**③ Chromium が動くための OS パッケージ（人間、rixyt で sudo）**

```bash
sudo /opt/research-autopilot/.venv/bin/playwright install-deps chromium
```

- `apt-get` で Chromium に必要なライブラリを入れる（Playwright 1.63 の `install-deps` コマンド）。
- 先に H5 の `uv sync` が終わっている必要がある。

**④ ra で Chromium・Connector・中継を用意する（人間、`sudo machinectl shell ra@`）**

```bash
cd /opt/research-autopilot
uv run --frozen --no-dev playwright install chromium
uv run --frozen --no-dev research-agent literature zotero fetch-connector
```

- `fetch-connector` は Chrome Web Store の**その時点の最新版**を `~/.research-autopilot/zotero/connector` に展開する。版は選べない。
- 出力の `version` を確認する。検証済みは 5.0.217。違う版なら、⑤ の確認を回帰確認として必ず行う。
- 一度入れたら、更新するとき以外は実行しない。

中継サービスを入れて起動する。

```bash
cp /opt/research-autopilot/infra/host/systemd/ra-zotero-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ra-zotero-bridge
journalctl --user -u ra-zotero-bridge -n 20 --no-pager   # "zotero bridge: 127.0.0.1:23119 -> ..." が出る
```

`/opt/research-autopilot/.env` に追記する。

```bash
RA_ZOTERO_ENABLED=true
RA_ZOTERO_SYNC_DIR=/mnt/c/Users/rixyt/GDriveSync/GDriveZotero
RA_ZOTERO_HEADLESS=1
```

- `RA_ZOTERO_SYNC_DIR` は、Windows の ZotMoov の移動先（＝ Zotero の Linked Attachment Base Directory、`extensions.zotero.baseAttachmentPath`）`C:\Users\rixyt\GDriveSync\GDriveZotero` の WSL 側のパス。
  - RA の自動検出は Linux の `~/.zotero` を読むので、Windows の Zotero の設定は見つけられない。そのため明示する。
- `RA_ZOTERO_HEADLESS=1` は既定値（書かなくても同じ）。

反映する（実行中の run がないことを確かめてから）。

```bash
systemctl --user restart ra-prefect-worker ra-discord-bot ra-watchdog
```

**⑤ 確認（ra）**

1. Zotero に届くか（rixyt でも実行できる）。
   ```bash
   curl -s http://127.0.0.1:23119/connector/ping
   # <!DOCTYPE html><html><body>Zotero is running</body></html>
   curl -s "http://127.0.0.1:23119/api/users/0/collections?limit=1"
   # JSON が返る。"Local API is not enabled" なら ① の 2 をやり直す
   ```
   - 何も返らない（`Empty reply from server`）：Zotero が起動していない、または ② が済んでいない。
2. RA の事前確認。
   ```bash
   cd /opt/research-autopilot
   uv run --frozen --no-dev research-agent literature zotero check
   # {"ok": true, "collection": "ResearchAutopilot", "sync_dir": true}
   ```
3. 実際に 1 件保存する。LLM を使うので少額の費用がかかる（Mac の BOCS 調査で約 $0.5）。
   - Zotero にまだ入っていない OA の arXiv 論文を seed にする。既にあると、保存せずに再利用する。
   ```bash
   uv run --frozen --no-dev research-agent literature research "<確認用の質問>" --seed arXiv:<id> --no-note
   ```
   - 見るところ：
     - 出力の `sources` で、その論文の取得方法が `zotero_connector`、状態が `ok`。
     - Zotero の `ResearchAutopilot` コレクションに項目と PDF が増えている。
     - PDF が `C:\Users\rixyt\GDriveSync\GDriveZotero`（WSL では `/mnt/c/Users/rixyt/GDriveSync/GDriveZotero`）にある。
     - 同じ DOI の項目が二重にできていない。
   - `failures` に `zotero connector ...` が出たら、その詳細をエージェントに渡す。

**セキュリティ上の注意**

- ポート 23120 は WSL のインターフェースからだけ許可している。LAN や Tailscale からは届かない。
  - portproxy 自体は `0.0.0.0` で待ち受ける。ほかのファイアウォール規則で 23120 を開けないこと。
- WSL の中のものは、中継を通して Zotero の Connector と Local API を使える。`ra` 以外の WSL ユーザー（`rixyt` を含む）や、WSL の中のコンテナ・プロセスも含む。
  - Zotero 9 の Local API は読み取り専用で認証がない。ライブラリ全体を読める。
  - Connector 経由では、選択中のコレクションに項目を保存できる。
- Zotero は `Host` が `127.0.0.1` / `localhost` 以外の要求を拒否する。中継は `Host` を書き換えないので、この確認は通る。

**この方式の制約**

- Zotero に元からある PDF を再利用できるのは、基本ディレクトリ（`attachments:` で始まるパス）にあるものだけ。
  - Windows の絶対パス（`C:\...`）でリンクされたファイルや、Zotero の `storage` に取り込まれたファイルは WSL から見つけられない。その場合は Connector で保存し直さず、OA 取得などに進む。

---

## 2. コードを更新するとき（人間、ra で）

```bash
sudo machinectl shell ra@
cd /opt/research-autopilot && git pull && uv sync --frozen --no-dev
systemctl --user restart ra-prefect-worker ra-discord-bot ra-watchdog   # 実行中の run がないことを確かめてから
systemctl --user restart ra-zotero-bridge   # infra/host/zotero_bridge.py が変わったときだけ
```

続けて、エージェント（または rixyt）が同じ commit で `prefect deploy --all` を行う（§3 A2）。

**注意：`prefect deploy --all` を実行すると、`autopilot-poll/scheduled` のスケジュールが無効に戻る。** `prefect.yaml` で `active: false` と定義しているため（初回の登録で勝手に動き出さないようにする設定）。運用中に deployment を登録し直したら、必ずスケジュールを有効に戻す（Prefect UI の deployment 画面、または API で `update_deployment_schedule(..., active=True)`）。2026-10-14 に一度、約 1 分止まった。

## 3. エージェントが行う操作（rixyt で実行できるもの）

人間はエージェントに「Eagle の A1 を実行して」と依頼すればよい。

- **A1**（H3 のあと、H5 のサービス有効化より前）
  1. `~/workspace/ResearchAutopilot` に clone し、`.env` を `chmod 600` で置く。
  2. `docker compose --env-file .env -f infra/compose.yaml -f infra/compose.host.yaml up -d` で postgres と prefect-server だけを起動する。
  3. `research_autopilot` DB を作り、`research-agent db init` を実行する。
  4. ポートを確認する：4200 と 5432 が Windows 側でも `127.0.0.1` だけで待ち受けていること。
- **A2**（H5 のあと）
  1. zombie run 検出の automation を登録する（初回のみ）。
  2. `GIT_SHA=$(git rev-parse --short HEAD) uv run prefect deploy --all --no-prompt` を実行する。
  3. `smoke-success/manual` を実行し、ra の worker が処理することを確認する。
  4. `research-agent discord check` を実行する。
  5. tailnet 経由で `/api/health` が 200、認証なしが 401 になることを確認する。

## 4. 判断待ち・未検証

- Windows 再起動後の自動ログイン（H1）。
- 研究リポジトリの選定と `RA_EXP_*` の値（H5）。
- Eagle では未確認のもの：
  - `ra` での Codex / Claude のログイン（特に headless）
  - rootless Docker 上の Phase 9 の通し E2E
  - Zotero の中継（H7）：Windows 側の portproxy・ファイアウォール規則と、WSL → Windows Zotero の通し確認、Connector での実保存
- 古い container worker の state volume（`research-autopilot_worker-state`、Phase 1 の試験データ）は移行しない。消すかどうかは人間が決める（消すと元に戻せない）。
- `infra/compose.yaml` の `prefect-worker` / `discord-bot` は profile `container-worker` に入れて止めている。ra のサービスと**同時に動かさない**（worker が二重に poll し、Discord gateway が二重に接続される）。
