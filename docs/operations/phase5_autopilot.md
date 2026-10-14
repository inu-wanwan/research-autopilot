# Phase 5 運用ハンドブック：Autopilot（承認ゲート付き自律研究ワークフロー）

仕様は `docs/phase_specs/phase5.md`。Phase 2〜4 の運用は `phase2_planning.md` / `phase3_implementation.md` / `phase4_experiments.md`。

## 1. 利用者から見た流れ

1. ChatGPT と相談して、Notion Research Tasks（Purpose / Implementation / Acceptance criteria / Scientific invariants / Repository / Allowed files）と Linear Issue（project Research、ラベル `autopilot`、Todo）を作る。
2. Autopilot が polling でタスクを見つけ、**初回の委任承認（onboarding）**を求める。Linear は Awaiting Approval になり、Discord の approvals チャンネルに通知とボタンが届く。
3. Discord で Approve / Reject / Request Revision を選ぶ。修正依頼ではモーダルに理由を書く。Linear の Status 変更だけでは承認にならない。
4. 承認後は自律的に進む：計画（Phase 2）→ 実装（Phase 3）→ 実験・検証・分析（Phase 4）→ 範囲内の追加実験の登録と実行 → Notes への書き込み → Linear へのコメント → Done。結果・エラー・主要遷移は用途別の Discord チャンネルに届く。
5. 途中で人間の判断が必要になるのは、範囲外の提案、invariants や acceptance の変更、予算の超過、自動では完了を判断できない場合だけ。判断は同じ Discord UI で行う。

## 2. Discord 通知と承認の仕組み

既定は `RA_NOTIFICATION_BACKEND=discord` / `RA_APPROVAL_MODE=discord`。Prefect worker は通知を PostgreSQL の outbox に保存し、別プロセスの `discord-bot` が Gateway 接続を維持して送信・Interaction 受信を行う。Discord は UI であり、最終判定は Approval Controller、承認監査の正本は PostgreSQL、タスク状態の正本は Linear のまま変わらない。

| 確認すること | 方法 |
|---|---|
| 誰が操作したか | Discord Interaction の user ID が `RA_DISCORD_AUTHORIZED_USER_IDS` に含まれること。bot user は拒否 |
| どこで操作したか | 指定 guild の approvals チャンネル内だけを許可 |
| 承認要求に対する操作か | custom_id には request ID と action だけを含め、DB の pending request と照合。秘密・仕様本文は含めない |
| 二重操作でないか | Interaction ID と request ID を DB で一意にし、同じ request の最初の入力だけを保存 |
| 古い承認でないこと | 承認要求の spec hash と、現在の Notion の仕様の hash が一致すること（仕様が編集されていれば stale として出し直す） |
| 有効期限 | 承認要求は7日、ResearchAuthorization は30日 |
| 誰が最終決定するか | polling 時に Approval Controller が DB の Interaction を再検証して `approval_decisions` に確定する。Discord handler 自身は承認しない |

確定後、Approve は実行状態（Todo / In Progress）へ進み、Reject は Canceled、Request Revision は Revision Requested として Linear に反映する。元メッセージのボタンは無効化される。Linear の手動 Status 変更は可視化には使えても、承認の証拠にはしない。

チャンネルの用途：

| チャンネル | イベント |
|---|---|
| approvals | onboarding、範囲外 proposal、予算、完了レビューなど人間の判断が必要なもの |
| results | 実験完了、解析完了、タスク完了 |
| errors | 復旧不能エラー、予算超過、worker failure |
| logs | task 開始、phase 遷移、自動 retry、repair 成功、proposal 生成 |

`linear` / `linear+form` と ntfy adapter は既存環境の移行用にコードを残すが、日常運用では使わない。Coding Agent / Reviewer / 検証コマンドには Discord Bot token を含む Research Autopilot の資格情報を渡さない。

## 3. 必要な設定（人間が行うこと）

