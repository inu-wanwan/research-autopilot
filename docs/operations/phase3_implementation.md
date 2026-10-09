# Phase 3 運用：Implementer / Code Reviewer と Phase 4 インターフェース

仕様は `docs/phase_specs/phase3.md`（§10 は Phase 4 連携の追加仕様）。

## 1. 流れ

```text
research-agent implement --task INU-123   (Prefect flow "implement-task")
  1. Phase 2 の handoff を読む（最新、または --handoff で指定）
  2. 承認ゲート：apply モードの handoff ＋ Phase 2 の run 記録が ready ＋ Linear が In Progress ＋ Notion の仕様が計画時から変わっていない
  3. Notion の Repository / Allowed files → リポジトリ登録ファイルで解決（未登録・未記載なら blocked）
  4. リース（全体で1件、タスクごとに1件）→ worktree の作成とロック
  5. 難易度・科学的リスクの分類 → モデルの選択
  6. 試行：実装（Codex、失敗時は Claude）→ commit → レビュー（決定論的検証 → LLM）
     修正は最大2回。同じ種類の失敗が続いたときだけ昇格する。fallback は1回まで。トークン・費用・時間に上限がある
  7. pass したら、固定 commit、ExperimentSpec、Phase 4 用の handoff、manifest を作る（merge / push はしない）
```

## 2. 準備

### リポジトリ登録ファイル

`config/research_repos.example.toml` を `config/research_repos.toml` にコピーして編集する（Git 管理外。パスは端末ごとに違うため）。`RA_REPOS_FILE` で場所を変えられる。

```toml
[repositories.h2-test]
path = "~/research-repos/h2-test"
base_branch = "main"
protected_paths = ["data/validated/**", "results/**"]
test_command = ["uv", "run", "pytest", "-q"]
lint_command = ["uv", "run", "ruff", "check", "."]
typecheck_command = ["uv", "run", "mypy", "."]
```

### Notion のタスク本文（人間が書く）

| 見出し | 内容 |
|---|---|
| `Repository` | 登録ファイルにあるリポジトリ ID（例：`h2-test`） |
| `Allowed files` | 変更してよいファイルの glob。1行に1つ（例：`- src/h2/**`） |
| `Non-goals`（任意） | やらないこと |
| `Inputs`（任意） | 入力データや既存コード |

`.autopilot/experiment.json` は、システムが定めたファイルとして常に変更範囲に含まれる。

### 実行承認

Phase 2 を **apply モード**で実行し（Linear が In Progress になる）、その handoff を使う。dry-run の handoff では実装しない。

## 3. コマンド

```bash
uv run research-agent implement --task INU-123           # Prefect flow として実行（履歴は UI に残る）
uv run research-agent implement --handoff <path> --local
uv run research-agent implement --resume <phase3_run_id>  # 中断した run を再開（同じ run ID で続ける）
uv run research-agent repair <failure_report.json>        # Phase 4 → 3 の修正
uv run research-agent impl-runs list [--task INU-123]
uv run research-agent impl-runs show <phase3_run_id>
```

終了コード：0 completed、2 blocked、4 interrupted、5 failed。blocked のときは ntfy に `approval_required` を送る（人間の判断が必要なため）。

## 4. モデルと上限（環境変数で変更できる）

| 項目 | 既定値 | 変数 |
|---|---|---|
| Implementer（Codex） | low → `gpt-5.6-luna`、medium → `gpt-5.6-terra`、high → `gpt-5.6-sol` | `RA_CODEX_MODEL_LIGHT/STANDARD/HIGH` |
| fallback（Claude） | `claude-haiku-4-5-20251001` / `claude-sonnet-5-5` / `claude-opus-5-5` | `RA_CLAUDE_MODEL_*` |
| Reviewer | 科学的リスク low → standard、medium 以上 → high（既定は Codex の read-only セッション） | `RA_IMPL_REVIEWER_BACKEND` |
| 修正 / 試行 | 2 / 3 | `RA_IMPL_MAX_REVISIONS` / `RA_IMPL_MAX_ATTEMPTS` |
| 実行時間 | 1800 秒 | `RA_IMPL_WALL_TIME_SECONDS` |
| トークン / 費用 | 300万 / $5（費用は CLI が報告するものだけ計上） | `RA_IMPL_MAX_TOKENS` / `RA_IMPL_MAX_COST_USD` |
| Claude の1回あたりの上限 | $2（`--max-budget-usd`） | `RA_CLAUDE_MAX_BUDGET_USD` |

Codex を ChatGPT ログイン（サブスクリプション）で使う場合、費用は unknown と記録し、回数・時間・トークン数で制御する。上限はリポジトリのチェーン全体（元の run と修正 run の合計）で数える。

## 5. 保存先

| データ | 場所 |
|---|---|
| manifest（JSON） | `$RA_STATE_DIR/implementation/<run>/manifest.json`。各試行のログと diff、`failure_report.json` も同じ場所 |
| manifest（DB） | `research_autopilot.implementation_runs`（`failure_report_id` は unique） |
| ExperimentSpec | `$RA_STATE_DIR/experiment_specs/<task>/<spec_id>.json` |
| Phase 4 用の handoff | `$RA_STATE_DIR/phase4_handoffs/<task>/<run>.json` |
| worktree | `$RA_WORKTREES_DIR/<repo>/<task>-<run8>`（ブランチは `autopilot/<task>-<run8>`） |

