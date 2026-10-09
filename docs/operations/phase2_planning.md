# Phase 2 運用：タスク計画（Task Manager / Orchestrator）

仕様は `docs/phase_specs/phase2.md`、接続の事前設定は `phase2_prerequisites.md` を参照。

## 1. 全体の流れ

```text
research-agent plan / Prefect deployment plan-task/manual
  └─ flow "plan-task"（Phase 1 の work pool research-local の worker で実行）
       1. Linear issue（実行状態の正本）と Notion page（仕様の正本）を取得
       2. 決定論的ゲート（下表）── 失敗 → blocked / insufficient_spec / transient_failure
       3. Planner (gpt-5.6-terra) → 4. Reviewer (gpt-5.6-sol)。毎回レビューし、再計画は最大2回
       6. 最新データで最後にもう一度ゲートを確認（計画中に状態・仕様が変わっていれば blocked）
       7. apply モードのときだけ Linear → In Progress（Done にはしない）
       8. 記録を JSON と PostgreSQL に保存し、Prefect artifact を作る
       9. ready なら Phase 3 用の handoff JSON を書く
```

## 2. 決定論的ゲート

| ゲート | 条件 | 失敗したときの decision / code |
|---|---|---|
| プロジェクト | issue が `LINEAR_PROJECT_ID` のプロジェクトにある | blocked / `out_of_scope` |
| スコープ | issue に `RA_SCOPE_LABEL`（既定 `autopilot`）が付いている | blocked / `out_of_scope` |
| 人間による承認 | Linear の state type が `unstarted`（Todo）で、かつ Notion の `Pending` が false | blocked / `not_approved` |
| Status | In Progress / Done / Canceled は対象外 | blocked / `invalid_status` |
| 依存 | Linear の blocked-by の issue がすべて `completed` | blocked / `dependency_incomplete` |
| 対応関係 | Notion の `Linear Issue ID` と、Linear にある Notion リンクが一致し、Notion 側に重複がない | blocked / `data_inconsistency` |
| 仕様 | Notion 本文に Purpose / Implementation（実験）/ Acceptance criteria の見出しがある（`RA_REQUIRED_SECTIONS`） | insufficient_spec / `missing_spec` |
| 排他 | 同じタスクを扱う有効なリースがない（TTL 1時間） | blocked / `concurrent_run` |

見出しの対応表は `src/research_autopilot/tasks/sections.py` にある（例：「目的」→ Purpose、「完了条件」→ Acceptance criteria、「Work」→ Implementation）。対応表にない見出しは、`other_sections` として Planner に渡す。

Planner / Reviewer の後に決まる decision：Reviewer が `blocked` → blocked / `review_blocked`。再計画2回でも承認されない → blocked / `revision_limit`。LLM の出力が2回続けて不正 → blocked / `llm_output_invalid`。OpenAI の一時障害が続く → transient_failure。認証エラーや quota 不足 → blocked / `connector_error`。

## 3. 実行モード

| モード | CLI | LLM | 外部への書き込み |
|---|---|---|---|
| `check_only` | `--check-only` | 使わない | なし |
| `dry_run`（既定） | なし | 使う | なし（ローカル DB と JSON にだけ記録） |
| `apply` | `--apply` | 使う | ready のときだけ Linear → In Progress。worker 側にも `RA_LINEAR_WRITE_ENABLED=true` が必要（二重の鍵） |

どのモードでも、Notion への書き込み、Linear へのコメント、Linear を Done にする操作はしない。

## 4. CLI（MacBook）

```bash
set -a; . ./.env; set +a      # 秘密情報はシェル環境に入るだけで、表示はされない
export PREFECT_API_URL=https://<TS_FQDN>/api PREFECT_API_AUTH_STRING="$PREFECT_AUTH_STRING"   # Prefect UI に履歴を残す場合

uv run research-agent plan --task INU-123 --check-only     # ゲートだけ確認（無料）
uv run research-agent plan --task INU-123                  # 計画とレビュー（dry-run）
uv run research-agent plan --next                          # 次の候補を自動で選ぶ（priority の高い順 → 作成日時の順）
RA_LINEAR_WRITE_ENABLED=true uv run research-agent plan --task INU-123 --apply
uv run research-agent plan --task INU-123 --local --json   # Prefect を通さずに実行（デバッグ用）

uv run research-agent runs list [--task INU-123]           # 履歴（status、トークン数、推定コスト）
uv run research-agent runs show <plan_run_id>              # 記録全体（入力のスナップショット、計画、レビュー、判断）
```