| 項目 | 内容 | 状態（2026-10-10） |
|---|---|---|
| Linear の workflow state | INU チームに Awaiting Approval / Approved / Revision Requested を追加（却下は既存の Canceled） | 設定済み（2026-10-10） |
| Notion integration | capabilities で Insert content と Update content を ON にする。Research Tasks と Research Notes の両方に接続する | 設定済み（書き込みを確認） |
| Discord backend | `RA_NOTIFICATION_BACKEND=discord`、`RA_APPROVAL_MODE=discord` | Mac で検証済み |
| Bot / guild / 承認者 | `RA_DISCORD_BOT_TOKEN`、`RA_DISCORD_GUILD_ID`、`RA_DISCORD_AUTHORIZED_USER_IDS` | `.env` に設定（値は Git 管理外） |
| 4チャンネル | `RA_DISCORD_{APPROVAL,RESULTS,ERRORS,LOGS}_CHANNEL_ID` | Mac から実 Bot で権限・テスト投稿を確認済み（2026-10-10） |
| `.env` | `RA_AUTOPILOT_WRITE=true` で書き込みを有効にする。`RA_AUTOPILOT_TEST_ONLY=true`（Mac）にすると [TEST] タスクだけに書き込む。`RA_MODEL_PRICING` は設定済み | Mac では TEST_ONLY のまま |
| Prefect deployment | `autopilot-poll/scheduled`（5分間隔、inactive で作成）、`approval-confirm/manual` | `prefect deploy --all` で作る |

## 4. コマンド

```bash
uv run research-agent autopilot poll --check-only          # 候補とゲートの確認だけ（書き込み・LLM なし）
uv run research-agent autopilot poll [--task INU-123]      # 1回分の処理（Prefect flow として実行。--local で Prefect を使わない）
uv run research-agent autopilot status                     # ワークフロー、承認待ち、outbox
uv run research-agent discord check                        # token / guild / 4 channel / 権限の検証
uv run research-agent discord check --send-test            # 4チャンネルにテスト Embed を1件ずつ投稿
uv run research-agent discord run                          # Gateway consumer（通常は compose service で常駐）
uv run research-agent autopilot cancel INU-123             # Autopilot の作業を止める（Linear は人間が操作する）
uv run research-agent autopilot resume INU-123 --stage planning   # 原因を直した後、blocked / dead_letter の run を再開する
uv run prefect deployment run autopilot-poll/scheduled     # 手動で起動する
```

blocked や dead_letter は errors、retry や stage 遷移は logs に届く。通知は送信成功後に Discord message ID とともに確定し、失敗は指数 backoff で再送する。Bot 再起動後は pending/failed 通知と承認待ちの persistent view を DB から復元する。通知障害は通常の自動処理を失敗扱いにせず、承認通知障害でも自動承認しない。Prefect UI では artifact `autopilot-status` で run の状態、blocked の理由、承認待ち、outbox を確認できる。

### Research Notes の結果レポート

結果ページは、「結論」「計算結果」「検証状況」「注意点」「次のアクション」と、折りたたみの「参照・再現情報」で構成する。表の数値は表示用に丸めるが、未丸め値と詳細な検証・監査情報は PostgreSQL / ExperimentManifest / JSON artifacts に残る。主要 artifact は repo-relative path で示し、worker の一時絶対パスや checksum は本文に展開しない。既存の結果ページは自動更新せず、この形式は新しく作成する Notes に適用される。

## 4b. 完了したタスクの push と Pull Request（2026-10-14 ユーザー決定）

Phase 3 の仕様（merge と push はしない）に対する追加。ユーザー決定（2026-10-14）は次の 4 点。

- 承認を求める時点：人が投げたタスクの完了時
- 承認方法：Discord のボタン
- 承認後に行うこと：タスクブランチを push し、Pull Request を作成する
- **PR の単位：人が投げたタスク 1 件**

PR の単位の詳細：

- Phase 10 の研究タスクでは、研究の完了時に 1 回だけ依頼が届く。
  - push する commit：サイクルの子タスクのうち、実験の検証まで通った最後の実装。子タスクは前の子の commit を土台に積み上がるので、それまでの変更がすべて入る。
  - 最後の commit の祖先でない実装があれば、依頼と PR に「含まれない」と明記する。
- サイクルの子タスク、6B の子タスク、自動のフォローアップには依頼を出さない。
- ブランチ名は `autopilot/<タスクID>`（例 `autopilot/inu-292`）。研究を Resume して再び完了すると新しい依頼が届き、承認すると同じブランチを進めて（fast-forward）PR が更新される。

流れ：

1. タスク（研究タスクなら研究）が完了すると、approvals チャンネルに「push して PR を作ってよいか」の依頼（種類 `publish`）が届く。
   - 依頼が届くのは、`config/research_repos.toml` で `publish = true` かつ GitHub の `url` がある repository だけ。
   - `RA_GITHUB_PUSH_TOKEN` が空なら依頼は作らない。
   - Linear の状態は Done のまま変わらない。