## 6. Phase 4 が使うインターフェース

### 6.1 入力：Phase4Handoff と ExperimentSpec（schema 1.0）

Phase 4 は `phase4_handoffs/<task>/<run>.json` を読み、`experiment_spec_path` にある ExperimentSpec から起動条件を得る。**LLM の推論は要らない。**

```text
ExperimentSpec
  identification: task_id, phase2_plan_run_id, plan_id, phase3_run_id, repository_id, repository_url, branch, commit
  execution:   argv[], working_directory, setup[[argv]], env_var_names[]（名前だけ）
  environment: type="python", python_version, dependency_files[], install_argv?
  inputs[]:    path, kind(config|data|code), scientific(bool), description
  resources:   cpu / memory_gb / gpu / walltime_minutes の各項目に {value, basis: required|estimated, rationale, confidence},
               parallelizable, parallel_notes, job_dependencies[]
  outputs[]:   path, format, required, units{field: unit}, description, validation
  validation:  acceptance_criteria[] / scientific_invariants[]（タスク仕様から逐語転記）, metrics[], numeric_checks[], output_integrity[]
  reproduction[], definition_path, supersedes_spec_id?, sources[]
```

- 実行は `git checkout <commit>` → `environment.install_argv` → `execution.setup` → `execution.argv`（`working_directory` で、シェルを使わずに実行）。
- `resources` は Phase 3 の見積もり。Phase 4 は利用できる資源を検出して最終的な割り当てを決める（`basis` と `confidence` を参照）。
- numeric_checks の条件：`lt` / `le` / `gt` / `ge` / `eq` / `abs_le` / `finite`。右辺は `value` か、同じ出力内の `reference_pointer`（JSON pointer）。`tolerance` は任意。

### 6.2 出力：FailureReport（schema 1.0）

```text
report_id（一意。同じ ID は二度処理されない）, task_id, phase3_run_id, phase4_run_id,
failed_commit, experiment_spec_id, argv[], input_config{path: sha/説明}, exit_code, signal,
error_class: infrastructure_failure | environment_failure | implementation_failure | numerical_failure |
             scientific_validation_failure | resource_exceeded | unknown,
stdout_ref, stderr_ref, artifacts[], summary, log_excerpt（8000文字以内、秘密を含めない）, reproduction[],
allowed_repair_scope[]（元の承認範囲の内側に限る）, scientific_invariants[]（元の仕様と同一）, roundtrip（1始まり）
```

`research-agent repair <file>` またはデプロイ `repair-task/manual -p report_path=...` に渡す。結果は新しい ImplementationManifest（`parent_run_id`、`failure_report_id`、`repair_round`）、新しい commit、新しい ExperimentSpec（`supersedes_spec_id`）になる。

| 判定（blocked の `failure_classification`） | 意味 | Phase 4 / 5 の対応 |
|---|---|---|
| `ok` → completed | 修正済み。新しい ExperimentSpec | 新しい spec で再実行 |
| `human_required` | 科学的な原因の可能性がある | 人間に判断を依頼 |
| `phase4_responsibility` | インフラ・資源の問題 | Phase 4 側で再試行・再割り当て |
| `scope_exceeds_approval` / `scope_touches_scientific_inputs` / `invariants_mismatch` | 依頼が承認範囲外 | 依頼内容を見直すか、人間の承認を得る |
| `roundtrip_limit` | 2往復を超えた | 人間に判断を依頼 |
| `original_run_invalid` | commit / spec / run が一致しない | 依頼の作成に誤りがある |
| `unapproved_change`（修正中） | 科学に関わる部分を変えようとした | 人間に判断を依頼 |

## 7. 安全性

- main / master にはチェックアウトも commit も merge もしない。push もしない。Git 操作はすべて manifest の `git_audit` に記録する。
- 1つの worktree はロックファイル（pid ＋ host）で1プロセスに限る。fallback の前には、旧プロセスが終了したことを確認する。
- 許可範囲外のファイル、保護パス、危険な追加（git push、sudo、curl | sh、秘密値）を検出したら blocked にする。モデルの昇格や fallback で回避することはしない。
- 生成物（`__pycache__`、`.pytest_cache` など）は差分と commit から除く。
- Phase 3 は Linear / Notion を更新しない（Done にしない）。

## 8. Eagle に反映するときに必要な設定（今回は未実施）

- worker イメージに Codex CLI と Claude Code CLI を入れ、認証する。ChatGPT ログインや claude.ai ログインの資格情報を worker の環境に置く方法と利用条件は**まだ検証していない**。
- worker から研究リポジトリへのアクセス：clone の置き場所（volume）、`config/research_repos.toml`、`RA_WORKTREES_DIR`（volume）。研究リポジトリの依存環境（uv / venv）も必要。
- `.env`：`RA_REPOS_FILE`、`RA_WORKTREES_DIR`、必要なら `RA_IMPL_*` と `RA_*_MODEL_*`。
- deployment：`implement-task/manual`、`repair-task/manual`（`prefect.yaml`）。worker の同時実行数は1のまま。