終了コード：0 ready、2 blocked、3 insufficient_spec、4 transient_failure。

MacBook で `RA_DATABASE_URL` を設定しない場合、履歴は `.prefect/research-autopilot-state/research_autopilot.db`（SQLite）に保存される。本番の履歴は研究室サーバーの PostgreSQL にある。

## 5. 研究室サーバー（Eagle）での実行

### 初回のセットアップ

```bash
cd ResearchAutopilot && git pull
# .env に Phase 2 の5変数（phase2_prerequisites.md §1）を設定。必要なら RA_* の設定も
alias ra-compose='docker compose --env-file .env -f infra/compose.yaml'
ra-compose exec postgres sh -c 'createdb -U "$POSTGRES_USER" research_autopilot'   # 初回のみ。既にあればエラーになるだけ
sed -i "s/^GIT_SHA=.*/GIT_SHA=$(git rev-parse --short HEAD)/" .env
ra-compose build prefect-worker
ra-compose up -d prefect-worker          # worker を作り直す（実行中の run がないことを確認してから）
ra-compose exec prefect-worker research-agent db init
```

MacBook から：`GIT_SHA=$(git rev-parse --short HEAD) uv run prefect deploy --all --no-prompt`

### 実行

```bash
uv run prefect deployment run plan-task/manual -p task_id=INU-123                  # dry-run
uv run prefect deployment run plan-task/manual -p next_task=true
uv run prefect deployment run plan-task/manual -p task_id=INU-123 -p mode=apply     # worker の RA_LINEAR_WRITE_ENABLED=true が必要
ra-compose exec prefect-worker research-agent runs list
```

Prefect UI では、flow run `plan-<task>-<mode>` のログ（判断理由）と、artifact `plan-<task>`（decision、トークン数、推定コスト、記録の場所）を確認できる。

### 保存先

| データ | 場所 |
|---|---|
| 実行記録（PlanRunRecord） | PostgreSQL の `research_autopilot.plan_runs` と `/var/lib/research-autopilot/state/plan_runs/<task>/<plan_run_id>.json` |
| Linear への更新の監査ログ | `research_autopilot.linear_status_audit` |
| 排他用のリース | `research_autopilot.task_leases` |
| Phase 3 用の handoff | `/var/lib/research-autopilot/state/handoffs/<task>/<plan_id>.json` |

`research_autopilot` DB は Prefect の DB と同じ PostgreSQL サーバーにある別の DB。バックアップは `recovery.md` §5 と同じ手順で、`-d research_autopilot` を指定して取る。

## 6. コスト

- 各 LLM 呼び出しの入力・出力・キャッシュ・推論のトークン数を記録する。
- 推定料金は `RA_MODEL_PRICING`（USD / 100万トークン）が設定されたモデルについてだけ計算する。**単価が分からないモデルの料金は null（推定できない）とし、数値を作り上げない。** 1回の実行に単価不明の呼び出しが含まれる場合、合計も null になる（`cost_complete=false`）。
- 失敗した LLM 呼び出しのトークン数は SDK から取れないので 0 として記録し、その実行の `cost_complete` は false になる。
- 実際の請求額は OpenAI の Usage 画面で確認する。予算上限は OpenAI 側で設定する（`phase2_prerequisites.md` §2）。

## 7. 安全性について

- Notion は読み取りだけ。Linear への書き込みは「apply モード ＋ `RA_LINEAR_WRITE_ENABLED=true` ＋ ready ＋ 最後のゲート確認を通過」のときだけ行い、更新先は state type が `started` の state に限る。
- 人間による承認（Linear Todo ＋ Pending=false）と Reviewer の承認は別々に記録する（`human_approval` と `reviewer_verdict`）。
- 認証情報は `SecretStr` で保持し、記録・ログ・artifact には出さない（テストで確認している）。
- OpenAI Agents SDK の tracing は既定で無効（`RA_AGENTS_TRACING=true` で有効になる）。

## 8. Eagle（Windows + WSL2）について

Eagle は Windows + WSL2 上で Docker を動かしている（2026-10-10 ユーザー回答）。Compose・`.env`・コマンドは WSL2 の Linux シェルで実行する。秘密情報を置く `.env` は WSL2 側のリポジトリ直下（Linux のファイルシステム上。`chmod 600` が効く場所）に置く。
