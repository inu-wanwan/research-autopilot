# Phase 5 運用ハンドブック：Autopilot（承認ゲート付き自律研究ワークフロー）

仕様は `docs/phase_specs/phase5.md`。Phase 2〜4 の運用は `phase2_planning.md` / `phase3_implementation.md` / `phase4_experiments.md`。

## 1. 利用者から見た流れ

1. ChatGPT と相談して、Notion Research Tasks（Purpose / Implementation / Acceptance criteria / Scientific invariants / Repository / Allowed files）と Linear Issue（project Research、ラベル `autopilot`、Todo）を作る。
2. Autopilot が polling でタスクを見つけ、**初回の委任承認（onboarding）**を求める。Linear は Awaiting Approval になり、ntfy に通知が届く。
3. 承認する：**Linear の Status を Approved にするだけ**（既定の `RA_APPROVAL_MODE=linear`）。却下は Canceled、修正依頼は Revision Requested。スマホの Linear アプリから操作できる。
4. 承認後は自律的に進む：計画（Phase 2）→ 実装（Phase 3）→ 実験・検証・分析（Phase 4）→ 範囲内の追加実験の登録と実行 → Notes への書き込み → Linear へのコメント → Done → ntfy への通知。
5. 途中で人間の判断が必要になるのは、範囲外の提案、invariants や acceptance の変更、予算の超過、自動では完了を判断できない場合だけ。どれも同じ「Linear の Status ＋ Prefect のフォーム」で答える。

## 2. 承認の仕組み（承認者の確定方法）

既定は `RA_APPROVAL_MODE=linear`（2026-10-10 ユーザー決定）。次の条件をすべて満たす Linear の Status 変更だけを承認として受け付ける。

| 確認すること | 方法 |
|---|---|
| 誰が変更したか | Linear の history の `actor.id` が `RA_APPROVER_LINEAR_USER_IDS` に含まれていること |
| bot による変更でないこと | `botActor` が null であること |
| 承認要求に対する操作であること | Autopilot が Awaiting Approval にした**後**に、Approved / Revision Requested / Canceled へ遷移していること |
| 自分自身による承認でないこと | Autopilot は承認用の状態へ遷移させることができない（コードで禁止）。自分の書き込みはすべて `autopilot_linear_writes` に記録し、同じ状態への書き込みがあれば無効とする |
| 古い承認でないこと | 承認要求の spec hash と、現在の Notion の仕様の hash が一致すること（仕様が編集されていれば stale として出し直す） |
| 有効期限 | 承認要求は7日、ResearchAuthorization は30日 |

**残るリスクと対策**：Personal API key で行った更新は、ユーザー本人の操作として記録される（2026-10-10 に確認）。同じ key を持つ別のプログラムがあれば、承認を偽装できてしまう。

- 対策①：Coding Agent（Codex / Claude）、Reviewer、検証コマンドに渡す環境変数から、Autopilot の認証情報（Notion / Linear / OpenAI の key、Prefect の認証、DB の URL）を取り除く（`sanitized_env`）。
- 対策②：自分の書き込みを照合する。
- **根本対策**：Autopilot 専用の Linear ID（OAuth アプリ、または別ユーザーの key）を使えば、actor で区別できる。Eagle に反映するときに導入を検討する。

`RA_APPROVAL_MODE=linear+form` にすると、Prefect の Human-in-the-loop フォーム（suspend した flow、`approval-confirm/manual`）による確認も必須になる。Prefect UI v2（3.8.8）では、suspend した run の Resume の場所が分かりにくい（2026-10-10、ユーザーがメニューで見つけられなかった）。そのため、送信は `research-agent autopilot approve <request_id> --decision approve --approver <name>`（Prefect API の資格情報が必要）で行う。

## 3. 必要な設定（人間が行うこと）

| 項目 | 内容 | 状態（2026-10-10） |
|---|---|---|
| Linear の workflow state | INU チームに Awaiting Approval / Approved / Revision Requested を追加（却下は既存の Canceled） | 設定済み（2026-10-10） |
| Notion integration | capabilities で Insert content と Update content を ON にする。Research Tasks と Research Notes の両方に接続する | 設定済み（書き込みを確認） |
| 承認者 | `RA_APPROVAL_MODE=linear`、`RA_APPROVER_LINEAR_USER_IDS=<あなたの Linear user id>` | Mac の検証では設定済み |
| ntfy | `RA_NOTIFY_URL=https://ntfy.sh`、`RA_NOTIFY_TOPIC=<推測されにくい名前>`。スマホの ntfy アプリで購読する | 未設定（今は通知がログに出るだけ） |
| `.env` | `RA_AUTOPILOT_WRITE=true` で書き込みを有効にする。`RA_AUTOPILOT_TEST_ONLY=true`（Mac）にすると [TEST] タスクだけに書き込む。`RA_MODEL_PRICING` は設定済み | Mac では TEST_ONLY のまま |
| Prefect deployment | `autopilot-poll/scheduled`（5分間隔、inactive で作成）、`approval-confirm/manual` | `prefect deploy --all` で作る |

## 4. コマンド

```bash
uv run research-agent autopilot poll --check-only          # 候補とゲートの確認だけ（書き込み・LLM なし）
uv run research-agent autopilot poll [--task INU-123]      # 1回分の処理（Prefect flow として実行。--local で Prefect を使わない）
uv run research-agent autopilot status                     # ワークフロー、承認待ち、outbox
uv run research-agent autopilot approve <request_id> --decision approve --approver <name>   # linear+form モードのとき
uv run research-agent autopilot cancel INU-123             # Autopilot の作業を止める（Linear は人間が操作する）
uv run research-agent autopilot resume INU-123 --stage planning   # 原因を直した後、blocked / dead_letter の run を再開する
uv run prefect deployment run autopilot-poll/scheduled     # 手動で起動する
```

blocked や dead_letter になったら ntfy に通知が届く（同じ通知は二度送らない）。Prefect UI では、artifact `autopilot-status` で run の状態、blocked の理由、承認待ち、outbox を確認できる。

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