2. Approve すると、次のポーリングで次の 2 つを行う。
   - その commit を `autopilot/...` ブランチとして push する。
   - base branch への PR を作成し、results チャンネルに PR の URL を送る。
3. Reject すると何もしない。
4. merge は GitHub で人が行う。

安全上の約束：

- push するのは承認した (repository, branch, commit, base) だけ。
  - repository の `publish` を外すか、`url` を変えると、依頼は失効する。
- base branch への push、force push、merge はしない。`autopilot/` で始まらないブランチは拒否する。
- token は push のときだけ環境変数（`GIT_CONFIG_*`）で git に渡す。argv、remote URL、`.git/config`、ログには残らない。
  - Implementer / Reviewer の環境からは除かれる（`TOKEN` を含む変数名はすべて除去される）。

失敗したとき：

- 通信などの一時的な失敗は、次のポーリングで再試行する（最大 5 回）。
- リモートのブランチが進んでいる（non-fast-forward）、権限がない、などの失敗では再試行しない。errors チャンネルに通知する。
- 原因を直したら `research-agent autopilot publish-retry <TASK>` を実行する。承認はそのまま有効。

## 5. 上限と予算（初期値）

研究タスクの同時実行は1件。1回の実験からの新しい提案は最大3件、連鎖の深さは3、保留できる提案は5件。予算は root タスクとそのすべての追加実験の累計で、API $5、計算 4 CPU 時間（`RA_AUTOPILOT_BUDGET_*`）。超過したら BUDGET 承認を求める（承認すると予算は2倍になる）。再試行・fallback・修正で予算がリセットされることはない。

## 6. 状態と復旧

- WorkflowRun の stage：discovered → (awaiting_authorization) → planning → implementing → experimenting → proposing → completing → done。ほかに awaiting_approval / blocked / cancelled / dead_letter。
- 各 stage は永続化しているので、polling が途中で落ちても次の polling で同じ stage から続く。一時的な失敗は3回まで再試行し、それを超えたら dead_letter にして通知する。
- Notion / Linear への書き込みは outbox（`autopilot_outbox`）で行う。タイトルの `[ap:<hash>]` マーカーで既存の書き込み結果を見つけて取り込むので、二重に作成されない。5回失敗したら DEAD（`autopilot status` で確認し、原因を直して `resume`）。
- polling 全体を lease（`phase5:poll`）で排他するので、同時に2つの polling が動いても二重に着手しない。

## 7. Eagle への一括反映手順（Phase 5 完了後。今回は実施しない）

1. **事前準備**：Mac で `uv run pytest` がすべて通ることを確認する。`main` を確定する。`.env` の差分を確認する（`RA_AUTOPILOT_*`、`RA_EXP_*`、`RA_IMPL_*`、`RA_MODEL_PRICING`、`NOTION_NOTES_DATA_SOURCE_ID`）。
2. **バックアップ**：`recovery.md` §5 の手順で、Prefect の DB と `research_autopilot` DB の両方を `pg_dump -Fc` で取る。
3. **止める順序**：まず `autopilot-poll` のスケジュールを inactive にする。次に実行中の flow が終わるのを待つ（`autopilot status`、`experiment list`）。worker を止めるのは、Phase 4 のジョブが走っていないことを確認してから。
4. **migration**：`research-agent db init`（`create_all` は既存のテーブルを変更しない。Phase 5 のテーブルを追加する）。
5. **worker の更新**：イメージを作り直す（Codex / Claude CLI と uv を含む。CLI の認証方式は Eagle でまだ検証していない）。volume（artifacts、env cache、worktrees、研究リポジトリ）と `config/research_repos.toml` も用意する。
6. **deploy**：`GIT_SHA=... prefect deploy --all`。最初は `RA_AUTOPILOT_TEST_ONLY=true` のまま [TEST] タスクで動かす。
7. **復旧訓練**：ジョブの実行中に worker を再起動し、`experiment resume` と `autopilot poll` で回復することを確認する。承認待ちの最中に server を再起動し、承認を確認する処理が続くことを確認する。
8. **本番化**：`RA_AUTOPILOT_TEST_ONLY=false` にし、`autopilot-poll` のスケジュールを active にする。
9. **rollback**：スケジュールを inactive にする → 1つ前の commit のイメージで worker を作り直す → 必要なら DB を復元する（`pg_restore`）。Phase 5 のテーブルを使わない古いコードはそれらを無視するので、テーブルを消す必要はない。
